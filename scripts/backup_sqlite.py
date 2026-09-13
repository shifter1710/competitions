"""Таймерный бэкап SQLite (deploy/systemd/competitions-backup.service).

Вся логика — в src/backup.py: тот же код исполняет ручной бэкап
с /admin/maintenance (замечание №10 из design/prototype/FEEDBACK.md).
Скрипт при успехе пишет audit-событие backup_created с source: 'timer'
(замечание №16а); отключается флагом --no-audit.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backup import run_backup  # noqa: E402
from src.backup import write_backup_audit_event  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(description='Create compressed SQLite backups with retention.')
    parser.add_argument('--db-path', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--files-dir', default=None, help='attachment directory to archive alongside the database')
    parser.add_argument('--keep', type=int, default=14)
    parser.add_argument(
        '--no-audit',
        action='store_true',
        help='do not write a backup_created audit event (written by default)',
    )
    return parser.parse_args()


def main():
    args = parse_args()
    db_path = Path(args.db_path)
    result = run_backup(
        db_path,
        Path(args.output_dir),
        keep=args.keep,
        files_dir=Path(args.files_dir) if args.files_dir else None,
    )
    print(result['archive'])
    if result['files_archive']:
        print(result['files_archive'])

    if not args.no_audit:
        written = write_backup_audit_event(
            db_path,
            {
                'source': 'timer',
                'archive': result['archive'].name,
                'archive_size': result['archive'].stat().st_size,
                'files_archive': result['files_archive'].name if result['files_archive'] else None,
            },
        )
        if not written:
            print('warning: backup created, but the audit event was not written', file=sys.stderr)


if __name__ == '__main__':
    main()
