from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class VPSPlan(Base):
    __tablename__ = "vps_plans"

    id           = Column(Integer, primary_key=True)
    name         = Column(String, nullable=False)
    ram_mb       = Column(Integer, nullable=False)      # MB
    cpu          = Column(Integer, nullable=False)
    disk_gb      = Column(Integer, nullable=False)
    credits_cost = Column(Integer, nullable=False)
    instance_type = Column(String, default="container")  # "container" or "vm"
    location     = Column(String, nullable=True)
    node_id      = Column(Integer, ForeignKey("nodes.id"), nullable=True)
    is_active    = Column(Boolean, default=True)
    created_by   = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

    node    = relationship("Node")
    creator = relationship("User", foreign_keys=[created_by])

    @property
    def display_ram(self):
        if self.ram_mb >= 1024:
            return f"{self.ram_mb // 1024} GB"
        return f"{self.ram_mb} MB"
