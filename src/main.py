import asyncio
import base64
import binascii
import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime
from io import BytesIO
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
from sanic.response import raw
from sanic_ext import render

from src.auth import hash_password
from src.auth import verify_password
from src.models.competition import Competition
from src.models.custom_field import CustomField
from src.models.http.student_info import StudentInfo
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
WRITE_ROLES = {ADMIN_ROLE, EDITOR_ROLE}

INSECURE_SECRET_VALUES = {'', 'change-me', 'replace-with-random-string'}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 900
LOGIN_LOCKOUT_SECONDS = 900
LOGIN_REJECT_STATUS = 429

login_failures: dict[str, list] = {}

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
REPORT_EXPORT_COLUMNS: Sequence[str] = (
    'ФИО',
    'Пол',
    'Институт',
    'Группа',
    'Курс',
    'Количество участий',
)
FIELD_TYPE_OPTIONS: Sequence[str] = ('text', 'number', 'date', 'url')
USER_ROLES: Sequence[str] = (ADMIN_ROLE, EDITOR_ROLE, VIEWER_ROLE)
MIN_PASSWORD_LENGTH = 6


DEFAULT_LEVELS = ('внутривузовские', 'межвузовские')


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

    if role not in {ADMIN_ROLE, EDITOR_ROLE, VIEWER_ROLE}:
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

    return {'username': payload['username'], 'role': payload['role']}


def authenticate_user(request: Request, username: str, password: str) -> dict | None:
    user = get_storage(request.app).get_user(username)
    if user is None or not user['active']:
        return None
    if not verify_password(password, user['password_hash']):
        return None
    return {
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


def require_admin(request: Request):
    user = get_auth_user(request)
    if not user:
        return text(body='Unauthorized', status=401)
    if user['role'] != ADMIN_ROLE:
        return text(body='Forbidden', status=403)
    return None


def require_writer(request: Request):
    user = get_auth_user(request)
    if not user:
        return text(body='Unauthorized', status=401)
    if user['role'] not in WRITE_ROLES:
        return text(body='Forbidden', status=403)
    return None


def build_redirect_with_message(*, message: str | None = None, error: str | None = None):
    params = {}
    if message:
        params['admin_message'] = message
    if error:
        params['admin_error'] = error
    query = urlencode(params)
    return redirect(f'/?{query}' if query else '/')


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

    return None


def get_student_infos(request: Request) -> Iterable[StudentInfo]:
    args = dict(request.args)
    storage = get_storage(request.app)

    student_infos = storage.get_filtered(
        date_from=get_param(args, 'date_from'),
        date_to=get_param(args, 'date_to'),
        position=get_param(args, 'position'),
        level=get_param(args, 'level'),
        name=get_param(args, 'name'),
    )

    return student_infos


@app.on_request
async def authorize_request(request: Request):
    request.ctx.auth_user = parse_auth_cookie(request)

    if request.path.startswith('/static'):
        return None

    if request.path in AUTH_ALLOWED_PATHS:
        return None

    if get_auth_user(request):
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
    competitions = storage.get_competitions()
    return await render(
        template_name=jinja_env.get_template('index.html'),
        context={
            'request': request,
            'competitions': competitions,
            'custom_fields': custom_fields,
            'admin_custom_fields': storage.get_custom_fields(include_inactive=True),
            'field_type_options': FIELD_TYPE_OPTIONS,
            'can_write': user_can_write(request),
            'is_admin': user_is_admin(request),
            'users': storage.list_users() if user_is_admin(request) else [],
            'user_roles': USER_ROLES,
            'levels': storage.get_level_names(),
            'has_unapproved': any(c.review_status != 'approved' for c in competitions),
            'admin_levels': storage.list_levels() if user_is_admin(request) else [],
            'current_username': (get_auth_user(request) or {}).get('username'),
            'admin_message': get_param(dict(request.args), 'admin_message'),
            'admin_error': get_param(dict(request.args), 'admin_error'),
        },
    )


@app.get('/template/empty.xlsx')
async def export_empty_template(request: Request):
    auth_error = require_writer(request)
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
    auth_error = require_writer(request)
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
    existing = await asyncio.to_thread(storage.get_competitions)
    new_competitions, skipped_duplicates = split_import_competitions(competitions, existing)
    if new_competitions:
        storage.save_competitions(new_competitions, owner_id=get_current_user_id(request))

    summary = f'Импортировано записей: {len(new_competitions)}'
    if skipped_duplicates:
        summary += f'. Пропущено дублей: {skipped_duplicates}'
    return text(body=summary)


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

    try:
        competition = build_competition(record, custom_fields=custom_fields, manual_input=True)
    except (TypeError, ValueError) as exc:
        return text(body=f'Invalid row data: {exc}', status=400)

    storage.save_competitions([competition], owner_id=get_current_user_id(request))
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
    review = storage.get_competition_review(numeric_id)
    if review and review['review_status'] != 'approved':
        current_user_id = get_current_user_id(request)
        if review['owner_id'] == current_user_id:
            storage.set_competition_review(numeric_id, 'pending')
        else:
            storage.set_competition_review(numeric_id, 'approved')
    return redirect(to='/')


@app.post('/competition/<record_id>/review/<decision>')
async def review_competition(request: Request, record_id: str, decision: str):
    auth_error = require_writer(request)
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


@app.post('/clean_db')
async def clean_db(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    storage = get_storage(request.app)
    storage.clean_db()
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
        return build_redirect_with_message(error='Название поля обязательно')
    if field_type not in FIELD_TYPE_OPTIONS:
        return build_redirect_with_message(error='Недопустимый тип поля')

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
        return build_redirect_with_message(error=f'Не удалось создать поле: {exc}')
    return build_redirect_with_message(message='Поле добавлено')


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
        return build_redirect_with_message(error='Название поля обязательно')
    if field_type not in FIELD_TYPE_OPTIONS:
        return build_redirect_with_message(error='Недопустимый тип поля')

    try:
        sort_order = int(get_form_value(request, 'sort_order') or 0)
    except ValueError:
        return build_redirect_with_message(error='Порядок должен быть числом')

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
    return build_redirect_with_message(message='Настройки поля сохранены')


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
    return build_redirect_with_message(message='Поле отключено')


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
        return build_redirect_with_message(error='Имя пользователя обязательно')
    if role not in USER_ROLES:
        return build_redirect_with_message(error='Недопустимая роль')
    if len(password) < MIN_PASSWORD_LENGTH:
        return build_redirect_with_message(error=f'Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов')
    if storage.get_user(username) is not None:
        return build_redirect_with_message(error='Пользователь уже существует')

    storage.create_user(username, hash_password(password), role)
    return build_redirect_with_message(message='Пользователь добавлен')


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
        return build_redirect_with_message(error=f'Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов')

    storage = get_storage(request.app)
    if storage.get_user_by_id(numeric_user_id) is None:
        return build_redirect_with_message(error='Пользователь не найден')
    storage.set_user_password(numeric_user_id, hash_password(password))
    return build_redirect_with_message(message='Пароль обновлён')


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
        return build_redirect_with_message(error='Пользователь не найден')
    if user['username'] == (get_auth_user(request) or {}).get('username'):
        return build_redirect_with_message(error='Нельзя отключить собственную учётную запись')

    storage.set_user_active(numeric_user_id, not user['active'])
    return build_redirect_with_message(message='Статус пользователя изменён')


@app.post('/admin/levels')
async def create_level(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    name = get_form_value(request, 'name').strip()
    if not name:
        return build_redirect_with_message(error='Название уровня обязательно')
    if name in get_storage(request.app).get_level_names(include_inactive=True):
        return build_redirect_with_message(error='Такой уровень уже существует')

    get_storage(request.app).create_level(name)
    return build_redirect_with_message(message='Уровень добавлен')


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
        return build_redirect_with_message(error='Название уровня обязательно')

    get_storage(request.app).rename_level(numeric_level_id, name)
    return build_redirect_with_message(message='Уровень переименован')


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
    return build_redirect_with_message(message='Уровень скрыт из списков')


@app.get('/report')
async def get_report(request: Request):
    error = validate_report_filters(dict(request.args))
    if error:
        return text(body=error, status=400)

    student_infos = get_student_infos(request)
    return await render(
        template_name=jinja_env.get_template('filtered.html'),
        context={
            'request': request,
            'student_infos': student_infos,
        },
    )


def sanitize_spreadsheet_value(value):
    if isinstance(value, str) and value[:1] in {'=', '+', '-', '@'}:
        return f"'{value}"
    return value


def build_report_dataframe(student_infos: Iterable[StudentInfo]) -> pd.DataFrame:
    df = pd.DataFrame.from_records([info.model_dump(by_alias=True) for info in student_infos])
    df = df.reindex(columns=REPORT_EXPORT_COLUMNS)
    for column in df.columns:
        if df[column].dtype == object:
            df[column] = df[column].map(sanitize_spreadsheet_value)
    return df


@app.get('/export/report')
async def export_report(request: Request):
    error = validate_report_filters(dict(request.args))
    if error:
        return text(body=error, status=400)

    student_infos = get_student_infos(request)
    df = await asyncio.to_thread(build_report_dataframe, student_infos)

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
