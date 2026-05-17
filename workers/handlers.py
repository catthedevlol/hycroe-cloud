"""
Background job handlers.

Key guarantees:
  - handle_create_vps: logs every step; sets VPS status='error' on ANY Incus
    failure.  On failure, issues an automatic credit refund so the user is never
    charged for a container that never started.
  - handle_node_status_webhook: uses asyncio.run() inside a daemon thread.
  - handle_abuse_check: samples CPU for all running VPS; auto-suspends abusers.
  - All handlers: structured logs include user_id, vps_id, node_id, action.
"""
import asyncio
import logging
from datetime import datetime

from database import SessionLocal
from models import VPS
from services.incus import IncusService
from services.node_selector import NodeSelector

logger = logging.getLogger(__name__)

# ── Node webhook state cache ───────────────────────────────────────────────────
# Keyed by node.id → {"cpu_cores": int, "ram_used_mb": int,
#                     "ram_total_mb": int, "status": str}
# Persists for the lifetime of the worker process.
# Webhook fires only when a value changes by more than _RAM_DELTA_MB MB
# or when online/offline status flips.
_node_last_state: dict = {}
_RAM_DELTA_MB = 64  # minimum RAM change (MB) to trigger a webhook


def _db():
    return SessionLocal()


# ── VPS creation ──────────────────────────────────────────────────────────────

def handle_create_vps(payload: dict):
    """
    Run the actual Incus instance creation for a VPS record.

    Flow:
      1. Load VPS from DB (must exist, status must be 'building')
      2. Call IncusService.create() — this is the real Incus subprocess call
      3a. On success → set status='running', log IP, commit
      3b. On failure → set status='error', issue AUTOMATIC CREDIT REFUND, commit
      Any unhandled exception → set status='error', refund credits, commit

    Structured log context: user_id, vps_id, node, action are included in
    every log line so log aggregation tools can filter by entity.
    """
    db = _db()
    try:
        vps_id = payload.get("vps_id")
        if vps_id is None:
            logger.error(
                "handle_create_vps: missing vps_id in payload | payload=%s", payload
            )
            return

        vps = db.query(VPS).filter(VPS.id == vps_id).first()
        if not vps:
            logger.error(
                "handle_create_vps: VPS not found | vps_id=%s action=abort", vps_id
            )
            return

        node   = vps.node
        remote = (node.incus_remote or None) if node else None

        logger.info(
            "handle_create_vps: START | vps_id=%d name=%s user_id=%d "
            "node=%s remote=%r ram=%dMB cpu=%d disk=%dGB os=%s type=%s",
            vps.id, vps.name, vps.user_id,
            node.name if node else "local",
            remote, vps.ram, vps.cpu, vps.disk_gb,
            vps.os_image or "ubuntu/22.04", vps.instance_type or "container",
        )

        # Confirm building status
        vps.status = "building"
        db.commit()

        is_vm = (vps.instance_type == "vm")
        result = IncusService.create(
            name=vps.name,
            ram=vps.ram,
            cpu=vps.cpu,
            disk_gb=vps.disk_gb,
            os_image=vps.os_image or "ubuntu/22.04",
            remote=remote,
            privileged=not is_vm,
            is_vm=is_vm,
        )

        if result["success"]:
            vps.status = "running"
            logger.info(
                "handle_create_vps: SUCCESS | vps_id=%d name=%s user_id=%d rc=%d",
                vps.id, vps.name, vps.user_id, result.get("returncode", 0),
            )
            # Try to capture assigned IP
            try:
                for lv in IncusService.list_vps(remote=remote):
                    if lv["name"] == vps.name:
                        vps.ipv4 = lv.get("ipv4") or ""
                        vps.ipv6 = lv.get("ipv6") or ""
                        logger.info(
                            "handle_create_vps: IP assigned | vps_id=%d name=%s "
                            "ipv4=%s ipv6=%s",
                            vps.id, vps.name,
                            vps.ipv4 or "none", vps.ipv6 or "none",
                        )
                        break
            except Exception as ip_exc:
                logger.warning(
                    "handle_create_vps: IP capture failed (non-fatal) | "
                    "vps_id=%d name=%s error=%s",
                    vps.id, vps.name, ip_exc,
                )

        else:
            # ── FAILURE: set error status and issue credit refund ────────────
            vps.status    = "error"
            incus_stderr  = (result.get("error") or "").strip()
            incus_stdout  = (result.get("output") or "").strip()
            incus_rc      = result.get("returncode", -1)
            logger.error(
                "handle_create_vps: FAILED | vps_id=%d name=%s user_id=%d "
                "node=%s rc=%d stderr=%s stdout=%s",
                vps.id, vps.name, vps.user_id,
                node.name if node else "local", incus_rc,
                incus_stderr[:500] or "(empty)",
                incus_stdout[:200] if incus_stdout else "(empty)",
            )

            # Automatic credit refund — find the matching deduction transaction
            # and issue an equal credit credit so the user is made whole.
            _refund_credits_for_failed_vps(db, vps)

        vps.last_action = datetime.utcnow()
        db.commit()
        logger.info(
            "handle_create_vps: DB updated | vps_id=%d name=%s status=%s",
            vps.id, vps.name, vps.status,
        )

        # Refresh node resources after creation attempt (success or failure)
        if node:
            try:
                NodeSelector.refresh_node(db, node)
                logger.info(
                    "handle_create_vps: node refreshed | node=%s vps_id=%d",
                    node.name, vps.id,
                )
            except Exception as ref_exc:
                logger.warning(
                    "handle_create_vps: node refresh failed (non-fatal) | "
                    "node=%s vps_id=%d error=%s",
                    node.name if node else "?", vps.id, ref_exc,
                )

    except Exception as exc:
        logger.exception(
            "handle_create_vps: UNHANDLED ERROR | vps_id=%s error=%s",
            payload.get("vps_id"), exc,
        )
        # Ensure VPS is never stuck on 'building'
        try:
            v = db.query(VPS).filter(VPS.id == payload.get("vps_id")).first()
            if v and v.status == "building":
                v.status      = "error"
                v.last_action = datetime.utcnow()
                _refund_credits_for_failed_vps(db, v)
                db.commit()
                logger.info(
                    "handle_create_vps: emergency status set to 'error' | vps_id=%s",
                    payload.get("vps_id"),
                )
        except Exception as fallback_exc:
            logger.error(
                "handle_create_vps: emergency status update also failed | "
                "vps_id=%s error=%s",
                payload.get("vps_id"), fallback_exc,
            )
    finally:
        db.close()


def _refund_credits_for_failed_vps(db, vps) -> None:
    """
    Issue a credit refund equal to the most recent deduction for this VPS.

    WHY: Credits are now deducted BEFORE the Incus subprocess runs (during
    the request, after job enqueue).  If Incus fails inside the worker, the
    user has already paid.  This function makes them whole automatically so
    no manual admin intervention is needed.

    Strategy: look for the most recent Transaction row with a negative amount
    whose description references the VPS name.  If found, add back that amount.
    If not found, log a WARNING for manual review — never raise.
    """
    try:
        from models import Transaction, User
        from services.billing import BillingService

        # Find the most recent deduction transaction for this VPS
        tx = (
            db.query(Transaction)
            .filter(
                Transaction.user_id == vps.user_id,
                Transaction.amount < 0,
                Transaction.description.contains(vps.name),
            )
            .order_by(Transaction.id.desc())
            .first()
        )

        if tx is None:
            logger.warning(
                "_refund_credits: no deduction transaction found for "
                "vps_id=%d name=%s user_id=%d — manual review required",
                vps.id, vps.name, vps.user_id,
            )
            return

        refund_amount = abs(tx.amount)
        user = db.query(User).filter(User.id == vps.user_id).first()
        if user is None:
            logger.error(
                "_refund_credits: user_id=%d not found for vps_id=%d — "
                "cannot refund %d credits automatically",
                vps.user_id, vps.id, refund_amount,
            )
            return

        BillingService.add_credits(
            db, user, refund_amount,
            description=f"AUTO-REFUND: VPS '{vps.name}' failed to start (build error)",
            tx_type="refund",
        )
        logger.info(
            "_refund_credits: refunded %d credits to user_id=%d (vps_id=%d name=%s)",
            refund_amount, user.id, vps.id, vps.name,
        )
    except Exception as exc:
        logger.error(
            "_refund_credits: FAILED for vps_id=%d user_id=%d: %s — manual review required",
            vps.id, vps.user_id, exc,
        )


# ── Metrics collection ────────────────────────────────────────────────────────

def handle_collect_metrics(payload: dict):
    """
    Background metrics warm-up cycle.

    WHY separate timeout: the API is capped at 3s (METRICS_TIMEOUT) to keep
    HTTP responses fast.  This background worker doesn't block any request and
    can afford a longer window (10s) so the _last_good_cache stays warm even
    when Incus is momentarily slow.  Fresh cache = better API fallback data.
    """
    _WORKER_METRICS_TIMEOUT = 10   # seconds per VPS — only used by this worker
    db = _db()
    try:
        running = db.query(VPS).filter(VPS.status == "running").all()
        logger.debug(
            "handle_collect_metrics: collecting metrics | vps_count=%d", len(running)
        )
        for vps in running:
            try:
                node   = vps.node
                remote = (node.incus_remote or None) if node else None
                # Use longer timeout than the API; result warms _last_good_cache
                state = IncusService.get_state(vps.name, remote,
                                               timeout=_WORKER_METRICS_TIMEOUT)
                if state:
                    IncusService.get_metrics(vps.name, remote, ram_limit_mb=vps.ram)
            except Exception as exc:
                logger.debug(
                    "handle_collect_metrics: skipped | vps_id=%d name=%s error=%s",
                    vps.id, vps.name, exc,
                )
    except Exception as exc:
        logger.exception("handle_collect_metrics: unhandled error | error=%s", exc)
    finally:
        db.close()



# ── Node refresh ──────────────────────────────────────────────────────────────

def handle_refresh_nodes(payload: dict):
    db = _db()
    try:
        logger.info("handle_refresh_nodes: starting full node refresh")
        NodeSelector.refresh_all(db)
        logger.info("handle_refresh_nodes: complete")
    except Exception as exc:
        logger.exception("handle_refresh_nodes error: %s", exc)
    finally:
        db.close()


# ── Ghost VPS sync ────────────────────────────────────────────────────────────

def handle_sync_nodes(payload: dict):
    """
    Compare each node's live instance list against the DB.
    Remove any DB records for instances that no longer exist on their node.
    This prevents ghost VPS entries after manual deletions or crashes.
    """
    db = _db()
    try:
        from models import Node
        nodes = db.query(Node).all()

        live: dict = {}  # node_id → set of names
        for node in nodes:
            remote = node.incus_remote or None
            try:
                live[node.id] = IncusService.sync_node(remote=remote)
                logger.debug("sync_nodes: node %s has %d live instances",
                             node.name, len(live[node.id]))
            except Exception as exc:
                logger.warning("sync_nodes: could not list node %s: %s", node.name, exc)
                live[node.id] = None  # None = skip cleanup for this node (unreachable)

        # Also check local Incus for VPS with no node assigned
        try:
            live[None] = IncusService.sync_node(remote=None)
        except Exception as exc:
            logger.warning("sync_nodes: could not list local node: %s", exc)
            live[None] = None

        deleted = 0
        all_vps = db.query(VPS).all()
        for vps in all_vps:
            node_live = live.get(vps.node_id)
            if node_live is None:
                continue  # node unreachable — don't delete records
            if vps.name not in node_live:
                logger.info("sync_nodes: removing ghost VPS %s (not on node %s)",
                            vps.name, vps.node_id)
                from models import Backup, PortForward
                db.query(Backup).filter(Backup.vps_id == vps.id).delete()
                db.query(PortForward).filter(PortForward.vps_id == vps.id).delete()
                db.delete(vps)
                deleted += 1

        if deleted:
            db.commit()
            logger.info("sync_nodes: removed %d ghost VPS records", deleted)
        else:
            logger.debug("sync_nodes: all VPS records are consistent")

    except Exception as exc:
        logger.exception("handle_sync_nodes error: %s", exc)
    finally:
        db.close()


# ── Node status webhook ───────────────────────────────────────────────────────

def handle_node_status_webhook(payload: dict):
    """
    Fetch node info via IncusService.get_node_info() and fire a webhook only
    when something meaningful has changed since the last successful send.

    Change triggers:
      - online/offline status flip
      - RAM used changes by more than _RAM_DELTA_MB MB

    Async coroutines are executed via asyncio.run() which is correct and safe
    inside a daemon thread. asyncio.new_event_loop() is NEVER used.

    Webhook failure is non-fatal — logged and retried on the next cycle.
    """
    global _node_last_state

    db = _db()
    try:
        from models import Node
        from services.settings import get_settings
        from services.webhook import send_node_status_webhook, is_valid_webhook_url

        settings    = get_settings(db)
        webhook_url = settings.get("discord_webhook_url") or ""

        if not webhook_url or not is_valid_webhook_url(webhook_url):
            logger.debug("node_status_webhook: no valid webhook configured — skipping")
            return

        nodes = db.query(Node).all()
        if not nodes:
            logger.debug("node_status_webhook: no nodes in DB — skipping")
            return

        logger.debug("node_status_webhook: checking %d node(s)", len(nodes))

        for node in nodes:
            # ── 1. Gather current stats ───────────────────────────────────────
            try:
                remote = node.incus_remote or None
                info   = IncusService.get_node_info(remote=remote)

                cpu_cores    = int(info.get("cpu_cores",    0))
                ram_used_mb  = int(info.get("ram_used_mb",  0))
                ram_total_mb = int(info.get("ram_total_mb", 0))
                status       = "online" if info.get("online", False) else "offline"

                logger.debug(
                    "node_status_webhook: node=%s status=%s ram=%d/%dMB cpu_cores=%d",
                    node.name, status, ram_used_mb, ram_total_mb, cpu_cores,
                )

            except Exception as exc:
                logger.warning(
                    "node_status_webhook: could not fetch info for node %s: %s",
                    node.name, exc,
                )
                cpu_cores    = 0
                ram_used_mb  = 0
                ram_total_mb = 0
                status       = "offline"

            # ── 2. Compare against last known state ───────────────────────────
            prev = _node_last_state.get(node.id)
            if prev is not None:
                status_changed = prev["status"] != status
                ram_delta      = abs(ram_used_mb - prev["ram_used_mb"])
                if not status_changed and ram_delta < _RAM_DELTA_MB:
                    logger.debug(
                        "node_status_webhook: node %s unchanged (status=%s Δram=%dMB) — skipping",
                        node.name, status, ram_delta,
                    )
                    continue
                logger.info(
                    "node_status_webhook: node %s changed — status: %s→%s, Δram=%dMB",
                    node.name, prev["status"], status, ram_delta,
                )

            # ── 3. Send webhook (asyncio.run inside thread — correct pattern) ──
            try:
                sent = asyncio.run(
                    send_node_status_webhook(
                        webhook_url=webhook_url,
                        node_name=node.display_name or node.name,
                        cpu_cores=cpu_cores,
                        ram_used_mb=ram_used_mb,
                        ram_total_mb=ram_total_mb,
                        status=status,
                    )
                )

                if sent:
                    _node_last_state[node.id] = {
                        "cpu_cores":    cpu_cores,
                        "ram_used_mb":  ram_used_mb,
                        "ram_total_mb": ram_total_mb,
                        "status":       status,
                    }
                    logger.info(
                        "node_status_webhook: SENT for node=%s status=%s",
                        node.name, status,
                    )
                else:
                    logger.warning(
                        "node_status_webhook: DELIVERY FAILED for node=%s (will retry next cycle)",
                        node.name,
                    )

            except Exception as exc:
                logger.error(
                    "node_status_webhook: webhook send error for node %s (non-fatal): %s",
                    node.name, exc,
                )

    except Exception as exc:
        logger.exception("handle_node_status_webhook unhandled error: %s", exc)
    finally:
        db.close()


# ── VPS expiry ────────────────────────────────────────────────────────────────

def handle_check_expiry(payload: dict):
    """
    Scan all VPS records for expired instances (expires_at < now).
    Expired VPS are stopped via Incus and marked suspended + status='stopped'.
    Runs once per hour via the periodic scheduler in main.py.
    """
    db = _db()
    try:
        from datetime import datetime as _dt
        now     = _dt.utcnow()
        all_vps = db.query(VPS).filter(
            VPS.suspended == False,  # noqa
            VPS.expires_at != None,  # noqa
        ).all()

        expired = [v for v in all_vps if v.expires_at and v.expires_at <= now]
        if not expired:
            logger.debug("check_expiry: no expired VPS found")
            return

        logger.info("check_expiry: found %d expired VPS", len(expired))
        for vps in expired:
            try:
                node   = vps.node
                remote = (node.incus_remote or None) if node else None
                IncusService.stop(vps.name, remote=remote)
                logger.info("check_expiry: stopped VPS %s", vps.name)
            except Exception as exc:
                logger.warning("check_expiry: could not stop %s: %s", vps.name, exc)

            vps.suspended   = True
            vps.status      = "stopped"
            vps.last_action = _dt.utcnow()
            logger.info("check_expiry: suspended VPS %s (expired %s)", vps.name, vps.expires_at)

        db.commit()

    except Exception as exc:
        logger.exception("handle_check_expiry unhandled error: %s", exc)
    finally:
        db.close()



# ── Abuse detection ───────────────────────────────────────────────────────────

def handle_abuse_check(payload: dict):
    """
    Two-stage CPU overload enforcement cycle.

    Stage 1 — WARNING  (>80% sustained, 3 samples):
      • VPS status → 'warning'
      • note appended to vps.notes
      • Discord embed: orange, action='warning'
      • Does NOT stop the container

    Stage 2 — CRITICAL (>90% sustained, 3 samples):
      • IncusService.stop() called
      • VPS status → 'suspended_overload', suspended=True
      • note appended to vps.notes
      • Discord embed: red, action='stopped'

    Cooldown: 15 min per VPS between any two actions (no spam).
    Metrics fail → skip that VPS silently (fail safe).
    """
    db = _db()
    try:
        from services.abuse import check_and_flag_abusers
        from services.settings import get_settings
        from services.webhook import fire_abuse_alert_sync, is_valid_webhook_url
        from models import User

        # Fetch all VPS that could be in either stage
        # ('warning' status is also scanned so it can escalate to CRITICAL)
        scannable = db.query(VPS).filter(
            VPS.status.in_(["running", "warning"]),
            VPS.suspended == False,  # noqa
        ).all()

        logger.debug(
            "handle_abuse_check: scanning %d VPS | status=running+warning",
            len(scannable),
        )

        events = check_and_flag_abusers(db, scannable)

        if not events:
            logger.debug("handle_abuse_check: no abuse events this cycle")
            return

        # ── Load webhook URL once ────────────────────────────────────────────
        settings = get_settings(db)
        # Prefer dedicated abuse alert URL; fall back to admin log URL
        wh_url = (
            settings.get("abuse_alert_webhook_url")
            or settings.get("admin_log_webhook_url")
            or ""
        )
        send_webhooks = bool(wh_url and is_valid_webhook_url(wh_url))

        # ── Process each event ───────────────────────────────────────────────
        warnings  = [e for e in events if e.action == "warning"]
        stops     = [e for e in events if e.action == "stopped"]

        logger.info(
            "handle_abuse_check: cycle complete | warnings=%d stops=%d",
            len(warnings), len(stops),
        )

        for event in events:
            # Structured machine-readable log line
            logger.warning(
                "event=abuse_%s vps_id=%d vps=%s user_id=%d cpu=%.1f node=%s",
                event.action, event.vps_id, event.vps_name,
                event.user_id, event.cpu_pct, event.node_name,
            )

            if not send_webhooks:
                continue

            # Look up username for the webhook embed (non-fatal if missing)
            try:
                user = db.query(User).filter(User.id == event.user_id).first()
                username = user.username if user else f"user_{event.user_id}"
            except Exception:
                username = f"user_{event.user_id}"

            try:
                fire_abuse_alert_sync(
                    webhook_url=wh_url,
                    vps_name=event.vps_name,
                    user_id=event.user_id,
                    username=username,
                    cpu_pct=event.cpu_pct,
                    node_name=event.node_name,
                    action=event.action,
                )
            except Exception as wh_exc:
                logger.warning(
                    "handle_abuse_check: webhook failed for vps_id=%d (non-fatal): %s",
                    event.vps_id, wh_exc,
                )

    except Exception as exc:
        logger.exception("handle_abuse_check: unhandled error | error=%s", exc)
    finally:
        db.close()


# ── Handler registry ──────────────────────────────────────────────────────────

JOB_HANDLERS = {
    "create_vps":          handle_create_vps,
    "collect_metrics":     handle_collect_metrics,
    "refresh_nodes":       handle_refresh_nodes,
    "sync_nodes":          handle_sync_nodes,
    "node_status_webhook": handle_node_status_webhook,
    "check_expiry":        handle_check_expiry,
    "abuse_check":         handle_abuse_check,
}
