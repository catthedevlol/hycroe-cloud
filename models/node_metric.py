from sqlalchemy import Column, Integer, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class NodeMetric(Base):
    __tablename__ = "node_metrics"

    id             = Column(Integer, primary_key=True)
    node_id        = Column(Integer, ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    cpu_pct        = Column(Float, default=0.0)
    ram_used_mb    = Column(Integer, default=0)
    ram_total_mb   = Column(Integer, default=0)
    disk_used_gb   = Column(Integer, default=0)
    disk_total_gb  = Column(Integer, default=0)
    instance_count = Column(Integer, default=0)
    recorded_at    = Column(DateTime, default=datetime.utcnow)

    node = relationship("Node")
