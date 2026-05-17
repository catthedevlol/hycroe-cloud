"""
services/abuse.py — Two-stage, threshold-based CPU overload protection.

Stages:
  WARNING  (>80% sustained) — flag VPS, log, send alert. Do NOT stop.
  CRITICAL (>90% sustained) — force stop VPS, suspend, send alert.

Sliding window:
  Last 6 CPU samples are kept per VPS.  Each sample is collected by the
  periodic abuse_check job (every ~5 min), so 6 samples = ~30 min window.

  WARNING  requires CPU_WARNING_SAMPLES  consecutive samples above WARNING_PCT.
  CRITICAL requires CPU_CRITICAL_SAMPLES consecutive samples above CRITICAL_PCT.

Cooldown:
  After any action (warn or stop), the VPS is immune for COOLDOWN_SECONDS.
  This prevents repeated alerts/stops on the same VPS every cycle.

Safety:
  - Missing or zero metrics → skip silently (fail safe, never auto-stop).
  - DB commit failures → log + rollback, never crash the worker.
  - All state is in-process; losing it on restart only delays detection by
    one window (advisory data — acceptable).

Structured log format:
  event=abuse_warning vps_id=X user_id=Y cpu=Z node=N
  event=abuse_stop    vps_id=X user_id=Y cpu=Z node=N
"""
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import NamedTuple

logger = logging.getLogger(__name__)


# ── Tunable thresholds ─────────────────────────────────────────────────────────

WARNING_PCT           = 80.0   # CPU% to enter WARNING stage
CRITICAL_PCT          = 90.0   # CPU% to trigger enforcement (stop)

WINDOW_SIZE           = 6      # samples kept in sliding window
CPU_WARNING_SAMPLES   = 3      # consecutive samples > WARNING_PCT  → warn
CPU_CRITICAL_SAMPLES  = 3      # consecutive samples > CRITICAL_PCT → stop

COOLDOWN_SECONDS      = 900    # 15 min immunity after any action (warn or stop)


# ── Per-VPS in-process state ───────────────────────────────────────────────────

# vps_id → deque of (datetime, cpu_pct)
_cpu_window: dict[int, deque] = defaultdict(
    lambda: deque(maxlen=WINDOW_SIZE)
)

# vps_id → datetime of last action (warn or stop) — enforces cooldown
_last_action_ts: dict[int, datetime] = {}

# vps_id → bool: True if we've already fired a WARNING this cycle (no spam)
_warned_ids: set[int] = set()


# ── Event result type ──────────────────────────────────────────────────────────

class AbuseEvent(NamedTuple):
    vps_id:   int
    action:   str          # "warning" | "stopped"
    cpu_pct:  float
    vps_name: str
    user_id:  int
    node_name: str


# ── Internal helpers ───────────────────────────────────────────────────────────

def _in_cooldown(vps_id: int) -> bool:
    last = _last_action_ts.get(vps_id)
    if last is None:
        return False
    return (datetime.utcnow() - last).total_seconds() < COOLDOWN_SECONDS


def _stamp_cooldown(vps_id: int) -> None:
    _last_action_ts[vps_id] = datetime.utcnow()


def _consecutive_above(vps_id: int, threshold: float, n: int) -> bool:
    """True if the last `n` samples all exceed `threshold`."""
    window = _cpu_window.get(vps_id)
    if not window or len(window) < n:
        return False
    return all(pct >= threshold for _, pct in list(window)[-n:])


def _peak_cpu(vps_id: int) -> float:
    """Highest CPU value in the current window."""
    window = _cpu_window.get(vps_id)
    if not window:
        return 0.0
    return max(pct for _, pct in window)


# ── Public API ─────────────────────────────────────────────────────────────────

def record_cpu_sample(vps_id: int, cpu_pct: float) -> None:
    """Record one CPU% sample. Always safe to call — never raises."""
    _cpu_window[vps_id].append((datetime.utcnow(), cpu_pct))


def check_and_flag_abusers(db, vps_list) -> list[AbuseEvent]:
    """
    Iterate running VPS, sample CPU, and enforce abuse policy.

    Returns a list of AbuseEvent namedtuples describing every action taken.
    The caller (handler) is responsible for sending webhooks and logging.

    db        : open SQLAlchemy session — caller owns lifecycle.
    vps_list  : iterable of VPS ORM objects.

    Guarantees:
      - Never raises (all exceptions are caught and logged).
      - Never stops a VPS if metrics are zero/missing.
      - Respects per-VPS cooldown window.
    """
    from services.incus import IncusService

    events: list[AbuseEvent] = []
    db_dirty = False  # track if we need a commit

    for vps in vps_list:
        # Only inspect actively running, non-suspended instances
        if vps.suspended or vps.status not in ("running", "warning"):
            continue

        node      = vps.node
        remote    = (node.incus_remote or None) if node else None
        node_name = (node.display_name or node.name) if node else "local"

        # ── 1. Fetch metrics — skip on any failure ────────────────────────────
        try:
            metrics = IncusService.get_metrics(
                vps.name, remote=remote, ram_limit_mb=vps.ram
            )
            cpu_pct = float(metrics.get("cpu", 0))
        except Exception as exc:
            logger.debug(
                "event=abuse_metrics_skip vps_id=%d name=%s error=%s",
                vps.id, vps.name, exc,
            )
            continue

        # Safety: zero/unreliable metric → skip, never act on ghost data
        if cpu_pct <= 0:
            logger.debug(
                "event=abuse_metrics_zero vps_id=%d name=%s action=skip",
                vps.id, vps.name,
            )
            continue

        record_cpu_sample(vps.id, cpu_pct)

        logger.debug(
            "event=abuse_sample vps_id=%d name=%s user_id=%d cpu=%.1f "
            "node=%s window=%s",
            vps.id, vps.name, vps.user_id, cpu_pct, node_name,
            [round(p, 1) for _, p in _cpu_window[vps.id]],
        )

        # ── 2. Skip if in cooldown ────────────────────────────────────────────
        if _in_cooldown(vps.id):
            logger.debug(
                "event=abuse_cooldown vps_id=%d name=%s action=skip",
                vps.id, vps.name,
            )
            continue

        # ── 3. CRITICAL check — force stop ────────────────────────────────────
        if _consecutive_above(vps.id, CRITICAL_PCT, CPU_CRITICAL_SAMPLES):
            peak = _peak_cpu(vps.id)
            logger.warning(
                "event=abuse_stop vps_id=%d name=%s user_id=%d "
                "cpu=%.1f node=%s threshold=%.0f samples=%d",
                vps.id, vps.name, vps.user_id,
                peak, node_name, CRITICAL_PCT, CPU_CRITICAL_SAMPLES,
            )

            # Attempt stop — non-fatal if Incus fails
            try:
                IncusService.stop(vps.name, remote=remote)
            except Exception as stop_exc:
                logger.error(
                    "event=abuse_stop_failed vps_id=%d name=%s error=%s",
                    vps.id, vps.name, stop_exc,
                )

            reason = (
                f"CPU overload auto-stop: {round(peak, 1)}% sustained "
                f"(>{CRITICAL_PCT}% for {CPU_CRITICAL_SAMPLES} samples)"
            )
            vps.suspended   = True
            vps.status      = "suspended_overload"
            vps.last_action = datetime.utcnow()
            vps.notes = (
                (vps.notes or "").rstrip() +
                f"\n[OVERLOAD-STOP {datetime.utcnow().isoformat()}] {reason}"
            ).strip()

            # Clear window + set cooldown so the next cycle doesn't re-trigger
            _cpu_window[vps.id].clear()
            _warned_ids.discard(vps.id)
            _stamp_cooldown(vps.id)
            db_dirty = True

            events.append(AbuseEvent(
                vps_id=vps.id, action="stopped",
                cpu_pct=peak,  vps_name=vps.name,
                user_id=vps.user_id, node_name=node_name,
            ))
            continue  # don't also warn after stopping

        # ── 4. WARNING check — flag only, no stop ─────────────────────────────
        if _consecutive_above(vps.id, WARNING_PCT, CPU_WARNING_SAMPLES):
            if vps.id not in _warned_ids:
                peak = _peak_cpu(vps.id)
                logger.warning(
                    "event=abuse_warning vps_id=%d name=%s user_id=%d "
                    "cpu=%.1f node=%s threshold=%.0f samples=%d",
                    vps.id, vps.name, vps.user_id,
                    peak, node_name, WARNING_PCT, CPU_WARNING_SAMPLES,
                )

                vps.status      = "warning"
                vps.last_action = datetime.utcnow()
                vps.notes = (
                    (vps.notes or "").rstrip() +
                    f"\n[OVERLOAD-WARN {datetime.utcnow().isoformat()}] "
                    f"CPU {round(peak, 1)}% sustained (>{WARNING_PCT}%)"
                ).strip()

                _warned_ids.add(vps.id)
                _stamp_cooldown(vps.id)
                db_dirty = True

                events.append(AbuseEvent(
                    vps_id=vps.id, action="warning",
                    cpu_pct=peak,  vps_name=vps.name,
                    user_id=vps.user_id, node_name=node_name,
                ))
        else:
            # CPU dropped below warning — clear the warned flag so a new spike
            # will generate a fresh alert instead of being silenced.
            _warned_ids.discard(vps.id)

    # ── Commit all DB mutations in one shot ───────────────────────────────────
    if db_dirty:
        try:
            db.commit()
            logger.info(
                "event=abuse_db_commit actions=%d",
                len(events),
            )
        except Exception as commit_exc:
            logger.error(
                "event=abuse_db_commit_failed error=%s", commit_exc
            )
            try:
                db.rollback()
            except Exception:
                pass

    return events

