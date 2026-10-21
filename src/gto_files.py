"""Award files under the existing backed-up files tree, accessed via dirfds.

Every directory component and final file is opened without following symlinks.
Metadata never contains a client-controlled path. Partial writes are removed.
"""
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from src.files import ATTACHMENT_MAX_SIZE
from src.files import detect_attachment_type
from src.settings import settings

STORED_NAME = re.compile(r'^[0-9a-f]{32}\.(pdf|png|jpe?g)$')


@contextmanager
def award_directory(record_id, *, create=False):
    if not isinstance(record_id, int) or record_id <= 0:
        raise ValueError('Invalid award ID')
    target = Path(os.path.abspath(settings.data_folder)) / 'files' / 'gto' / str(record_id)
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in target.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def checked_name(name):
    if not STORED_NAME.fullmatch(name or ''):
        raise ValueError('Invalid certificate storage name')
    return name


def upload_fields(upload):
    if upload is None or not upload.body:
        raise ValueError('Выберите сертификат.')
    if len(upload.body) > ATTACHMENT_MAX_SIZE:
        raise ValueError('Размер сертификата не должен превышать 5 МБ.')
    filename = (upload.name or 'certificate').replace('\\', '/').rsplit('/', 1)[-1]
    filename = ''.join(c for c in filename if ord(c) >= 32 and ord(c) != 127).strip()
    mime = detect_attachment_type(upload.body, filename)
    declared = (upload.type or '').split(';', 1)[0].strip().lower()
    if not mime or declared not in ('', 'application/octet-stream', mime):
        raise ValueError('Допустимы PDF, PNG и JPEG с соответствующим содержимым и типом файла.')
    return filename, mime


def store_certificate(record_id, upload):
    filename, mime = upload_fields(upload)
    name = uuid4().hex + Path(filename).suffix.lower()
    with award_directory(record_id, create=True) as directory:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, 'wb') as target:
                target.write(upload.body)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            os.unlink(name, dir_fd=directory)
            raise
    return dict(filename=filename, stored_name=name, content_type=mime, size=len(upload.body))


def remove_certificate(record_id, stored_name):
    name = checked_name(stored_name)
    try:
        with award_directory(record_id) as directory:
            # unlink itself never follows a final symlink; parents are protected.
            os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass


def identity(info):
    if not stat.S_ISREG(info.st_mode) or info.st_size > ATTACHMENT_MAX_SIZE:
        raise ValueError('Invalid certificate file')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_certificate(record_id, stored_name):
    with award_directory(record_id) as directory:
        descriptor = os.open(checked_name(stored_name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as source:
            before = identity(os.fstat(source.fileno()))
            body = source.read(ATTACHMENT_MAX_SIZE + 1)
            after = identity(os.fstat(source.fileno()))
        if before != after or len(body) > ATTACHMENT_MAX_SIZE:
            raise ValueError('Certificate changed')
        return body, after


def certificate_identity(record_id, stored_name):
    with award_directory(record_id) as directory:
        return identity(os.stat(checked_name(stored_name), dir_fd=directory, follow_symlinks=False))
