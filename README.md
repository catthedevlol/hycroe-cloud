# Hycroe Cloud

Self-hosted LXC/Incus management panel built with FastAPI.

> Lightweight infrastructure management platform focused on containers, node management, and automation.

---

## Features

- LXC / Incus container management
- Node management system
- FastAPI backend
- Authentication system
- Billing structure
- Worker/background task system
- Docker deployment support
- Proxmox integration
- Template-based frontend
- API-driven architecture

---

## Tech Stack

### Backend
- Python
- FastAPI
- SQLAlchemy
- Jinja2

### Infrastructure
- Incus / LXC
- Docker
- Proxmox

### Database
- SQLite / PostgreSQL (configurable)

---

## Project Structure

```bash
api/          # API routes
models/       # Database models
services/     # Infrastructure services
templates/    # Frontend templates
workers/      # Background workers


---

Installation

Clone Repository

git clone https://github.com/catthedevlol/hycroe-cloud.git
cd hycroe-cloud

Install Dependencies

pip install -r requirements.txt

Run Application

python main.py


---

Docker

docker-compose up -d


---

Goals

Hycroe Cloud aims to provide:

Simple self-hosted infrastructure management

Lightweight container orchestration

Easy deployment workflows

Automation-focused tooling

Minimal resource usage



---

Status

Project is currently under active development.

Expect:

bugs

unfinished features

breaking changes



---

Screenshots

Coming soon.


---

Security Notice

This project is experimental.

Do NOT expose production instances publicly without:

proper firewall rules

HTTPS

secure authentication

infrastructure hardening



---

Contributing

Pull requests, issues, and feedback are welcome.


---

License

MIT License
