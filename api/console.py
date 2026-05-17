"""
Console router — xterm.js PTY WebSocket terminal.

Uses os.openpty() to allocate a real PTY master/slave pair:
  - Interactive programs (vim, htop, etc.) work correctly.
  - TIOCSWINSZ resize signals propagate instantly to the container.
  - select() used for blocking I/O — no CPU spin, no polling loop.
  - Proper error messages for common failure modes (container not running, etc.)
"""
import asyncio
import fcntl
import json
import logging
import os
import select
import struct
import termios

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import get_db
from models import VPS
from services.auth import AuthService

router = APIRouter()
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)

_CHUNK = 4096


@router.get("/console/{name}", response_class=HTMLResponse)
def console_page(name: str, request: Request, db: Session = Depends(get_db)):
    user = AuthService.get_user(request, db)
    if not user:
        return RedirectResponse("/", status_code=303)
    if user.is_admin:
        vps = db.query(VPS).filter(VPS.name == name).first()
    else:
        vps = db.query(VPS).filter(VPS.name == name, VPS.user_id == user.id).first()
    if not vps:
        from fastapi import HTTPException
        raise HTTPException(404, "VPS not found")
    return templates.TemplateResponse("console.html", {
        "request": request, "user": user, "vps": vps
    })


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    """Send TIOCSWINSZ to the PTY master fd so the container sees the new size."""
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass


def _read_master(fd: int, timeout: float = 0.2) -> bytes:
    """
    Blocking read from PTY master using select() with a timeout.
    Returns bytes read (may be empty on timeout), raises OSError on EIO (process exit).
    """
    r, _, _ = select.select([fd], [], [], timeout)
    if r:
        return os.read(fd, _CHUNK)   # raises OSError(EIO) when slave side closes
    return b""


@router.websocket("/ws/console/{name}")
async def ws_console(websocket: WebSocket, name: str, db: Session = Depends(get_db)):
    """
    Full-duplex WebSocket ↔ incus exec PTY bridge.

    Client protocol (xterm.js):
      Binary frames → raw keyboard bytes
      Text JSON     → {type:"resize", cols:N, rows:M}  →  TIOCSWINSZ
    """
    # ── Auth ──────────────────────────────────────────────────────────────────
    token = websocket.cookies.get("session_token")
    user  = AuthService.get_user_by_token(token, db)
    if not user:
        await websocket.close(code=4001)
        return

    if user.is_admin:
        vps = db.query(VPS).filter(VPS.name == name).first()
    else:
        vps = db.query(VPS).filter(VPS.name == name, VPS.user_id == user.id).first()

    if not vps:
        await websocket.close(code=4004)
        return
    if vps.suspended:
        await websocket.close(code=4003)
        return

    await websocket.accept()

    # Remote prefix — None means local Incus
    remote = (vps.node.incus_remote or None) if vps.node else None
    target = f"{remote}:{name}" if remote else name

    # ── PTY allocation ────────────────────────────────────────────────────────
    master_fd, slave_fd = os.openpty()
    # Set a sane initial terminal size (80×24); client will send a resize right after connect
    _set_winsize(master_fd, 80, 24)

    env = {
        **os.environ,
        "TERM":    "xterm-256color",
        "LANG":    "en_US.UTF-8",
        "LC_ALL":  "en_US.UTF-8",
        "COLUMNS": "80",
        "LINES":   "24",
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            "incus", "exec", target, "--force-interactive", "--",
            "/bin/bash", "--login",
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            close_fds=True,
        )
        logger.info("Console PTY started: vps=%s target=%s pid=%s user=%s",
                    name, target, proc.pid, user.username)
    except FileNotFoundError:
        os.close(master_fd)
        os.close(slave_fd)
        logger.error("Console: incus binary not found in PATH for vps=%s", name)
        await websocket.send_text(
            "\r\n\x1b[1;31m[Error: incus not found. Is Incus installed?]\x1b[0m\r\n")
        await websocket.close()
        return
    except Exception as exc:
        os.close(master_fd)
        os.close(slave_fd)
        logger.error("Console: failed to start shell for vps=%s: %s", name, exc)
        await websocket.send_text(
            f"\r\n\x1b[1;31m[Failed to open shell: {exc}]\x1b[0m\r\n")
        await websocket.close()
        return

    # Close our copy of slave_fd — the child process owns it now
    os.close(slave_fd)

    # Keep master_fd BLOCKING — we use select() for readiness, not O_NONBLOCK polling
    # (O_NONBLOCK + run_in_executor causes a CPU spin; select + blocking read does not)

    done = asyncio.Event()
    loop = asyncio.get_event_loop()

    # ── Reader: PTY master → WebSocket ────────────────────────────────────────
    async def _reader():
        try:
            while not done.is_set():
                try:
                    # Run blocking select+read in a thread pool executor so the
                    # event loop remains free for the writer coroutine
                    chunk = await loop.run_in_executor(
                        None, lambda: _read_master(master_fd, timeout=0.2))
                    if chunk:
                        await websocket.send_bytes(chunk)
                    else:
                        # Timeout with no data — check if process has exited
                        if proc.returncode is not None:
                            logger.info("Console: process exited (rc=%d) vps=%s",
                                        proc.returncode, name)
                            break
                except OSError as exc:
                    # EIO = slave side closed (process exited)
                    logger.info("Console: PTY closed for vps=%s (%s)", name, exc)
                    break
                except Exception as exc:
                    logger.warning("Console reader error vps=%s: %s", name, exc)
                    break
        finally:
            done.set()

    # ── Writer: WebSocket → PTY master ────────────────────────────────────────
    async def _writer():
        try:
            while not done.is_set():
                try:
                    msg = await asyncio.wait_for(websocket.receive(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Send a keepalive so the browser doesn't time out
                    try:
                        await websocket.send_bytes(b"")
                    except Exception:
                        break
                    continue

                if msg.get("type") == "websocket.disconnect":
                    logger.info("Console: client disconnected vps=%s user=%s",
                                name, user.username)
                    break

                if msg.get("bytes"):
                    try:
                        os.write(master_fd, msg["bytes"])
                    except OSError as exc:
                        logger.info("Console: write error vps=%s: %s", name, exc)
                        break

                elif msg.get("text"):
                    try:
                        obj = json.loads(msg["text"])
                        if obj.get("type") == "resize":
                            cols = max(1, int(obj.get("cols", 80)))
                            rows = max(1, int(obj.get("rows", 24)))
                            _set_winsize(master_fd, cols, rows)
                            logger.debug("Console resize: vps=%s %dx%d", name, cols, rows)
                    except (json.JSONDecodeError, ValueError):
                        try:
                            os.write(master_fd, msg["text"].encode())
                        except OSError:
                            break

        except WebSocketDisconnect:
            logger.info("Console WS disconnected: vps=%s user=%s", name, user.username)
        except Exception as exc:
            logger.debug("Console writer ended: vps=%s reason=%s", name, exc)
        finally:
            done.set()

    reader_task = asyncio.create_task(_reader())
    writer_task = asyncio.create_task(_writer())

    try:
        await done.wait()
    finally:
        reader_task.cancel()
        writer_task.cancel()

        # Close PTY master — signals EIO to any lingering reads
        try:
            os.close(master_fd)
        except OSError:
            pass

        # Gracefully terminate the shell process
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        try:
            await websocket.close()
        except Exception:
            pass

        logger.info("Console session ended: vps=%s user=%s rc=%s",
                    name, user.username, proc.returncode)
