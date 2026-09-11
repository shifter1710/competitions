# Competitions Agent Context

This repository is one project inside `/root/chatgpt`. It is a SIBADI student
sports competitions management app.

## Purpose

Manage student sports competition participation records, imports, exports,
custom fields, and table views.

## Current Shape

- Backend: Sanic.
- Templates: Jinja2.
- Frontend: vanilla JavaScript.
- Runtime database: SQLite at `./data/competitions.sqlite3`.
- Docker container published on `127.0.0.1:8081` and intended to be proxied by
  nginx.
- Deployment templates target `dokin-app.online`.

## Key Files

- `README.md`
- `src/`
- `data/`
- `deploy/nginx/competitions.conf`
- `deploy/systemd/`
- `scripts/backup_sqlite.py`
- `scripts/migrate_mongo_to_sqlite.py`

## Notes

- `data/` is live application data and is mounted by the running container. Do
  not casually move, delete, or rewrite it.
- `.env` is local; use `.env.example` for tracked config shape.
- The `admin` role manages custom fields and destructive actions.
- The `editor` role is intended for data entry, imports, template download, and
  record edits.

## Safe Start

- Read `README.md`.
- For UI/data behavior, inspect `src/` before changing deploy files.
- For deployment changes, inspect `deploy/` and ask before restarting services.
