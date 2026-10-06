import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import timedelta
from typing import Sequence

from sanic import Request

from src.settings import settings
from src.storage.sqlite import SQLiteAdapter
from src.web import forbidden
from src.web import get_auth_user
from src.web import get_current_user_id
from src.web import get_storage
from src.web import unauthorized

logger = logging.getLogger(__name__)

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return f'scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split('$')
        if scheme != 'scrypt':
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
        )
        return secrets.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# --- Роли, сессии, присутствие и аудит (перенесено из src/main.py, Architecture v1). ---

ADMIN_ROLE = 'admin'


EDITOR_ROLE = 'editor'


VIEWER_ROLE = 'viewer'


ATHLETE_ROLE = 'athlete'


MODERATOR_ROLES = {ADMIN_ROLE, EDITOR_ROLE}


WRITE_ROLES = {ADMIN_ROLE, EDITOR_ROLE, ATHLETE_ROLE}


KNOWN_ROLES = {ADMIN_ROLE, EDITOR_ROLE, VIEWER_ROLE, ATHLETE_ROLE}


USER_ROLES: Sequence[str] = (ADMIN_ROLE, EDITOR_ROLE, VIEWER_ROLE, ATHLETE_ROLE)


MIN_PASSWORD_LENGTH = 6


def log_audit_event(
    request: Request,
    action: str,
    details: dict | None = None,
    *,
    user_id: int | None = None,
    username: str | None = None,
) -> None:
    """Записать событие в журнал безопасности.

    Журнал — вспомогательный инструмент: сбой записи не должен ломать
    основной сценарий, поэтому любые ошибки глотаем с логом.
    """
    try:
        user = get_auth_user(request)
        if username is None:
            username = (user or {}).get('username') or ''
        if user_id is None and user:
            record = get_storage(request.app).get_user(user['username'])
            user_id = record['id'] if record else None
        get_storage(request.app).add_audit_event(
            user_id=user_id,
            username=username or '',
            action=action,
            details=json.dumps(details or {}, ensure_ascii=False),
        )
    except Exception:
        logger.exception('Failed to write audit event %s', action)


def create_auth_cookie_value(
    username: str,
    role: str,
    issued_at: int | None = None,
    pwd_ver: int = 0,
) -> str:
    if issued_at is None:
        issued_at = int(time.time())
    payload = f'{username}:{role}:{issued_at}:{pwd_ver}'
    signature = hmac.new(
        settings.auth_secret_key.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    token = f'{payload}:{signature}'
    return base64.urlsafe_b64encode(token.encode()).decode()


def decode_auth_token(token: str) -> dict | None:
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        username, role, issued_at, pwd_ver, signature = decoded.split(':', 4)
        issued_at = int(issued_at)
        pwd_ver = int(pwd_ver)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None

    if role not in KNOWN_ROLES:
        return None
    if int(time.time()) - issued_at > settings.auth_session_ttl_seconds:
        return None

    expected_signature = hmac.new(
        settings.auth_secret_key.encode(),
        f'{username}:{role}:{issued_at}:{pwd_ver}'.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not secrets.compare_digest(signature, expected_signature):
        return None

    return {'username': username, 'role': role, 'pwd_ver': pwd_ver}


def parse_auth_cookie(request: Request) -> dict | None:
    token = request.cookies.get(settings.auth_cookie_name)
    if not token:
        return None

    payload = decode_auth_token(token)
    if payload is None:
        return None

    try:
        storage = get_storage(request.app)
    except RuntimeError:
        storage = None
    if storage is not None:
        user = storage.get_user(payload['username'])
        if user is None or not user.get('active', 1):
            return None
        if int(user.get('pwd_ver', 0)) != payload['pwd_ver']:
            return None
        # id нужен middleware присутствия (№17): last_seen без лишнего запроса.
        return {'username': payload['username'], 'role': payload['role'], 'id': user['id']}

    return {'username': payload['username'], 'role': payload['role']}


def authenticate_user(request: Request, username: str, password: str) -> dict | None:
    user = get_storage(request.app).get_user(username)
    if user is None or not user['active']:
        return None
    if not verify_password(password, user['password_hash']):
        return None
    return {
        'id': user['id'],
        'username': user['username'],
        'role': user['role'],
        'pwd_ver': int(user.get('pwd_ver', 0)),
    }


def username_error(username: str) -> str | None:
    """Недопустимые символы логина (L8): None — логин корректен.

    Двоеточие ломает разбор подписанной cookie «логин:роль:время:версия»,
    управляющие символы (включая DEL) в логине не нужны вовсе.
    """
    if ':' in username or any(ord(char) < 32 or ord(char) == 127 for char in username):
        return 'Логин не может содержать двоеточие и управляющие символы'
    return None


def request_is_secure(request: Request) -> bool:
    forwarded_proto = request.headers.get('x-forwarded-proto')
    if forwarded_proto:
        return forwarded_proto == 'https'
    return request.scheme == 'https'


def set_auth_cookie(request: Request, response, username: str, role: str, pwd_ver: int = 0):
    response.add_cookie(
        settings.auth_cookie_name,
        create_auth_cookie_value(username, role, pwd_ver=pwd_ver),
        httponly=True,
        samesite='Lax',
        secure=request_is_secure(request),
        path='/',
        max_age=settings.auth_session_ttl_seconds,
    )


def clear_auth_cookie(response):
    response.delete_cookie(settings.auth_cookie_name, path='/')


def user_is_admin(request: Request) -> bool:
    user = get_auth_user(request)
    return bool(user and user['role'] == ADMIN_ROLE)


def user_can_write(request: Request) -> bool:
    user = get_auth_user(request)
    return bool(user and user['role'] in WRITE_ROLES)


def user_is_moderator(request: Request) -> bool:
    user = get_auth_user(request)
    return bool(user and user['role'] in MODERATOR_ROLES)


def user_is_athlete(request: Request) -> bool:
    user = get_auth_user(request)
    return bool(user and user['role'] == ATHLETE_ROLE)


def student_hashes_for_request(request: Request) -> list[str]:
    if not user_is_athlete(request):
        return []
    user_id = get_current_user_id(request)
    if user_id is None:
        return []
    storage = get_storage(request.app)
    names = storage.get_name_aliases(user_id)
    profile_name = storage.get_profile(user_id).get('student_name', '').strip()
    if profile_name and profile_name not in names:
        names = names + [profile_name]
    return [hashlib.sha256(name.strip().encode()).hexdigest() for name in names if name.strip()]


def athlete_identity_context(request: Request) -> dict | None:
    """P3 Runtime Identity: контекст идентичности атлета для запроса.

    Один источник scope кабинета: id аккаунта (owner), стабильная связь с
    карточкой студента (student_ref_id из users) и легаси-хеши ФИО. None —
    не атлет: ограничение видимости реестра не применяется."""
    if not user_is_athlete(request):
        return None
    user = get_auth_user(request)
    record = get_storage(request.app).get_user(user['username']) if user else None
    return {
        'user_id': record['id'] if record else None,
        'student_ref_id': record.get('student_ref_id') if record else None,
        'hashes': student_hashes_for_request(request),
    }


def user_owns_record(request: Request, review: dict) -> bool:
    if not user_is_athlete(request):
        return True
    if review.get('owner_id') == get_current_user_id(request):
        return True
    # P3 dual-read: стабильная связь с карточкой тоже даёт право на запись
    # (правка, вложения) — в обоих режимах.
    storage = get_storage(request.app)
    user = get_auth_user(request)
    record = storage.get_user(user['username']) if user else None
    user_ref = record.get('student_ref_id') if record else None
    if user_ref is not None and review.get('student_ref_id') == user_ref:
        return True
    # Легаси-ветка права по хешу ФИО — ТОЛЬКО в dual (QA D2). В ref право
    # на запись = owner OR ref: разрешённый flip (H=0) означает, что хеш-
    # совпадения уже покрыты owner/ref, так что хеш-ветка не даёт ничего
    # легитимного — она лишь открывала бы тёзкам правку чужих записей и
    # доступ к чужим вложениям на post-flip данных.
    if storage.get_identity_mode() == 'ref':
        return False
    return review.get('student_id') in student_hashes_for_request(request)


def require_admin(request: Request):
    user = get_auth_user(request)
    if not user:
        return unauthorized(request)
    if user['role'] != ADMIN_ROLE:
        return forbidden(request)
    return None


def require_staff(request: Request):
    """Любой штатный пользователь: admin / editor / viewer, НЕ атлет.

    Точечное расширение доступа (карточка/список студентов с ГТО): атлету
    — прежний запрет (forbidden), неаутентифицированному — прежний 401
    (глобальный контракт). Новые роли не вводятся."""
    user = get_auth_user(request)
    if not user:
        return unauthorized(request)
    if user['role'] == ATHLETE_ROLE:
        return forbidden(request)
    return None


def require_moderator(request: Request):
    user = get_auth_user(request)
    if not user:
        return unauthorized(request)
    if user['role'] not in MODERATOR_ROLES:
        return forbidden(request)
    return None


def require_writer(request: Request):
    user = get_auth_user(request)
    if not user:
        return unauthorized(request)
    if user['role'] not in WRITE_ROLES:
        return forbidden(request)
    return None


# Присутствие пользователей (№17): last_seen обновляется не чаще раза в
# PRESENCE_THROTTLE_SECONDS на пользователя. Кэш в памяти экономит сам вызов
# storage; окончательный троттлинг — в UPDATE (storage.touch_user_seen),
# поэтому несколько воркеров не amplify-ят запись.


PRESENCE_THROTTLE_SECONDS = 60


PRESENCE_ONLINE_WINDOW = timedelta(minutes=5)


_last_seen_touch: dict[int, float] = {}


def reset_presence_tracking() -> None:
    """Сбросить кэш троттлинга (для тестов)."""
    _last_seen_touch.clear()


def touch_user_seen(request: Request) -> None:
    """Обновить last_seen аутентифицированного пользователя (№17).

    Активность — сам факт запроса с валидной сессией; сбой обновления не
    должен ломать запрос, поэтому ошибки глотаем с логом.
    """
    try:
        user = get_auth_user(request) or {}
        user_id = user.get('id')
        if not user_id:
            return
        now = time.monotonic()
        if now - _last_seen_touch.get(user_id, 0.0) < PRESENCE_THROTTLE_SECONDS:
            return
        get_storage(request.app).touch_user_seen(user_id)
        _last_seen_touch[user_id] = now
    except Exception:
        logger.exception('Failed to update last_seen_at')


def get_athlete_profile_defaults(request: Request, storage: SQLiteAdapter) -> dict:
    if not user_is_athlete(request):
        return {}
    user_id = get_current_user_id(request)
    if user_id is None:
        return {}
    return storage.get_profile(user_id)
