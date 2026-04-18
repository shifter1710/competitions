## SQLite backups

### What is included

- Backup script: `scripts/backup_sqlite.py`
- Systemd service: `deploy/systemd/competitions-backup.service`
- Systemd timer: `deploy/systemd/competitions-backup.timer`

### Backup target

- Source DB: `data/competitions.sqlite3`
- Output directory: `data/backups/sqlite`
- Format: compressed `.sqlite3.gz`
- Retention: keep the latest 14 backups

### Notes

- The script uses SQLite online backup API instead of raw file copy.
- Timer schedule is daily at `03:15 UTC`.
- A manual test run can be done with:
  `python3 scripts/backup_sqlite.py --db-path data/competitions.sqlite3 --output-dir data/backups/sqlite --keep 14`
