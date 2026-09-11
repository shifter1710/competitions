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

- The script uses SQLite online backup API instead of raw file copy, so the
  snapshot is consistent even while the app is running and with WAL enabled.
- Every archive is verified right after creation: the script decompresses it
  into a temporary file and runs `PRAGMA quick_check`. A failed verification
  deletes the archive and aborts with an error.
- Archives are written with `0600` permissions and the output directory is
  kept at `0700`: backups contain password hashes and student personal data.
- The timer schedule (`OnCalendar=*-*-* 03:15:00`) is interpreted in the
  **server's local timezone** (systemd default), not UTC. Check `timedatectl`
  on the host to know the actual backup time.
- A manual test run can be done with:
  `python3 scripts/backup_sqlite.py --db-path data/competitions.sqlite3 --output-dir data/backups/sqlite --keep 14`

### Restore procedure

1. Stop the application: `systemctl stop competitions-compose.service`
   (or `docker compose down` in the project directory).
2. Pick the archive to restore and decompress it:
   `gunzip -c data/backups/sqlite/competitions-YYYYMMDD-HHMMSS.sqlite3.gz > data/competitions.sqlite3`
3. Remove stale WAL/SHM files from the old database, if present:
   `rm -f data/competitions.sqlite3-wal data/competitions.sqlite3-shm`
4. Start the application back up and check `/healthcheck` and the main page.

Prefer restoring to a copy first and opening it with
`sqlite3 <copy> 'PRAGMA quick_check;'` if you want to inspect the data
before overwriting the live database.
