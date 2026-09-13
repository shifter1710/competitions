"""Общая логика бэкапов SQLite (вызывается и веб-интерфейсом, и скриптом).

Ручной бэкап с /admin/maintenance и таймерный scripts/backup_sqlite.py
работают через один и тот же ``run_backup`` — код не дублируется
(решение по замечанию №10 из design/prototype/FEEDBACK.md).
"""
import gzip
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def verify_archive(archive_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        restored_path = Path(tmp_dir) / 'verify.sqlite3'
        with gzip.open(archive_path, 'rb') as source_file, restored_path.open('wb') as target_file:
            shutil.copyfileobj(source_file, target_file)
        connection = sqlite3.connect(restored_path)
        try:
            result = connection.execute('PRAGMA quick_check').fetchone()[0]
        finally:
            connection.close()
    if result != 'ok':
        archive_path.unlink(missing_ok=True)
        raise RuntimeError(f'backup verification failed: {result}')


def backup_database(db_path: Path, output_dir: Path, keep: int = 14) -> tuple[Path, str]:
    """Скопировать БД (read-only соединение), сжать gzip, проверить и обрезать старые.

    Возвращает (путь к архиву, штамп времени имени файла).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)

    timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    temp_path = output_dir / f'competitions-{timestamp}.sqlite3'
    archive_path = output_dir / f'competitions-{timestamp}.sqlite3.gz'

    source = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    destination = sqlite3.connect(temp_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    try:
        with temp_path.open('rb') as source_file, gzip.open(archive_path, 'wb') as gzip_file:
            shutil.copyfileobj(source_file, gzip_file)
        os.chmod(archive_path, 0o600)
        verify_archive(archive_path)
    finally:
        temp_path.unlink(missing_ok=True)

    backups = sorted(output_dir.glob('competitions-*.sqlite3.gz'), reverse=True)
    for backup in backups[keep:]:
        backup.unlink()

    return archive_path, timestamp


def backup_files(files_dir: Path, output_dir: Path, timestamp: str, keep: int = 14) -> Path | None:
    """Заархивировать вложения (data/files) рядом с бэкапом БД; каталога нет — None."""
    if not files_dir.is_dir():
        return None
    archive_path = output_dir / f'competitions-{timestamp}-files.zip'
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(files_dir.rglob('*')):
            if file_path.is_file():
                archive.write(file_path, file_path.relative_to(files_dir))
    os.chmod(archive_path, 0o600)
    file_backups = sorted(output_dir.glob('competitions-*-files.zip'), reverse=True)
    for backup in file_backups[keep:]:
        backup.unlink()
    return archive_path


def run_backup(db_path: Path, output_dir: Path, keep: int = 14, files_dir: Path | None = None) -> dict:
    """Полный цикл бэкапа: БД + (опционально) вложения.

    Возвращает метаданные для аудита/интерфейса:
    ``{'archive': Path, 'timestamp': str, 'files_archive': Path | None}``.
    """
    archive, timestamp = backup_database(db_path, output_dir, keep=keep)
    files_archive = None
    if files_dir is not None:
        files_archive = backup_files(files_dir, output_dir, timestamp, keep=keep)
    return {'archive': archive, 'timestamp': timestamp, 'files_archive': files_archive}


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
