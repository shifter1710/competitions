"""Stateless, authenticated confidential report navigation; never store filter values."""
import base64
import json

from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.settings import settings

MAX_CONTEXT_BYTES = 4096
MAX_CONTEXT_TOKEN = 5600


def _cipher() -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b'competitions/report-calendar-context/v1').derive(
        settings.auth_secret_key.encode()
    )
    return Fernet(base64.urlsafe_b64encode(key))


def _validate(entries) -> list[tuple[str, str]]:
    from src.routes.reports import REPORT_FILTER_KEYS

    allowed = {*REPORT_FILTER_KEYS, 'period', 'date_from', 'date_to', 'sort', 'order'}
    if not isinstance(entries, list) or len(entries) > 128:
        raise ValueError('Некорректный контекст отчёта')
    result = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ValueError('Некорректный контекст отчёта')
        key, value = entry
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or len(key) > 100
            or len(value) > 1024
            or (key not in allowed and not key.startswith('custom__'))
        ):
            raise ValueError('Некорректный контекст отчёта')
        from src.routes.reports import REPORT_MULTI_KEYS

        if key in seen and key not in REPORT_MULTI_KEYS:
            raise ValueError('Некорректный контекст отчёта')
        seen.add(key)
        result.append((key, value))
    return result


def encode_report_context(entries) -> str:
    payload = json.dumps(_validate(list(entries)), ensure_ascii=False, separators=(',', ':')).encode()
    if len(payload) > MAX_CONTEXT_BYTES:
        raise ValueError('Слишком много условий отчёта для ссылки календаря')
    return _cipher().encrypt(payload).decode()


def decode_report_context(token: str) -> list[tuple[str, str]]:
    if not isinstance(token, str) or len(token) > MAX_CONTEXT_TOKEN:
        raise ValueError('Некорректный контекст отчёта')
    try:
        payload = _cipher().decrypt(token.encode())
        if len(payload) > MAX_CONTEXT_BYTES:
            raise ValueError('Некорректный контекст отчёта')
        return _validate(json.loads(payload))
    except (InvalidToken, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError('Ссылка отчёта повреждена или больше недоступна') from exc


def context_args(entries) -> dict[str, list[str]]:
    args = {}
    for key, value in entries:
        args.setdefault(key, []).append(value)
    return args
