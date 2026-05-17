# Hycroe Panel v3 → v3.1 Upgrade Guide

## What Changed

### New Files
- `Dockerfile` — containerized deployment
- `docker-compose.yml` — full stack (panel + PostgreSQL + Redis)
- `UPGRADE.md` — this file

### Modified Files
| File | Changes |
|------|---------|
| `main.py` | `load_dotenv()` on startup; 404/500 error handlers; docs hidden in production |
| `requirements.txt` | Added `psycopg2-binary`, `alembic`, `slowapi`, `redis` |
| `api/admin.py` | Added: create user, create VPS for user, admin VPS power actions, admin rebuild, JSON VPS list endpoint |
| `api/console.py` | Added `--force-interactive` flag for proper PTY; admin console access; graceful SIGTERM→SIGKILL cleanup; auto-reconnect |
| `templates/admin.html` | Full rewrite: create user modal, create VPS modal, admin VPS power/delete/rebuild controls, search/filter tables, flash messages |
| `templates/console.html` | Added web-links addon, paste support, smarter auto-reconnect with backoff, copy selection button |
| `.env.example` | Added `DB_PASSWORD` for Docker |

---

## Quick Start (Docker)

```bash
cp .env.example .env
# Edit .env — set SECRET_KEY, DB_PASSWORD, BASE_URL, optional Discord/Stripe keys

docker compose up -d
```

The panel will be available at `http://localhost:8000`.

**First-time setup:** Register at `/register` — the first registered user is
automatically the admin. (Or create the admin manually via the DB / API.)

---

## Running Without Docker (existing setup)

```bash
pip install -r requirements.txt
cp .env.example .env  # fill in values
uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## PostgreSQL Migration (SQLite → Postgres)

If you have existing data in `cloud.db` and want to migrate to PostgreSQL:

1. Install `pgloader`: `sudo apt install pgloader`
2. Run: `pgloader sqlite:///cloud.db postgresql://hycroe:pass@localhost/hycroe`
3. Update `DATABASE_URL` in `.env`

---

## New Admin Features

### Create a User
Admin Panel → top-right **+ New User** button  
Fields: username, password, email (optional), starting credits, admin flag.

### Create a VPS for Any User
Admin Panel → **+ New VPS** button (or click `+ vps` next to a user row).  
Bypasses credit check — admin allocates resources directly.

### Admin VPS Controls
In the **All VPS** tab, each row has:
- `start` / `stop` / `restart` — power actions
- `rebuild` — re-image with new OS (opens modal)
- `suspend` / `unsuspend` — freeze the instance
- `delete` — remove from Incus + database

### Admin Console Access
Admins can open `/console/<any-vps-name>` — not limited to their own VPS.

---

## Console Improvements

| Feature | v3 | v3.1 |
|---------|-----|-------|
| PTY flag | none | `--force-interactive` |
| Resize | stty only | stty + proper PTY |
| Auto-reconnect | none | yes (3 attempts, backoff) |
| Paste support | none | Ctrl+V / right-click |
| Copy selection | none | Copy button |
| Clickable URLs | none | xterm-addon-web-links |
| Process cleanup | SIGKILL | SIGTERM → SIGKILL |

---

## Discord OAuth

1. Create an app at https://discord.com/developers
2. Add redirect URI: `https://your-domain.com/auth/discord/callback`
3. Set in `.env`:
   ```
   DISCORD_CLIENT_ID=your_client_id
   DISCORD_CLIENT_SECRET=your_client_secret
   ```
4. The **"Continue with Discord"** button appears automatically on the login page.
5. New Discord users get a free account linked to their Discord ID.
6. Existing users who log in via Discord have their avatar updated.

---

## Security Notes

- `SECRET_KEY` **must** be changed in production (32+ random bytes)
- Set `SECURE_COOKIES=true` when running behind HTTPS
- API docs (`/docs`) are disabled in `ENV=production`
- Rate limiting is applied on login (10/min) and register (5/5min) by IP
- All container names are validated against `^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$`
- All subprocess calls use argument lists (no `shell=True`)
- Incus commands use whitelisted image names — no arbitrary image injection
