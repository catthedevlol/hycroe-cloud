from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class VPS(Base):
    __tablename__ = "vps"

    id           = Column(Integer, primary_key=True)
    name         = Column(String, unique=True, index=True, nullable=False)
    display_name = Column(String, nullable=True)
    instance_type = Column(String, default="container")        # container / vm
    ram          = Column(Integer, nullable=False)           # MB
    cpu          = Column(Integer, nullable=False)           # vCPU count
    disk_gb      = Column(Integer, default=20)
    os_image     = Column(String, default="ubuntu/22.04")
    status       = Column(String, default="stopped")        # running/stopped/error/building
    suspended    = Column(Boolean, default=False)
    node_id      = Column(Integer, ForeignKey("nodes.id"), nullable=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    ipv4         = Column(String, nullable=True)
    ipv6         = Column(String, nullable=True)
    notes        = Column(Text, nullable=True)
    build_job_id = Column(String, nullable=True)            # background job ref
    created_at   = Column(DateTime, default=datetime.utcnow)
    last_action  = Column(DateTime, nullable=True)
    expires_at   = Column(DateTime, nullable=True)  # Renewal timer — set to created_at + 30 days

    owner    = relationship("User", back_populates="vps")
    node     = relationship("Node", back_populates="vps_list")
    backups  = relationship("Backup", back_populates="vps")
    ips      = relationship("VpsIP", back_populates="vps")
    forwards = relationship("PortForward", back_populates="vps")
