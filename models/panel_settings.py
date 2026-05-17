from sqlalchemy import Column, Integer, String, Boolean
from .base import Base


class PanelSettings(Base):
    __tablename__ = "panel_settings"

    id                   = Column(Integer, primary_key=True)
    panel_name           = Column(String,  default="Hycroe Panel")
    panel_description    = Column(String,  default="Infrastructure management for your cluster.")
    theme_color          = Column(String,  default="#3b82f6")
    logo_url             = Column(String,  nullable=True)
    enable_registration  = Column(Boolean, default=True)
    enable_discord_login = Column(Boolean, default=True)
    enable_billing       = Column(Boolean, default=True)
    require_discord_verify = Column(Boolean, default=False)
    discord_webhook_url  = Column(String,  nullable=True)   # kept for billing/ticket events
    notify_user_id       = Column(String,  nullable=True)   # User ID to @mention in ticket notifications
    node_webhook_url     = Column(String,  nullable=True)   # node start/offline/status events
    admin_log_webhook_url = Column(String, nullable=True)   # user actions, VPS create/delete, admin actions
    abuse_alert_webhook_url = Column(String, nullable=True) # CPU overload warnings and auto-stops
    announcement         = Column(String,  nullable=True)   # global banner shown on dashboard (markdown-free)
