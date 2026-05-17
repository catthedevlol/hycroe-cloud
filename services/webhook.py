"""
Webhook service for Hycroe Cloud Panel.
Sends Discord (or generic HTTPS) webhook notifications for key events.

All sync wrappers use asyncio.run() via ThreadPoolExecutor — never new_event_loop().
All async senders use _send_with_retry() — 3 attempts, exponential delays, full logging.
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def is_valid_webhook_url(url: str) -> bool:
    """Validate that a URL looks like a usable webhook URL (Discord or generic HTTPS)."""
    if not url:
        return False
    url = url.strip()
    return url.startswith("https://") and len(url) > 12


# ── Core retry sender ─────────────────────────────────────────────────────────

async def _send_with_retry(
    url: str,
    payload: dict,
    max_attempts: int = 3,
    timeout: float = 8.0,
) -> bool:
    """
    POST payload to url with up to max_attempts retries.
    Waits 0s, 1s, 2s between attempts.
    Returns True if any attempt succeeds (HTTP 200 or 204).
    Non-fatal — all failures are logged.
    """
    delays = [0, 1, 2]
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code in (200, 204):
                logger.debug("Webhook delivered on attempt %d/%d to %s", attempt + 1, max_attempts, url[:60])
                return True
            logger.warning(
                "Webhook attempt %d/%d returned HTTP %d for %s",
                attempt + 1, max_attempts, resp.status_code, url[:60],
            )
        except Exception as exc:
            logger.warning(
                "Webhook attempt %d/%d failed for %s: %s",
                attempt + 1, max_attempts, url[:60], exc,
            )
        if attempt < max_attempts - 1:
            await asyncio.sleep(delays[attempt + 1])

    logger.error("Webhook FAILED after %d attempts: %s", max_attempts, url[:60])
    return False


# ── Safe sync wrapper ─────────────────────────────────────────────────────────

def _run_async_in_thread(coro) -> bool:
    """
    Run an async coroutine safely from any synchronous/non-async context.

    Uses ThreadPoolExecutor + asyncio.run() — the ONLY safe pattern.
    asyncio.new_event_loop() is intentionally NEVER used anywhere in this module.

    Returns the coroutine result (bool), or False on any error.
    """
    def _worker():
        return asyncio.run(coro)

    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_worker)
            return future.result(timeout=30)
    except Exception as exc:
        logger.error("_run_async_in_thread failed: %s", exc)
        return False


# ── Credit purchase ───────────────────────────────────────────────────────────

async def send_credit_purchase_webhook(
    webhook_url: str,
    username: str,
    credits: int,
) -> bool:
    """Send a Discord webhook notification for a credit purchase."""
    if not webhook_url or not webhook_url.strip():
        return True
    webhook_url = webhook_url.strip()
    if not is_valid_webhook_url(webhook_url):
        logger.warning("Credit purchase webhook: invalid URL — skipping")
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    payload = {
        "embeds": [{
            "title": "💰 New Credit Purchase",
            "description": "A user purchased credits.",
            "color": 5763719,
            "fields": [
                {"name": "User",    "value": username,      "inline": True},
                {"name": "Credits", "value": str(credits),  "inline": True},
                {"name": "Time",    "value": timestamp,     "inline": False},
            ],
            "footer": {"text": "Hycroe Cloud"},
        }],
    }
    result = await _send_with_retry(webhook_url, payload)
    if result:
        logger.info("Credit purchase webhook sent: user=%s credits=%d", username, credits)
    return result


def send_credit_purchase_webhook_sync(
    webhook_url: str,
    username: str,
    credits: int,
) -> bool:
    """
    Synchronous wrapper for credit purchase webhook.
    Uses ThreadPoolExecutor + asyncio.run() — never asyncio.new_event_loop().
    """
    if not webhook_url or not webhook_url.strip():
        return True
    return _run_async_in_thread(
        send_credit_purchase_webhook(webhook_url, username, credits)
    )


# ── Ticket webhook ────────────────────────────────────────────────────────────

async def send_ticket_webhook(
    webhook_url: str,
    notify_user_id: Optional[str],
    username: str,
    ticket_title: str,
    ticket_message: str,
    ticket_id: int,
) -> bool:
    """Send a webhook notification when a ticket is created."""
    if not webhook_url or not webhook_url.strip():
        return True
    webhook_url = webhook_url.strip()
    if not is_valid_webhook_url(webhook_url):
        logger.warning("Ticket webhook: invalid URL — skipping")
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    mention = ""
    if notify_user_id and notify_user_id.strip():
        uid = notify_user_id.strip()
        mention = f"<@{uid}>" if uid.isdigit() else uid

    content = f"🎟 New support ticket {mention}".strip() if mention else "🎟 New support ticket"
    short_msg = ticket_message[:500] + ("…" if len(ticket_message) > 500 else "")
    payload = {
        "content": content,
        "embeds": [{
            "title": f"Ticket #{ticket_id}: {ticket_title}",
            "description": short_msg,
            "color": 255,
            "fields": [
                {"name": "User",   "value": username,  "inline": True},
                {"name": "Status", "value": "Open",    "inline": True},
                {"name": "Time",   "value": timestamp, "inline": False},
            ],
            "footer": {"text": "Hycroe Cloud — Support"},
        }],
    }
    result = await _send_with_retry(webhook_url, payload)
    if result:
        logger.info("Ticket webhook sent: ticket_id=%d user=%s", ticket_id, username)
    else:
        logger.warning("Ticket webhook failed: ticket_id=%d user=%s", ticket_id, username)
    return result


# ── Node status webhook ───────────────────────────────────────────────────────

async def send_node_status_webhook(
    webhook_url: str,
    node_name: str,
    cpu_cores: int,
    ram_used_mb: int,
    ram_total_mb: int,
    status: str,
) -> bool:
    """Send a Discord webhook notification with node status information."""
    if not webhook_url or not webhook_url.strip():
        return True
    webhook_url = webhook_url.strip()
    if not is_valid_webhook_url(webhook_url):
        logger.warning("Node status webhook: invalid URL — skipping")
        return False

    timestamp    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    status_emoji = "🟢" if status == "online" else "🔴"
    ram_used_gb  = round(ram_used_mb / 1024, 2)
    ram_total_gb = round(ram_total_mb / 1024, 2)
    ram_pct      = round((ram_used_mb / ram_total_mb) * 100, 1) if ram_total_mb > 0 else 0
    embed_color  = 3066993 if status == "online" else 15158332

    payload = {
        "embeds": [{
            "title": f"{status_emoji} Node Status — {node_name}",
            "color": embed_color,
            "fields": [
                {"name": "Status",    "value": status.capitalize(), "inline": True},
                {"name": "CPU Cores", "value": str(cpu_cores),      "inline": True},
                {
                    "name":   "RAM Usage",
                    "value":  f"{ram_used_gb} GB / {ram_total_gb} GB ({ram_pct}%)",
                    "inline": False,
                },
                {"name": "Checked At", "value": timestamp, "inline": False},
            ],
            "footer": {"text": "Hycroe Cloud — Node Monitor"},
        }],
    }

    result = await _send_with_retry(webhook_url, payload)
    if result:
        logger.info("Node status webhook sent: node=%s status=%s", node_name, status)
    else:
        logger.warning("Node status webhook failed: node=%s status=%s", node_name, status)
    return result


# ── Node event webhook ────────────────────────────────────────────────────────

async def send_node_event_webhook(
    webhook_url: str,
    node_name: str,
    event: str,
    detail: str = "",
) -> bool:
    """
    Send a node or VPS lifecycle event webhook.

    Supported events:
      Node:  online | offline | maintenance | added | removed
      VPS:   vps_deployed | vps_deleted

    Uses _send_with_retry() — 3 attempts, 8s timeout each.
    Failure is non-fatal.
    """
    if not webhook_url or not is_valid_webhook_url(webhook_url.strip()):
        return True

    emoji_map = {
        "online":       "🟢",
        "offline":      "🔴",
        "maintenance":  "🟡",
        "added":        "➕",
        "removed":      "🗑️",
        "vps_deployed": "🚀",
        "vps_deleted":  "🗑️",
    }
    emoji = emoji_map.get(event, "📡")
    color_map = {
        "online": 3066993, "added": 3066993, "vps_deployed": 3066993,
        "offline": 15158332, "removed": 15158332, "vps_deleted": 15158332,
        "maintenance": 16776960,
    }
    color     = color_map.get(event, 3447003)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    title = (
        f"{emoji} VPS {event.replace('vps_', '').capitalize()} — {node_name}"
        if event.startswith("vps_")
        else f"{emoji} Node {event.capitalize()} — {node_name}"
    )

    payload = {
        "embeds": [{
            "title": title,
            "color": color,
            "fields": [
                {"name": "Event",  "value": event.replace("_", " ").capitalize(), "inline": True},
                {"name": "Node",   "value": node_name,   "inline": True},
                {"name": "Detail", "value": detail or "—", "inline": False},
                {"name": "Time",   "value": timestamp,    "inline": False},
            ],
            "footer": {"text": "Hycroe Cloud — Node Events"},
        }],
    }

    logger.info("Firing node event webhook: event=%s node=%s detail=%s", event, node_name, detail or "—")
    result = await _send_with_retry(webhook_url.strip(), payload)
    if result:
        logger.info("Node event webhook SUCCESS: event=%s node=%s", event, node_name)
    else:
        logger.error("Node event webhook FAILED: event=%s node=%s", event, node_name)
    return result


# ── Admin log webhook ─────────────────────────────────────────────────────────

async def send_admin_log_webhook(
    webhook_url: str,
    actor: str,
    action: str,
    detail: str = "",
) -> bool:
    """Send an admin audit log event (VPS create/delete, user actions, admin actions)."""
    if not webhook_url or not is_valid_webhook_url(webhook_url.strip()):
        return True

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    payload = {
        "embeds": [{
            "title": f"📋 Admin Log — {action}",
            "color": 3447003,
            "fields": [
                {"name": "Actor",  "value": actor,       "inline": True},
                {"name": "Action", "value": action,      "inline": True},
                {"name": "Detail", "value": detail or "—", "inline": False},
                {"name": "Time",   "value": timestamp,   "inline": False},
            ],
            "footer": {"text": "Hycroe Cloud — Admin Log"},
        }],
    }
    result = await _send_with_retry(webhook_url.strip(), payload)
    if result:
        logger.info("Admin log webhook sent: actor=%s action=%s", actor, action)
    else:
        logger.warning("Admin log webhook failed: actor=%s action=%s", actor, action)
    return result


def fire_admin_log(webhook_url: str, actor: str, action: str, detail: str = "") -> None:
    """
    Synchronous fire-and-forget wrapper for admin log webhook.
    Uses ThreadPoolExecutor + asyncio.run() — never asyncio.new_event_loop().
    """
    if not webhook_url:
        return
    try:
        _run_async_in_thread(send_admin_log_webhook(webhook_url, actor, action, detail))
    except Exception as exc:
        logger.error("fire_admin_log error (non-fatal): %s", exc)


# ── Abuse / overload alert webhook ────────────────────────────────────────────

async def send_abuse_alert_webhook(
    webhook_url: str,
    vps_name: str,
    user_id: int,
    username: str,
    cpu_pct: float,
    node_name: str,
    action: str,          # "warning" | "stopped"
) -> bool:
    """
    Send a Discord embed when abuse detection fires a WARNING or STOP event.

    WHY separate from fire_admin_log: the abuse alert needs structured fields
    (CPU %, node, action colour-coding) that the generic admin log embed cannot
    express cleanly.  Keeping it separate also allows admins to route abuse
    alerts to a different channel from general admin logs.

    Color coding:
      warning → orange (0xFFA500)
      stopped → red    (0xFF0000)
    """
    if not webhook_url or not is_valid_webhook_url(webhook_url.strip()):
        return True  # no URL configured — silently skip

    action_lower = action.lower()
    if action_lower == "stopped":
        emoji = "🛑"
        color = 0xFF0000   # red
        title = f"🛑 VPS Auto-Stopped — CPU Overload"
        action_label = "Force-Stopped (suspended_overload)"
    else:
        emoji = "⚠️"
        color = 0xFFA500   # orange
        title = "⚠️ VPS CPU Warning — High Usage"
        action_label = "Warning (running — not stopped yet)"

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    payload = {
        "embeds": [{
            "title": title,
            "color": color,
            "fields": [
                {"name": "VPS",     "value": vps_name,           "inline": True},
                {"name": "Node",    "value": node_name,           "inline": True},
                {"name": "CPU",     "value": f"{round(cpu_pct, 1)}%", "inline": True},
                {"name": "User",    "value": f"{username} (id={user_id})", "inline": True},
                {"name": "Action",  "value": action_label,        "inline": False},
                {"name": "Time",    "value": timestamp,           "inline": False},
            ],
            "footer": {"text": "Hycroe Cloud — Abuse Monitor"},
        }],
    }

    result = await _send_with_retry(webhook_url.strip(), payload)
    if result:
        logger.info(
            "abuse_alert webhook sent: vps=%s user_id=%d cpu=%.1f action=%s",
            vps_name, user_id, cpu_pct, action,
        )
    else:
        logger.warning(
            "abuse_alert webhook FAILED: vps=%s user_id=%d action=%s",
            vps_name, user_id, action,
        )
    return result


def fire_abuse_alert_sync(
    webhook_url: str,
    vps_name: str,
    user_id: int,
    username: str,
    cpu_pct: float,
    node_name: str,
    action: str,
) -> None:
    """
    Synchronous fire-and-forget wrapper for the abuse alert webhook.
    Safe to call from any non-async context (e.g. worker thread).
    Never raises — all errors are logged.
    """
    if not webhook_url:
        return
    try:
        _run_async_in_thread(
            send_abuse_alert_webhook(
                webhook_url=webhook_url,
                vps_name=vps_name,
                user_id=user_id,
                username=username,
                cpu_pct=cpu_pct,
                node_name=node_name,
                action=action,
            )
        )
    except Exception as exc:
        logger.error("fire_abuse_alert_sync error (non-fatal): %s", exc)

