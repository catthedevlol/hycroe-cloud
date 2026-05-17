from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class Backup(Base):
    __tablename__ = "backups"

    id            = Column(Integer, primary_key=True)
    vps_id        = Column(Integer, ForeignKey("vps.id"), nullable=False)
    snapshot_name = Column(String, nullable=False)
    description   = Column(String, nullable=True)
    size_mb       = Column(Integer, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)

    vps = relationship("VPS", back_populates="backups")
