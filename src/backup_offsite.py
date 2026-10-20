"""Verified crypt offsite upload, independent of Sanic; never deletes objects."""
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from datetime import timezone
from pathlib import Path

from src.backup import BackupError
from src.backup import verify_set
from src.ops_state import locked
from src.ops_state import read_json
from src.ops_state import update_component
from src.ops_state import utcnow


def age_seconds(value, now=None):
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo is None:
            return None
        age = ((now or datetime.now(timezone.utc)) - date).total_seconds()
        return age if age >= 0 else None
    except (TypeError, ValueError):
        return None


def rclone(args, config=None):
    command = ['rclone']
    if config:
        command += ['--config', str(config)]
    try:
        result = subprocess.run(command + args, capture_output=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError('s3_unavailable') from exc
    if result.returncode:
        raise BackupError('s3_upload_failed')
    return result.stdout + result.stderr


def cryptcheck(directory, destination, expected, config=None):
    # rclone 1.60 exits 0 and writes '=' even when hashes were unavailable.
    # Explicit flags override inherited size-only/log settings; require both the
    # complete positive summary and exact checked filenames, never expose logs.
    with tempfile.TemporaryDirectory(prefix='cryptcheck-') as temporary:
        report_path = Path(temporary) / 'combined'
        output = rclone(
            [
                'cryptcheck',
                str(directory),
                destination,
                '--one-way',
                '--size-only=false',
                '--log-level',
                'INFO',
                '--use-json-log=false',
                '--syslog=false',
                '--log-file',
                '',
                '--combined',
                str(report_path),
            ],
            config,
        )
        report = output.decode('utf-8', errors='replace')
        matches = re.findall(r'\b([0-9]+) matching files\b', report)
        wanted = {'= ' + path.name for path in Path(directory).iterdir() if path.is_file()}
        try:
            lines = report_path.read_text().splitlines()
        except OSError as exc:
            raise BackupError('s3_verification_unproven') from exc
        if (
            'hashes could not be checked' in report
            or not re.search(r'\b0 differences found\b', report)
            or not matches
            or int(matches[-1]) != expected
            or len(wanted) != expected
            or len(lines) != expected
            or set(lines) != wanted
        ):
            raise BackupError('s3_verification_unproven')


def upload_set(directory, remote, *, config=None):
    manifest = verify_set(directory)
    identifier = manifest['id']
    if Path(directory).name != identifier or '/' in identifier or '..' in identifier:
        raise BackupError('invalid_set_id')
    destination = remote.rstrip('/') + '/sets/' + identifier
    # Temporary copies have fresh mtimes; crypt exposes no normal comparison hash.
    # Transfer decisions use size to resume without modifying existing objects.
    # --immutable still rejects size conflicts; mandatory cryptcheck below verifies
    # content (explicit --size-only=false), including same-size remote corruption.
    with tempfile.TemporaryDirectory() as temporary:
        for name in manifest['artifacts']:
            shutil.copyfile(Path(directory) / name, Path(temporary) / name)
        rclone(['copy', temporary, destination, '--immutable', '--size-only'], config)
        cryptcheck(temporary, destination, 2, config)
    rclone(
        [
            'copyto',
            str(Path(directory) / 'manifest.json'),
            destination + '/manifest.json',
            '--immutable',
            '--size-only',
        ],
        config,
    )
    cryptcheck(directory, destination, 3, config)
    return manifest


def upload_fresh(roots, remote, state_dir, *, config=None, max_age=26 * 3600, set_dir=None):
    """Daily selector includes queued verified release/manual sets without new backups."""
    with locked(Path(state_dir) / 'offsite.lock'):
        try:
            candidates = (
                [Path(set_dir)]
                if set_dir
                else [path.parent for root in roots for path in Path(root).glob('**/sets/*/manifest.json')]
            )
            fresh = []
            for directory in candidates:
                manifest = read_json(directory / 'manifest.json')
                age = age_seconds(manifest.get('created_at'))
                if not set_dir and manifest.get('mode') == 'database_only':
                    continue
                if manifest.get('status') == 'Verified' and age is not None and age <= max_age:
                    fresh.append((manifest['created_at'], directory))
            if not fresh:
                raise BackupError('no_fresh_local_backup')
            uploaded = []
            receipts = read_json(Path(state_dir) / 'offsite-receipts.json')
            for _, directory in sorted(fresh):
                manifest = verify_set(directory)
                upload_set(directory, remote, config=config)
                receipts[manifest['id']] = {'uploaded_at': utcnow(), 'created_at': manifest['created_at']}
                from src.ops_state import atomic_json

                atomic_json(Path(state_dir) / 'offsite-receipts.json', receipts)
                uploaded.append(manifest)
                previous = read_json(Path(state_dir) / 'health.json').get('s3', {})
                value = {
                    'status': 'OK',
                    'code': 'verified',
                    'last_attempt_at': utcnow(),
                    'last_uploaded_id': manifest['id'],
                    'last_uploaded_created_at': manifest['created_at'],
                }
                compared_at = datetime.now(timezone.utc)
                previous_age = (
                    age_seconds(previous.get('created_at'), compared_at) if isinstance(previous, dict) else None
                )
                current_age = age_seconds(manifest['created_at'], compared_at)
                if previous_age is None or current_age <= previous_age:
                    value.update(
                        created_at=manifest['created_at'],
                        uploaded_at=receipts[manifest['id']]['uploaded_at'],
                        id=manifest['id'],
                    )
                update_component(state_dir, 's3', value)
            latest = max(uploaded, key=lambda item: item['created_at'])
            return latest
        except Exception as exc:
            code = str(exc) if isinstance(exc, BackupError) else 's3_upload_failed'
            update_component(state_dir, 's3', {'status': 'CRITICAL', 'code': code})
            raise BackupError(code) from exc
