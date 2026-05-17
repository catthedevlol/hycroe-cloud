"""
Nodes API — manage Incus cluster nodes (Incus only).
"""
import logging

from fastapi import APIRouter, Form, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional

from database import get_db
from models import Node, VPS
from services.auth import AuthService
from services.incus import IncusService
from services.node_selector import NodeSelector

router = APIRouter()
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)


def _require_admin(request: Request, db: Session):
    user = AuthService.get_user(request, db)
    if not user or not user.is_admin:
        raise HTTPException(403, "Admin only")
    return user


# ── Pages ──────────────────────────────────────────────────────────────────

@router.get("/nodes", response_class=HTMLResponse)
def nodes_page(request: Request, db: Session = Depends(get_db)):
    user = AuthService.get_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse("/dashboard", status_code=303)

    nodes = db.query(Node).order_by(Node.id).all()

    # Refresh incus nodes individually so one bad node can't crash the page
    for node in nodes:
        try:
            NodeSelector.refresh_node(db, node)
        except Exception as exc:
            logger.warning("Node refresh failed for %s: %s", node.name, exc)

    db.expire_all()
    nodes = db.query(Node).order_by(Node.id).all()

    node_data = []
    for node in nodes:
        ram_pct = 0
        if node.ram_total_mb > 0:
            ram_pct = round(node.ram_used_mb / node.ram_total_mb * 100, 1)
        disk_pct = 0
        disk_used  = node.disk_used_gb  or 0
        disk_total = node.disk_total_gb or 0
        if disk_total > 0:
            disk_pct = round(disk_used / disk_total * 100, 1)

        running_count = db.query(VPS).filter(
            VPS.node_id == node.id, VPS.status == "running").count()
        total_count   = db.query(VPS).filter(VPS.node_id == node.id).count()

        node_data.append({
            "id":            node.id,
            "name":          node.name,
            "display_name":  node.display_name or node.name,
            "address":       node.address,
            "port":          node.port or 8443,
            "status":        node.status,
            "node_type":     node.node_type or "incus",
            "ram_used":      node.ram_used_mb,
            "ram_total":     node.ram_total_mb,
            "ram_pct":       ram_pct,
            "cpu_cores":     node.cpu_cores or 0,
            "cpu_load":      node.cpu_load  or 0.0,
            "disk_used_gb":  disk_used,
            "disk_total_gb": disk_total,
            "disk_pct":      disk_pct,
            "vps_count":     total_count,
            "running_count": running_count,
            "max_vps":       node.max_vps,
            "is_default":    node.is_default,
            "maintenance":   node.maintenance,
            "location":      node.location or "—",
            "last_seen":     node.last_seen,
        })

    return templates.TemplateResponse("nodes.html", {
        "request": request, "user": user, "nodes": node_data
    })


# ── Add node ───────────────────────────────────────────────────────────────

@router.post("/nodes/add")
def add_node(
    name:          str  = Form(...),
    display_name:  str  = Form(""),
    address:       str  = Form(...),
    port:          int  = Form(8443),
    incus_remote:  str  = Form(""),
    location:      str  = Form(""),
    max_vps:       int  = Form(50),
    is_default:    bool = Form(False),
    ram_total_mb:  int  = Form(0),
    cpu_cores:     int  = Form(0),
    disk_total_gb: int  = Form(0),
    request:       Request = None,
    db:            Session = Depends(get_db),
):
    _require_admin(request, db)

    name = name.strip().lower()
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        return RedirectResponse("/nodes?error=invalid_name", status_code=303)

    if db.query(Node).filter(Node.name == name).first():
        return RedirectResponse("/nodes?error=name_taken", status_code=303)

    if is_default:
        db.query(Node).update({Node.is_default: False})
        db.flush()

    remote_name = incus_remote.strip() or name
    add_result  = IncusService.add_remote(remote_name, address.strip())
    if not add_result["success"]:
        logger.warning("Incus remote add failed for %s: %s", name, add_result["error"])

    node = Node(
        name=name,
        display_name=display_name.strip() or name,
        address=address.strip(),
        port=port,
        node_type="incus",
        incus_remote=remote_name,
        location=location.strip(),
        max_vps=max(1, max_vps),
        is_default=is_default,
        status="offline",  # safe default; refresh_node() will set "online" if reachable
        # Admin-supplied capacity values; overwritten by refresh if Incus responds
        ram_total_mb=max(0, ram_total_mb),
        cpu_cores=max(0, cpu_cores),
        disk_total_gb=max(0, disk_total_gb),
    )
    db.add(node)
    db.commit()
    db.refresh(node)

    # Probe immediately, but don't fail the redirect if it errors
    try:
        NodeSelector.refresh_node(db, node)
    except Exception as exc:
        logger.warning("Initial node probe failed for %s: %s", name, exc)

    _fire_node_event(db, node.display_name or node.name, "added", f"Address: {node.address}")
    return RedirectResponse("/nodes?success=node_added", status_code=303)


def _fire_node_event(db, node_name: str, event: str, detail: str = "") -> None:
    """
    Fire a node/VPS lifecycle event webhook in a background thread.

    Uses ThreadPoolExecutor + asyncio.run() — the only safe pattern from
    sync request handlers. asyncio.new_event_loop() is NEVER used.
    Failure is non-fatal and fully logged.
    """
    try:
        from services.settings import get_settings
        from services.webhook import send_node_event_webhook, is_valid_webhook_url
        from concurrent.futures import ThreadPoolExecutor
        import asyncio as _asyncio

        wh = get_settings(db).get("node_webhook_url") or ""
        if not wh:
            logger.debug("node_webhook_url not configured — skipping event '%s'", event)
            return
        if not is_valid_webhook_url(wh):
            logger.warning("node_webhook_url is invalid — skipping event '%s'", event)
            return

        logger.info(
            "Firing node event webhook: event='%s' node='%s' detail='%s'",
            event, node_name, detail or "—",
        )

        def _run():
            """asyncio.run() is safe inside a ThreadPoolExecutor thread."""
            try:
                result = _asyncio.run(send_node_event_webhook(wh, node_name, event, detail))
                if result:
                    logger.info("Node event webhook SUCCESS: event='%s' node='%s'", event, node_name)
                else:
                    logger.warning("Node event webhook FAILED: event='%s' node='%s'", event, node_name)
            except Exception as inner_exc:
                logger.error("Node event webhook thread error: %s", inner_exc)

        with ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(_run)

    except Exception as exc:
        logger.error("Node event webhook fire failed (non-fatal): %s", exc)


# ── Delete / Maintenance ───────────────────────────────────────────────────

@router.post("/nodes/delete")
def delete_node(
    node_id: int = Form(...),
    request: Request = None,
    db:      Session = Depends(get_db),
):
    _require_admin(request, db)
    node = db.query(Node).filter(Node.id == node_id).first()
    if node:
        node_name = node.display_name or node.name
        try:
            IncusService.remove_remote(node.incus_remote or node.name)
        except Exception:
            pass
        db.delete(node)
        db.commit()
        _fire_node_event(db, node_name, "removed")
    return RedirectResponse("/nodes", status_code=303)


@router.post("/nodes/toggle-maintenance")
def toggle_maintenance(
    node_id: int = Form(...),
    request: Request = None,
    db:      Session = Depends(get_db),
):
    _require_admin(request, db)
    node = db.query(Node).filter(Node.id == node_id).first()
    if node:
        node.maintenance = not node.maintenance
        db.commit()
        event = "maintenance" if node.maintenance else "online"
        _fire_node_event(db, node.display_name or node.name, event)
    return RedirectResponse("/nodes", status_code=303)


# ── Node edit ──────────────────────────────────────────────────────────────

@router.post("/nodes/{node_id}/edit")
def edit_node(
    node_id:       int,
    display_name:  str  = Form(""),
    location:      str  = Form(""),
    max_vps:       int  = Form(50),
    is_default:    bool = Form(False),
    maintenance:   bool = Form(False),
    ram_total_mb:  int  = Form(0),
    cpu_cores:     int  = Form(0),
    disk_total_gb: int  = Form(0),
    request:       Request = None,
    db:            Session = Depends(get_db),
):
    _require_admin(request, db)
    node = db.query(Node).filter(Node.id == node_id).first()
    if not node:
        raise HTTPException(404, "Node not found")

    if is_default and not node.is_default:
        db.query(Node).filter(Node.id != node_id).update({Node.is_default: False})

    node.display_name = display_name.strip() or node.display_name
    node.location     = location.strip() or node.location
    node.max_vps      = max(1, max_vps)
    node.is_default   = is_default
    node.maintenance  = maintenance
    # Only override capacity if a non-zero value was submitted
    if ram_total_mb  > 0: node.ram_total_mb  = ram_total_mb
    if cpu_cores     > 0: node.cpu_cores     = cpu_cores
    if disk_total_gb > 0: node.disk_total_gb = disk_total_gb
    db.commit()
    return RedirectResponse("/nodes?success=node_updated", status_code=303)


# ── Status APIs ────────────────────────────────────────────────────────────

@router.get("/api/nodes/status")
def api_node_status(request: Request, db: Session = Depends(get_db)):
    user = AuthService.get_user(request, db)
    if not user or not user.is_admin:
        raise HTTPException(403)
    nodes = db.query(Node).all()
    return [
        {
            "id":        n.id,
            "name":      n.display_name or n.name,
            "node_type": n.node_type or "incus",
            "status":    n.status,
            "ram_used":  n.ram_used_mb,
            "ram_total": n.ram_total_mb,
            "vps_count": db.query(VPS).filter(VPS.node_id == n.id).count(),
        }
        for n in nodes
    ]


@router.get("/api/nodes/{node_id}/stats")
def api_node_stats(node_id: int, request: Request, db: Session = Depends(get_db)):
    """Return current node resource stats, refreshing from live Incus."""
    user = AuthService.get_user(request, db)
    if not user or not user.is_admin:
        raise HTTPException(403)

    node = db.query(Node).filter(Node.id == node_id).first()
    if not node:
        raise HTTPException(404)

    try:
        NodeSelector.refresh_node(db, node)
        db.expire_all()
        node = db.query(Node).filter(Node.id == node_id).first()
    except Exception as exc:
        # refresh_node() already sets offline and commits on error; just log here
        logger.warning("Stats refresh failed for node %d: %s — node forced offline", node_id, exc)
        db.expire_all()
        node = db.query(Node).filter(Node.id == node_id).first()

    ram_pct  = round(node.ram_used_mb / node.ram_total_mb * 100, 1) if node.ram_total_mb > 0 else 0
    disk_pct = round((node.disk_used_gb or 0) / (node.disk_total_gb or 1) * 100, 1) if node.disk_total_gb else 0

    running = db.query(VPS).filter(VPS.node_id == node_id, VPS.status == "running").count()
    total   = db.query(VPS).filter(VPS.node_id == node_id).count()

    # Persist metric snapshot for history graphs
    try:
        from models import NodeMetric
        from sqlalchemy import text as sa_text
        db.add(NodeMetric(
            node_id=node_id,
            cpu_pct=node.cpu_load or 0,
            ram_used_mb=node.ram_used_mb,
            ram_total_mb=node.ram_total_mb,
            disk_used_gb=node.disk_used_gb or 0,
            disk_total_gb=node.disk_total_gb or 0,
            instance_count=running,
        ))
        db.execute(sa_text(
            "DELETE FROM node_metrics WHERE node_id = :nid AND id NOT IN "
            "(SELECT id FROM node_metrics WHERE node_id = :nid ORDER BY recorded_at DESC LIMIT 120)"
        ), {"nid": node_id})
        db.commit()
    except Exception as exc:
        logger.debug("Metric snapshot error for node %d: %s", node_id, exc)
        db.rollback()

    return JSONResponse({
        "id":                node.id,
        "name":              node.display_name or node.name,
        "node_type":         node.node_type or "incus",
        "status":            node.status,
        "cpu_pct":           round(node.cpu_load or 0, 1),
        "cpu_cores":         node.cpu_cores or 0,
        "ram_used_mb":       node.ram_used_mb,
        "ram_total_mb":      node.ram_total_mb,
        "ram_pct":           ram_pct,
        "disk_used_gb":      node.disk_used_gb or 0,
        "disk_total_gb":     node.disk_total_gb or 0,
        "disk_pct":          disk_pct,
        "running_instances": running,
        "total_instances":   total,
        "max_vps":           node.max_vps,
        "location":          node.location or "—",
    })


@router.get("/api/nodes/{node_id}/history")
def api_node_history(node_id: int, request: Request, db: Session = Depends(get_db)):
    """Return last 60 metric snapshots for sparkline graphs."""
    user = AuthService.get_user(request, db)
    if not user or not user.is_admin:
        raise HTTPException(403)

    from models import NodeMetric
    snapshots = list(reversed(
        db.query(NodeMetric)
        .filter(NodeMetric.node_id == node_id)
        .order_by(NodeMetric.recorded_at.desc())
        .limit(60)
        .all()
    ))
    return JSONResponse({
        "cpu":       [round(s.cpu_pct, 1) for s in snapshots],
        "ram":       [s.ram_used_mb       for s in snapshots],
        "ram_total": snapshots[-1].ram_total_mb if snapshots else 0,
        "labels":    [s.recorded_at.strftime("%H:%M") for s in snapshots],
    })


@router.get("/api/nodes/refresh-all")
def api_refresh_all(request: Request, db: Session = Depends(get_db)):
    user = AuthService.get_user(request, db)
    if not user or not user.is_admin:
        raise HTTPException(403)
    NodeSelector.refresh_all(db)
    return JSONResponse({"refreshed": db.query(Node).count()})
