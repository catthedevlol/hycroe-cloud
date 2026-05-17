# Hycroe Panel — v4.0.0 Changelog

## What Changed

### 1. UI Complete Redesign
- **New design system**: DM Sans + DM Mono fonts, clean dark palette with `--bg` variables
- **Professional layout**: Sticky sidebar with proper section labels, responsive hamburger menu
- **Cards**: Subtle borders, proper spacing, hover states — no more "glowing cyan" AI aesthetics
- **Badges**: Dot-prefix status indicators matching Hetzner/Proxmox style
- **Buttons**: Consistent `.btn` system with primary/secondary/ghost/danger variants
- **Forms**: Consistent `.form-input` / `.form-label` with proper focus rings
- **Responsive**: Works on mobile — sidebar slides in, layout adapts to small screens
- **Empty states**: Clean, actionable empty states instead of blank areas
- **Toast system**: Redesigned non-intrusive notifications

### 2. Mobile Console Fix
- Added `maximum-scale=1.0` viewport to prevent iOS zoom on input focus
- Uses `window.visualViewport` API to detect keyboard open/close and re-fit terminal
- `fitAddon.fit()` is debounced on `resize`, `visualViewport.resize`, and `scroll`
- Added reconnect overlay with spinner instead of plain text
- Console is now full-screen with no sidebar/topbar interference
- Fullscreen button added (⛶) for desktop
- Mobile tap-to-focus on terminal wrapper
- Auto-reconnect with exponential backoff (up to 5 attempts)
- Terminal reset (not just clear) on reconnect

### 3. Proxmox Node Support
- **New `services/proxmox.py`**: Full Proxmox VE API v2 client using API tokens
  - Node resource info (CPU, RAM, disk)
  - List VMs (QEMU) and containers (LXC)
  - Start / stop / reboot / shutdown
  - Create VM with ISO selection
  - Create LXC container with template
  - Storage pool listing
- **Updated `models/node.py`**: Added `node_type`, `proxmox_node`, `proxmox_token_id`, `proxmox_token_secret` columns
- **Updated `api/nodes.py`**: 
  - Two separate Add Node modals (Incus vs Proxmox)
  - `/nodes/{id}/proxmox` page shows all VMs + LXC with start/stop/reboot actions
  - REST endpoints for creating VMs/LXC, power actions, listing VMs
- **Updated `services/node_selector.py`**: Auto-detects node type for resource refresh; Proxmox nodes excluded from Incus VPS scheduling
- **Setup guide** updated with Proxmox token creation instructions

### 4. Stability Improvements
- **`main.py`**: Added `catch_exceptions` middleware for uncaught errors; proper 403/404/500 HTML error pages; quieted noisy log spam from SQLAlchemy and uvicorn.access
- **`services/proxmox.py`**: Uses stdlib `urllib` only (no extra deps); all methods return `{"success": bool, ...}` consistently; `ProxmoxError` exception type for clean error propagation
- **`services/node_selector.py`**: Wrapped DB commits in try/except with `db.rollback()` on failure; separate `_refresh_incus` / `_refresh_proxmox` methods
- **`api/nodes.py`**: `db.expire_all()` instead of broken `db.refresh` call; proper error logging for all Proxmox API failures
- **Error pages**: Styled HTML error pages for 403/404/500 instead of plain text

## Database Migration Note

If upgrading from v3.x, run this SQL to add the new Proxmox columns:

```sql
ALTER TABLE nodes ADD COLUMN node_type VARCHAR DEFAULT 'incus';
ALTER TABLE nodes ADD COLUMN proxmox_node VARCHAR;
ALTER TABLE nodes ADD COLUMN proxmox_token_id VARCHAR;
ALTER TABLE nodes ADD COLUMN proxmox_token_secret VARCHAR;
```

Or delete the DB file and let SQLAlchemy recreate it (dev only).
