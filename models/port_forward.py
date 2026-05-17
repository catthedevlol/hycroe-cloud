from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class PortForward(Base):
    __tablename__ = "port_forwards"

    id           = Column(Integer, primary_key=True)
    vps_id       = Column(Integer, ForeignKey("vps.id"), nullable=False)
    protocol     = Column(String, default="tcp")          # tcp / udp
    host_port    = Column(Integer, nullable=False)
    container_port = Column(Integer, nullable=False)
    description  = Column(String, nullable=True)
    active       = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

    vps = relationship("VPS", back_populates="forwards")
