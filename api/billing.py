"""
Billing API — Stripe, Razorpay (UPI), NOWPayments (Crypto).
"""
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

import stripe
import httpx
from fastapi import APIRouter, Form, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import get_db
from models import Transaction, Coupon, CouponRedemption, User
from services.auth import AuthService
from services.billing import BillingService, CREDIT_PACKAGES
from services.webhook import send_credit_purchase_webhook


async def _fire_webhook(db, user: User, credits: int):
    """Helper: fire Discord webhook if one is configured in panel settings."""
    try:
        from models import PanelSettings
        s = db.query(PanelSettings).filter(PanelSettings.id == 1).first()
        webhook_url = getattr(s, "discord_webhook_url", None) if s else None
        if webhook_url:
            await send_credit_purchase_webhook(webhook_url, user.username, credits)
    except Exception as exc:
        logger.warning("Webhook dispatch failed (non-fatal): %s", exc)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

# ── Razorpay config ─────────────────────────────────────────────────────────
RZP_KEY_ID     = os.getenv("RAZORPAY_KEY_ID", "")
RZP_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RZP_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

# ── NOWPayments config ───────────────────────────────────────────────────────
NP_API_KEY      = os.getenv("NOWPAYMENTS_API_KEY", "")
NP_IPN_SECRET   = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
NP_BASE         = "https://api.nowpayments.io/v1"

CRYPTO_COINS = ["BTC", "ETH", "USDTTRC20", "USDTERC20", "LTC", "SOL"]
CRYPTO_LABELS = {
    "BTC": "Bitcoin (BTC)",
    "ETH": "Ethereum (ETH)",
    "USDTTRC20": "USDT (TRC-20)",
    "USDTERC20": "USDT (ERC-20)",
    "LTC": "Litecoin (LTC)",
    "SOL": "Solana (SOL)",
}

# ── Billing page ─────────────────────────────────────────────────────────────

@router.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request, db: Session = Depends(get_db)):
    user = AuthService.get_user(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)
    transactions = (
        db.query(Transaction)
        .filter(Transaction.user_id == user.id)
        .order_by(Transaction.created_at.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse("billing.html", {
        "request": request, "user": user,
        "transactions": transactions,
        "packages": CREDIT_PACKAGES,
        "stripe_pub": os.getenv("STRIPE_PUBLIC_KEY", ""),
        "razorpay_enabled": bool(RZP_KEY_ID and RZP_KEY_SECRET),
        "razorpay_key_id": RZP_KEY_ID,
        "nowpayments_enabled": bool(NP_API_KEY),
        "crypto_labels": CRYPTO_LABELS,
        "crypto_coins": CRYPTO_COINS,
        "low_credits": user.credits < 200,
    })


# ── Stripe ───────────────────────────────────────────────────────────────────

@router.post("/billing/checkout")
async def billing_checkout(
    package_id:     str = Form(...),
    payment_method: str = Form("stripe"),
    request: Request = None,
    db: Session = Depends(get_db),
):
    user = AuthService.get_user(request, db)
    if not user:
        raise HTTPException(401)
    pkg = BillingService.get_package(package_id)
    if not pkg:
        raise HTTPException(400, "Invalid package")

    if payment_method == "upi":
        return await _razorpay_checkout(request, user, pkg, db)
    if payment_method == "crypto":
        coin = (await request.form()).get("crypto_coin", "BTC")
        return await _nowpayments_checkout(request, user, pkg, coin, db)

    # Default: Stripe
    if not stripe.api_key:
        raise HTTPException(503, "Stripe not configured")
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"Hycroe Credits – {pkg['label']}"},
                    "unit_amount": pkg["price"],
                },
                "quantity": 1,
            }],
            mode="payment",
            metadata={"user_id": str(user.id), "credits": str(pkg["credits"]), "package": package_id},
            success_url=f"{base_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/billing",
        )
        return RedirectResponse(session.url, status_code=303)
    except stripe.error.StripeError as e:
        raise HTTPException(500, str(e))


@router.get("/billing/success")
async def billing_success(session_id: str, request: Request, db: Session = Depends(get_db)):
    user = AuthService.get_user(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)
    if not stripe.api_key:
        return RedirectResponse("/billing?error=stripe_not_configured", status_code=303)
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status == "paid":
            credits = int(session.metadata.get("credits", 0))
            existing = db.query(Transaction).filter(
                Transaction.gateway_payment_id == session_id
            ).first()
            if not existing:
                BillingService.add_credits(
                    db, user, credits,
                    f"Purchased {credits} credits via card",
                    "purchase", stripe_id=session_id,
                    gateway="stripe", gateway_payment_id=session_id,
                    currency="USD",
                )
                db.commit()
                await _fire_webhook(db, user, credits)
    except Exception:
        pass
    return RedirectResponse("/billing?success=credits_added", status_code=303)


@router.post("/billing/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if webhook_secret:
        try:
            event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
        except Exception:
            raise HTTPException(400, "Invalid signature")
    else:
        event = json.loads(payload)

    if event["type"] == "checkout.session.completed":
        s = event["data"]["object"]
        if s["payment_status"] == "paid":
            uid = int(s["metadata"]["user_id"])
            credits = int(s["metadata"]["credits"])
            existing = db.query(Transaction).filter(
                Transaction.gateway_payment_id == s["id"]
            ).first()
            if not existing:
                u = db.query(User).filter(User.id == uid).first()
                if u:
                    BillingService.add_credits(
                        db, u, credits,
                        f"Purchased {credits} credits via card",
                        "purchase", stripe_id=s["id"],
                        gateway="stripe", gateway_payment_id=s["id"],
                        currency="USD",
                    )
                    db.commit()
                    await _fire_webhook(db, u, credits)
    return JSONResponse({"status": "ok"})


# ── Razorpay (UPI) ───────────────────────────────────────────────────────────

async def _razorpay_checkout(request: Request, user: User, pkg: dict, db: Session):
    """Create Razorpay order and redirect to UPI payment page."""
    if not (RZP_KEY_ID and RZP_KEY_SECRET):
        raise HTTPException(503, "Razorpay not configured")

    amount_paise = BillingService.inr_amount(pkg["id"])
    order_data = {
        "amount": amount_paise,
        "currency": "INR",
        "receipt": f"hycroe_{user.id}_{int(time.time())}",
        "notes": {"user_id": str(user.id), "credits": str(pkg["credits"]), "package": pkg["id"]},
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.razorpay.com/v1/orders",
                json=order_data,
                auth=(RZP_KEY_ID, RZP_KEY_SECRET),
            )
            resp.raise_for_status()
            order = resp.json()
    except Exception as exc:
        logger.error("Razorpay order creation failed: %s", exc)
        raise HTTPException(502, "Payment gateway error")

    # Store pending transaction
    BillingService.add_transaction(
        db, user.id, 0,
        f"UPI payment pending – {pkg['credits']} credits",
        "upi",
        gateway="razorpay",
        gateway_payment_id=order["id"],
        gateway_status="pending",
        currency="INR",
        amount_paid=str(amount_paise / 100),
    )
    db.commit()

    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    return templates.TemplateResponse("billing_upi.html", {
        "request": request, "user": user, "pkg": pkg,
        "order": order,
        "rzp_key_id": RZP_KEY_ID,
        "amount_paise": amount_paise,
        "callback_url": f"{base_url}/billing/razorpay/callback",
    })


@router.post("/billing/razorpay/callback")
async def razorpay_callback(
    razorpay_order_id:   str = Form(...),
    razorpay_payment_id: str = Form(...),
    razorpay_signature:  str = Form(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """Verify Razorpay signature and grant credits."""
    user = AuthService.get_user(request, db)
    if not user:
        raise HTTPException(401)

    # Signature verification
    msg = f"{razorpay_order_id}|{razorpay_payment_id}"
    expected = hmac.new(
        RZP_KEY_SECRET.encode(), msg.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, razorpay_signature):
        logger.warning("Razorpay signature mismatch for user %s", user.id)
        return RedirectResponse("/billing?error=payment_signature_invalid", status_code=303)

    # Find pending transaction by order ID
    tx = db.query(Transaction).filter(
        Transaction.gateway_payment_id == razorpay_order_id,
        Transaction.gateway == "razorpay",
        Transaction.gateway_status == "pending",
    ).first()

    if not tx:
        return RedirectResponse("/billing?error=payment_not_found", status_code=303)

    # Find package from description
    # Parse credits from description "UPI payment pending – X credits"
    try:
        credits = int(tx.description.split("–")[1].strip().split(" ")[0])
    except Exception:
        return RedirectResponse("/billing?error=payment_error", status_code=303)

    # Ensure not already processed
    already = db.query(Transaction).filter(
        Transaction.gateway_payment_id == razorpay_payment_id,
        Transaction.gateway_status == "completed",
    ).first()
    if already:
        return RedirectResponse("/billing?success=credits_added", status_code=303)

    # Grant credits
    BillingService.add_credits(
        db, user, credits,
        f"Purchased {credits} credits via UPI",
        "upi",
        gateway="razorpay",
        gateway_payment_id=razorpay_payment_id,
        gateway_status="completed",
        currency="INR",
        amount_paid=tx.amount_paid,
    )
    # Mark pending tx as done
    tx.gateway_status = "completed"
    tx.amount = credits
    db.commit()

    await _fire_webhook(db, user, credits)
    logger.info("UPI payment successful: user=%s credits=%d payment=%s",
                user.username, credits, razorpay_payment_id)
    return RedirectResponse("/billing?success=credits_added", status_code=303)


@router.post("/billing/razorpay/webhook")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """Razorpay webhook for async payment confirmation."""
    payload = await request.body()
    sig = request.headers.get("x-razorpay-signature", "")

    if RZP_WEBHOOK_SECRET:
        expected = hmac.new(
            RZP_WEBHOOK_SECRET.encode(), payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise HTTPException(400, "Invalid signature")

    event = json.loads(payload)
    if event.get("event") == "payment.captured":
        payment = event["payload"]["payment"]["entity"]
        order_id = payment.get("order_id")
        payment_id = payment.get("id")
        notes = payment.get("notes", {})
        uid = int(notes.get("user_id", 0))
        credits = int(notes.get("credits", 0))

        if uid and credits:
            already = db.query(Transaction).filter(
                Transaction.gateway_payment_id == payment_id,
                Transaction.gateway_status == "completed",
            ).first()
            if not already:
                u = db.query(User).filter(User.id == uid).first()
                if u:
                    BillingService.add_credits(
                        db, u, credits,
                        f"Purchased {credits} credits via UPI",
                        "upi", gateway="razorpay",
                        gateway_payment_id=payment_id,
                        gateway_status="completed",
                        currency="INR",
                    )
                    db.commit()
                    await _fire_webhook(db, u, credits)
                    logger.info("Razorpay webhook: %d credits → user %d", credits, uid)
    return JSONResponse({"status": "ok"})


# ── NOWPayments (Crypto) ──────────────────────────────────────────────────────

async def _nowpayments_checkout(
    request: Request, user: User, pkg: dict, coin: str, db: Session
):
    """Create NOWPayments invoice."""
    if not NP_API_KEY:
        raise HTTPException(503, "Crypto payments not configured")

    if coin not in CRYPTO_COINS:
        coin = "BTC"

    usd_amount = pkg["usd"]
    base_url = os.getenv("BASE_URL", "http://localhost:8000")

    payload = {
        "price_amount": usd_amount,
        "price_currency": "usd",
        "pay_currency": coin.lower(),
        "order_id": f"hycroe_{user.id}_{int(time.time())}",
        "order_description": f"Hycroe {pkg['label']} – {pkg['credits']} credits",
        "ipn_callback_url": f"{base_url}/billing/nowpayments/webhook",
        "success_url": f"{base_url}/billing/nowpayments/success",
        "cancel_url": f"{base_url}/billing",
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{NP_BASE}/invoice",
                json=payload,
                headers={"x-api-key": NP_API_KEY, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            invoice = resp.json()
    except Exception as exc:
        logger.error("NOWPayments invoice creation failed: %s", exc)
        raise HTTPException(502, "Crypto payment gateway error")

    invoice_id = invoice.get("id", "")
    invoice_url = invoice.get("invoice_url", "")

    # Store pending transaction
    BillingService.add_transaction(
        db, user.id, 0,
        f"Crypto payment pending – {pkg['credits']} credits ({coin})",
        "crypto",
        gateway="nowpayments",
        gateway_payment_id=invoice_id,
        gateway_status="pending",
        currency=coin,
        amount_paid=str(usd_amount),
    )
    db.commit()

    if invoice_url:
        return RedirectResponse(invoice_url, status_code=303)

    # Fallback: show payment page manually
    return templates.TemplateResponse("billing_crypto.html", {
        "request": request, "user": user, "pkg": pkg, "invoice": invoice,
        "coin": coin, "coin_label": CRYPTO_LABELS.get(coin, coin),
    })


@router.get("/billing/nowpayments/success")
def nowpayments_success(request: Request, db: Session = Depends(get_db)):
    user = AuthService.get_user(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/billing?msg=crypto_pending", status_code=303)


@router.post("/billing/nowpayments/webhook")
async def nowpayments_webhook(request: Request, db: Session = Depends(get_db)):
    """NOWPayments IPN webhook — grants credits on confirmed payment."""
    payload = await request.body()
    sig = request.headers.get("x-nowpayments-sig", "")

    if NP_IPN_SECRET:
        import json as _json
        # NOWPayments signs the sorted JSON body
        try:
            body_dict = _json.loads(payload)
            sorted_body = _json.dumps(body_dict, sort_keys=True, separators=(",", ":"))
            expected = hmac.new(
                NP_IPN_SECRET.encode(), sorted_body.encode(), hashlib.sha512
            ).hexdigest()
            if not hmac.compare_digest(expected, sig):
                raise HTTPException(400, "Invalid IPN signature")
        except json.JSONDecodeError:
            raise HTTPException(400, "Invalid JSON")

    data = json.loads(payload)
    payment_id   = str(data.get("payment_id", ""))
    payment_status = data.get("payment_status", "")
    order_id     = data.get("order_id", "")      # e.g. "hycroe_42_1710000000"
    pay_currency = (data.get("pay_currency") or "").upper()
    pay_amount   = str(data.get("actually_paid", ""))

    CONFIRMED_STATUSES = {"finished", "confirmed", "partially_paid"}
    if payment_status not in CONFIRMED_STATUSES:
        return JSONResponse({"status": "noted"})

    # Parse user_id and credits from order_id
    try:
        parts = order_id.split("_")
        uid = int(parts[1])
    except Exception:
        logger.warning("NOWPayments webhook: bad order_id %s", order_id)
        return JSONResponse({"status": "bad_order"})

    # Find the pending transaction
    tx = db.query(Transaction).filter(
        Transaction.gateway == "nowpayments",
        Transaction.user_id == uid,
        Transaction.gateway_status == "pending",
    ).order_by(Transaction.id.desc()).first()

    if not tx:
        return JSONResponse({"status": "no_pending_tx"})

    # Prevent double-credit
    already = db.query(Transaction).filter(
        Transaction.gateway_payment_id == payment_id,
        Transaction.gateway == "nowpayments",
        Transaction.gateway_status == "completed",
    ).first()
    if already:
        return JSONResponse({"status": "already_processed"})

    # Parse credits from description "Crypto payment pending – X credits ..."
    try:
        credits = int(tx.description.split("–")[1].strip().split(" ")[0])
    except Exception:
        return JSONResponse({"status": "parse_error"})

    u = db.query(User).filter(User.id == uid).first()
    if not u:
        return JSONResponse({"status": "user_not_found"})

    BillingService.add_credits(
        db, u, credits,
        f"Purchased {credits} credits via crypto ({pay_currency})",
        "crypto",
        gateway="nowpayments",
        gateway_payment_id=payment_id,
        gateway_status="completed",
        currency=pay_currency,
        amount_paid=pay_amount,
    )
    tx.gateway_status = "completed"
    tx.amount = credits
    db.commit()

    await _fire_webhook(db, u, credits)
    logger.info("NOWPayments webhook: %d credits → user %d (payment %s status=%s)",
                credits, uid, payment_id, payment_status)
    return JSONResponse({"status": "ok"})


# ── Coupon redeem ─────────────────────────────────────────────────────────────

@router.post("/billing/redeem")
async def redeem_coupon(request: Request, db: Session = Depends(get_db)):
    from datetime import datetime
    user = AuthService.get_user(request, db)
    if not user:
        raise HTTPException(401)

    body = await request.json()
    code = (body.get("code") or "").strip().upper()
    if not code:
        return JSONResponse({"success": False, "error": "Please enter a coupon code."}, status_code=400)

    coupon = db.query(Coupon).filter(Coupon.code == code).first()
    if not coupon or not coupon.is_active:
        return JSONResponse({"success": False, "error": "Invalid or expired coupon code."}, status_code=400)

    if coupon.expires_at and coupon.expires_at < datetime.utcnow():
        return JSONResponse({"success": False, "error": "This coupon has expired."}, status_code=400)

    if coupon.max_uses is not None and coupon.times_used >= coupon.max_uses:
        return JSONResponse({"success": False, "error": "This coupon has reached its usage limit."}, status_code=400)

    already = db.query(CouponRedemption).filter(
        CouponRedemption.coupon_id == coupon.id,
        CouponRedemption.user_id == user.id,
    ).first()
    if already:
        return JSONResponse({"success": False, "error": "You have already redeemed this coupon."}, status_code=400)

    BillingService.add_credits(
        db, user, coupon.credits,
        f"Coupon redeemed: {coupon.code}",
        "coupon", gateway="coupon",
    )
    redemption = CouponRedemption(coupon_id=coupon.id, user_id=user.id)
    db.add(redemption)
    coupon.times_used += 1
    db.commit()

    return JSONResponse({"success": True, "credits": coupon.credits})
