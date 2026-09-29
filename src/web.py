"""HTTP-инфраструктура приложения (общие хелперы веб-слоя).

Jinja-окружения, доступ к storage из request.app, разбор форм и GET-параметров,
парсер/форматер дат-диапазонов, CSRF-токены, ответы 401/403 и flash-параметры.
Перенесено из src/main.py без изменения поведения (Architecture v1)."""
import hashlib
import hmac
import re
import secrets
from datetime import datetime
from urllib.parse import parse_qs
from urllib.parse import urlencode

from jinja2 import Environment
from jinja2 import PackageLoader
from jinja2 import select_autoescape
from pandas import isna
from sanic import html
from sanic import redirect
from sanic import Request
from sanic import Sanic
from sanic import text

from src.settings import settings
from src.storage.sqlite import SQLiteAdapter


jinja_env = Environment(
    loader=PackageLoader('src'),
    autoescape=select_autoescape(),
    enable_async=True,
)


# Синхронный клон окружения для error-страниц: хелперы unauthorized/forbidden
# синхронны (их зовут require_* из синхронных проверок ролей), а render()
# основной среды возвращает корутину (enable_async).
jinja_env_sync = Environment(
    loader=PackageLoader('src'),
    autoescape=select_autoescape(),
)


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


def form_key_present(request: Request, key: str) -> bool:
    """Ключ присутствует в теле формы, ДАЖЕ с пустым значением (P7).

    Sanic выбрасывает пустые значения из request.form, а presence-based
    логика правки записи различает «поле прислано пустым» (осознанная
    очистка → NULL/'') и «поле вообще не присылалось» (восстановить из
    существующей записи). Для urlencoded-тел читаем сырое тело с
    keep_blank_values=True — это ровно то, что шлёт браузер; multipart
    (загрузка файлов) presence-based логикой не пользуется.
    """
    if key in request.form:
        return True
    if 'application/x-www-form-urlencoded' not in (request.headers.get('content-type') or ''):
        return False
    try:
        body = request.body.decode()
    except UnicodeDecodeError:
        return False
    return key in parse_qs(body, keep_blank_values=True)


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
jinja_env_sync.globals['csrf_token'] = create_csrf_token


# UI-ответы 401/403 (IA редизайна 2026-09): браузерной навигации (Accept:
# text/html) вместо голого text() отдаём редирект на вход / страницу
# «Доступ запрещён»; программным клиентам (fetch, API-тесты) поведение
# прежнее — строго text() с теми же статусами. CSRF-ответы middleware
# этими хелперами не затрагиваются.


def request_accepts_html(request: Request) -> bool:
    return 'text/html' in (request.headers.get('accept') or '')


def unauthorized(request: Request):
    if request_accepts_html(request):
        return redirect('/login')
    return text(body='Unauthorized', status=401)


def forbidden(request: Request):
    if request_accepts_html(request):
        return html(
            jinja_env_sync.get_template('error403.html').render(request=request),
            status=403,
        )
    return text(body='Forbidden', status=403)


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


def clean_str(value) -> str:
    """Значение ячейки импорта/формы как строка; NaN/None → пустая строка.

    Замечание с живого импорта (docs/feedback-live.md «nan»): pandas
    превращает пустые ячейки Excel в NaN, и str(NaN) давал текст «nan»
    в Поле/Институте/Группе. Любая пустая ячейка теперь — пустое значение.
    """
    if value is None or isna(value):
        return ''
    return str(value).strip()


# Гибридный ввод дат-диапазонов (решение 2026-09-13, docs/data-model-decisions.md
# «Даты-диапазоны: визуально одно, под капотом два»). Одно поле ввода/колонка
# «Дата» разбирается на пару (date, date_to); формат решает парсер:
DATE_RANGE_SINGLE = re.compile(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$')


# «25-27.06.2026» — границы в одном месяце
DATE_RANGE_ONE_MONTH = re.compile(r'^(\d{1,2})-(\d{1,2})\.(\d{1,2})\.(\d{4})$')


# «30.01-01.02.2026» — разные месяцы одного года
DATE_RANGE_ONE_YEAR = re.compile(r'^(\d{1,2})\.(\d{1,2})-(\d{1,2})\.(\d{1,2})\.(\d{4})$')


# «28.12.2025-02.01.2026» — полный формат (любые даты, включая разные годы)
DATE_RANGE_FULL = re.compile(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})-(\d{1,2})\.(\d{1,2})\.(\d{4})$')


def _range_date(day: str, month: str, year: str, label: str) -> datetime:
    try:
        return datetime(int(year), int(month), int(day))
    except ValueError:
        raise ValueError(f'Некорректная дата ({label})') from None


def _match_date_range(raw: str) -> tuple[datetime, datetime | None] | None:
    """Распознать формат диапазона (или однодневной даты). None — не узнано."""
    match = DATE_RANGE_FULL.match(raw)
    if match:
        return (
            _range_date(match.group(1), match.group(2), match.group(3), 'дата начала'),
            _range_date(match.group(4), match.group(5), match.group(6), 'дата окончания'),
        )
    match = DATE_RANGE_ONE_YEAR.match(raw)
    if match:
        year = match.group(5)
        return (
            _range_date(match.group(1), match.group(2), year, 'дата начала'),
            _range_date(match.group(3), match.group(4), year, 'дата окончания'),
        )
    match = DATE_RANGE_ONE_MONTH.match(raw)
    if match:
        month, year = match.group(3), match.group(4)
        return (
            _range_date(match.group(1), month, year, 'дата начала'),
            _range_date(match.group(2), month, year, 'дата окончания'),
        )
    match = DATE_RANGE_SINGLE.match(raw)
    if match:
        return _range_date(*match.groups(), 'дата'), None
    return None


def parse_date_value(value, manual_input: bool = False) -> tuple[datetime, datetime | None]:
    """Разобрать поле «Дата» в пару (date, date_to).

    Поддерживаются однодневное «25.06.2026», компактные диапазоны
    «25-27.06.2026» и «30.01-01.02.2026» и полный «25.06.2026-27.06.2026»
    — один парсер для ручного ввода и колонки «Дата» импорта. Excel-ячейка
    с типом дата приходит объектом datetime (однодневная, импорт). date_to
    раньше date — отказ (маршрут отдаёт 400).
    """
    if isinstance(value, datetime):
        return value, None

    raw = str(value or '').strip()
    if not raw:
        raise ValueError('Дата обязательна')

    parsed = _match_date_range(raw)
    if parsed is None:
        if manual_input:
            # Ручной ввод жёстче: только форматы дд.мм.гггг выше.
            raise ValueError(f'Некорректная дата: {raw}')
        # Легаси-прочтение импорта: текстовая ISO-ячейка Excel.
        try:
            return datetime.fromisoformat(raw), None
        except ValueError:
            raise ValueError(f'Некорректная дата: {raw}') from None

    date, date_to = parsed
    if date_to is not None and date_to < date:
        raise ValueError('Дата окончания не может быть раньше даты начала')
    return date, date_to


def format_date_range(date_from: datetime, date_to: datetime | None) -> str:
    """Компактное отображение диапазона (решение 2026-09-13): совпадающие
    месяц/год — «25-27.06.2026», совпадающий год — «30.01-01.02.2026»,
    разные годы — полный «28.12.2025-02.01.2026»; однодневное — «25.06.2026».
    Используется в таблице, экспорте (колонка «Дата» остаётся реимпортируемой)
    и предзаполнении инлайн-правки."""
    if not date_to or date_to <= date_from:
        return date_from.strftime('%d.%m.%Y')
    if (date_from.year, date_from.month) == (date_to.year, date_to.month):
        return f'{date_from.day:02d}-{date_to.day:02d}.{date_from.month:02d}.{date_from.year}'
    if date_from.year == date_to.year:
        return f'{date_from.day:02d}.{date_from.month:02d}-' f'{date_to.day:02d}.{date_to.month:02d}.{date_from.year}'
    return f'{date_from.strftime("%d.%m.%Y")}-{date_to.strftime("%d.%m.%Y")}'


# Компактное отображение дат-диапазонов в шаблонах (таблица реестра).
jinja_env.globals['format_date_range'] = format_date_range


def checkbox_to_bool(value: str) -> bool:
    return value.lower() in {'1', 'true', 'on', 'yes'}


def parse_checkbox(request: Request, key: str) -> bool:
    return checkbox_to_bool(get_form_value(request, key))


def is_http_url(value: str) -> bool:
    """Ссылка строго со схемой http/https (без прочих схем вроде javascript:)."""
    return bool(re.fullmatch(r'https?://\S+', value))


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


def parse_reconcile_int(raw: str, label: str):
    """Целое из строки формы/URL: (значение, None) или (None, ответ 400).

    Пустая строка — тоже некорректный id (поле обязательное).
    """
    raw = (raw or '').strip()
    if not raw:
        return None, text(body=f'Invalid {label}', status=400)
    try:
        return int(raw), None
    except ValueError:
        return None, text(body=f'Invalid {label}', status=400)
