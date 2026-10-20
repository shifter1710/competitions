"""Маршруты отчётов: страница отчётов, отчёт по фильтрам и выгрузка в Excel.

Reports v2 (docs/data-model-decisions.md «Reports v2»): 6 метрик с
identity-правилом уникальных, период-учебный год, фильтры курс/год
поступления, GET-навигация /reports (канонический адрес). /report остаётся
фрагментом результата для существующих ссылок и тестов.

Перенесено из src/main.py без изменения поведения (Architecture v1),
расширено Reports v2."""
import asyncio
import re
from datetime import datetime
from io import BytesIO
from typing import Sequence
from urllib.parse import urlencode

from openpyxl import Workbook
from sanic import Request
from sanic import Sanic
from sanic import text
from sanic.response import raw
from sanic_ext import render

from src.auth import user_is_athlete
from src.models.custom_field import CustomField
from src.periods import build_report_period_options
from src.periods import current_report_period_value
from src.periods import REPORT_PERIOD_ALL
from src.periods import REPORT_PERIOD_CUSTOM
from src.periods import report_period_label
from src.periods import resolve_report_period
from src.periods import validate_report_period
from src.records import sanitize_spreadsheet_value
from src.web import forbidden
from src.web import get_param
from src.web import get_storage
from src.web import jinja_env
from src.web import ru_plural


# Расширение отчётов (замечание №19, docs/data-model-decisions.md «Расширение
# отчётов»): срез × метрики × фильтры. Группировка всегда по данным записи.
# Reports v2: добавлен срез «год поступления» (admission_year), подпись
# «календарный год» отличает его от учебного года периода.
REPORT_SLICES: Sequence[dict[str, str]] = (
    {'key': 'student', 'label': 'Студент'},
    {'key': 'group', 'label': 'Группа'},
    {'key': 'institute', 'label': 'Институт'},
    {'key': 'course', 'label': 'Курс'},
    {'key': 'admission_year', 'label': 'Год поступления'},
    {'key': 'sport', 'label': 'Вид спорта'},
    {'key': 'level', 'label': 'Уровень'},
    {'key': 'year', 'label': 'Календарный год'},
)


REPORT_SLICE_KEYS = {item['key'] for item in REPORT_SLICES}


DEFAULT_REPORT_SLICE = 'student'


REPORT_METRIC_COLUMNS: Sequence[str] = ('Участий', 'Побед', 'Призовых')

# Reports v2: уникальные метрики показываются у всех срезов, кроме
# «студента» (внутри его группы один identity — счётчик всегда 1).
REPORT_UNIQUE_METRIC_COLUMNS: Sequence[str] = ('Уникальных участников', 'Уникальных призёров')


# Колонки среза (без метрик) в HTML-таблице и выгрузке: для «студента» —
# как раньше (ФИО/пол/институт/группа/курс), для остальных — название среза.
REPORT_SLICE_COLUMNS: dict[str, tuple[str, ...]] = {
    'student': ('ФИО', 'Пол', 'Институт', 'Группа', 'Курс'),
    'group': ('Группа',),
    'institute': ('Институт',),
    'course': ('Курс',),
    'admission_year': ('Год поступления',),
    'sport': ('Вид спорта',),
    'level': ('Уровень соревнований',),
    'year': ('Год',),
}


# Колонки с числовым значением среза (сортировка в HTML-таблице).
REPORT_SLICE_NUMERIC_KEYS = frozenset({'course', 'year', 'admission_year'})


# Старые ссылки/закладки с выбором колонок продолжают работать.
LEGACY_REPORT_COLUMN_ALIASES = {'Количество участий': 'Участий'}


# Отображение пустого значения среза (Reports v2): NULL/'' — «Не указан»,
# данные в БД не переписываются.
REPORT_EMPTY_SLICE_LABEL = 'Не указано'


def report_export_columns(slice_key: str = DEFAULT_REPORT_SLICE) -> list[str]:
    """Колонки выгрузки для среза: название среза + метрики.

    «Студент» — 3 счётчика, остальные срезы — 5 (с уникальными).
    """
    metrics = (
        REPORT_METRIC_COLUMNS
        if slice_key == DEFAULT_REPORT_SLICE
        else (*REPORT_METRIC_COLUMNS, *REPORT_UNIQUE_METRIC_COLUMNS)
    )
    return [*REPORT_SLICE_COLUMNS[slice_key], *metrics]


def parse_report_slice(args: dict) -> str:
    """Срез отчёта из GET-параметра group_by; отсутствие — «студент»."""
    value = get_param(args, 'group_by')
    return value if value else DEFAULT_REPORT_SLICE


def validate_report_filters(args: dict) -> str | None:
    error = validate_report_period(args)
    if error:
        return error

    if any(not re.fullmatch(r'[1-9]\d*', value) for value in report_filter_values(args, 'event_id')):
        return 'Некорректный ID соревнования'

    position = get_param(args, 'position')
    # Reports v2: голое 0 — класс «Без результата» (position = 0).
    if position and not re.fullmatch(r'(?:[<>]\d+|0)', position):
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


# --- Применённое состояние отчёта (Reports v2, GET-навигация). ---

# Канонический порядок параметров отчёта (чипы, скрытые поля экспорта,
# data-applied-query для dirty-детекта). Пустые значения не применяются.
REPORT_FILTER_KEYS: tuple[str, ...] = (
    'name',
    'position',
    'level',
    'institute',
    'group',
    'sport',
    'event_id',
    'course',
    'admission_year',
    'group_by',
)


REPORT_MULTI_KEYS = frozenset({'institute', 'group', 'sport', 'level', 'event_id'})


def report_filter_values(args: dict, key: str) -> list[str]:
    """Repeated GET values, with scalar helper compatibility and no empty restriction."""
    raw = args.get(key, [])
    values = [raw] if isinstance(raw, str) else raw
    return sorted({str(value).strip() for value in values if str(value).strip()})


def collect_report_period_entries(args: dict) -> list[tuple[str, str]]:
    """Период и даты применённого запроса (канонический вид).

    period=ГГГГ-ГГГГ/all поглощает прямые даты (они игнорируются сервером
    и не должны мешать dirty-детекту), для custom даты идут рядом с
    period. Легаси-ссылка с ОБЕИМИ датами без period канонизируется в
    period=custom + даты: так же восстанавливается форма
    (build_report_form_values), поэтому data-applied-query совпадает с
    сериализацией формы и без действий пользователя она не «грязная»;
    экспорт (period=custom + обе даты) валиден. Одна дата без period
    остаётся как есть — экспорт такой ссылки без period валиден.
    """
    period = (get_param(args, 'period') or '').strip()
    date_from = (get_param(args, 'date_from') or '').strip()
    date_to = (get_param(args, 'date_to') or '').strip()
    if period and period != REPORT_PERIOD_CUSTOM:
        return [('period', period)]
    entries: list[tuple[str, str]] = []
    if period:
        entries.append(('period', period))
    elif date_from and date_to:
        entries.append(('period', REPORT_PERIOD_CUSTOM))
    for key, value in (('date_from', date_from), ('date_to', date_to)):
        if value:
            entries.append((key, value))
    return entries


def collect_applied_report_query(args: dict, custom_fields: Sequence[CustomField]) -> list[tuple[str, str]]:
    """Применённые параметры отчёта в каноническом порядке.

    Период — первым, разрез — вторым (порядок чипов контракта UI); правила
    периода/дат — collect_report_period_entries.
    """
    entries: list[tuple[str, str]] = collect_report_period_entries(args)
    for key in REPORT_FILTER_KEYS:
        if key in REPORT_MULTI_KEYS:
            entries.extend((key, value) for value in report_filter_values(args, key))
            continue
        value = (get_param(args, key) or '').strip()
        if not value:
            continue
        # Дефолтный разрез «студент» — как отсутствие параметра (сервер
        # считает его дефолтом); не попасть в URL/чипы/экспорт.
        if key == 'group_by' and value == DEFAULT_REPORT_SLICE:
            continue
        entries.append((key, value))
    for field in custom_fields:
        value = (get_param(args, f'custom__{field.key}') or '').strip()
        if value:
            entries.append((f'custom__{field.key}', value))
    return entries


def build_report_filter_kwargs(request: Request) -> tuple[dict, str | None]:
    """Общие kwargs фильтров для строк отчёта и сводки + текст ошибки."""
    args = dict(request.args)
    error = validate_report_filters(args) or collect_custom_filters(request)[1]
    if error:
        return {}, error
    date_from, date_to = resolve_report_period(args)
    filter_kwargs = dict(
        date_from=date_from,
        date_to=date_to,
        position=(get_param(args, 'position') or '').strip(),
        level=report_filter_values(args, 'level'),
        name=(get_param(args, 'name') or '').strip(),
        institute=report_filter_values(args, 'institute'),
        group=report_filter_values(args, 'group'),
        sport=report_filter_values(args, 'sport'),
        course=(get_param(args, 'course') or '').strip(),
        admission_year=(get_param(args, 'admission_year') or '').strip(),
        custom_filters=collect_custom_filters(request)[0],
        event_id=report_filter_values(args, 'event_id'),
    )
    return filter_kwargs, None


def get_report_rows(storage, slice_key: str, filter_kwargs: dict) -> Sequence:
    """Строки отчёта для среза с фильтрами.

    Для «студента» — StudentInfo (снимок записи + 3 счётчика), для остальных
    срезов — ReportSliceRow (значение поля записи + 5 метрик).
    """
    if slice_key == DEFAULT_REPORT_SLICE:
        return storage.get_filtered(**filter_kwargs)
    return storage.get_grouped_report(slice_key, **filter_kwargs)


# --- Чипы применённых условий (Reports v2: ссылки, не JS). ---

REPORT_POSITION_CHIP_LABELS = {
    '<2': 'Победа',
    '<4': 'Призовое место',
    '>3': 'Не призовое место',
    '0': 'Без результата',
}

REPORT_CHIP_LABELS: dict[str, str] = {
    'group_by': 'Разрез',
    'name': 'ФИО',
    'position': 'Место',
    'level': 'Уровень',
    'institute': 'Институт',
    'group': 'Группа',
    'sport': 'Вид спорта',
    'event_id': 'Соревнование',
    'course': 'Курс',
    'admission_year': 'Год поступления',
}


def report_url_without(entries: Sequence[tuple[str, str]], removed_keys: set[str]) -> str:
    """/reports без перечисленных параметров применённого запроса."""
    kept = urlencode([(key, value) for key, value in entries if key not in removed_keys])
    return '/reports' + (f'?{kept}' if kept else '')


def report_chip_value(key: str, value: str, event_labels: dict[str, str]) -> str:
    if key == 'group_by':
        return next((item['label'] for item in REPORT_SLICES if item['key'] == value), value)
    if key == 'position':
        return REPORT_POSITION_CHIP_LABELS.get(value, value)
    if key == 'admission_year' and value == 'none':
        return REPORT_EMPTY_SLICE_LABEL
    if key == 'event_id':
        return event_labels.get(value, f'№{value} (недоступно)')
    return value


def build_report_chips(
    entries: Sequence[tuple[str, str]],
    custom_fields: Sequence[CustomField],
    event_labels: dict[str, str] | None = None,
) -> list[dict]:
    """Чипы применённых условий отчёта (порядок: период → разрез → фильтры
    → кастомные). Крестик чипа — обычная ссылка на /reports без его
    параметров (без JS); период-чип снимает period + обе даты."""
    args = dict(entries)
    chips: list[dict] = []

    has_period = 'period' in args or 'date_from' in args or 'date_to' in args
    if has_period:
        chips.append(
            {
                'label': 'Период',
                'value': report_period_label(args),
                'remove_url': report_url_without(entries, {'period', 'date_from', 'date_to'}),
            }
        )

    for key, label in REPORT_CHIP_LABELS.items():
        for entry_key, raw_value in entries:
            if entry_key != key or not raw_value:
                continue
            display = report_chip_value(key, raw_value, event_labels or {})
            kept = [(k, v) for k, v in entries if (k, v) != (key, raw_value)]
            chips.append({'label': label, 'value': display, 'remove_url': '/reports?' + urlencode(kept)})

    field_labels = {f'custom__{field.key}': field.label for field in custom_fields}
    for key, value in entries:
        if key.startswith('custom__'):
            chips.append(
                {'label': field_labels.get(key, key), 'value': value, 'remove_url': report_url_without(entries, {key})}
            )
    return chips


def parse_report_export_columns(request: Request, slice_key: str) -> tuple[Sequence[str], str | None]:
    """Выбор колонок для выгрузки отчёта (GET-параметр ``columns``).

    Формат — повторённые параметры и/или список через запятую. Набор колонок
    зависит от среза (group_by): «название среза + метрики» (3 счётчика для
    «студента», 5 с уникальными для остальных). Порядок в файле всегда
    канонический (как в HTML-отчёте), неизвестные имена игнорируются,
    старое имя «Количество участий» — алиас «Участий». Без параметра —
    полный набор (обратная совместимость; пустой ``?columns=`` парсер
    запроса отбрасывает так же, как отсутствие параметра). Параметр есть,
    но валидных колонок не осталось — 400: минимум одна колонка обязательна.
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


# --- Выгрузка XLSX (Reports v2: шапка параметров + строка «Итого»). ---


def report_slice_display_value(value) -> str | int:
    """Значение среза для экрана и XLSX: пустое — «Не указан»."""
    return value if value not in (None, '') else REPORT_EMPTY_SLICE_LABEL


def build_report_total_row(columns: Sequence[str], summary) -> list:
    """Строка «Итого»: метрики из сводного запроса по всей выборке
    (distinct-метрики — НЕ суммы по строкам таблицы)."""
    values = {
        'Участий': summary.count_participation,
        'Побед': summary.count_wins,
        'Призовых': summary.count_prizes,
        'Уникальных участников': summary.count_unique_participants,
        'Уникальных призёров': summary.count_unique_prizers,
    }
    total_row: list = []
    label_written = False
    for column in columns:
        if column in values:
            total_row.append(values[column])
        else:
            # Неметрические колонки: «Итого» в первой, остальное пусто.
            total_row.append('Итого' if not label_written else '')
            label_written = True
    return total_row


def build_report_workbook(
    report_rows: Sequence,
    columns: Sequence[str],
    slice_key: str,
    summary,
    *,
    period_label: str,
    filter_chips: Sequence[dict],
    slice_label: str,
) -> BytesIO:
    """XLSX отчёта: шапка параметров, заголовки, данные, «Итого».

    Строки данных — в каноническом порядке сервера, до визуальной
    группировки HTML. Защита от formula
    injection — sanitize_spreadsheet_value (как в реестре).
    """
    slice_column = REPORT_SLICE_COLUMNS[slice_key][0]
    metric_values = {
        'Участий': lambda row: row.count_participation,
        'Побед': lambda row: row.count_wins,
        'Призовых': lambda row: row.count_prizes,
        'Уникальных участников': lambda row: row.count_unique_participants,
        'Уникальных призёров': lambda row: row.count_unique_prizers,
    }
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Отчет'
    sheet.append(['Отчет по участию в соревнованиях'])
    sheet.append(['Период:', period_label])
    if filter_chips:
        sheet.append(['Фильтры:', '; '.join(f'{chip["label"]}: {chip["value"]}' for chip in filter_chips)])
    sheet.append(['Разрез:', slice_label])
    sheet.append(
        [
            'Сформирован (UTC):',
            datetime.utcnow().strftime('%d.%m.%Y %H:%M'),
            'Строк:',
            len(report_rows),
        ]
    )
    sheet.append(
        [
            'Метрики:',
            'Участий',
            'Побед',
            'Призовых',
            'Уникальных участников',
            'Уникальных призёров',
            'Соревнований',
        ]
    )
    sheet.append(
        [
            '',
            summary.count_participation,
            summary.count_wins,
            summary.count_prizes,
            summary.count_unique_participants,
            summary.count_unique_prizers,
            summary.count_events,
        ]
    )
    sheet.append(
        [
            'Соревнований: уникальные ID событий; участия без связи не учитываются.',
            'Участий без связи с событием:',
            summary.count_unlinked_participations,
        ]
    )
    sheet.append([])
    sheet.append(list(columns))
    if slice_key == DEFAULT_REPORT_SLICE:
        records = [dict(info.model_dump(by_alias=True)) for info in report_rows]
    else:
        records = [
            {
                slice_column: report_slice_display_value(row.slice_value),
                **{column: getter(row) for column, getter in metric_values.items()},
            }
            for row in report_rows
        ]
    for record in records:
        sheet.append([sanitize_spreadsheet_value(record.get(column, '')) for column in columns])
    sheet.append(build_report_total_row(columns, summary))
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def build_report_display_groups(report_rows: Sequence, slice_key: str) -> list[list]:
    """HTML-only blocks. A confirmed card and exact historical name are required.

    Keep SQL/XLSX rows unchanged; an absent card or empty name is a singleton.
    Dict insertion order preserves the first canonical occurrence of each block.
    """
    groups: dict[tuple, list] = {}
    for index, row in enumerate(report_rows):
        key = ('row', index)
        if slice_key == DEFAULT_REPORT_SLICE and row.student_ref_id is not None and row.student_name.strip():
            key = ('student', row.student_ref_id, row.student_name)
        groups.setdefault(key, []).append(row)
    return list(groups.values())


REPORT_DRILLDOWN_METRICS = {
    'participations': 'Участия',
    'wins': 'Победы',
    'prizes': 'Призовые',
}


def report_drilldown_url(entries: Sequence[tuple[str, str]], metric: str, slice_key: str = '', anchor=None) -> str:
    params = [*entries, ('report_metric', metric)]
    if slice_key and anchor is not None:
        params.extend([('report_slice', slice_key), ('report_anchor', str(anchor))])
    return '/?' + urlencode(params)


def parse_report_drilldown(args: dict) -> tuple[dict, str | None]:
    metric = get_param(args, 'report_metric') or ''
    slice_key = get_param(args, 'report_slice') or ''
    anchor = get_param(args, 'report_anchor') or ''
    if metric not in REPORT_DRILLDOWN_METRICS:
        return {}, 'Неизвестный показатель отчёта'
    if bool(slice_key) != bool(anchor) or (slice_key and slice_key not in REPORT_SLICE_KEYS):
        return {}, 'Некорректный срез отчёта'
    if anchor and (not re.fullmatch(r'[1-9]\d{0,18}', anchor) or int(anchor) > 9223372036854775807):
        return {}, 'Некорректная запись среза'
    return dict(metric=metric, slice_key=slice_key, anchor_id=int(anchor) if anchor else None), None


def report_drilldown_row_labels(slice_key: str, values: tuple) -> list[dict]:
    if not slice_key:
        return []
    if not values:
        return [{'label': 'Строка отчёта', 'value': 'Больше не доступна в выбранных условиях'}]
    if slice_key == DEFAULT_REPORT_SLICE:
        identity, *historical = values
        linked = str(identity).startswith('r')
        values = (str(identity)[1:], *historical)
        labels = (
            'ID студента (в записи)' if linked else 'ID участия без карточки',
            'ФИО',
            'Пол',
            'Институт',
            'Группа',
            'Курс',
        )
    else:
        labels = REPORT_SLICE_COLUMNS[slice_key]
    return [
        {'label': label, 'value': 'Не указано' if value in (None, '') else str(value)}
        for label, value in zip(labels, values)
    ]


def build_report_result_context(request: Request, filter_kwargs: dict) -> dict:
    """Контекст зоны результата отчёта (общий для /reports и /report).

    Только по применённым параметрам URL: строки, сводка (отдельный
    агрегат по выборке), чипы-ссылки, счётчик строк и панель экспорта со
    скрытыми input применённого запроса.
    """
    args = dict(request.args)
    storage = get_storage(request.app)
    custom_fields = storage.get_custom_fields()
    entries = collect_applied_report_query(args, custom_fields)
    slice_key = parse_report_slice(args)
    report_rows = get_report_rows(storage, slice_key, filter_kwargs)
    summary = storage.get_report_summary(**filter_kwargs)
    rows_count = len(report_rows)
    display_groups = build_report_display_groups(report_rows, slice_key)
    return {
        'report_rows': report_rows,
        'report_drilldown_url': lambda metric, anchor=None: report_drilldown_url(
            entries or [('period', REPORT_PERIOD_ALL)], metric, slice_key if anchor is not None else '', anchor
        ),
        'report_display_groups': display_groups,
        'visual_groups_count': len(display_groups),
        # Пустой /report — всё время; /reports без условий не показывает результат.
        'report_return_url': '/reports?' + urlencode(entries or [('period', REPORT_PERIOD_ALL)]),
        'report_slice': slice_key,
        'report_slice_column': REPORT_SLICE_COLUMNS[slice_key][0],
        'report_slice_sort_type': 'number' if slice_key in REPORT_SLICE_NUMERIC_KEYS else 'text',
        'report_slice_is_student': slice_key == DEFAULT_REPORT_SLICE,
        'report_chips': build_report_chips(entries, custom_fields, report_event_labels(storage, args)),
        'report_summary': summary,
        'rows_count': rows_count,
        'rows_count_label': ru_plural(rows_count, 'строка', 'строки', 'строк'),
        'total_participations': summary.count_participation,
        'participations_label': ru_plural(summary.count_participation, 'участие', 'участия', 'участий'),
        # Панель экспорта: применённый запрос скрытыми input + колонки
        # применённого среза (серверный рендер, не текущие поля формы).
        'report_export_params': entries,
        'report_export_columns': report_export_columns(slice_key),
    }


def build_report_form_values(args: dict, custom_fields: Sequence[CustomField], *, applied: bool = False) -> dict:
    """Значения формы условий для восстановления из URL (F5/ссылка).

    Период: period из URL; без него прямые даты → 'custom'; применённый
    отчёт без period и дат → 'all' (нет фильтра дат ≙ «Все время» —
    восстановленная форма равна применённому состоянию, а не дефолту
    чистой страницы, иначе dirty-детект ложно срабатывает после
    навигации); чистая страница без параметров — дефолт формы (текущий
    учебный год).
    """
    period = (get_param(args, 'period') or '').strip()
    date_from = (get_param(args, 'date_from') or '').strip()
    date_to = (get_param(args, 'date_to') or '').strip()
    if not period and (date_from or date_to):
        period = REPORT_PERIOD_CUSTOM
    elif not period and applied:
        period = REPORT_PERIOD_ALL
    values = {
        'period': period or current_report_period_value(),
        'date_from': date_from,
        'date_to': date_to,
        'name': (get_param(args, 'name') or '').strip(),
        'position': (get_param(args, 'position') or '').strip(),
        'level': report_filter_values(args, 'level'),
        'institute': report_filter_values(args, 'institute'),
        'group': report_filter_values(args, 'group'),
        'sport': report_filter_values(args, 'sport'),
        'course': (get_param(args, 'course') or '').strip(),
        'admission_year': (get_param(args, 'admission_year') or '').strip(),
        'event_id': report_filter_values(args, 'event_id'),
        'group_by': parse_report_slice(args),
        'custom': {field.key: (get_param(args, f'custom__{field.key}') or '').strip() for field in custom_fields},
    }
    return values


def build_report_event_options(storage, selected: Sequence[str] = ()) -> list[dict]:
    """Events keep their stable ID even when titles repeat or period changes."""
    options = []
    for event in storage.list_calendar_events():
        start = str(event['date'])[:10]
        end = str(event.get('date_to') or '')[:10]
        detail = ' · '.join(
            str(value)
            for value in (
                start + (f' – {end}' if end and end != start else ''),
                event.get('sport'),
                event.get('level'),
                f"№{event['id']}",
            )
            if value
        )
        options.append(
            {
                'value': str(event['id']),
                'label': event['name'],
                'detail': detail,
                'date_from': start,
                'date_to': end or start,
            }
        )
    known = {option['value'] for option in options}
    options.extend(
        {'value': value, 'label': f'Соревнование №{value} (недоступно)', 'detail': 'Условие сохранено'}
        for value in selected
        if value not in known
    )
    return options


def report_event_labels(storage, args: dict) -> dict[str, str]:
    return {
        option['value']: f"{option['label']} · {option['detail']}"
        for option in build_report_event_options(storage, report_filter_values(args, 'event_id'))
    }


async def reports_page_handler(request: Request):
    # Каноническая страница отчётов (Reports v2): GET-навигация —
    # применённое состояние живёт в URL, форма восстанавливается из него.
    if user_is_athlete(request):
        return forbidden(request)
    storage = get_storage(request.app)
    custom_fields = storage.get_custom_fields()
    args = dict(request.args)
    filter_kwargs, error = build_report_filter_kwargs(request)
    entries = collect_applied_report_query(args, custom_fields)
    # Отчёт «сформирован», когда в URL есть хотя бы один применяемый
    # параметр; ошибка валидации — форма со значениями + alert, зоны
    # результата нет.
    applied = bool(entries) and error is None

    groups_by_institute = storage.get_group_options_by_institute()
    admission_years, admission_has_none = storage.get_report_admission_year_options()
    form_values = build_report_form_values(args, custom_fields, applied=applied)
    context = {
        'request': request,
        'event_options': build_report_event_options(storage, form_values['event_id']),
        'report_advanced_count': sum(
            bool(form_values[key]) for key in ('level', 'course', 'admission_year', 'name', 'position')
        )
        + sum(bool(value) for value in form_values['custom'].values()),
        'custom_fields': custom_fields,
        'levels': storage.get_level_names(),
        'report_slice_options': REPORT_SLICES,
        'institute_options': storage.list_catalog('institute'),
        'sport_options': storage.list_catalog('sport'),
        'groups_by_institute': groups_by_institute,
        'group_options': sorted({group for groups in groups_by_institute.values() for group in groups}),
        'course_options': storage.get_report_course_options(),
        'admission_year_options': admission_years,
        'admission_has_none': admission_has_none,
        'period_options': build_report_period_options(storage.get_report_period_year_options()),
        'report_form': form_values,
        'report_error': error,
        'report_applied': applied,
        'applied_query': urlencode(entries),
    }
    for key, options_key in (
        ('institute', 'institute_options'),
        ('group', 'group_options'),
        ('sport', 'sport_options'),
        ('level', 'levels'),
    ):
        context[options_key] = sorted(set(context[options_key]) | set(form_values[key]))
    if applied:
        context.update(build_report_result_context(request, filter_kwargs))
    return await render(
        template_name=jinja_env.get_template('report.html'),
        context=context,
    )


async def report_fragment_handler(request: Request):
    # Фрагмент зоны результата для существующих ссылок/тестов;
    # /reports — канонический адрес Reports v2.
    if user_is_athlete(request):
        return forbidden(request)
    filter_kwargs, error = build_report_filter_kwargs(request)
    if error:
        return text(body=error, status=400)
    return await render(
        template_name=jinja_env.get_template('filtered.html'),
        context={'request': request, **build_report_result_context(request, filter_kwargs)},
    )


async def export_report_handler(request: Request):
    # Доступ как у просмотра отчёта: всем, кроме athlete. Наследует
    # срез/метрики/фильтры применённого отчёта (GET-параметры); файл
    # строится ТОЛЬКО из применённых параметров.
    if user_is_athlete(request):
        return forbidden(request)
    filter_kwargs, error = build_report_filter_kwargs(request)
    if error:
        return text(body=error, status=400)

    args = dict(request.args)
    slice_key = parse_report_slice(args)
    columns, columns_error = parse_report_export_columns(request, slice_key)
    if columns_error:
        return text(body=columns_error, status=400)

    storage = get_storage(request.app)
    report_rows = get_report_rows(storage, slice_key, filter_kwargs)
    summary = storage.get_report_summary(**filter_kwargs)
    custom_fields = storage.get_custom_fields()
    entries = collect_applied_report_query(args, custom_fields)
    chips = build_report_chips(entries, custom_fields, report_event_labels(storage, args))
    slice_label = next(item['label'] for item in REPORT_SLICES if item['key'] == slice_key)
    buffer = await asyncio.to_thread(
        build_report_workbook,
        report_rows,
        columns,
        slice_key,
        summary,
        period_label=report_period_label(dict(entries)),
        # Фильтры шапки — метки как у чипов, без периода и разреза
        # (у них свои строки шапки).
        filter_chips=[chip for chip in chips if chip['label'] not in ('Период', 'Разрез')],
        slice_label=slice_label,
    )

    now_str = datetime.utcnow().strftime('%d-%m-%Y_%H-%M-%S')
    filename = f'Отчет_{now_str}.xlsx'

    return raw(
        buffer.getvalue(),
        headers={
            'content-type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'content-disposition': f'attachment; filename="{filename}"',
        },
    )


def register(app: Sanic) -> None:
    @app.get('/reports')
    async def reports_page(request: Request):
        return await reports_page_handler(request)

    @app.get('/report')
    async def get_report(request: Request):
        return await report_fragment_handler(request)

    @app.get('/export/report')
    async def export_report(request: Request):
        return await export_report_handler(request)
