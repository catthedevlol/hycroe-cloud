from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class VpsIP(Base):
    __tablename__ = "vps_ips"

    id         = Column(Integer, primary_key=True)
    vps_id     = Column(Integer, ForeignKey("vps.id"), nullable=False)
    address    = Column(String, nullable=False)      # IP address
    family     = Column(String, default="ipv4")      # ipv4 / ipv6
    is_primary = Column(Boolean, default=False)
    netmask    = Column(String, nullable=True)
    gateway    = Column(String, nullable=True)
    assigned_at = Column(DateTime, default=datetime.utcnow)

    vps = relationship("VPS", back_populates="ips")
