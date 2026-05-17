"""
Admin panel router — v3 upgraded.

Additions over original:
  - POST /admin/create-user    → admins can create users directly
  - POST /admin/vps/create     → admins can create a VPS for any user
  - POST /admin/vps/action     → start/stop/restart/delete any VPS
  - POST /admin/vps/rebuild    → rebuild any VPS
  - GET  /api/admin/vps        → JSON list of all VPS
All existing endpoints preserved and hardened.
"""
import logging
import urllib.parse
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Form, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import get_db
from models import User, VPS, Node, Transaction, Backup, PortForward, Coupon, CouponRedemption, VPSPlan
from services.auth import AuthService
from services.billing import BillingService
from services.incus import IncusService, AVAILABLE_IMAGES
from services.node_selector import NodeSelector
try:
    import workers as _workers
    def _enqueue(j,p): _workers.enqueue(j,p)
except Exception:
    def _enqueue(j,p): pass

router = APIRouter()
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)

VALID_CPU = set(range(1, 65))    # 1–64 vCPUs
MIN_RAM_MB  = 128
MAX_RAM_MB  = 131072
MIN_DISK_GB = 5
MAX_DISK_GB = 2000


def _fire_admin_log(db, actor: str, action: str, detail: str = "") -> None:
    """Non-blocking admin audit log webhook — fails silently."""
    try:
        from services.settings import get_settings
        from services.webhook import fire_admin_log
        wh = get_settings(db).get("admin_log_webhook_url") or ""
        if wh:
            fire_admin_log(wh, actor, action, detail)
    except Exception as exc:
        logger.debug("_fire_admin_log (non-fatal): %s", exc)


def _require_admin(request, db) -> User:
    user = AuthService.get_user(request, db)
    if not user or not user.is_admin:
        raise HTTPException(403, "Admin access required")
    return user


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db)):
    user = AuthService.get_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse("/dashboard", status_code=303)

    users     = db.query(User).order_by(User.created_at.desc()).all()
    all_vps   = db.query(VPS).order_by(VPS.created_at.desc()).all()
    recent_tx = db.query(Transaction).order_by(Transaction.created_at.desc()).limit(20).all()
    nodes     = db.query(Node).all()
    coupons   = db.query(Coupon).order_by(Coupon.created_at.desc()).all()
    plans     = db.query(VPSPlan).order_by(VPSPlan.created_at.desc()).all()

    vps_data = [
        {
            "id": v.id, "name": v.name, "status": v.status,
            "instance_type": v.instance_type or "container",
            "ram": v.ram, "cpu": v.cpu, "disk_gb": v.disk_gb,
            "os_image": v.os_image or "ubuntu/22.04",
            "node": (v.node.display_name or v.node.name) if v.node else "local",
            "node_id": v.node_id,
            "owner": v.owner.username if v.owner else "?",
            "owner_id": v.user_id,
            "suspended": v.suspended,
            "ipv4": v.ipv4 or "—",
            "created_at": v.created_at,
        }
        for v in all_vps
    ]

    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user,
        "users": users, "vps_data": vps_data, "recent_tx": recent_tx,
        "nodes": nodes, "os_images": AVAILABLE_IMAGES,
        "coupons": coupons, "plans": plans, "now": datetime.utcnow(),
        "stats": {
            "total_vps":    db.query(VPS).count(),
            "running_vps":  db.query(VPS).filter(VPS.status == "running").count(),
            "total_users":  db.query(User).count(),
            "total_nodes":  db.query(Node).count(),
            "online_nodes": db.query(Node).filter(Node.status == "online").count(),
        },
    })


@router.post("/admin/create-user")
def admin_create_user(
    username: str = Form(...),
    password: str = Form(...),
    email:    str = Form(""),
    credits:  int = Form(0),
    is_admin: str = Form("off"),
    request:  Request = None,
    db:       Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    username = username.strip().lower()
    if len(username) < 3 or not username.replace("-", "").replace("_", "").isalnum():
        return RedirectResponse("/admin?error=invalid_username", status_code=303)
    if len(password) < 8:
        return RedirectResponse("/admin?error=password_too_short", status_code=303)
    if db.query(User).filter(User.username == username).first():
        return RedirectResponse("/admin?error=username_taken", status_code=303)
    new_user = User(
        username=username,
        email=email.strip() or None,
        password=AuthService.hash_password(password),
        credits=max(0, credits),
        is_admin=(is_admin == "on"),
    )
    db.add(new_user)
    db.commit()
    logger.info("Admin %s created user %s", admin.username, username)
    return RedirectResponse("/admin?success=user_created", status_code=303)


@router.post("/admin/credits")
def admin_credits(
    target_user_id: int = Form(...),
    amount: int = Form(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    admin  = _require_admin(request, db)
    target = db.query(User).filter(User.id == target_user_id).first()
    if not target:
        raise HTTPException(404, "User not found")
    if not (-1_000_000 <= amount <= 1_000_000):
        raise HTTPException(400, "Amount out of allowed range")
    logger.info("Admin %s adjusted credits for %s: %+d", admin.username, target.username, amount)
    BillingService.add_credits(db, target, amount, f"Admin adjustment by {admin.username}", "admin")
    db.commit()
    _fire_admin_log(db, admin.username, "Credits Adjusted",
                    f"User: {target.username}, Amount: {amount:+d}cr")
    return RedirectResponse("/admin?success=credits_adjusted", status_code=303)


@router.post("/admin/suspend")
def admin_suspend(
    target_user_id: int = Form(...),
    reason: str = Form(""),
    request: Request = None,
    db: Session = Depends(get_db),
):
    admin  = _require_admin(request, db)
    target = db.query(User).filter(User.id == target_user_id).first()
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == admin.id:
        raise HTTPException(400, "Cannot suspend yourself")
    target.is_suspended   = not target.is_suspended
    target.suspend_reason = reason[:500] if target.is_suspended else None
    db.commit()
    action = "User Suspended" if target.is_suspended else "User Unsuspended"
    _fire_admin_log(db, admin.username, action, f"User: {target.username}")
    return RedirectResponse("/admin?success=user_updated", status_code=303)


@router.post("/admin/delete-user")
def admin_delete_user(
    target_user_id: int = Form(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    admin  = _require_admin(request, db)
    target = db.query(User).filter(User.id == target_user_id).first()
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == admin.id:
        raise HTTPException(400, "Cannot delete your own account")
    for vps in list(target.vps):
        remote = (vps.node.incus_remote or None) if vps.node else None
        IncusService.delete(vps.name, remote=remote)
        db.query(Backup).filter(Backup.vps_id == vps.id).delete()
        db.query(PortForward).filter(PortForward.vps_id == vps.id).delete()
    db.query(VPS).filter(VPS.user_id == target_user_id).delete()
    db.query(Transaction).filter(Transaction.user_id == target_user_id).delete()
    db.delete(target)
    db.commit()
    logger.info("Admin %s deleted user %s", admin.username, target.username)
    _fire_admin_log(db, admin.username, "User Deleted", f"User: {target.username}")
    return RedirectResponse("/admin?success=user_deleted", status_code=303)


@router.post("/admin/toggle-admin")
def toggle_admin(
    target_user_id: int = Form(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    admin  = _require_admin(request, db)
    target = db.query(User).filter(User.id == target_user_id).first()
    if not target:
        raise HTTPException(404)
    if target.id == admin.id:
        raise HTTPException(400, "Cannot change your own admin status")
    target.is_admin = not target.is_admin
    db.commit()
    return RedirectResponse("/admin?success=user_updated", status_code=303)


@router.post("/admin/suspend-vps")
def admin_suspend_vps(
    vps_id: int = Form(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    vps   = db.query(VPS).filter(VPS.id == vps_id).first()
    if not vps:
        raise HTTPException(404, "VPS not found")
    vps.suspended = not vps.suspended
    if vps.suspended:
        remote = (vps.node.incus_remote or None) if vps.node else None
        IncusService.stop(vps.name, remote=remote)
        vps.status = "stopped"
    db.commit()
    return RedirectResponse("/admin?success=vps_updated", status_code=303)


# ── Admin VPS Creation ────────────────────────────────────────────────────────

@router.post("/admin/vps/create")
def admin_create_vps(
    target_user_id: int = Form(...),
    name:          str  = Form(...),
    instance_type: str  = Form("container"),
    ram:           int  = Form(1024),
    cpu:           int  = Form(1),
    disk_gb:       int  = Form(20),
    os_image:      str  = Form("ubuntu/22.04"),
    node_id:       Optional[int] = Form(None),
    request:       Request = None,
    db:            Session = Depends(get_db),
):
    admin  = _require_admin(request, db)
    target = db.query(User).filter(User.id == target_user_id).first()
    if not target:
        return RedirectResponse("/admin?error=user_not_found", status_code=303)

    instance_type = instance_type.strip().lower()
    if instance_type not in ("container", "vm"):
        instance_type = "container"

    name = name.strip().lower()
    if not IncusService.validate_name(name):
        return RedirectResponse("/admin?error=invalid_name", status_code=303)
    if db.query(VPS).filter(VPS.name == name).first():
        return RedirectResponse("/admin?error=name_taken", status_code=303)
    if not (MIN_RAM_MB <= ram <= MAX_RAM_MB) or cpu not in VALID_CPU:
        return RedirectResponse("/admin?error=invalid_resources", status_code=303)
    if not (MIN_DISK_GB <= disk_gb <= MAX_DISK_GB):
        return RedirectResponse("/admin?error=invalid_resources", status_code=303)
    if os_image not in AVAILABLE_IMAGES:
        os_image = "ubuntu/22.04"

    node = NodeSelector.pick(db, ram_needed=ram, cpu_needed=cpu, preferred_node_id=node_id)
    vps  = VPS(name=name, instance_type=instance_type,
               ram=ram, cpu=cpu, disk_gb=disk_gb, os_image=os_image,
               status="building", user_id=target.id, node_id=node.id if node else None)
    db.add(vps)
    db.commit()
    db.refresh(vps)
    _enqueue("create_vps", {"vps_id": vps.id})
    logger.info("Admin %s created %s %s for user %s",
                admin.username, instance_type, name, target.username)
    return RedirectResponse("/admin?success=vps_creating", status_code=303)



# ── Admin VPS Power/Delete ────────────────────────────────────────────────────

@router.post("/admin/vps/action")
def admin_vps_action(
    vps_id:  int = Form(...),
    action:  str = Form(...),
    request: Request = None,
    db:      Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    vps   = db.query(VPS).filter(VPS.id == vps_id).first()
    if not vps:
        raise HTTPException(404, "VPS not found")

    remote = (vps.node.incus_remote or None) if vps.node else None

    if action == "start":
        result = IncusService.start(vps.name, remote=remote)
        new_status = "running"
    elif action == "stop":
        result = IncusService.stop(vps.name, remote=remote)
        new_status = "stopped"
    elif action == "restart":
        result = IncusService.restart(vps.name, remote=remote)
        new_status = "running"
    elif action == "delete":
        result = IncusService.delete(vps.name, remote=remote)
        if result["success"]:
            db.query(Backup).filter(Backup.vps_id == vps.id).delete()
            db.query(PortForward).filter(PortForward.vps_id == vps.id).delete()
            db.delete(vps)
            db.commit()
            return RedirectResponse("/admin?success=vps_deleted", status_code=303)
        msg = urllib.parse.quote(result["error"].strip()[:200])
        return RedirectResponse(f"/admin?error=incus_error&msg={msg}", status_code=303)
    else:
        raise HTTPException(400, "Invalid action")

    if not result["success"]:
        msg = urllib.parse.quote(result["error"].strip()[:200])
        return RedirectResponse(f"/admin?error=incus_error&msg={msg}", status_code=303)

    vps.status      = new_status
    vps.last_action = datetime.utcnow()
    db.commit()
    logger.info("Admin %s performed %s on VPS %s", admin.username, action, vps.name)
    return RedirectResponse("/admin?success=vps_updated", status_code=303)


@router.post("/admin/vps/rebuild")
def admin_vps_rebuild(
    vps_id:   int = Form(...),
    os_image: str = Form("ubuntu/22.04"),
    request:  Request = None,
    db:       Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    vps   = db.query(VPS).filter(VPS.id == vps_id).first()
    if not vps:
        raise HTTPException(404, "VPS not found")
    if os_image not in AVAILABLE_IMAGES:
        os_image = "ubuntu/22.04"

    remote       = (vps.node.incus_remote or None) if vps.node else None
    is_vm        = (vps.instance_type == "vm")
    vps.os_image = os_image
    vps.status   = "building"
    db.commit()

    result = IncusService.rebuild(vps.name, vps.ram, vps.cpu, vps.disk_gb,
                                  os_image, remote=remote, is_vm=is_vm)
    vps.status      = "running" if result["success"] else "error"
    vps.last_action = datetime.utcnow()
    db.commit()

    if not result["success"]:
        msg = urllib.parse.quote(result["error"].strip()[:200])
        return RedirectResponse(f"/admin?error=incus_error&msg={msg}", status_code=303)

    return RedirectResponse("/admin?success=vps_rebuilt", status_code=303)


@router.get("/api/admin/vps")
def admin_vps_json(request: Request, db: Session = Depends(get_db)):
    _require_admin(request, db)
    all_vps = db.query(VPS).order_by(VPS.created_at.desc()).all()
    return JSONResponse([
        {
            "id": v.id, "name": v.name, "status": v.status,
            "instance_type": v.instance_type or "container",
            "ram": v.ram, "cpu": v.cpu, "disk_gb": v.disk_gb,
            "node": (v.node.display_name or v.node.name) if v.node else "local",
            "owner": v.owner.username if v.owner else "?",
            "suspended": v.suspended, "ipv4": v.ipv4 or "—",
        }
        for v in all_vps
    ])


@router.post("/admin/vps/edit")
def admin_vps_edit(
    vps_id:  int = Form(...),
    ram:     int = Form(...),
    cpu:     int = Form(...),
    disk_gb: int = Form(...),
    request: Request = None,
    db:      Session = Depends(get_db),
):
    """
    Live-edit VPS resources via incus config set.
    Updates limits.cpu and limits.memory on the running/stopped instance,
    then saves the new values to the DB record.
    """
    admin = _require_admin(request, db)
    vps   = db.query(VPS).filter(VPS.id == vps_id).first()
    if not vps:
        raise HTTPException(404, "VPS not found")

    if not (MIN_RAM_MB <= ram <= MAX_RAM_MB) or cpu not in VALID_CPU:
        return RedirectResponse("/admin?tab=vps&error=invalid_resources", status_code=303)
    if not (MIN_DISK_GB <= disk_gb <= MAX_DISK_GB):
        return RedirectResponse("/admin?tab=vps&error=invalid_resources", status_code=303)

    remote = (vps.node.incus_remote or None) if vps.node else None

    # Apply live config changes
    IncusService.config_set(vps.name, "limits.memory", f"{ram}MB", remote=remote)
    IncusService.config_set(vps.name, "limits.cpu",    str(cpu),   remote=remote)

    # Update DB
    vps.ram     = ram
    vps.cpu     = cpu
    vps.disk_gb = disk_gb
    vps.last_action = datetime.utcnow()
    db.commit()
    logger.info("Admin %s edited VPS %s → ram=%dMB cpu=%d disk=%dGB",
                admin.username, vps.name, ram, cpu, disk_gb)
    return RedirectResponse("/admin?tab=vps&success=vps_updated", status_code=303)


@router.post("/admin/sync-nodes")
def admin_sync_nodes(request: Request, db: Session = Depends(get_db)):
    """Trigger a node sync job to remove ghost VPS records."""
    _require_admin(request, db)
    _enqueue("sync_nodes", {})
    return RedirectResponse("/admin?tab=vps&success=sync_started", status_code=303)


# ── Coupon Management ─────────────────────────────────────────────────────────

VALID_RAM_PLAN = {512, 1024, 2048, 4096, 8192, 16384}  # kept for reference; no longer enforced
VALID_CPU_PLAN = set(range(1, 17))                     # kept for reference; no longer enforced
PLAN_MIN_RAM_MB  = 128
PLAN_MAX_RAM_MB  = 262144   # 256 GB
PLAN_MIN_CPU     = 1
PLAN_MAX_CPU     = 128
PLAN_MIN_DISK_GB = 5
PLAN_MAX_DISK_GB = 10000


@router.post("/admin/coupon/create")
def admin_coupon_create(
    code:       str           = Form(...),
    credits:    int           = Form(...),
    max_uses:   int           = Form(0),
    expires_at: Optional[str] = Form(None),
    request:    Request       = None,
    db:         Session       = Depends(get_db),
):
    admin = _require_admin(request, db)
    code = code.strip().upper()
    if not (3 <= len(code) <= 64) or not code.replace("-", "").replace("_", "").isalnum():
        return RedirectResponse("/admin?tab=coupons&error=invalid_coupon_code", status_code=303)
    if db.query(Coupon).filter(Coupon.code == code).first():
        return RedirectResponse("/admin?tab=coupons&error=coupon_code_taken", status_code=303)
    if not (1 <= credits <= 1_000_000):
        return RedirectResponse("/admin?tab=coupons&error=invalid_coupon_credits", status_code=303)

    exp = None
    if expires_at and expires_at.strip():
        try:
            exp = datetime.fromisoformat(expires_at.strip().replace("Z", ""))
        except ValueError:
            pass

    coupon = Coupon(
        code=code,
        credits=credits,
        max_uses=max_uses if max_uses > 0 else None,
        is_active=True,
        created_by=admin.id,
        expires_at=exp,
    )
    db.add(coupon)
    db.commit()
    logger.info("Admin %s created coupon %s (%dcr, max_uses=%s)", admin.username, code, credits, max_uses)
    return RedirectResponse("/admin?tab=coupons&success=coupon_created", status_code=303)


@router.post("/admin/coupon/delete")
def admin_coupon_delete(
    coupon_id: int     = Form(...),
    request:   Request = None,
    db:        Session = Depends(get_db),
):
    _require_admin(request, db)
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id).first()
    if not coupon:
        raise HTTPException(404, "Coupon not found")
    db.query(CouponRedemption).filter(CouponRedemption.coupon_id == coupon_id).delete()
    db.delete(coupon)
    db.commit()
    return RedirectResponse("/admin?tab=coupons&success=coupon_deleted", status_code=303)


@router.post("/admin/coupon/toggle")
def admin_coupon_toggle(
    coupon_id: int     = Form(...),
    request:   Request = None,
    db:        Session = Depends(get_db),
):
    _require_admin(request, db)
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id).first()
    if not coupon:
        raise HTTPException(404)
    coupon.is_active = not coupon.is_active
    db.commit()
    return RedirectResponse("/admin?tab=coupons&success=coupon_updated", status_code=303)


# ── Plan Management ───────────────────────────────────────────────────────────

@router.post("/admin/plan/create")
def admin_plan_create(
    name:          str           = Form(...),
    ram_mb:        int           = Form(1024),
    cpu:           int           = Form(1),
    disk_gb:       int           = Form(20),
    credits_cost:  int           = Form(100),
    instance_type: str           = Form("container"),
    location:      Optional[str] = Form(None),
    node_id:       Optional[int] = Form(None),
    request:       Request       = None,
    db:            Session       = Depends(get_db),
):
    admin = _require_admin(request, db)
    name = name.strip()
    if not name or len(name) > 80:
        return RedirectResponse("/admin?tab=plans&error=invalid_plan_name", status_code=303)
    if not (PLAN_MIN_RAM_MB <= ram_mb <= PLAN_MAX_RAM_MB):
        return RedirectResponse("/admin?tab=plans&error=invalid_plan_resources", status_code=303)
    if not (PLAN_MIN_CPU <= cpu <= PLAN_MAX_CPU):
        return RedirectResponse("/admin?tab=plans&error=invalid_plan_resources", status_code=303)
    if not (PLAN_MIN_DISK_GB <= disk_gb <= PLAN_MAX_DISK_GB):
        return RedirectResponse("/admin?tab=plans&error=invalid_plan_resources", status_code=303)
    if credits_cost <= 0:
        return RedirectResponse("/admin?tab=plans&error=invalid_plan_cost", status_code=303)
    instance_type = instance_type.strip().lower()
    if instance_type not in ("container", "vm"):
        instance_type = "container"

    plan = VPSPlan(
        name=name,
        ram_mb=ram_mb,
        cpu=cpu,
        disk_gb=disk_gb,
        credits_cost=credits_cost,
        instance_type=instance_type,
        location=location.strip() if location else None,
        node_id=node_id or None,
        is_active=True,
        created_by=admin.id,
    )
    db.add(plan)
    db.commit()
    logger.info("Admin %s created plan '%s' (type=%s)", admin.username, name, instance_type)
    return RedirectResponse("/admin?tab=plans&success=plan_created", status_code=303)


@router.post("/admin/plan/delete")
def admin_plan_delete(
    plan_id: int     = Form(...),
    request: Request = None,
    db:      Session = Depends(get_db),
):
    _require_admin(request, db)
    plan = db.query(VPSPlan).filter(VPSPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404)
    db.delete(plan)
    db.commit()
    return RedirectResponse("/admin?tab=plans&success=plan_deleted", status_code=303)


@router.post("/admin/plan/toggle")
def admin_plan_toggle(
    plan_id: int     = Form(...),
    request: Request = None,
    db:      Session = Depends(get_db),
):
    _require_admin(request, db)
    plan = db.query(VPSPlan).filter(VPSPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404)
    plan.is_active = not plan.is_active
    db.commit()
    return RedirectResponse("/admin?tab=plans&success=plan_updated", status_code=303)


@router.get("/api/admin/plans")
def admin_plans_json(request: Request, db: Session = Depends(get_db)):
    _require_admin(request, db)
    plans = db.query(VPSPlan).filter(VPSPlan.is_active == True).order_by(VPSPlan.credits_cost).all()
    return JSONResponse([
        {
            "id": p.id, "name": p.name,
            "ram_mb": p.ram_mb, "cpu": p.cpu, "disk_gb": p.disk_gb,
            "credits_cost": p.credits_cost, "location": p.location or "",
            "node_id": p.node_id,
            "display_ram": p.display_ram,
            "instance_type": p.instance_type or "container",
        }
        for p in plans
    ])


# ── Panel Settings ─────────────────────────────────────────────────────────

@router.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(request: Request, db: Session = Depends(get_db)):
    user = AuthService.get_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse("/dashboard", status_code=303)
    from services.settings import get_settings
    from models import PanelSettings
    s = db.query(PanelSettings).filter(PanelSettings.id == 1).first()
    if not s:
        s = PanelSettings(id=1)
        db.add(s)
        db.commit()
        db.refresh(s)
    return templates.TemplateResponse("admin_settings.html", {
        "request": request, "user": user,
        "s": s,
        "settings": get_settings(db),
    })


@router.post("/admin/settings/save")
def admin_settings_save(
    panel_name:             str  = Form("Hycroe Panel"),
    panel_description:      str  = Form(""),
    theme_color:            str  = Form("#0066FF"),
    logo_url:               str  = Form(""),
    enable_registration:    bool = Form(False),
    enable_discord_login:   bool = Form(False),
    enable_billing:         bool = Form(False),
    require_discord_verify: bool = Form(False),
    discord_webhook_url:    str  = Form(""),
    notify_user_id:         str  = Form(""),
    node_webhook_url:       str  = Form(""),
    admin_log_webhook_url:  str  = Form(""),
    announcement:           str  = Form(""),
    request: Request = None,
    db: Session = Depends(get_db),
):
    user = AuthService.get_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse("/dashboard", status_code=303)

    from models import PanelSettings
    from services.settings import invalidate_cache
    from services.webhook import is_valid_webhook_url
    import re

    s = db.query(PanelSettings).filter(PanelSettings.id == 1).first()
    if not s:
        s = PanelSettings(id=1)
        db.add(s)

    if not re.match(r'^#[0-9a-fA-F]{3,6}$', theme_color):
        theme_color = "#0066FF"

    # Validate each webhook URL independently — blank is always allowed
    webhook_url = discord_webhook_url.strip()
    if webhook_url and not is_valid_webhook_url(webhook_url):
        return RedirectResponse("/admin/settings?error=invalid_webhook_url", status_code=303)

    node_wh = node_webhook_url.strip()
    if node_wh and not is_valid_webhook_url(node_wh):
        return RedirectResponse("/admin/settings?error=invalid_node_webhook_url", status_code=303)

    admin_wh = admin_log_webhook_url.strip()
    if admin_wh and not is_valid_webhook_url(admin_wh):
        return RedirectResponse("/admin/settings?error=invalid_admin_log_webhook_url", status_code=303)

    notify_user_id_val = notify_user_id.strip()
    if notify_user_id_val and not notify_user_id_val.isdigit():
        return RedirectResponse("/admin/settings?error=invalid_notify_user_id", status_code=303)

    s.panel_name             = panel_name.strip()[:80]   or "Hycroe Panel"
    s.panel_description      = panel_description.strip()[:200]
    s.theme_color            = theme_color
    s.logo_url               = logo_url.strip() or None
    s.enable_registration    = enable_registration
    s.enable_discord_login   = enable_discord_login
    s.enable_billing         = enable_billing
    s.require_discord_verify = require_discord_verify
    s.discord_webhook_url    = webhook_url or None
    s.notify_user_id         = notify_user_id_val or None
    s.node_webhook_url       = node_wh or None
    s.admin_log_webhook_url  = admin_wh or None
    s.announcement           = announcement.strip()[:500] or None

    db.commit()
    invalidate_cache()
    return RedirectResponse("/admin/settings?success=settings_saved", status_code=303)
