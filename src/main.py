import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import time
import uuid
import zipfile
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from io import BytesIO
from pathlib import Path
from typing import Iterable
from typing import Sequence
from urllib.parse import urlencode

import pandas as pd
from jinja2 import Environment
from jinja2 import PackageLoader
from jinja2 import select_autoescape
from openpyxl.utils import get_column_letter
from pandas import isna
from sanic import redirect
from sanic import Request
from sanic import Sanic
from sanic import text
from sanic.response import json as json_response
from sanic.response import raw
from sanic_ext import render

from src.auth import hash_password
from src.auth import verify_password
from src.backup import run_backup
from src.models.competition import Competition
from src.models.custom_field import CustomField
from src.settings import settings
from src.storage.sqlite import SQLiteAdapter

jinja_env = Environment(
    loader=PackageLoader('src'),
    autoescape=select_autoescape(),
    enable_async=True,
)

app = Sanic('SIBADI_competitions')

app.config.REQUEST_MAX_SIZE = 10 * 1024 * 1024
app.config.REQUEST_TIMEOUT = 60
app.config.RESPONSE_TIMEOUT = 60

app.static(
    uri='/static',
    file_or_directory='src/static',
    name='static',
    directory_view=False,
)

app.ctx.storage = None

AUTH_ALLOWED_PATHS = {'/healthcheck', '/login'}
ADMIN_ROLE = 'admin'
EDITOR_ROLE = 'editor'
VIEWER_ROLE = 'viewer'
ATHLETE_ROLE = 'athlete'
MODERATOR_ROLES = {ADMIN_ROLE, EDITOR_ROLE}
WRITE_ROLES = {ADMIN_ROLE, EDITOR_ROLE, ATHLETE_ROLE}
KNOWN_ROLES = {ADMIN_ROLE, EDITOR_ROLE, VIEWER_ROLE, ATHLETE_ROLE}

# Очистка базы — явное админское действие в два шага (объём + фраза).
# См. docs/data-model-decisions.md «Очистка и выгрузка базы».
# Порядок пунктов — по возрастанию ущерба: вложения → записи → всё.
WIPE_SCOPES = ('attachments', 'records', 'all')
WIPE_MODES = ('scope', 'date')
WIPE_CONFIRM_PHRASE = 'УДАЛИТЬ'

INSECURE_SECRET_VALUES = {'', 'change-me', 'replace-with-random-string'}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 180
LOGIN_LOCKOUT_SECONDS = 180
LOGIN_REJECT_STATUS = 429

login_failures: dict[str, list] = {}

logger = logging.getLogger(__name__)

AUDIT_PAGE_SIZE = 50
PRE_WIPE_ARCHIVES_SHOWN = 20

BASE_FIELD_SPECS: Sequence[dict[str, str]] = (
    {'key': 'student_name', 'label': 'ФИО'},
    {'key': 'student_sex', 'label': 'Пол'},
    {'key': 'institute', 'label': 'Институт'},
    {'key': 'group', 'label': 'Группа'},
    {'key': 'sport', 'label': 'Вид спорта'},
    {'key': 'date', 'label': 'Дата'},
    {'key': 'level', 'label': 'Уровень соревнований'},
    {'key': 'name', 'label': 'Название соревнований'},
    {'key': 'position', 'label': 'Место'},
    {'key': 'course', 'label': 'Курс'},
)

REQUIRED_IMPORT_COLUMNS: Sequence[str] = tuple(field['label'] for field in BASE_FIELD_SPECS)
INDEX_EXPORT_COLUMNS: Sequence[str] = tuple(field['label'] for field in BASE_FIELD_SPECS)

# Расширение отчётов (замечание №19, docs/data-model-decisions.md «Расширение
# отчётов»): срез × метрики × фильтры. Группировка всегда по данным записи.
REPORT_SLICES: Sequence[dict[str, str]] = (
    {'key': 'student', 'label': 'Студент'},
    {'key': 'group', 'label': 'Группа'},
    {'key': 'institute', 'label': 'Институт'},
    {'key': 'course', 'label': 'Курс'},
    {'key': 'sport', 'label': 'Вид спорта'},
    {'key': 'level', 'label': 'Уровень'},
    {'key': 'year', 'label': 'Год'},
)
REPORT_SLICE_KEYS = {item['key'] for item in REPORT_SLICES}
DEFAULT_REPORT_SLICE = 'student'
REPORT_METRIC_COLUMNS: Sequence[str] = ('Участий', 'Побед', 'Призовых')
# Колонки среза (без метрик) в HTML-таблице и выгрузке: для «студента» —
# как раньше (ФИО/пол/институт/группа/курс), для остальных — название среза.
REPORT_SLICE_COLUMNS: dict[str, tuple[str, ...]] = {
    'student': ('ФИО', 'Пол', 'Институт', 'Группа', 'Курс'),
    'group': ('Группа',),
    'institute': ('Институт',),
    'course': ('Курс',),
    'sport': ('Вид спорта',),
    'level': ('Уровень соревнований',),
    'year': ('Год',),
}
# Колонки с числовым значением среза (сортировка в HTML-таблице).
REPORT_SLICE_NUMERIC_KEYS = frozenset({'course', 'year'})
# Старые ссылки/закладки с выбором колонок продолжают работать.
LEGACY_REPORT_COLUMN_ALIASES = {'Количество участий': 'Участий'}
FIELD_TYPE_OPTIONS: Sequence[str] = ('text', 'number', 'date', 'url')
USER_ROLES: Sequence[str] = (ADMIN_ROLE, EDITOR_ROLE, VIEWER_ROLE, ATHLETE_ROLE)
MIN_PASSWORD_LENGTH = 6
ATTACHMENT_MAX_SIZE = 5 * 1024 * 1024
ATTACHMENT_EXTENSIONS = {
    'pdf': 'application/pdf',
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
}
PROFILE_FIELDS = ('student_name', 'student_sex', 'institute', 'group', 'course')
ATTACHMENT_SIGNATURES = {
    'application/pdf': b'%PDF-',
    'image/png': b'\x89PNG\r\n\x1a\n',
    'image/jpeg': b'\xff\xd8\xff',
}


DEFAULT_LEVELS = ('внутривузовские', 'межвузовские')

# Управляемые справочники значений: записи остаются свободным текстом,
# справочник — только подсказки (docs/data-model-decisions.md,
# «Справочники значений»). Уровни живут в своей таблице на той же странице.
# Группы — иерархическая категория: существуют только с parent_id на
# институт, поэтому в одиночное создание не входят.
CATALOG_CATEGORIES: Sequence[str] = ('sport', 'institute')
GROUP_CATEGORY = 'group'
CATALOG_MANAGE_CATEGORIES: Sequence[str] = (*CATALOG_CATEGORIES, GROUP_CATEGORY)


def seed_levels(storage: SQLiteAdapter):
    if not storage.get_level_names(include_inactive=True):
        for name in DEFAULT_LEVELS:
            storage.create_level(name)


def seed_users(storage: SQLiteAdapter):
    env_accounts = [
        (settings.auth_admin_username, settings.auth_admin_password, ADMIN_ROLE),
        (settings.auth_editor_username, settings.auth_editor_password, EDITOR_ROLE),
        (settings.auth_viewer_username, settings.auth_viewer_password, VIEWER_ROLE),
    ]
    for username, password, role in env_accounts:
        if username and password and storage.get_user(username) is None:
            storage.create_user(username, hash_password(password), role)


@app.before_server_start
async def init_storage(app: Sanic, _):
    if not Sanic.test_mode and settings.auth_secret_key in INSECURE_SECRET_VALUES:
        raise RuntimeError('AUTH_SECRET_KEY is not configured: set it to a random value in the environment or .env')
    if app.ctx.storage is None:
        app.ctx.storage = SQLiteAdapter(settings.database_path)
    seed_levels(app.ctx.storage)
    seed_users(app.ctx.storage)


def get_storage(app: Sanic) -> SQLiteAdapter:
    storage = getattr(app.ctx, 'storage', None)
    if storage is None:
        raise RuntimeError('Storage adapter is not initialized')
    return storage


def get_param(args: dict, key: str) -> str | None:
    raw_value = args.get(key, [])
    if raw_value:
        return raw_value[0]
    return None


def get_form_value(request: Request, key: str) -> str:
    value = request.form.get(key, '')
    if isinstance(value, list):
        return value[0]
    return value


def get_auth_user(request: Request) -> dict | None:
    return getattr(request.ctx, 'auth_user', None)


def get_current_user_id(request: Request) -> int | None:
    user = get_auth_user(request)
    if not user:
        return None
    try:
        record = get_storage(request.app).get_user(user['username'])
    except RuntimeError:
        return None
    return record['id'] if record else None


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


def register_login_failure(ip: str):
    now = time.monotonic()
    attempts = [stamp for stamp in login_failures.get(ip, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    attempts.append(now)
    login_failures[ip] = attempts
    return len(attempts)


def login_is_locked(ip: str) -> bool:
    now = time.monotonic()
    attempts = [stamp for stamp in login_failures.get(ip, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    login_failures[ip] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS


def clear_login_failures(ip: str):
    login_failures.pop(ip, None)


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


def create_csrf_token(request: Request) -> str:
    auth_cookie = request.cookies.get(settings.auth_cookie_name, '')
    return hmac.new(
        settings.auth_secret_key.encode(),
        b'csrf:' + auth_cookie.encode(),
        hashlib.sha256,
    ).hexdigest()


def request_csrf_is_valid(request: Request) -> bool:
    form_token = get_form_value(request, 'csrf_token')
    if not form_token:
        return False
    return secrets.compare_digest(form_token, create_csrf_token(request))


jinja_env.globals['csrf_token'] = create_csrf_token


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


def user_owns_record(request: Request, review: dict) -> bool:
    if not user_is_athlete(request):
        return True
    if review.get('owner_id') == get_current_user_id(request):
        return True
    return review.get('student_id') in student_hashes_for_request(request)


def require_admin(request: Request):
    user = get_auth_user(request)
    if not user:
        return text(body='Unauthorized', status=401)
    if user['role'] != ADMIN_ROLE:
        return text(body='Forbidden', status=403)
    return None


def require_moderator(request: Request):
    user = get_auth_user(request)
    if not user:
        return text(body='Unauthorized', status=401)
    if user['role'] not in MODERATOR_ROLES:
        return text(body='Forbidden', status=403)
    return None


def require_writer(request: Request):
    user = get_auth_user(request)
    if not user:
        return text(body='Unauthorized', status=401)
    if user['role'] not in WRITE_ROLES:
        return text(body='Forbidden', status=403)
    return None


def build_redirect_with_message(*, message: str | None = None, error: str | None = None, url: str = '/'):
    params = {}
    if message:
        params['admin_message'] = message
    if error:
        params['admin_error'] = error
    query = urlencode(params)
    return redirect(f'{url}?{query}' if query else url)


def get_flash_args(request: Request) -> dict[str, str | None]:
    args = dict(request.args)
    return {
        'admin_message': get_param(args, 'admin_message'),
        'admin_error': get_param(args, 'admin_error'),
    }


def validate_import_columns(df: pd.DataFrame):
    missing_columns = [column for column in REQUIRED_IMPORT_COLUMNS if column not in df.columns]
    if missing_columns:
        raise ValueError(f'Missing required columns: {", ".join(missing_columns)}')


def normalize_position(value) -> int:
    if isna(value) or value == '':
        return 0
    return int(value)


def normalize_course(value) -> int:
    if isna(value):
        raise ValueError('Курс обязателен')
    return int(value)


def parse_manual_date(value: str) -> datetime:
    if not value:
        raise ValueError('Дата обязательна')
    return datetime.strptime(value, settings.date_format)


def parse_import_date(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.strptime(value.strip(), settings.date_format)
        except ValueError:
            pass
    return value


def normalize_custom_field_key(label: str) -> str:
    normalized = re.sub(r'\W+', '_', label.strip().lower())
    normalized = normalized.strip('_')
    if not normalized:
        normalized = 'field'
    return normalized


def make_unique_custom_field_key(label: str, existing_fields: Sequence[CustomField]) -> str:
    base_key = normalize_custom_field_key(label)
    existing_keys = {field.key for field in existing_fields}
    candidate = base_key
    index = 2
    while candidate in existing_keys:
        candidate = f'{base_key}_{index}'
        index += 1
    return candidate


def checkbox_to_bool(value: str) -> bool:
    return value.lower() in {'1', 'true', 'on', 'yes'}


def parse_checkbox(request: Request, key: str) -> bool:
    return checkbox_to_bool(get_form_value(request, key))


def parse_custom_field_value(raw_value, field: CustomField) -> str:
    if isna(raw_value):
        raw_value = ''

    value = str(raw_value).strip()
    if not value:
        if field.required:
            raise ValueError(f'Поле "{field.label}" обязательно')
        return ''

    if field.field_type == 'number':
        return str(int(float(value)))
    if field.field_type == 'date':
        return datetime.strptime(value, settings.date_format).strftime(settings.date_format)
    if field.field_type == 'url':
        if not re.fullmatch(r'https?://\S+', value):
            raise ValueError(f'Поле "{field.label}" должно быть ссылкой (http:// или https://)')
        return value
    return value


def extract_custom_field_values(record: dict, custom_fields: Sequence[CustomField]) -> dict[str, str]:
    extra_data = {}
    for field in custom_fields:
        raw_value = record.get(field.label, '')
        value = parse_custom_field_value(raw_value, field)
        extra_data[field.key] = value
    return extra_data


def extract_custom_field_values_from_request(
    request: Request,
    custom_fields: Sequence[CustomField],
) -> dict[str, str]:
    extra_data = {}
    for field in custom_fields:
        extra_data[field.key] = parse_custom_field_value(
            get_form_value(request, f'custom__{field.key}'),
            field,
        )
    return extra_data


def build_competition(
    record: dict,
    *,
    custom_fields: Sequence[CustomField],
    manual_input: bool = False,
) -> Competition:
    student_name = str(record['ФИО']).strip()
    if not student_name:
        raise ValueError('ФИО обязательно')

    date = record['Дата']
    if manual_input:
        date = parse_manual_date(str(date).strip())
    else:
        date = parse_import_date(date)

    return Competition(
        student_id=hashlib.sha256(student_name.encode()).hexdigest(),
        student_name=student_name,
        student_sex=str(record['Пол']).strip(),
        institute=str(record['Институт']).strip(),
        group=str(record['Группа']).strip(),
        date=date,
        sport=str(record['Вид спорта']).strip(),
        level=str(record['Уровень соревнований']).strip().lower(),
        name=str(record['Название соревнований']).strip(),
        position=normalize_position(record['Место']),
        course=normalize_course(record['Курс']),
        extra_data=extract_custom_field_values(record, custom_fields),
    )


def competition_to_export_row(
    competition: Competition,
    export_custom_fields: Sequence[CustomField],
) -> dict[str, str | int]:
    row = competition.model_dump(by_alias=True)
    row.pop('_id', None)
    row.pop('Время создания записи (UTC)', None)
    row.pop('extra_data', None)
    row.pop('Статус проверки', None)
    row.pop('Комментарий проверки', None)
    for field in export_custom_fields:
        row[field.label] = competition.extra_data.get(field.key, '')
    return row


def report_export_columns(slice_key: str = DEFAULT_REPORT_SLICE) -> list[str]:
    """Колонки выгрузки для среза: название среза + Участий/Побед/Призовых."""
    return [*REPORT_SLICE_COLUMNS[slice_key], *REPORT_METRIC_COLUMNS]


def parse_report_slice(args: dict) -> str:
    """Срез отчёта из GET-параметра group_by; отсутствие — «студент»."""
    value = get_param(args, 'group_by')
    return value if value else DEFAULT_REPORT_SLICE


def validate_report_filters(args: dict) -> str | None:
    for key in ('date_from', 'date_to'):
        value = get_param(args, key)
        if value:
            try:
                datetime.strptime(value, settings.date_format)
            except ValueError:
                return f'Некорректная дата в фильтре ({key}): {value}'

    position = get_param(args, 'position')
    if position and not re.fullmatch(r'[<>]\d+', position):
        return f'Некорректное значение места: {position}'

    group_by = get_param(args, 'group_by')
    if group_by and group_by not in REPORT_SLICE_KEYS:
        return f'Неизвестный срез отчёта: {group_by}'

    return None


def collect_custom_filters(request: Request) -> tuple[list[tuple[str, str, str]], str | None]:
    fields = {field.key: field for field in get_storage(request.app).get_custom_fields()}
    filters: list[tuple[str, str, str]] = []
    for key, raw_values in request.args.items():
        if not key.startswith('custom__'):
            continue
        field = fields.get(key.removeprefix('custom__'))
        value = raw_values[0].strip() if raw_values else ''
        if field is None or not value:
            continue
        if field.field_type == 'number':
            try:
                value = str(int(value))
            except ValueError:
                return [], f'Фильтр «{field.label}» должен быть числом'
        filters.append((field.key, field.field_type, value))
    return filters, None


def get_report_rows(request: Request, slice_key: str):
    """Строки отчёта для среза с фильтрами из запроса.

    Для «студента» — StudentInfo (профиль записи + метрики), для остальных
    срезов — ReportSliceRow (значение поля записи + метрики).
    """
    args = dict(request.args)
    storage = get_storage(request.app)

    filter_kwargs = dict(
        date_from=get_param(args, 'date_from'),
        date_to=get_param(args, 'date_to'),
        position=get_param(args, 'position'),
        level=get_param(args, 'level'),
        name=get_param(args, 'name'),
        institute=get_param(args, 'institute') or '',
        group=get_param(args, 'group') or '',
        sport=get_param(args, 'sport') or '',
        custom_filters=collect_custom_filters(request)[0],
    )

    if slice_key == DEFAULT_REPORT_SLICE:
        return storage.get_filtered(**filter_kwargs)
    return storage.get_grouped_report(slice_key, **filter_kwargs)


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


@app.on_request
async def authorize_request(request: Request):
    request.ctx.auth_user = parse_auth_cookie(request)

    if request.path.startswith('/static'):
        return None

    if request.path in AUTH_ALLOWED_PATHS:
        return None

    if get_auth_user(request):
        touch_user_seen(request)
        if request.method == 'POST' and request.path != '/login':
            if not request_csrf_is_valid(request):
                return text(body='CSRF token missing or invalid', status=403)
        return None

    if request.method == 'GET':
        return redirect('/login')

    return text(body='Unauthorized', status=401)


@app.get('/login')
async def login_page(request: Request):
    if get_auth_user(request):
        return redirect('/')

    return await render(
        template_name=jinja_env.get_template('login.html'),
        context={
            'request': request,
            'error_message': request.args.get('error'),
        },
    )


@app.post('/login')
async def login(request: Request):
    if login_is_locked(request.ip):
        return text(body='Слишком много неудачных попыток входа. Повторите позже', status=LOGIN_REJECT_STATUS)

    username = str(get_form_value(request, 'username')).strip()
    password = str(get_form_value(request, 'password'))

    user = authenticate_user(request, username, password)
    if user is None:
        register_login_failure(request.ip)
        log_audit_event(request, 'login_failed', username=username)
        response = await render(
            template_name=jinja_env.get_template('login.html'),
            context={
                'request': request,
                'error_message': 'Неверный логин или пароль',
            },
        )
        response.status = 401
        return response

    clear_login_failures(request.ip)
    log_audit_event(
        request,
        'login_success',
        user_id=user.get('id'),
        username=user['username'],
    )
    try:
        # Присутствие (№17): успешный вход фиксирует last_login_at
        # (и last_seen — вход тоже активность).
        get_storage(request.app).set_user_last_login(user['id'])
    except Exception:
        logger.exception('Failed to update last_login_at')
    response = redirect('/')
    set_auth_cookie(request, response, user['username'], user['role'], pwd_ver=user.get('pwd_ver', 0))
    return response


@app.post('/logout')
async def logout(request: Request):
    response = redirect('/login')
    clear_auth_cookie(response)
    return response


@app.get('/')
async def index(request: Request):
    storage = get_storage(request.app)
    custom_fields = storage.get_custom_fields()
    owner_filter = get_current_user_id(request) if user_is_athlete(request) else None
    profile_hashes = student_hashes_for_request(request)
    competitions = storage.get_competitions(owner_id=owner_filter, student_id_hashes=profile_hashes)
    return await render(
        template_name=jinja_env.get_template('index.html'),
        context={
            'request': request,
            'competitions': competitions,
            'custom_fields': custom_fields,
            'admin_custom_fields': storage.get_custom_fields(include_inactive=True),
            'field_type_options': FIELD_TYPE_OPTIONS,
            'can_write': user_can_write(request),
            'can_import': user_is_moderator(request),
            'is_moderator': user_is_moderator(request),
            'is_athlete': user_is_athlete(request),
            'is_admin': user_is_admin(request),
            'users': storage.list_users() if user_is_admin(request) else [],
            'user_roles': USER_ROLES,
            'levels': storage.get_level_names(),
            'sport_options': storage.list_catalog('sport'),
            'institute_options': storage.list_catalog('institute'),
            'groups_by_institute': storage.get_group_options_by_institute(),
            'has_unapproved': any(c.review_status != 'approved' for c in competitions),
            'attachments_by_record': build_attachments_by_record(storage.get_attachments()),
            'admin_levels': storage.list_levels() if user_is_admin(request) else [],
            'current_username': (get_auth_user(request) or {}).get('username'),
            'admin_message': get_param(dict(request.args), 'admin_message'),
            'admin_error': get_param(dict(request.args), 'admin_error'),
        },
    )


@app.get('/reports')
async def reports_page(request: Request):
    if user_is_athlete(request):
        return text(body='Forbidden', status=403)
    storage = get_storage(request.app)
    groups_by_institute = storage.get_group_options_by_institute()
    return await render(
        template_name=jinja_env.get_template('report.html'),
        context={
            'request': request,
            'custom_fields': storage.get_custom_fields(),
            'levels': storage.get_level_names(),
            'report_export_columns': report_export_columns(),
            'report_slice_options': REPORT_SLICES,
            'report_export_columns_by_slice': {
                slice_key: report_export_columns(slice_key) for slice_key in REPORT_SLICE_KEYS
            },
            'institute_options': storage.list_catalog('institute'),
            'sport_options': storage.list_catalog('sport'),
            'groups_by_institute': groups_by_institute,
            'group_options': sorted({group for groups in groups_by_institute.values() for group in groups}),
        },
    )


@app.get('/admin')
async def admin_page(request: Request):
    auth_error = require_moderator(request)
    if auth_error is not None:
        return auth_error
    return await render(
        template_name=jinja_env.get_template('admin.html'),
        context={
            'request': request,
            'can_import': user_is_moderator(request),
            'is_admin': user_is_admin(request),
            **get_flash_args(request),
        },
    )


@app.get('/admin/import')
async def admin_import_page(request: Request):
    auth_error = require_moderator(request)
    if auth_error is not None:
        return auth_error
    return await render(
        template_name=jinja_env.get_template('admin_import.html'),
        context={
            'request': request,
            **get_flash_args(request),
        },
    )


@app.get('/admin/fields')
async def admin_fields_page(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error
    storage = get_storage(request.app)
    return await render(
        template_name=jinja_env.get_template('admin_fields.html'),
        context={
            'request': request,
            'admin_custom_fields': storage.get_custom_fields(include_inactive=True),
            'field_type_options': FIELD_TYPE_OPTIONS,
            **get_flash_args(request),
        },
    )


@app.get('/admin/levels')
async def admin_levels_page(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error
    # Уровни переехали на страницу «Справочники»; старый URL остаётся
    # рабочим, flash-параметры сохраняются в редиректе.
    query = request.query_string
    return redirect(f'/admin/catalogs?{query}' if query else '/admin/catalogs')


def build_catalog_entries(storage: SQLiteAdapter, category: str) -> list[dict]:
    """Значения справочника для страницы админки: каждое со счётчиком записей."""
    return [
        {**row, 'records_count': storage.count_records_using(category, row['value'])}
        for row in storage.list_catalog_all(category)
    ]


@app.get('/admin/catalogs')
async def admin_catalogs_page(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error
    storage = get_storage(request.app)
    return await render(
        template_name=jinja_env.get_template('admin_catalogs.html'),
        context={
            'request': request,
            'sport_values': build_catalog_entries(storage, 'sport'),
            'institute_tree': storage.list_catalog_tree(),
            'admin_levels': [
                {**level, 'records_count': storage.count_records_using('level', level['name'])}
                for level in storage.list_levels()
            ],
            **get_flash_args(request),
        },
    )


def resolve_catalog_value(request: Request, category: str, value_id: str):
    """Общая проверка для действий над значением справочника: категория и id."""
    if category not in CATALOG_MANAGE_CATEGORIES:
        return None, text(body='Unknown catalog', status=404)
    try:
        numeric_value_id = int(value_id)
    except ValueError:
        return None, text(body='Invalid value id', status=400)
    row = get_storage(request.app).get_catalog_value(numeric_value_id)
    if row is None or row['category'] != category:
        return None, build_redirect_with_message(error='Значение не найдено', url='/admin/catalogs')
    return row, None


@app.post('/admin/catalogs/<category>')
async def create_catalog_value(request: Request, category: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error
    if category not in CATALOG_CATEGORIES:
        return text(body='Unknown catalog', status=404)

    value = get_form_value(request, 'value').strip()
    if not value:
        return build_redirect_with_message(error='Значение обязательно', url='/admin/catalogs')

    get_storage(request.app).add_catalog_value(category, value)
    return build_redirect_with_message(message='Значение добавлено', url='/admin/catalogs')


@app.post('/admin/catalogs/<category>/<value_id>/hide')
async def hide_catalog_value(request: Request, category: str, value_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error
    row, error = resolve_catalog_value(request, category, value_id)
    if error is not None:
        return error

    get_storage(request.app).hide_catalog_value(row['id'])
    return build_redirect_with_message(message='Значение скрыто из подсказок', url='/admin/catalogs')


@app.post('/admin/catalogs/<category>/<value_id>/unhide')
async def unhide_catalog_value(request: Request, category: str, value_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error
    row, error = resolve_catalog_value(request, category, value_id)
    if error is not None:
        return error

    get_storage(request.app).unhide_catalog_value(row['id'])
    return build_redirect_with_message(message='Значение снова видно в подсказках', url='/admin/catalogs')


@app.post('/admin/catalogs/<category>/<value_id>/delete')
async def delete_catalog_value(request: Request, category: str, value_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error
    row, error = resolve_catalog_value(request, category, value_id)
    if error is not None:
        return error

    storage = get_storage(request.app)
    parent_value = None
    if category == GROUP_CATEGORY and row.get('parent_id'):
        parent = storage.get_catalog_value(row['parent_id'])
        parent_value = parent['value'] if parent else None
    records_count = storage.count_records_using(category, row['value'], parent_value=parent_value)
    if records_count > 0:
        # Значение висит на записях — историчность неприкосновенна, удалять нельзя.
        return text(
            body=f'Нельзя удалить значение «{row["value"]}»: на нём записей — {records_count}',
            status=400,
        )

    if category == 'institute':
        child_groups = storage.count_child_groups(row['id'])
        if child_groups:
            # Группы без родителя не бывают: не оставляем сирот, удаление
            # института — только после удаления его групп.
            return text(
                body=f'У института «{row["value"]}» есть группы ({child_groups}) — сначала удалите их',
                status=400,
            )

    storage.delete_catalog_value(row['id'])
    return build_redirect_with_message(message='Значение удалено', url='/admin/catalogs')


@app.post('/admin/catalogs/institute/<institute_id>/group')
async def create_catalog_group(request: Request, institute_id: str):
    """Добавить группу в институт (иерархия справочника, №15)."""
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error
    try:
        numeric_institute_id = int(institute_id)
    except ValueError:
        return text(body='Invalid value id', status=400)

    storage = get_storage(request.app)
    institute = storage.get_catalog_value(numeric_institute_id)
    if institute is None or institute['category'] != 'institute':
        return build_redirect_with_message(error='Институт не найден', url='/admin/catalogs')

    value = get_form_value(request, 'value').strip()
    if not value:
        return build_redirect_with_message(error='Название группы обязательно', url='/admin/catalogs')

    storage.add_catalog_value(GROUP_CATEGORY, value, parent_id=institute['id'])
    return build_redirect_with_message(
        message=f'Группа «{value}» добавлена в институт «{institute["value"]}»',
        url='/admin/catalogs',
    )


def ru_plural(number: int, one: str, few: str, many: str) -> str:
    """Русская плюрализация: 1 минуту / 2 минуты / 5 минут."""
    mod_100 = number % 100
    mod_10 = number % 10
    if mod_100 in range(11, 15):
        return many
    if mod_10 == 1:
        return one
    if mod_10 in range(2, 5):
        return few
    return many


def format_login_moment(raw_value) -> str:
    """Последний вход для подсказки: дд.мм.гггг чч:мм или «—»."""
    if not raw_value:
        return '—'
    try:
        moment = datetime.fromisoformat(str(raw_value)).replace(tzinfo=timezone.utc).astimezone()
    except ValueError:
        return '—'
    return moment.strftime('%d.%m.%Y %H:%M')


def describe_presence(last_seen_raw, *, now: datetime | None = None) -> dict:
    """Колонка «Активность» на /admin/users (№17).

    «Онлайн» = last_seen в пределах PRESENCE_ONLINE_WINDOW (сессии
    stateless-cookie, точного списка живых сессий нет — приближение).
    Остальное — человекочитаемое «был(а): …»; храним в UTC, показываем
    локальное время сервера.
    """
    if not last_seen_raw:
        return {'online': False, 'label': '—'}
    try:
        last_seen = datetime.fromisoformat(str(last_seen_raw)).replace(tzinfo=timezone.utc).astimezone()
    except ValueError:
        return {'online': False, 'label': '—'}

    local_now = now or datetime.now(timezone.utc).astimezone()
    delta = max(timedelta(0), local_now - last_seen)
    if delta <= PRESENCE_ONLINE_WINDOW:
        return {'online': True, 'label': 'онлайн'}

    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return {'online': False, 'label': f'{minutes} {ru_plural(minutes, "минуту", "минуты", "минут")} назад'}

    if last_seen.date() == local_now.date():
        return {'online': False, 'label': f'сегодня {last_seen.strftime("%H:%M")}'}
    if last_seen.date() == (local_now - timedelta(days=1)).date():
        return {'online': False, 'label': f'вчера {last_seen.strftime("%H:%M")}'}
    return {'online': False, 'label': last_seen.strftime('%d.%m.%Y %H:%M')}


@app.get('/admin/users')
async def admin_users_page(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error
    storage = get_storage(request.app)
    users = [
        {
            **user,
            'records_count': storage.count_records_by_owner(user['id']),
            'presence': describe_presence(user.get('last_seen_at')),
            'last_login_label': format_login_moment(user.get('last_login_at')),
        }
        for user in storage.list_users()
    ]
    return await render(
        template_name=jinja_env.get_template('admin_users.html'),
        context={
            'request': request,
            'users': users,
            'user_roles': USER_ROLES,
            'current_username': (get_auth_user(request) or {}).get('username'),
            **get_flash_args(request),
        },
    )


@app.get('/admin/students')
async def admin_students_page(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error
    return await render(
        template_name=jinja_env.get_template('admin_students.html'),
        context={
            'request': request,
            **get_flash_args(request),
        },
    )


@app.get('/profile')
async def profile_page(request: Request):
    return await render(
        template_name=jinja_env.get_template('profile.html'),
        context={
            'request': request,
            'is_athlete': user_is_athlete(request),
        },
    )


@app.get('/template/empty.xlsx')
async def export_empty_template(request: Request):
    auth_error = require_moderator(request)
    if auth_error is not None:
        return auth_error

    storage = get_storage(request.app)
    custom_fields = [field for field in storage.get_custom_fields() if field.show_in_template]
    columns = list(REQUIRED_IMPORT_COLUMNS) + [field.label for field in custom_fields]

    df = pd.DataFrame(columns=columns)
    buffer = BytesIO()
    df.to_excel(buffer, index=False)
    apply_template_date_format(buffer, columns)

    now_str = datetime.utcnow().strftime('%d-%m-%Y_%H-%M-%S')
    return raw(
        buffer.getvalue(),
        headers={
            'content-type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'content-disposition': f'attachment; filename="Шаблон_соревнования_{now_str}.xlsx"',
        },
    )


def apply_template_date_format(buffer: BytesIO, columns: Sequence[str]) -> None:
    from openpyxl import load_workbook

    if 'Дата' not in columns:
        buffer.seek(0)
        return
    date_column_letter = get_column_letter(columns.index('Дата') + 1)
    buffer.seek(0)
    workbook = load_workbook(buffer)
    sheet = workbook.worksheets[0]
    for cell in sheet[date_column_letter]:
        cell.number_format = 'DD.MM.YYYY'
    sheet.column_dimensions[date_column_letter].width = 14
    buffer.seek(0)
    workbook.save(buffer)
    buffer.seek(0)


def build_index_dataframe(competitions, export_custom_fields) -> pd.DataFrame:
    df = pd.DataFrame.from_records([competition_to_export_row(comp, export_custom_fields) for comp in competitions])
    df = df.reindex(columns=list(INDEX_EXPORT_COLUMNS) + [field.label for field in export_custom_fields])
    for column in df.columns:
        if df[column].dtype == object:
            df[column] = df[column].map(sanitize_spreadsheet_value)
    return df


@app.get('/export/index')
async def export_index(request: Request):
    if user_is_athlete(request):
        return text(body='Forbidden', status=403)
    storage = get_storage(request.app)
    competitions = storage.get_competitions()
    export_custom_fields = [field for field in storage.get_custom_fields() if field.show_in_export]
    df = await asyncio.to_thread(build_index_dataframe, competitions, export_custom_fields)

    now_str = datetime.utcnow().strftime('%d-%m-%Y_%H-%M-%S')
    filename = f'Отчет_{now_str}.xlsx'
    buffer = BytesIO()
    await asyncio.to_thread(df.to_excel, buffer, index=False)
    buffer.seek(0)

    return raw(
        buffer.getvalue(),
        headers={
            'content-type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'content-disposition': f'attachment; filename="{filename}"',
        },
    )


def build_import_competitions(df: pd.DataFrame, custom_fields) -> list[Competition]:
    competitions = []
    for index, row in df.iterrows():
        record = row.to_dict()
        try:
            competitions.append(build_competition(record, custom_fields=custom_fields))
        except (TypeError, ValueError) as exc:
            raise ValueError(f'строка {index + 2}: {exc}') from exc
    return competitions


def competition_duplicate_key(competition: Competition) -> tuple:
    return (
        competition.student_name,
        competition.date.date().isoformat(),
        competition.sport,
        competition.name,
    )


def ensure_levels(storage: SQLiteAdapter, competitions: Iterable[Competition]):
    known = set(storage.get_level_names(include_inactive=True))
    for competition in competitions:
        if competition.level not in known:
            storage.create_level(competition.level)
            known.add(competition.level)


def ensure_catalog_values(storage: SQLiteAdapter, competitions: Iterable[Competition]):
    """Новое значение вида спорта/института из записи или импорта попадает в справочник.

    Как с уровнями: справочник только собирает подсказки и ничего не
    перезаписывает в записях (docs/data-model-decisions.md). Дубликаты
    игнорируются на стороне storage. Пара институт→группа попадает в иерархию:
    группа кладётся под свой институт (уникальность (parent, value)).
    """
    for competition in competitions:
        for category, value in (('sport', competition.sport), ('institute', competition.institute)):
            if value:
                storage.add_catalog_value(category, value)
        if competition.institute and competition.group:
            storage.ensure_catalog_pair(competition.institute, competition.group)


def split_import_competitions(
    competitions: Sequence[Competition],
    existing_competitions: Iterable[Competition],
) -> tuple[list[Competition], int]:
    seen_keys = {competition_duplicate_key(comp) for comp in existing_competitions}
    new_competitions = []
    skipped_duplicates = 0
    for competition in competitions:
        key = competition_duplicate_key(competition)
        if key in seen_keys:
            skipped_duplicates += 1
            continue
        seen_keys.add(key)
        new_competitions.append(competition)
    return new_competitions, skipped_duplicates


@app.post('/')
async def upload(request: Request):
    auth_error = require_moderator(request)
    if auth_error is not None:
        return auth_error

    upload_file = request.files.get('file')
    if not upload_file or not upload_file.body:
        return text(body='No file uploaded', status=400)

    storage = get_storage(request.app)
    custom_fields = storage.get_custom_fields()

    try:
        df = await asyncio.to_thread(pd.read_excel, io=upload_file.body)
        validate_import_columns(df)
    except ValueError as exc:
        return text(body=str(exc), status=400)

    try:
        competitions = await asyncio.to_thread(build_import_competitions, df, custom_fields)
    except (TypeError, ValueError) as exc:
        return text(body=f'Invalid row data: {exc}', status=400)

    ensure_levels(storage, competitions)
    ensure_catalog_values(storage, competitions)
    existing = await asyncio.to_thread(storage.get_competitions)
    new_competitions, skipped_duplicates = split_import_competitions(competitions, existing)
    if new_competitions:
        storage.save_competitions(new_competitions, owner_id=get_current_user_id(request))

    summary = f'Импортировано записей: {len(new_competitions)}'
    if skipped_duplicates:
        summary += f'. Пропущено дублей: {skipped_duplicates}'
    return text(body=summary)


def get_athlete_profile_defaults(request: Request, storage: SQLiteAdapter) -> dict:
    if not user_is_athlete(request):
        return {}
    user_id = get_current_user_id(request)
    if user_id is None:
        return {}
    return storage.get_profile(user_id)


def read_profile_payload(request: Request) -> tuple[dict, str | None]:
    profile = {}
    for field in PROFILE_FIELDS:
        value = get_form_value(request, field).strip()
        if value:
            profile[field] = value
    if profile.get('student_sex') not in (None, '', 'М', 'Ж'):
        return {}, 'Пол должен быть М или Ж'
    if 'course' in profile:
        try:
            profile['course'] = str(int(profile['course']))
        except ValueError:
            return {}, 'Курс должен быть числом'
    return profile, None


@app.get('/api/profile')
async def get_profile(request: Request):
    if get_auth_user(request) is None:
        return text(body='Unauthorized', status=401)
    user_id = get_current_user_id(request)
    profile = get_storage(request.app).get_profile(user_id) if user_id else {}
    return json_response({'profile': profile})


@app.post('/api/profile')
async def save_profile(request: Request):
    if get_auth_user(request) is None:
        return text(body='Unauthorized', status=401)
    user_id = get_current_user_id(request)
    if user_id is None:
        return text(body='Unknown user', status=400)
    profile, error = read_profile_payload(request)
    if error:
        return text(body=error, status=400)
    storage = get_storage(request.app)
    storage.set_profile(user_id, profile)
    if profile.get('student_name'):
        storage.add_name_alias(user_id, profile['student_name'])
    return json_response({'profile': profile})


@app.get('/api/students')
async def list_students(request: Request):
    # Полный список ФИО раскрывает персональные данные других студентов —
    # доступен только admin/editor (см. docs/data-model-decisions.md).
    auth_error = require_moderator(request)
    if auth_error is not None:
        return auth_error
    names = get_storage(request.app).get_student_names()
    return json_response(names)


@app.get('/api/students/lookup')
async def lookup_student(request: Request):
    # Атлету нельзя отдавать чужие ФИО — только факт точного совпадения.
    user = get_auth_user(request)
    if user is None:
        return text(body='Unauthorized', status=401)
    if user['role'] not in MODERATOR_ROLES and user['role'] != ATHLETE_ROLE:
        return text(body='Forbidden', status=403)

    name = str(request.args.get('name', '')).strip()
    if not name:
        return text(body='Параметр name обязателен', status=400)

    # Тот же хеш, что использует привязка записей к кабинету атлета.
    name_hash = hashlib.sha256(name.encode()).hexdigest()
    records_count = get_storage(request.app).count_records_by_student_hash(name_hash)
    return json_response({'found': records_count > 0})


@app.post('/competition')
async def add_competition(request: Request):
    auth_error = require_writer(request)
    if auth_error is not None:
        return auth_error

    storage = get_storage(request.app)
    custom_fields = storage.get_custom_fields()
    record = {
        'ФИО': get_form_value(request, 'student_name'),
        'Пол': get_form_value(request, 'student_sex'),
        'Институт': get_form_value(request, 'institute'),
        'Группа': get_form_value(request, 'group'),
        'Вид спорта': get_form_value(request, 'sport'),
        'Дата': get_form_value(request, 'date'),
        'Уровень соревнований': get_form_value(request, 'level'),
        'Название соревнований': get_form_value(request, 'name'),
        'Место': get_form_value(request, 'position'),
        'Курс': get_form_value(request, 'course'),
    }
    record.update({field.label: get_form_value(request, f'custom__{field.key}') for field in custom_fields})

    profile_defaults = get_athlete_profile_defaults(request, storage)
    for form_key, profile_key in (
        ('ФИО', 'student_name'),
        ('Пол', 'student_sex'),
        ('Институт', 'institute'),
        ('Группа', 'group'),
        ('Курс', 'course'),
    ):
        if not str(record.get(form_key, '')).strip() and profile_key in profile_defaults:
            record[form_key] = profile_defaults[profile_key]

    try:
        competition = build_competition(record, custom_fields=custom_fields, manual_input=True)
    except (TypeError, ValueError) as exc:
        return text(body=f'Invalid row data: {exc}', status=400)

    ensure_catalog_values(storage, [competition])
    review_status = 'pending' if user_is_athlete(request) else 'approved'
    storage.save_competitions(
        [competition],
        review_status=review_status,
        owner_id=get_current_user_id(request),
    )
    return redirect(to='/')


@app.post('/competition/<record_id>')
async def update_competition(request: Request, record_id: str):
    auth_error = require_writer(request)
    if auth_error is not None:
        return auth_error

    try:
        numeric_id = int(record_id)
    except ValueError:
        return text(body='Invalid record id', status=400)

    storage = get_storage(request.app)
    existing_review = storage.get_competition_review(numeric_id)
    if existing_review is not None and not user_owns_record(request, existing_review):
        return text(body='Forbidden', status=403)

    custom_fields = storage.get_custom_fields()
    record = {
        'ФИО': get_form_value(request, 'student_name'),
        'Пол': get_form_value(request, 'student_sex'),
        'Институт': get_form_value(request, 'institute'),
        'Группа': get_form_value(request, 'group'),
        'Вид спорта': get_form_value(request, 'sport'),
        'Дата': get_form_value(request, 'date'),
        'Уровень соревнований': get_form_value(request, 'level'),
        'Название соревнований': get_form_value(request, 'name'),
        'Место': get_form_value(request, 'position'),
        'Курс': get_form_value(request, 'course'),
    }
    record.update({field.label: get_form_value(request, f'custom__{field.key}') for field in custom_fields})

    try:
        competition = build_competition(record, custom_fields=custom_fields, manual_input=True)
    except (TypeError, ValueError) as exc:
        return text(body=f'Invalid row data: {exc}', status=400)

    storage.update_competition(numeric_id, competition)
    # Роль решает статус после правки: модератор (admin/editor)
    # подтверждает, атлет отправляет на повторную проверку — независимо
    # от прежнего статуса записи.
    if user_is_moderator(request):
        storage.set_competition_review(numeric_id, 'approved')
        # Правка модератора = авто-подтверждение, фиксируем как record_approved.
        log_audit_event(request, 'record_approved', {'record_id': numeric_id, 'auto': True})
    else:
        storage.set_competition_review(numeric_id, 'pending')
    return redirect(to='/')


@app.post('/competition/<record_id>/review/<decision>')
async def review_competition(request: Request, record_id: str, decision: str):
    auth_error = require_moderator(request)
    if auth_error is not None:
        return auth_error

    if decision not in ('approve', 'reject'):
        return text(body='Unknown review decision', status=400)

    try:
        numeric_id = int(record_id)
    except ValueError:
        return text(body='Invalid record id', status=400)

    if get_storage(request.app).get_competition_review(numeric_id) is None:
        return text(body='Record not found', status=404)

    status = 'approved' if decision == 'approve' else 'rejected'
    comment = get_form_value(request, 'comment').strip() if decision == 'reject' else ''
    get_storage(request.app).set_competition_review(numeric_id, status, comment)
    log_audit_event(
        request,
        'record_approved' if decision == 'approve' else 'record_rejected',
        {'record_id': numeric_id, 'comment': comment},
    )
    return redirect(to='/')


@app.post('/competition/<record_id>/delete')
async def delete_competition(request: Request, record_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    try:
        numeric_id = int(record_id)
    except ValueError:
        return text(body='Invalid record id', status=400)

    storage = get_storage(request.app)
    storage.delete_competition(numeric_id)
    return redirect(to='/')


@app.post('/admin/fields')
async def create_custom_field(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    storage = get_storage(request.app)
    label = get_form_value(request, 'label').strip()
    field_type = get_form_value(request, 'field_type').strip() or 'text'
    if not label:
        return build_redirect_with_message(error='Название поля обязательно', url='/admin/fields')
    if field_type not in FIELD_TYPE_OPTIONS:
        return build_redirect_with_message(error='Недопустимый тип поля', url='/admin/fields')

    try:
        storage.create_custom_field(
            key=make_unique_custom_field_key(label, storage.get_custom_fields(include_inactive=True)),
            label=label,
            field_type=field_type,
            required=parse_checkbox(request, 'required'),
            show_in_table=parse_checkbox(request, 'show_in_table'),
            show_in_export=parse_checkbox(request, 'show_in_export'),
            show_in_template=parse_checkbox(request, 'show_in_template'),
            sort_order=int(get_form_value(request, 'sort_order') or 0),
        )
    except Exception as exc:
        return build_redirect_with_message(error=f'Не удалось создать поле: {exc}', url='/admin/fields')
    return build_redirect_with_message(message='Поле добавлено', url='/admin/fields')


@app.post('/admin/fields/<field_id>')
async def update_custom_field(request: Request, field_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    try:
        numeric_field_id = int(field_id)
    except ValueError:
        return text(body='Invalid field id', status=400)

    label = get_form_value(request, 'label').strip()
    field_type = get_form_value(request, 'field_type').strip() or 'text'
    if not label:
        return build_redirect_with_message(error='Название поля обязательно', url='/admin/fields')
    if field_type not in FIELD_TYPE_OPTIONS:
        return build_redirect_with_message(error='Недопустимый тип поля', url='/admin/fields')

    try:
        sort_order = int(get_form_value(request, 'sort_order') or 0)
    except ValueError:
        return build_redirect_with_message(error='Порядок должен быть числом', url='/admin/fields')

    storage = get_storage(request.app)
    storage.update_custom_field(
        field_id=numeric_field_id,
        label=label,
        field_type=field_type,
        required=parse_checkbox(request, 'required'),
        show_in_table=parse_checkbox(request, 'show_in_table'),
        show_in_export=parse_checkbox(request, 'show_in_export'),
        show_in_template=parse_checkbox(request, 'show_in_template'),
        sort_order=sort_order,
        active=parse_checkbox(request, 'active'),
    )
    return build_redirect_with_message(message='Настройки поля сохранены', url='/admin/fields')


@app.post('/admin/fields/<field_id>/delete')
async def delete_custom_field(request: Request, field_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    try:
        numeric_field_id = int(field_id)
    except ValueError:
        return text(body='Invalid field id', status=400)

    storage = get_storage(request.app)
    storage.disable_custom_field(numeric_field_id)
    return build_redirect_with_message(message='Поле отключено', url='/admin/fields')


@app.post('/admin/users')
async def create_user(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    storage = get_storage(request.app)
    username = get_form_value(request, 'username').strip()
    password = get_form_value(request, 'password')
    role = get_form_value(request, 'role').strip()
    if not username:
        return build_redirect_with_message(error='Имя пользователя обязательно', url='/admin/users')
    if role not in USER_ROLES:
        return build_redirect_with_message(error='Недопустимая роль', url='/admin/users')
    if len(password) < MIN_PASSWORD_LENGTH:
        return build_redirect_with_message(
            error=f'Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов', url='/admin/users'
        )
    if storage.get_user(username) is not None:
        return build_redirect_with_message(error='Пользователь уже существует', url='/admin/users')

    storage.create_user(username, hash_password(password), role)
    return build_redirect_with_message(message='Пользователь добавлен', url='/admin/users')


@app.post('/admin/users/<user_id>/password')
async def reset_user_password(request: Request, user_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    try:
        numeric_user_id = int(user_id)
    except ValueError:
        return text(body='Invalid user id', status=400)

    password = get_form_value(request, 'password')
    if len(password) < MIN_PASSWORD_LENGTH:
        return build_redirect_with_message(
            error=f'Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов', url='/admin/users'
        )

    storage = get_storage(request.app)
    target_user = storage.get_user_by_id(numeric_user_id)
    if target_user is None:
        return build_redirect_with_message(error='Пользователь не найден', url='/admin/users')
    storage.set_user_password(numeric_user_id, hash_password(password))
    log_audit_event(
        request,
        'password_changed',
        {'target_user_id': numeric_user_id, 'target_username': target_user['username']},
    )
    return build_redirect_with_message(message='Пароль обновлён', url='/admin/users')


@app.post('/admin/users/<user_id>/active')
async def toggle_user_active(request: Request, user_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    try:
        numeric_user_id = int(user_id)
    except ValueError:
        return text(body='Invalid user id', status=400)

    storage = get_storage(request.app)
    user = storage.get_user_by_id(numeric_user_id)
    if user is None:
        return build_redirect_with_message(error='Пользователь не найден', url='/admin/users')
    if user['username'] == (get_auth_user(request) or {}).get('username'):
        return build_redirect_with_message(error='Нельзя отключить собственную учётную запись', url='/admin/users')

    storage.set_user_active(numeric_user_id, not user['active'])
    return build_redirect_with_message(message='Статус пользователя изменён', url='/admin/users')


def count_active_admins(storage: SQLiteAdapter) -> int:
    return sum(1 for user in storage.list_users() if user['role'] == ADMIN_ROLE and user['active'])


@app.post('/admin/users/<user_id>/delete')
async def delete_user(request: Request, user_id: str):
    # Удаление аккаунта: записи — исторические факты, остаются с owner_id NULL;
    # псевдонимы и привязка кабинета исчезают вместе с аккаунтом. Подтверждение —
    # ввод логина в поле (docs/data-model-decisions.md «Удаление пользователей»).
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    try:
        numeric_user_id = int(user_id)
    except ValueError:
        return text(body='Invalid user id', status=400)

    storage = get_storage(request.app)
    target_user = storage.get_user_by_id(numeric_user_id)
    if target_user is None:
        return build_redirect_with_message(error='Пользователь не найден', url='/admin/users')
    if target_user['username'] == (get_auth_user(request) or {}).get('username'):
        return build_redirect_with_message(error='Нельзя удалить собственную учётную запись', url='/admin/users')
    if target_user['role'] == ADMIN_ROLE and target_user['active'] and count_active_admins(storage) <= 1:
        return build_redirect_with_message(
            error='Нельзя удалить последнего активного администратора', url='/admin/users'
        )

    confirm_username = get_form_value(request, 'confirm_username').strip()
    if not confirm_username or confirm_username != target_user['username']:
        return build_redirect_with_message(
            error='Для подтверждения введите логин удаляемого пользователя',
            url='/admin/users',
        )

    detached = storage.delete_user(numeric_user_id)
    log_audit_event(
        request,
        'user_deleted',
        {
            'target_user_id': numeric_user_id,
            'target_username': target_user['username'],
            'records_detached': detached,
        },
    )
    return build_redirect_with_message(
        message=f'Пользователь «{target_user["username"]}» удалён. Отвязано записей: {detached}',
        url='/admin/users',
    )


def build_attachments_by_record(all_attachments: list[dict]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for attachment in all_attachments:
        grouped.setdefault(attachment['record_id'], []).append(attachment)
    return grouped


def attachments_dir() -> Path:
    base = Path(settings.data_folder) / 'files'
    base.mkdir(parents=True, exist_ok=True)
    return base


def detect_attachment_type(body: bytes, filename: str) -> str | None:
    extension = Path(filename).suffix.lower().lstrip('.')
    expected_type = ATTACHMENT_EXTENSIONS.get(extension)
    if expected_type is None:
        return None
    signature = ATTACHMENT_SIGNATURES[expected_type]
    return expected_type if body.startswith(signature) else None


@app.post('/competition/<record_id>/attachments')
async def upload_attachment(request: Request, record_id: str):
    auth_error = require_writer(request)
    if auth_error is not None:
        return auth_error

    try:
        numeric_id = int(record_id)
    except ValueError:
        return text(body='Invalid record id', status=400)

    storage = get_storage(request.app)
    review = storage.get_competition_review(numeric_id)
    if review is None:
        return text(body='Record not found', status=404)
    if not user_owns_record(request, review):
        return text(body='Forbidden', status=403)

    upload_file = request.files.get('file')
    if not upload_file or not upload_file.body:
        return text(body='No file uploaded', status=400)
    if len(upload_file.body) > ATTACHMENT_MAX_SIZE:
        return text(body='Файл больше 5 МБ', status=400)

    filename = (upload_file.name or 'attachment').rsplit('/', 1)[-1]
    content_type = detect_attachment_type(upload_file.body, filename)
    if content_type is None:
        return text(body='Допустимы только PDF, JPEG и PNG', status=400)

    extension = Path(filename).suffix.lower().lstrip('.') or 'bin'
    stored_name = f'{uuid.uuid4().hex}.{extension}'
    record_dir = attachments_dir() / str(numeric_id)
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / stored_name).write_bytes(upload_file.body)

    storage.create_attachment(
        record_id=numeric_id,
        filename=filename,
        stored_name=stored_name,
        content_type=content_type,
        size=len(upload_file.body),
        uploaded_by=get_current_user_id(request),
    )
    return text(body='Файл загружен')


@app.get('/attachment/<attachment_id>')
async def download_attachment(request: Request, attachment_id: str):
    if get_auth_user(request) is None:
        return text(body='Unauthorized', status=401)

    try:
        numeric_attachment_id = int(attachment_id)
    except ValueError:
        return text(body='Invalid attachment id', status=400)

    storage = get_storage(request.app)
    attachment = storage.get_attachment(numeric_attachment_id)
    if attachment is None:
        return text(body='Not Found', status=404)
    review = storage.get_competition_review(attachment['record_id'])
    if review is not None and not user_owns_record(request, review):
        return text(body='Forbidden', status=403)

    file_path = attachments_dir() / str(attachment['record_id']) / attachment['stored_name']
    if not file_path.is_file():
        return text(body='Not Found', status=404)

    return raw(
        await asyncio.to_thread(file_path.read_bytes),
        headers={
            'content-type': attachment['content_type'],
            'content-disposition': f'attachment; filename="{attachment["stored_name"]}"',
        },
    )


@app.post('/attachment/<attachment_id>/delete')
async def delete_attachment(request: Request, attachment_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    try:
        numeric_attachment_id = int(attachment_id)
    except ValueError:
        return text(body='Invalid attachment id', status=400)

    storage = get_storage(request.app)
    attachment = storage.get_attachment(numeric_attachment_id)
    if attachment is None:
        return text(body='Not Found', status=404)

    file_path = attachments_dir() / str(attachment['record_id']) / attachment['stored_name']
    file_path.unlink(missing_ok=True)
    storage.delete_attachment(numeric_attachment_id)
    return redirect(to='/')


@app.post('/admin/levels')
async def create_level(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    name = get_form_value(request, 'name').strip()
    if not name:
        return build_redirect_with_message(error='Название уровня обязательно', url='/admin/catalogs')
    if name in get_storage(request.app).get_level_names(include_inactive=True):
        return build_redirect_with_message(error='Такой уровень уже существует', url='/admin/catalogs')

    get_storage(request.app).create_level(name)
    return build_redirect_with_message(message='Уровень добавлен', url='/admin/catalogs')


@app.post('/admin/levels/<level_id>')
async def rename_level(request: Request, level_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    try:
        numeric_level_id = int(level_id)
    except ValueError:
        return text(body='Invalid level id', status=400)

    name = get_form_value(request, 'name').strip()
    if not name:
        return build_redirect_with_message(error='Название уровня обязательно', url='/admin/catalogs')

    get_storage(request.app).rename_level(numeric_level_id, name)
    return build_redirect_with_message(message='Уровень переименован', url='/admin/catalogs')


@app.post('/admin/levels/<level_id>/delete')
async def disable_level(request: Request, level_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    try:
        numeric_level_id = int(level_id)
    except ValueError:
        return text(body='Invalid level id', status=400)

    get_storage(request.app).disable_level(numeric_level_id)
    return build_redirect_with_message(message='Уровень скрыт из списков', url='/admin/catalogs')


@app.post('/admin/users/<user_id>/alias')
async def add_user_alias(request: Request, user_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    try:
        numeric_user_id = int(user_id)
    except ValueError:
        return text(body='Invalid user id', status=400)

    storage = get_storage(request.app)
    if storage.get_user_by_id(numeric_user_id) is None:
        return build_redirect_with_message(error='Пользователь не найден', url='/admin/users')

    name = get_form_value(request, 'name').strip()
    if not name:
        return build_redirect_with_message(error='ФИО обязательно', url='/admin/users')

    target_user = storage.get_user_by_id(numeric_user_id)
    storage.add_name_alias(numeric_user_id, name)
    log_audit_event(
        request,
        'alias_added',
        {
            'target_user_id': numeric_user_id,
            'target_username': target_user['username'] if target_user else '',
            'name': name,
        },
    )
    return build_redirect_with_message(message=f'ФИО «{name}» привязано к аккаунту', url='/admin/users')


@app.post('/admin/students/merge')
async def merge_students(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    from_name = get_form_value(request, 'from_name').strip()
    to_name = get_form_value(request, 'to_name').strip()
    confirm = checkbox_to_bool(get_form_value(request, 'confirm'))
    rewrite_names = checkbox_to_bool(get_form_value(request, 'rewrite_names'))
    if not from_name or not to_name:
        return text(body='Укажите оба ФИО', status=400)
    if from_name == to_name:
        return text(body='ФИО совпадают', status=400)

    from_hash = hashlib.sha256(from_name.encode()).hexdigest()
    to_hash = hashlib.sha256(to_name.encode()).hexdigest()
    storage = get_storage(request.app)
    affected = storage.count_records_by_student_hash(from_hash)
    if not confirm:
        return json_response(
            {
                'preview': True,
                'from_name': from_name,
                'to_name': to_name,
                'records_to_merge': affected,
            }
        )

    merged = storage.merge_students(from_hash, to_hash, new_name=to_name if rewrite_names else None)
    aliases_updated = storage.carry_name_aliases(from_name, to_name)
    log_audit_event(
        request,
        'students_merged',
        {
            'from_name': from_name,
            'to_name': to_name,
            'merged': merged,
            'rewrite_names': rewrite_names,
            'aliases_updated': aliases_updated,
        },
    )
    return json_response(
        {
            'merged': merged,
            'rewritten_names': rewrite_names,
            'aliases_updated': aliases_updated,
        }
    )


def backups_dir() -> Path:
    return Path(settings.data_folder) / 'backups'


def files_dir() -> Path:
    return Path(settings.data_folder) / 'files'


def format_size(num_bytes: int | float) -> str:
    """Человекочитаемый размер: 12,4 ГБ / 5 МБ / 512 Б (запятая как разделитель)."""
    size = float(num_bytes)
    for unit in ('Б', 'КБ', 'МБ', 'ГБ', 'ТБ'):
        if size < 1024 or unit == 'ТБ':
            if unit == 'Б':
                return f'{int(size)} Б'
            rounded = f'{size:.1f}'.replace('.', ',')
            if rounded.endswith(',0'):
                rounded = rounded[:-2]
            return f'{rounded} {unit}'
        size /= 1024
    return f'{int(size)} Б'


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(item.stat().st_size for item in path.rglob('*') if item.is_file())


def format_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime('%d.%m.%Y %H:%M')


def collect_maintenance_stats() -> dict:
    """Панель состояния обслуживания (замечания №10 и №16б из FEEDBACK.md).

    Только чтение: размеры БД (файл + WAL/SHM), вложений, бэкапов, место на
    диске тома с data/, последний бэкап и список архивов очисток.
    """
    data_root = Path(settings.data_folder)
    db_path = Path(settings.database_path)

    db_files = []
    for suffix, label in (('', 'файл БД'), ('-wal', 'WAL'), ('-shm', 'SHM')):
        candidate = db_path.with_name(db_path.name + suffix)
        size = file_size(candidate)
        if size > 0 or not suffix:
            db_files.append({'label': label, 'size': size, 'size_label': format_size(size)})
    db_total = sum(item['size'] for item in db_files)

    attachments_size = dir_size(data_root / 'files')
    backups_size = dir_size(data_root / 'backups')

    disk_root = data_root if data_root.is_dir() else db_path.parent if db_path.parent.is_dir() else Path('.')
    usage = shutil.disk_usage(disk_root)
    disk_percent = round(usage.used / usage.total * 100, 1) if usage.total else 0

    last_backup = None
    pre_wipe_archives = []
    root = data_root / 'backups'
    if root.is_dir():
        backup_files = [item for item in root.rglob('*') if item.is_file()]
        if backup_files:
            latest = max(backup_files, key=lambda item: item.stat().st_mtime)
            stat = latest.stat()
            last_backup = {
                'name': latest.name,
                'subdir': str(latest.parent.relative_to(root)) if latest.parent != root else '',
                'time': format_timestamp(stat.st_mtime),
                'size_label': format_size(stat.st_size),
            }
        pre_wipe_files = sorted(
            (item for item in root.iterdir() if item.is_file() and item.name.startswith('pre-wipe-')),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:PRE_WIPE_ARCHIVES_SHOWN]
        pre_wipe_archives = [
            {
                'name': item.name,
                'time': format_timestamp(item.stat().st_mtime),
                'size_label': format_size(item.stat().st_size),
            }
            for item in pre_wipe_files
        ]

    return {
        'db_files': db_files,
        'db_total_label': format_size(db_total),
        'attachments_size_label': format_size(attachments_size),
        'backups_size_label': format_size(backups_size),
        'disk': {
            'percent': disk_percent,
            'used_label': format_size(usage.used),
            'total_label': format_size(usage.total),
            'free_label': format_size(usage.free),
            'caption': f'Диск: {format_size(usage.used)} из {format_size(usage.total)} занято',
        },
        'last_backup': last_backup,
        'pre_wipe_archives': pre_wipe_archives,
    }


@app.get('/admin/maintenance')
async def admin_maintenance_page(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error
    storage = get_storage(request.app)
    stats = await asyncio.to_thread(collect_maintenance_stats)
    return await render(
        template_name=jinja_env.get_template('admin_maintenance.html'),
        context={
            'request': request,
            'wipe_confirm_phrase': WIPE_CONFIRM_PHRASE,
            'records_count': storage.count_competitions(),
            'attachments_count': storage.count_attachments(),
            'stats': stats,
            **get_flash_args(request),
        },
    )


def sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    for column in df.columns:
        if df[column].dtype == object:
            df[column] = df[column].map(sanitize_spreadsheet_value)
    return df


def build_users_dataframe(users: Sequence[dict]) -> pd.DataFrame:
    rows = [
        {
            'Логин': user['username'],
            'Роль': user['role'],
            'Активен': 'да' if user.get('active') else 'нет',
            'Псевдонимы ФИО': ', '.join(user.get('name_aliases') or []),
        }
        for user in users
    ]
    return sanitize_dataframe(pd.DataFrame.from_records(rows, columns=['Логин', 'Роль', 'Активен', 'Псевдонимы ФИО']))


def build_levels_dataframe(levels: Sequence[dict]) -> pd.DataFrame:
    rows = [{'Уровень': level['name'], 'Статус': 'активен' if level.get('active') else 'скрыт'} for level in levels]
    return sanitize_dataframe(pd.DataFrame.from_records(rows, columns=['Уровень', 'Статус']))


def build_sports_dataframe(sport_names: Sequence[str]) -> pd.DataFrame:
    return sanitize_dataframe(
        pd.DataFrame.from_records([{'Вид спорта': name} for name in sport_names], columns=['Вид спорта'])
    )


def build_database_export_frames(storage: SQLiteAdapter) -> dict[str, pd.DataFrame]:
    """All database sheets for the maintenance export (one xlsx, one sheet per entity)."""
    competitions = storage.get_competitions()
    export_custom_fields = [field for field in storage.get_custom_fields() if field.show_in_export]
    return {
        'Записи': build_index_dataframe(competitions, export_custom_fields),
        'Пользователи': build_users_dataframe(storage.list_users()),
        'Уровни': build_levels_dataframe(storage.list_levels()),
        'Виды спорта': build_sports_dataframe(storage.get_sport_names()),
    }


def write_database_workbook(frames: dict[str, pd.DataFrame], buffer: BytesIO) -> None:
    with pd.ExcelWriter(buffer) as writer:
        for sheet_name, frame in frames.items():
            frame.to_excel(writer, sheet_name=sheet_name, index=False)


@app.get('/admin/maintenance/export')
async def export_database(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    storage = get_storage(request.app)
    frames = await asyncio.to_thread(build_database_export_frames, storage)
    buffer = BytesIO()
    await asyncio.to_thread(write_database_workbook, frames, buffer)

    now_str = datetime.utcnow().strftime('%d-%m-%Y_%H-%M-%S')
    filename = f'База_данных_{now_str}.xlsx'
    return raw(
        buffer.getvalue(),
        headers={
            'content-type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'content-disposition': f'attachment; filename="{filename}"',
        },
    )


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


def parse_wipe_request(request: Request) -> tuple[dict | None, str | None]:
    """Валидация параметров очистки: режим (полная/по дате), объём, дата, галочка вложений."""
    mode = get_form_value(request, 'mode').strip() or 'scope'
    if mode not in WIPE_MODES:
        return None, 'Некорректный режим очистки'
    params: dict = {'mode': mode, 'scope': None, 'date_before': None, 'with_attachments': True}
    if mode == 'scope':
        scope = get_form_value(request, 'scope').strip()
        if scope not in WIPE_SCOPES:
            return None, 'Некорректный объём очистки'
        params['scope'] = scope
    else:
        raw_date = get_form_value(request, 'date_before').strip()
        try:
            params['date_before'] = datetime.strptime(raw_date, settings.date_format)
        except ValueError:
            return None, 'Некорректная дата очистки (ожидается дд.мм.гггг)'
        # Галочка «вместе с вложениями» включена по умолчанию: нет поля —
        # считаем включённой; скрытый маркер 0 + чекбокс 1 различают снятие.
        raw_flags = request.form.getlist('with_attachments')
        params['with_attachments'] = '1' in raw_flags or not raw_flags
    return params, None


def wipe_scope_label(params: dict) -> str:
    return params['scope'] if params['mode'] == 'scope' else 'date'


def collect_wipe_targets(storage: SQLiteAdapter, params: dict) -> tuple[list, list[dict]]:
    """Записи и вложения, попадающие под очистку (для превью и архива)."""
    if params['mode'] == 'scope':
        records = list(storage.get_competitions()) if params['scope'] != 'attachments' else []
        attachments = storage.get_attachments() if params['scope'] != 'records' else []
    else:
        records = storage.get_competitions_before(params['date_before'])
        record_ids = [int(comp.record_id) for comp in records]
        attachments = storage.get_attachments_for_records(record_ids) if params['with_attachments'] else []
    return records, attachments


def perform_wipe(storage: SQLiteAdapter, params: dict, records: Sequence) -> tuple[int, int]:
    """Исполнить очистку; возвращает (удалено записей, удалено вложений)."""
    records_deleted = 0
    attachments_deleted = 0
    if params['mode'] == 'scope':
        if params['scope'] in ('records', 'all'):
            records_deleted = storage.delete_all_competitions()
        if params['scope'] in ('attachments', 'all'):
            attachments_deleted = storage.delete_all_attachments()
            remove_attachment_files()
    else:
        record_ids = [int(comp.record_id) for comp in records]
        records_deleted = storage.delete_competitions_before(params['date_before'])
        if params['with_attachments']:
            attachments_deleted = storage.delete_attachments_for_records(record_ids)
            remove_attachment_record_dirs(record_ids)
    return records_deleted, attachments_deleted


def create_pre_wipe_archive(
    storage: SQLiteAdapter,
    *,
    scope_label: str,
    records: Sequence,
    attachments: Sequence[dict],
) -> dict:
    """Архив удаляемого перед очисткой (№12): xlsx записей + zip их вложений.

    В data/backups: pre-wipe-<дата>-<scope>.xlsx (когда удаляются записи)
    и pre-wipe-<дата>-<scope>.zip (когда удаляются вложения с файлами).
    """
    root = backups_dir()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    result = {
        'scope': scope_label,
        'xlsx': None,
        'zip': None,
        'records': len(records),
        'attachments': len(attachments),
    }

    if records:
        export_custom_fields = [field for field in storage.get_custom_fields() if field.show_in_export]
        df = build_index_dataframe(records, export_custom_fields)
        xlsx_path = root / f'pre-wipe-{stamp}-{scope_label}.xlsx'
        df.to_excel(xlsx_path, index=False)
        result['xlsx'] = xlsx_path.name

    if attachments:
        zip_path = root / f'pre-wipe-{stamp}-{scope_label}.zip'
        written = False
        seen_names: set[str] = set()
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for attachment in attachments:
                source = attachment_source_path(attachment)
                if not source.is_file():
                    continue
                # Внутри архива — понятное имя файла; при коллизии (одноимённые
                # вложения у одной записи) откатываемся на уникальное stored_name.
                arc_name = f'{attachment["record_id"]}/{attachment["filename"]}'
                if arc_name in seen_names:
                    arc_name = f'{attachment["record_id"]}/{attachment["stored_name"]}'
                seen_names.add(arc_name)
                archive.write(source, arc_name)
                written = True
        if written:
            result['zip'] = zip_path.name
        else:
            zip_path.unlink(missing_ok=True)
    return result


def build_wipe_preview(storage: SQLiteAdapter, params: dict) -> dict:
    """Превью очистки: сколько записей/вложений уйдёт и сколько места освободится (№18)."""
    records, attachments = collect_wipe_targets(storage, params)
    attachments_bytes = attachments_files_bytes(attachments)
    return {
        'records': len(records),
        'attachments': len(attachments),
        'attachments_size_bytes': attachments_bytes,
        'attachments_size': format_size(attachments_bytes),
    }


def log_wipe_execution(
    request: Request,
    params: dict,
    archive: dict,
    records_deleted: int,
    attachments_deleted: int,
    freed_total: int,
) -> str:
    """Аудит выполненной очистки (№16а) и итоговое flash-сообщение."""
    log_audit_event(
        request,
        'pre_wipe_archive_created',
        {
            'scope': archive['scope'],
            'archive_xlsx': archive['xlsx'],
            'archive_zip': archive['zip'],
            'records': archive['records'],
            'attachments': archive['attachments'],
        },
    )
    wipe_details = {
        'scope': archive['scope'],
        'records_deleted': records_deleted,
        'attachments_deleted': attachments_deleted,
        'freed_bytes': freed_total,
        'archive_xlsx': archive['xlsx'],
        'archive_zip': archive['zip'],
    }
    if params['mode'] == 'date':
        wipe_details['date_before'] = params['date_before'].strftime(settings.date_format)
        wipe_details['with_attachments'] = params['with_attachments']
    log_audit_event(request, 'db_wiped', wipe_details)

    summary = (
        f'Очистка выполнена: удалено записей {records_deleted}, вложений {attachments_deleted}. '
        f'Освобождено: {format_size(freed_total)}'
    )
    archive_names = ', '.join(name for name in (archive['xlsx'], archive['zip']) if name)
    if archive_names:
        summary += f'. Архив: {archive_names}'
    return summary


@app.post('/admin/maintenance/wipe')
async def wipe_database(request: Request):
    # Очистка в два шага: превью (POST без confirm=1 → JSON с числами) и
    # выполнение с фразой «УДАЛИТЬ» (docs/data-model-decisions.md).
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    storage = get_storage(request.app)
    params, error = parse_wipe_request(request)
    if error is not None:
        return text(body=error, status=400)

    if get_form_value(request, 'confirm') != '1':
        return json_response(await asyncio.to_thread(build_wipe_preview, storage, params))

    confirm_phrase = get_form_value(request, 'confirm_phrase').strip()
    if confirm_phrase != WIPE_CONFIRM_PHRASE:
        return text(body=f'Для подтверждения введите слово «{WIPE_CONFIRM_PHRASE}»', status=400)

    db_size_before = file_size(Path(settings.database_path))
    records, attachments = await asyncio.to_thread(collect_wipe_targets, storage, params)
    freed_files_bytes = attachments_files_bytes(attachments)

    # Сначала архив — страховка от «очистил и пожалел»; не создался — не чистим.
    try:
        archive = await asyncio.to_thread(
            create_pre_wipe_archive,
            storage,
            scope_label=wipe_scope_label(params),
            records=records,
            attachments=attachments,
        )
    except Exception:
        logger.exception('Failed to create pre-wipe archive')
        return build_redirect_with_message(
            error='Не удалось создать архив очистки — очистка отменена',
            url='/admin/maintenance',
        )

    records_deleted, attachments_deleted = await asyncio.to_thread(perform_wipe, storage, params, records)
    try:
        # DELETE не сжимает файл SQLite: перестраиваем базу, чтобы место
        # реально освободилось (№18). Только здесь — редкая админская операция.
        await asyncio.to_thread(storage.vacuum)
    except Exception:
        logger.exception('VACUUM after wipe failed')

    freed_db_bytes = max(0, db_size_before - file_size(Path(settings.database_path)))
    freed_total = freed_db_bytes + freed_files_bytes

    summary = log_wipe_execution(request, params, archive, records_deleted, attachments_deleted, freed_total)
    return build_redirect_with_message(message=summary, url='/admin/maintenance')


@app.post('/admin/maintenance/backup')
async def backup_now(request: Request):
    """Ручной бэкап (№10): та же логика, что у таймерного scripts/backup_sqlite.py."""
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    result = await asyncio.to_thread(
        run_backup,
        Path(settings.database_path),
        backups_dir(),
        14,
        files_dir(),
    )
    log_audit_event(
        request,
        'backup_created',
        {
            'source': 'manual',
            'archive': result['archive'].name,
            'archive_size': result['archive'].stat().st_size,
            'files_archive': result['files_archive'].name if result['files_archive'] else None,
        },
    )
    message = f'Бэкап создан: {result["archive"].name}'
    if result['files_archive'] is not None:
        message += f', вложения: {result["files_archive"].name}'
    return build_redirect_with_message(message=message, url='/admin/maintenance')


BACKUP_DOWNLOAD_NAME = re.compile(r'\A[A-Za-z0-9][A-Za-z0-9._-]*\Z')
BACKUP_CONTENT_TYPES = {
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.zip': 'application/zip',
    '.gz': 'application/gzip',
}


@app.get('/admin/maintenance/backups/<filename>')
async def download_backup(request: Request, filename: str):
    """Скачать файл из data/backups (архивы очисток и бэкапы).

    Имя строго валидируется (без путей и спецсимволов) + контроль после
    resolve() — защита от path-traversal.
    """
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    if not BACKUP_DOWNLOAD_NAME.fullmatch(filename):
        return text(body='Invalid backup filename', status=400)

    root = backups_dir().resolve()
    target = (root / filename).resolve()
    if target.parent != root or not target.is_file():
        return text(body='Not Found', status=404)

    content_type = BACKUP_CONTENT_TYPES.get(target.suffix.lower(), 'application/octet-stream')
    return raw(
        await asyncio.to_thread(target.read_bytes),
        headers={
            'content-type': content_type,
            'content-disposition': f'attachment; filename="{filename}"',
        },
    )


def parse_audit_filters(request: Request) -> tuple[dict, dict, str | None]:
    """Фильтры журнала из GET-параметров.

    Возвращает (storage-фильтры с ISO-датами, сырые значения для формы/ссылок,
    текст ошибки или None).
    """
    args = dict(request.args)
    user_filter = (get_param(args, 'user') or '').strip()
    action_filter = (get_param(args, 'action') or '').strip()
    date_from_raw = (get_param(args, 'date_from') or '').strip()
    date_to_raw = (get_param(args, 'date_to') or '').strip()
    raw_values = {
        'user': user_filter,
        'action': action_filter,
        'date_from': date_from_raw,
        'date_to': date_to_raw,
    }

    iso_dates = {}
    for key in ('date_from', 'date_to'):
        raw_value = raw_values[key]
        if not raw_value:
            iso_dates[key] = None
            continue
        try:
            iso_dates[key] = datetime.strptime(raw_value, settings.date_format).date().isoformat()
        except ValueError:
            return {}, raw_values, f'Некорректная дата фильтра: {raw_value}'

    filters = {
        'username': user_filter or None,
        'action': action_filter or None,
        'date_from': iso_dates['date_from'],
        'date_to': iso_dates['date_to'],
    }
    return filters, raw_values, None


@app.get('/admin/audit')
async def audit_page(request: Request):
    # Журнал с фильтрами (пользователь/действие/период) и пагинацией —
    # «стрелки + N из M» (замечание №14 из FEEDBACK.md).
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    filters, raw_values, error = parse_audit_filters(request)
    if error is not None:
        return text(body=error, status=400)

    try:
        page = max(1, int(get_param(dict(request.args), 'page') or 1))
    except ValueError:
        page = 1

    events: list[dict] = []
    actions: list[str] = []
    audit_available = True
    total = 0
    try:
        storage = get_storage(request.app)
        total = storage.count_audit_events(**filters)
        pages = max(1, -(-total // AUDIT_PAGE_SIZE))
        page = min(page, pages)
        events = storage.get_audit_events(limit=AUDIT_PAGE_SIZE, offset=(page - 1) * AUDIT_PAGE_SIZE, **filters)
        actions = storage.list_audit_actions()
    except Exception:
        logger.exception('Failed to read audit events')
        audit_available = False
        pages = 1
        page = 1

    query = urlencode({key: value for key, value in raw_values.items() if value})

    def page_url(page_number: int) -> str:
        return f'/admin/audit?{query}&page={page_number}' if query else f'/admin/audit?page={page_number}'

    return await render(
        template_name=jinja_env.get_template('audit.html'),
        context={
            'request': request,
            'events': events,
            'audit_available': audit_available,
            'actions': actions,
            'filters': raw_values,
            'page': page,
            'pages': pages,
            'total': total,
            'page_size': AUDIT_PAGE_SIZE,
            'prev_url': page_url(page - 1) if page > 1 else None,
            'next_url': page_url(page + 1) if page < pages else None,
        },
    )


@app.get('/report')
async def get_report(request: Request):
    # Доступ как раньше: всем ролям, кроме athlete (личный кабинет вместо отчётов).
    if user_is_athlete(request):
        return text(body='Forbidden', status=403)
    error = validate_report_filters(dict(request.args)) or collect_custom_filters(request)[1]
    if error:
        return text(body=error, status=400)

    slice_key = parse_report_slice(dict(request.args))
    report_rows = get_report_rows(request, slice_key)
    return await render(
        template_name=jinja_env.get_template('filtered.html'),
        context={
            'request': request,
            'report_rows': report_rows,
            'report_slice': slice_key,
            'report_slice_column': REPORT_SLICE_COLUMNS[slice_key][0],
            'report_slice_sort_type': 'number' if slice_key in REPORT_SLICE_NUMERIC_KEYS else 'text',
        },
    )


def sanitize_spreadsheet_value(value):
    if isinstance(value, str) and value[:1] in {'=', '+', '-', '@'}:
        return f"'{value}"
    return value


def parse_report_export_columns(request: Request, slice_key: str) -> tuple[Sequence[str], str | None]:
    """Выбор колонок для выгрузки отчёта (GET-параметр ``columns``).

    Формат — повторённые параметры и/или список через запятую. Набор колонок
    зависит от среза (group_by): «название среза + Участий/Побед/Призовых»,
    для «студента» — как раньше (ФИО/пол/институт/группа/курс + метрики).
    Порядок в файле всегда канонический (как в HTML-отчёте), неизвестные
    имена игнорируются, старое имя «Количество участий» — алиас «Участий».
    Без параметра — полный набор (обратная совместимость; пустой ``?columns=``
    парсер запроса отбрасывает так же, как отсутствие параметра). Параметр
    есть, но валидных колонок не осталось — 400: минимум одна колонка
    обязательна.
    """
    available = report_export_columns(slice_key)
    raw_values = request.args.getlist('columns')
    if not raw_values:
        return available, None
    requested = {
        LEGACY_REPORT_COLUMN_ALIASES.get(part.strip(), part.strip())
        for value in raw_values
        for part in value.split(',')
        if part.strip()
    }
    columns = [column for column in available if column in requested]
    if not columns:
        return [], 'Не выбрано ни одной колонки для выгрузки'
    return columns, None


def build_report_dataframe(
    report_rows: Sequence,
    columns: Sequence[str],
    slice_key: str = DEFAULT_REPORT_SLICE,
) -> pd.DataFrame:
    if slice_key == DEFAULT_REPORT_SLICE:
        records = [info.model_dump(by_alias=True) for info in report_rows]
    else:
        slice_column = REPORT_SLICE_COLUMNS[slice_key][0]
        records = [
            {
                slice_column: row.slice_value,
                'Участий': row.count_participation,
                'Побед': row.count_wins,
                'Призовых': row.count_prizes,
            }
            for row in report_rows
        ]
    df = pd.DataFrame.from_records(records)
    df = df.reindex(columns=columns)
    for column in df.columns:
        if df[column].dtype == object:
            df[column] = df[column].map(sanitize_spreadsheet_value)
    return df


@app.get('/export/report')
async def export_report(request: Request):
    # Доступ как у просмотра отчёта: всем, кроме athlete. Наследует
    # срез/метрики/фильтры применённого отчёта (GET-параметры).
    if user_is_athlete(request):
        return text(body='Forbidden', status=403)
    error = validate_report_filters(dict(request.args)) or collect_custom_filters(request)[1]
    if error:
        return text(body=error, status=400)

    slice_key = parse_report_slice(dict(request.args))
    columns, columns_error = parse_report_export_columns(request, slice_key)
    if columns_error:
        return text(body=columns_error, status=400)

    report_rows = get_report_rows(request, slice_key)
    df = await asyncio.to_thread(build_report_dataframe, report_rows, columns, slice_key)

    now_str = datetime.utcnow().strftime('%d-%m-%Y_%H-%M-%S')
    filename = f'Отчет_{now_str}.xlsx'
    buffer = BytesIO()
    await asyncio.to_thread(df.to_excel, buffer, index=False)
    buffer.seek(0)

    return raw(
        buffer.getvalue(),
        headers={
            'content-type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'content-disposition': f'attachment; filename="{filename}"',
        },
    )


@app.exception(IsADirectoryError)
async def handle_directory_request(_, exception: IsADirectoryError):
    return text(body='Not Found', status=404)


@app.get('/healthcheck')
def healthcheck(request: Request):
    return text('OK')
