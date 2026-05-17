"""
services/rate_limit.py — Persistent, restart-safe rate limiter.

WHY: The original _user_last_create dict lives in process memory.
     On restart or with multiple uvicorn workers it resets / diverges,
     allowing users to bypass the cooldown trivially.

FIX: Store the last-create timestamp in the existing Job table
     (zero new migrations needed) keyed by a synthetic job_type
     "rate_limit:create_vps:<user_id>".  Falls back gracefully if
     the DB write fails — never blocks a legitimate request due to
     a rate-limit bookkeeping error.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Synthetic job_type used as the rate-limit record key
_RL_JOB_TYPE_PREFIX = "rate_limit:create_vps"


def check_and_stamp_create_rate_limit(
    db: Session,
    user_id: int,
    cooldown_secs: int = 15,
) -> tuple[bool, int]:
    """
    Check whether user_id is within the cooldown window.

    Returns (allowed: bool, retry_after_seconds: int).
    - allowed=True, retry_after=0  → proceed
    - allowed=False, retry_after>0 → reject with 429

    Side-effect: if allowed, writes/updates the timestamp row so the
    next call within cooldown_secs is rejected.

    If ANY DB error occurs the call is logged and (True, 0) is returned
    so a DB fault never permanently blocks VPS creation.
    """
    from models import Job  # local import avoids circular deps at module load

    job_type = f"{_RL_JOB_TYPE_PREFIX}:{user_id}"
    now = datetime.utcnow()

    try:
        record = db.query(Job).filter(Job.job_type == job_type).first()

        if record is not None and record.created_at is not None:
            elapsed = (now - record.created_at).total_seconds()
            remaining = cooldown_secs - elapsed
            if remaining > 0:
                retry_after = int(remaining) + 1
                logger.warning(
                    "rate_limit: user_id=%d is within cooldown (%.1fs remaining)",
                    user_id, remaining,
                )
                return False, retry_after

        # Upsert — update existing or create new
        if record is None:
            import uuid
            record = Job(
                job_id=str(uuid.uuid4()),
                job_type=job_type,
                user_id=user_id,
                status="done",
                created_at=now,
            )
            db.add(record)
        else:
            record.created_at = now

        db.commit()
        logger.debug("rate_limit: stamped user_id=%d at %s", user_id, now.isoformat())
        return True, 0

    except Exception as exc:
        logger.error(
            "rate_limit: DB error for user_id=%d (allowing request): %s",
            user_id, exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
        # Fail open — a DB error must not permanently lock out users
        return True, 0
