"""
Safe idempotent migration script.
PostgreSQL + SQLite compatible.
Called automatically from main.py on startup.
"""
import logging
from sqlalchemy import inspect, text
from database import engine

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_postgres() -> bool:
    return engine.dialect.name == "postgresql"


def _cols(insp, table: str) -> set:
    """Return set of column names for a table, empty set if table doesn't exist."""
    try:
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()


def _tables(insp) -> set:
    """Return set of table names in the current schema."""
    try:
        return set(insp.get_table_names())
    except Exception:
        return set()


def _add_column(conn, table: str, column: str, definition: str):
    """
    Add a column to a table.
    PostgreSQL supports IF NOT EXISTS natively (9.6+).
    SQLite does not — we guard with a pre-check instead.
    """
    if _is_postgres():
        conn.execute(text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}"
        ))
    else:
        # SQLite: caller must check existence before calling
        conn.execute(text(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        ))


def _bool_default(value: bool) -> str:
    """Return dialect-correct BOOLEAN DEFAULT literal."""
    if _is_postgres():
        return "TRUE" if value else "FALSE"
    return "1" if value else "0"


def _serial_pk() -> str:
    """Return dialect-correct auto-increment primary key type."""
    if _is_postgres():
        return "SERIAL PRIMARY KEY"
    return "INTEGER PRIMARY KEY AUTOINCREMENT"


def _timestamp_type() -> str:
    if _is_postgres():
        return "TIMESTAMP"
    return "DATETIME"


def _text_type() -> str:
    """PostgreSQL prefers TEXT; SQLite VARCHAR is fine for both."""
    return "TEXT"


# ── Migration runner ──────────────────────────────────────────────────────────

def run_migrations():
    insp = inspect(engine)
    existing_tables = _tables(insp)

    # ── 1. coupons.created_by ─────────────────────────────────────────────────
    cc = _cols(insp, "coupons")
    if "created_by" not in cc:
        try:
            with engine.begin() as conn:
                _add_column(conn, "coupons", "created_by",
                            "INTEGER REFERENCES users(id)")
            logger.info("Migration: added coupons.created_by")
        except Exception as e:
            logger.warning("Migration coupons.created_by skipped: %s", e)

    # ── 2. vps_plans table ────────────────────────────────────────────────────
    if "vps_plans" not in existing_tables:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"""
                    CREATE TABLE vps_plans (
                        id           {_serial_pk()},
                        name         VARCHAR(255) NOT NULL,
                        ram_mb       INTEGER NOT NULL,
                        cpu          INTEGER NOT NULL,
                        disk_gb      INTEGER NOT NULL,
                        credits_cost INTEGER NOT NULL,
                        location     VARCHAR(255),
                        node_id      INTEGER REFERENCES nodes(id),
                        is_active    BOOLEAN DEFAULT {_bool_default(True)},
                        created_by   INTEGER REFERENCES users(id),
                        created_at   {_timestamp_type()} DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            logger.info("Migration: created vps_plans table")
        except Exception as e:
            logger.warning("Migration vps_plans skipped: %s", e)

    # ── 3. Transaction multi-gateway columns ──────────────────────────────────
    tc = _cols(insp, "transactions")
    for col in ("gateway", "gateway_payment_id", "gateway_status", "currency", "amount_paid"):
        if col not in tc:
            try:
                with engine.begin() as conn:
                    _add_column(conn, "transactions", col, "VARCHAR(255)")
                logger.info("Migration: added transactions.%s", col)
            except Exception as e:
                logger.warning("Migration transactions.%s skipped: %s", col, e)

    # ── 4. node_metrics table ─────────────────────────────────────────────────
    if "node_metrics" not in existing_tables:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"""
                    CREATE TABLE node_metrics (
                        id             {_serial_pk()},
                        node_id        INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                        cpu_pct        REAL DEFAULT 0,
                        ram_used_mb    INTEGER DEFAULT 0,
                        ram_total_mb   INTEGER DEFAULT 0,
                        disk_used_gb   INTEGER DEFAULT 0,
                        disk_total_gb  INTEGER DEFAULT 0,
                        instance_count INTEGER DEFAULT 0,
                        recorded_at    {_timestamp_type()} DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                conn.execute(text(
                    "CREATE INDEX idx_node_metrics_node_time "
                    "ON node_metrics(node_id, recorded_at)"
                ))
            logger.info("Migration: created node_metrics table")
        except Exception as e:
            logger.warning("Migration node_metrics skipped: %s", e)

    # ── 5. nodes — disk columns ───────────────────────────────────────────────
    nc = _cols(insp, "nodes")
    for col in ("disk_total_gb", "disk_used_gb"):
        if col not in nc:
            try:
                with engine.begin() as conn:
                    _add_column(conn, "nodes", col, "INTEGER DEFAULT 0")
                logger.info("Migration: added nodes.%s", col)
            except Exception as e:
                logger.warning("Migration nodes.%s skipped: %s", col, e)

    # ── 6. panel_settings table ───────────────────────────────────────────────
    # Re-inspect: Base.metadata.create_all may have just created it
    insp2 = inspect(engine)
    existing_tables2 = _tables(insp2)

    if "panel_settings" not in existing_tables2:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"""
                    CREATE TABLE panel_settings (
                        id                     INTEGER PRIMARY KEY,
                        panel_name             VARCHAR(255) DEFAULT 'Hycroe Panel',
                        panel_description      TEXT DEFAULT 'Infrastructure management for your cluster.',
                        theme_color            VARCHAR(20)  DEFAULT '#3b82f6',
                        logo_url               TEXT,
                        enable_registration    BOOLEAN DEFAULT {_bool_default(True)},
                        enable_discord_login   BOOLEAN DEFAULT {_bool_default(True)},
                        enable_billing         BOOLEAN DEFAULT {_bool_default(True)},
                        require_discord_verify BOOLEAN DEFAULT {_bool_default(False)}
                    )
                """))
            logger.info("Migration: created panel_settings table")
        except Exception as e:
            logger.warning("Migration panel_settings create skipped: %s", e)

    # Ensure the single settings row (id=1) always exists
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT id FROM panel_settings WHERE id=1")
            ).fetchone()
            if not row:
                conn.execute(text(f"""
                    INSERT INTO panel_settings
                        (id, panel_name, panel_description, theme_color,
                         enable_registration, enable_discord_login,
                         enable_billing, require_discord_verify)
                    VALUES
                        (1, 'Hycroe Panel',
                         'Infrastructure management for your cluster.',
                         '#3b82f6',
                         {_bool_default(True)},
                         {_bool_default(True)},
                         {_bool_default(True)},
                         {_bool_default(False)})
                """))
                logger.info("Migration: seeded panel_settings row id=1")
    except Exception as e:
        logger.warning("Migration panel_settings seed skipped: %s", e)

    # ── 7. users — Discord columns ────────────────────────────────────────────
    # discord_id and discord_username were in the original schema.
    # discord_avatar and discord_verified are new in v3.
    # All four are checked so a fresh DB or an upgraded DB both end up correct.
    uc = _cols(insp, "users")

    discord_user_cols = {
        # column_name        : SQL definition (PostgreSQL + SQLite compatible)
        "discord_id":       f"{_text_type()} NULL",
        "discord_username": f"{_text_type()} NULL",
        "discord_avatar":   f"{_text_type()} NULL",
        "discord_verified": f"BOOLEAN DEFAULT {_bool_default(False)}",
    }

    for col, definition in discord_user_cols.items():
        if col not in uc:
            try:
                with engine.begin() as conn:
                    _add_column(conn, "users", col, definition)
                logger.info("Migration: added users.%s", col)
            except Exception as e:
                logger.warning("Migration users.%s skipped: %s", col, e)

    # ── 8. users — avatar_url (may be missing on very old DBs) ───────────────
    uc2 = _cols(inspect(engine), "users")
    if "avatar_url" not in uc2:
        try:
            with engine.begin() as conn:
                _add_column(conn, "users", "avatar_url", f"{_text_type()} NULL")
            logger.info("Migration: added users.avatar_url")
        except Exception as e:
            logger.warning("Migration users.avatar_url skipped: %s", e)

    # ── 9. vps — instance_type column ────────────────────────────────────────
    vc = _cols(inspect(engine), "vps")
    if "instance_type" not in vc:
        try:
            with engine.begin() as conn:
                _add_column(conn, "vps", "instance_type",
                            f"{_text_type()} DEFAULT 'container'")
            logger.info("Migration: added vps.instance_type")
        except Exception as e:
            logger.warning("Migration vps.instance_type skipped: %s", e)

    # ── 10. vps_plans — instance_type column ─────────────────────────────────
    plc = _cols(inspect(engine), "vps_plans")
    if "instance_type" not in plc:
        try:
            with engine.begin() as conn:
                _add_column(conn, "vps_plans", "instance_type",
                            f"{_text_type()} DEFAULT 'container'")
            logger.info("Migration: added vps_plans.instance_type")
        except Exception as e:
            logger.warning("Migration vps_plans.instance_type skipped: %s", e)

    # ── 11. panel_settings — discord_webhook_url column ──────────────────────
    psc = _cols(inspect(engine), "panel_settings")
    if "discord_webhook_url" not in psc:
        try:
            with engine.begin() as conn:
                _add_column(conn, "panel_settings", "discord_webhook_url",
                            f"{_text_type()} NULL")
            logger.info("Migration: added panel_settings.discord_webhook_url")
        except Exception as e:
            logger.warning("Migration panel_settings.discord_webhook_url skipped: %s", e)

    # ── 12. panel_settings — notify_user_id column ───────────────────────────
    # Also handles panels that previously had admin_id — both are checked so
    # re-running migrations on any DB version is safe.
    psc2 = _cols(inspect(engine), "panel_settings")
    if "notify_user_id" not in psc2:
        try:
            with engine.begin() as conn:
                _add_column(conn, "panel_settings", "notify_user_id",
                            f"{_text_type()} NULL")
            logger.info("Migration: added panel_settings.notify_user_id")
        except Exception as e:
            logger.warning("Migration panel_settings.notify_user_id skipped: %s", e)
    # Legacy: rename admin_id → notify_user_id data if the old column exists
    psc3 = _cols(inspect(engine), "panel_settings")
    if "admin_id" in psc3:
        try:
            with engine.begin() as conn:
                # Copy data from old column into new one (no-op if notify_user_id already has data)
                conn.execute(text(
                    "UPDATE panel_settings SET notify_user_id = admin_id "
                    "WHERE notify_user_id IS NULL AND admin_id IS NOT NULL"
                ))
            logger.info("Migration: migrated panel_settings.admin_id → notify_user_id")
        except Exception as e:
            logger.warning("Migration admin_id→notify_user_id data copy skipped: %s", e)

    # ── 13. tickets table ─────────────────────────────────────────────────────
    insp3 = inspect(engine)
    if "tickets" not in _tables(insp3):
        try:
            with engine.begin() as conn:
                conn.execute(text(f"""
                    CREATE TABLE tickets (
                        id         {_serial_pk()},
                        title      VARCHAR(200) NOT NULL,
                        message    {_text_type()} NOT NULL,
                        user_id    INTEGER NOT NULL REFERENCES users(id),
                        status     VARCHAR(20) DEFAULT 'open',
                        created_at {_timestamp_type()} DEFAULT CURRENT_TIMESTAMP,
                        updated_at {_timestamp_type()} DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                conn.execute(text(
                    "CREATE INDEX idx_tickets_user_id ON tickets(user_id)"
                ))
            logger.info("Migration: created tickets table")
        except Exception as e:
            logger.warning("Migration tickets skipped: %s", e)

    # ── 14. ticket_messages table ─────────────────────────────────────────────
    insp4 = inspect(engine)
    if "ticket_messages" not in _tables(insp4):
        try:
            with engine.begin() as conn:
                conn.execute(text(f"""
                    CREATE TABLE ticket_messages (
                        id         {_serial_pk()},
                        ticket_id  INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                        user_id    INTEGER NOT NULL REFERENCES users(id),
                        message    {_text_type()} NOT NULL,
                        created_at {_timestamp_type()} DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                conn.execute(text(
                    "CREATE INDEX idx_ticket_messages_ticket_id "
                    "ON ticket_messages(ticket_id)"
                ))
            logger.info("Migration: created ticket_messages table")
            # Seed: promote existing Ticket.message into first chat message
            try:
                with engine.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO ticket_messages (ticket_id, user_id, message, created_at)
                        SELECT id, user_id, message, created_at FROM tickets
                        WHERE message IS NOT NULL AND message != ''
                    """))
                logger.info("Migration: seeded ticket_messages from tickets.message")
            except Exception as seed_exc:
                logger.warning("Migration ticket_messages seed skipped: %s", seed_exc)
        except Exception as e:
            logger.warning("Migration ticket_messages skipped: %s", e)

    # ── 15. vps — expires_at column ───────────────────────────────────────────
    vc2 = _cols(inspect(engine), "vps")
    if "expires_at" not in vc2:
        try:
            with engine.begin() as conn:
                _add_column(conn, "vps", "expires_at", f"{_timestamp_type()} NULL")
            logger.info("Migration: added vps.expires_at")
        except Exception as e:
            logger.warning("Migration vps.expires_at skipped: %s", e)

    # ── 16. panel_settings — split webhooks + announcement ───────────────────
    psc4 = _cols(inspect(engine), "panel_settings")
    for col in ("node_webhook_url", "admin_log_webhook_url", "announcement"):
        if col not in psc4:
            try:
                with engine.begin() as conn:
                    _add_column(conn, "panel_settings", col, f"{_text_type()} NULL")
                logger.info("Migration: added panel_settings.%s", col)
            except Exception as e:
                logger.warning("Migration panel_settings.%s skipped: %s", col, e)

    # ── 17. panel_settings — abuse_alert_webhook_url ──────────────────────────
    psc5 = _cols(inspect(engine), "panel_settings")
    if "abuse_alert_webhook_url" not in psc5:
        try:
            with engine.begin() as conn:
                _add_column(conn, "panel_settings", "abuse_alert_webhook_url",
                            f"{_text_type()} NULL")
            logger.info("Migration: added panel_settings.abuse_alert_webhook_url")
        except Exception as e:
            logger.warning("Migration panel_settings.abuse_alert_webhook_url skipped: %s", e)

    logger.info("Migration: run_migrations() complete")


def _seed_local_node():
    """
    If the nodes table is empty and local Incus is reachable, create a
    'local' node record so the dashboard shows at least one node and
    VPS can be assigned and managed without manual admin setup.
    """
    from database import SessionLocal
    from models import Node
    from services.incus import IncusService

    db = SessionLocal()
    try:
        if db.query(Node).count() > 0:
            return  # nodes already exist, nothing to do

        info = IncusService.get_node_info(remote=None)
        if info.get("status") != "online":
            logger.info("Local Incus not reachable — skipping local node seed")
            return

        local = Node(
            name="local",
            display_name="Local Node",
            address="127.0.0.1",
            port=8443,
            node_type="incus",
            incus_remote=None,          # None means use local incus binary directly
            status="online",
            is_default=True,
            ram_total_mb=info.get("ram_total_mb", 0),
            ram_used_mb=info.get("ram_used_mb", 0),
            cpu_cores=info.get("cpu_cores", 0),
            cpu_load=info.get("cpu_load", 0.0),
            max_vps=50,
        )
        db.add(local)
        db.commit()
        logger.info("Migration: seeded local Incus node (ram=%dMB total)",
                    local.ram_total_mb)
    except Exception as exc:
        logger.warning("Could not seed local node: %s", exc)
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migrations()
    _seed_local_node()
    print("Migrations complete.")
