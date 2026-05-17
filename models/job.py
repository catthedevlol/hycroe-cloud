from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text
from datetime import datetime
from .base import Base


class Job(Base):
    __tablename__ = "jobs"

    id         = Column(Integer, primary_key=True)
    job_id     = Column(String, unique=True, nullable=False, index=True)
    job_type   = Column(String, nullable=False)    # create_vps / backup / metrics / rebuild
    status     = Column(String, default="pending") # pending/running/done/failed
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=True)
    vps_id     = Column(Integer, ForeignKey("vps.id"), nullable=True)
    payload    = Column(Text, nullable=True)       # JSON
    result     = Column(Text, nullable=True)       # JSON
    error      = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at= Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
