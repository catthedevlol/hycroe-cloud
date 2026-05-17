from sqlalchemy.orm import Session
from models import User, Transaction

CREDIT_PACKAGES = [
    {"id": "credits_500",  "credits": 500,  "price": 499,  "label": "Starter",    "usd": 4.99},
    {"id": "credits_1500", "credits": 1500, "price": 999,  "label": "Pro",         "usd": 9.99},
    {"id": "credits_5000", "credits": 5000, "price": 2499, "label": "Enterprise",  "usd": 24.99},
]

# INR prices (1 USD ≈ 83 INR at time of writing)
INR_MULTIPLIER = 83

def _inr(usd_cents: int) -> int:
    """Convert USD cents to INR paise (100 paise = 1 INR)."""
    return round((usd_cents / 100) * INR_MULTIPLIER * 100)


class BillingService:

    @staticmethod
    def add_transaction(
        db: Session, user_id: int, amount: int,
        description: str, tx_type: str,
        stripe_id: str = None,
        gateway: str = None,
        gateway_payment_id: str = None,
        gateway_status: str = "completed",
        currency: str = None,
        amount_paid: str = None,
    ):
        tx = Transaction(
            user_id=user_id,
            amount=amount,
            description=description,
            tx_type=tx_type,
            stripe_payment_id=stripe_id,
            gateway=gateway or ("stripe" if stripe_id else None),
            gateway_payment_id=gateway_payment_id or stripe_id,
            gateway_status=gateway_status,
            currency=currency,
            amount_paid=amount_paid,
        )
        db.add(tx)

    @classmethod
    def deduct(cls, db: Session, user: User, amount: int, description: str) -> bool:
        if user.credits < amount:
            return False
        user.credits -= amount
        cls.add_transaction(db, user.id, -amount, description, "deduction")
        return True

    @classmethod
    def add_credits(
        cls, db: Session, user: User, amount: int,
        description: str, tx_type: str = "purchase",
        stripe_id: str = None,
        gateway: str = None,
        gateway_payment_id: str = None,
        gateway_status: str = "completed",
        currency: str = None,
        amount_paid: str = None,
    ):
        user.credits += amount
        cls.add_transaction(
            db, user.id, amount, description, tx_type,
            stripe_id=stripe_id,
            gateway=gateway,
            gateway_payment_id=gateway_payment_id,
            gateway_status=gateway_status,
            currency=currency,
            amount_paid=amount_paid,
        )

    @staticmethod
    def get_package(package_id: str) -> dict:
        return next((p for p in CREDIT_PACKAGES if p["id"] == package_id), None)

    @staticmethod
    def inr_amount(package_id: str) -> int:
        """Return INR paise for Razorpay."""
        pkg = next((p for p in CREDIT_PACKAGES if p["id"] == package_id), None)
        if not pkg:
            return 0
        return _inr(pkg["price"])
