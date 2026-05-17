from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    username        = Column(String, unique=True, index=True, nullable=False)
    email           = Column(String, unique=True, nullable=True)
    password        = Column(String, nullable=True)

    # Discord
    discord_id       = Column(String, unique=True, nullable=True, index=True)
    discord_username = Column(String, nullable=True)
    discord_avatar   = Column(String, nullable=True)   # hash only, or full URL
    discord_verified = Column(Boolean, default=False)
    avatar_url       = Column(String, nullable=True)   # legacy / computed

    credits         = Column(Integer, default=0)
    is_admin        = Column(Boolean, default=False)
    is_suspended    = Column(Boolean, default=False)
    suspend_reason  = Column(String, nullable=True)
    two_fa_secret   = Column(String, nullable=True)
    last_login      = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

    vps          = relationship("VPS", back_populates="owner")
    transactions = relationship("Transaction", back_populates="user")

    @property
    def display_avatar(self) -> str | None:
        """Return a usable avatar URL (discord CDN preferred, then avatar_url)."""
        if self.discord_id and self.discord_avatar:
            return f"https://cdn.discordapp.com/avatars/{self.discord_id}/{self.discord_avatar}.png"
        return self.avatar_url
