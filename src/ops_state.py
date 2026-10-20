"""Small private, atomic operational state store; no application dependency."""
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.state-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def update_component(state_dir, component, value):
    path = Path(state_dir) / 'health.json'
    with locked(Path(state_dir) / 'state.lock'):
        state = read_json(path)
        previous = state.get(component, {})
        state[component] = dict(previous if isinstance(previous, dict) else {}, **value, checked_at=utcnow())
        atomic_json(path, state)
