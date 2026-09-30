"""Файловая инфраструктура вложений и положений событий.

Каталоги data/files, сигнатурная валидация типов файлов и подсчёт/удаление
файлов вложений. Перенесено из src/main.py без изменения поведения
(Architecture v1)."""
import shutil
from pathlib import Path
from typing import Sequence

from src.settings import settings


ATTACHMENT_MAX_SIZE = 5 * 1024 * 1024


ATTACHMENT_EXTENSIONS = {
    'pdf': 'application/pdf',
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
}


ATTACHMENT_SIGNATURES = {
    'application/pdf': b'%PDF-',
    'image/png': b'\x89PNG\r\n\x1a\n',
    'image/jpeg': b'\xff\xd8\xff',
}


def attachments_dir() -> Path:
    base = Path(settings.data_folder) / 'files'
    base.mkdir(parents=True, exist_ok=True)
    return base


def calendar_regulation_dir(event_id: int) -> Path:
    """Каталог файла положения события: data/files/calendar/<event_id>
    (общий корень data/files — вложения и положения бэкапятся вместе)."""
    return Path(settings.data_folder) / 'files' / 'calendar' / str(event_id)


def regulation_source_path(event_id: int, stored_name: str) -> Path:
    """Путь к файлу положения события на диске по служебному имени."""
    return calendar_regulation_dir(event_id) / stored_name


def detect_attachment_type(body: bytes, filename: str) -> str | None:
    extension = Path(filename).suffix.lower().lstrip('.')
    expected_type = ATTACHMENT_EXTENSIONS.get(extension)
    if expected_type is None:
        return None
    signature = ATTACHMENT_SIGNATURES[expected_type]
    return expected_type if body.startswith(signature) else None


def backups_dir() -> Path:
    return Path(settings.data_folder) / 'backups'


def files_dir() -> Path:
    return Path(settings.data_folder) / 'files'


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def remove_attachment_files() -> None:
    """Remove uploaded attachment files only (data/files), nothing else inside data/."""
    root = files_dir()
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)


def remove_attachment_record_dirs(record_ids: Sequence[int]) -> None:
    """Удалить каталоги вложений указанных записей (очистка по дате)."""
    root = files_dir()
    for record_id in record_ids:
        shutil.rmtree(root / str(record_id), ignore_errors=True)


def attachment_source_path(attachment: dict) -> Path:
    return files_dir() / str(attachment['record_id']) / attachment['stored_name']


def attachments_files_bytes(attachments: Sequence[dict]) -> int:
    """Сумма размеров файлов вложений на диске (что реально освободится, №18)."""
    return sum(file_size(attachment_source_path(attachment)) for attachment in attachments)
