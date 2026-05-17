"""
Authentication routes — login, register, logout, Discord OAuth.
Respects PanelSettings for registration / Discord toggle.
"""
import os
import logging

from fastapi import APIRouter, Form, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from authlib.integrations.starlette_client import OAuth

from database import get_db
from models import User
from services.auth import AuthService
from services.settings import get_settings

router = APIRouter()
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)

# Discord OAuth setup
oauth = OAuth()
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
if DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET:
    oauth.register(
        name="discord",
        client_id=DISCORD_CLIENT_ID,
        client_secret=DISCORD_CLIENT_SECRET,
        authorize_url="https://discord.com/api/oauth2/authorize",
        access_token_url="https://discord.com/api/oauth2/token",
        api_base_url="https://discord.com/api/",
        client_kwargs={"scope": "identify email"},
    )


def _discord_active(settings: dict) -> bool:
    return bool(DISCORD_CLIENT_ID) and settings.get("enable_discord_login", True)



@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    """Show the login form. Redirects to /dashboard if already authenticated."""
    user = AuthService.get_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    settings = get_settings(db)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "settings": settings,
        "discord_enabled": _discord_active(settings),
    })


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    settings = get_settings(db)
    if not settings.get("enable_registration", True):
        return RedirectResponse("/?error=registration_disabled", status_code=303)
    return templates.TemplateResponse("register.html", {
        "request": request,
        "settings": settings,
        "discord_enabled": _discord_active(settings),
    })


@router.post("/register")
def register(
    username: str = Form(...),
    password: str = Form(...),
    email:    str = Form(""),
    request:  Request = None,
    db:       Session = Depends(get_db),
):
    settings = get_settings(db)
    if not settings.get("enable_registration", True):
        return RedirectResponse("/?error=registration_disabled", status_code=303)

    ip = request.client.host
    if not AuthService.rate_limit(f"register:{ip}", max_req=5, window=300):
        return templates.TemplateResponse("register.html", {
            "request": request, "settings": settings,
            "discord_enabled": _discord_active(settings),
            "error": "Too many attempts. Wait 5 minutes.",
        })

    username = username.strip().lower()
    if len(username) < 3 or not username.replace("-", "").replace("_", "").isalnum():
        return templates.TemplateResponse("register.html", {
            "request": request, "settings": settings,
            "discord_enabled": _discord_active(settings),
            "error": "Username must be 3+ alphanumeric chars.",
        })
    if len(password) < 8:
        return templates.TemplateResponse("register.html", {
            "request": request, "settings": settings,
            "discord_enabled": _discord_active(settings),
            "error": "Password must be at least 8 characters.",
        })
    if db.query(User).filter(User.username == username).first():
        return templates.TemplateResponse("register.html", {
            "request": request, "settings": settings,
            "discord_enabled": _discord_active(settings),
            "error": "Username already taken.",
        })

    user = User(
        username=username,
        email=email.strip() or None,
        password=AuthService.hash_password(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    resp = RedirectResponse("/dashboard", status_code=303)
    AuthService.create_session(resp, user.id)
    return resp


@router.post("/login")
def login(
    username: str = Form(...),
    password: str = Form(...),
    request:  Request = None,
    db:       Session = Depends(get_db),
):
    settings = get_settings(db)
    ip = request.client.host
    if not AuthService.rate_limit(f"login:{ip}", max_req=10, window=60):
        return templates.TemplateResponse("login.html", {
            "request": request, "settings": settings,
            "discord_enabled": _discord_active(settings),
            "error": "Too many login attempts. Slow down.",
        })

    user = db.query(User).filter(User.username == username.strip().lower()).first()
    if not user or not user.password or not AuthService.verify_password(password, user.password):
        return templates.TemplateResponse("login.html", {
            "request": request, "settings": settings,
            "discord_enabled": _discord_active(settings),
            "error": "Invalid credentials.",
        })

    if user.is_suspended:
        return templates.TemplateResponse("login.html", {
            "request": request, "settings": settings,
            "discord_enabled": _discord_active(settings),
            "error": f"Account suspended: {user.suspend_reason or 'Contact support.'}",
        })

    from datetime import datetime
    user.last_login = datetime.utcnow()
    db.commit()

    resp = RedirectResponse("/dashboard", status_code=303)
    AuthService.create_session(resp, user.id)
    return resp


@router.get("/logout")
def logout(request: Request):
    resp = RedirectResponse("/", status_code=303)
    AuthService.destroy_session(request, resp)
    return resp


# ── Discord OAuth (login / register) ──────────────────────────────────────────

@router.get("/auth/discord")
async def discord_login(request: Request, db: Session = Depends(get_db)):
    settings = get_settings(db)
    if not _discord_active(settings):
        return RedirectResponse("/?error=discord_not_configured", status_code=303)
    redirect_uri = os.getenv("BASE_URL", "http://localhost:8000") + "/auth/discord/callback"
    return await oauth.discord.authorize_redirect(request, redirect_uri)


@router.get("/auth/discord/callback")
async def discord_callback(request: Request, db: Session = Depends(get_db)):
    settings = get_settings(db)
    try:
        token = await oauth.discord.authorize_access_token(request)
        r = await oauth.discord.get("users/@me", token=token)
        profile = r.json()
    except Exception as exc:
        logger.warning("Discord OAuth error: %s", exc)
        return RedirectResponse("/?error=discord_failed", status_code=303)

    discord_id       = str(profile["id"])
    discord_username = profile.get("username", "user")
    avatar_hash      = profile.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png"
        if avatar_hash else None
    )

    user = db.query(User).filter(User.discord_id == discord_id).first()
    if not user:
        # Auto-create account from Discord
        if not settings.get("enable_registration", True):
            return RedirectResponse("/?error=registration_disabled", status_code=303)
        base = discord_username.lower().replace(" ", "_")[:20]
        username = base
        counter = 1
        while db.query(User).filter(User.username == username).first():
            username = f"{base}{counter}"
            counter += 1
        user = User(
            username=username,
            discord_id=discord_id,
            discord_username=discord_username,
            discord_avatar=avatar_hash,
            discord_verified=True,
            avatar_url=avatar_url,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Update existing discord user's profile
        user.discord_username = discord_username
        user.discord_avatar   = avatar_hash
        user.discord_verified = True
        if avatar_url:
            user.avatar_url = avatar_url
        db.commit()

    if user.is_suspended:
        return RedirectResponse("/?error=suspended", status_code=303)

    from datetime import datetime
    user.last_login = datetime.utcnow()
    db.commit()

    resp = RedirectResponse("/dashboard", status_code=303)
    AuthService.create_session(resp, user.id)
    return resp
