import secrets
import time
from collections import defaultdict
from typing import Optional, Dict
import bcrypt
import os
from fastapi import Request, Response
from sqlalchemy.orm import Session
from models import User


# In-process session store (replace with Redis for multi-worker deployments)
_sessions: Dict[str, int] = {}
_rate_limits: Dict[str, list] = defaultdict(list)


class AuthService:

    # ── Sessions ──────────────────────────────────────────────────────────────

    @staticmethod
    def create_session(response: Response, user_id: int) -> str:
        token = secrets.token_urlsafe(48)
        _sessions[token] = user_id
        response.set_cookie(
            key="session_token",
            value=token,
            httponly=True,
            secure=os.getenv("SECURE_COOKIES", "false").lower() == "true",
            samesite="lax",
            max_age=86400 * 7,
        )
        return token

    @staticmethod
    def destroy_session(request: Request, response: Response) -> None:
        token = request.cookies.get("session_token")
        if token and token in _sessions:
            del _sessions[token]
        response.delete_cookie("session_token")

    @staticmethod
    def get_user(request: Request, db: Session) -> Optional[User]:
        token = request.cookies.get("session_token")
        if not token or token not in _sessions:
            return None
        return db.query(User).filter(User.id == _sessions[token]).first()

    @staticmethod
    def get_user_by_token(token: str, db: Session) -> Optional[User]:
        if not token or token not in _sessions:
            return None
        return db.query(User).filter(User.id == _sessions[token]).first()

    # ── Password ──────────────────────────────────────────────────────────────

    @staticmethod
    def hash_password(plain: str) -> str:
        return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_password(plain: str, hashed: str) -> bool:
        try:
            return bcrypt.checkpw(plain.encode(), hashed.encode())
        except Exception:
            return False

    # ── Rate limiting ─────────────────────────────────────────────────────────

    @staticmethod
    def rate_limit(key: str, max_req: int = 10, window: int = 60) -> bool:
        now = time.time()
        hits = [t for t in _rate_limits[key] if now - t < window]
        _rate_limits[key] = hits
        if len(hits) >= max_req:
            return False
        _rate_limits[key].append(now)
        return True

    # ── CSRF ──────────────────────────────────────────────────────────────────

    @staticmethod
    def generate_csrf_token() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def validate_csrf(request: Request, token: str) -> bool:
        # For session-based apps, simple double-submit cookie pattern
        cookie_token = request.cookies.get("csrf_token", "")
        return secrets.compare_digest(cookie_token, token) if cookie_token else False
