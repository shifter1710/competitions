"""Маршруты студентов: список/карточки, сопоставление записей и аккаунтов,
импорт студентов из Excel и слияние ФИО. Перенесено из src/main.py без
изменения поведения (Architecture v1)."""
import asyncio
import hashlib
import secrets
import time
from datetime import datetime
from io import BytesIO
from typing import Sequence
from urllib.parse import urlencode

import pandas as pd
from pandas import isna
from sanic import redirect
from sanic import Request
from sanic import Sanic
from sanic import text
from sanic.response import json as json_response
from sanic.response import raw
from sanic_ext import render

from src.auth import get_current_user_id
from src.auth import log_audit_event
from src.auth import require_admin
from src.auth import require_moderator
from src.storage.sqlite import SQLiteAdapter
from src.storage.sqlite import STUDENT_SORT_COLUMNS
from src.web import build_redirect_with_message
from src.web import checkbox_to_bool
from src.web import clean_str
from src.web import get_flash_args
from src.web import get_form_value
from src.web import get_param
from src.web import get_storage
from src.web import jinja_env
from src.web import parse_reconcile_int


# Карточки студентов (Student Identity v1, Phase 1): допустимые значения
# поля «Пол» — только М/Ж или пусто (не указан).
STUDENT_SEX_OPTIONS: frozenset[str] = frozenset({'', 'М', 'Ж'})


# Импорт студентов из Excel (Student Identity v1, Phase 2.5): колонки файла
# (обязательна только «ФИО», остальные опциональны), лимит строк на файл и
# TTL staging-сессии предпросмотра (см. student_import_sessions).
STUDENT_IMPORT_COLUMNS: Sequence[str] = ('ФИО', 'Пол', 'Институт', 'Группа', 'Курс')


STUDENT_IMPORT_MAX_ROWS = 5000


STUDENT_IMPORT_SESSION_TTL_SECONDS = 2 * 60 * 60


RECONCILE_PAGE_SIZE = 50


def parse_student_form(request: Request) -> tuple[dict, str | None]:
    """Поля карточки студента из формы. None-ошибка — текст для редиректа.

    Поле формы группы называется «group» (HTML-конвенция) и маппится в
    group_name хранилища. Все значения — свободный текст со strip().
    """
    full_name = get_form_value(request, 'full_name').strip()
    sex = get_form_value(request, 'sex').strip()
    institute = get_form_value(request, 'institute').strip()
    group_name = get_form_value(request, 'group').strip()
    course = get_form_value(request, 'course').strip()
    if not full_name:
        return {}, 'Укажите ФИО студента.'
    if sex not in STUDENT_SEX_OPTIONS:
        return {}, 'Пол может быть «М», «Ж» или не указан.'
    return {
        'full_name': full_name,
        'sex': sex,
        'institute': institute,
        'group_name': group_name,
        'course': course,
    }, None


def student_field_changes(student: dict, form: dict) -> dict:
    """Diff изменённых полей карточки для аудита: поле → {old, new}."""
    return {
        field: {'old': student[field], 'new': form[field]}
        for field in ('full_name', 'sex', 'institute', 'group_name', 'course')
        if student[field] != form[field]
    }


def resolve_student_id(request: Request, student_id: str):
    """Числовой id карточки из URL: (id, None) или (None, ответ 400)."""
    try:
        return int(student_id), None
    except ValueError:
        return None, text(body='Invalid student id', status=400)


def parse_people_sort(args: dict) -> tuple[str, str]:
    """Сортировка таблицы «Студенты»: sort ∈ ключей STUDENT_SORT_COLUMNS
    (белый список), order ∈ {'asc', 'desc'}. Применяется только полной
    валидной парой; некорректное/пустое любое из значений — дефолтный
    порядок (паттерн parse_index_per_page)."""
    sort = get_param(args, 'sort') or ''
    order = get_param(args, 'order') or ''
    if sort not in STUDENT_SORT_COLUMNS or order not in ('asc', 'desc'):
        return '', ''
    return sort, order


# --- Сопоставление данных (Student Identity v1, Phase 2 + P3 Runtime Identity). ---
#
# Ручное заполнение стабильных связей student_ref_id у СУЩЕСТВУЮЩИХ записей
# и аккаунтов атлетов. Кандидаты на странице — только ПРЕДЛОЖЕНИЯ, ничего
# не связывается автоматически. Привязка меняет ТОЛЬКО student_ref_id:
# снимки данных в записях, легаси-ключ sha256(ФИО), отчёты, импорт/экспорт
# и календарь работают как раньше; кабинет атлета с P3 читает связь
# (dual — вдобавок к легаси-хешу, ref — вместо него; проверка и переключение
# режима — третья вкладка /admin/people/reconcile/identity). Регистрируется
# ДО /admin/people/<student_id>, чтобы статический путь
# /admin/people/reconcile не разбирался как id карточки.


def reconcile_back_url(raw: str) -> str:
    """Возврат внутрь раздела сопоставления: открытые редиректы исключены,
    посторонние пути заменяются вкладкой записей."""
    raw = (raw or '').strip()
    if raw.startswith('/admin/people/reconcile'):
        return raw
    return '/admin/people/reconcile'


def reconcile_student_error(storage: SQLiteAdapter, student_id: int, code: str) -> str:
    """Flash-текст ошибки привязки по коду хранилища (карточка/активность)."""
    student = storage.get_student_by_id(student_id)
    if student is None:
        return 'Студент не найден.'
    if code == 'student_inactive':
        return f'Студент «{student["full_name"]}» неактивен: привязка возможна только к активным студентам.'
    return 'Студент не найден.'


def reconcile_link_error(
    storage: SQLiteAdapter,
    record_ids: list[int],
    student_id: int,
    code: str,
) -> str:
    """Flash-текст ошибки привязки записей (одиночной и массовой)."""
    if code in ('student_not_found', 'student_inactive'):
        return reconcile_student_error(storage, student_id, code)
    if code == 'records_not_found':
        return 'Запись не найдена.'
    if len(record_ids) == 1:
        return f'Запись №{record_ids[0]} уже привязана к студенту.'
    return 'Ничего не привязано: часть выбранных записей уже привязана. Обновите список и повторите.'


def reconcile_create_url(*, record_id: int | None = None, user_id: int | None = None, back: str) -> str:
    """URL страницы создания студента из записи/профиля с возвратом."""
    params = {'back': back}
    if record_id is not None:
        params['record_id'] = record_id
    if user_id is not None:
        params['user_id'] = user_id
    query = urlencode(params)
    if record_id is not None:
        return f'/admin/people/reconcile/create?{query}'
    return f'/admin/people/reconcile/users/create?{query}'


# --- Импорт студентов из Excel (Student Identity v1, Phase 2.5). ---
#
# Загрузка списка студентов с предпросмотром: файл разбирается в память
# (staging-сессия), карточки создаются ТОЛЬКО явным подтверждением строк —
# предпросмотр не пишет в БД ничего. Кандидаты — только ПРЕДЛОЖЕНИЯ:
# точное совпадение с существующей карточкой не создаёт и не связывает
# ничего автоматически. Импорт трогает ТОЛЬКО students + audit_log:
# записи соревнований, аккаунты и student_ref_id не меняются, справочники
# только читаются (подсказки института/группы), ничего в них не сеется.
# Регистрируется ДО /admin/people/<student_id>, чтобы статические пути
# /admin/people/import и /admin/people/import/template не разбирались
# как id карточки.


def normalize_import_course(value) -> str:
    """Курс из ячейки Excel как строка: 2.0 → «2», NaN/None → «».

    Числовые ячейки pandas отдаёт float'ами («2» в Excel → 2.0); целые
    выводятся без дробной части, остальное (текст, нецелые) — как str().
    """
    if value is None or isna(value):
        return ''
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def normalize_student_sex(value: str) -> str | None:
    """Пол из ячейки Excel в каноническом виде: '' → '', м/м → «М», ж/ж → «Ж».

    upper() в Python работает с кириллицей («м» → «М»); латинские m/M не
    совпадают с кириллическими «М»/«Ж» и отвергаются естественно. Всё
    остальное — None (ошибка строки, а не молчаливая правка).
    """
    if value == '':
        return ''
    if value.upper() == 'М':
        return 'М'
    if value.upper() == 'Ж':
        return 'Ж'
    return None


def parse_student_import_rows(df: pd.DataFrame) -> tuple[list[dict], list[str]]:
    """Разобранные строки файла импорта студентов + неизвестные колонки.

    Возвращает строки staging: row_number = index + 2 (заголовок — строка 1)
    и статус pending/error. Значения — clean_str (NaN → пустая строка), курс —
    normalize_import_course, пол — normalize_student_sex (регистр «м»/«ж»
    приводится к «М»/«Ж»). Ошибки строки: пустое ФИО, недопустимый Пол
    (ошибка, а не молчаливая правка). Дубли ФИО в файле здесь НЕ помечаются
    — группы пересчитываются при каждом использовании (правки строк меняют
    их состав естественно).
    """
    columns = [str(column) for column in df.columns]
    unknown_columns = [column for column in columns if column not in STUDENT_IMPORT_COLUMNS]
    rows: list[dict] = []
    for index, record in df.iterrows():
        full_name = clean_str(record.get('ФИО'))
        sex = normalize_student_sex(clean_str(record.get('Пол')))
        institute = clean_str(record.get('Институт'))
        group = clean_str(record.get('Группа'))
        course = normalize_import_course(record.get('Курс'))
        error = None
        if not full_name:
            error = 'Пустое ФИО.'
        elif sex is None:
            error = 'Пол должен быть «М» или «Ж».'
        rows.append(
            {
                'row_number': int(index) + 2,
                'full_name': full_name,
                'sex': sex,
                'institute': institute,
                'group': group,
                'course': course,
                'status': 'error' if error else 'pending',
                'error': error,
                'student_id': None,
            }
        )
    return rows, unknown_columns


def student_import_duplicate_rows(rows: Sequence[dict]) -> dict[int, list[int]]:
    """Одинаковые ФИО в файле: номер строки → номера всех строк группы.

    Группировка по casefold(strip(ФИО)); возвращаются только строки из групп
    с БОЛЕЕ одной строкой. Каждому члену группы виден весь список строк.
    """
    by_name: dict[str, list[int]] = {}
    for row in rows:
        name = (row['full_name'] or '').strip()
        if name:
            by_name.setdefault(name.casefold(), []).append(row['row_number'])
    return {row_number: numbers for numbers in by_name.values() if len(numbers) > 1 for row_number in numbers}


def student_file_group_institutes(rows: Sequence[dict]) -> dict[str, list[dict]]:
    """Карта группа → институты из строк файла импорта (по самой сессии).

    Ключ — casefold(strip(группа)); значение — институты этой группы,
    уникальные по casefold, в порядке появления (сохраняется первое
    написание). Строки без группы или института не участвуют; статус строки
    не важен — карта строится из ВСЕХ строк сессии при каждом рендере,
    поэтому правка строки-«донора» меняет подсказки остальных естественно.
    """
    institutes: dict[str, list[dict]] = {}
    for row in rows:
        group = (row['group'] or '').strip()
        institute = (row['institute'] or '').strip()
        if not group or not institute:
            continue
        bucket = institutes.setdefault(group.casefold(), [])
        if not any(item['cf'] == institute.casefold() for item in bucket):
            bucket.append({'cf': institute.casefold(), 'raw': institute})
    return institutes


def canonical_group_in_institute(storage: SQLiteAdapter, institute: str, group: str) -> str:
    """Каноническое написание группы внутри института (уникальность группы —
    по паре институт+группа); без изменений, если пара справочнику неизвестна."""
    institute_row = storage.find_catalog_row('institute', institute)
    if institute_row is None:
        return group
    canonical = storage.find_catalog_canonical('group', group, parent_id=institute_row['id'])
    return canonical if canonical else group


def student_catalog_group_hints(
    storage: SQLiteAdapter,
    hints: dict,
    group: str,
    file_groups: dict[str, list[dict]] | None = None,
) -> dict:
    """Институт не указан, группа указана: институт — из ТЕКУЩЕГО файла
    (file_groups, если группа встречается в нём ровно с одним институтом),
    иначе из справочника (если группа принадлежит ровно одному институту);
    иначе предупреждение без изменения значений.

    Файл важнее справочника: импорт списков одной группы обычно идёт из
    файла факультета. Расхождение со справочником — предупреждение, значение
    остаётся файловым.
    """
    file_entry = (file_groups or {}).get(group.casefold())
    if file_entry is not None:
        if len(file_entry) > 1:
            hints['warnings'].append('Институт не определён: в файле группа относится к разным институтам')
            return hints
        raw_institute = file_entry[0]['raw']
        institute_value = storage.find_catalog_canonical('institute', raw_institute) or raw_institute
        hints['institute'] = institute_value
        hints['institute_autofilled'] = True
        hints['institute_source'] = 'file'
        hints['group'] = canonical_group_in_institute(storage, institute_value, group)
        catalog_owner = storage.find_unique_group_institute(group)
        if catalog_owner and catalog_owner.casefold() != institute_value.casefold():
            hints['warnings'].append('Требует внимания: справочник относит группу к другому институту')
        return hints
    institute_value = storage.find_unique_group_institute(group)
    if not institute_value:
        hints['warnings'].append(
            'Институт не определён: группа отсутствует в справочнике или относится к нескольким институтам'
        )
        return hints
    hints['institute'] = institute_value
    hints['institute_autofilled'] = True
    hints['institute_source'] = 'catalog'
    hints['group'] = canonical_group_in_institute(storage, institute_value, group)
    return hints


def student_catalog_pair_hints(storage: SQLiteAdapter, hints: dict, institute: str, group: str) -> dict:
    """Институт и группа указаны: канонизация обоих по справочнику; группа,
    однозначно принадлежащая ДРУГОМУ институту — предупреждение, значения
    не переписываются."""
    canonical_institute = storage.find_catalog_canonical('institute', institute)
    if canonical_institute:
        hints['institute'] = canonical_institute
        hints['group'] = canonical_group_in_institute(storage, canonical_institute, group)
    owner = storage.find_unique_group_institute(group)
    if owner and owner.lower() != hints['institute'].lower():
        hints['warnings'].append('Требует внимания: группа относится к другому институту')
    return hints


def student_catalog_hints(
    storage: SQLiteAdapter,
    institute: str,
    group: str,
    file_groups: dict[str, list[dict]] | None = None,
) -> dict:
    """Подсказки справочников для строки импорта студентов — ТОЛЬКО ЧТЕНИЕ.

    Возвращает значения (возможно канонизированные по регистру), признак
    автозаполнения института (institute_source: 'file' — из текущего файла,
    'catalog' — из справочника) и предупреждения. Импорт студентов НИЧЕГО
    не пишет в справочники (в отличие от импорта записей соревнований).
    """
    hints = {
        'institute': institute,
        'group': group,
        'institute_autofilled': False,
        'institute_source': None,
        'warnings': [],
    }

    if not institute and group:
        return student_catalog_group_hints(storage, hints, group, file_groups=file_groups)
    if institute and group:
        return student_catalog_pair_hints(storage, hints, institute, group)
    if institute:
        canonical_institute = storage.find_catalog_canonical('institute', institute)
        if canonical_institute:
            hints['institute'] = canonical_institute
    return hints


# Staging предпросмотра живёт в памяти (прецедент login_failures): в БД не
# пишется НИЧЕГО до подтверждения строк. Приложение однопроцессное, поэтому
# словарь процесса — корректное хранилище; рестарт просто теряет сессии
# (безопасно — админ загрузит файл заново). Один файл на админа: новая
# загрузка заменяет прежнюю сессию. ФИО из файла не логируются.
student_import_sessions: dict[str, dict] = {}


def sweep_student_import_sessions() -> None:
    """Удалить просроченные сессии (TTL); вызывается при каждом обращении."""
    now = time.time()
    expired = [
        token
        for token, session in student_import_sessions.items()
        if now - session['created_at'] > STUDENT_IMPORT_SESSION_TTL_SECONDS
    ]
    for token in expired:
        student_import_sessions.pop(token, None)


def get_student_import_session(request: Request, token: str):
    """Сессия предпросмотра для запроса: (session, None) или (None, redirect).

    Неизвестный/просроченный токен и чужая сессия — одно и то же поведение:
    возврат на страницу загрузки с flash о истечении сессии.
    """
    sweep_student_import_sessions()
    session = student_import_sessions.get(token)
    if session is None or session['user_id'] != get_current_user_id(request):
        return None, build_redirect_with_message(
            error='Время сессии предпросмотра истекло. Загрузите файл заново.',
            url='/admin/people/import',
        )
    return session, None


def student_import_preview_url(token: str) -> str:
    return f'/admin/people/import/preview/{token}'


def student_import_row(session: dict, raw_row_number: str):
    """Строка сессии по номеру из URL: (row, None) или (None, ответ 400)."""
    row_number, error = parse_reconcile_int(raw_row_number, 'row number')
    if error is not None:
        return None, error
    for row in session['rows']:
        if row['row_number'] == row_number:
            return row, None
    return None, text(body='Invalid row number', status=400)


def student_import_bulk_rows(session: dict) -> list[dict]:
    """Строки массового создания: pending без дублей ФИО в файле и БЕЗ
    кандидатов на момент последнего рендера предпросмотра (признак
    had_candidates фиксируется контекстом предпросмотра) — ровно группа
    «Новые». Неразобранные строки с кандидатами в батч НЕ входят и
    отмену не вызывают; повторная проверка в bulk-create отменяет всё,
    только если у «чистой» строки совпадения ПОЯВИЛИСЬ с момента рендера."""
    duplicates = student_import_duplicate_rows(session['rows'])
    return [
        row
        for row in session['rows']
        if row['status'] == 'pending' and row['row_number'] not in duplicates and not row.get('had_candidates')
    ]


def student_import_preview_context(storage: SQLiteAdapter, session: dict) -> dict:
    """Контекст предпросмотра. Всё производное (кандидаты, подсказки
    справочников, группы дублей, категории, счётчики) пересчитывается при
    каждом рендере; в сессии хранятся строки файла, решения и фиксируемый
    рендером признак had_candidates (см. student_import_bulk_rows)."""
    duplicates = student_import_duplicate_rows(session['rows'])
    # Карта группа → институты текущего файла строится один раз на рендер —
    # и предпросмотр, и создание используют один и тот же источник данных.
    file_groups = student_file_group_institutes(session['rows'])
    groups: dict[str, list[dict]] = {'new': [], 'review': [], 'error': [], 'resolved': []}
    resolved_counts = {'created': 0, 'reused_existing': 0, 'skipped': 0}
    pending = 0
    for row in session['rows']:
        status = row['status']
        if status == 'pending':
            pending += 1
        elif status in resolved_counts:
            resolved_counts[status] += 1
        view = {**row, 'candidates': [], 'hints': None, 'duplicate_rows': duplicates.get(row['row_number'])}
        if status == 'pending':
            view['candidates'] = storage.find_student_candidates(row['full_name'])
            view['hints'] = student_catalog_hints(storage, row['institute'], row['group'], file_groups)
            # Классификация рендера фиксируется в строке сессии: массовое
            # создание (bulk-create) берёт только «чистые» строки (без
            # кандидатов на момент предпросмотра) и отменяется целиком,
            # только если у такой строки совпадения появились ПОЗЖЕ. После
            # правки строки следующий рендер пересчитает признак естественно.
            row['had_candidates'] = bool(view['candidates'])
            category = 'review' if (view['candidates'] or view['duplicate_rows']) else 'new'
        elif status == 'error':
            category = 'error'
        else:
            category = 'resolved'
        groups[category].append(view)
    return {
        'token': session['token'],
        'filename': session['filename'],
        'unknown_columns': session['unknown_columns'],
        'groups': groups,
        'total': len(session['rows']),
        'pending': pending,
        'new_count': len(groups['new']),
        'review_count': len(groups['review']),
        'error_count': len(groups['error']),
        'resolved_count': len(groups['resolved']),
        'created_count': resolved_counts['created'],
        'reused_count': resolved_counts['reused_existing'],
        'skipped_count': resolved_counts['skipped'],
    }


def student_import_counters(session: dict) -> dict[str, int]:
    """Итоги сессии для аудита/сообщений: created/reused_existing/skipped/
    errors/total (по статусам строк)."""
    counters = {'created': 0, 'reused_existing': 0, 'skipped': 0, 'errors': 0}
    for row in session['rows']:
        if row['status'] == 'error':
            counters['errors'] += 1
        elif row['status'] in counters:
            counters[row['status']] += 1
    counters['total'] = len(session['rows'])
    return counters


def create_imported_student(
    request: Request,
    storage: SQLiteAdapter,
    row: dict,
    file_groups: dict[str, list[dict]] | None = None,
) -> int:
    """Создать карточку по строке импорта с подсказками справочников.

    Создаётся ровно то, что показано в предпросмотре: институт/группа —
    после канонизации и автозаполнения по группе (student_catalog_hints,
    с той же картой группа→институты текущего файла — file_groups).
    """
    hints = student_catalog_hints(storage, row['institute'], row['group'], file_groups)
    student_id = storage.create_student(
        row['full_name'],
        row['sex'],
        hints['institute'],
        hints['group'],
        row['course'],
    )
    row['status'] = 'created'
    row['student_id'] = student_id
    log_audit_event(
        request,
        'student_created',
        {'student_id': student_id, 'full_name': row['full_name'], 'source': 'excel-import'},
    )
    return student_id


def student_import_upload_error(df: pd.DataFrame) -> str | None:
    """Ошибки файла на уровне колонок/объёма; None — файл пригоден."""
    if 'ФИО' not in {str(column) for column in df.columns}:
        return 'В файле нет обязательной колонки «ФИО». Скачайте шаблон и заполните его.'
    if len(df) > STUDENT_IMPORT_MAX_ROWS:
        return f'В файле больше {STUDENT_IMPORT_MAX_ROWS} строк. Разбейте список на части.'
    if df.empty:
        return 'В файле нет строк с данными.'
    return None


def replace_student_import_session(user_id: int, token: str, session: dict) -> None:
    """Сохранить новую сессию предпросмотра; прежняя сессия того же админа
    заменяется (один файл на админа)."""
    sweep_student_import_sessions()
    for existing_token, existing in list(student_import_sessions.items()):
        if existing['user_id'] == user_id:
            student_import_sessions.pop(existing_token, None)
    student_import_sessions[token] = session


def register(app: Sanic) -> None:
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


    @app.get('/api/students')
    async def list_students(request: Request):
        # Полный список ФИО раскрывает персональные данные других студентов —
        # доступен только admin/editor (см. docs/data-model-decisions.md).
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        names = get_storage(request.app).get_student_names()
        return json_response(names)


    @app.get('/admin/people')
    async def admin_people_page(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        storage = get_storage(request.app)
        args = dict(request.args)
        search = (get_param(args, 'q') or '').strip()
        sort, order = parse_people_sort(args)
        students = storage.list_students(search=search, sort=sort, order=order)
        total = len(storage.list_students())

        # Ссылки сортировки сохраняют текущий q (urlencode: пробелы и кириллица,
        # паттерн page_url сопоставления). Двухпозиционное переключение: неактивная
        # колонка — asc, активная — смена направления; третий «несортированный»
        # состояние отсутствует, возврат к дефолту — «Сброс».
        def sort_url(column: str) -> str:
            next_order = 'desc' if column == sort and order == 'asc' else 'asc'
            params = {'sort': column, 'order': next_order}
            if search:
                params['q'] = search
            return f'/admin/people?{urlencode(params)}'

        return await render(
            template_name=jinja_env.get_template('admin_people.html'),
            context={
                'request': request,
                'students': students,
                'q': search,
                'total': total,
                'sort': sort,
                'order': order,
                'sort_urls': {column: sort_url(column) for column in STUDENT_SORT_COLUMNS},
                'reconcile_unlinked': storage.count_student_reconciliation()['records_unlinked'],
                **get_flash_args(request),
            },
        )


    @app.post('/admin/people')
    async def admin_person_create(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        form, form_error = parse_student_form(request)
        if form_error is not None:
            return build_redirect_with_message(error=form_error, url='/admin/people')

        storage = get_storage(request.app)
        student_id = storage.create_student(
            form['full_name'],
            form['sex'],
            form['institute'],
            form['group_name'],
            form['course'],
        )
        log_audit_event(request, 'student_created', {'student_id': student_id, 'full_name': form['full_name']})
        return build_redirect_with_message(
            message=f'Студент «{form["full_name"]}» добавлен.',
            url=f'/admin/people/{student_id}',
        )


    @app.get('/admin/people/reconcile')
    async def admin_reconcile_page(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        storage = get_storage(request.app)
        q = (get_param(dict(request.args), 'q') or '').strip()
        try:
            page = max(1, int(get_param(dict(request.args), 'page') or 1))
        except ValueError:
            page = 1

        counters = storage.count_student_reconciliation()
        total = storage.count_unlinked_competitions(search=q)
        pages = max(1, -(-total // RECONCILE_PAGE_SIZE))
        page = min(page, pages)
        records = storage.list_unlinked_competitions(
            search=q, limit=RECONCILE_PAGE_SIZE, offset=(page - 1) * RECONCILE_PAGE_SIZE
        )
        # Кандидаты — только для текущей страницы (точное совпадение ФИО).
        for record in records:
            record['candidates'] = storage.find_student_candidates(record['student_name'])
            # Даты — datetime для format_date_range в шаблоне.
            record['date'] = datetime.fromisoformat(record['date']) if record['date'] else None
            record['date_to'] = datetime.fromisoformat(record['date_to']) if record['date_to'] else None
        active_students = [student for student in storage.list_students() if student['active']]

        query = urlencode({'q': q}) if q else ''

        def page_url(page_number: int) -> str:
            return (
                f'/admin/people/reconcile?{query}&page={page_number}'
                if query
                else f'/admin/people/reconcile?page={page_number}'
            )

        return await render(
            template_name=jinja_env.get_template('admin_reconcile.html'),
            context={
                'request': request,
                'counters': counters,
                'hash_only_total': storage.count_hash_only_visible(),
                'q': q,
                'records': records,
                'active_students': active_students,
                'page': page,
                'pages': pages,
                'total': total,
                'page_size': RECONCILE_PAGE_SIZE,
                'back_url': page_url(page),
                'prev_url': page_url(page - 1) if page > 1 else None,
                'next_url': page_url(page + 1) if page < pages else None,
                **get_flash_args(request),
            },
        )


    @app.get('/admin/people/reconcile/users')
    async def admin_reconcile_users_page(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        storage = get_storage(request.app)
        counters = storage.count_student_reconciliation()
        identity_mode = storage.get_identity_mode()
        users = storage.list_unlinked_athlete_users()
        # Кандидаты — по ФИО из профиля аккаунта; псевдонимы аккаунта НЕ участвуют.
        # P3: предпросмотры видимости кабинета — «сейчас K», у кандидата —
        # «после привязки N» (считается текущим режимом dual/ref).
        for user in users:
            user['candidates'] = storage.find_student_candidates(user['profile_data'].get('student_name') or '')
            hashes = storage.athlete_name_hashes(user['id'])
            user['visible_now'] = storage.count_competitions_visible(
                owner_id=user['id'],
                student_id_hashes=hashes,
                identity_mode=identity_mode,
            )
            for candidate in user['candidates']:
                candidate['visible_after'] = storage.count_competitions_visible(
                    owner_id=user['id'],
                    student_ref_id=candidate['student_id'],
                    student_id_hashes=hashes,
                    identity_mode=identity_mode,
                )

        return await render(
            template_name=jinja_env.get_template('admin_reconcile_users.html'),
            context={
                'request': request,
                'counters': counters,
                'hash_only_total': storage.count_hash_only_visible(),
                'identity_mode': identity_mode,
                'users': users,
                'back_url': '/admin/people/reconcile/users',
                **get_flash_args(request),
            },
        )


    @app.post('/admin/people/reconcile/link')
    async def admin_reconcile_link(request: Request):
        """Привязка записей к карточке: одна точка для одиночной (кнопка
        кандидата) и массовой (галочки + список студентов) привязки."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        raw_ids = [value for value in request.form.getlist('record_ids[]') if str(value).strip()]
        numeric_ids: list[int] = []
        for raw_id in raw_ids:
            numeric_id, id_error = parse_reconcile_int(str(raw_id), 'record id')
            if id_error is not None:
                return id_error
            numeric_ids.append(numeric_id)

        back = reconcile_back_url(get_form_value(request, 'back'))
        if not numeric_ids:
            return build_redirect_with_message(error='Не выбрано ни одной записи.', url=back)
        student_id_raw = get_form_value(request, 'student_id').strip()
        if not student_id_raw:
            return build_redirect_with_message(error='Выберите студента.', url=back)
        student_id, id_error = parse_reconcile_int(student_id_raw, 'student id')
        if id_error is not None:
            return id_error

        storage = get_storage(request.app)
        count, error = storage.link_competitions(numeric_ids, student_id)
        if error is not None:
            return build_redirect_with_message(
                error=reconcile_link_error(storage, numeric_ids, student_id, error), url=back
            )

        student = storage.get_student_by_id(student_id)
        full_name = student['full_name'] if student else ''
        if len(numeric_ids) == 1 and count == 1:
            log_audit_event(
                request,
                'competition_linked_to_student',
                {'record_id': numeric_ids[0], 'old_ref': None, 'new_ref': student_id, 'student_id': student_id},
            )
            message = f'Запись №{numeric_ids[0]} привязана к студенту «{full_name}».'
        else:
            # Одна запись аудита на всю массовую привязку.
            log_audit_event(
                request,
                'student_records_bulk_linked',
                {'student_id': student_id, 'record_ids': numeric_ids, 'count': count},
            )
            message = f'Привязано записей: {count} — студент «{full_name}».'
        return build_redirect_with_message(message=message, url=back)


    @app.get('/admin/people/reconcile/create')
    async def admin_reconcile_create_page(request: Request):
        """Создание карточки из записи: поля предзаполнены снимком записи."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        record_id, id_error = parse_reconcile_int(get_param(dict(request.args), 'record_id') or '', 'record id')
        if id_error is not None:
            return id_error
        back = reconcile_back_url(get_param(dict(request.args), 'back') or '')

        storage = get_storage(request.app)
        record = storage.get_competition_by_id(record_id)
        found, student_ref = storage.get_competition_student_ref(record_id)
        if record is None or not found:
            return build_redirect_with_message(error='Запись не найдена.', url=back)
        if student_ref is not None:
            return build_redirect_with_message(
                error=f'Запись №{record_id} уже привязана к студенту.',
                url=back,
            )

        return await render(
            template_name=jinja_env.get_template('admin_reconcile_create.html'),
            context={
                'request': request,
                'source': 'record',
                'record': record,
                'record_id': record_id,
                'form': {
                    'full_name': record.student_name,
                    'sex': record.student_sex or '',
                    'institute': record.institute or '',
                    'group_name': record.group or '',
                    'course': '' if record.course is None else str(record.course),
                },
                'back_url': back,
                **get_flash_args(request),
            },
        )


    @app.post('/admin/people/reconcile/create')
    async def admin_reconcile_create_record(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        record_id, id_error = parse_reconcile_int(get_form_value(request, 'record_id'), 'record id')
        if id_error is not None:
            return id_error
        back = reconcile_back_url(get_form_value(request, 'back'))
        form, form_error = parse_student_form(request)
        if form_error is not None:
            return build_redirect_with_message(
                error=form_error,
                url=reconcile_create_url(record_id=record_id, back=back),
            )

        storage = get_storage(request.app)
        record = storage.get_competition_by_id(record_id)
        found, student_ref = storage.get_competition_student_ref(record_id)
        if record is None or not found:
            return build_redirect_with_message(error='Запись не найдена.', url=back)
        if student_ref is not None:
            return build_redirect_with_message(
                error=f'Запись №{record_id} уже привязана к студенту.',
                url=back,
            )

        student_id = storage.create_student(
            form['full_name'],
            form['sex'],
            form['institute'],
            form['group_name'],
            form['course'],
        )
        log_audit_event(
            request,
            'student_created',
            {'student_id': student_id, 'full_name': form['full_name'], 'source': 'reconciliation-record'},
        )
        _, link_error = storage.link_competitions([record_id], student_id)
        if link_error is not None:
            # Гонка: карточка уже создана (и остаётся), запись связал кто-то другой.
            if link_error == 'already_linked':
                message = (
                    f'Студент «{form["full_name"]}» создан, но запись №{record_id} уже была привязана другим действием.'
                )
            else:
                message = f'Студент «{form["full_name"]}» создан, но запись №{record_id} не найдена.'
            return build_redirect_with_message(message=message, url=f'/admin/people/{student_id}')
        log_audit_event(
            request,
            'competition_linked_to_student',
            {'record_id': record_id, 'old_ref': None, 'new_ref': student_id, 'student_id': student_id},
        )
        return build_redirect_with_message(
            message=f'Студент «{form["full_name"]}» создан, запись №{record_id} привязана к нему.',
            url=f'/admin/people/{student_id}',
        )


    @app.post('/admin/people/reconcile/unlink')
    async def admin_reconcile_unlink(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        record_id, id_error = parse_reconcile_int(get_form_value(request, 'record_id'), 'record id')
        if id_error is not None:
            return id_error

        storage = get_storage(request.app)
        found, student_ref = storage.get_competition_student_ref(record_id)
        if not found:
            return build_redirect_with_message(error='Запись не найдена.', url='/admin/people/reconcile')
        if student_ref is None:
            return build_redirect_with_message(
                error=f'Запись №{record_id} не привязана к студенту.',
                url='/admin/people/reconcile',
            )

        old_ref = storage.unlink_competition(record_id)
        if old_ref is None:
            return build_redirect_with_message(
                error=f'Запись №{record_id} не привязана к студенту.',
                url='/admin/people/reconcile',
            )
        student = storage.get_student_by_id(old_ref)
        full_name = student['full_name'] if student else ''
        log_audit_event(
            request,
            'competition_unlinked_from_student',
            {'record_id': record_id, 'old_ref': old_ref},
        )
        return build_redirect_with_message(
            message=f'Запись №{record_id} отвязана от студента «{full_name}».',
            url=f'/admin/people/{old_ref}',
        )


    @app.post('/admin/people/reconcile/relink')
    async def admin_reconcile_relink(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        record_id, id_error = parse_reconcile_int(get_form_value(request, 'record_id'), 'record id')
        if id_error is not None:
            return id_error
        student_id, id_error = parse_reconcile_int(get_form_value(request, 'student_id'), 'student id')
        if id_error is not None:
            return id_error

        storage = get_storage(request.app)
        old_ref, error = storage.relink_competition(record_id, student_id)
        if error is not None:
            if error == 'records_not_found':
                return build_redirect_with_message(error='Запись не найдена.', url='/admin/people/reconcile')
            if error in ('student_not_found', 'student_inactive'):
                # Возврат к карточке, где кнопка: прежняя связь записи ещё цела.
                _, current_ref = storage.get_competition_student_ref(record_id)
                url = f'/admin/people/{current_ref}' if current_ref else '/admin/people/reconcile'
                return build_redirect_with_message(error=reconcile_student_error(storage, student_id, error), url=url)

        student = storage.get_student_by_id(student_id)
        full_name = student['full_name'] if student else ''
        log_audit_event(
            request,
            'competition_relinked',
            {'record_id': record_id, 'old_ref': old_ref, 'new_ref': student_id, 'student_id': student_id},
        )
        return build_redirect_with_message(
            message=f'Запись №{record_id} перепривязана на студента «{full_name}».',
            url=f'/admin/people/{student_id}',
        )


    @app.get('/admin/people/reconcile/users/create')
    async def admin_reconcile_user_create_page(request: Request):
        """Создание карточки из профиля аккаунта атлета: псевдонимы аккаунта
        НЕ переносятся — у карточки будут только данные профиля."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        user_id, id_error = parse_reconcile_int(get_param(dict(request.args), 'user_id') or '', 'user id')
        if id_error is not None:
            return id_error
        back = reconcile_back_url(get_param(dict(request.args), 'back') or '')

        storage = get_storage(request.app)
        user = storage.get_unlinked_athlete_user(user_id)
        if user is None:
            legacy_user = storage.get_user_by_id(user_id)
            if legacy_user is None:
                return build_redirect_with_message(error='Пользователь не найден.', url=back)
            if legacy_user['role'] != 'athlete':
                return build_redirect_with_message(error='Пользователь не является атлетом.', url=back)
            return build_redirect_with_message(
                error=f'Аккаунт {legacy_user["username"]} уже привязан к студенту.',
                url=back,
            )

        profile = user['profile_data']
        return await render(
            template_name=jinja_env.get_template('admin_reconcile_create.html'),
            context={
                'request': request,
                'source': 'user',
                'user': user,
                'user_id': user_id,
                'form': {
                    'full_name': str(profile.get('student_name') or ''),
                    'sex': str(profile.get('student_sex') or ''),
                    'institute': str(profile.get('institute') or ''),
                    'group_name': str(profile.get('group') or ''),
                    'course': str(profile.get('course') or ''),
                },
                'back_url': back,
                **get_flash_args(request),
            },
        )


    @app.post('/admin/people/reconcile/users/create')
    async def admin_reconcile_create_user(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        user_id, id_error = parse_reconcile_int(get_form_value(request, 'user_id'), 'user id')
        if id_error is not None:
            return id_error
        back = reconcile_back_url(get_form_value(request, 'back'))
        form, form_error = parse_student_form(request)
        if form_error is not None:
            return build_redirect_with_message(
                error=form_error,
                url=reconcile_create_url(user_id=user_id, back=back),
            )

        storage = get_storage(request.app)
        user = storage.get_unlinked_athlete_user(user_id)
        if user is None:
            legacy_user = storage.get_user_by_id(user_id)
            if legacy_user is None:
                return build_redirect_with_message(error='Пользователь не найден.', url=back)
            if legacy_user['role'] != 'athlete':
                return build_redirect_with_message(error='Пользователь не является атлетом.', url=back)
            return build_redirect_with_message(
                error=f'Аккаунт {legacy_user["username"]} уже привязан к студенту.',
                url=back,
            )

        student_id = storage.create_student(
            form['full_name'],
            form['sex'],
            form['institute'],
            form['group_name'],
            form['course'],
        )
        log_audit_event(
            request,
            'student_created',
            {'student_id': student_id, 'full_name': form['full_name'], 'source': 'reconciliation-user'},
        )
        _, link_error = storage.link_user(user_id, student_id)
        if link_error is not None:
            if link_error == 'user_already_linked':
                message = (
                    f'Студент «{form["full_name"]}» создан, но аккаунт {user["username"]} уже был '
                    'привязан другим действием.'
                )
            else:
                message = f'Студент «{form["full_name"]}» создан, но аккаунт {user["username"]} недоступен для привязки.'
            return build_redirect_with_message(message=message, url=f'/admin/people/{student_id}')
        log_audit_event(
            request,
            'user_linked_to_student',
            {'user_id': user_id, 'username': user['username'], 'old_ref': None, 'new_ref': student_id},
        )
        return build_redirect_with_message(
            message=f'Студент «{form["full_name"]}» создан, аккаунт {user["username"]} привязан к нему.',
            url=f'/admin/people/{student_id}',
        )


    @app.post('/admin/people/reconcile/users/link')
    async def admin_reconcile_user_link(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        user_id, id_error = parse_reconcile_int(get_form_value(request, 'user_id'), 'user id')
        if id_error is not None:
            return id_error
        back = reconcile_back_url(get_form_value(request, 'back'))
        student_id_raw = get_form_value(request, 'student_id').strip()
        if not student_id_raw:
            return build_redirect_with_message(error='Выберите студента.', url=back)
        student_id, id_error = parse_reconcile_int(student_id_raw, 'student id')
        if id_error is not None:
            return id_error

        storage = get_storage(request.app)
        _, error = storage.link_user(user_id, student_id)
        if error is not None:
            if error == 'user_not_found':
                return build_redirect_with_message(error='Пользователь не найден.', url=back)
            if error == 'user_not_athlete':
                return build_redirect_with_message(error='Пользователь не является атлетом.', url=back)
            if error == 'user_already_linked':
                user = storage.get_user_by_id(user_id)
                username = user['username'] if user else ''
                return build_redirect_with_message(
                    error=f'Аккаунт {username} уже привязан к студенту.',
                    url=back,
                )
            return build_redirect_with_message(error=reconcile_student_error(storage, student_id, error), url=back)

        user = storage.get_user_by_id(user_id)
        username = user['username'] if user else ''
        student = storage.get_student_by_id(student_id)
        full_name = student['full_name'] if student else ''
        log_audit_event(
            request,
            'user_linked_to_student',
            {'user_id': user_id, 'username': username, 'old_ref': None, 'new_ref': student_id},
        )
        return build_redirect_with_message(
            message=f'Аккаунт {username} привязан к студенту «{full_name}».',
            url=back,
        )


    @app.post('/admin/people/reconcile/users/unlink')
    async def admin_reconcile_user_unlink(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        user_id, id_error = parse_reconcile_int(get_form_value(request, 'user_id'), 'user id')
        if id_error is not None:
            return id_error

        storage = get_storage(request.app)
        user = storage.get_user_by_id(user_id)
        if user is None:
            return build_redirect_with_message(error='Пользователь не найден.', url='/admin/people/reconcile/users')
        old_ref = storage.unlink_user(user_id)
        if old_ref is None:
            return build_redirect_with_message(
                error=f'Аккаунт {user["username"]} не привязан к студенту.',
                url='/admin/people/reconcile/users',
            )
        student = storage.get_student_by_id(old_ref)
        full_name = student['full_name'] if student else ''
        log_audit_event(
            request,
            'user_unlinked_from_student',
            {'user_id': user_id, 'username': user['username'], 'old_ref': old_ref, 'new_ref': None},
        )
        return build_redirect_with_message(
            message=f'Аккаунт {user["username"]} отвязан от студента «{full_name}».',
            url=f'/admin/people/{old_ref}',
        )


    @app.post('/admin/people/reconcile/users/relink')
    async def admin_reconcile_user_relink(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        user_id, id_error = parse_reconcile_int(get_form_value(request, 'user_id'), 'user id')
        if id_error is not None:
            return id_error
        student_id, id_error = parse_reconcile_int(get_form_value(request, 'student_id'), 'student id')
        if id_error is not None:
            return id_error

        storage = get_storage(request.app)
        old_ref, error = storage.relink_user(user_id, student_id)
        if error is not None:
            if error == 'user_not_found':
                return build_redirect_with_message(error='Пользователь не найден.', url='/admin/people/reconcile/users')
            if error == 'user_not_athlete':
                return build_redirect_with_message(
                    error='Пользователь не является атлетом.',
                    url='/admin/people/reconcile/users',
                )
            return build_redirect_with_message(
                error=reconcile_student_error(storage, student_id, error), url=f'/admin/people/{student_id}'
            )

        user = storage.get_user_by_id(user_id)
        username = user['username'] if user else ''
        student = storage.get_student_by_id(student_id)
        full_name = student['full_name'] if student else ''
        log_audit_event(
            request,
            'user_relinked_to_student',
            {'user_id': user_id, 'username': username, 'old_ref': old_ref, 'new_ref': student_id},
        )
        return build_redirect_with_message(
            message=f'Аккаунт {username} перепривязан на студента «{full_name}».',
            url=f'/admin/people/{student_id}',
        )


    @app.get('/admin/people/reconcile/identity')
    async def admin_reconcile_identity_page(request: Request):
        """P3 Runtime Identity: отчёт проверки идентификации и переключение
        режима видимости кабинета атлета (dual/ref).

        Guard без waiver: переход на «только связи» блокируется, пока хоть одна
        запись видна атлету только по легаси-хешу ФИО (hash-only)."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        storage = get_storage(request.app)
        try:
            page = max(1, int(get_param(dict(request.args), 'page') or 1))
        except ValueError:
            page = 1

        def fetch(page_number: int) -> dict:
            return storage.identity_verification_data(
                users_limit=RECONCILE_PAGE_SIZE,
                users_offset=(page_number - 1) * RECONCILE_PAGE_SIZE,
            )

        data = fetch(page)
        pages = max(1, -(-data['users_total'] // RECONCILE_PAGE_SIZE))
        # Страница вне диапазона зажимается на последнюю (как в сопоставлении).
        if page > pages:
            page = pages
            data = fetch(page)
        # Даты — datetime для format_date_range в шаблоне.
        for item in data['disappearing']:
            item['date'] = datetime.fromisoformat(item['date']) if item['date'] else None
            item['date_to'] = datetime.fromisoformat(item['date_to']) if item['date_to'] else None

        def page_url(page_number: int) -> str:
            return f'/admin/people/reconcile/identity?page={page_number}'

        return await render(
            template_name=jinja_env.get_template('admin_reconcile_identity.html'),
            context={
                'request': request,
                'counters': storage.count_student_reconciliation(),
                'identity_mode': storage.get_identity_mode(),
                'hash_only_total': data['hash_only_total'],
                'users': data['users'],
                'disappearing': data['disappearing'],
                'disappearing_total': data['disappearing_total'],
                'page': page,
                'pages': pages,
                'prev_url': page_url(page - 1) if page > 1 else None,
                'next_url': page_url(page + 1) if page < pages else None,
                **get_flash_args(request),
            },
        )


    @app.post('/admin/people/reconcile/identity/flip')
    async def admin_reconcile_identity_flip(request: Request):
        """Переключение режима идентификации (admin). На «только связи» — только
        через guard (hash-only счётчик 0); на двойной режим — всегда. CSRF
        проверяет общий middleware POST-запросов."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        target = get_form_value(request, 'mode').strip()
        if target not in ('dual', 'ref'):
            return text(body='Unknown identity mode', status=400)

        storage = get_storage(request.app)
        old_mode = storage.get_identity_mode()
        ok, hash_only = storage.set_identity_mode_guarded(target)
        if not ok:
            # Отказ guard'а: ничего не изменилось, аудита нет — только flash
            # с числом блокирующих записей.
            return build_redirect_with_message(
                error=(
                    f'Переход на «только связи» заблокирован: {hash_only} записей видны атлетам '
                    'только по ФИО. Привяжите их к карточкам студентов в сопоставлении.'
                ),
                url='/admin/people/reconcile/identity',
            )

        log_audit_event(
            request,
            'identity_mode_changed',
            {'old': old_mode, 'new': target, 'hash_only_visible': hash_only},
        )
        if target == 'ref':
            message = 'Режим переключён на «только связи» (ref): кабинеты атлетов не потеряли ни одной записи.'
        else:
            message = 'Режим возвращён на двойной (dual): кабинеты атлетов снова учитывают совпадения по ФИО.'
        return build_redirect_with_message(message=message, url='/admin/people/reconcile/identity')


    @app.get('/admin/people/import')
    async def admin_student_import_page(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        return await render(
            template_name=jinja_env.get_template('admin_people_import.html'),
            context={
                'request': request,
                **get_flash_args(request),
            },
        )


    @app.get('/admin/people/import/template')
    async def export_student_import_template(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        df = pd.DataFrame(columns=list(STUDENT_IMPORT_COLUMNS))
        buffer = BytesIO()
        await asyncio.to_thread(df.to_excel, buffer, index=False)

        now_str = datetime.utcnow().strftime('%d-%m-%Y_%H-%M-%S')
        return raw(
            buffer.getvalue(),
            headers={
                'content-type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'content-disposition': f'attachment; filename="Шаблон_студенты_{now_str}.xlsx"',
            },
        )


    @app.post('/admin/people/import')
    async def admin_student_import_upload(request: Request):
        """Загрузка файла: разбор → staging в памяти → предпросмотр.

        Ошибки файла — возврат на страницу загрузки с flash; в БД не пишется
        ничего.
        """
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        upload_file = request.files.get('file')
        if upload_file is None or not upload_file.body:
            return build_redirect_with_message(error='Выберите файл.', url='/admin/people/import')
        if not (upload_file.name or '').lower().endswith('.xlsx'):
            return build_redirect_with_message(
                error='Файл должен быть в формате .xlsx.',
                url='/admin/people/import',
            )

        try:
            df = await asyncio.to_thread(pd.read_excel, io=upload_file.body)
        except Exception:
            return build_redirect_with_message(
                error='Не удалось прочитать файл. Проверьте, что это не повреждённый .xlsx.',
                url='/admin/people/import',
            )

        upload_error = student_import_upload_error(df)
        if upload_error is not None:
            return build_redirect_with_message(error=upload_error, url='/admin/people/import')

        rows, unknown_columns = await asyncio.to_thread(parse_student_import_rows, df)

        token = secrets.token_urlsafe(16)
        user_id = get_current_user_id(request)
        replace_student_import_session(
            user_id,
            token,
            {
                'token': token,
                'user_id': user_id,
                'created_at': time.time(),
                'filename': upload_file.name or '',
                'unknown_columns': unknown_columns,
                'rows': rows,
            },
        )
        return redirect(student_import_preview_url(token))


    @app.get('/admin/people/import/preview/<token>')
    async def admin_student_import_preview(request: Request, token: str):
        """Предпросмотр: НОЛЬ записей в БД — только чтение кандидатов/справочников."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        session, session_error = get_student_import_session(request, token)
        if session_error is not None:
            return session_error

        context = student_import_preview_context(get_storage(request.app), session)
        return await render(
            template_name=jinja_env.get_template('admin_people_import_preview.html'),
            context={
                'request': request,
                **context,
                **get_flash_args(request),
            },
        )


    @app.post('/admin/people/import/preview/<token>/bulk-create')
    async def admin_student_import_bulk_create(request: Request, token: str):
        """Создать «новых» (pending без кандидатов и дублей на момент рендера)
        одной транзакцией.

        Неразобранные строки с кандидатами в батч не входят — их решает админ
        по отдельности. Предпроверка: кандидаты каждой строки БАТЧА
        пересчитываются на момент клика — если у любой «чистой» строки
        появились совпадения с момента рендера, не создаётся ничего (строки
        перейдут в «Требуют решения» при следующем рендере).
        """
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        session, session_error = get_student_import_session(request, token)
        if session_error is not None:
            return session_error

        storage = get_storage(request.app)
        bulk_rows = student_import_bulk_rows(session)
        # Повторная проверка на момент клика: совпадения появились у «чистой»
        # строки (их не было при рендере) — отмена целиком.
        if any(storage.find_student_candidates(row['full_name']) for row in bulk_rows):
            return build_redirect_with_message(
                error='Ничего не создано: у части строк появились совпадения с существующими студентами. '
                'Проверьте раздел «Требуют решение».',
                url=student_import_preview_url(token),
            )

        # Значения — как в предпросмотре: с канонизацией и автозаполнением
        # института по группе (справочники при этом только читаются). Карта
        # группа→институты — та же функция от строк сессии, что и в рендере.
        file_groups = student_file_group_institutes(session['rows'])
        prepared = []
        for row in bulk_rows:
            hints = student_catalog_hints(storage, row['institute'], row['group'], file_groups)
            prepared.append((row['full_name'], row['sex'], hints['institute'], hints['group'], row['course']))
        student_ids = await asyncio.to_thread(storage.create_students, prepared)
        for row, student_id in zip(bulk_rows, student_ids):
            row['status'] = 'created'
            row['student_id'] = student_id
            log_audit_event(
                request,
                'student_created',
                {'student_id': student_id, 'full_name': row['full_name'], 'source': 'excel-import'},
            )
        return build_redirect_with_message(
            message=f'Создано студентов: {len(student_ids)}.',
            url=student_import_preview_url(token),
        )


    @app.post('/admin/people/import/preview/<token>/row/<row_number>/create')
    async def admin_student_import_row_create(request: Request, token: str, row_number: str):
        """Явное создание одной строки. Разрешено и при 100% совпадении —
        полные тёзки бывают; переспрашивать кандидатов не нужно (кнопку нажали
        осознанно)."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        session, session_error = get_student_import_session(request, token)
        if session_error is not None:
            return session_error
        row, row_error = student_import_row(session, row_number)
        if row_error is not None:
            return row_error
        if row['status'] == 'error':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} содержит ошибку — исправьте её.',
                url=student_import_preview_url(token),
            )
        if row['status'] != 'pending':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} уже разобрана.',
                url=student_import_preview_url(token),
            )

        storage = get_storage(request.app)
        create_imported_student(request, storage, row, student_file_group_institutes(session['rows']))
        return build_redirect_with_message(
            message=f'Строка {row["row_number"]}: студент «{row["full_name"]}» создан.',
            url=student_import_preview_url(token),
        )


    @app.post('/admin/people/import/preview/<token>/row/<row_number>/use-existing')
    async def admin_student_import_row_use_existing(request: Request, token: str, row_number: str):
        """Использовать существующую карточку: пометка ТОЛЬКО в сессии предпросмотра.

        Ничего не создаётся и не связывается — записи и аккаунты не трогаются
        (связи — отдельный ручной workflow сопоставления, Phase 2).
        """
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        session, session_error = get_student_import_session(request, token)
        if session_error is not None:
            return session_error
        row, row_error = student_import_row(session, row_number)
        if row_error is not None:
            return row_error
        if row['status'] == 'error':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} содержит ошибку — исправьте её.',
                url=student_import_preview_url(token),
            )
        if row['status'] != 'pending':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} уже разобрана.',
                url=student_import_preview_url(token),
            )

        student_id, id_error = parse_reconcile_int(get_form_value(request, 'student_id'), 'student id')
        if id_error is not None:
            return id_error
        storage = get_storage(request.app)
        if storage.get_student_by_id(student_id) is None:
            return build_redirect_with_message(
                error='Студент не найден.',
                url=student_import_preview_url(token),
            )

        row['status'] = 'reused_existing'
        row['student_id'] = student_id
        return build_redirect_with_message(
            message=f'Строка {row["row_number"]}: использован существующий студент #{student_id}.',
            url=student_import_preview_url(token),
        )


    @app.post('/admin/people/import/preview/<token>/row/<row_number>/skip')
    async def admin_student_import_row_skip(request: Request, token: str, row_number: str):
        """Пропустить строку (в т.ч. ошибочную — как быстрый способ убрать её
        из виду)."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        session, session_error = get_student_import_session(request, token)
        if session_error is not None:
            return session_error
        row, row_error = student_import_row(session, row_number)
        if row_error is not None:
            return row_error
        if row['status'] not in ('pending', 'error'):
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} уже разобрана.',
                url=student_import_preview_url(token),
            )

        row['status'] = 'skipped'
        row['student_id'] = None
        return build_redirect_with_message(
            message=f'Строка {row["row_number"]} пропущена.',
            url=student_import_preview_url(token),
        )


    @app.post('/admin/people/import/preview/<token>/row/<row_number>/edit')
    async def admin_student_import_row_edit(request: Request, token: str, row_number: str):
        """Правка строки в сессии. В отличие от очереди конфликтов импорта
        записей, ФИО здесь редактируемо — им можно привести строку к написанию
        существующего студента. Некорректные данные — строка становится
        ошибочной; кандидаты/подсказки пересчитаются при следующем рендере."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        session, session_error = get_student_import_session(request, token)
        if session_error is not None:
            return session_error
        row, row_error = student_import_row(session, row_number)
        if row_error is not None:
            return row_error
        if row['status'] not in ('pending', 'error'):
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} уже разобрана.',
                url=student_import_preview_url(token),
            )

        row['full_name'] = get_form_value(request, 'full_name').strip()
        row['sex'] = get_form_value(request, 'sex').strip()
        row['institute'] = get_form_value(request, 'institute').strip()
        row['group'] = get_form_value(request, 'group').strip()
        row['course'] = get_form_value(request, 'course').strip()
        error = None
        if not row['full_name']:
            error = 'Пустое ФИО.'
        elif row['sex'] not in STUDENT_SEX_OPTIONS:
            error = 'Пол должен быть «М» или «Ж».'
        if error is not None:
            row['status'] = 'error'
            row['error'] = error
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} обновлена, но данные некорректны: {error}',
                url=student_import_preview_url(token),
            )
        row['status'] = 'pending'
        row['error'] = None
        return build_redirect_with_message(
            message=f'Строка {row["row_number"]} обновлена.',
            url=student_import_preview_url(token),
        )


    @app.post('/admin/people/import/preview/<token>/finish')
    async def admin_student_import_finish(request: Request, token: str):
        """Завершить импорт: одно audit-событие с итогами, сессия удаляется.

        Ошибочные строки завершению НЕ мешают (они учтены как errors) — блокируют
        только неразобранные pending-строки.
        """
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        session, session_error = get_student_import_session(request, token)
        if session_error is not None:
            return session_error

        pending = sum(1 for row in session['rows'] if row['status'] == 'pending')
        if pending:
            return build_redirect_with_message(
                error=f'Завершение недоступно: не разобрано строк: {pending}.',
                url=student_import_preview_url(token),
            )

        counters = student_import_counters(session)
        log_audit_event(request, 'student_import_completed', counters)
        student_import_sessions.pop(token, None)
        return build_redirect_with_message(
            message=(
                f'Импорт завершён: создано {counters["created"]}, '
                f'использовано существующих {counters["reused_existing"]}, '
                f'пропущено {counters["skipped"]}.'
            ),
            url='/admin/people',
        )


    @app.post('/admin/people/import/preview/<token>/discard')
    async def admin_student_import_discard(request: Request, token: str):
        """Отменить импорт: сессия удаляется без аудита. Уже созданные
        подтверждением карточки остаются (создание было явным)."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        session, session_error = get_student_import_session(request, token)
        if session_error is not None:
            return session_error

        created = sum(1 for row in session['rows'] if row['status'] == 'created')
        student_import_sessions.pop(token, None)
        if created:
            return build_redirect_with_message(
                message=f'Импорт отменён. Созданные карточки ({created}) сохранены.',
                url='/admin/people',
            )
        return build_redirect_with_message(message='Импорт отменён.', url='/admin/people')


    @app.get('/admin/people/<student_id>')
    async def admin_person_card(request: Request, student_id: str):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        numeric_id, id_error = resolve_student_id(request, student_id)
        if id_error is not None:
            return id_error

        storage = get_storage(request.app)
        student = storage.get_student_by_id(numeric_id)
        if student is None:
            return build_redirect_with_message(error='Студент не найден.', url='/admin/people')
        linked_records = storage.list_linked_records(numeric_id, limit=50)
        for record in linked_records:
            # Даты — datetime для format_date_range в шаблоне.
            record['date'] = datetime.fromisoformat(record['date']) if record['date'] else None
            record['date_to'] = datetime.fromisoformat(record['date_to']) if record['date_to'] else None
        # P3: предпросмотр «Видимо атлету» — сколько записей видит аккаунт сейчас
        # (со связью с этой карточкой) и сколько увидит после отвязки. Подтверждение
        # отвязки различает три случая (не изменится / частично / пропадёт всё).
        identity_mode = storage.get_identity_mode()
        linked_users = storage.linked_athlete_users(numeric_id)
        for user in linked_users:
            hashes = storage.athlete_name_hashes(user['id'])
            user['visible_now'] = storage.count_competitions_visible(
                owner_id=user['id'],
                student_ref_id=numeric_id,
                student_id_hashes=hashes,
                identity_mode=identity_mode,
            )
            user['visible_after_unlink'] = storage.count_competitions_visible(
                owner_id=user['id'],
                student_id_hashes=hashes,
                identity_mode=identity_mode,
            )
            if user['visible_after_unlink'] == user['visible_now']:
                user['unlink_confirm'] = (
                    f'Отвязать аккаунт {user["username"]} от студента? '
                    f'Видимость кабинета атлета не изменится ({user["visible_now"]} записей).'
                )
            elif user['visible_after_unlink'] == 0:
                user['unlink_confirm'] = (
                    f'Отвязать аккаунт {user["username"]} от студента? Атлет перестанет видеть записи '
                    f'этого студента (сейчас {user["visible_now"]}, останется 0).'
                )
            else:
                user['unlink_confirm'] = (
                    f'Отвязать аккаунт {user["username"]} от студента? '
                    f'Атлет будет видеть {user["visible_after_unlink"]} из {user["visible_now"]} записей.'
                )
        return await render(
            template_name=jinja_env.get_template('admin_person.html'),
            context={
                'request': request,
                'student': student,
                'aliases': storage.list_student_aliases(numeric_id),
                'linked_records_count': storage.linked_records_count(numeric_id),
                'linked_records': linked_records,
                'linked_users': linked_users,
                'active_students': [item for item in storage.list_students() if item['active']],
                **get_flash_args(request),
            },
        )


    @app.post('/admin/people/<student_id>/edit')
    async def admin_person_edit(request: Request, student_id: str):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        numeric_id, id_error = resolve_student_id(request, student_id)
        if id_error is not None:
            return id_error

        storage = get_storage(request.app)
        student = storage.get_student_by_id(numeric_id)
        if student is None:
            return build_redirect_with_message(error='Студент не найден.', url='/admin/people')
        form, form_error = parse_student_form(request)
        if form_error is not None:
            return build_redirect_with_message(error=form_error, url=f'/admin/people/{numeric_id}')

        changes = student_field_changes(student, form)
        if not storage.update_student(
            numeric_id,
            form['full_name'],
            form['sex'],
            form['institute'],
            form['group_name'],
            form['course'],
        ):
            return build_redirect_with_message(error='Студент не найден.', url='/admin/people')
        log_audit_event(
            request,
            'student_updated',
            {'student_id': numeric_id, 'full_name': form['full_name'], 'changed': changes},
        )
        return build_redirect_with_message(
            message=f'Данные студента «{form["full_name"]}» сохранены.',
            url=f'/admin/people/{numeric_id}',
        )


    @app.post('/admin/people/<student_id>/active')
    async def admin_person_toggle_active(request: Request, student_id: str):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        numeric_id, id_error = resolve_student_id(request, student_id)
        if id_error is not None:
            return id_error

        storage = get_storage(request.app)
        student = storage.get_student_by_id(numeric_id)
        if student is None:
            return build_redirect_with_message(error='Студент не найден.', url='/admin/people')

        deactivate = bool(student['active'])
        storage.set_student_active(numeric_id, not deactivate)
        if deactivate:
            log_audit_event(request, 'student_deactivated', {'student_id': numeric_id, 'full_name': student['full_name']})
            message = f'Студент «{student["full_name"]}» помечен неактивным.'
        else:
            log_audit_event(request, 'student_activated', {'student_id': numeric_id, 'full_name': student['full_name']})
            message = f'Студент «{student["full_name"]}» снова активен.'
        return build_redirect_with_message(message=message, url=f'/admin/people/{numeric_id}')


    @app.post('/admin/people/<student_id>/alias')
    async def admin_person_add_alias(request: Request, student_id: str):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        numeric_id, id_error = resolve_student_id(request, student_id)
        if id_error is not None:
            return id_error

        name = get_form_value(request, 'name').strip()
        if not name:
            return build_redirect_with_message(error='Укажите псевдоним ФИО.', url=f'/admin/people/{numeric_id}')

        storage = get_storage(request.app)
        if storage.get_student_by_id(numeric_id) is None:
            return build_redirect_with_message(error='Студент не найден.', url='/admin/people')
        if not storage.add_student_alias(numeric_id, name):
            return build_redirect_with_message(
                error=f'Псевдоним «{name}» уже есть у этого студента.',
                url=f'/admin/people/{numeric_id}',
            )
        log_audit_event(request, 'student_alias_added', {'student_id': numeric_id, 'name': name})
        return build_redirect_with_message(
            message=f'Псевдоним «{name}» добавлен.',
            url=f'/admin/people/{numeric_id}',
        )


    @app.post('/admin/people/<student_id>/alias/<alias_id>/delete')
    async def admin_person_remove_alias(request: Request, student_id: str, alias_id: str):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        numeric_id, id_error = resolve_student_id(request, student_id)
        if id_error is not None:
            return id_error
        try:
            numeric_alias_id = int(alias_id)
        except ValueError:
            return text(body='Invalid alias id', status=400)

        storage = get_storage(request.app)
        if storage.get_student_by_id(numeric_id) is None:
            return build_redirect_with_message(error='Студент не найден.', url='/admin/people')
        # Имя псевдонима нужно для сообщения и аудита — берём до удаления.
        alias = next(
            (item for item in storage.list_student_aliases(numeric_id) if item['id'] == numeric_alias_id),
            None,
        )
        if alias is None:
            return build_redirect_with_message(error='Псевдоним не найден.', url=f'/admin/people/{numeric_id}')

        storage.remove_student_alias(numeric_id, numeric_alias_id)
        log_audit_event(
            request,
            'student_alias_removed',
            {'student_id': numeric_id, 'alias_id': numeric_alias_id, 'name': alias['name']},
        )
        return build_redirect_with_message(
            message=f'Псевдоним «{alias["name"]}» удалён.',
            url=f'/admin/people/{numeric_id}',
        )


    @app.post('/admin/people/<student_id>/delete')
    async def admin_person_delete(request: Request, student_id: str):
        """Полное удаление ОШИБОЧНО созданной карточки (hard delete, admin).

        Возможно только для карточки без связей: storage блокирует удаление
        при привязанных записях/аккаунтах и слитых карточках, не меняя ни
        одной строки. Записи соревнований вместе с карточкой не удаляются.
        """
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        numeric_id, id_error = resolve_student_id(request, student_id)
        if id_error is not None:
            return id_error

        storage = get_storage(request.app)
        # Снимок до удаления: ФИО нужно для сообщения и аудита после того,
        # как строка исчезла (паттерн admin_person_remove_alias).
        student = storage.get_student_by_id(numeric_id)
        if student is None:
            return build_redirect_with_message(error='Студент не найден.', url='/admin/people')

        code, blockers = storage.delete_student(numeric_id)
        if code == 'not_found':
            return build_redirect_with_message(error='Студент не найден.', url='/admin/people')
        if code == 'blocked':
            # Отказ не пишется в аудит (конвенция остальных проверок ролей/связей).
            facts = []
            if blockers.get('records'):
                facts.append(f'записей — {blockers["records"]}')
            if blockers.get('athlete_users'):
                facts.append(f'аккаунтов атлета — {blockers["athlete_users"]}')
            if blockers.get('merged_children'):
                facts.append(f'объединённых карточек — {blockers["merged_children"]}')
            return build_redirect_with_message(
                error=(
                    f'Удалить карточку нельзя: к ней привязано {", ".join(facts)}. '
                    'Отвязайте их на этой странице или деактивируйте карточку.'
                ),
                url=f'/admin/people/{numeric_id}',
            )

        log_audit_event(
            request,
            'student_deleted',
            {'student_id': numeric_id, 'full_name': student['full_name']},
        )
        return build_redirect_with_message(
            message=f'Карточка «{student["full_name"]}» (#{numeric_id}) удалена.',
            url='/admin/people',
        )


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
