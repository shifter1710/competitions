"""Маршруты отчётов: страница отчётов, отчёт по фильтрам и выгрузка в Excel.

Перенесено из src/main.py без изменения поведения (Architecture v1)."""
import asyncio
import re
from datetime import datetime
from io import BytesIO
from typing import Sequence

import pandas as pd
from sanic import Request
from sanic import Sanic
from sanic import text
from sanic.response import raw
from sanic_ext import render

from src.auth import user_is_athlete
from src.models.custom_field import CustomField
from src.records import sanitize_spreadsheet_value
from src.settings import settings
from src.web import forbidden
from src.web import get_param
from src.web import get_storage
from src.web import jinja_env
from src.web import ru_plural


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


# Чипы применённых условий отчёта (прототип 06): активный фильтр — чип,
# сброс одного условия — перезагрузка отчёта без его GET-параметра.
REPORT_CHIP_LABELS: Sequence[tuple[str, str]] = (
    ('name', 'ФИО'),
    ('date_from', 'Дата (от)'),
    ('date_to', 'Дата (до)'),
    ('position', 'Место'),
    ('level', 'Уровень'),
    ('institute', 'Институт'),
    ('group', 'Группа'),
    ('sport', 'Вид спорта'),
    ('group_by', 'Срез'),
)


REPORT_POSITION_CHIP_LABELS = {'<2': 'Победа', '<4': 'Призовое место', '>3': 'Не призовое место'}


def build_report_chips(args: dict, custom_fields: Sequence[CustomField]) -> list[dict]:
    """Чипы применённых условий отчёта для страницы /report.

    Каждый активный GET-параметр — отдельный чип c data-remove-key; метки
    позиций и среза — человекочитаемые. Кастомные поля — по ярлыку поля.
    """
    slice_labels = {item['key']: item['label'] for item in REPORT_SLICES}
    chips: list[dict] = []
    for key, label in REPORT_CHIP_LABELS:
        raw_value = (get_param(args, key) or '').strip()
        if not raw_value:
            continue
        if key == 'position':
            raw_value = REPORT_POSITION_CHIP_LABELS.get(raw_value, raw_value)
        if key == 'group_by':
            raw_value = slice_labels.get(raw_value, raw_value)
        chips.append({'label': label, 'value': raw_value, 'remove_key': key})
    for field in custom_fields:
        raw_value = (get_param(args, f'custom__{field.key}') or '').strip()
        if raw_value:
            chips.append({'label': field.label, 'value': raw_value, 'remove_key': f'custom__{field.key}'})
    return chips


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


def register(app: Sanic) -> None:
    @app.get('/reports')
    async def reports_page(request: Request):
        if user_is_athlete(request):
            return forbidden(request)
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

    @app.get('/report')
    async def get_report(request: Request):
        # Доступ как раньше: всем ролям, кроме athlete (личный кабинет вместо отчётов).
        if user_is_athlete(request):
            return forbidden(request)
        error = validate_report_filters(dict(request.args)) or collect_custom_filters(request)[1]
        if error:
            return text(body=error, status=400)

        slice_key = parse_report_slice(dict(request.args))
        report_rows = get_report_rows(request, slice_key)
        custom_fields = get_storage(request.app).get_custom_fields()
        rows_count = len(report_rows)
        total_participations = sum(int(row.count_participation) for row in report_rows)
        return await render(
            template_name=jinja_env.get_template('filtered.html'),
            context={
                'request': request,
                'report_rows': report_rows,
                'report_slice': slice_key,
                'report_slice_column': REPORT_SLICE_COLUMNS[slice_key][0],
                'report_slice_sort_type': 'number' if slice_key in REPORT_SLICE_NUMERIC_KEYS else 'text',
                # Композиция отчёта (прототип 06): чипы условий и счётчик строк.
                'report_chips': build_report_chips(dict(request.args), custom_fields),
                'rows_count': rows_count,
                'rows_count_label': ru_plural(rows_count, 'строка', 'строки', 'строк'),
                'total_participations': total_participations,
                'participations_label': ru_plural(total_participations, 'участие', 'участия', 'участий'),
            },
        )

    @app.get('/export/report')
    async def export_report(request: Request):
        # Доступ как у просмотра отчёта: всем, кроме athlete. Наследует
        # срез/метрики/фильтры применённого отчёта (GET-параметры).
        if user_is_athlete(request):
            return forbidden(request)
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
