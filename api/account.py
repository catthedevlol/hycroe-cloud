"""
Account settings routes — password change, Discord connect/disconnect.
"""
import os
import logging

from fastapi import APIRouter, Form, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import get_db
from models import User
from services.auth import AuthService
from services.settings import get_settings

router = APIRouter()
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)


def _get_user_or_redirect(request: Request, db: Session):
    user = AuthService.get_user(request, db)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


# ── Account settings page ──────────────────────────────────────────────────

@router.get("/account/settings", response_class=HTMLResponse)
def account_settings(request: Request, db: Session = Depends(get_db)):
    user = AuthService.get_user(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)
    settings = get_settings(db)
    discord_client_id = os.getenv("DISCORD_CLIENT_ID", "")
    return templates.TemplateResponse("account_settings.html", {
        "request": request,
        "user": user,
        "settings": settings,
        "discord_enabled": bool(discord_client_id) and settings.get("enable_discord_login", True),
    })


# ── Password change ────────────────────────────────────────────────────────

@router.post("/account/password/change")
def password_change(
    current_password: str = Form(...),
    new_password:     str = Form(...),
    confirm_password: str = Form(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    user = AuthService.get_user(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)

    # Discord-only accounts have no password
    if not user.password:
        return RedirectResponse(
            "/account/settings?error=no_password",
            status_code=303,
        )

    if not AuthService.verify_password(current_password, user.password):
        return RedirectResponse(
            "/account/settings?error=wrong_password",
            status_code=303,
        )

    if new_password != confirm_password:
        return RedirectResponse(
            "/account/settings?error=password_mismatch",
            status_code=303,
        )

    if len(new_password) < 8:
        return RedirectResponse(
            "/account/settings?error=password_too_short",
            status_code=303,
        )

    user.password = AuthService.hash_password(new_password)
    db.commit()
    logger.info("User %s changed password", user.username)
    return RedirectResponse("/account/settings?success=password_changed", status_code=303)


# ── Discord connect / disconnect ──────────────────────────────────────────

@router.get("/account/discord/connect")
async def discord_connect(request: Request, db: Session = Depends(get_db)):
    """Initiate Discord OAuth to link account."""
    user = AuthService.get_user(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)

    discord_client_id = os.getenv("DISCORD_CLIENT_ID", "")
    if not discord_client_id:
        return RedirectResponse("/account/settings?error=discord_not_configured", status_code=303)

    # Store intent in session
    request.session["discord_link_user_id"] = user.id

    redirect_uri = os.getenv("BASE_URL", "http://localhost:8000") + "/account/discord/callback"
    from api.auth import oauth
    return await oauth.discord.authorize_redirect(request, redirect_uri)


@router.get("/account/discord/callback")
async def discord_link_callback(request: Request, db: Session = Depends(get_db)):
    """Handle Discord OAuth callback for account linking."""
    user_id = request.session.pop("discord_link_user_id", None)
    user = AuthService.get_user(request, db)

    # Allow both: user is logged in (preferred) or session had user_id
    if not user and user_id:
        user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/", status_code=303)

    from api.auth import oauth
    try:
        token = await oauth.discord.authorize_access_token(request)
        r = await oauth.discord.get("users/@me", token=token)
        profile = r.json()
    except Exception as exc:
        logger.warning("Discord link callback error: %s", exc)
        return RedirectResponse("/account/settings?error=discord_failed", status_code=303)

    discord_id = str(profile["id"])
    discord_username = profile.get("username", "")
    avatar_hash = profile.get("avatar")

    # Check if this discord_id is already linked to another account
    existing = db.query(User).filter(
        User.discord_id == discord_id,
        User.id != user.id,
    ).first()
    if existing:
        return RedirectResponse("/account/settings?error=discord_already_linked", status_code=303)

    user.discord_id       = discord_id
    user.discord_username = discord_username
    user.discord_avatar   = avatar_hash
    user.discord_verified = True
    if avatar_hash:
        user.avatar_url = f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png"
    db.commit()
    logger.info("User %s linked Discord account %s", user.username, discord_username)
    return RedirectResponse("/account/settings?success=discord_linked", status_code=303)


@router.post("/account/discord/disconnect")
def discord_disconnect(request: Request, db: Session = Depends(get_db)):
    user = AuthService.get_user(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)

    if not user.discord_id:
        return RedirectResponse("/account/settings?error=not_linked", status_code=303)

    # Prevent disconnect if it's their only login method
    if not user.password:
        return RedirectResponse(
            "/account/settings?error=discord_only_auth",
            status_code=303,
        )

    user.discord_id       = None
    user.discord_username = None
    user.discord_avatar   = None
    user.discord_verified = False
    # Keep avatar_url in case they uploaded a custom one
    db.commit()
    logger.info("User %s disconnected Discord", user.username)
    return RedirectResponse("/account/settings?success=discord_disconnected", status_code=303)
