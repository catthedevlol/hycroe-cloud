"""
Ticket system router — Hycroe Cloud Panel.

Routes:
  GET  /tickets                        → Ticket list (paginated)
  POST /tickets/create                 → Open new ticket + first message
  GET  /tickets/{id}                   → Chat view (messages oldest→newest)
  POST /tickets/{id}/reply             → Post a reply message
  POST /tickets/{id}/close             → Close ticket (owner or admin)
  GET  /api/tickets/{id}/messages      → JSON poll endpoint for new messages
  POST /admin/tickets/{id}/status      → Admin: update status
  POST /admin/tickets/{id}/delete      → Admin: delete ticket
"""
import logging
from fastapi import APIRouter, Form, Request, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import get_db
from models import Ticket, TicketMessage
from services.auth import AuthService
from services.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

VALID_STATUSES = {"open", "in_progress", "closed"}
PAGE_SIZE = 20  # tickets per page


def _auth(request: Request, db: Session):
    return AuthService.get_user(request, db)


# ── Ticket list (paginated) ───────────────────────────────────────────────────

@router.get("/tickets", response_class=HTMLResponse)
def tickets_page(
    request: Request,
    page: int = Query(default=1, ge=1),
    db:   Session = Depends(get_db),
):
    user = _auth(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)

    base_q = db.query(Ticket)
    if not user.is_admin:
        base_q = base_q.filter(Ticket.user_id == user.id)

    total       = base_q.count()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page        = min(page, total_pages)
    offset      = (page - 1) * PAGE_SIZE

    tickets = (
        base_q
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .offset(offset)
        .limit(PAGE_SIZE)
        .all()
    )

    return templates.TemplateResponse("tickets.html", {
        "request":     request,
        "user":        user,
        "tickets":     tickets,
        "settings":    get_settings(db),
        "page":        page,
        "total_pages": total_pages,
        "total":       total,
        "page_size":   PAGE_SIZE,
    })


# ── Create ticket ─────────────────────────────────────────────────────────────

@router.post("/tickets/create")
async def create_ticket(
    title:   str = Form(...),
    message: str = Form(...),
    request: Request = None,
    db:      Session = Depends(get_db),
):
    user = _auth(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)
    if user.is_suspended:
        return RedirectResponse("/tickets?error=suspended", status_code=303)

    title   = title.strip()
    message = message.strip()
    if not title or len(title) > 200:
        return RedirectResponse("/tickets?error=invalid_title", status_code=303)
    if not message or len(message) > 5000:
        return RedirectResponse("/tickets?error=invalid_message", status_code=303)

    # Keep ticket.message for backward compat; TicketMessage is the live chat record
    ticket = Ticket(title=title, message=message, user_id=user.id, status="open")
    db.add(ticket)
    db.flush()  # get ticket.id before commit

    db.add(TicketMessage(ticket_id=ticket.id, user_id=user.id, message=message))
    db.commit()
    db.refresh(ticket)

    logger.info("Ticket #%d created by %s: %s", ticket.id, user.username, title)

    # Webhook — failure must NOT crash the system
    try:
        settings       = get_settings(db)
        webhook_url    = settings.get("discord_webhook_url") or ""
        notify_user_id = settings.get("notify_user_id") or ""
        if webhook_url:
            from services.webhook import send_ticket_webhook
            await send_ticket_webhook(
                webhook_url=webhook_url,
                notify_user_id=notify_user_id,
                username=user.username,
                ticket_title=title,
                ticket_message=message,
                ticket_id=ticket.id,
            )
    except Exception as exc:
        logger.error("Ticket webhook error (non-fatal): %s", exc)

    return RedirectResponse(f"/tickets/{ticket.id}?success=ticket_created", status_code=303)


# ── Chat view ─────────────────────────────────────────────────────────────────

@router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
def view_ticket(ticket_id: int, request: Request, db: Session = Depends(get_db)):
    user = _auth(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket.user_id != user.id and not user.is_admin:
        raise HTTPException(403, "Access denied")

    # Messages oldest → newest for natural chat flow
    messages = (
        db.query(TicketMessage)
        .filter(TicketMessage.ticket_id == ticket_id)
        .order_by(TicketMessage.created_at.asc(), TicketMessage.id.asc())
        .all()
    )

    return templates.TemplateResponse("ticket_detail.html", {
        "request":  request,
        "user":     user,
        "ticket":   ticket,
        "messages": messages,
        "settings": get_settings(db),
    })


# ── Reply ─────────────────────────────────────────────────────────────────────

@router.post("/tickets/{ticket_id}/reply")
def reply_ticket(
    ticket_id: int,
    message:   str = Form(...),
    request:   Request = None,
    db:        Session = Depends(get_db),
):
    user = _auth(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket.user_id != user.id and not user.is_admin:
        raise HTTPException(403, "Access denied")
    if ticket.status == "closed":
        return RedirectResponse(f"/tickets/{ticket_id}?error=ticket_closed", status_code=303)

    message = message.strip()
    if not message or len(message) > 5000:
        return RedirectResponse(f"/tickets/{ticket_id}?error=invalid_message", status_code=303)

    db.add(TicketMessage(ticket_id=ticket_id, user_id=user.id, message=message))

    # Admin first reply auto-moves ticket to in_progress
    if user.is_admin and ticket.status == "open":
        ticket.status = "in_progress"

    db.commit()
    logger.info("Ticket #%d reply by %s", ticket_id, user.username)
    return RedirectResponse(f"/tickets/{ticket_id}#bottom", status_code=303)


# ── Close ─────────────────────────────────────────────────────────────────────

@router.post("/tickets/{ticket_id}/close")
def close_ticket(ticket_id: int, request: Request, db: Session = Depends(get_db)):
    user = _auth(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket.user_id != user.id and not user.is_admin:
        raise HTTPException(403, "Access denied")

    ticket.status = "closed"
    db.commit()
    logger.info("Ticket #%d closed by %s", ticket.id, user.username)
    return RedirectResponse(f"/tickets/{ticket_id}?success=ticket_closed", status_code=303)


# ── Admin: update status ──────────────────────────────────────────────────────

@router.post("/admin/tickets/{ticket_id}/status")
def admin_update_ticket_status(
    ticket_id: int,
    status:    str = Form(...),
    request:   Request = None,
    db:        Session = Depends(get_db),
):
    user = AuthService.get_user(request, db)
    if not user or not user.is_admin:
        raise HTTPException(403, "Admin access required")

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    status = status.strip().lower()
    if status not in VALID_STATUSES:
        return RedirectResponse(f"/tickets/{ticket_id}?error=invalid_status", status_code=303)

    ticket.status = status
    db.commit()
    logger.info("Admin %s set ticket #%d → %s", user.username, ticket_id, status)
    return RedirectResponse(f"/tickets/{ticket_id}?success=status_updated", status_code=303)


# ── Admin: delete ticket ──────────────────────────────────────────────────────

@router.post("/admin/tickets/{ticket_id}/delete")
def admin_delete_ticket(
    ticket_id: int,
    request:   Request = None,
    db:        Session = Depends(get_db),
):
    user = AuthService.get_user(request, db)
    if not user or not user.is_admin:
        raise HTTPException(403, "Admin access required")

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    # Delete messages first (CASCADE not guaranteed on all SQLite configs)
    db.query(TicketMessage).filter(TicketMessage.ticket_id == ticket_id).delete()
    db.delete(ticket)
    db.commit()
    logger.info("Admin %s deleted ticket #%d", user.username, ticket_id)
    return RedirectResponse("/tickets?success=ticket_deleted", status_code=303)


# ── JSON polling endpoint ─────────────────────────────────────────────────────

@router.get("/api/tickets/{ticket_id}/messages")
def api_ticket_messages(
    ticket_id: int,
    after:     int = Query(default=0, ge=0),  # last known message id; return only newer
    request:   Request = None,
    db:        Session = Depends(get_db),
):
    """
    Returns messages as JSON for live polling.
    Client sends ?after=<last_msg_id> and receives only new messages.
    Returns ticket status so UI can reflect close/reopen without reload.
    """
    user = AuthService.get_user(request, db)
    if not user:
        raise HTTPException(401, "Not authenticated")

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket.user_id != user.id and not user.is_admin:
        raise HTTPException(403, "Access denied")

    q = db.query(TicketMessage).filter(TicketMessage.ticket_id == ticket_id)
    if after > 0:
        q = q.filter(TicketMessage.id > after)
    msgs = q.order_by(TicketMessage.created_at.asc(), TicketMessage.id.asc()).all()

    return JSONResponse({
        "ticket_status": ticket.status,
        "messages": [
            {
                "id":         m.id,
                "user_id":    m.user_id,
                "is_mine":    m.user_id == user.id,
                "is_admin":   bool(m.author and m.author.is_admin),
                "username":   m.author.username if m.author else "?",
                "message":    m.message,
                "created_at": m.created_at.strftime("%Y-%m-%d %H:%M"),
            }
            for m in msgs
        ],
    })
