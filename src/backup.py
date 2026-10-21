"""Verified immutable backup sets; no retention deletion in BACKUP-02."""
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from src.ops_state import atomic_json
from src.ops_state import locked
from src.ops_state import update_component
from src.ops_state import utcnow

logger = logging.getLogger(__name__)
GTO_CERTIFICATE_NAME = re.compile(r'[0-9a-f]{32}\.(?:pdf|png|jpe?g)')


class BackupError(RuntimeError):
    """Safe machine-readable failure; never includes source data or paths."""


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def check_sqlite(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise BackupError('sqlite_integrity_failed')
    finally:
        connection.close()


def verify_archive(archive_path):
    try:
        with tempfile.TemporaryDirectory() as temporary:
            restored = Path(temporary) / 'verify.sqlite3'
            with gzip.open(archive_path, 'rb') as source, restored.open('wb') as target:
                shutil.copyfileobj(source, target)  # full read also verifies gzip CRC/trailer
            check_sqlite(restored)
    except (OSError, EOFError, sqlite3.Error) as exc:
        raise BackupError('sqlite_integrity_failed') from exc


def verify_zip(path):
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise BackupError('zip_verification_failed')
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise BackupError('zip_verification_failed')
            for item in archive.infolist():
                name = Path(item.filename)
                if name.is_absolute() or '..' in name.parts or (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise BackupError('zip_verification_failed')
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise BackupError('zip_verification_failed') from exc


def inventory(root):
    result = {}
    if root is None or not root.exists():
        return result
    if root.is_symlink() or not root.is_dir():
        raise BackupError('unsafe_file')
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise BackupError('unsafe_file')
        if path.is_file():
            before = path.stat()
            digest = sha256(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise BackupError('source_changed')
            result[path.relative_to(root).as_posix()] = [after.st_size, after.st_mtime_ns, digest]
    return result


def reference_names(database):  # noqa: C901
    # Explicitly enumerated schema references; no dynamic SQL identifiers.
    connection = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = set()
        if 'attachments' in tables:
            for record, name in connection.execute('SELECT record_id, stored_name FROM attachments'):
                result.add(f'{record}/{name}')
        if 'calendar_event_documents' in tables:
            for event, name in connection.execute(
                'SELECT calendar_event_id, stored_name FROM calendar_event_documents'
            ):
                result.add(f'calendar/{event}/{name}')
        if 'calendar_events' in tables:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(calendar_events)')}
            if 'regulation_stored_name' in columns:
                for event, name in connection.execute(
                    'SELECT id, regulation_stored_name FROM calendar_events '
                    "WHERE regulation_stored_name IS NOT NULL AND regulation_stored_name != ''"
                ):
                    result.add(f'calendar/{event}/{name}')
        if 'student_gto_certificates' in tables:
            for record, name in connection.execute('SELECT gto_record_id, stored_name FROM student_gto_certificates'):
                if (
                    not isinstance(record, int)
                    or record <= 0
                    or not isinstance(name, str)
                    or not GTO_CERTIFICATE_NAME.fullmatch(name)
                ):
                    raise BackupError('unsafe_reference')
                result.add(f'gto/{record}/{name}')
        for name in result:
            if Path(name).is_absolute() or '..' in Path(name).parts:
                raise BackupError('unsafe_reference')
        return result
    finally:
        connection.close()


def backup_database(db_path, output_dir, keep=14):
    """Compatibility helper, keep accepted but never deletes existing copies."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:12]
    temporary = output_dir / f'competitions-{timestamp}.sqlite3'
    archive = output_dir / f'competitions-{timestamp}.sqlite3.gz'
    source = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
    destination = sqlite3.connect(temporary)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    try:
        with temporary.open('rb') as stream, gzip.open(archive, 'wb') as target:
            shutil.copyfileobj(stream, target)
        os.chmod(archive, 0o600)
        verify_archive(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive, timestamp


def backup_files(files_dir, output_dir, timestamp, keep=14):
    archive_path = Path(output_dir) / f'competitions-{timestamp}-files.zip'
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in inventory(Path(files_dir)):
            archive.write(Path(files_dir) / name, name)
    os.chmod(archive_path, 0o600)
    verify_zip(archive_path)
    return archive_path


def verify_set(directory, *, require_complete=True):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('status') != 'Verified' or manifest.get('schema') != 1:
        raise BackupError('incomplete_set')
    artifacts = manifest.get('artifacts', {})
    database_only = manifest.get('mode') == 'database_only'
    expected = {'database.sqlite3.gz'} if database_only else {'database.sqlite3.gz', 'files.zip'}
    if set(artifacts) != expected or (database_only and require_complete):
        raise BackupError('incomplete_set')
    for name, metadata in artifacts.items():
        path = directory / name
        if path.is_symlink() or path.stat().st_size != metadata['size'] or sha256(path) != metadata['sha256']:
            raise BackupError('artifact_integrity_failed')
    verify_archive(directory / 'database.sqlite3.gz')
    if database_only:
        return manifest
    verify_zip(directory / 'files.zip')
    with tempfile.TemporaryDirectory() as temporary:
        db = Path(temporary) / 'database.sqlite3'
        with gzip.open(directory / 'database.sqlite3.gz', 'rb') as stream, db.open('wb') as target:
            shutil.copyfileobj(stream, target)
        with zipfile.ZipFile(directory / 'files.zip') as archive:
            if not reference_names(db).issubset(set(archive.namelist())):
                raise BackupError('referenced_file_missing')
    return manifest


def run_backup(db_path, output_dir, keep=14, files_dir=None, *, source='manual', state_dir=None):  # noqa: C901
    """Create, verify and atomically publish a complete set. No old copy is removed.

    data_version and full inventory bracket the operation; bounded retry detects
    concurrent changes. This is an optimistic check, not an atomic FS/DB snapshot.
    """
    if source not in {'daily', 'manual', 'release'}:
        raise ValueError('invalid backup source')
    db_path, output_dir = Path(db_path), Path(output_dir)
    files_dir = Path(files_dir) if files_dir is not None else None
    state_dir = Path(state_dir) if state_dir else db_path.parent / 'ops'
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    identifier = datetime.utcnow().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex
    manifest = {
        'schema': 1,
        'id': identifier,
        'source': source,
        'created_at': utcnow(),
        'status': 'Started',
        'mode': 'complete' if files_dir is not None else 'database_only',
    }
    with locked(db_path.parent / '.backup.lock'):
        update_component(state_dir, 'local', {'status': 'WARNING', 'code': 'backup_started'})
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=output_dir) as temporary:
            staging = Path(temporary)
            atomic_json(staging / 'manifest.json', manifest)
            try:
                for attempt in range(3):
                    monitor = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)
                    try:
                        version = monitor.execute('PRAGMA data_version').fetchone()[0]
                        before = inventory(files_dir)
                        archive, timestamp = backup_database(db_path, staging)
                        files = backup_files(files_dir, staging, timestamp) if files_dir is not None else None
                        after = inventory(files_dir)
                        stable = before == after and version == monitor.execute('PRAGMA data_version').fetchone()[0]
                        if not stable:
                            archive.unlink()
                            if files:
                                files.unlink()
                            continue
                        with tempfile.TemporaryDirectory() as check_dir:
                            restored = Path(check_dir) / 'db'
                            with gzip.open(archive, 'rb') as stream, restored.open('wb') as target:
                                shutil.copyfileobj(stream, target)
                            if files_dir is not None and not reference_names(restored).issubset(before):
                                raise BackupError('referenced_file_missing')
                        break
                    except BackupError as exc:
                        if str(exc) != 'source_changed':
                            raise
                        # Only files inside this private, unpublished attempt are discarded.
                        for path in staging.iterdir():
                            if path.name != 'manifest.json':
                                path.unlink()
                        continue
                    finally:
                        monitor.close()
                else:
                    raise BackupError('source_changed')
                archive.rename(staging / 'database.sqlite3.gz')
                if files:
                    files.rename(staging / 'files.zip')
                manifest.update(status='Completed', completed_at=utcnow())
                atomic_json(staging / 'manifest.json', manifest)
                manifest['artifacts'] = {
                    name: {'size': (staging / name).stat().st_size, 'sha256': sha256(staging / name)}
                    for name in (('database.sqlite3.gz', 'files.zip') if files else ('database.sqlite3.gz',))
                }
                manifest.update(status='Verified', verified_at=utcnow())
                atomic_json(staging / 'manifest.json', manifest)
                verify_set(staging, require_complete=False)
                final = output_dir / 'sets' / identifier
                final.parent.mkdir(exist_ok=True, mode=0o700)
                os.rename(staging, final)
                update_component(
                    state_dir,
                    'local',
                    {
                        'status': 'OK' if files else 'WARNING',
                        'code': 'verified' if files else 'database_only',
                        'created_at': manifest['created_at'],
                        'id': identifier,
                        'source': source,
                        'size': sum(item['size'] for item in manifest['artifacts'].values()),
                    },
                )
                return {
                    'archive': final / 'database.sqlite3.gz',
                    'timestamp': identifier,
                    'files_archive': final / 'files.zip' if files else None,
                    'manifest': final / 'manifest.json',
                }
            except Exception as exc:
                code = str(exc) if isinstance(exc, BackupError) else 'backup_failed'
                manifest.update(status='Failed', code=code)
                atomic_json(output_dir / ('failed-' + identifier + '.json'), manifest)
                update_component(state_dir, 'local', {'status': 'CRITICAL', 'code': code})
                raise BackupError(code) from exc


def write_backup_audit_event(
    db_path: Path,
    details: dict,
    *,
    username: str = 'system',
    user_id: int | None = None,
    action: str = 'backup_created',
) -> bool:
    """Вписать audit-событие о бэкапе прямо в живую БД (путь для таймера).

    Отдельное соединение: таймер запускается вне приложения. Сбой записи не
    должен ронять бэкап — возвращаем False и логируем.
    """
    connection = None
    try:
        connection = sqlite3.connect(db_path, timeout=5)
        connection.execute(
            'INSERT INTO audit_log (created_at, user_id, username, action, details) VALUES (?, ?, ?, ?, ?)',
            (
                datetime.utcnow().isoformat(),
                user_id,
                username,
                action,
                json.dumps(details, ensure_ascii=False),
            ),
        )
        connection.commit()
        return True
    except sqlite3.Error:
        logger.warning('Failed to write backup audit event to %s', db_path, exc_info=True)
        return False
    finally:
        if connection is not None:
            connection.close()
