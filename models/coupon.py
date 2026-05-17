from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class Coupon(Base):
    __tablename__ = "coupons"

    id          = Column(Integer, primary_key=True)
    code        = Column(String, unique=True, nullable=False, index=True)
    credits     = Column(Integer, nullable=False)          # credits to award
    max_uses    = Column(Integer, nullable=True)            # None = unlimited
    times_used  = Column(Integer, default=0)
    is_active   = Column(Boolean, default=True)
    created_by  = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    expires_at  = Column(DateTime, nullable=True)

    redemptions = relationship("CouponRedemption", back_populates="coupon")
    creator     = relationship("User", foreign_keys=[created_by])


class CouponRedemption(Base):
    __tablename__ = "coupon_redemptions"

    id          = Column(Integer, primary_key=True)
    coupon_id   = Column(Integer, ForeignKey("coupons.id"), nullable=False)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    redeemed_at = Column(DateTime, default=datetime.utcnow)

    coupon = relationship("Coupon", back_populates="redemptions")
    user   = relationship("User")
