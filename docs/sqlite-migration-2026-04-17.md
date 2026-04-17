## SQLite migration on 2026-04-17

### What changed

- The application storage was switched from MongoDB to SQLite.
- Runtime database path is `./data/competitions.sqlite3` on the host and `/code/data/competitions.sqlite3` inside the container.
- The Docker Compose stack no longer requires a MongoDB service for normal operation.

### Data migration

- Existing MongoDB data was exported to `data/migration-backups/competitions-mongo-backup-2026-04-17.json`.
- Records were migrated into SQLite with `scripts/migrate_mongo_to_sqlite.py`.
- Result after migration: `2` records in SQLite.

### Validation

- `docker compose up -d --build competitions` completed successfully.
- The `competitions` container became healthy.
- `GET /healthcheck` returned `200 OK`.

### Rollback

- Keep the JSON backup file in `data/migration-backups/`.
- Keep the old `competitions-mongo` container stopped until the new runtime is confirmed stable.
- To roll back, restore the Mongo-backed code/config and import the backup JSON back into MongoDB.
