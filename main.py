"""
Hycroe Node Panel — main.py
Mounts all API routers and starts background workers.
"""
import os
import secrets
import logging
import traceback

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware

from database import engine
from models import Base
from api import (
    auth_router, vps_router, nodes_router,
    admin_router, billing_router, console_router,
    account_router, tickets_router,
)
# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Quieten noisy libraries
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

# ── App ───────────────────────────────────────────────────────────────────────

_ENV = os.getenv("ENV", "production")

app = FastAPI(
    title="Hycroe Node Panel",
    version="4.0.0",
    docs_url="/docs" if _ENV == "development" else None,
    redoc_url=None,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", secrets.token_hex(32)),
    max_age=86400 * 7,
    https_only=os.getenv("SECURE_COOKIES", "false").lower() == "true",
)

# ── DB Init ───────────────────────────────────────────────────────────────────

try:
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created/verified")
except Exception as exc:
    logger.critical("Database init failed: %s", exc)
    raise

# ── Migrations ────────────────────────────────────────────────────────────────
try:
    from migrate import run_migrations
    run_migrations()
    from migrate import _seed_local_node
    _seed_local_node()
except Exception as mex:
    logger.warning("Migration warning (non-fatal): %s", mex)

# ── Templates ────────────────────────────────────────────────────────────────

from fastapi.templating import Jinja2Templates as _Jinja2Templates
from fastapi import Depends as _Depends
from sqlalchemy.orm import Session as _Session
from database import get_db as _get_db
from services.auth import AuthService as _AuthService
from fastapi.responses import RedirectResponse as _RedirectResponse

_templates = _Jinja2Templates(directory="templates")

# ── Public routes — these must bypass any auth checks ────────────────────────

# Paths that are always publicly accessible (no login required).
_PUBLIC_PATHS = {"/", "/login", "/register"}


@app.get("/", response_class=HTMLResponse)
def landing_page(request: Request):
    """Serve the public landing page — no authentication required."""
    return _templates.TemplateResponse("landing.html", {"request": request})


@app.get("/api/plans")
def public_plans(db: _Session = _Depends(_get_db)):
    """Return active VPS plans for the public landing page — no auth required."""
    from models import VPSPlan as _VPSPlan
    plans = (
        db.query(_VPSPlan)
        .filter(_VPSPlan.is_active == True)
        .order_by(_VPSPlan.credits_cost)
        .all()
    )
    return [
        {
            "id":           p.id,
            "name":         p.name,
            "ram_mb":       p.ram_mb,
            "ram_display":  p.display_ram,
            "cpu":          p.cpu,
            "disk_gb":      p.disk_gb,
            "credits_cost": p.credits_cost,
            "instance_type": p.instance_type or "container",
            "location":     p.location or None,
        }
        for p in plans
    ]


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(vps_router)
app.include_router(nodes_router)
app.include_router(admin_router)
app.include_router(billing_router)
app.include_router(console_router)
app.include_router(account_router)
app.include_router(tickets_router)

# ── About page ────────────────────────────────────────────────────────────────

@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request, db: _Session = _Depends(_get_db)):
    user = _AuthService.get_user(request, db)
    if not user:
        return _RedirectResponse("/", status_code=303)
    return _templates.TemplateResponse("about.html", {"request": request, "user": user})

# ── Static files ──────────────────────────────────────────────────────────────

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Panel settings — inject into every Jinja2 template via globals ────────────
# Rather than a fragile monkey-patch, we register a callable in jinja2's globals
# so every template can call get_panel_settings() if it needs to.
# Templates that already receive `settings` from their route use that value;
# templates that don't will see `settings` as undefined (base.html guards all
# accesses with `if settings`, so it degrades gracefully).
#
# For routes that don't pass settings explicitly (nodes, billing, admin, vps, etc.)
# we install a Jinja2 global function that fetches settings on demand.

from database import SessionLocal as _SL
from services.settings import get_settings as _get_panel_settings, DEFAULTS as _SETTINGS_DEFAULTS

def _settings_global():
    """Called by Jinja2 templates to get panel settings when not passed explicitly."""
    _db = _SL()
    try:
        return _get_panel_settings(_db)
    except Exception:
        return dict(_SETTINGS_DEFAULTS)
    finally:
        _db.close()

# Inject into every Jinja2Templates instance by setting env globals after routers load.
# We patch all router template instances:
def _inject_settings_globals():
    try:
        from api import auth_router, vps_router, nodes_router, admin_router, billing_router, console_router, account_router
        import api.auth as _auth_mod
        import api.vps as _vps_mod
        import api.nodes as _nodes_mod
        import api.admin as _admin_mod
        import api.billing as _billing_mod
        import api.console as _console_mod
        import api.account as _account_mod
        import api.tickets as _tickets_mod
        for mod in [_auth_mod, _vps_mod, _nodes_mod, _admin_mod, _billing_mod, _console_mod, _account_mod, _tickets_mod]:
            if hasattr(mod, 'templates') and hasattr(mod.templates, 'env'):
                # Add a callable global so {{ settings }} works in templates
                # that don't have it in their context
                mod.templates.env.globals.setdefault('settings', _settings_global())
        # Also patch _templates in main.py
        if hasattr(_templates, 'env'):
            _templates.env.globals.setdefault('settings', _settings_global())
    except Exception as _e:
        logger.warning("Could not inject settings globals: %s", _e)

_inject_settings_globals()


# ── Error handlers ────────────────────────────────────────────────────────────

_ERROR_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Hycroe</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'DM Sans',sans-serif;background:#0f1117;color:#e8eaf0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}}
  .box{{max-width:440px;text-align:center}}
  .code{{font-size:72px;font-weight:800;color:#1c1f2e;line-height:1;margin-bottom:8px}}
  .title{{font-size:22px;font-weight:700;margin-bottom:10px}}
  .msg{{font-size:14px;color:#8b91a8;margin-bottom:28px;line-height:1.6}}
  a{{display:inline-flex;align-items:center;gap:6px;background:#0066FF;color:#fff;text-decoration:none;padding:10px 20px;border-radius:8px;font-size:13px;font-weight:600}}
</style>
</head>
<body>
<div class="box">
  <div class="code">{code}</div>
  <div class="title">{title}</div>
  <div class="msg">{message}</div>
  <a href="/dashboard">← Back to dashboard</a>
</div>
</body>
</html>"""


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith("/api/") or request.url.path.startswith("/ws/"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    # Public paths should never land here — if they do, serve them directly
    # rather than redirecting (avoids masking routing bugs).
    if request.url.path in _PUBLIC_PATHS or request.url.path.startswith("/static"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    # For other authenticated page requests, redirect to dashboard
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/dashboard", status_code=303)


@app.exception_handler(403)
async def forbidden(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return HTMLResponse(
        _ERROR_PAGE.format(
            code=403, title="Access Denied",
            message="You don't have permission to access this page."
        ), status_code=403)


@app.exception_handler(500)
async def server_error(request: Request, exc):
    logger.error(
        "500 error on %s %s\n%s",
        request.method, request.url.path,
        traceback.format_exc()
    )
    if request.url.path.startswith("/api/") or request.url.path.startswith("/ws/"):
        return JSONResponse({"detail": "Internal server error"}, status_code=500)
    return HTMLResponse(
        _ERROR_PAGE.format(
            code=500, title="Server Error",
            message="Something went wrong on our end. If this persists, please contact support."
        ), status_code=500)


# ── Public-route passthrough middleware ──────────────────────────────────────
# This MUST be defined after all routes so it runs with lower priority.
# FastAPI middleware stack runs in LIFO order (last added = first executed),
# so this wraps around routing but must not block public paths.
@app.middleware("http")
async def public_route_passthrough(request: Request, call_next):
    """Pass public paths through immediately, bypassing any session logic."""
    path = request.url.path
    # Always allow public paths and static files without any interference
    if path in _PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)
    # For all other paths, just pass through normally
    return await call_next(request)


# Catch-all for unhandled exceptions in middleware
@app.middleware("http")
async def catch_exceptions(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        logger.exception("Unhandled exception in middleware for %s: %s", request.url.path, exc)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Internal server error"}, status_code=500)
        return HTMLResponse(
            _ERROR_PAGE.format(
                code=500, title="Server Error",
                message="An unexpected error occurred. Please try again."
            ), status_code=500)


# ── Background Worker ─────────────────────────────────────────────────────────

# Single task reference — prevents duplicate periodic tasks
_node_webhook_task: "asyncio.Task | None" = None
_expiry_check_task: "asyncio.Task | None" = None
_abuse_check_task:  "asyncio.Task | None" = None


@app.on_event("startup")
async def startup():
    global _node_webhook_task, _expiry_check_task, _abuse_check_task
    import asyncio

    try:
        from workers import start_worker
        from workers.handlers import JOB_HANDLERS
        start_worker(JOB_HANDLERS)
        logger.info("Background worker started")
        import workers
        workers.enqueue("refresh_nodes", {})
    except Exception as exc:
        logger.critical(
            "Worker startup FAILED: %s. VPS creation and background jobs will NOT work. "
            "Investigate immediately.", exc
        )

    if _node_webhook_task is None or _node_webhook_task.done():
        try:
            _node_webhook_task = asyncio.get_event_loop().create_task(
                _periodic_node_status_webhook(),
                name="node_status_webhook_periodic",
            )
            logger.info("Periodic node status webhook task started")
        except Exception as exc:
            logger.error("Could not start node webhook task (non-fatal): %s", exc)
    else:
        logger.debug("Periodic node status webhook task already running — skipping duplicate")

    if _expiry_check_task is None or _expiry_check_task.done():
        try:
            _expiry_check_task = asyncio.get_event_loop().create_task(
                _periodic_expiry_check(),
                name="vps_expiry_check_periodic",
            )
            logger.info("Periodic VPS expiry check task started")
        except Exception as exc:
            logger.error("Could not start expiry check task (non-fatal): %s", exc)

    if _abuse_check_task is None or _abuse_check_task.done():
        try:
            _abuse_check_task = asyncio.get_event_loop().create_task(
                _periodic_abuse_check(),
                name="vps_abuse_check_periodic",
            )
            logger.info("Periodic VPS abuse check task started")
        except Exception as exc:
            logger.error("Could not start abuse check task (non-fatal): %s", exc)


async def _periodic_expiry_check():
    """Check for expired VPS every hour. Cancels cleanly on shutdown."""
    import asyncio
    import workers as _workers

    _EXPIRY_CHECK_INTERVAL = 3600  # 1 hour
    await asyncio.sleep(60)  # brief startup delay
    while True:
        try:
            _workers.enqueue("check_expiry", {})
            logger.debug("check_expiry job enqueued")
        except Exception as exc:
            logger.error("Could not enqueue check_expiry (non-fatal): %s", exc)
        try:
            await asyncio.sleep(_EXPIRY_CHECK_INTERVAL)
        except asyncio.CancelledError:
            logger.info("Expiry check task cancelled — exiting cleanly")
            return


async def _periodic_abuse_check():
    """Check all running VPS for CPU abuse every 5 minutes."""
    import asyncio
    import workers as _workers

    _ABUSE_CHECK_INTERVAL = 300  # 5 minutes — matches metrics window
    await asyncio.sleep(90)  # initial delay to let containers settle
    while True:
        try:
            _workers.enqueue("abuse_check", {})
            logger.debug("abuse_check job enqueued")
        except Exception as exc:
            logger.error("Could not enqueue abuse_check (non-fatal): %s", exc)
        try:
            await asyncio.sleep(_ABUSE_CHECK_INTERVAL)
        except asyncio.CancelledError:
            logger.info("Abuse check task cancelled — exiting cleanly")
            return


@app.on_event("shutdown")
async def shutdown():
    global _node_webhook_task, _expiry_check_task, _abuse_check_task
    import asyncio

    for task, name in [
        (_node_webhook_task, "node webhook"),
        (_expiry_check_task, "expiry check"),
        (_abuse_check_task,  "abuse check"),
    ]:
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:
                pass
            logger.info("%s periodic task stopped", name)

    try:
        from workers import stop_worker
        stop_worker()
        logger.info("Worker stopped")
    except Exception as exc:
        logger.warning("Worker shutdown error: %s", exc)


async def _periodic_node_status_webhook():
    """
    Enqueue a node_status_webhook job every 5 minutes.
    Runs as a single asyncio Task; duplicate prevention is handled in startup().
    Uses asyncio.CancelledError to exit cleanly on shutdown.
    """
    import asyncio
    import workers as _workers

    _NODE_WEBHOOK_INTERVAL = 300  # seconds (5 min)

    # Brief initial delay so all services are fully initialised before first run.
    await asyncio.sleep(30)

    while True:
        try:
            _workers.enqueue("node_status_webhook", {})
            logger.debug("node_status_webhook job enqueued")
        except Exception as exc:
            logger.error("Could not enqueue node_status_webhook (non-fatal): %s", exc)

        try:
            await asyncio.sleep(_NODE_WEBHOOK_INTERVAL)
        except asyncio.CancelledError:
            logger.info("Periodic node status webhook task cancelled — exiting cleanly")
            return


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=_ENV == "development",
        log_level="info",
        access_log=_ENV == "development",
    )
