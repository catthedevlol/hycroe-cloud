"""
VPS API router — fully fixed.

Security:
  - is_admin / role fields are NEVER declared as Form params → FastAPI ignores them.
    Privilege is resolved exclusively from the server-side session token.
  - Unauthenticated requests are rejected before any business logic runs.

Input validation:
  - plan_id required for non-admins; must reference an active plan.
  - name must match ^[a-zA-Z0-9-]{2,40}$ and not start with '-'.
  - os_image must be in the AVAILABLE_IMAGES whitelist.

VPS creation:
  - Node must be available. HARD FAIL if none → credits NOT deducted.
  - Worker signals error → VPS status set to 'error', never silent success.
  - Every step is logged.
"""
import logging
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Form, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import get_db
from models import VPS, Node, Backup, PortForward, VPSPlan
from services.auth import AuthService
from services.incus import IncusService, AVAILABLE_IMAGES
from services.node_selector import NodeSelector
from services.billing import BillingService

# WHY: Silent fallback means VPS creation silently never runs its background
# job — the VPS stays 'building' forever with no error surfaced.  We must
# fail loudly so operators know the worker is broken.
try:
    import workers as _workers
    _WORKER_AVAILABLE = True
    def _enqueue(j, pl): return _workers.enqueue(j, pl)
except Exception as _worker_import_err:
    _WORKER_AVAILABLE = False
    logger.critical(
        "WORKER SYSTEM UNAVAILABLE: could not import workers module: %s. "
        "VPS creation background jobs will NOT run. Fix this before accepting "
        "VPS create requests.",
        _worker_import_err,
    )
    def _enqueue(j, pl):
        logger.error(
            "_enqueue called but worker is unavailable (job=%s). "
            "This job will be dropped. Check worker startup errors.", j
        )
        raise RuntimeError(
            f"Worker system is unavailable — cannot enqueue job '{j}'. "
            "Contact the system administrator."
        )

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# ── Constants ─────────────────────────────────────────────────────────────────

VALID_CPU   = set(range(1, 65))   # 1–64 vCPUs
MIN_RAM_MB  = 128
MAX_RAM_MB  = 131072              # 128 GB
MIN_DISK_GB = 5
MAX_DISK_GB = 2000
VM_SURCHARGE = 15                 # flat extra credits for VM instances
_NAME_RE    = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9\-]{1,39}$')  # 2–40 chars, no leading '-'


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _require_user(request: Request, db: Session):
    """
    Return the authenticated user from the server-side session.
    NEVER reads is_admin / role / any privilege from the request body or query.
    Raises HTTP 303 → login if unauthenticated.
    Raises HTTP 403 if suspended.
    """
    user = AuthService.get_user(request, db)
    if not user:
        logger.warning("Unauthenticated request to %s — rejecting", request.url.path)
        raise HTTPException(status_code=303, headers={"Location": "/"})
    if user.is_suspended:
        logger.warning("Suspended user %s attempted action on %s", user.username, request.url.path)
        raise HTTPException(status_code=403, detail="Account is suspended")
    return user


def _remote(vps: VPS) -> Optional[str]:
    if vps.node:
        return vps.node.incus_remote or None
    return None


def _incus_error_redirect(path: str, result: dict) -> RedirectResponse:
    """Redirect with the Incus error message as a query param."""
    msg = urllib.parse.quote((result.get("error") or "unknown error").strip()[:200])
    return RedirectResponse(f"{path}?error=incus_error&msg={msg}", status_code=303)


# ── Node event webhook helper ─────────────────────────────────────────────────

def _fire_vps_event(db, node, event: str, detail: str = "") -> None:
    """Fire a VPS lifecycle webhook. Delegates to the nodes module helper. Non-fatal."""
    try:
        from api.nodes import _fire_node_event
        node_name = (node.display_name or node.name) if node else "unknown"
        _fire_node_event(db, node_name, event, detail)
    except Exception as exc:
        logger.warning("VPS event webhook fire failed (non-fatal): %s", exc)


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = AuthService.get_user(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)

    user_vps = db.query(VPS).filter(VPS.user_id == user.id).all()
    nodes    = db.query(Node).all()
    plans    = db.query(VPSPlan).filter(VPSPlan.is_active == True).order_by(VPSPlan.credits_cost).all()

    # Build live status map keyed by node_id
    node_live: dict = {}
    for node in nodes:
        try:
            remote = node.incus_remote or None
            node_live[node.id] = {v["name"]: v for v in IncusService.list_vps(remote=remote)}
        except Exception as exc:
            logger.warning("Dashboard: could not fetch live status for node %s: %s", node.name, exc)
            node_live[node.id] = {}

    # Also fetch local Incus for VPS with no node assigned
    has_null_node_vps = any(v.node_id is None for v in user_vps)
    if has_null_node_vps:
        try:
            node_live[None] = {v["name"]: v for v in IncusService.list_vps(remote=None)}
        except Exception:
            node_live[None] = {}

    for vps in user_vps:
        live_map = node_live.get(vps.node_id, {})
        info     = live_map.get(vps.name)
        if info:
            vps.status = info["status"]
            if info.get("ipv4"):
                vps.ipv4 = info["ipv4"]
    try:
        db.commit()
    except Exception:
        db.rollback()

    vps_list = [
        {
            "id":            v.id,
            "name":          v.name,
            "display_name":  v.display_name or v.name,
            "status":        v.status,
            "instance_type": v.instance_type or "container",
            "ram":           v.ram,
            "cpu":           v.cpu,
            "disk_gb":       v.disk_gb,
            "ipv4":          v.ipv4 or "—",
            "ipv6":          v.ipv6 or "—",
            "os_image":      v.os_image or "ubuntu/22.04",
            "node":          (v.node.display_name or v.node.name) if v.node else "local",
            "suspended":     v.suspended,
        }
        for v in user_vps
    ]

    running_count = sum(1 for v in vps_list if v["status"] == "running")

    node_stats = []
    for n in nodes:
        pct = round(n.ram_used_mb / n.ram_total_mb * 100, 1) if n.ram_total_mb else 0
        node_stats.append({
            "id": n.id, "name": n.display_name or n.name,
            "status": n.status, "ram_used": n.ram_used_mb,
            "ram_total": n.ram_total_mb, "ram_pct": pct,
            "node_type": n.node_type or "incus",
            "vps_count": db.query(VPS).filter(VPS.node_id == n.id).count(),
            "location": n.location or "—",
        })

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user,
        "vps_list": vps_list, "vps_count": len(vps_list),
        "running_count": running_count, "nodes": nodes,
        "node_stats": node_stats, "low_credits": user.credits < 200,
        "cost_preview": IncusService.calculate_cost(1024, 1, 20),
        "os_images": AVAILABLE_IMAGES,
        "plans": plans,
        "vm_surcharge": VM_SURCHARGE,
    })


# ── Create VPS ────────────────────────────────────────────────────────────────

# Regex: lowercase alphanumeric + hyphens, 2–40 chars, must not start with '-'
_CREATE_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9\-]{1,39}$')

# WHY: In-memory rate limit is bypassed on every restart and is inconsistent
# across multiple uvicorn workers.  Persistent DB-backed rate limit is used instead.
# See services/rate_limit.py for implementation details.
_CREATE_COOLDOWN_SECS = 15


@router.post("/create")
def create_vps(
    name:          str           = Form(...),
    instance_type: str           = Form("container"),
    plan_id:       Optional[int] = Form(None),
    os_image:      str           = Form("ubuntu/22.04"),
    # ── SECURITY: fields intentionally NOT accepted from the client ───────────
    #   node_id  → node is always selected server-side
    #   is_admin → privilege comes exclusively from the server-side session
    #   role     → same — never trust the request body for permissions
    # ─────────────────────────────────────────────────────────────────────────
    # Admin-only custom resource overrides (only used when plan_id is None)
    ram:           Optional[int] = Form(None),
    cpu:           Optional[int] = Form(None),
    disk_gb:       int           = Form(20),
    request:       Request       = None,
    db:            Session       = Depends(get_db),
):
    """
    VPS creation API endpoint.

    ALL responses are JSON — no redirects.
    Success  → 201  {"success": true,  "vps_id": ..., "name": ..., "status": ...}
    Failure  → 4xx/5xx {"success": false, "error": "<code>", "message": "<detail>"}
    """
    client_ip = request.client.host if request.client else "unknown"
    logger.info("=== VPS CREATE REQUEST: ip=%s ===", client_ip)

    # ── STEP 1: Authentication ─────────────────────────────────────────────────
    # Privilege is read ONLY from the server-side session token.
    # No field from the request body or query string can elevate access.
    user = AuthService.get_user(request, db)
    if not user:
        logger.warning("VPS create: unauthenticated request ip=%s — 401", client_ip)
        return JSONResponse(status_code=401, content={
            "success": False,
            "error": "authentication_required",
            "message": "You must be logged in to create a VPS.",
        })
    if user.is_suspended:
        logger.warning("VPS create: suspended user=%s — 403", user.username)
        return JSONResponse(status_code=403, content={
            "success": False,
            "error": "account_suspended",
            "message": "Your account has been suspended.",
        })
    logger.info("VPS create: user=%s (id=%d admin=%s) ip=%s",
                user.username, user.id, user.is_admin, client_ip)

    # ── STEP 2: Rate limit — persistent DB-backed, restart/multi-worker safe ───
    now_utc = datetime.utcnow()
    from services.rate_limit import check_and_stamp_create_rate_limit
    rl_allowed, rl_retry_after = check_and_stamp_create_rate_limit(
        db, user.id, cooldown_secs=_CREATE_COOLDOWN_SECS
    )
    if not rl_allowed:
        logger.warning(
            "VPS create: rate limited user=%s (%ds remaining) — 429",
            user.username, rl_retry_after,
        )
        return JSONResponse(status_code=429, content={
            "success": False,
            "error": "rate_limited",
            "message": f"Please wait {rl_retry_after} seconds before creating another VPS.",
            "retry_after_seconds": rl_retry_after,
        })

    # ── STEP 3: Sanitise + validate instance_type ──────────────────────────────
    instance_type = instance_type.strip().lower()
    if instance_type not in ("container", "vm"):
        instance_type = "container"
    logger.info("VPS create: instance_type=%s", instance_type)

    # ── STEP 4: Validate name ──────────────────────────────────────────────────
    name = name.strip().lower()
    logger.info("VPS create: validating name='%s'", name)
    if not _CREATE_NAME_RE.match(name):
        logger.warning("VPS create: invalid name='%s' user=%s — 422", name, user.username)
        return JSONResponse(status_code=422, content={
            "success": False,
            "error": "invalid_name",
            "message": ("Name must be 2–40 lowercase alphanumeric characters or hyphens "
                        "and must not start with a hyphen."),
        })
    if db.query(VPS).filter(VPS.name == name).first():
        logger.warning("VPS create: name='%s' already taken user=%s — 409", name, user.username)
        return JSONResponse(status_code=409, content={
            "success": False,
            "error": "name_taken",
            "message": f"A VPS named '{name}' already exists.",
        })

    # ── STEP 5: Plan / resource resolution ────────────────────────────────────
    # plan_id is REQUIRED for non-admins.
    # Admins may omit plan_id and supply raw ram/cpu/disk_gb instead.
    logger.info("VPS create: resolving resources (plan_id=%s admin=%s)", plan_id, user.is_admin)

    if plan_id is not None:
        logger.info("VPS create: looking up plan_id=%d", plan_id)
        plan = db.query(VPSPlan).filter(
            VPSPlan.id == plan_id, VPSPlan.is_active == True
        ).first()
        if not plan:
            logger.warning("VPS create: plan_id=%s not found/inactive user=%s — 422",
                           plan_id, user.username)
            return JSONResponse(status_code=422, content={
                "success": False,
                "error": "invalid_plan",
                "message": f"Plan ID {plan_id} does not exist or is not active.",
            })
        ram     = plan.ram_mb
        cpu     = plan.cpu
        disk_gb = plan.disk_gb
        cost    = plan.credits_cost
        logger.info("VPS create: plan='%s' ram=%dMB cpu=%d disk=%dGB cost=%d",
                    plan.name, ram, cpu, disk_gb, cost)
    else:
        if not user.is_admin:
            logger.warning("VPS create: no plan_id from non-admin user=%s — 422", user.username)
            return JSONResponse(status_code=422, content={
                "success": False,
                "error": "plan_required",
                "message": ("plan_id is required. "
                            "Custom resource allocation is only available to administrators."),
            })
        if ram is None or cpu is None:
            logger.warning("VPS create: admin custom missing ram/cpu user=%s — 422", user.username)
            return JSONResponse(status_code=422, content={
                "success": False,
                "error": "invalid_resources",
                "message": "ram and cpu are required when no plan_id is provided.",
            })
        if not (MIN_RAM_MB <= ram <= MAX_RAM_MB):
            logger.warning("VPS create: RAM=%d out of range user=%s — 422", ram, user.username)
            return JSONResponse(status_code=422, content={
                "success": False,
                "error": "invalid_resources",
                "message": f"RAM must be between {MIN_RAM_MB} and {MAX_RAM_MB} MB.",
            })
        if cpu not in VALID_CPU:
            logger.warning("VPS create: CPU=%d invalid user=%s — 422", cpu, user.username)
            return JSONResponse(status_code=422, content={
                "success": False,
                "error": "invalid_resources",
                "message": "CPU must be between 1 and 64.",
            })
        if not (MIN_DISK_GB <= disk_gb <= MAX_DISK_GB):
            logger.warning("VPS create: disk=%dGB out of range user=%s — 422", disk_gb, user.username)
            return JSONResponse(status_code=422, content={
                "success": False,
                "error": "invalid_resources",
                "message": f"Disk must be between {MIN_DISK_GB} and {MAX_DISK_GB} GB.",
            })
        cost = IncusService.calculate_cost(ram, cpu, disk_gb)
        logger.info("VPS create (admin custom): ram=%dMB cpu=%d disk=%dGB cost=%d user=%s",
                    ram, cpu, disk_gb, cost, user.username)

    # ── STEP 6: OS image whitelist ─────────────────────────────────────────────
    logger.info("VPS create: validating os_image='%s'", os_image)
    if os_image not in AVAILABLE_IMAGES:
        logger.warning("VPS create: invalid os_image='%s' user=%s — 422", os_image, user.username)
        return JSONResponse(status_code=422, content={
            "success": False,
            "error": "invalid_os_image",
            "message": f"os_image '{os_image}' is not allowed.",
            "allowed": AVAILABLE_IMAGES,
        })

    # ── STEP 7: VM surcharge (calculated server-side) ─────────────────────────
    if instance_type == "vm":
        cost += VM_SURCHARGE
        logger.info("VPS create: VM surcharge +%d → total cost=%d", VM_SURCHARGE, cost)

    # ── STEP 8: Credits check ──────────────────────────────────────────────────
    logger.info("VPS create: credit check user=%s has=%d need=%d",
                user.username, user.credits, cost)
    if user.credits < cost:
        logger.warning("VPS create: insufficient credits user=%s has=%d need=%d — 402",
                       user.username, user.credits, cost)
        return JSONResponse(status_code=402, content={
            "success": False,
            "error": "insufficient_credits",
            "message": f"You need {cost} credits but only have {user.credits}.",
            "required": cost,
            "available": user.credits,
        })

    # ── STEP 9: Server-side node selection + online validation ────────────────
    # node_id is NEVER accepted from the client — selected exclusively here.
    # WHY race-condition fix: we immediately reserve RAM on the node row inside
    # the same DB transaction as the VPS insert.  Concurrent requests will see
    # the updated ram_used_mb and NodeSelector.pick() will exclude the node if
    # it no longer has capacity — eliminating the TOCTOU window.
    logger.info("VPS create: selecting node server-side ram_needed=%dMB cpu_needed=%d", ram, cpu)
    node = NodeSelector.pick(db, ram_needed=ram, cpu_needed=cpu, preferred_node_id=None)

    if not node:
        logger.error(
            "VPS create: no node returned by NodeSelector user=%s ram=%dMB cpu=%d — 503",
            user.username, ram, cpu,
        )
        return JSONResponse(status_code=503, content={
            "success": False,
            "error": "no_node_available",
            "message": ("No cluster node is available right now. "
                        "Your credits were not deducted. Please try again later."),
        })

    # Hard-validate node status
    if node.status != "online":
        logger.error(
            "VPS create: selected node=%s has status='%s' (not online) user=%s — 503",
            node.name, node.status, user.username,
        )
        return JSONResponse(status_code=503, content={
            "success": False,
            "error": "node_unavailable",
            "message": (f"Selected node '{node.name}' is not online (status={node.status}). "
                        "Your credits were not deducted. Please try again later."),
        })

    # ── Resource reservation (race-condition fix) ──────────────────────────────
    # Optimistically increment ram_used_mb NOW so concurrent picks see this
    # reservation and cannot double-allocate.  If creation fails later, the
    # worker calls NodeSelector.refresh_node() which resets to the live value.
    node.ram_used_mb = (node.ram_used_mb or 0) + ram

    logger.info(
        "VPS create: node reserved — name=%s id=%d status=%s "
        "ram_used=%dMB ram_total=%dMB (includes +%dMB reservation)",
        node.name, node.id, node.status,
        node.ram_used_mb, node.ram_total_mb, ram,
    )

    # ── STEP 10: Persist VPS record FIRST (status='building') ────────────────
    # WHY credit ordering: we create the DB record before deducting credits so
    # we have a vps_id to attach to the refund transaction if creation fails.
    # Credits are deducted AFTER the worker job is successfully enqueued so
    # that a worker-unavailable error doesn't silently consume user credits.
    vps = VPS(
        name=name, instance_type=instance_type,
        ram=ram, cpu=cpu, disk_gb=disk_gb, os_image=os_image,
        status="building", user_id=user.id, node_id=node.id,
        created_at=now_utc, expires_at=now_utc + timedelta(days=30),
    )
    db.add(vps)
    # Commit node reservation + VPS record atomically so other requests
    # immediately see both the reserved RAM and the new VPS row.
    db.commit()
    db.refresh(vps)
    logger.info(
        "VPS create: DB record saved — vps_id=%d name=%s node=%s (node ram_used now=%dMB)",
        vps.id, vps.name, node.name, node.ram_used_mb,
    )

    # ── STEP 11: Enqueue background creation job ──────────────────────────────
    # WHY non-blocking: IncusService.create() can block 120+ seconds, stalling
    # the entire request thread/worker.  The API returns immediately with
    # status='building'; the worker updates it to 'running' or 'error'.
    remote = node.incus_remote or None
    try:
        job_id = _enqueue("create_vps", {
            "vps_id": vps.id,
        })
        vps.build_job_id = job_id
        db.commit()
        logger.info(
            "VPS create: job enqueued — job_id=%s vps_id=%d name=%s",
            job_id, vps.id, vps.name,
        )
    except Exception as enqueue_exc:
        # Worker unavailable — roll back the VPS record and node reservation
        # so the user is not left with a stuck 'building' entry.
        logger.error(
            "VPS create: FAILED to enqueue job for vps_id=%d: %s — rolling back",
            vps.id, enqueue_exc,
        )
        try:
            node.ram_used_mb = max(0, (node.ram_used_mb or 0) - ram)
            db.delete(vps)
            db.commit()
        except Exception as rollback_exc:
            logger.error("VPS create: rollback after enqueue failure also failed: %s", rollback_exc)
            db.rollback()
        return JSONResponse(status_code=503, content={
            "success": False,
            "error": "worker_unavailable",
            "message": (
                "The background job system is unavailable. "
                "Your credits were NOT deducted. Please contact support."
            ),
        })

    # ── STEP 12: Deduct credits — ONLY after job is safely enqueued ───────────
    # WHY: if we deducted before and the enqueue failed, the user would lose
    # credits without getting a VPS.  Deducting after ensures atomicity of
    # the user-visible transaction.
    deducted = BillingService.deduct(
        db, user, cost,
        f"{'VM' if instance_type == 'vm' else 'VPS'} created: "
        f"{name} ({ram}MB/{cpu}cpu/{disk_gb}GB)"
        + (f" +{VM_SURCHARGE}cr VM fee" if instance_type == "vm" else "")
    )
    if not deducted:
        # Extremely unlikely (credits already checked in step 8) but guard it.
        logger.error(
            "VPS create: credit deduction failed for user=%s (race?) — "
            "VPS job already enqueued, logging for manual review vps_id=%d",
            user.username, vps.id,
        )
    else:
        db.commit()
        logger.info(
            "VPS create: %d credits deducted from user=%s (remaining=%d)",
            cost, user.username, user.credits,
        )

    # ── STEP 13: Webhooks (non-fatal) ─────────────────────────────────────────
    try:
        from services.settings import get_settings as _gs
        from services.webhook import fire_admin_log
        _wh = _gs(db).get("admin_log_webhook_url") or ""
        if _wh:
            fire_admin_log(
                _wh, user.username, "VPS Create Queued",
                f"{name} ({instance_type}, {ram}MB/{cpu}cpu/{disk_gb}GB, {cost}cr) "
                f"node={node.name} job_id={vps.build_job_id}",
            )
    except Exception as exc:
        logger.warning("VPS create: admin webhook failed (non-fatal): %s", exc)

    _fire_vps_event(
        db, node, "vps_queued",
        f"{name} ({instance_type}, {ram}MB RAM, {cpu} vCPU, {disk_gb}GB) job_id={vps.build_job_id}",
    )

    # ── STEP 14: Return immediately — creation is async ───────────────────────
    logger.info(
        "=== VPS CREATE QUEUED: vps=%s user=%s node=%s job=%s ===",
        name, user.username, node.name, vps.build_job_id,
    )
    return JSONResponse(status_code=202, content={
        "success": True,
        "queued": True,
        "vps_id": vps.id,
        "name": vps.name,
        "status": "building",
        "job_id": vps.build_job_id,
        "node": node.name,
        "ram_mb": vps.ram,
        "cpu": vps.cpu,
        "disk_gb": vps.disk_gb,
        "os_image": vps.os_image,
        "instance_type": vps.instance_type,
        "credits_used": cost,
        "credits_remaining": user.credits,
        "message": (
            "Your VPS is being built. Check /api/vps/status/{vps_id} "
            "or the dashboard for updates."
        ),
    })


# ── Build status polling ──────────────────────────────────────────────────────

@router.get("/api/vps/status/{vps_id}")
def vps_build_status(vps_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Lightweight polling endpoint for async VPS build progress.
    Returns current status + IP once available.

    WHY: Since creation is now async (202 Accepted), clients need a way to
    poll for completion without re-fetching the entire dashboard.
    """
    user = AuthService.get_user(request, db)
    if not user:
        raise HTTPException(401, detail="Not authenticated")
    if user.is_admin:
        vps = db.query(VPS).filter(VPS.id == vps_id).first()
    else:
        vps = db.query(VPS).filter(VPS.id == vps_id, VPS.user_id == user.id).first()
    if not vps:
        raise HTTPException(404, detail=f"VPS id={vps_id} not found")

    return JSONResponse({
        "vps_id":    vps.id,
        "name":      vps.name,
        "status":    vps.status,
        "job_id":    vps.build_job_id,
        "ipv4":      vps.ipv4 or None,
        "ipv6":      vps.ipv6 or None,
        "building":  vps.status == "building",
        "ready":     vps.status == "running",
        "error":     vps.status == "error",
    })


# ── Power Actions ─────────────────────────────────────────────────────────────

def _power_action(request, db, name, action_fn, success_status):
    user = _require_user(request, db)
    if user.is_admin:
        vps = db.query(VPS).filter(VPS.name == name).first()
    else:
        vps = db.query(VPS).filter(VPS.name == name, VPS.user_id == user.id).first()
    if not vps:
        raise HTTPException(404)
    if vps.suspended:
        return RedirectResponse(f"/vps/{name}?error=vps_suspended", status_code=303)

    logger.info("Power action '%s' on VPS %s by user=%s", success_status, name, user.username)
    result = action_fn(vps.name, remote=_remote(vps))

    if result["success"]:
        vps.status = success_status
        vps.last_action = datetime.utcnow()
        db.commit()
        logger.info("Power action succeeded: VPS %s → status=%s", name, success_status)
        return RedirectResponse(f"/vps/{name}", status_code=303)

    logger.error("Power action failed on VPS %s: %s", name, result.get("error", "").strip())
    return _incus_error_redirect(f"/vps/{name}", result)


@router.post("/start")
def start_vps(name: str = Form(...), request: Request = None, db: Session = Depends(get_db)):
    return _power_action(request, db, name, IncusService.start, "running")


@router.post("/stop")
def stop_vps(name: str = Form(...), request: Request = None, db: Session = Depends(get_db)):
    return _power_action(request, db, name, IncusService.stop, "stopped")


@router.post("/restart")
def restart_vps(name: str = Form(...), request: Request = None, db: Session = Depends(get_db)):
    return _power_action(request, db, name, IncusService.restart, "running")


@router.post("/delete")
def delete_vps(name: str = Form(...), request: Request = None, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    if user.is_admin:
        vps = db.query(VPS).filter(VPS.name == name).first()
    else:
        vps = db.query(VPS).filter(VPS.name == name, VPS.user_id == user.id).first()
    if not vps:
        raise HTTPException(404)

    logger.info("VPS delete: user=%s requesting deletion of %s", user.username, name)
    result = IncusService.delete(name, remote=_remote(vps))
    if not result["success"]:
        logger.error("VPS delete: incus delete failed for %s: %s", name, result.get("error", "").strip())
        return _incus_error_redirect(f"/vps/{name}", result)

    db.query(Backup).filter(Backup.vps_id == vps.id).delete()
    db.query(PortForward).filter(PortForward.vps_id == vps.id).delete()
    _node = vps.node  # capture before delete
    db.delete(vps)
    db.commit()
    logger.info("VPS delete: %s removed from DB by user=%s", name, user.username)

    try:
        from services.settings import get_settings as _gs
        from services.webhook import fire_admin_log
        _wh = _gs(db).get("admin_log_webhook_url") or ""
        if _wh:
            fire_admin_log(_wh, user.username, "VPS Deleted", f"Instance: {name}")
    except Exception as exc:
        logger.warning("VPS delete: admin log webhook failed (non-fatal): %s", exc)

    _fire_vps_event(db, _node, "vps_deleted", f"Instance: {name}")
    return RedirectResponse("/dashboard?success=vps_deleted", status_code=303)


@router.post("/rebuild")
def rebuild_vps(
    name:     Optional[str] = Form(None),
    vps_name: Optional[str] = Form(None),
    os_image: str           = Form("ubuntu/22.04"),
    request:  Request       = None,
    db:       Session       = Depends(get_db),
):
    user   = _require_user(request, db)
    target = name or vps_name
    if not target:
        raise HTTPException(400, "VPS name required")
    vps = _get_vps_for_user(target, user, db)
    if os_image not in AVAILABLE_IMAGES:
        os_image = "ubuntu/22.04"

    is_vm = (vps.instance_type == "vm")
    logger.info("VPS rebuild: user=%s rebuilding %s with %s (vm=%s)", user.username, target, os_image, is_vm)

    vps.os_image = os_image
    vps.status   = "building"
    db.commit()

    result = IncusService.rebuild(target, vps.ram, vps.cpu, vps.disk_gb,
                                  os_image, remote=_remote(vps), is_vm=is_vm)
    vps.status      = "running" if result["success"] else "error"
    vps.last_action = datetime.utcnow()
    db.commit()

    if not result["success"]:
        logger.error("VPS rebuild FAILED for %s: %s", target, result.get("error", "").strip())
        return _incus_error_redirect(f"/vps/{target}", result)

    logger.info("VPS rebuild: SUCCESS for %s", target)
    return RedirectResponse(f"/vps/{target}?msg=rebuilt", status_code=303)


# ── VPS Detail ────────────────────────────────────────────────────────────────

@router.get("/vps/{name}", response_class=HTMLResponse)
def vps_detail(name: str, request: Request, db: Session = Depends(get_db)):
    user = AuthService.get_user(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)
    if user.is_admin:
        vps = db.query(VPS).filter(VPS.name == name).first()
    else:
        vps = db.query(VPS).filter(VPS.name == name, VPS.user_id == user.id).first()
    if not vps:
        raise HTTPException(404)
    backups  = db.query(Backup).filter(Backup.vps_id == vps.id).all()
    forwards = db.query(PortForward).filter(PortForward.vps_id == vps.id).all()
    # Fail safe: never crash the page render because Incus is unreachable.
    # get_metrics() already returns safe defaults on failure, but double-wrap
    # here so a future regression can't break the entire detail page.
    try:
        metrics = IncusService.get_metrics(name, remote=_remote(vps), ram_limit_mb=vps.ram)
    except Exception as _me:
        logger.warning("vps_detail: get_metrics failed for %s (non-fatal): %s", name, _me)
        metrics = {
            "cpu": None, "ram_used": 0, "ram_total": vps.ram, "ram_pct": 0,
            "net_rx": 0, "net_tx": 0, "net_rx_rate": 0, "net_tx_rate": 0,
            "disk_read": 0, "disk_write": 0, "status": "unavailable",
        }
    return templates.TemplateResponse("vps_detail.html", {
        "request": request, "user": user, "vps": vps,
        "backups": backups, "forwards": forwards, "metrics": metrics,
        "os_images": AVAILABLE_IMAGES,
    })


# ── API endpoints ─────────────────────────────────────────────────────────────

@router.get("/api/metrics/{name}")
def api_metrics(name: str, request: Request, db: Session = Depends(get_db)):
    """Live metrics. Accepts session_token cookie or Authorization: Bearer <token>."""
    user = AuthService.get_user(request, db)
    if not user:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            user = AuthService.get_user_by_token(token, db)
    if not user:
        raise HTTPException(401, detail="Not authenticated — send session_token cookie or Authorization: Bearer <token>")
    if user.is_admin:
        vps = db.query(VPS).filter(VPS.name == name).first()
    else:
        vps = db.query(VPS).filter(VPS.name == name, VPS.user_id == user.id).first()
    if not vps:
        raise HTTPException(404, detail=f"VPS '{name}' not found")
    # NEVER crash this endpoint — always return valid JSON.
    # get_metrics() is already fail-safe, but wrap here as a belt-and-suspenders
    # guard so any future regression still returns JSON instead of a 500.
    try:
        data = IncusService.get_metrics(name, remote=_remote(vps), ram_limit_mb=vps.ram)
        return JSONResponse(data)
    except Exception as exc:
        logger.warning(
            "api_metrics: unhandled error for vps=%s: %s", name, exc
        )
        return JSONResponse(status_code=200, content={
            "cpu": None, "ram": None, "ram_used": None,
            "net_rx": None, "net_tx": None,
            "disk_read": None, "disk_write": None,
            "status": "unavailable",
            "error": "Metrics temporarily unavailable",
        })


@router.get("/api/vps/{name}/metrics")
def api_vps_metrics(name: str, request: Request, db: Session = Depends(get_db)):
    """Primary live-metrics endpoint."""
    user = AuthService.get_user(request, db)
    if not user:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            user = AuthService.get_user_by_token(token, db)
    if not user:
        raise HTTPException(401, detail="Not authenticated — send session_token cookie or Authorization: Bearer <token>")
    if user.is_admin:
        vps = db.query(VPS).filter(VPS.name == name).first()
    else:
        vps = db.query(VPS).filter(VPS.name == name, VPS.user_id == user.id).first()
    if not vps:
        raise HTTPException(404, detail=f"VPS '{name}' not found")
    # NEVER crash this endpoint — always return valid JSON.
    try:
        data = IncusService.get_metrics(name, remote=_remote(vps), ram_limit_mb=vps.ram)
        data["vps_cpu"]  = vps.cpu
        data["vps_ram"]  = vps.ram
        data["vps_disk"] = vps.disk_gb
        return JSONResponse(data)
    except Exception as exc:
        logger.warning(
            "api_vps_metrics: unhandled error for vps=%s: %s", name, exc
        )
        return JSONResponse(status_code=200, content={
            "cpu": None, "ram_used": None, "ram_total": vps.ram, "ram_pct": None,
            "net_rx": None, "net_tx": None, "net_rx_rate": None, "net_tx_rate": None,
            "disk_read": None, "disk_write": None,
            "vps_cpu":  vps.cpu,
            "vps_ram":  vps.ram,
            "vps_disk": vps.disk_gb,
            "status":   "unavailable",
            "error":    "Metrics temporarily unavailable",
        })


@router.get("/api/cost")
def cost_preview(ram: int = 1024, cpu: int = 1, disk_gb: int = 20):
    ram     = max(MIN_RAM_MB, min(ram, MAX_RAM_MB))
    cpu     = max(1, min(cpu, 64))
    disk_gb = max(MIN_DISK_GB, min(disk_gb, MAX_DISK_GB))
    return {"cost": IncusService.calculate_cost(ram, cpu, disk_gb)}


# ── Snapshots ─────────────────────────────────────────────────────────────────

def _get_vps_for_user(name: str, user, db) -> VPS:
    """Return VPS by name; admins can access any, users only their own."""
    if user.is_admin:
        vps = db.query(VPS).filter(VPS.name == name).first()
    else:
        vps = db.query(VPS).filter(VPS.name == name, VPS.user_id == user.id).first()
    if not vps:
        raise HTTPException(404)
    return vps


@router.post("/backup/create")
def create_backup(vps_name: str = Form(...), description: str = Form(""),
                  request: Request = None, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    vps  = _get_vps_for_user(vps_name, user, db)
    snap = f"snap-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    result = IncusService.create_snapshot(vps_name, snap, remote=_remote(vps))
    if result["success"]:
        db.add(Backup(vps_id=vps.id, snapshot_name=snap, description=description[:200]))
        db.commit()
        logger.info("Backup created: vps=%s snap=%s user=%s", vps_name, snap, user.username)
    else:
        logger.error("Backup create failed: vps=%s error=%s", vps_name, result.get("error", "").strip())
    return RedirectResponse(f"/vps/{vps_name}?msg=backup_created", status_code=303)


@router.post("/backup/restore")
def restore_backup(vps_name: str = Form(...), snapshot_name: str = Form(...),
                   request: Request = None, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    vps  = _get_vps_for_user(vps_name, user, db)
    logger.info("Backup restore: vps=%s snap=%s user=%s", vps_name, snapshot_name, user.username)
    IncusService.restore_snapshot(vps_name, snapshot_name, remote=_remote(vps))
    return RedirectResponse(f"/vps/{vps_name}?msg=restored", status_code=303)


@router.post("/backup/delete")
def delete_backup(vps_name: str = Form(...), snapshot_name: str = Form(...),
                  backup_id: int = Form(...), request: Request = None,
                  db: Session = Depends(get_db)):
    user = _require_user(request, db)
    vps  = _get_vps_for_user(vps_name, user, db)
    IncusService.delete_snapshot(vps_name, snapshot_name, remote=_remote(vps))
    db.query(Backup).filter(Backup.id == backup_id, Backup.vps_id == vps.id).delete()
    db.commit()
    return RedirectResponse(f"/vps/{vps_name}", status_code=303)


# ── Port Forwarding ───────────────────────────────────────────────────────────

@router.post("/forward/add")
def add_forward(vps_name: str = Form(...), protocol: str = Form("tcp"),
                host_port: int = Form(...), container_port: int = Form(...),
                description: str = Form(""), request: Request = None,
                db: Session = Depends(get_db)):
    user = _require_user(request, db)
    vps  = _get_vps_for_user(vps_name, user, db)
    if protocol not in ("tcp", "udp"):
        raise HTTPException(400, "Protocol must be tcp or udp")
    if not (1 <= host_port <= 65535) or not (1 <= container_port <= 65535):
        raise HTTPException(400, "Port out of range 1–65535")
    result = IncusService.add_port_forward(vps_name, protocol, host_port,
                                           container_port, remote=_remote(vps))
    if result["success"]:
        db.add(PortForward(vps_id=vps.id, protocol=protocol, host_port=host_port,
                           container_port=container_port, description=description[:200]))
        db.commit()
        logger.info("Port forward added: vps=%s %s:%d→%d user=%s",
                    vps_name, protocol, host_port, container_port, user.username)
    else:
        logger.error("Port forward failed: vps=%s error=%s", vps_name, result.get("error", "").strip())
    return RedirectResponse(f"/vps/{vps_name}?msg=forward_added", status_code=303)


@router.post("/forward/delete")
def delete_forward(vps_name: str = Form(...), forward_id: int = Form(...),
                   request: Request = None, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    vps  = _get_vps_for_user(vps_name, user, db)
    fwd = db.query(PortForward).filter(
        PortForward.id == forward_id, PortForward.vps_id == vps.id).first()
    if not fwd:
        raise HTTPException(404)
    IncusService.remove_port_forward(vps_name, fwd.host_port, fwd.protocol, remote=_remote(vps))
    db.delete(fwd)
    db.commit()
    return RedirectResponse(f"/vps/{vps_name}", status_code=303)
