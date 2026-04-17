import base64
import binascii
import hashlib
import hmac
import secrets
from datetime import datetime
from os.path import join
from typing import Iterable
from typing import Sequence

import pandas as pd
from jinja2 import Environment
from jinja2 import PackageLoader
from jinja2 import select_autoescape
from pandas import isna
from sanic import redirect
from sanic import Request
from sanic import Sanic
from sanic import text
from sanic.response import file
from sanic_ext import render

from src.models.competition import Competition
from src.models.http.student_info import StudentInfo
from src.settings import settings
from src.storage.sqlite import SQLiteAdapter

jinja_env = Environment(
    loader=PackageLoader('src'),
    autoescape=select_autoescape(),
    enable_async=True,
)

app = Sanic('SIBADI_competitions')

app.static(
    uri='/static',
    file_or_directory='src/static',
    name='static',
    directory_view=True,
)

app.ctx.storage = None

AUTH_ALLOWED_PATHS = {'/healthcheck', '/login'}
ADMIN_ROLE = 'admin'
VIEWER_ROLE = 'viewer'

REQUIRED_IMPORT_COLUMNS: Sequence[str] = (
    'ФИО',
    'Пол',
    'Институт',
    'Группа',
    'Вид спорта',
    'Дата',
    'Уровень соревнований',
    'Название соревнований',
    'Место',
    'Курс',
)

INDEX_EXPORT_COLUMNS: Sequence[str] = (
    'ФИО',
    'Пол',
    'Институт',
    'Группа',
    'Вид спорта',
    'Дата',
    'Уровень соревнований',
    'Название соревнований',
    'Место',
    'Курс',
)

REPORT_EXPORT_COLUMNS: Sequence[str] = (
    'ФИО',
    'Пол',
    'Институт',
    'Группа',
    'Курс',
    'Количество участий',
)


@app.before_server_start
async def init_storage(app: Sanic, _):
    if app.ctx.storage is None:
        app.ctx.storage = SQLiteAdapter(settings.database_path)

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


def create_auth_cookie_value(username: str, role: str) -> str:
    payload = f'{username}:{role}'
    signature = hmac.new(
        settings.auth_secret_key.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    token = f'{payload}:{signature}'
    return base64.urlsafe_b64encode(token.encode()).decode()


def parse_auth_cookie(request: Request) -> dict | None:
    token = request.cookies.get(settings.auth_cookie_name)
    if not token:
        return None

    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        username, role, signature = decoded.split(':', 2)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None

    expected_signature = hmac.new(
        settings.auth_secret_key.encode(),
        f'{username}:{role}'.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not secrets.compare_digest(signature, expected_signature):
        return None

    if role not in {ADMIN_ROLE, VIEWER_ROLE}:
        return None

    return {'username': username, 'role': role}


def get_auth_user(request: Request) -> dict | None:
    return getattr(request.ctx, 'auth_user', None)


def authenticate_user(username: str, password: str) -> dict | None:
    accounts = [
        {
            'username': settings.auth_admin_username,
            'password': settings.auth_admin_password,
            'role': ADMIN_ROLE,
        }
    ]
    if settings.auth_viewer_username and settings.auth_viewer_password:
        accounts.append(
            {
                'username': settings.auth_viewer_username,
                'password': settings.auth_viewer_password,
                'role': VIEWER_ROLE,
            }
        )

    for account in accounts:
        if username == account['username'] and password == account['password']:
            return {'username': username, 'role': account['role']}
    return None


def request_is_secure(request: Request) -> bool:
    forwarded_proto = request.headers.get('x-forwarded-proto')
    if forwarded_proto:
        return forwarded_proto == 'https'
    return request.scheme == 'https'


def set_auth_cookie(request: Request, response, username: str, role: str):
    response.add_cookie(
        settings.auth_cookie_name,
        create_auth_cookie_value(username, role),
        httponly=True,
        samesite='Lax',
        secure=request_is_secure(request),
        path='/',
    )


def clear_auth_cookie(response):
    response.delete_cookie(settings.auth_cookie_name, path='/')


def require_admin(request: Request):
    user = get_auth_user(request)
    if not user:
        return text(body='Unauthorized', status=401)
    if user['role'] != ADMIN_ROLE:
        return text(body='Forbidden', status=403)
    return None


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


def build_competition(record: dict, *, manual_input: bool = False) -> Competition:
    student_name = str(record['ФИО']).strip()
    if not student_name:
        raise ValueError('ФИО обязательно')

    date = record['Дата']
    if manual_input:
        date = parse_manual_date(str(date).strip())

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
    )


@app.on_request
async def authorize_request(request: Request):
    request.ctx.auth_user = parse_auth_cookie(request)

    if request.path.startswith('/static'):
        return None

    if request.path in AUTH_ALLOWED_PATHS:
        return None

    if get_auth_user(request):
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
    username = str(get_form_value(request, 'username')).strip()
    password = str(get_form_value(request, 'password'))

    user = authenticate_user(username, password)
    if user is None:
        response = await render(
            template_name=jinja_env.get_template('login.html'),
            context={
                'request': request,
                'error_message': 'Неверный логин или пароль',
            },
        )
        response.status = 401
        return response

    response = redirect('/')
    set_auth_cookie(request, response, user['username'], user['role'])
    return response


@app.post('/logout')
async def logout(request: Request):
    response = redirect('/login')
    clear_auth_cookie(response)
    return response


@app.get('/')
async def index(request: Request):
    storage = get_storage(request.app)
    competitions = storage.get_competitions()
    return await render(
        template_name=jinja_env.get_template('index.html'),
        context={
            'request': request,
            'competitions': competitions,
        },
    )


@app.get('/export/index')
async def export_index(request: Request):
    storage = get_storage(request.app)
    competitions = storage.get_competitions()
    df = pd.DataFrame.from_records([comp.dict(by_alias=True) for comp in competitions])
    df = df.reindex(columns=INDEX_EXPORT_COLUMNS)

    now_str = datetime.now().strftime('%d-%m-%Y_%H-%M-%S')
    filename = f'Отчет_{now_str}.xlsx'
    filepath = join(settings.data_folder, filename)
    df.to_excel(filepath, index=False)

    return await file(filepath, filename=filename)


@app.post('/')
async def upload(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    upload_file = request.files.get('file')
    if not upload_file or not upload_file.body:
        return text(body='No file uploaded', status=400)

    storage = get_storage(request.app)

    try:
        df = pd.read_excel(io=upload_file.body)
        validate_import_columns(df)
    except ValueError as exc:
        return text(body=str(exc), status=400)

    competitions = []
    for _, row in df.iterrows():
        record = row.to_dict()

        try:
            competition = build_competition(record)
        except (TypeError, ValueError) as exc:
            return text(body=f'Invalid row data: {exc}', status=400)

        competitions.append(competition)

    storage.save_competitions(competitions)
    return redirect(to='/')


@app.post('/competition')
async def add_competition(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    storage = get_storage(request.app)
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

    try:
        competition = build_competition(record, manual_input=True)
    except (TypeError, ValueError) as exc:
        return text(body=f'Invalid row data: {exc}', status=400)

    storage.save_competitions([competition])
    return redirect(to='/')


@app.post('/competition/<record_id>')
async def update_competition(request: Request, record_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    storage = get_storage(request.app)
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

    try:
        competition = build_competition(record, manual_input=True)
    except (TypeError, ValueError) as exc:
        return text(body=f'Invalid row data: {exc}', status=400)

    storage.update_competition(record_id, competition)
    return redirect(to='/')


@app.post('/competition/<record_id>/delete')
async def delete_competition(request: Request, record_id: str):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    storage = get_storage(request.app)
    storage.delete_competition(record_id)
    return redirect(to='/')


@app.post('/clean_db')
async def clean_db(request: Request):
    auth_error = require_admin(request)
    if auth_error is not None:
        return auth_error

    storage = get_storage(request.app)
    storage.clean_db()
    return redirect(to='/')


@app.get('/report')
async def get_report(request: Request):
    student_infos = get_student_infos(request)
    return await render(
        template_name=jinja_env.get_template('filtered.html'),
        context={
            'request': request,
            'student_infos': student_infos,
        },
    )


@app.get('/export/report')
async def export_report(request: Request):
    student_infos = get_student_infos(request)
    df = pd.DataFrame.from_records([info.dict(by_alias=True) for info in student_infos])
    df = df.reindex(columns=REPORT_EXPORT_COLUMNS)

    now_str = datetime.now().strftime('%d-%m-%Y_%H-%M-%S')
    filename = f'Отчет_{now_str}.xlsx'
    filepath = join(settings.data_folder, filename)
    df.to_excel(filepath, index=False)

    return await file(filepath, filename=filename)


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


@app.get('/healthcheck')
def healthcheck(request: Request):
    return text('OK')
