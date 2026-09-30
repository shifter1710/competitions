"""Маршруты календаря: список/карточки событий, участники, файлы положений,
импорт участников из Excel и связка «запись → событие» (P5b). Перенесено из
src/main.py без изменения поведения (Architecture v1)."""
import asyncio
import logging
import secrets
import shutil
import time
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Sequence

import pandas as pd
from pandas import isna
from sanic import redirect
from sanic import Request
from sanic import Sanic
from sanic import text
from sanic.response import HTTPResponse
from sanic.response import raw
from sanic_ext import render

from src.auth import get_current_user_id
from src.auth import log_audit_event
from src.auth import require_admin
from src.auth import require_moderator
from src.auth import user_can_write
from src.auth import user_is_admin
from src.auth import user_is_athlete
from src.auth import user_is_moderator
from src.files import ATTACHMENT_EXTENSIONS
from src.files import ATTACHMENT_MAX_SIZE
from src.files import calendar_regulation_dir
from src.files import detect_attachment_type
from src.files import regulation_source_path
from src.models.competition import Competition
from src.models.custom_field import CustomField
from src.records import build_competition
from src.records import build_link_diff_rows
from src.records import collides_with_fixed_columns
from src.records import competition_content
from src.records import ensure_catalog_values
from src.records import event_participant_content
from src.records import event_participant_identity
from src.records import event_participation_effective_values
from src.records import event_participation_matched_duplicate_guard
from src.records import get_base_field_settings
from src.records import LINK_EVENT_FIELD_LABELS
from src.records import participation_field_changes
from src.settings import settings
from src.storage.sqlite import CalendarEventDuplicateError
from src.storage.sqlite import dedup_discipline
from src.storage.sqlite import dedup_place
from src.storage.sqlite import dedup_text
from src.storage.sqlite import EventParticipationConflictError
from src.storage.sqlite import SQLiteAdapter
from src.students import normalize_import_course
from src.students import normalize_student_sex
from src.students import parse_student_form
from src.students import reconcile_link_error
from src.students import student_catalog_hints
from src.students import student_file_group_institutes
from src.students import STUDENT_SEX_OPTIONS
from src.web import build_redirect_with_message
from src.web import clean_str
from src.web import forbidden
from src.web import format_date_range
from src.web import get_flash_args
from src.web import get_form_value
from src.web import get_param
from src.web import get_storage
from src.web import is_http_url
from src.web import jinja_env
from src.web import parse_date_value
from src.web import parse_reconcile_int

logger = logging.getLogger(__name__)


# Календарь соревнований (волна A, docs/feedback-live.md №23): план на год.
# Статус «запланировано/прошло» — АВТОМАТИЧЕСКИ по дате окончания (выводимой):
# date_to (или date) >= сегодня → «запланировано».
CALENDAR_MONTH_NAMES: Sequence[str] = (
    'Январь',
    'Февраль',
    'Март',
    'Апрель',
    'Май',
    'Июнь',
    'Июль',
    'Август',
    'Сентябрь',
    'Октябрь',
    'Ноябрь',
    'Декабрь',
)


CALENDAR_STATUS_PLANNED = 'planned'


CALENDAR_STATUS_PAST = 'past'


CALENDAR_STATUS_FILTERS: Sequence[tuple[str, str]] = (
    ('', 'Все'),
    (CALENDAR_STATUS_PLANNED, 'запланировано'),
    (CALENDAR_STATUS_PAST, 'прошло'),
)


def calendar_event_status(event: dict, today=None) -> str:
    end_raw = event.get('date_to') or event['date']
    end = datetime.fromisoformat(end_raw).date() if isinstance(end_raw, str) else end_raw
    current = today if today is not None else datetime.now().date()
    return CALENDAR_STATUS_PLANNED if end >= current else CALENDAR_STATUS_PAST


def decorate_calendar_event(event: dict, today=None) -> dict:
    """Готовые к шаблону поля: даты-объекты, компактный период, статус."""
    date_from = datetime.fromisoformat(event['date'])
    date_to = datetime.fromisoformat(event['date_to']) if event.get('date_to') else None
    decorated = dict(event)
    decorated['date_from'] = date_from
    decorated['date_to_obj'] = date_to
    decorated['date_label'] = format_date_range(date_from, date_to)
    decorated['status'] = calendar_event_status(event, today)
    return decorated


def group_calendar_events_by_month(events: Sequence[dict]) -> list[dict]:
    """Группировка карточек по месяцам начала (прототип 15): заголовок
    «Сентябрь 2026 · N», внутри — карточка соревнования."""
    groups: list[dict] = []
    index: dict[tuple[int, int], dict] = {}
    for event in events:
        date_from = event['date_from']
        key = (date_from.year, date_from.month)
        if key not in index:
            group = {
                'key': key,
                'label': f'{CALENDAR_MONTH_NAMES[date_from.month - 1]} {date_from.year}',
                'events': [],
            }
            index[key] = group
            groups.append(group)
        index[key]['events'].append(event)
    for group in groups:
        group['count'] = len(group['events'])
    return groups


def calendar_event_date_label(date_iso: str | None) -> str:
    """День начала ISO-строки события ('YYYY-MM-DD' или '…T00:00:00') как
    dd.mm.yyyy — сообщения guard'а дублей и похожих событий (2026-09-27)."""
    return datetime.fromisoformat((date_iso or '')[:10]).strftime(settings.date_format)


def parse_calendar_event_form(request: Request) -> tuple[dict, str | None]:
    """Разобрать форму соревнования календаря. Возвращает (значения, ошибка).

    Период — одно гибридное поле, тот же парсер, что у записей реестра
    (решение «Даты-диапазоны: визуально одно, под капотом два»).
    """
    name = clean_str(get_form_value(request, 'name'))
    if not name:
        return {}, 'Название обязательно'
    try:
        date_from, date_to = parse_date_value(get_form_value(request, 'date'), manual_input=True)
    except ValueError as exc:
        return {}, str(exc)
    url = clean_str(get_form_value(request, 'url'))
    # Ссылка календаря рендерится как href: принимаем только http/https,
    # прочие схемы (javascript:, data:, vbscript:) — отказ.
    if url and not is_http_url(url):
        return {}, 'Ссылка должна начинаться с http:// или https://'
    return {
        'name': name,
        'date': date_from,
        'date_to': date_to,
        'level': clean_str(get_form_value(request, 'level')),
        'sport': clean_str(get_form_value(request, 'sport')),
        'url': url,
    }, None


# Страница соревнования (волна B, прототип 16): участники = записи реестра
# по пресету события (name + date + date_to). Фильтр результата — GET,
# ссылки шарятся, как на главной.
CALENDAR_RESULT_FILTERS: Sequence[tuple[str, str]] = (
    ('', 'Все'),
    ('with', 'С результатом'),
    ('without', 'Без результата'),
)


def get_calendar_event_or_error(request: Request, event_id: str):
    """Общий разбор id события из URL: (event dict | None, response | None)."""
    try:
        numeric_id = int(event_id)
    except ValueError:
        return None, text(body='Invalid event id', status=400)
    event = get_storage(request.app).get_calendar_event(numeric_id)
    if event is None:
        return None, text(body='Event not found', status=404)
    return event, None


def calendar_participant_groups(
    participants: Sequence[dict],
    custom_fields: Sequence[CustomField],
) -> list[dict]:
    """Группы участий страницы события (P7) — ТОЛЬКО визуализация.

    Несколько участий одной карточки студента (student_ref_id NOT NULL)
    показываются parent-строкой + child-строками; БД и реестр остаются
    плоскими — по записи на участие, никакой группировки там нет. Cardless-
    участия (student_ref_id NULL) — всегда синглтоны: одинаковые ФИО у
    разных людей не сливается (no-guess).

    Группа = {'is_group', 'student_ref_id', поля студента (снимок ПЕРВОГО
    участия в текущей серверной сортировке list_calendar_event_participants),
    'participations'}; is_group = участий больше одного (одиночная группа
    рендерится полной строкой, как раньше). Каждое участие получает
    эффективные discipline/result (базовая колонка ?? legacy-фолбэк в
    extra_data коллидирующего кастома, event_participation_effective_values,
    только чтение). Children сортируются по (нормализованная дисциплина,
    record_id). Ключи участий читаются через .get() — фикстуры-моки
    присылают сокращённые словари.
    """
    groups: list[dict] = []
    by_ref: dict[int, dict] = {}

    def new_group(participant: dict, student_ref_id: int | None) -> dict:
        return {
            'student_ref_id': student_ref_id,
            'student_name': participant.get('student_name'),
            'student_sex': participant.get('student_sex'),
            'institute': participant.get('institute'),
            'group_name': participant.get('group_name'),
            'course': participant.get('course'),
            'participations': [],
        }

    for participant in participants:
        discipline, result = event_participation_effective_values(participant, custom_fields)
        participation = {**participant, 'effective_discipline': discipline, 'effective_result': result}
        student_ref_id = participant.get('student_ref_id')
        if student_ref_id is None:
            groups.append(new_group(participant, None))
            groups[-1]['participations'].append(participation)
            continue
        group = by_ref.get(student_ref_id)
        if group is None:
            group = new_group(participant, student_ref_id)
            by_ref[student_ref_id] = group
            groups.append(group)
        group['participations'].append(participation)

    for group in groups:
        group['participations'].sort(
            key=lambda item: (dedup_discipline(item['effective_discipline']), item.get('record_id'))
        )
        group['is_group'] = len(group['participations']) > 1
    return groups


def calendar_participant_prefill_group(groups: Sequence[dict], raw_ref: str | None) -> dict | None:
    """Группа для prefill-режима «+ участие» (P7): ?add_participation=<ref>
    ищется по карточке среди ВСЕХ групп (без учёта фильтра результата —
    кнопка «+ участие» видна и из отфильтрованного вида). Нет/мусор в
    параметре — None: параметр молча игнорируется."""
    if not raw_ref:
        return None
    try:
        student_ref_id = int(raw_ref)
    except ValueError:
        return None
    return next((group for group in groups if group.get('student_ref_id') == student_ref_id), None)


def resolve_manual_participant_student_ref(
    storage: SQLiteAdapter,
    request: Request,
    back_url: str,
) -> tuple[int | None, HTTPResponse | None]:
    """Явный выбор карточки студента в ручном добавлении участника события:
    непустое значение валидируется (карточка существует и активна); невалидное
    — отказ БЕЗ создания записи, молчаливо терять выбранную админом связь
    нельзя. Возвращает (student_ref_id, редирект-ошибка | None)."""
    student_ref_raw = get_form_value(request, 'student_ref_id').strip()
    if not student_ref_raw:
        return None, None
    try:
        student_ref_id = int(student_ref_raw)
    except ValueError:
        student_ref_id = None
    student = storage.get_student_by_id(student_ref_id) if student_ref_id is not None else None
    if student is None or not student['active']:
        return None, build_redirect_with_message(
            error='Выбранная карточка студента не найдена или неактивна. Сбросьте связь и выберите карточку заново.',
            url=back_url,
        )
    return student_ref_id, None


# Связывание cardless-участия события с карточкой студента со страницы
# события (2026-09-26). Все Student-управляющие операции — admin-only
# (editor/viewer карточек не видит, права не расширяем молча): кнопка
# «Связать со студентом» у cardless-строки ведёт на отдельную страницу —
# существующая карточка (поиск/однофамильцы) или создание новой из снимка
# записи. Переиспользуются примитивы сопоставления (link_competitions,
# find_student_candidates/search_student_candidates, create_student,
# parse_student_form); привязка меняет ТОЛЬКО student_ref_id — снимок
# записи остаётся байт-в-байт. Никаких bulk/автосвязей других строк.


def event_participant_for_link(storage: SQLiteAdapter, event: dict, raw_record_id: str):
    """Cardless-участие события для страницы/POST привязки: (Competition |
    None, redirect-ошибка | None). Запись должна существовать, принадлежать
    этому событию (calendar_event_id) и не иметь связи с карточкой — иначе
    редирект на страницу события с admin_error (кнопка в строке есть только
    у cardless, но URL открыт для гонок/ручных ссылок)."""
    back_url = f'/calendar/{event["id"]}'
    try:
        record_id = int(raw_record_id)
    except (TypeError, ValueError):
        record_id = None
    record = storage.get_competition_by_id(record_id) if record_id is not None else None
    if record is None or record.calendar_event_id != event['id']:
        return None, build_redirect_with_message(error='Запись не найдена.', url=back_url)
    if record.student_ref_id is not None:
        return None, build_redirect_with_message(
            error=f'Запись №{record_id} уже связана со студентом.',
            url=back_url,
        )
    return record, None


def event_participant_link_guard(
    storage: SQLiteAdapter,
    event: dict,
    record: Competition,
    student_id: int,
) -> str | None:
    """Matched-инвариант привязки cardless-участия к карточке: тот же
    event_participation_matched_duplicate_guard, что у вставки участий —
    target-карточка подставляется в копию записи (student_ref_id),
    дисциплина берётся ЭФФЕКТИВНАЯ (базовая колонка ?? legacy-фолбэк в
    extra_data), сама запись исключается из сравнения (exclude_record_id).
    Привязка не может создать второе участие той же карточки с той же
    дисциплиной; конфликт — 0 изменений."""
    custom_fields = storage.get_custom_fields()
    discipline, _ = event_participation_effective_values(
        {'discipline': record.discipline, 'result': record.result, 'extra_data': record.extra_data},
        custom_fields,
    )
    guarded = record.model_copy(update={'student_ref_id': int(student_id), 'discipline': discipline or None})
    return event_participation_matched_duplicate_guard(
        storage,
        event,
        guarded,
        exclude_record_id=int(record.record_id),
    )


# --- Импорт участников события календаря из Excel. ---
#
# Массовое добавление участников В КОНТЕКСТЕ события: пресет соревнования
# (название/даты/уровень/вид спорта) берётся из события и в Excel НЕ
# повторяется — обязательное значение только ФИО. Паттерн импорта студентов
# (Phase 2.5): разбор → staging-сессия в памяти → предпросмотр с
# кандидатами-карточками → явные решения построчно или батчем; в БД не
# пишется НИЧЕГО до подтверждения. Приоритет снимка участия: ЯВНОЕ значение
# Excel > карточка Student (явно выбранная или единственный точный кандидат
# для автозаполнения пол/институт/группа/курс) > пусто; карточка Student при
# этом НЕ меняется. Подтверждённые участия получают student_ref_id — запись
# стабильной связи; с P3 кабинет атлета её читает (dual/ref), остальной
# runtime (отчёты, выгрузки, модерация) работает по легаси-ключу как раньше.
# Права: страницы/действия импорта — модераторы (admin/editor); «создать
# карточку студента» — только admin (конвенция Phase 1/2.5: Students —
# admin-only), editor видит выбор существующей карточки и пропуск.
#
# P4 (Event Participant Import v3): опциональные фиксированные колонки
# «Дисциплина» (identity-часть участия) и «Результат» (informational);
# строгий repeat-safe дедуп — предпросмотр классифицирует строки против
# живых участий (already/update-candidate/possible-duplicate) и повторов
# файла (дубликат-в-файле/конфликт), сервер повторяет проверку под _lock
# (apply_event_participation_batch) — повторное участие НЕ создаётся:
# вместо дубля доступно restricted-обновление место/результат существующего.


# P4: «Дисциплина»/«Результат» — фиксированные опциональные колонки файла
# участников (после «Места»): дисциплина — identity-часть участия (вместе с
# карточкой/ФИО различает 100 м, 200 м и эстафету одного человека), результат
# — informational-колонка участия (базовая result записи; импорт реестра её
# по-прежнему не пишет).
EVENT_PARTICIPANT_IMPORT_BASE_COLUMNS: Sequence[str] = (
    'ФИО',
    'Пол',
    'Институт',
    'Группа',
    'Курс',
    'Место',
    'Дисциплина',
    'Результат',
)


EVENT_PARTICIPANT_IMPORT_MAX_ROWS = 5000


EVENT_IMPORT_SESSION_TTL_SECONDS = 2 * 60 * 60


# Поиск карточки для строки предпросмотра («Найти студента»): лимит
# результатов и максимальная длина запроса.
EVENT_IMPORT_STUDENT_SEARCH_LIMIT = 8


EVENT_IMPORT_STUDENT_SEARCH_QUERY_MAX_LENGTH = 100


# Staging живёт в памяти (прецедент student_import_sessions): однопроцессное
# приложение, рестарт просто теряет сессии — безопасно, файл загрузят заново.
# Один файл на пользователя; сессия привязана к владельцу и событию; ФИО из
# файла не логируются.
event_import_sessions: dict[str, dict] = {}


def event_import_custom_fields(custom_fields: Sequence[CustomField]) -> list[CustomField]:
    """Custom-поля импорта участников события: активные с show_in_template,
    КРОМЕ url-полей (ссылки уровня записи/события к снимку участия не
    относятся; признак — field_type, не label) и КРОМЕ коллизии label с
    фиксированными колонками «Дисциплина»/«Результат» (P5a-exception, P4):
    колонка файла одна, значение идёт в базовые колонки записи через
    build_competition. Определения полей и их значения в реестре/extra_data
    не трогаются; select_template_custom_fields реестра — свои правила.

    Сами исключённые поля в системе остаются (реестр, обычный
    импорт/экспорт, link_target) — исключение касается только этого
    workflow. Фильтр применяется на входе каждого пути (шаблон, разбор,
    предпросмотр, правки, валидация, добавление) и обязан доходить до
    валидации (build_event_participant_competition): иначе гипотетическое
    ОБЯЗАТЕЛЬНОЕ url-поле заблокировало бы все строки импорта участников.
    """
    return [
        field
        for field in custom_fields
        if field.active
        and field.show_in_template
        and field.field_type != 'url'
        and not collides_with_fixed_columns(field.label, {'дисциплина', 'результат'})
    ]


def event_participant_import_columns(custom_fields: Sequence[CustomField]) -> list[str]:
    """Колонки файла/шаблона импорта участников: базовые + активные
    custom-поля с show_in_template (генерически, по существующим флагам)."""
    return [
        *EVENT_PARTICIPANT_IMPORT_BASE_COLUMNS,
        *[field.label for field in custom_fields if field.show_in_template],
    ]


def normalize_import_position(value) -> str:
    """Место из ячейки Excel как строка — та же нормализация, что у курса
    (числовые ячейки pandas отдаёт float'ами: 1 → «1»). Пусто → «»."""
    return normalize_import_course(value)


def parse_event_participant_rows(
    df: pd.DataFrame,
    custom_fields: Sequence[CustomField],
) -> tuple[list[dict], list[str], list[str]]:
    """Разобранные строки файла импорта участников + неизвестные колонки +
    колонки custom-полей, присутствующие в файле.

    Строка staging: row_number = index + 2 (заголовок — строка 1), статус
    pending/error. Пол — normalize_student_sex («м» → «М», недопустимое —
    ошибка строки, а не молчаливая правка); курс и место — числовая
    нормализация Excel. Дубли строк в файле здесь НЕ помечаются: группы
    пересчитываются при каждом рендере (правки строк меняют состав).
    """
    import_columns = set(event_participant_import_columns(custom_fields))
    columns = [str(column) for column in df.columns]
    unknown_columns = [column for column in columns if column not in import_columns]
    custom_by_label = {field.label: field for field in custom_fields if field.show_in_template}
    custom_columns = [column for column in columns if column in custom_by_label]
    rows: list[dict] = []
    for index, record in df.iterrows():
        full_name = clean_str(record.get('ФИО'))
        sex = normalize_student_sex(clean_str(record.get('Пол')))
        error = None
        if not full_name:
            error = 'Пустое ФИО.'
        elif sex is None:
            error = 'Пол должен быть «М» или «Ж».'
        rows.append(
            {
                'row_number': int(index) + 2,
                'full_name': full_name,
                'sex': sex or '',
                'institute': clean_str(record.get('Институт')),
                'group': clean_str(record.get('Группа')),
                'course': normalize_import_course(record.get('Курс')),
                'position': normalize_import_position(record.get('Место')),
                # P4: фиксированные опциональные колонки участия (clean_str,
                # пустая ячейка → «»); дисциплина идёт в запись через
                # build_competition, результат — присвоением после build.
                'discipline': clean_str(record.get('Дисциплина')),
                'result': clean_str(record.get('Результат')),
                'custom': {custom_by_label[column].key: clean_str(record.get(column)) for column in custom_columns},
                'status': 'error' if error else 'pending',
                'error': error,
                # Явный выбор карточки админом (select-student/create-student).
                'student_id': None,
                # Явное решение «без карточки» (clear-student): подавляет
                # автозаполнение единственным точным кандидатом — участие
                # добавляется без связи, сопоставить можно позже.
                'student_forced_none': False,
                # Карточка, реально использованная добавленной записью.
                'added_student_id': None,
            }
        )
    return rows, unknown_columns, custom_columns


# Полное равенство строк файла (P4) — с дисциплиной/результатом: «Иванов|100 м|1»
# и «Иванов|200 м|3» — ДВА корректных участия, точная копия строки — повтор.
EVENT_PARTICIPANT_SNAPSHOT_KEYS: Sequence[str] = (
    'full_name',
    'sex',
    'institute',
    'group',
    'course',
    'position',
    'discipline',
    'result',
)


def event_import_duplicate_rows(rows: Sequence[dict]) -> dict[int, list[int]]:
    """Полностью одинаковые строки файла: номер строки → номера всех строк группы.

    «Одинаковость» — полное равенство снимка импортируемых колонок
    (strip + casefold, включая custom-значения и дисциплину/результат):
    «Иванов|100 м|1» и «Иванов|200 м|3» — ДВА корректных участия, точная
    копия строки — повтор. P4: повторы-копии pending-строк БОЛЬШЕ не
    блокируют обе строки — поздняя копия получает производный статус
    «дубликат в файле — пропускается» (event_import_infile_duplicates:
    якорь — первая строка группы), карта используется как подсказка якорю.
    """
    groups: dict[tuple, list[int]] = {}
    for row in rows:
        key = (
            tuple((row[column] or '').strip().casefold() for column in EVENT_PARTICIPANT_SNAPSHOT_KEYS),
            tuple(sorted((key.strip(), (value or '').strip().casefold()) for key, value in row['custom'].items())),
        )
        groups.setdefault(key, []).append(row['row_number'])
    return {row_number: numbers for numbers in groups.values() if len(numbers) > 1 for row_number in numbers}


def sweep_event_import_sessions() -> None:
    """Удалить просроченные сессии (TTL); вызывается при каждом обращении."""
    now = time.time()
    expired = [
        token
        for token, session in event_import_sessions.items()
        if now - session['created_at'] > EVENT_IMPORT_SESSION_TTL_SECONDS
    ]
    for token in expired:
        event_import_sessions.pop(token, None)


def get_event_import_session(request: Request, token: str, event_id: int):
    """Сессия предпросмотра для запроса: (session, None) или (None, redirect).

    Неизвестный/просроченный токен, чужая сессия и сессия другого события —
    одно и то же поведение: возврат на страницу загрузки с flash.
    """
    sweep_event_import_sessions()
    session = event_import_sessions.get(token)
    if session is None or session['user_id'] != get_current_user_id(request) or session['event_id'] != event_id:
        return None, build_redirect_with_message(
            error='Время сессии предпросмотра истекло. Загрузите файл заново.',
            url=f'/calendar/{event_id}/participants/import',
        )
    return session, None


def replace_event_import_session(user_id: int, token: str, session: dict) -> None:
    """Сохранить новую сессию предпросмотра; прежняя сессия того же
    пользователя заменяется (один файл на пользователя)."""
    sweep_event_import_sessions()
    for existing_token, existing in list(event_import_sessions.items()):
        if existing['user_id'] == user_id:
            event_import_sessions.pop(existing_token, None)
    event_import_sessions[token] = session


def event_import_preview_url(event_id: int, token: str) -> str:
    return f'/calendar/{event_id}/participants/import/preview/{token}'


def event_import_row(session: dict, raw_row_number: str):
    """Строка сессии по номеру из URL: (row, None) или (None, ответ 400)."""
    row_number, error = parse_reconcile_int(raw_row_number, 'row number')
    if error is not None:
        return None, error
    for row in session['rows']:
        if row['row_number'] == row_number:
            return row, None
    return None, text(body='Invalid row number', status=400)


def event_participant_preset(event: dict) -> dict:
    """Пресет события в колонки записи: sport/date/date_to/level/name.

    Тот же пресет, что у ручного добавления участника (POST
    /calendar/<id>/participants): период — компактной строкой, парсер
    ручного ввода понимает её обратно.
    """
    event_date = datetime.fromisoformat(event['date'])
    event_date_to = datetime.fromisoformat(event['date_to']) if event.get('date_to') else None
    return {
        'Вид спорта': event['sport'],
        'Дата': format_date_range(event_date, event_date_to),
        'Уровень соревнований': event['level'],
        'Название соревнований': event['name'],
    }


def event_import_effective_student(storage: SQLiteAdapter, row: dict, candidates: Sequence[dict]) -> dict | None:
    """Карточка для автозаполнения/связи: явный выбор админа, иначе при
    отсутствии решения «без карточки» — единственный точный кандидат.

    Полные тёзки (≥2) и 0 кандидатов НЕ выбираются автоматически — участие
    просто добавляется без связи (сопоставить можно позже). Явное решение
    «без карточки» (student_forced_none, clear-student) подавляет
    автозаполнение единственным кандидатом. Выбранная карточка проверяется
    на активность. Возвращаемая карточка нормализована: id — в 'id' (у
    кандидатов find_student_cards ключ 'student_id', у get_student_by_id —
    'id').
    """
    if row.get('student_id') is not None:
        student = storage.get_student_by_id(row['student_id'])
        if student is not None and student['active']:
            return student
        return None
    if row.get('student_forced_none'):
        return None
    if len(candidates) == 1:
        candidate = dict(candidates[0])
        candidate['id'] = candidate['student_id']
        return candidate
    return None


def event_import_row_record(
    storage: SQLiteAdapter,
    row: dict,
    preset: dict,
    student: dict | None,
    custom_fields: Sequence[CustomField],
    file_groups: dict[str, list[dict]] | None = None,
) -> tuple[dict, list[str], dict | None]:
    """Снимок строки для build_competition: Excel > Student > пусто.

    Возвращает (record, автозаполненные-из-карточки-колонки, hints).
    Карточка Student только читается (master не мутируется). Если институт
    пуст и группа заполнена (и карточка института не дала) — подсказки
    student_catalog_hints: сначала карта группа→институты ТЕКУЩЕГО файла,
    затем справочник (справочники при этом только читаются).
    """
    record = {
        'ФИО': row['full_name'],
        'Пол': row['sex'],
        'Институт': row['institute'],
        'Группа': row['group'],
        'Курс': row['course'],
        'Место': row['position'],
        # P4: дисциплина — фиксированной колонкой в build_competition (путь
        # P5a, обязательный для реестра); результат — только в participation-
        # workflow: присваивается ПОСЛЕ build (см. build_event_participant_
        # competition), build_competition его не читает.
        'Дисциплина': row.get('discipline', ''),
        'Результат': row.get('result', ''),
        **preset,
    }
    record.update(
        {field.label: row['custom'].get(field.key, '') for field in custom_fields if field.key in row['custom']}
    )
    autofilled: list[str] = []
    if student is not None:
        for column, student_key in (
            ('Пол', 'sex'),
            ('Институт', 'institute'),
            ('Группа', 'group_name'),
            ('Курс', 'course'),
        ):
            value = str(student.get(student_key) or '').strip()
            if value and not str(record.get(column) or '').strip():
                record[column] = value
                autofilled.append(column)
    hints = None
    if not str(record.get('Институт') or '').strip() and str(record.get('Группа') or '').strip():
        hints = student_catalog_hints(storage, '', record['Группа'], file_groups)
        if hints.get('institute'):
            record['Институт'] = hints['institute']
            record['Группа'] = hints.get('group') or record['Группа']
    return record, autofilled, hints


def build_event_participant_competition(
    record: dict,
    custom_fields: Sequence[CustomField],
    storage: SQLiteAdapter,
    *,
    result: str = '',
) -> tuple[Competition | None, str | None]:
    """Финальная валидация строки тем же механизмом, что ручной ввод
    (build_competition: field_settings, required custom-поля, число места).
    Возвращает (competition, None) или (None, текст ошибки).

    P4: «Результат» — informational-колонка участия; build_competition её не
    читает (базовый result импортом РЕЕСТРА не пишется — P5a), поэтому
    значение присваивается здесь, сразу после build — все пути этого
    workflow (предпросмотр/правка/вставка) получают одинаковую запись."""
    try:
        competition = build_competition(record, custom_fields=custom_fields, manual_input=True, storage=storage)
    except (TypeError, ValueError) as exc:
        return None, str(exc)
    competition.result = str(result or '').strip() or None
    return competition, None


def event_import_existing_participations(
    existing_participants: Sequence[dict],
    row: dict,
    student: dict | None,
) -> list[dict]:
    """Уже существующие участия того же человека в этом событии.

    Совпадение — ФИО (casefold) ИЛИ выбранная карточка (student_ref_id).
    Только информация для предпросмотра («уже участвует»), НЕ блокировка:
    несколько участий одного Student в событии — нормальный случай
    (разные дисциплины), решение всегда за админом.
    """
    target = (row['full_name'] or '').strip().casefold()
    student_ref = student['id'] if student else None
    return [
        participant
        for participant in existing_participants
        if (participant['student_name'] or '').strip().casefold() == target
        or (student_ref is not None and participant.get('student_ref_id') == student_ref)
    ]


# --- Классификация повторов импорта участников (P4, repeat-safe dedup). ---
#
# Строгая уникальность участия события enforced сервером (storage.
# apply_event_participation_batch): MATCHED-строка (карточка определена)
# уникальна по identity = (карточка, дисциплина), CARDLESS-строка — по
# полному контенту (ФИО+дисциплина+снимок). Предпросмотр классифицирует
# строки ТЕМИ ЖЕ ключами (пересчёт на каждом рендере), серверная проверка
# повторяет её под _lock на момент клика — расхождение предпросмотра и БД
# откатывает весь батч, а не создаёт дубль.


def event_import_diff_value(value) -> str:
    """Значение диффа место/результат: 0/пусто → «—», иначе — как введено."""
    text = str(value if value is not None else '').strip()
    if not text or text == '0':
        return '—'
    return text


def classify_event_import_row(
    existing_participants: Sequence[dict],
    row: dict,
    student: dict | None,
    competition: Competition | None,
    custom_fields: Sequence[CustomField],
) -> dict:
    """Классификация pending-строки против УЖЕ существующих участий события
    (P4; in-file повторы — отдельно, event_import_infile_duplicates).

    MATCHED (карточка определена: явный выбор/единственный кандидат): identity
    = (карточка, дисциплина). Участия с той же identity нет — ready; есть и
    mutable (место+результат) равны — already (resolved-подобный, без
    действий, в БД ничего); отличаются — update-candidate с диффом
    место/результат. CARDLESS (без карточки): точная копия контента
    существующего участия — already; контент-ключ (ФИО+дисциплина) совпал,
    контент другой — possible-duplicate (явное решение «пропустить/добавить
    как новое», обновить cardless-участие нельзя); иначе ready.

    Возвращает {'status', 'match', 'diff'}: status — ready/already/
    update-candidate/possible-duplicate, match — существующее участие,
    diff — {'position'/'result': (старое, новое)} только изменившихся полей.
    """
    if student is not None:
        identity = ('ref', student['id'], dedup_discipline(row.get('discipline')))
        match = next(
            (
                participant
                for participant in existing_participants
                if event_participant_identity(participant, custom_fields) == identity
            ),
            None,
        )
        if match is None:
            return {'status': 'ready', 'match': None, 'diff': None}
        _, old_result = event_participation_effective_values(match, custom_fields)
        new_place = row.get('position')
        if dedup_place(match['position']) == dedup_place(new_place) and dedup_text(old_result) == dedup_text(
            row.get('result')
        ):
            return {'status': 'already', 'match': match, 'diff': None}
        diff: dict[str, tuple[str, str]] = {}
        if dedup_place(match['position']) != dedup_place(new_place):
            diff['position'] = (event_import_diff_value(match['position']), event_import_diff_value(new_place))
        if dedup_text(old_result) != dedup_text(row.get('result')):
            diff['result'] = (event_import_diff_value(old_result), event_import_diff_value(row.get('result')))
        return {'status': 'update-candidate', 'match': match, 'diff': diff}
    if competition is not None:
        content = competition_content(competition, custom_fields)
        exact = next(
            (
                participant
                for participant in existing_participants
                if event_participant_content(participant, custom_fields) == content
            ),
            None,
        )
        if exact is not None:
            return {'status': 'already', 'match': exact, 'diff': None}
    content_key = (dedup_text(row['full_name']), dedup_discipline(row.get('discipline')))
    possible = next(
        (
            participant
            for participant in existing_participants
            if (
                dedup_text(participant['student_name']),
                dedup_discipline(event_participation_effective_values(participant, custom_fields)[0]),
            )
            == content_key
        ),
        None,
    )
    if possible is not None:
        return {'status': 'possible-duplicate', 'match': possible, 'diff': None}
    return {'status': 'ready', 'match': None, 'diff': None}


def event_import_row_snapshot(row: dict) -> tuple:
    """Полный снимок строки для сравнения in-file (равенство контента):
    базовые колонки (с дисциплиной/результатом) + custom, strip + casefold —
    та же форма, что у event_import_duplicate_rows."""
    return (
        tuple((row[column] or '').strip().casefold() for column in EVENT_PARTICIPANT_SNAPSHOT_KEYS),
        tuple(sorted((key, (value or '').strip().casefold()) for key, value in row['custom'].items())),
    )


def event_import_row_uniqueness_key(row: dict, student: dict | None) -> tuple:
    """Ключ уникальности строки (P4): MATCHED — (карточка, дисциплина),
    CARDLESS — (ФИО, дисциплина); совпадает с серверной проверкой батча
    (identity MATCHED / контент-ключ CARDLESS)."""
    discipline = dedup_discipline(row.get('discipline'))
    if student is not None:
        return ('ref', student['id'], discipline)
    return ('name', dedup_text(row['full_name']), discipline)


def event_import_infile_duplicates(
    rows: Sequence[dict],
    students: dict[int, dict | None],
) -> tuple[dict[int, int], dict[int, list[int]]]:
    """In-file повторы импорта участников (P4), по pending-строкам.

    Возвращает (копия → якорь, строка → номера конфликтующих строк):
    - точная копия снимка (casefold) БОЛЕЕ ПОЗДНЕЙ строки — производный
      пропуск «дубликат в файле» (якорь — первая строка группы по
      row_number; флаг НЕ sticky: разобранный якорь продвигает следующую
      копию; в bulk не входит, завершение не блокирует);
    - тот же ключ уникальности (карточка/ФИО + дисциплина) при РАЗНОМ
      контенте — взаимный конфликт строк (v2-семантика «требуют решения»,
      пока одна из строк не разобрана): сервер обе не вставит.
    """
    anchor_by_snapshot: dict[tuple, int] = {}
    duplicate_of: dict[int, int] = {}
    anchor_rows: list[dict] = []
    for row in sorted(rows, key=lambda item: item['row_number']):
        snapshot = event_import_row_snapshot(row)
        anchor = anchor_by_snapshot.get(snapshot)
        if anchor is not None:
            duplicate_of[row['row_number']] = anchor
            continue
        anchor_by_snapshot[snapshot] = row['row_number']
        anchor_rows.append(row)
    by_key: dict[tuple, list[dict]] = {}
    for row in anchor_rows:
        by_key.setdefault(event_import_row_uniqueness_key(row, students.get(row['row_number'])), []).append(row)
    conflicts: dict[int, list[int]] = {}
    for group in by_key.values():
        if len(group) > 1:
            numbers = [row['row_number'] for row in group]
            for row in group:
                conflicts[row['row_number']] = sorted(number for number in numbers if number != row['row_number'])
    return duplicate_of, conflicts


# Производные статусы pending-строк (P4): пересчитываются на каждом рендере,
# в сессии НЕ фиксируются (не sticky) — изменение БД/строк меняет их сами.
EVENT_IMPORT_STATUS_FILE_DUPLICATE = 'file-duplicate'


EVENT_IMPORT_STATUS_CONFLICT = 'conflict'


# Финальные статусы строк: производные pending + разобранные (sticky).
EVENT_IMPORT_DERIVED_RESOLVED = (EVENT_IMPORT_STATUS_FILE_DUPLICATE, 'already')


EVENT_IMPORT_REVIEW_STATUSES = ('update-candidate', 'possible-duplicate', EVENT_IMPORT_STATUS_CONFLICT)


def event_import_pending_states(
    storage: SQLiteAdapter,
    event: dict,
    session: dict,
    custom_fields: Sequence[CustomField],
) -> dict[int, dict]:
    """Один проход по pending-строкам сессии (P4): кандидаты, эффективная
    карточка, автозаполнение, снимок для build_competition, валидация,
    классификация против живых участников события и in-file повторы.
    Только чтение; вызывается предпросмотром, завершением и guard'ами
    вставки — классификация везде одинаковая.

    Возвращает {row_number: state}; state['import_status'] — финальный
    производный статус строки (classify-статус с наложением in-file:
    точная копия — file-duplicate, конфликт ключей — conflict)."""
    preset = event_participant_preset(event)
    existing_participants = storage.list_calendar_event_participants(event['id'])
    all_custom_fields = storage.get_custom_fields()
    file_groups = student_file_group_institutes(session['rows'])
    states: dict[int, dict] = {}
    for row in session['rows']:
        if row['status'] != 'pending':
            continue
        candidates = storage.find_student_candidates(row['full_name'])
        student = event_import_effective_student(storage, row, candidates)
        record, autofilled, hints = event_import_row_record(storage, row, preset, student, custom_fields, file_groups)
        competition, validation_error = build_event_participant_competition(
            record, custom_fields, storage, result=row.get('result', '')
        )
        classification = classify_event_import_row(existing_participants, row, student, competition, all_custom_fields)
        states[row['row_number']] = {
            'candidates': candidates,
            'student': student,
            'record': record,
            'autofilled': autofilled,
            'hints': hints,
            'competition': competition,
            'validation_error': validation_error,
            'classification': classification,
        }
    duplicate_of, conflicts = event_import_infile_duplicates(
        [row for row in session['rows'] if row['status'] == 'pending'],
        {number: state['student'] for number, state in states.items()},
    )
    for number, state in states.items():
        state['duplicate_of'] = duplicate_of.get(number)
        state['conflict_rows'] = conflicts.get(number)
        if state['duplicate_of'] is not None:
            state['import_status'] = EVENT_IMPORT_STATUS_FILE_DUPLICATE
        elif state['classification']['status'] == 'ready' and state['conflict_rows']:
            state['import_status'] = EVENT_IMPORT_STATUS_CONFLICT
        else:
            state['import_status'] = state['classification']['status']
    return states


def event_import_row_display(record: dict, competition: Competition | None) -> dict:
    """Значения строки для таблицы предпросмотра: из ПОСТРОЕННОЙ записи
    (ровно то, что будет вставлено — с канонизацией справочников), при
    ошибке валидации — из record после автозаполнения. P4: дисциплина и
    результат участия — те же колонки таблицы (пустые рисуются тире)."""
    if competition is not None:
        return {
            'sex': competition.student_sex,
            'institute': competition.institute,
            'group': competition.group,
            'course': competition.course,
            'position': competition.position,
            'discipline': competition.discipline or '',
            'result': competition.result or '',
        }
    return {
        'sex': record.get('Пол', ''),
        'institute': record.get('Институт', ''),
        'group': record.get('Группа', ''),
        'course': record.get('Курс', ''),
        'position': record.get('Место', ''),
        'discipline': record.get('Дисциплина', ''),
        'result': record.get('Результат', ''),
    }


def event_import_pending_category(import_status: str, validation_error: str | None) -> str:
    """Финальная категория pending-строки (P4): ошибочные — валидация;
    «уже существует»/«дубликат в файле» — «Решено» (производные, без
    действий); update-candidate/possible-duplicate/конфликт файла —
    «Требуют решения»; готовые (в т.ч. без карточки) — «Готовы»."""
    if validation_error:
        return 'error'
    if import_status in EVENT_IMPORT_DERIVED_RESOLVED:
        return 'resolved'
    if import_status in EVENT_IMPORT_REVIEW_STATUSES:
        return 'review'
    return 'ready'


def event_import_pending_view(
    view: dict,
    row: dict,
    state: dict,
    custom_fields: Sequence[CustomField],
    existing_participants: Sequence[dict],
) -> str:
    """Заполнить view pending-строки производными данными classify-прохода и
    вернуть её категорию (P4). Заодно фиксирует в строке сессии признаки
    последнего рендера: bulk-commit берёт только «готовые» строки и
    отменяется целиком, если их состав изменился ПОСЛЕ рендера."""
    student = state['student']
    competition = state['competition']
    import_status = state['import_status']
    view.update(
        {
            'candidates': state['candidates'],
            'effective_student': student,
            'auto_preselected': student is not None and row.get('student_id') is None,
            'autofilled': state['autofilled'],
            'hints': state['hints'],
            'validation_error': state['validation_error'],
            'display': event_import_row_display(state['record'], competition),
            'import_status': import_status,
            'classification': state['classification'],
            'file_anchor': state['duplicate_of'],
            'conflict_rows': state['conflict_rows'],
        }
    )
    view['custom_display'] = {
        field.label: (
            competition.extra_data.get(field.key, '')
            if competition is not None
            else state['record'].get(field.label, '')
        )
        for field in custom_fields
        if field.key in row['custom']
    }
    view['existing'] = event_import_existing_participations(existing_participants, row, student)
    row['had_student_id'] = student['id'] if student else None
    row['had_validation_error'] = bool(state['validation_error'])
    row['had_import_ready'] = import_status == 'ready' and not state['validation_error']
    return event_import_pending_category(import_status, state['validation_error'])


def event_import_preview_context(
    storage: SQLiteAdapter,
    event: dict,
    session: dict,
    custom_fields: Sequence[CustomField],
) -> dict:
    """Контекст предпросмотра импорта участников.

    Всё производное (кандидаты, автозаполнение, подсказки, дубли файла,
    существующие участия, валидация, классификация повторов P4, категории,
    счётчики) пересчитывается при каждом рендере; в сессии — строки файла,
    явные решения и фиксируемые рендером признаки had_student_id/
    had_validation_error/had_import_ready (см. event_import_bulk_rows).
    """
    duplicates = event_import_duplicate_rows(session['rows'])
    existing_participants = storage.list_calendar_event_participants(event['id'])
    states = event_import_pending_states(storage, event, session, custom_fields)
    groups: dict[str, list[dict]] = {'ready': [], 'review': [], 'error': [], 'resolved': []}
    for row in session['rows']:
        status = row['status']
        view = {
            **row,
            'candidates': [],
            'effective_student': None,
            'auto_preselected': False,
            'autofilled': [],
            'hints': None,
            'validation_error': None,
            'display': None,
            'custom_display': {},
            'duplicate_rows': duplicates.get(row['row_number']),
            'existing': [],
            'import_status': status,
            'classification': None,
            'update_diff': None,
            'file_anchor': None,
            'conflict_rows': None,
        }
        if status == 'pending':
            category = event_import_pending_view(
                view, row, states[row['row_number']], custom_fields, existing_participants
            )
        elif status == 'error':
            category = 'error'
        else:
            category = 'resolved'
            if status == 'updated':
                view['update_diff'] = row.get('update_diff')
        groups[category].append(view)
    resolved_counts = {'added': 0, 'updated': 0, 'kept': 0, 'skipped': 0, 'already': 0, 'file_duplicate': 0}
    for view in groups['resolved']:
        if view['status'] in resolved_counts:
            resolved_counts[view['status']] += 1
        elif view['import_status'] == 'already':
            resolved_counts['already'] += 1
        else:
            resolved_counts['file_duplicate'] += 1
    linked_ready = sum(1 for view in groups['ready'] if view['effective_student'] is not None)
    resolved_parts = [
        f'{label} {resolved_counts[key]}'
        for label, key in (
            ('добавлено', 'added'),
            ('обновлено', 'updated'),
            ('оставлено', 'kept'),
            ('уже существует', 'already'),
            ('дубликатов в файле', 'file_duplicate'),
            ('пропущено', 'skipped'),
        )
        if resolved_counts[key]
    ]
    return {
        'token': session['token'],
        'event': decorate_calendar_event(event),
        'filename': session['filename'],
        'unknown_columns': session['unknown_columns'],
        'custom_columns': session['custom_columns'],
        'custom_keys': {field.label: field.key for field in custom_fields},
        'groups': groups,
        'total': len(session['rows']),
        # pending — строки, требующие решения (готовые + review): производные
        # already/«дубликат в файле» решения не требуют.
        'pending': len(groups['ready']) + len(groups['review']),
        'ready_count': len(groups['ready']),
        'review_count': len(groups['review']),
        'error_count': len(groups['error']),
        'resolved_count': len(groups['resolved']),
        'resolved_line': ', '.join(resolved_parts),
        'already_count': resolved_counts['already'],
        'file_duplicate_count': resolved_counts['file_duplicate'],
        'added_count': resolved_counts['added'],
        'updated_count': resolved_counts['updated'],
        'kept_count': resolved_counts['kept'],
        'skipped_count': resolved_counts['skipped'],
        # Для confirm-текста bulk: сколько готовых строк уйдут со связью и
        # сколько — без карточки.
        'linked_ready': linked_ready,
        'unlinked_ready': len(groups['ready']) - linked_ready,
    }


def event_import_bulk_rows(session: dict) -> list[dict]:
    """Строки массового добавления: ровно строки, которые последний рендер
    предпросмотра классифицировал как «готовые» (had_import_ready, P4), —
    счётчик кнопки совпадает с числом вставляемых строк.

    В батч НЕ входят: ошибочные строки, строки review (update-candidate /
    possible-duplicate / конфликт файла — обновление существующих только
    построчными действиями), производные «уже существует» и «дубликат в
    файле». Сопоставление с карточкой обязательным НЕ является: строка без
    карточки добавляется с student_ref_id IS NULL. Авторитетная повторная
    проверка уникальности — storage.apply_event_participation_batch под
    _lock на момент клика: коллизия откатывает весь батч."""
    return [
        row
        for row in session['rows']
        if row['status'] == 'pending' and not row.get('had_validation_error') and row.get('had_import_ready')
    ]


def event_import_counters(session: dict, derived_counts: dict[str, int] | None = None) -> dict[str, int]:
    """Итоги сессии для аудита/сообщений (без содержимого файла).

    P4: добавлены updated/kept (построчные решения по существующим участиям)
    и производные already_exists/file_duplicates (строки, оказавшиеся
    точными повторами — в БД ничего не писалось)."""
    counters = {
        'added': 0,
        'updated': 0,
        'kept': 0,
        'skipped': 0,
        'already_exists': 0,
        'file_duplicates': 0,
        'errors': 0,
    }
    for row in session['rows']:
        if row['status'] == 'error':
            counters['errors'] += 1
        elif row['status'] in counters:
            counters[row['status']] += 1
    derived = derived_counts or {}
    counters['already_exists'] = derived.get('already', 0)
    counters['file_duplicates'] = derived.get('file-duplicate', 0)
    counters['total'] = len(session['rows'])
    return counters


def event_import_upload_error(df: pd.DataFrame) -> str | None:
    """Ошибки файла на уровне колонок/объёма; None — файл пригоден."""
    if 'ФИО' not in {str(column) for column in df.columns}:
        return 'В файле нет обязательной колонки «ФИО». Скачайте шаблон и заполните его.'
    if len(df) > EVENT_PARTICIPANT_IMPORT_MAX_ROWS:
        return f'В файле больше {EVENT_PARTICIPANT_IMPORT_MAX_ROWS} строк. Разбейте список на части.'
    if df.empty:
        return 'В файле нет строк с данными.'
    return None


def event_import_row_search(
    request: Request,
    storage: SQLiteAdapter,
    session: dict,
) -> tuple[int | None, str, list[dict], bool]:
    """Поиск карточки для строки предпросмотра (GET-параметры find/q).

    Возвращает (номер строки, запрос, результаты, усечён ли список). Только
    чтение: в сессию ничего не пишет и не влияет на had_student_id/bulk.
    Некорректные find/q (нет параметра, не число, строка не существует/не
    pending, пустой или слишком длинный запрос) молча игнорируются — обычный
    предпросмотр без блока поиска.
    """
    raw_find = request.args.get('find', '')
    if not raw_find:
        return None, '', [], False
    row_number, error = parse_reconcile_int(raw_find, 'row number')
    if error is not None:
        return None, '', [], False
    row = next((row for row in session['rows'] if row['row_number'] == row_number), None)
    if row is None or row['status'] != 'pending':
        return None, '', [], False

    query = request.args.get('q', '').strip()
    if not query or len(query) > EVENT_IMPORT_STUDENT_SEARCH_QUERY_MAX_LENGTH:
        return row_number, '', [], False
    results = storage.search_student_candidates(query, limit=EVENT_IMPORT_STUDENT_SEARCH_LIMIT)
    truncated = len(results) >= EVENT_IMPORT_STUDENT_SEARCH_LIMIT
    return row_number, query, results, truncated


def resolve_event_import_row(request: Request, event_id: str, token: str, row_number: str):
    """Общие проверки действия над строкой: событие, сессия, строка."""
    event, error = get_calendar_event_or_error(request, event_id)
    if error is not None:
        return None, None, None, error
    session, session_error = get_event_import_session(request, token, event['id'])
    if session_error is not None:
        return None, None, None, session_error
    row, row_error = event_import_row(session, row_number)
    if row_error is not None:
        return None, None, None, row_error
    return event, session, row, None


def event_import_insert_guard(
    storage: SQLiteAdapter,
    event: dict,
    session: dict,
    row: dict,
    student: dict | None,
    competition: Competition | None,
) -> str | None:
    """Серверная повторная классификация строки перед одиночной вставкой
    (add/create-student, P4): одним запросом против живых участников события
    + проверка якоря in-file. Возвращает текст ошибки или None (вставлять
    можно). possible-duplicate НЕ отказ — «Добавить как новое» явное решение
    админа; авторитетная повторная проверка под _lock — в
    storage.apply_event_participation_batch."""
    classification = classify_event_import_row(
        storage.list_calendar_event_participants(event['id']),
        row,
        student,
        competition,
        storage.get_custom_fields(),
    )
    status = classification['status']
    if status == 'already':
        return 'такое участие уже существует — точная копия в списке участников'
    if status == 'update-candidate':
        return 'участие с этой карточкой и дисциплиной уже существует — обновите его кнопкой «Обновить существующее»'
    # In-file копия с ещё не разобранным якорем: якорь добавят/пропустят,
    # копия — производный повтор (сервер обе не вставляет).
    snapshot = event_import_row_snapshot(row)
    for other in session['rows']:
        if (
            other['status'] == 'pending'
            and other['row_number'] < row['row_number']
            and event_import_row_snapshot(other) == snapshot
        ):
            return f'дубликат в файле — повтор строки {other["row_number"]}'
    return None


def event_import_add_row_participation(
    request: Request, storage: SQLiteAdapter, event: dict, session: dict, row: dict
) -> str | None:
    """Добавить участие по строке с её текущей карточкой (явный выбор или
    единственный кандидат) — или без карточки, если её нет. Возвращает текст
    ошибки или None.

    Кандидаты и валидация пересчитываются на момент клика — предпросмотр
    мог устареть. P4: вставка — через apply_event_participation_batch
    (повторная проверка уникальности под _lock + справочники одной
    транзакцией); коллизия — отказ, строка остаётся pending. Пресет события,
    approved, student_ref_id подобранной карточки или NULL (сопоставить
    можно позже), result — informational-колонка участия."""
    candidates = storage.find_student_candidates(row['full_name'])
    student = event_import_effective_student(storage, row, candidates)
    custom_fields = event_import_custom_fields(storage.get_custom_fields())
    record, _, _ = event_import_row_record(
        storage,
        row,
        event_participant_preset(event),
        student,
        custom_fields,
        student_file_group_institutes(session['rows']),
    )
    competition, validation_error = build_event_participant_competition(
        record, custom_fields, storage, result=row.get('result', '')
    )
    if validation_error is not None:
        return validation_error
    guard_error = event_import_insert_guard(storage, event, session, row, student, competition)
    if guard_error is not None:
        return guard_error
    competition.student_ref_id = student['id'] if student else None
    # P2: участие явно связывается с событием (участники читаются по ссылке,
    # не по пресету).
    competition.calendar_event_id = event['id']
    try:
        storage.apply_event_participation_batch(event['id'], [competition], owner_id=get_current_user_id(request))
    except EventParticipationConflictError:
        return 'участники события изменились — обновите страницу и проверьте строки'
    row['status'] = 'added'
    row['student_id'] = student['id'] if student else None
    row['added_student_id'] = student['id'] if student else None
    log_audit_event(
        request,
        'event_participant_imported',
        {'event_id': event['id'], 'student_ref_id': row['added_student_id']},
    )
    return None


def event_import_update_candidate_classification(
    storage: SQLiteAdapter, event: dict, session: dict, row: dict
) -> tuple[dict | None, Competition | None, str | None]:
    """Повторная классификация строки для update-existing/keep-existing (P4):
    (classification, competition, ошибка). Только update-candidate-строки
    годятся для обоих действий — существующее участие той же карточки и
    дисциплины с отличающимися местом/результатом."""
    candidates = storage.find_student_candidates(row['full_name'])
    student = event_import_effective_student(storage, row, candidates)
    custom_fields = event_import_custom_fields(storage.get_custom_fields())
    record, _, _ = event_import_row_record(
        storage,
        row,
        event_participant_preset(event),
        student,
        custom_fields,
        student_file_group_institutes(session['rows']),
    )
    competition, validation_error = build_event_participant_competition(
        record, custom_fields, storage, result=row.get('result', '')
    )
    if validation_error is not None:
        return None, None, validation_error
    classification = classify_event_import_row(
        storage.list_calendar_event_participants(event['id']),
        row,
        student,
        competition,
        storage.get_custom_fields(),
    )
    return classification, competition, None


def event_import_create_student_participation(
    request: Request,
    storage: SQLiteAdapter,
    event: dict,
    session: dict,
    row: dict,
    full_name: str,
    sex: str,
    hints: dict,
    course: str,
) -> str | None:
    """Создать карточку + связанное участие по строке (P4). Возвращает текст
    ошибки или None. Валидация и guard — ДО создания карточки (провал не
    должен оставлять карточку без участия); вставка — guarded-батч."""
    file_groups = student_file_group_institutes(session['rows'])
    # Валидация будущего участия с данными БУДУЩЕЙ карточки.
    future_student = {
        'id': 0,
        'full_name': full_name,
        'sex': sex,
        'institute': hints['institute'],
        'group_name': hints['group'],
        'course': course,
    }
    custom_fields = event_import_custom_fields(storage.get_custom_fields())
    record, _, _ = event_import_row_record(
        storage,
        row,
        event_participant_preset(event),
        future_student,
        custom_fields,
        file_groups,
    )
    competition, validation_error = build_event_participant_competition(
        record, custom_fields, storage, result=row.get('result', '')
    )
    if validation_error is not None:
        return validation_error
    # Тот же guard, что у одиночного добавления (P4): для БУДУЩЕЙ карточки
    # (id 0) identity-коллизий нет по построению, проверка единая для всех
    # путей вставки — включая in-file якорь и точные копии.
    guard_error = event_import_insert_guard(storage, event, session, row, future_student, competition)
    if guard_error is not None:
        return guard_error

    student_id = storage.create_student(full_name, sex, hints['institute'], hints['group'], course)
    log_audit_event(
        request,
        'student_created',
        {'student_id': student_id, 'full_name': full_name, 'source': 'event-participant-import'},
    )
    competition.student_ref_id = student_id
    # P2: участие явно связывается с событием (участники читаются по ссылке,
    # не по пресету). P4: вставка — apply_event_participation_batch (повторная
    # проверка + справочники одной транзакцией).
    competition.calendar_event_id = event['id']
    try:
        storage.apply_event_participation_batch(event['id'], [competition], owner_id=get_current_user_id(request))
    except EventParticipationConflictError:
        return 'участники события изменились — обновите страницу и проверьте строки'
    row['status'] = 'added'
    row['student_id'] = student_id
    row['student_forced_none'] = False
    row['added_student_id'] = student_id
    log_audit_event(
        request,
        'event_participant_imported',
        {'event_id': event['id'], 'student_ref_id': student_id},
    )
    return None


def event_import_prepare_bulk_rows(
    storage: SQLiteAdapter,
    event: dict,
    session: dict,
    custom_fields: Sequence[CustomField],
) -> tuple[list[tuple[dict, Competition, dict | None]] | None, str | None]:
    """Пересобрать строки bulk-батча на момент клика (P4): (prepared, None)
    или (None, текст ошибки). Кандидаты/валидация пересчитываются, состав
    «готовых» (had_student_id) сверяется с зафиксированным рендером."""
    bulk_rows = event_import_bulk_rows(session)
    file_groups = student_file_group_institutes(session['rows'])
    preset = event_participant_preset(event)
    prepared: list[tuple[dict, Competition, dict | None]] = []
    for row in bulk_rows:
        candidates = storage.find_student_candidates(row['full_name'])
        student = event_import_effective_student(storage, row, candidates)
        actual_ref = student['id'] if student else None
        if actual_ref != row.get('had_student_id'):
            return None, 'Список карточек изменился — обновите страницу и проверьте строки.'
        record, _, _ = event_import_row_record(storage, row, preset, student, custom_fields, file_groups)
        competition, validation_error = build_event_participant_competition(
            record, custom_fields, storage, result=row.get('result', '')
        )
        if validation_error is not None:
            return None, f'Строка {row["row_number"]}: {validation_error}'
        competition.student_ref_id = actual_ref
        # P2: участие явно связывается с событием (участники читаются по
        # ссылке, не по пресету).
        competition.calendar_event_id = event['id']
        prepared.append((row, competition, student))
    return prepared, None


def normalize_position(value) -> int:
    if isna(value) or value == '':
        return 0
    return int(value)


def normalize_course(value) -> int:
    if isna(value):
        raise ValueError('Курс обязателен')
    return int(value)


# --- P5b: явные связки «запись → событие» (Registry ↔ Calendar). ---
#
# Страница связывания NULL-записи со соревнованием календаря: снимок записи,
# GET-поиск событий с пресет-кандидатами (точное name+date+date_to) и диффом
# 5 event-owned полей «после связывания», либо создание нового события прямо
# со страницы записи (атомарно create+link). Сами операции — guarded UPDATE
# в storage (link_participation_to_event / create_calendar_event_and_link):
# гонку «двое связывают одну запись» закрывает calendar_event_id IS NULL.


LINK_EVENT_CANDIDATE_CAP = 20


LINK_EVENT_SEARCH_KEYS = ('name', 'sport', 'level', 'date_from', 'date_to')


def parse_link_event_filters(args: dict) -> tuple[dict[str, str], str | None]:
    """Параметры GET-поиска событий на странице связывания: подстроки
    name/sport/level + границы периода дд.мм.гггг (валидация — как
    parse_index_filters; некорректная дата — текст ошибки для 400)."""
    filters = {key: (get_param(args, key) or '').strip() for key in ('name', 'sport', 'level')}
    for key in ('date_from', 'date_to'):
        raw_value = (get_param(args, key) or '').strip()
        if raw_value:
            try:
                datetime.strptime(raw_value, settings.date_format)
            except ValueError:
                return {}, f'Некорректная дата в фильтре ({key}): {raw_value}'
        filters[key] = raw_value
    return filters, None


def calendar_event_overlaps_period(event: dict, date_from: str, date_to: str) -> bool:
    """Пересекается ли период события с границами фильтра (пустая граница —
    не ограничивает). Записи и события хранят даты ISO-текстом."""
    event_start = datetime.fromisoformat(event['date']).date()
    event_end = datetime.fromisoformat(event['date_to'] or event['date']).date()
    if date_from:
        start_bound = datetime.strptime(date_from, settings.date_format).date()
        if event_end < start_bound:
            return False
    if date_to:
        end_bound = datetime.strptime(date_to, settings.date_format).date()
        if event_start > end_bound:
            return False
    return True


def filter_link_event_candidates(events: Sequence[dict], filters: dict[str, str]) -> list[dict]:
    """Фильтрация событий поверх list_calendar_events(): casefold-подстрока
    по названию/уровню/виду спорта + пересечение периода."""
    name_query = filters['name'].casefold()
    sport_query = filters['sport'].casefold()
    level_query = filters['level'].casefold()
    found = []
    for event in events:
        if name_query and name_query not in (event['name'] or '').casefold():
            continue
        if sport_query and sport_query not in (event['sport'] or '').casefold():
            continue
        if level_query and level_query not in (event['level'] or '').casefold():
            continue
        if not calendar_event_overlaps_period(event, filters['date_from'], filters['date_to']):
            continue
        found.append(event)
    return found


def event_is_record_preset(event: dict, record: Competition) -> bool:
    """Точное совпадение события с пресетом записи — та же семантика, что у
    _calendar_preset_match_sql (name + date + COALESCE(date_to, ''))."""
    return (
        event['name'] == record.name
        and event['date'] == record.date.isoformat()
        and (event.get('date_to') or '') == (record.date_to.isoformat() if record.date_to else '')
    )


def competition_link_changes(record: Competition, event: dict) -> dict[str, dict[str, str | None]]:
    return participation_field_changes(
        record.name,
        record.sport,
        record.date.isoformat(),
        record.date_to.isoformat() if record.date_to else None,
        record.level,
        event,
    )


def decorate_link_event_candidates(
    record: Competition,
    events: Sequence[dict],
) -> tuple[list[dict], int]:
    """Кандидаты связывания для страницы записи: пресеты первыми (без очистки
    полей выше пресетов с очисткой), затем остальные по хронологии; каждому —
    дифф 5 полей и готовый текст confirm. Возвращает (строки, всего до капа)."""
    decorated = []
    for event in events:
        diff = build_link_diff_rows(competition_link_changes(record, event))
        decorated.append(
            {
                'event': decorate_calendar_event(event),
                'is_preset': event_is_record_preset(event, record),
                'diff': diff,
                'confirm_text': build_link_confirm_text(
                    int(record.record_id),
                    event['name'],
                    [item['key'] for item in diff if item['clears']],
                ),
            }
        )
    decorated.sort(key=lambda row: (not row['is_preset'], any(item['clears'] for item in row['diff'])))
    total = len(decorated)
    return decorated[:LINK_EVENT_CANDIDATE_CAP], total


def build_link_confirm_text(record_id: int, event_name: str, cleared_fields: Sequence[str]) -> str:
    base = (
        f'Связать запись №{record_id} с соревнованием «{event_name}»? '
        'Название, вид спорта, дату и уровень запись возьмёт из соревнования.'
    )
    if cleared_fields:
        labels = ', '.join(LINK_EVENT_FIELD_LABELS[key] for key in cleared_fields)
        base += f' Пустыми станут: {labels}.'
    return base


# C901 (осознанное подавление): mccabe суммирует сложность вложенных
# verbatim-хендлеров, перенесённых из main.py без изменений; разбиение
# register() — Architecture v2, не pre-merge gate.
def register(app: Sanic) -> None:  # noqa: C901
    @app.get('/calendar')
    async def calendar_page(request: Request):
        # Решение по ролям: admin/editor — полный доступ, viewer — просмотр без
        # кнопок, athlete — 403 (план — внутренняя кухня).
        if user_is_athlete(request):
            return forbidden(request)
        storage = get_storage(request.app)
        args = dict(request.args)

        raw_status = (get_param(args, 'status') or '').strip()
        if raw_status not in {key for key, _ in CALENDAR_STATUS_FILTERS}:
            raw_status = ''
        sport_filter = (get_param(args, 'sport') or '').strip()

        today = datetime.now().date()
        events = [decorate_calendar_event(event, today) for event in storage.list_calendar_events(sport=sport_filter)]
        if raw_status:
            events = [event for event in events if event['status'] == raw_status]

        return await render(
            template_name=jinja_env.get_template('calendar.html'),
            context={
                'request': request,
                'month_groups': group_calendar_events_by_month(events),
                'events_count': len(events),
                'planned_count': sum(1 for event in events if event['status'] == CALENDAR_STATUS_PLANNED),
                'past_count': sum(1 for event in events if event['status'] == CALENDAR_STATUS_PAST),
                'can_manage': user_is_moderator(request),
                'status_filter': raw_status,
                'status_filter_options': CALENDAR_STATUS_FILTERS,
                'sport_filter': sport_filter,
                'sport_options': storage.list_catalog('sport'),
                'level_options': storage.get_level_names(),
                **get_flash_args(request),
            },
        )

    @app.post('/calendar/new')
    async def create_calendar_event(request: Request):
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error

        values, error = parse_calendar_event_form(request)
        if error is not None:
            return text(body=error, status=400)

        storage = get_storage(request.app)
        new_date = values['date'].isoformat()
        new_date_to = values['date_to'].isoformat() if values['date_to'] else None
        # Guard точных дублей (2026-09-27): событие с тем же identity-ключом
        # уже есть — новое не создаётся, аудита успеха нет (блок — не успех).
        try:
            event_id = storage.create_calendar_event(
                name=values['name'],
                date=new_date,
                date_to=new_date_to,
                level=values['level'],
                sport=values['sport'],
                url=values['url'],
            )
        except CalendarEventDuplicateError:
            return build_redirect_with_message(
                error=(
                    f'Соревнование «{values["name"]}» на {values["date"].strftime(settings.date_format)}'
                    ' уже существует — новое не создано. Откройте его в календаре.'
                ),
                url='/calendar',
            )
        message = f'Соревнование «{values["name"]}» запланировано'
        # Неблокирующее предупреждение о похожем (то же название+дата, но
        # другой ключ — отличились date_to/sport/level): создание прошло,
        # решение «дубль или нет» остаётся за человеком.
        similar = storage.find_similar_calendar_event(
            name=values['name'],
            date=new_date,
            date_to=new_date_to,
            level=values['level'],
            sport=values['sport'],
            exclude_id=event_id,
        )
        if similar is not None:
            message += (
                f'. Похоже, уже есть похожее: «{similar["name"]}»'
                f' ({calendar_event_date_label(similar["date"])}) — проверьте, не дубль ли это.'
            )
        return build_redirect_with_message(message=message, url='/calendar')

    @app.post('/calendar/<event_id>/edit')
    async def edit_calendar_event(request: Request, event_id: str):
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error

        try:
            numeric_id = int(event_id)
        except ValueError:
            return text(body='Invalid event id', status=400)

        storage = get_storage(request.app)
        event = storage.get_calendar_event(numeric_id)
        if event is None:
            return text(body='Event not found', status=404)

        values, error = parse_calendar_event_form(request)
        if error is not None:
            return text(body=error, status=400)

        new_date = values['date'].isoformat()
        new_date_to = values['date_to'].isoformat() if values['date_to'] else None
        # P2: правка события — владелец полей участия; связанные записи (по
        # calendar_event_id) синхронизируются той же транзакцией (см.
        # SQLiteAdapter.update_calendar_event), synced — их число для аудита.
        # Guard дублей (2026-09-27): правка совпала с чужим событием — 0
        # изменений и 0 sync (guard до UPDATE в storage), аудита нет.
        try:
            synced_participations = storage.update_calendar_event(
                event_id=numeric_id,
                name=values['name'],
                date=new_date,
                date_to=new_date_to,
                level=values['level'],
                sport=values['sport'],
                url=values['url'],
            )
        except CalendarEventDuplicateError:
            next_url = get_form_value(request, 'next')
            redirect_url = next_url if next_url.startswith('/calendar/') else '/calendar'
            return build_redirect_with_message(
                error=(
                    f'Соревнование «{values["name"]}» на {values["date"].strftime(settings.date_format)}'
                    ' уже существует. Правка не сохранена.'
                ),
                url=redirect_url,
            )
        log_audit_event(
            request,
            'calendar_event_edited',
            {
                'event_id': numeric_id,
                'name': {'old': event['name'], 'new': values['name']},
                'date': {'old': event['date'], 'new': new_date},
                'date_to': {'old': event.get('date_to'), 'new': new_date_to},
                'sport': {'old': event['sport'], 'new': values['sport']},
                'level': {'old': event['level'], 'new': values['level']},
                'synced_participations': synced_participations,
            },
        )
        # Правка со страницы соревнования (волна B, прототип 16) возвращает
        # внутрь события; из календаря — в календарь. next принимаем только
        # как путь внутрь /calendar/ — открытый редирект исключён.
        next_url = get_form_value(request, 'next')
        if next_url.startswith('/calendar/'):
            return build_redirect_with_message(message='Соревнование обновлено', url=next_url)
        return build_redirect_with_message(message='Соревнование обновлено', url='/calendar')

    @app.get('/calendar/<event_id>')
    async def calendar_event_page(request: Request, event_id: str):
        # Решение по ролям — как у календаря: admin/editor — полный доступ,
        # viewer — просмотр без кнопок, athlete — 403.
        if user_is_athlete(request):
            return forbidden(request)

        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error

        args = dict(request.args)
        result_filter = (get_param(args, 'result') or '').strip()
        if result_filter not in {key for key, _ in CALENDAR_RESULT_FILTERS}:
            result_filter = ''

        storage = get_storage(request.app)
        all_participants = storage.list_calendar_event_participants(event['id'])
        participations_count = len(all_participants)
        no_result_count = sum(1 for participant in all_participants if participant.get('position') == 0)
        participants = list(all_participants)
        if result_filter == 'with':
            participants = [participant for participant in participants if participant.get('position') != 0]
        elif result_filter == 'without':
            participants = [participant for participant in participants if participant.get('position') == 0]

        # P7: группы участий — только визуализация страницы (БД/реестр плоские).
        # Счётчики считаются по НЕотфильтрованному списку (семантика фильтра и
        # счётчика «без результата» не меняется); группы для отображения
        # строятся по уже отфильтрованным участиям.
        custom_fields = storage.get_custom_fields()
        all_groups = calendar_participant_groups(all_participants, custom_fields)
        participant_groups = (
            all_groups if not result_filter else calendar_participant_groups(participants, custom_fields)
        )
        can_write = user_can_write(request)

        return await render(
            template_name=jinja_env.get_template('calendar_event.html'),
            context={
                'request': request,
                'event': decorate_calendar_event(event),
                'participant_groups': participant_groups,
                'participants_count': len(all_groups),
                'participations_count': participations_count,
                'no_result_count': no_result_count,
                'shown_count': len(participants),
                'can_write': can_write,
                # Prefill-режим «+ участие» (P7): группа по ref из
                # ?add_participation=<ref>; нет группы — параметр игнорируется.
                # Только для пишущих ролей: viewer форму не видит вовсе.
                'can_add_for': calendar_participant_prefill_group(all_groups, get_param(args, 'add_participation'))
                if can_write
                else None,
                # Кнопка удаления участника — только admin (роут /competition/<id>/delete
                # админский): без флага кнопка в шаблоне не рисовалась вовсе.
                'is_admin': user_is_admin(request),
                # Управление файлом положения — модераторы (admin/editor);
                # can_write сюда не годится: он включает атлета.
                'can_manage': user_is_moderator(request),
                'result_filter': result_filter,
                'result_filter_options': CALENDAR_RESULT_FILTERS,
                'sport_options': storage.list_catalog('sport'),
                'level_options': storage.get_level_names(),
                # Настройки полей для строки ручного добавления (required/тип
                # Курса и Места — как в реестре; ФИО всегда обязательное).
                'base_field_settings': get_base_field_settings(storage),
                # Participation-кастомы (P7): те же правила, что у импорта
                # участников (event_import_custom_fields) — для инпутов формы
                # добавления/prefill и инлайн-правки на клиенте.
                'participant_custom_fields': [
                    {'key': field.key, 'label': field.label} for field in event_import_custom_fields(custom_fields)
                ],
                **get_flash_args(request),
            },
        )

    @app.post('/calendar/<event_id>/participants')
    async def add_calendar_event_participant(request: Request, event_id: str):
        # Участник добавляется как ОБЫЧНАЯ запись реестра (историчность —
        # docs/data-model-decisions.md): пресет события (name/date/date_to)
        # копируется в запись, никаких FK. Роли: admin/editor — полный доступ
        # (viewer пишет? нет), поэтому require_moderator; запись сразу approved.
        # При явном выборе карточки студента в резолвере ФИО форма присылает
        # student_ref_id: связь стабильная, но снимок полей остаётся снимком
        # формы (Excel/ручной ввод всегда приоритетнее карточки).
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error

        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error

        back_url = f'/calendar/{event["id"]}'
        student_name = clean_str(get_form_value(request, 'student_name'))
        if not student_name:
            return build_redirect_with_message(error='ФИО обязательно', url=back_url)

        event_date = datetime.fromisoformat(event['date'])
        event_date_to = datetime.fromisoformat(event['date_to']) if event.get('date_to') else None
        storage = get_storage(request.app)

        student_ref_id, ref_error = resolve_manual_participant_student_ref(storage, request, back_url)
        if ref_error is not None:
            return ref_error

        custom_fields = storage.get_custom_fields()
        record = {
            'ФИО': student_name,
            'Пол': get_form_value(request, 'student_sex'),
            'Институт': get_form_value(request, 'institute'),
            'Группа': get_form_value(request, 'group'),
            'Вид спорта': event['sport'],
            # Период — компактной строкой: тот же парсер, что у ручного ввода
            # (format_date_range реимпортируем обратно).
            'Дата': format_date_range(event_date, event_date_to),
            'Уровень соревнований': event['level'],
            'Название соревнований': event['name'],
            # Место опционально («можно дописать позже»): пусто → position = 0.
            'Место': get_form_value(request, 'position'),
            'Курс': get_form_value(request, 'course'),
            # P4: дисциплина участия — та же фиксированная колонка, что у импорта
            # (build_competition кладёт её в базовую колонку записи); поле формы
            # опциональное, пусто → NULL.
            'Дисциплина': get_form_value(request, 'discipline'),
        }
        # P7: participation-кастомы — тот же фильтр, что у импорта участников
        # (event_import_custom_fields: активные show_in_template без url и
        # коллизий label с «Дисциплина»/«Результат»); шлёт их prefill-форма
        # «+ участие», обычная строка добавления — только базовые поля.
        record.update(
            {
                field.label: get_form_value(request, f'custom__{field.key}')
                for field in event_import_custom_fields(custom_fields)
            }
        )

        # P7: результат участия — informational-колонка, присваивается ПОСЛЕ
        # build (паттерн build_event_participant_competition импорта).
        competition, build_error = build_event_participant_competition(
            record,
            custom_fields,
            storage,
            result=get_form_value(request, 'result'),
        )
        if build_error is not None:
            return build_redirect_with_message(error=build_error, url=back_url)

        competition.student_ref_id = student_ref_id
        # P2: участник события получает явную ссылку на событие — состав
        # участников читается по calendar_event_id, а не по пресету.
        competition.calendar_event_id = event['id']
        # Глобальный matched-инвариант (P4 QA D2): второй участия той же карточки
        # с той же дисциплиной ручной ввод создать не может — guard до любых
        # записей (справочники/участие), существующая строка не меняется.
        guard_error = event_participation_matched_duplicate_guard(storage, event, competition)
        if guard_error is not None:
            return build_redirect_with_message(error=guard_error, url=back_url)
        ensure_catalog_values(storage, [competition])
        storage.save_competitions(
            [competition],
            review_status='approved',
            owner_id=get_current_user_id(request),
        )
        return build_redirect_with_message(
            message=f'Участник «{student_name}» добавлен',
            url=back_url,
        )

    @app.get('/calendar/<event_id>/participants/<record_id>/link')
    async def calendar_event_participant_link_page(request: Request, event_id: str, record_id: str):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error

        storage = get_storage(request.app)
        record, record_error = event_participant_for_link(storage, event, record_id)
        if record_error is not None:
            return record_error

        custom_fields = storage.get_custom_fields()
        discipline, result = event_participation_effective_values(
            {'discipline': record.discipline, 'result': record.result, 'extra_data': record.extra_data},
            custom_fields,
        )
        # Однофамильцы — только ПРЕДЛОЖЕНИЯ (точное совпадение ФИО/псевдонима);
        # поиск — подстрока по тем же карточкам, паттерн «Найти студента»
        # предпросмотра импорта. Обе выборки — только активные карточки.
        q = (get_param(dict(request.args), 'q') or '').strip()
        search_results = storage.search_student_candidates(q) if q else []
        return await render(
            template_name=jinja_env.get_template('calendar_event_participant_link.html'),
            context={
                'request': request,
                'event': decorate_calendar_event(event),
                'record': record,
                'record_id': int(record.record_id),
                'effective_discipline': discipline,
                'effective_result': result,
                'namesakes': storage.find_student_candidates(record.student_name),
                'q': q,
                'search_results': search_results,
                **get_flash_args(request),
            },
        )

    @app.post('/calendar/<event_id>/participants/<record_id>/link')
    async def calendar_event_participant_link(request: Request, event_id: str, record_id: str):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error

        back_url = f'/calendar/{event["id"]}'
        storage = get_storage(request.app)
        record, record_error = event_participant_for_link(storage, event, record_id)
        if record_error is not None:
            return record_error

        student_id, id_error = parse_reconcile_int(get_form_value(request, 'student_id'), 'student id')
        if id_error is not None:
            return id_error

        # Guard ДО привязки: карточка + эффективная дисциплина уникальны в
        # событии; конфликт — 0 изменений (сама запись и карточка не тронуты).
        guard_error = event_participant_link_guard(storage, event, record, student_id)
        if guard_error is not None:
            return build_redirect_with_message(error=guard_error, url=back_url)

        numeric_record_id = int(record.record_id)
        _, link_error = storage.link_competitions([numeric_record_id], student_id)
        if link_error is not None:
            return build_redirect_with_message(
                error=reconcile_link_error(storage, [numeric_record_id], student_id, link_error),
                url=back_url,
            )

        student = storage.get_student_by_id(student_id)
        full_name = student['full_name'] if student else ''
        log_audit_event(
            request,
            'competition_linked_to_student',
            {
                'record_id': numeric_record_id,
                'old_ref': None,
                'new_ref': student_id,
                'student_id': student_id,
                'source': 'event-participant-page',
            },
        )
        return build_redirect_with_message(
            message=f'Запись привязана к студенту «{full_name}»',
            url=back_url,
        )

    @app.post('/calendar/<event_id>/participants/<record_id>/create-and-link')
    async def calendar_event_participant_create_and_link(request: Request, event_id: str, record_id: str):
        # «Создать и связать» — ОДНА транзакция (create_student_and_link_
        # participation): форма и запись проверяются ДО неё, карточка и связь
        # появляются вместе либо не появляется ничего — карточка-сирота
        # невозможна (в отличие от прецедента admin_reconcile_create_record,
        # где create и link были отдельными транзакциями). Matched-guard (P4)
        # для НОВОЙ карточки избыточен: её свежий student_ref_id не
        # пересекается ни с одним существующим участием, а после привязки у
        # карточки ровно одно участие — сама запись (см. docstring метода);
        # дубль против существующих карточек закрывает POST .../link.
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error

        back_url = f'/calendar/{event["id"]}'
        storage = get_storage(request.app)
        record, record_error = event_participant_for_link(storage, event, record_id)
        if record_error is not None:
            return record_error

        numeric_record_id = int(record.record_id)
        form, form_error = parse_student_form(request)
        if form_error is not None:
            return build_redirect_with_message(
                error=form_error,
                url=f'/calendar/{event["id"]}/participants/{numeric_record_id}/link',
            )

        student_id, create_error = storage.create_student_and_link_participation(
            event_id=int(event['id']),
            record_id=numeric_record_id,
            full_name=form['full_name'],
            sex=form['sex'],
            institute=form['institute'],
            group_name=form['group_name'],
            course=form['course'],
        )
        if create_error is not None:
            if create_error in ('invalid_full_name', 'invalid_sex'):
                # Ревалидация storage (страховка расхождения с parse_student_form):
                # назад на форму, как у обычной ошибки полей.
                form_error_text = (
                    'Укажите ФИО студента.'
                    if create_error == 'invalid_full_name'
                    else 'Пол может быть «М», «Ж» или не указан.'
                )
                return build_redirect_with_message(
                    error=form_error_text,
                    url=f'/calendar/{event["id"]}/participants/{numeric_record_id}/link',
                )
            if create_error == 'already_linked':
                # Гонка внутри транзакции: запись связал другой процесс — полный
                # откат, карточка НЕ создана.
                error_text = f'Запись №{numeric_record_id} уже была привязана другим действием.'
            else:
                # record_not_found / event_mismatch: запись исчезла или перенесена
                # на другое событие между проверкой роута и транзакцией.
                error_text = 'Запись не найдена.'
            return build_redirect_with_message(error=error_text, url=back_url)
        log_audit_event(
            request,
            'student_created',
            {'student_id': student_id, 'full_name': form['full_name'], 'source': 'event-participant-page'},
        )
        log_audit_event(
            request,
            'competition_linked_to_student',
            {
                'record_id': numeric_record_id,
                'old_ref': None,
                'new_ref': student_id,
                'student_id': student_id,
                'source': 'event-participant-page',
            },
        )
        return build_redirect_with_message(
            message=f'Студент «{form["full_name"]}» создан, запись привязана к нему.',
            url=back_url,
        )

    @app.post('/calendar/<event_id>/delete')
    async def delete_calendar_event(request: Request, event_id: str):
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error

        try:
            numeric_id = int(event_id)
        except ValueError:
            return text(body='Invalid event id', status=400)

        storage = get_storage(request.app)
        event = storage.get_calendar_event(numeric_id)
        if event is None:
            return text(body='Event not found', status=404)

        # Удаление с участниками — отказ: сначала удалить участников (волна B
        # даст инструмент), счётчик подсказывает объём.
        participants = storage.count_calendar_event_participants(numeric_id)
        if participants > 0:
            return build_redirect_with_message(
                error=f'У соревнования «{event["name"]}» есть участники: {participants}. '
                'Сначала удалите участников, затем соревнование.',
                url='/calendar',
            )

        storage.delete_calendar_event(numeric_id)
        # Файл положения живёт вне БД: каталог события удаляется вместе с ним
        # (best-effort — как каталоги вложений записей).
        shutil.rmtree(calendar_regulation_dir(numeric_id), ignore_errors=True)
        log_audit_event(
            request,
            'calendar_event_deleted',
            {'event_id': numeric_id, 'name': event['name'], 'date': event['date']},
        )
        return build_redirect_with_message(message='Соревнование удалено', url='/calendar')

    # Файл положения события календаря (решение 2026-09-22): один файл на событие
    # (PDF/JPEG/PNG до 5 МБ, та же валидация, что у вложений), колонки
    # calendar_events.regulation_* + файл в data/files/calendar/<event_id>/.
    # Скачивание — все не-атлеты (как страница события), замена/удаление —
    # модераторы.

    @app.get('/calendar/<event_id>/regulation')
    async def download_calendar_regulation(request: Request, event_id: str):
        # Права — как у страницы события: аноним перенаправляется на вход
        # middleware, атлету страница события недоступна — файл тоже.
        if user_is_athlete(request):
            return forbidden(request)
        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error

        filename = event.get('regulation_filename') or ''
        stored_name = event.get('regulation_stored_name') or ''
        if not filename or not stored_name:
            return text(body='Regulation not found', status=404)
        file_path = regulation_source_path(event['id'], stored_name)
        if not file_path.is_file():
            return text(body='Not Found', status=404)

        extension = Path(filename).suffix.lower().lstrip('.')
        return raw(
            await asyncio.to_thread(file_path.read_bytes),
            headers={
                'content-type': ATTACHMENT_EXTENSIONS.get(extension, 'application/octet-stream'),
                'content-disposition': f'attachment; filename="{filename}"',
            },
        )

    @app.post('/calendar/<event_id>/regulation')
    async def upload_calendar_regulation(request: Request, event_id: str):
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error

        back_url = f'/calendar/{event["id"]}'
        upload_file = request.files.get('regulation')
        if upload_file is None or not upload_file.body:
            return build_redirect_with_message(error='Выберите файл.', url=back_url)
        if len(upload_file.body) > ATTACHMENT_MAX_SIZE:
            return build_redirect_with_message(error='Файл больше 5 МБ', url=back_url)
        filename = (upload_file.name or 'regulation').rsplit('/', 1)[-1]
        content_type = detect_attachment_type(upload_file.body, filename)
        if content_type is None:
            return build_redirect_with_message(error='Допустимы только PDF, JPEG и PNG', url=back_url)

        # Различаем первое прикрепление и замену до загрузки (flash-текст).
        had_regulation = bool(event.get('regulation_filename'))

        # Порядок как у вложений: сначала новый файл на диск, затем колонки БД,
        # затем best-effort удаление прежнего файла (БД уже согласована).
        extension = Path(filename).suffix.lower().lstrip('.') or 'bin'
        stored_name = f'{uuid.uuid4().hex}.{extension}'
        event_dir = calendar_regulation_dir(event['id'])
        event_dir.mkdir(parents=True, exist_ok=True)
        (event_dir / stored_name).write_bytes(upload_file.body)

        old_stored_name = event.get('regulation_stored_name') or ''
        get_storage(request.app).set_calendar_regulation(event['id'], filename, stored_name)

        if old_stored_name:
            try:
                regulation_source_path(event['id'], old_stored_name).unlink(missing_ok=True)
            except OSError:
                logger.warning('Failed to remove old regulation file of event %s', event['id'], exc_info=True)

        log_audit_event(
            request,
            'calendar_regulation_uploaded',
            {
                'event_id': event['id'],
                'event_name': event['name'],
                'filename': filename,
                'replaced': bool(old_stored_name),
            },
        )
        return build_redirect_with_message(
            message='Положение заменено' if had_regulation else 'Положение прикреплено',
            url=back_url,
        )

    @app.post('/calendar/<event_id>/regulation/delete')
    async def delete_calendar_regulation(request: Request, event_id: str):
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error

        back_url = f'/calendar/{event["id"]}'
        filename = event.get('regulation_filename') or ''
        stored_name = event.get('regulation_stored_name') or ''
        if not stored_name:
            return build_redirect_with_message(error='Положение не прикреплено.', url=back_url)

        get_storage(request.app).clear_calendar_regulation(event['id'])
        shutil.rmtree(calendar_regulation_dir(event['id']), ignore_errors=True)
        log_audit_event(
            request,
            'calendar_regulation_deleted',
            {'event_id': event['id'], 'event_name': event['name'], 'filename': filename},
        )
        return build_redirect_with_message(message='Положение удалено', url=back_url)

    @app.get('/calendar/<event_id>/participants/import')
    async def event_participants_import_page(request: Request, event_id: str):
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error
        return await render(
            template_name=jinja_env.get_template('calendar_event_import.html'),
            context={
                'request': request,
                'event': decorate_calendar_event(event),
                **get_flash_args(request),
            },
        )

    @app.get('/calendar/<event_id>/participants/import/template')
    async def export_event_participants_template(request: Request, event_id: str):
        """Пустой шаблон: ФИО|Пол|Институт|Группа|Курс|Место + активные
        custom-поля с show_in_template (кроме url-полей — см.
        event_import_custom_fields). Без колонок события (пресет берётся из
        события) и без каких-либо данных."""
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error

        custom_fields = event_import_custom_fields(get_storage(request.app).get_custom_fields())
        df = pd.DataFrame(columns=event_participant_import_columns(custom_fields))
        buffer = BytesIO()
        await asyncio.to_thread(df.to_excel, buffer, index=False)

        now_str = datetime.utcnow().strftime('%d-%m-%Y_%H-%M-%S')
        return raw(
            buffer.getvalue(),
            headers={
                'content-type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'content-disposition': f'attachment; filename="Шаблон_участники_{now_str}.xlsx"',
            },
        )

    @app.post('/calendar/<event_id>/participants/import')
    async def event_participants_import_upload(request: Request, event_id: str):
        """Загрузка файла: разбор → staging в памяти → предпросмотр.

        Ошибки файла — возврат на страницу загрузки с flash; в БД не пишется
        ничего.
        """
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error

        back_url = f'/calendar/{event["id"]}/participants/import'
        upload_file = request.files.get('file')
        if upload_file is None or not upload_file.body:
            return build_redirect_with_message(error='Выберите файл.', url=back_url)
        if not (upload_file.name or '').lower().endswith('.xlsx'):
            return build_redirect_with_message(error='Файл должен быть в формате .xlsx.', url=back_url)

        try:
            df = await asyncio.to_thread(pd.read_excel, io=upload_file.body)
        except Exception:
            return build_redirect_with_message(
                error='Не удалось прочитать файл. Проверьте, что это не повреждённый .xlsx.',
                url=back_url,
            )

        upload_error = event_import_upload_error(df)
        if upload_error is not None:
            return build_redirect_with_message(error=upload_error, url=back_url)

        custom_fields = event_import_custom_fields(get_storage(request.app).get_custom_fields())
        rows, unknown_columns, custom_columns = await asyncio.to_thread(parse_event_participant_rows, df, custom_fields)

        token = secrets.token_urlsafe(16)
        replace_event_import_session(
            get_current_user_id(request),
            token,
            {
                'token': token,
                'user_id': get_current_user_id(request),
                'created_at': time.time(),
                'event_id': event['id'],
                'filename': upload_file.name or '',
                'unknown_columns': unknown_columns,
                'custom_columns': custom_columns,
                'rows': rows,
            },
        )
        return redirect(event_import_preview_url(event['id'], token))

    @app.get('/calendar/<event_id>/participants/import/preview/<token>')
    async def event_participants_import_preview(request: Request, event_id: str, token: str):
        """Предпросмотр: НОЛЬ записей в БД — только чтение кандидатов/справочников.

        GET-параметры find/q — поиск карточки для конкретной строки («Найти
        студента»): только чтение, в сессию ничего не пишет и не влияет на
        bulk-логику; некорректные find/q молча игнорируются."""
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error
        session, session_error = get_event_import_session(request, token, event['id'])
        if session_error is not None:
            return session_error

        storage = get_storage(request.app)
        search_row, search_query, search_results, search_truncated = event_import_row_search(request, storage, session)
        context = event_import_preview_context(
            storage, event, session, event_import_custom_fields(storage.get_custom_fields())
        )
        return await render(
            template_name=jinja_env.get_template('calendar_event_import_preview.html'),
            context={
                'request': request,
                'is_admin': user_is_admin(request),
                **context,
                'search_row': search_row,
                'search_query': search_query,
                'search_results': search_results,
                'search_truncated': search_truncated,
                **get_flash_args(request),
            },
        )

    @app.post('/calendar/<event_id>/participants/import/preview/<token>/row/<row_number>/select-student')
    async def event_import_row_select_student(request: Request, event_id: str, token: str, row_number: str):
        """Явный выбор карточки для строки: пометка ТОЛЬКО в сессии предпросмотра.

        Автозаполнение и категория пересчитаются при следующем рендере (строка
        переходит в «Готовы к добавлению»); ничего не создаётся.
        """
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, session, row, error = resolve_event_import_row(request, event_id, token, row_number)
        if error is not None:
            return error
        if row['status'] != 'pending':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} уже разобрана.',
                url=event_import_preview_url(event['id'], token),
            )

        student_id, id_error = parse_reconcile_int(get_form_value(request, 'student_id'), 'student id')
        if id_error is not None:
            return id_error
        storage = get_storage(request.app)
        student = storage.get_student_by_id(student_id)
        if student is None:
            return build_redirect_with_message(
                error='Студент не найден.',
                url=event_import_preview_url(event['id'], token),
            )
        if not student['active']:
            return build_redirect_with_message(
                error=f'Карточка #{student_id} неактивна — выберите активную.',
                url=event_import_preview_url(event['id'], token),
            )

        row['student_id'] = student_id
        row['student_forced_none'] = False
        return build_redirect_with_message(
            message=f'Строка {row["row_number"]}: выбрана карточка #{student_id} — {student["full_name"]}.',
            url=event_import_preview_url(event['id'], token),
        )

    @app.post('/calendar/<event_id>/participants/import/preview/<token>/row/<row_number>/clear-student')
    async def event_import_row_clear_student(request: Request, event_id: str, token: str, row_number: str):
        """Сбросить карточку строки: участие будет добавлено без связи.

        Явное решение «без карточки» (подавляет автозаполнение единственным
        точным кандидатом): пометка ТОЛЬКО в сессии предпросмотра, категория
        пересчитается при следующем рендере; запись появится в сопоставлении
        (/admin/people/reconcile) и её можно связать с карточкой позже.
        """
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, session, row, error = resolve_event_import_row(request, event_id, token, row_number)
        if error is not None:
            return error
        if row['status'] != 'pending':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} уже разобрана.',
                url=event_import_preview_url(event['id'], token),
            )

        row['student_id'] = None
        row['student_forced_none'] = True
        return build_redirect_with_message(
            message=f'Строка {row["row_number"]}: карточка сброшена — участие будет добавлено без связи.',
            url=event_import_preview_url(event['id'], token),
        )

    @app.post('/calendar/<event_id>/participants/import/preview/<token>/row/<row_number>/skip')
    async def event_import_row_skip(request: Request, event_id: str, token: str, row_number: str):
        """Пропустить строку (в т.ч. ошибочную — как быстрый способ убрать её
        из виду)."""
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, session, row, error = resolve_event_import_row(request, event_id, token, row_number)
        if error is not None:
            return error
        if row['status'] not in ('pending', 'error'):
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} уже разобрана.',
                url=event_import_preview_url(event['id'], token),
            )

        row['status'] = 'skipped'
        row['added_student_id'] = None
        return build_redirect_with_message(
            message=f'Строка {row["row_number"]} пропущена.',
            url=event_import_preview_url(event['id'], token),
        )

    @app.post('/calendar/<event_id>/participants/import/preview/<token>/row/<row_number>/edit')
    async def event_import_row_edit(request: Request, event_id: str, token: str, row_number: str):
        """Правка строки в сессии: ФИО/пол/институт/группа/курс/место/дисциплина/
        результат/custom.

        Некорректные данные — строка становится ошибочной; после правки ФИО
        кандидаты и автозаполнение пересчитаются при следующем рендере (строка
        мигрирует между группами естественно). Изменённое ФИО сбрасывает ОБА
        решения по карточке — явный выбор и «без карточки» (оба относились к
        прежнему ФИО и могли устареть): для нового ФИО кандидаты подбираются
        заново, решение принимают отдельным действием.
        """
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, session, row, error = resolve_event_import_row(request, event_id, token, row_number)
        if error is not None:
            return error
        if row['status'] not in ('pending', 'error'):
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} уже разобрана.',
                url=event_import_preview_url(event['id'], token),
            )

        storage = get_storage(request.app)
        custom_fields = event_import_custom_fields(storage.get_custom_fields())
        # Правка ФИО сбрасывает явный выбор карточки и решение «без карточки»:
        # оба решения относятся к прежнему ФИО и могли стать ошибочными
        # (устаревшее молчаливо переносить нельзя). Кандидаты и автозаполнение
        # пересчитаются при следующем рендере.
        new_name = get_form_value(request, 'full_name').strip()
        if new_name.casefold() != (row['full_name'] or '').casefold():
            row['student_id'] = None
            row['student_forced_none'] = False
        row['full_name'] = new_name
        row['sex'] = get_form_value(request, 'sex').strip()
        row['institute'] = get_form_value(request, 'institute').strip()
        row['group'] = get_form_value(request, 'group').strip()
        row['course'] = get_form_value(request, 'course').strip()
        row['position'] = get_form_value(request, 'position').strip()
        row['discipline'] = get_form_value(request, 'discipline').strip()
        row['result'] = get_form_value(request, 'result').strip()
        # Все импортируемые custom-поля (в т.ч. отсутствовавшие в файле —
        # обязательное поле можно заполнить при правке); url-поля и коллидирующие
        # label «Дисциплина»/«Результат» сюда не входят (фиксированные колонки).
        for field in custom_fields:
            row['custom'][field.key] = get_form_value(request, f'custom__{field.key}').strip()
        error = None
        if not row['full_name']:
            error = 'Пустое ФИО.'
        elif row['sex'] not in STUDENT_SEX_OPTIONS:
            error = 'Пол должен быть «М» или «Ж».'
        else:
            # Быстрая проверка правки тем же путём, что предпросмотр: с карточкой
            # строки (явный выбор/единственный кандидат) — иначе пустой курс,
            # который подставился бы из карточки, ложно проваливал бы правку.
            candidates = storage.find_student_candidates(row['full_name'])
            student = event_import_effective_student(storage, row, candidates)
            record, _, _ = event_import_row_record(
                storage,
                row,
                event_participant_preset(event),
                student,
                custom_fields,
                student_file_group_institutes(session['rows']),
            )
            _, error = build_event_participant_competition(record, custom_fields, storage, result=row['result'])
        if error is not None:
            row['status'] = 'error'
            row['error'] = error
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} обновлена, но данные некорректны: {error}',
                url=event_import_preview_url(event['id'], token),
            )
        row['status'] = 'pending'
        row['error'] = None
        return build_redirect_with_message(
            message=f'Строка {row["row_number"]} обновлена.',
            url=event_import_preview_url(event['id'], token),
        )

    @app.post('/calendar/<event_id>/participants/import/preview/<token>/row/<row_number>/add')
    async def event_import_row_add(request: Request, event_id: str, token: str, row_number: str):
        """Добавить одну строку («готовую»): участие создаётся сразу, решение
        фиксируется в сессии («Решено: добавлен · карточка #N»)."""
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, session, row, error = resolve_event_import_row(request, event_id, token, row_number)
        if error is not None:
            return error
        if row['status'] == 'error':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} содержит ошибку — исправьте её.',
                url=event_import_preview_url(event['id'], token),
            )
        if row['status'] != 'pending':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} уже разобрана.',
                url=event_import_preview_url(event['id'], token),
            )

        storage = get_storage(request.app)
        add_error = await asyncio.to_thread(event_import_add_row_participation, request, storage, event, session, row)
        if add_error is not None:
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]}: {add_error}',
                url=event_import_preview_url(event['id'], token),
            )
        return build_redirect_with_message(
            message=f'Строка {row["row_number"]}: участник добавлен.',
            url=event_import_preview_url(event['id'], token),
        )

    @app.post('/calendar/<event_id>/participants/import/preview/<token>/row/<row_number>/update-existing')
    async def event_import_row_update_existing(request: Request, event_id: str, token: str, row_number: str):
        """Обновить место/результат СУЩЕСТВУЮЩЕГО участия по строке предпросмотра
        (P4; только update-candidate-строки: та же карточка+дисциплина, место
        или результат отличаются).

        Повторная классификация на клике: участие найдено и mutable отличаются —
        restricted-update (storage.update_event_participation_result: SET только
        position/result, WHERE id + calendar_event_id — снимок, custom-поля,
        вложения, student_ref_id и связь с событием недостижимы). Участие
        исчезло/стало равным — flash, строка остаётся pending и
        переклассифицируется. Фиксирует статус updated + дифф; аудит
        event_participant_updated."""
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, session, row, error = resolve_event_import_row(request, event_id, token, row_number)
        if error is not None:
            return error
        if row['status'] == 'error':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} содержит ошибку — исправьте её.',
                url=event_import_preview_url(event['id'], token),
            )
        if row['status'] != 'pending':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} уже разобрана.',
                url=event_import_preview_url(event['id'], token),
            )

        storage = get_storage(request.app)
        classification, competition, recheck_error = await asyncio.to_thread(
            event_import_update_candidate_classification, storage, event, session, row
        )
        if recheck_error is not None:
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]}: {recheck_error}',
                url=event_import_preview_url(event['id'], token),
            )
        if classification is None or classification['status'] != 'update-candidate' or competition is None:
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]}: обновление недоступно — участие не найдено '
                'или место/результат не отличаются.',
                url=event_import_preview_url(event['id'], token),
            )
        match = classification['match']
        updated = await asyncio.to_thread(
            storage.update_event_participation_result,
            match['record_id'],
            event['id'],
            competition.position,
            row.get('result') or None,
        )
        if not updated:
            return build_redirect_with_message(
                error='Участники события изменились — обновите страницу и проверьте строки.',
                url=event_import_preview_url(event['id'], token),
            )
        row['status'] = 'updated'
        # Дифф фиксируется на момент действия (дальнейшие изменения БД бейдж не меняют).
        row['update_diff'] = classification['diff']
        _, old_result = event_participation_effective_values(match, storage.get_custom_fields())
        log_audit_event(
            request,
            'event_participant_updated',
            {
                'event_id': event['id'],
                'record_id': match['record_id'],
                'position': {'old': match['position'], 'new': competition.position},
                'result': {'old': old_result, 'new': (row.get('result') or '')},
            },
        )
        return build_redirect_with_message(
            message=f'Строка {row["row_number"]}: существующее участие обновлено.',
            url=event_import_preview_url(event['id'], token),
        )

    @app.post('/calendar/<event_id>/participants/import/preview/<token>/row/<row_number>/keep-existing')
    async def event_import_row_keep_existing(request: Request, event_id: str, token: str, row_number: str):
        """Оставить СУЩЕСТВУЮЩЕЕ участие как есть по строке-кандидату (P4; только
        update-candidate-строки): статус kept, в БД не меняется НИЧЕГО, аудит не
        пишется — явное решение «новое место/результат из файла не переносим»."""
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, session, row, error = resolve_event_import_row(request, event_id, token, row_number)
        if error is not None:
            return error
        if row['status'] == 'error':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} содержит ошибку — исправьте её.',
                url=event_import_preview_url(event['id'], token),
            )
        if row['status'] != 'pending':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} уже разобрана.',
                url=event_import_preview_url(event['id'], token),
            )

        storage = get_storage(request.app)
        classification, _, recheck_error = await asyncio.to_thread(
            event_import_update_candidate_classification, storage, event, session, row
        )
        if recheck_error is not None:
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]}: {recheck_error}',
                url=event_import_preview_url(event['id'], token),
            )
        if classification is None or classification['status'] != 'update-candidate':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]}: оставление недоступно — участие не найдено '
                'или место/результат не отличаются.',
                url=event_import_preview_url(event['id'], token),
            )
        row['status'] = 'kept'
        return build_redirect_with_message(
            message=f'Строка {row["row_number"]}: оставлено существующее участие.',
            url=event_import_preview_url(event['id'], token),
        )

    @app.post('/calendar/<event_id>/participants/import/preview/<token>/row/<row_number>/create-student')
    async def event_import_row_create_student(request: Request, event_id: str, token: str, row_number: str):
        """Создать карточку по строке и СРАЗУ добавить связанного участника.

        Одно явное решение админа: форма предзаполнена Excel-значениями и
        проверена перед отправкой. Сначала валидируется БУДУЩЕЕ участие (с
        данными будущей карточки), затем создаётся карточка (audit
        student_created, source='event-participant-import') и участие с
        student_ref_id новой карточки. Доступно только admin — editor создаёт
        карточки через раздел Students (admin-only, конвенция Phase 1/2.5).
        """
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        event, session, row, error = resolve_event_import_row(request, event_id, token, row_number)
        if error is not None:
            return error
        if row['status'] == 'error':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} содержит ошибку — исправьте её.',
                url=event_import_preview_url(event['id'], token),
            )
        if row['status'] != 'pending':
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]} уже разобрана.',
                url=event_import_preview_url(event['id'], token),
            )

        storage = get_storage(request.app)
        full_name = get_form_value(request, 'full_name').strip()
        sex = get_form_value(request, 'sex').strip()
        institute = get_form_value(request, 'institute').strip()
        group_name = get_form_value(request, 'group').strip()
        course = get_form_value(request, 'course').strip()
        form_error = None
        if not full_name:
            form_error = 'Пустое ФИО.'
        elif sex not in STUDENT_SEX_OPTIONS:
            form_error = 'Пол должен быть «М» или «Ж».'
        if form_error is not None:
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]}: {form_error}',
                url=event_import_preview_url(event['id'], token),
            )

        hints = student_catalog_hints(storage, institute, group_name, student_file_group_institutes(session['rows']))
        add_error = await asyncio.to_thread(
            event_import_create_student_participation,
            request,
            storage,
            event,
            session,
            row,
            full_name,
            sex,
            hints,
            course,
        )
        if add_error is not None:
            return build_redirect_with_message(
                error=f'Строка {row["row_number"]}: {add_error}',
                url=event_import_preview_url(event['id'], token),
            )
        return build_redirect_with_message(
            message=f'Строка {row["row_number"]}: студент «{full_name}» создан, участник добавлен.',
            url=event_import_preview_url(event['id'], token),
        )

    @app.post('/calendar/<event_id>/participants/import/preview/<token>/bulk-commit')
    async def event_import_bulk_commit(request: Request, event_id: str, token: str):
        """Добавить «готовые» строки одной транзакцией.

        В батч входят ТОЛЬКО строки «Готовы к добавлению» на момент последнего
        рендера (фиксированные признаки had_validation_error/had_import_ready);
        строки review (update-candidate/possible-duplicate/конфликт), производные
        «уже существует»/«дубликат в файле» и ошибочные в батч не входят.
        Предпроверка: эффективная карточка каждой строки батча пересчитывается
        на момент клика и сравнивается с зафиксированной рендером
        (had_student_id — id или None) — если состав «готовых» изменился, не
        вставляется ничего (строки перейдут в правильные группы при следующем
        рендере). Строка без карточки добавляется с student_ref_id IS NULL
        (сопоставить можно позже). P4: вставка — apply_event_participation_batch:
        повторная проверка уникальности (identity/cardless-контент) под _lock,
        коллизия — откат всего батча вместе со справочниками и flash «участники
        изменились»; уже существующие/дубли в файле в успешный батч не попадают.
        """
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error
        session, session_error = get_event_import_session(request, token, event['id'])
        if session_error is not None:
            return session_error

        storage = get_storage(request.app)
        prepared, prepare_error = await asyncio.to_thread(
            event_import_prepare_bulk_rows,
            storage,
            event,
            session,
            event_import_custom_fields(storage.get_custom_fields()),
        )
        if prepare_error is not None:
            return build_redirect_with_message(
                error=prepare_error,
                url=event_import_preview_url(event['id'], token),
            )

        if prepared:
            try:
                await asyncio.to_thread(
                    storage.apply_event_participation_batch,
                    event['id'],
                    [competition for _, competition, _ in prepared],
                    owner_id=get_current_user_id(request),
                )
            except EventParticipationConflictError:
                return build_redirect_with_message(
                    error='Участники события изменились — обновите страницу и проверьте строки.',
                    url=event_import_preview_url(event['id'], token),
                )
        for row, _, student in prepared:
            row['status'] = 'added'
            row['student_id'] = student['id'] if student else None
            row['added_student_id'] = student['id'] if student else None
            log_audit_event(
                request,
                'event_participant_imported',
                {'event_id': event['id'], 'student_ref_id': row['added_student_id']},
            )
        return build_redirect_with_message(
            message=f'Добавлено участников из Excel: {len(prepared)}.',
            url=f'/calendar/{event["id"]}',
        )

    @app.post('/calendar/<event_id>/participants/import/preview/<token>/finish')
    async def event_import_finish(request: Request, event_id: str, token: str):
        """Завершить импорт: итоговое audit-событие с счётчиками (без содержимого
        файла), сессия удаляется.

        Неразобранные pending-строки блокируют завершение; P4: производные
        «уже существует»/«дубликат в файле» решений не требуют (учтены в
        счётчиках already_exists/file_duplicates и отбрасываются вместе с
        сессией); ошибочные — не блокируют (учтены как errors, их можно
        пропустить по одной).
        """
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error
        session, session_error = get_event_import_session(request, token, event['id'])
        if session_error is not None:
            return session_error

        storage = get_storage(request.app)
        states = event_import_pending_states(
            storage, event, session, event_import_custom_fields(storage.get_custom_fields())
        )
        # Требуют решения: готовые и review-строки; производные повторы
        # (already/дубликат в файле) завершение не блокируют.
        pending = sum(1 for state in states.values() if state['import_status'] not in EVENT_IMPORT_DERIVED_RESOLVED)
        if pending:
            return build_redirect_with_message(
                error=f'Завершение недоступно: не разобрано строк: {pending}.',
                url=event_import_preview_url(event['id'], token),
            )

        derived_counts: dict[str, int] = {}
        for state in states.values():
            derived_counts[state['import_status']] = derived_counts.get(state['import_status'], 0) + 1
        counters = event_import_counters(session, derived_counts)
        log_audit_event(
            request,
            'event_participants_import_completed',
            {'event_id': event['id'], 'event_name': event['name'], 'counters': counters},
        )
        event_import_sessions.pop(token, None)
        summary = ', '.join(
            f'{label} {counters[key]}'
            for label, key in (
                ('добавлено', 'added'),
                ('обновлено', 'updated'),
                ('оставлено', 'kept'),
                ('уже существует', 'already_exists'),
                ('дубликатов в файле', 'file_duplicates'),
                ('пропущено', 'skipped'),
            )
            if counters[key]
        )
        return build_redirect_with_message(
            message=f'Импорт завершён: {summary}.' if summary else 'Импорт завершён.',
            url=f'/calendar/{event["id"]}',
        )

    @app.post('/calendar/<event_id>/participants/import/preview/<token>/discard')
    async def event_import_discard(request: Request, event_id: str, token: str):
        """Отменить импорт: сессия удаляется без аудита. Уже добавленные
        подтверждением участники остаются (добавление было явным)."""
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        event, error = get_calendar_event_or_error(request, event_id)
        if error is not None:
            return error
        session, session_error = get_event_import_session(request, token, event['id'])
        if session_error is not None:
            return session_error

        added = sum(1 for row in session['rows'] if row['status'] == 'added')
        event_import_sessions.pop(token, None)
        if added:
            return build_redirect_with_message(
                message=f'Импорт отменён. Добавленные участники ({added}) сохранены.',
                url=f'/calendar/{event["id"]}',
            )
        return build_redirect_with_message(message='Импорт отменён.', url=f'/calendar/{event["id"]}')

    @app.get('/competition/<record_id>/link-event')
    async def competition_link_event_page(request: Request, record_id: str):
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error

        try:
            numeric_id = int(record_id)
        except ValueError:
            return text(body='Invalid record id', status=400)

        storage = get_storage(request.app)
        record = storage.get_competition_by_id(numeric_id)
        if record is None:
            return text(body='Record not found', status=404)
        if record.calendar_event_id:
            return build_redirect_with_message(
                error=f'Запись №{numeric_id} уже связана с соревнованием.',
                url='/',
            )

        args = dict(request.args)
        filters, filter_error = parse_link_event_filters(args)
        if filter_error is not None:
            return text(body=filter_error, status=400)
        # Первый заход без параметров ищет по пресету записи (форма предзаполнена
        # его же значениями) — пресет-кандидаты видны сразу; «Сбросить» возвращает
        # к этому же состоянию.
        if not any((get_param(args, key) or '').strip() for key in LINK_EVENT_SEARCH_KEYS):
            filters = {
                'name': record.name,
                'sport': '',
                'level': '',
                'date_from': record.date.strftime(settings.date_format),
                'date_to': record.date_to.strftime(settings.date_format) if record.date_to else '',
            }

        candidates, found_total = decorate_link_event_candidates(
            record,
            filter_link_event_candidates(storage.list_calendar_events(), filters),
        )

        return await render(
            template_name=jinja_env.get_template('competition_link.html'),
            context={
                'request': request,
                'record': record,
                'record_id': numeric_id,
                'filters': filters,
                'candidates': candidates,
                'found_total': found_total,
                'candidates_capped': found_total > LINK_EVENT_CANDIDATE_CAP,
                'candidate_cap': LINK_EVENT_CANDIDATE_CAP,
                # Нет кандидатов — секция создания события раскрыта сразу.
                'new_form_open': found_total == 0,
                'sport_options': storage.list_catalog('sport'),
                'level_options': storage.get_level_names(),
                **get_flash_args(request),
            },
        )

    @app.post('/competition/<record_id>/link-event')
    async def competition_link_event(request: Request, record_id: str):
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error

        try:
            numeric_id = int(record_id)
        except ValueError:
            return text(body='Invalid record id', status=400)

        storage = get_storage(request.app)
        record = storage.get_competition_by_id(numeric_id)
        if record is None:
            return text(body='Record not found', status=404)

        try:
            event_id = int(get_form_value(request, 'event_id'))
        except ValueError:
            return text(body='Invalid event id', status=400)

        event, error = storage.link_participation_to_event(numeric_id, event_id)
        if error == 'record_not_found':
            return text(body='Record not found', status=404)
        if error == 'already_linked':
            return build_redirect_with_message(
                error=f'Запись №{numeric_id} уже связана с соревнованием.',
                url='/',
            )
        if error == 'event_not_found':
            return build_redirect_with_message(error='Соревнование не найдено.', url='/')

        log_audit_event(
            request,
            'participation_linked',
            {
                'record_id': numeric_id,
                'student_name': record.student_name,
                'event_id': event_id,
                'event_name': event['name'],
                'changes': competition_link_changes(record, event),
            },
        )
        return build_redirect_with_message(
            message=f'Запись №{numeric_id} связана с соревнованием «{event["name"]}».',
            url='/',
        )

    @app.post('/competition/<record_id>/link-event/new')
    async def competition_link_event_new(request: Request, record_id: str):
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error

        try:
            numeric_id = int(record_id)
        except ValueError:
            return text(body='Invalid record id', status=400)

        storage = get_storage(request.app)
        record = storage.get_competition_by_id(numeric_id)
        if record is None:
            return text(body='Record not found', status=404)

        values, error = parse_calendar_event_form(request)
        if error is not None:
            return text(body=error, status=400)

        new_date = values['date'].isoformat()
        new_date_to = values['date_to'].isoformat() if values['date_to'] else None
        event_id, error = storage.create_calendar_event_and_link(
            name=values['name'],
            date=new_date,
            date_to=new_date_to,
            level=values['level'],
            sport=values['sport'],
            url=values['url'],
            record_id=numeric_id,
        )
        if error == 'record_not_found':
            return text(body='Record not found', status=404)
        if error == 'already_linked':
            return build_redirect_with_message(
                error=f'Запись №{numeric_id} уже связана с соревнованием.',
                url='/',
            )
        if error == 'duplicate_event':
            # Guard дублей (2026-09-27): точный дубль уже есть — событие не
            # создано, запись не связана; возвращаем на страницу связывания,
            # она уже рендерит выбор существующих событий (контракт P5b).
            # Аудита calendar_event_created/participation_linked нет (блок —
            # не успех).
            return build_redirect_with_message(
                error=(
                    f'Событие «{values["name"]}» от {values["date"].strftime(settings.date_format)}'
                    ' уже существует — новое не создано, запись не связана. Выберите его из списка ниже.'
                ),
                url=f'/competition/{numeric_id}/link-event',
            )

        log_audit_event(
            request,
            'calendar_event_created',
            {
                'event_id': event_id,
                'name': values['name'],
                'date': new_date,
                'date_to': new_date_to,
                'sport': values['sport'],
                'level': values['level'],
                'url': values['url'],
                'linked_record_id': numeric_id,
                'source': 'registry',
            },
        )
        log_audit_event(
            request,
            'participation_linked',
            {
                'record_id': numeric_id,
                'student_name': record.student_name,
                'event_id': event_id,
                'event_name': values['name'],
                'changes': competition_link_changes(
                    record,
                    {
                        'name': values['name'],
                        'sport': values['sport'],
                        'date': new_date,
                        'date_to': new_date_to,
                        'level': values['level'],
                    },
                ),
                'event_created': True,
            },
        )
        return build_redirect_with_message(
            message=f'Соревнование «{values["name"]}» запланировано, запись №{numeric_id} связана с ним.',
            url='/',
        )
