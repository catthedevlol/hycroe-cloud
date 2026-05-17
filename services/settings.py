"""
Panel-wide settings service.
Loads from DB with a short in-process cache so every template render
doesn't hammer the database.
"""
import time
from typing import Optional
from sqlalchemy.orm import Session


_cache: Optional[dict] = None
_cache_ts: float = 0.0
_CACHE_TTL = 30.0   # seconds

DEFAULTS = {
    "panel_name":           "Hycroe Panel",
    "panel_description":    "Infrastructure management for your cluster.",
    "theme_color":          "#0066FF",
    "logo_url":             None,
    "enable_registration":  True,
    "enable_discord_login": True,
    "enable_billing":       True,
    "require_discord_verify": False,
    "discord_webhook_url":  None,
    "notify_user_id":       None,
    "node_webhook_url":     None,
    "admin_log_webhook_url": None,
    "abuse_alert_webhook_url": None,   # CPU overload warnings / auto-stops
    "announcement":         None,
}


def get_settings(db: Session) -> dict:
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache and (now - _cache_ts) < _CACHE_TTL:
        return _cache

    from models import PanelSettings
    row = db.query(PanelSettings).filter(PanelSettings.id == 1).first()
    if not row:
        # Auto-create default row
        row = PanelSettings(id=1)
        db.add(row)
        try:
            db.commit()
        except Exception:
            db.rollback()

    result = dict(DEFAULTS)
    if row:
        result.update({
            "panel_name":           row.panel_name           or DEFAULTS["panel_name"],
            "panel_description":    row.panel_description    or DEFAULTS["panel_description"],
            "theme_color":          row.theme_color          or DEFAULTS["theme_color"],
            "logo_url":             row.logo_url,
            "enable_registration":  bool(row.enable_registration  if row.enable_registration  is not None else True),
            "enable_discord_login": bool(row.enable_discord_login if row.enable_discord_login is not None else True),
            "enable_billing":       bool(row.enable_billing       if row.enable_billing       is not None else True),
            "require_discord_verify": bool(row.require_discord_verify if row.require_discord_verify is not None else False),
            "discord_webhook_url":  getattr(row, "discord_webhook_url", None),
            "notify_user_id":       getattr(row, "notify_user_id", None),
            "node_webhook_url":     getattr(row, "node_webhook_url", None),
            "admin_log_webhook_url": getattr(row, "admin_log_webhook_url", None),
            "abuse_alert_webhook_url": getattr(row, "abuse_alert_webhook_url", None),
            "announcement":         getattr(row, "announcement", None),
        })

    _cache = result
    _cache_ts = now
    return result


def invalidate_cache():
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0.0
