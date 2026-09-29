"""Собрание приложения Competitions (Architecture v1).

main.py — оболочка: создание и настройка Sanic-приложения, посев учёток,
глобальные middleware и подключение доменных модулей маршрутов через
register(app). Вся логика живёт в src/web.py, src/auth.py, src/records.py,
src/files.py и src/routes/* (перенесено из main.py без изменения поведения)."""
from sanic import redirect
from sanic import Request
from sanic import Sanic
from sanic import text
from sanic.response import HTTPResponse

from src.auth import ADMIN_ROLE
from src.auth import EDITOR_ROLE
from src.auth import hash_password
from src.auth import parse_auth_cookie
from src.auth import touch_user_seen
from src.auth import username_error
from src.auth import VIEWER_ROLE
from src.routes import admin
from src.routes import auth
from src.routes import calendar
from src.routes import registry
from src.routes import reports
from src.routes import students
from src.settings import settings
from src.storage.sqlite import SQLiteAdapter
from src.web import get_auth_user
from src.web import request_csrf_is_valid
from src.web import unauthorized


app = Sanic('SIBADI_competitions')

app.config.REQUEST_MAX_SIZE = 10 * 1024 * 1024
app.config.REQUEST_TIMEOUT = 60
app.config.RESPONSE_TIMEOUT = 60
# Ровно один доверенный proxy-хоп (nginx): приложение наружу напрямую не
# публикуется — deploy/nginx и docker-compose. nginx выставляет
# X-Forwarded-For через $proxy_add_x_forwarded_for, поэтому реальный IP
# клиента — всегда ПОСЛЕДНЯЯ запись заголовка, и клиент её подделать не
# может (свои поддельные значения nginx дописывает перед ней).
app.config.PROXIES_COUNT = 1

app.static(
    uri='/static',
    file_or_directory='src/static',
    name='static',
    directory_view=False,
)

app.ctx.storage = None


AUTH_ALLOWED_PATHS = {'/healthcheck', '/login'}


INSECURE_SECRET_VALUES = {'', 'change-me', 'replace-with-random-string'}


# Слабые пароли посева учёток (M3): дефолты settings.py и заглушки .env.example.
WEAK_BOOTSTRAP_PASSWORDS = INSECURE_SECRET_VALUES | {'change-me-editor', 'change-me-viewer'}


# Имена переменных окружения для сообщений об ошибке старта (без значений!).
BOOTSTRAP_PASSWORD_ENV_VARS = {
    ADMIN_ROLE: 'AUTH_ADMIN_PASSWORD',
    EDITOR_ROLE: 'AUTH_EDITOR_PASSWORD',
    VIEWER_ROLE: 'AUTH_VIEWER_PASSWORD',
}


BOOTSTRAP_USERNAME_ENV_VARS = {
    ADMIN_ROLE: 'AUTH_ADMIN_USERNAME',
    EDITOR_ROLE: 'AUTH_EDITOR_USERNAME',
    VIEWER_ROLE: 'AUTH_VIEWER_USERNAME',
}


DEFAULT_LEVELS = ('внутривузовские', 'межвузовские')


def seed_levels(storage: SQLiteAdapter):
    if not storage.get_level_names(include_inactive=True):
        for name in DEFAULT_LEVELS:
            storage.create_level(name)


def env_bootstrap_accounts() -> list[tuple[str, str, str]]:
    """Аккаунты, сеемые из AUTH_*-переменных: (роль, логин, пароль).

    admin и editor всегда настроены дефолтами; viewer с пустым логином
    или паролем не создаётся вовсе.
    """
    accounts = [
        (ADMIN_ROLE, settings.auth_admin_username, settings.auth_admin_password),
        (EDITOR_ROLE, settings.auth_editor_username, settings.auth_editor_password),
        (VIEWER_ROLE, settings.auth_viewer_username, settings.auth_viewer_password),
    ]
    return [(role, username, password) for role, username, password in accounts if username and password]


def seed_users(storage: SQLiteAdapter):
    for role, username, password in env_bootstrap_accounts():
        if storage.get_user(username) is None:
            storage.create_user(username, hash_password(password), role)


def find_weak_bootstrap_accounts(storage: SQLiteAdapter) -> list[str]:
    """Роли, чья учётка БУДЕТ посеяна из окружения со слабым паролем (M3).

    Посев не перезаписывает существующие учётки, поэтому слабый пароль в
    .env опасен только пока учётки с таким логином ещё нет.
    """
    return [
        role
        for role, username, password in env_bootstrap_accounts()
        if password in WEAK_BOOTSTRAP_PASSWORDS and storage.get_user(username) is None
    ]


@app.before_server_start
async def init_storage(app: Sanic, _):
    if not Sanic.test_mode and settings.auth_secret_key in INSECURE_SECRET_VALUES:
        raise RuntimeError('AUTH_SECRET_KEY is not configured: set it to a random value in the environment or .env')
    if app.ctx.storage is None:
        app.ctx.storage = SQLiteAdapter(settings.database_path)
    if not Sanic.test_mode:
        # Отказ старта вместо посева учёток со слабым паролем (M3) или с
        # недопустимым логином (L8). В сообщении — только имена переменных
        # окружения, значения (пароли) не раскрываются и не логируются.
        weak_roles = find_weak_bootstrap_accounts(app.ctx.storage)
        if weak_roles:
            weak_vars = ', '.join(BOOTSTRAP_PASSWORD_ENV_VARS[role] for role in weak_roles)
            raise RuntimeError(f'{weak_vars} is not configured: set a strong unique password in .env')
        for role, username, _password in env_bootstrap_accounts():
            if app.ctx.storage.get_user(username) is None and username_error(username):
                raise RuntimeError(
                    f'{BOOTSTRAP_USERNAME_ENV_VARS[role]} contains characters '
                    'that are not allowed in logins (: or control characters)'
                )
    seed_levels(app.ctx.storage)
    seed_users(app.ctx.storage)


@app.on_response
async def static_no_cache(request: Request, response: HTTPResponse):
    """Статика без версионирования в URL: заставляем браузер
    перепроверять файл по Last-Modified, иначе после деплоя дни висит
    старый CSS/JS (симптом: «поломанная тёмная тема»)."""
    if request.path.startswith('/static'):
        response.headers['Cache-Control'] = 'no-cache'


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

    return unauthorized(request)


@app.exception(IsADirectoryError)
async def handle_directory_request(_, exception: IsADirectoryError):
    return text(body='Not Found', status=404)


@app.get('/healthcheck')
def healthcheck(request: Request):
    return text('OK')


# Порядок подключения доменов: относительный порядок маршрутов внутри каждого
# модуля сохранён исходным (внутри students /admin/people/reconcile и
# /admin/people/import регистрируются до динамического /admin/people/<id>).
auth.register(app)
registry.register(app)
calendar.register(app)
students.register(app)
reports.register(app)
admin.register(app)
