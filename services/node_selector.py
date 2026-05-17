"""
NodeSelector — picks the best Incus node and refreshes resource data.

Guarantees:
  - On ANY error during refresh → status is set to "offline" and committed immediately.
  - Every status transition (online↔offline) is logged at INFO level.
  - pick() returns None only when truly no node is available; caller must HARD FAIL.
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from models import Node
from services.incus import IncusService

logger = logging.getLogger(__name__)


class NodeSelector:

    @staticmethod
    def refresh_node(db: Session, node: Node) -> None:
        """
        Pull latest resource data from an Incus node and persist to DB.

        On success  → updates status, RAM, CPU, disk; commits.
        On ANY error → forces status='offline'; commits; never leaves stale data.

        Admin-supplied capacity (ram_total_mb, cpu_cores, disk_total_gb set via
        the Edit Node form) is treated as a floor — Incus values only replace
        them when Incus reports a non-zero value.
        """
        prev_status = node.status  # capture before any change

        try:
            remote = node.incus_remote if node.incus_remote else None
            logger.debug("Refreshing node %s (remote=%r)", node.name, remote)

            info = IncusService.get_node_info(remote=remote)

            new_status = info.get("status", "offline")
            node.status    = new_status
            node.last_seen = datetime.utcnow()

            # RAM total: keep existing non-zero admin value if Incus returns 0
            incus_ram_total = info.get("ram_total_mb", 0)
            if incus_ram_total > 0:
                node.ram_total_mb = incus_ram_total
            # RAM used: always live
            node.ram_used_mb = info.get("ram_used_mb", 0)

            # CPU cores: keep existing non-zero admin value if Incus returns 0
            incus_cores = info.get("cpu_cores", 0)
            if incus_cores > 0:
                node.cpu_cores = incus_cores

            # CPU load: always refresh
            node.cpu_load = info.get("cpu_load", node.cpu_load or 0.0)

            # Disk: keep existing non-zero admin value if Incus returns 0
            incus_disk_total = info.get("disk_total_gb", 0)
            incus_disk_used  = info.get("disk_used_gb",  0)
            if incus_disk_total > 0:
                node.disk_total_gb = incus_disk_total
            if incus_disk_used > 0:
                node.disk_used_gb = incus_disk_used

            db.commit()

            # Log status transitions at INFO, routine updates at DEBUG
            if prev_status != new_status:
                logger.info(
                    "Node %s STATUS CHANGED: %s → %s (ram=%d/%d MB, cpu=%.1f%%)",
                    node.name, prev_status, new_status,
                    node.ram_used_mb, node.ram_total_mb, node.cpu_load or 0,
                )
            else:
                logger.debug(
                    "Node %s refreshed: status=%s ram=%d/%d MB cpu=%.1f%%",
                    node.name, node.status,
                    node.ram_used_mb, node.ram_total_mb, node.cpu_load or 0,
                )

        except Exception as exc:
            # ANY error → mark offline immediately, never leave stale 'unknown'
            logger.warning(
                "Node %s refresh FAILED (%s) — forcing status=offline",
                node.name, exc,
            )
            try:
                prev = node.status
                node.status    = "offline"
                node.last_seen = datetime.utcnow()
                db.commit()

                if prev != "offline":
                    logger.info(
                        "Node %s STATUS CHANGED: %s → offline (incus unreachable)",
                        node.name, prev,
                    )
            except Exception as commit_exc:
                logger.error(
                    "Node %s: FAILED to persist offline status: %s",
                    node.name, commit_exc,
                )
                db.rollback()

    @classmethod
    def refresh_all(cls, db: Session) -> None:
        """Refresh all nodes. One bad node never blocks the rest."""
        nodes = db.query(Node).all()
        logger.info("Refreshing all nodes (%d total)", len(nodes))
        for node in nodes:
            cls.refresh_node(db, node)
        logger.info("Node refresh complete")

    @classmethod
    def pick(
        cls,
        db: Session,
        ram_needed: int,
        cpu_needed: int = 1,
        preferred_node_id: Optional[int] = None,
    ) -> Optional[Node]:
        """
        Return the best Node for a new VPS.

        Returns None ONLY when no node is available — callers MUST treat
        None as a hard failure and abort (never create a VPS with no node).

        Selection order:
          1. Preferred node (if specified, online, not in maintenance, has capacity)
          2. Best available online node with most free RAM
          3. Default node (fallback, only if online and not in maintenance)
        """
        from models import VPS as VPSModel

        # ── 1. Try preferred node ─────────────────────────────────────────────
        if preferred_node_id:
            node = db.query(Node).filter(
                Node.id == preferred_node_id,
                Node.status == "online",
                Node.maintenance == False,  # noqa
            ).first()
            if node and cls._can_fit(node, ram_needed):
                logger.debug(
                    "Node selection: preferred node %s (id=%d) selected",
                    node.name, node.id,
                )
                return node
            logger.debug(
                "Node selection: preferred node_id=%d rejected (not online/no capacity)",
                preferred_node_id,
            )

        # ── 2. Best online node by free RAM ───────────────────────────────────
        candidates = db.query(Node).filter(
            Node.status == "online",
            Node.maintenance == False,  # noqa
        ).all()

        best: Optional[Node] = None
        best_free = -1
        for node in candidates:
            free      = node.ram_total_mb - node.ram_used_mb
            vps_count = db.query(VPSModel).filter(VPSModel.node_id == node.id).count()
            if free >= ram_needed and vps_count < node.max_vps:
                logger.debug(
                    "Node selection candidate: %s free=%dMB vps=%d/%d",
                    node.name, free, vps_count, node.max_vps,
                )
                if free > best_free:
                    best_free = free
                    best = node

        if best:
            logger.info(
                "Node selection: auto-selected %s (id=%d, free_ram=%dMB)",
                best.name, best.id, best_free,
            )
            return best

        # ── 3. No candidates — log and return None ────────────────────────────
        logger.error(
            "Node selection: NO AVAILABLE NODE for ram_needed=%dMB cpu_needed=%d "
            "(online nodes=%d, candidates with capacity=%d)",
            ram_needed, cpu_needed,
            len(candidates),
            sum(
                1 for n in candidates
                if (n.ram_total_mb - n.ram_used_mb) >= ram_needed
            ),
        )
        return None

    @staticmethod
    def _can_fit(node: Node, ram_needed: int) -> bool:
        free = node.ram_total_mb - node.ram_used_mb
        # ram_total_mb == 0 means unknown/unconfigured — allow it as fallback
        return free >= ram_needed or node.ram_total_mb == 0
