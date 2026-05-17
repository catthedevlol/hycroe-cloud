from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class Transaction(Base):
    __tablename__ = "transactions"

    id                = Column(Integer, primary_key=True)
    user_id           = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount            = Column(Integer, nullable=False)   # positive=credit, negative=debit
    description       = Column(String, nullable=False)
    tx_type           = Column(String, nullable=False)    # purchase/deduction/refund/admin/coupon/upi/crypto
    stripe_payment_id = Column(String, nullable=True)
    # Multi-gateway payment tracking
    gateway           = Column(String, nullable=True)     # stripe / razorpay / nowpayments / manual
    gateway_payment_id = Column(String, nullable=True)    # external payment ID from any gateway
    gateway_status    = Column(String, nullable=True)     # pending / completed / failed
    currency          = Column(String, nullable=True)     # USD / INR / BTC / ETH / USDT
    amount_paid       = Column(String, nullable=True)     # raw paid amount (string for crypto precision)
    created_at        = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="transactions")
