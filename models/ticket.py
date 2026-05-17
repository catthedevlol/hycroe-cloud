from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class Ticket(Base):
    __tablename__ = "tickets"

    id         = Column(Integer, primary_key=True)
    title      = Column(String(200), nullable=False)
    message    = Column(Text, nullable=False)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    status     = Column(String(20), default="open")  # open | in_progress | closed
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", backref="tickets", foreign_keys=[user_id])
