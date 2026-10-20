"""Small scheduled checks and alerts. Uses environment-only secrets, no Sanic import."""
import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.backup import check_sqlite  # noqa: E402
from src.backup_offsite import rclone  # noqa: E402
from src.ops_state import locked, read_json, update_component, utcnow  # noqa: E402
from src.telegram_alerts import notify  # noqa: E402


def collect(  # noqa: C901
    db_path,
    data_dir,
    state_dir,
    *,
    url='http://127.0.0.1:8081/healthcheck',
    remote=None,
    config=None,
    disk_threshold=85,
    check_units=False,
):
    # No trustworthy running-image revision source yet; host Git/env can be stale.
    revision = None
    with locked(Path(state_dir) / 'collector.lock'):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                healthy = response.status == 200 and response.read(16).strip() == b'OK'
            update_component(
                state_dir,
                'application',
                {
                    'status': 'OK' if healthy else 'CRITICAL',
                    'code': 'ok' if healthy else 'unavailable',
                    'revision': revision,
                },
            )
        except Exception:
            update_component(
                state_dir, 'application', {'status': 'CRITICAL', 'code': 'unavailable', 'revision': revision}
            )
        try:
            check_sqlite(db_path)
            update_component(
                state_dir, 'sqlite', {'status': 'OK', 'code': 'ok', 'available': True, 'integrity_at': utcnow()}
            )
        except Exception:
            update_component(
                state_dir, 'sqlite', {'status': 'CRITICAL', 'code': 'sqlite_integrity_failed', 'available': False}
            )
        try:
            disk = shutil.disk_usage(data_dir)
            percent = 100 * disk.used / disk.total
            low = percent >= disk_threshold
            update_component(
                state_dir,
                'disk',
                {
                    'status': 'WARNING' if low else 'OK',
                    'code': 'disk_low' if low else 'ok',
                    'free_bytes': disk.free,
                    'used_percent': percent,
                },
            )
        except Exception:
            update_component(state_dir, 'disk', {'status': 'UNKNOWN', 'code': 'unavailable'})
        try:
            if not remote:
                raise ValueError('remote not configured')
            rclone(['lsf', remote, '--max-depth', '1'], config)
            update_component(state_dir, 's3_probe', {'status': 'OK', 'code': 'ok'})
        except Exception:
            update_component(state_dir, 's3_probe', {'status': 'CRITICAL', 'code': 's3_unavailable'})
        if check_units:
            check_systemd(state_dir)
        state = read_json(Path(state_dir) / 'health.json')
        restore = state.get('restore')
        if not isinstance(restore, dict):
            restore = {'status': 'UNKNOWN', 'code': 'never_tested'}
        restore.pop('checked_at', None)
        update_component(state_dir, 'restore', restore)


def check_systemd(state_dir):
    for unit, component, code in [
        ('competitions-backup.service', 'local', 'backup_failed'),
        ('competitions-backup-offsite.service', 's3', 's3_upload_failed'),
    ]:
        try:
            result = subprocess.run(
                ['systemctl', 'show', unit, '--property=Result', '--value'], capture_output=True, timeout=5, check=False
            )
            if result.returncode == 0 and result.stdout.strip() not in {b'success', b''}:
                update_component(state_dir, component, {'status': 'CRITICAL', 'code': code})
        except (OSError, subprocess.TimeoutExpired):
            pass  # No fabricated success; source freshness still degrades naturally.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db-path', required=True)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--check-systemd', action='store_true')
    args = parser.parse_args()
    collect(
        Path(args.db_path),
        Path(args.data_dir),
        Path(args.state_dir),
        url=os.getenv('HEALTH_APP_URL', 'http://127.0.0.1:8081/healthcheck'),
        remote=os.getenv('BACKUP_CRYPT_REMOTE'),
        config=os.getenv('RCLONE_CONFIG'),
        disk_threshold=float(os.getenv('HEALTH_DISK_WARNING_PERCENT', '85')),
        check_units=args.check_systemd,
    )
    notify(
        args.state_dir,
        enabled=os.getenv('TELEGRAM_ENABLED', 'false').lower() == 'true',
        token=os.getenv('TELEGRAM_BOT_TOKEN', ''),
        chat_id=os.getenv('TELEGRAM_CHAT_ID', ''),
        timeout=float(os.getenv('TELEGRAM_TIMEOUT_SECONDS', '5')),
        retries=int(os.getenv('TELEGRAM_RETRIES', '2')),
    )


if __name__ == '__main__':
    main()
