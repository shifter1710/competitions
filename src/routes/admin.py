"""Маршруты администрирования: панель, кастомные/базовые поля, справочники,
пользователи, уровни, обслуживание базы (очистка/бэкап/экспорт) и журнал
аудита. Перенесено из src/main.py без изменения поведения (Architecture v1)."""
import asyncio
import logging
import re
import shutil
import zipfile
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from io import BytesIO
from pathlib import Path
from typing import Sequence
from urllib.parse import urlencode

import pandas as pd
from sanic import redirect
from sanic import Request
from sanic import Sanic
from sanic import text
from sanic.response import json as json_response
from sanic.response import raw
from sanic_ext import render

from src.auth import ADMIN_ROLE
from src.auth import get_auth_user
from src.auth import hash_password
from src.auth import log_audit_event
from src.auth import MIN_PASSWORD_LENGTH
from src.auth import PRESENCE_ONLINE_WINDOW
from src.auth import require_admin
from src.auth import require_moderator
from src.auth import user_is_admin
from src.auth import user_is_moderator
from src.auth import USER_ROLES
from src.auth import username_error
from src.backup import run_backup
from src.education import academic_year_label
from src.education import derive_course
from src.education import effective_duration_years
from src.education import MAX_ADMISSION_YEAR
from src.education import MAX_DURATION_YEARS
from src.education import MIN_ADMISSION_YEAR
from src.education import MIN_DURATION_YEARS
from src.education import parse_group_admission_year
from src.education import parse_group_admission_year_evidence
from src.files import attachment_source_path
from src.files import attachments_files_bytes
from src.files import backups_dir
from src.files import file_size
from src.files import files_dir
from src.files import remove_attachment_files
from src.files import remove_attachment_record_dirs
from src.models.custom_field import CustomField
from src.records import ALWAYS_REQUIRED_BASE_FIELDS
from src.records import BASE_FIELD_LABELS
from src.records import BASE_FIELD_SETTING_DEFAULTS
from src.records import BASE_FIELD_VALUE_TYPES
from src.records import build_index_dataframe
from src.records import build_link_diff_rows
from src.records import FIELD_TYPE_OPTIONS
from src.records import get_base_field_settings
from src.records import LINK_EVENT_FIELD_LABELS
from src.records import LINK_TARGETABLE_BASE_KEYS
from src.records import make_unique_custom_field_key
from src.records import participation_field_changes
from src.records import sanitize_spreadsheet_value
from src.records import select_export_custom_fields
from src.settings import settings
from src.storage.sqlite import SQLiteAdapter
from src.web import build_redirect_with_message
from src.web import checkbox_to_bool
from src.web import format_date_range
from src.web import format_size
from src.web import get_flash_args
from src.web import get_form_value
from src.web import get_param
from src.web import get_storage
from src.web import jinja_env
from src.web import parse_checkbox
from src.web import ru_plural

logger = logging.getLogger(__name__)


# Очистка базы — явное админское действие в два шага (объём + фраза).
# См. docs/data-model-decisions.md «Очистка и выгрузка базы».
# Порядок пунктов — по возрастанию ущерба: вложения → записи → всё.
WIPE_SCOPES = ('attachments', 'records', 'all')


WIPE_MODES = ('scope', 'date')


WIPE_CONFIRM_PHRASE = 'УДАЛИТЬ'


AUDIT_PAGE_SIZE = 50


PRE_WIPE_ARCHIVES_SHOWN = 20


# Управляемые справочники значений: записи остаются свободным текстом,
# справочник — только подсказки (docs/data-model-decisions.md,
# «Справочники значений»). Уровни живут в своей таблице на той же странице.
# Группы — иерархическая категория: существуют только с parent_id на
# институт, поэтому в одиночное создание не входят.
CATALOG_CATEGORIES: Sequence[str] = ('sport', 'institute')


GROUP_CATEGORY = 'group'


CATALOG_MANAGE_CATEGORIES: Sequence[str] = (*CATALOG_CATEGORIES, GROUP_CATEGORY)


# Переименование значений справочника (решение 2026-09-13): уровень живёт в
# levels, остальные — в catalog_values; label — для страницы-подтверждения.
CATALOG_RENAME_LABELS: dict[str, str] = {
    'level': 'Уровень',
    'sport': 'Вид спорта',
    'institute': 'Институт',
    GROUP_CATEGORY: 'Группа',
}


def get_allowed_link_targets(
    custom_fields: Sequence[CustomField],
    *,
    exclude_key: str | None = None,
) -> dict[str, str]:
    """№24: допустимые цели привязки link-поля — ключ → подпись колонки.

    Базовые текстовые колонки таблицы + активные кастомные текстовые поля
    (кроме самого link-поля).
    """
    targets = {key: BASE_FIELD_LABELS[key] for key in LINK_TARGETABLE_BASE_KEYS}
    for field in custom_fields:
        if field.field_type == 'text' and field.active and field.key != exclude_key:
            targets[field.key] = field.label
    return targets


def parse_link_target_form(
    request: Request,
    *,
    field_type: str,
    custom_fields: Sequence[CustomField],
    exclude_key: str | None = None,
) -> str | None:
    """Прочитать link_target из формы полей: NULL/пусто = не привязана.

    Значение принимается только у url-полей и только из допустимого списка
    целей — подделка разметки не уводит рендер в неизвестную колонку.
    """
    if field_type != 'url':
        return None
    raw_target = get_form_value(request, 'link_target').strip()
    if not raw_target:
        return None
    if raw_target not in get_allowed_link_targets(custom_fields, exclude_key=exclude_key):
        return None
    return raw_target


def parse_base_field_settings_form(request: Request) -> dict[str, tuple[str, bool]]:
    """Собрать настройки базовых полей из формы /admin/fields/base.

    Заблокированные поля (ФИО, Дата) игнорируют форму: обязательность
    не отключается, тип фиксированный — защита от подделки разметки.
    """
    settings_form: dict[str, tuple[str, bool]] = {}
    for key in BASE_FIELD_SETTING_DEFAULTS:
        default_type, default_required = BASE_FIELD_SETTING_DEFAULTS[key]
        if key in ALWAYS_REQUIRED_BASE_FIELDS:
            settings_form[key] = (default_type, True)
            continue
        value_type = get_form_value(request, f'type_{key}')
        if value_type not in BASE_FIELD_VALUE_TYPES:
            value_type = default_type
        settings_form[key] = (value_type, parse_checkbox(request, f'required_{key}'))
    return settings_form


def build_catalog_entries(storage: SQLiteAdapter, category: str) -> list[dict]:
    """Значения справочника для страницы админки: каждое со счётчиком записей."""
    return [
        {**row, 'records_count': storage.count_records_using(category, row['value'])}
        for row in storage.list_catalog_all(category)
    ]


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


def resolve_rename_target(request: Request, category: str, value_id: str):
    """Строка справочника для переименования: уровень — по id в levels,
    остальные категории — по id в catalog_values. Для группы вторым
    элементом сразу её институт (переименование затрагивает пару)."""
    if category not in CATALOG_RENAME_LABELS:
        return None, None, text(body='Unknown catalog', status=404)
    try:
        numeric_value_id = int(value_id)
    except ValueError:
        return None, None, text(body='Invalid value id', status=400)

    storage = get_storage(request.app)
    if category == 'level':
        level = next((item for item in storage.list_levels() if item['id'] == numeric_value_id), None)
        if level is None:
            return None, None, build_redirect_with_message(error='Значение не найдено', url='/admin/catalogs')
        # уровни хранят имя в «name»; дальше работаем с унифицированным «value»
        return {'id': level['id'], 'value': level['name'], 'active': level.get('active')}, None, None

    row = storage.get_catalog_value(numeric_value_id)
    if row is None or row['category'] != category:
        return None, None, build_redirect_with_message(error='Значение не найдено', url='/admin/catalogs')
    parent_value = None
    if category == GROUP_CATEGORY:
        parent = storage.get_catalog_value(row['parent_id']) if row.get('parent_id') else None
        if parent is None or parent['category'] != 'institute':
            return None, None, build_redirect_with_message(error='Институт группы не найден', url='/admin/catalogs')
        parent_value = parent['value']
    return row, parent_value, None


def catalog_rename_conflict(storage: SQLiteAdapter, category: str, row: dict, new_name: str) -> bool:
    """Конфликт нового имени с существующим значением категории (для группы —
    в том же институте). Проверка до выполнения, чтобы вернуть ошибку рано;
    авторитетная — внутри storage.rename_catalog_value, в транзакции."""
    if category == 'level':
        return new_name in storage.get_level_names(include_inactive=True)
    if category == GROUP_CATEGORY:
        return any(
            item['value'] == new_name and item['parent_id'] == row.get('parent_id')
            for item in storage.list_catalog_all(GROUP_CATEGORY)
        )
    return any(item['value'] == new_name for item in storage.list_catalog_all(category))


def build_institute_options(storage: SQLiteAdapter) -> list[dict]:
    """Активные институты плоским списком id+значение (форма переноса группы)."""
    return [{'id': row['id'], 'value': row['value']} for row in storage.list_catalog_all('institute') if row['active']]


def resolve_group_move_target(request: Request, value_id: str, target_institute_id: str):
    """Группа и целевой институт переноса: ((group, target), None) или
    (None, redirect с ошибкой на /admin/catalogs)."""
    try:
        numeric_group_id = int(value_id)
        numeric_target_id = int(target_institute_id)
    except (TypeError, ValueError):
        return None, text(body='Invalid value id', status=400)
    storage = get_storage(request.app)
    group = storage.get_catalog_value(numeric_group_id)
    if group is None or group['category'] != GROUP_CATEGORY or not group.get('parent_id'):
        return None, build_redirect_with_message(error='Группа или институт не найдены', url='/admin/catalogs')
    target = storage.get_catalog_value(numeric_target_id)
    if target is None or target['category'] != 'institute':
        return None, build_redirect_with_message(error='Группа или институт не найдены', url='/admin/catalogs')
    if target['id'] == group['parent_id']:
        return None, build_redirect_with_message(
            error='Группа уже относится к выбранному институту',
            url='/admin/catalogs',
        )
    conflict = storage.find_catalog_row(GROUP_CATEGORY, group['value'], parent_id=target['id'])
    if conflict is not None:
        return None, build_redirect_with_message(
            error=(
                f'В институте «{target["value"]}» уже есть группа «{group["value"]}» '
                '— переименуйте одну из групп перед переносом'
            ),
            url='/admin/catalogs',
        )
    return (group, target), None


def format_login_moment(raw_value) -> str:
    """Последний вход для подсказки: дд.мм.гггг чч:мм или «—»."""
    if not raw_value:
        return '—'
    try:
        moment = datetime.fromisoformat(str(raw_value)).replace(tzinfo=timezone.utc).astimezone()
    except ValueError:
        return '—'
    return moment.strftime('%d.%m.%Y %H:%M')


# --- Course / Education, Phase A: уровни образования и учебные данные групп. ---


def build_education_level_entries(storage: SQLiteAdapter) -> list[dict]:
    """Уровни образования для страницы «Справочники»: каждый со счётчиком
    групп, у которых задан этот уровень."""
    return [
        {**level, 'groups_count': storage.count_groups_using_level(level['id'])}
        for level in storage.list_education_levels(include_inactive=True)
    ]


def resolve_group_academic_target(request: Request, value_id: str) -> tuple[dict | None, dict | None, str | None]:
    """Группа учебных данных + её институт: ((group, institute), None) или
    (None, None, redirect с ошибкой на /admin/catalogs)."""
    try:
        numeric_value_id = int(value_id)
    except ValueError:
        return None, None, text(body='Invalid value id', status=400)
    storage = get_storage(request.app)
    group = storage.get_catalog_value(numeric_value_id)
    if group is None or group['category'] != GROUP_CATEGORY or not group.get('parent_id'):
        return None, None, build_redirect_with_message(error='Группа не найдена', url='/admin/catalogs')
    institute = storage.get_catalog_value(group['parent_id'])
    if institute is None or institute['category'] != 'institute':
        return None, None, build_redirect_with_message(error='Институт группы не найден', url='/admin/catalogs')
    return group, institute, None


def parse_optional_int_form(request: Request, key: str, *, label: str) -> tuple[int | None, str | None]:
    """Необязательное целое из формы: (значение, ошибка).

    Пусто → (None, None) — «очистить поле»; нечисло → (None, текст ошибки).
    Диапазонную валидацию выполняет storage (год поступления, длительности).
    """
    raw_value = get_form_value(request, key).strip()
    if not raw_value:
        return None, None
    try:
        return int(raw_value), None
    except ValueError:
        return None, f'{label} должно быть целым числом'


def group_academic_duration_hint(stored: dict | None) -> str:
    """Подсказка поля «Длительность обучения»: что будет, если оставить
    пусто (по СООХРАНЁННОМУ уровню — без JS страница не знает выбор)."""
    if not stored or stored.get('education_level_id') is None:
        return 'Пусто — уровень не выбран, длительность неизвестна.'
    level_name = stored.get('education_level_name') or ''
    default = stored.get('level_default_duration_years')
    if default is None:
        return 'Пусто — у выбранного уровня длительность по умолчанию не задана.'
    return f'Пусто — используется длительность уровня «{level_name}» по умолчанию ({default}).'


def group_academic_course_preview(stored: dict | None) -> str:
    """Строка предпросмотра «какой курс сейчас» по сохранённым данным."""
    if stored is None or stored.get('admission_year') is None:
        return 'Курс пока не рассчитывается: не указан год поступления.'
    duration = effective_duration_years(
        stored.get('duration_years_override'), stored.get('level_default_duration_years')
    )
    derived = derive_course(stored['admission_year'], date.today(), duration)
    if derived['status'] == 'future':
        return 'Курс пока не рассчитывается: год поступления позже текущей даты.'
    if derived['status'] != 'ok' or derived['course'] is None:
        return 'Курс пока не рассчитывается: не указан год поступления.'
    return (
        f'По сохранённым данным сейчас: {derived["course"]} курс '
        f'(учебный год {academic_year_label(derived["academic_year_start"])}).'
    )


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


def count_active_admins(storage: SQLiteAdapter) -> int:
    return sum(1 for user in storage.list_users() if user['role'] == ADMIN_ROLE and user['active'])


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


# --- P5b: массовое связывание записей без события (обслуживание базы). ---
#
# Предпросмотр по P0-семантике стартового backfill (однозначный пресет
# name + date + date_to): matched — можно связать, ambiguous — только
# вручную со страницы записи, unmatched — нет события. Apply проводит
# ТОЛЬКО matched со синхронизацией 5 полей; гонки закрывает guarded
# UPDATE (пропуск, не откат пакета).


LINK_PREVIEW_AMBIGUOUS_NAME_CAP = 5


def decorate_calendar_link_preview(preview: dict, events_by_id: dict[int, dict]) -> dict:
    """Строки matched с диффом «после связывания» и агрегаты очисток полей
    для warning-бейджа и текста confirm у кнопки пакета."""
    matched_rows = []
    clearing_counts: dict[str, int] = {}
    for row in preview['matched']:
        event = events_by_id.get(row['event_id'])
        if event is None:
            continue
        diff = build_link_diff_rows(
            participation_field_changes(row['name'], row['sport'], row['date'], row['date_to'], row['level'], event)
        )
        for item in diff:
            if item['clears']:
                clearing_counts[item['key']] = clearing_counts.get(item['key'], 0) + 1
        matched_rows.append(
            {
                **row,
                'record_date_label': format_date_range(
                    datetime.fromisoformat(row['date']),
                    datetime.fromisoformat(row['date_to']) if row['date_to'] else None,
                ),
                'event_period': format_date_range(
                    datetime.fromisoformat(event['date']),
                    datetime.fromisoformat(event['date_to']) if event.get('date_to') else None,
                ),
                'event_level': event['level'],
                'event_sport': event['sport'],
                'diff': diff,
            }
        )
    # Число ЗАПИСЕЙ хотя бы с одной очисткой (не сумма очисток по полям):
    # бейдж и confirm подают его как «У K записей …», рядом — поля с счётчиками.
    clearing_total = sum(1 for row in matched_rows if any(item['clears'] for item in row['diff']))

    def decorate_record_row(row: dict) -> dict:
        return {
            **row,
            'record_date_label': format_date_range(
                datetime.fromisoformat(row['date']),
                datetime.fromisoformat(row['date_to']) if row['date_to'] else None,
            ),
        }

    clearing_labels = [LINK_EVENT_FIELD_LABELS[key] for key, count in clearing_counts.items() if count]
    matched_total = preview['counters']['matched']
    apply_confirm = (
        f'Связать {matched_total} записей с найденными соревнованиями? '
        'Название, вид спорта, дату и уровень записи возьмут из соревнований.'
    )
    if clearing_labels:
        apply_confirm += f' У {clearing_total} записей станут пустыми: {", ".join(clearing_labels)}.'
    return {
        'counters': preview['counters'],
        'matched': matched_rows,
        'ambiguous': [decorate_record_row(row) for row in preview['ambiguous']],
        'unmatched': [decorate_record_row(row) for row in preview['unmatched']],
        'clearing_total': clearing_total,
        # Бейдж: «уровень (2), вид спорта (1)» — по полям с очистками.
        'clearing_parts': [
            f'{LINK_EVENT_FIELD_LABELS[key]} ({count})' for key, count in clearing_counts.items() if count
        ],
        'clearing_labels': clearing_labels,
        'apply_confirm_text': apply_confirm,
        'ambiguous_name_cap': LINK_PREVIEW_AMBIGUOUS_NAME_CAP,
    }


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
    export_custom_fields = select_export_custom_fields(storage.get_custom_fields())
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
        export_custom_fields = select_export_custom_fields(storage.get_custom_fields())
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


BACKUP_DOWNLOAD_NAME = re.compile(r'\A[A-Za-z0-9][A-Za-z0-9._-]*\Z')


BACKUP_CONTENT_TYPES = {
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.zip': 'application/zip',
    '.gz': 'application/gzip',
}


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


# C901 (осознанное подавление): mccabe суммирует сложность вложенных
# verbatim-хендлеров, перенесённых из main.py без изменений; разбиение
# register() — Architecture v2, не pre-merge gate.
def register(app: Sanic) -> None:  # noqa: C901
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

    @app.get('/admin/data-map')
    async def admin_data_map_page(request: Request):
        """«Как работает система»: пользовательская модель данных (студенты,
        соревнования, участия) без служебных подробностей."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        return await render(
            template_name=jinja_env.get_template('admin_data_map.html'),
            context={'request': request},
        )

    @app.get('/admin/data-guide')
    async def admin_data_guide_page(request: Request):
        """Инструкция простыми словами по разделам: Студенты, Календарь,
        участники соревнования, Реестр, импорт, спортсмен, отчёты."""
        auth_error = require_moderator(request)
        if auth_error is not None:
            return auth_error
        return await render(
            template_name=jinja_env.get_template('admin_data_guide.html'),
            context={'request': request},
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
                # №24: варианты привязки link-полей к колонкам таблицы.
                'link_target_base_options': [(key, BASE_FIELD_LABELS[key]) for key in LINK_TARGETABLE_BASE_KEYS],
                'link_target_custom_options': [
                    (field.key, field.label) for field in storage.get_custom_fields() if field.field_type == 'text'
                ],
                'base_field_settings': get_base_field_settings(storage),
                'base_field_labels': BASE_FIELD_LABELS,
                'always_required_base_fields': ALWAYS_REQUIRED_BASE_FIELDS,
                'base_field_value_types': BASE_FIELD_VALUE_TYPES,
                **get_flash_args(request),
            },
        )

    @app.post('/admin/fields/base')
    async def update_base_field_settings(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        storage = get_storage(request.app)
        new_settings = parse_base_field_settings_form(request)
        old_settings = get_base_field_settings(storage)
        changed = {
            key: {
                'old': old_settings[key],
                'new': {'value_type': value_type, 'required': required},
            }
            for key, (value_type, required) in new_settings.items()
            if old_settings[key]['value_type'] != value_type or old_settings[key]['required'] != required
        }
        if changed:
            storage.update_field_settings(new_settings)
            log_audit_event(request, 'field_settings_changed', {'fields': changed})
        return redirect(to='/admin/fields?admin_message=Настройки+базовых+полей+сохранены')

    @app.get('/admin/levels')
    async def admin_levels_page(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        # Уровни переехали на страницу «Справочники»; старый URL остаётся
        # рабочим, flash-параметры сохраняются в редиректе.
        query = request.query_string
        return redirect(f'/admin/catalogs?{query}' if query else '/admin/catalogs')

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
                'institute_options': build_institute_options(storage),
                'admin_levels': [
                    {**level, 'records_count': storage.count_records_using('level', level['name'])}
                    for level in storage.list_levels()
                ],
                # Course/Education Phase A: уровни образования + счётчик групп.
                'education_levels': build_education_level_entries(storage),
                **get_flash_args(request),
            },
        )

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

        # №1.1: регистровый дубль — отказ с подсказкой канонического написания.
        existing = get_storage(request.app).find_catalog_canonical(category, value)
        if existing is not None and existing != value:
            return build_redirect_with_message(
                error=f'Значение «{value}» уже есть: «{existing}» (отличается только регистром)',
                url='/admin/catalogs',
            )

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

        # №1.1: регистровый дубль внутри этого института — отказ с подсказкой.
        existing = storage.find_catalog_canonical('group', value, parent_id=institute['id'])
        if existing is not None and existing != value:
            return build_redirect_with_message(
                error=f'Группа «{value}» уже есть: «{existing}» (отличается только регистром)',
                url='/admin/catalogs',
            )

        storage.add_catalog_value(GROUP_CATEGORY, value, parent_id=institute['id'])
        return build_redirect_with_message(
            message=f'Группа «{value}» добавлена в институт «{institute["value"]}»',
            url='/admin/catalogs',
        )

    @app.get('/admin/catalogs/<category>/<value_id>/rename')
    async def catalog_rename_page(request: Request, category: str, value_id: str):
        """Шаг 2 переименования без JS: подтверждение с числом записей и галочкой
        «обновить записи» (шаг 1 — GET-форма со старым значением в списке)."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        row, parent_value, error = resolve_rename_target(request, category, value_id)
        if error is not None:
            return error

        new_name = (get_param(dict(request.args), 'new_name') or '').strip()
        if not new_name:
            return build_redirect_with_message(error='Новое имя обязательно', url='/admin/catalogs')
        if new_name == row['value']:
            return build_redirect_with_message(error='Новое имя совпадает со старым', url='/admin/catalogs')
        storage = get_storage(request.app)
        if catalog_rename_conflict(storage, category, row, new_name):
            return build_redirect_with_message(error=f'«{new_name}» уже есть в справочнике', url='/admin/catalogs')

        return await render(
            template_name=jinja_env.get_template('admin_catalog_rename.html'),
            context={
                'request': request,
                'category': category,
                'category_label': CATALOG_RENAME_LABELS[category],
                'value_id': row['id'],
                'old_name': row['value'],
                'new_name': new_name,
                'parent_value': parent_value,
                'records_count': storage.count_records_using(category, row['value'], parent_value=parent_value),
            },
        )

    @app.post('/admin/catalogs/<category>/<value_id>/rename')
    async def rename_catalog_value(request: Request, category: str, value_id: str):
        """Переименование значения справочника (решение 2026-09-13, по образцу merge).

        Без confirm — JSON-превью, сколько записей затронет. С confirm —
        выполнение: справочник и (по галочке update_records) записи одной
        транзакцией в storage; каждое выполнение — audit catalog_value_renamed
        (кто, что, во что, сколько записей обновлено).
        """
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        row, parent_value, error = resolve_rename_target(request, category, value_id)
        if error is not None:
            return error

        new_name = get_form_value(request, 'new_name').strip()
        if not new_name:
            return build_redirect_with_message(error='Новое имя обязательно', url='/admin/catalogs')
        if new_name == row['value']:
            return build_redirect_with_message(error='Новое имя совпадает со старым', url='/admin/catalogs')
        storage = get_storage(request.app)
        if catalog_rename_conflict(storage, category, row, new_name):
            return build_redirect_with_message(error=f'«{new_name}» уже есть в справочнике', url='/admin/catalogs')

        if not checkbox_to_bool(get_form_value(request, 'confirm')):
            return json_response(
                {
                    'preview': True,
                    'category': category,
                    'old': row['value'],
                    'new': new_name,
                    'records': storage.count_records_using(category, row['value'], parent_value=parent_value),
                }
            )

        update_records = checkbox_to_bool(get_form_value(request, 'update_records'))
        try:
            records_updated = storage.rename_catalog_value(
                category,
                row['value'],
                new_name,
                parent_value=parent_value,
                update_records=update_records,
            )
        except ValueError as exc:
            return build_redirect_with_message(error=str(exc), url='/admin/catalogs')

        audit_details = {
            'category': category,
            'old': row['value'],
            'new': new_name,
            'update_records': update_records,
            'records_updated': records_updated,
        }
        if category == GROUP_CATEGORY:
            audit_details['parent'] = parent_value
        log_audit_event(request, 'catalog_value_renamed', audit_details)

        message = f'«{row["value"]}» → «{new_name}»'
        message += f': обновлено записей — {records_updated}' if update_records else ' (записи не тронуты)'
        return build_redirect_with_message(message=message, url='/admin/catalogs')

    @app.get('/admin/catalogs/group/<value_id>/move')
    async def catalog_group_move_page(request: Request, value_id: str):
        """Перенос группы в другой институт (решение 2026-09-22), шаг 2 без JS:
        страница-подтверждение со счётчиками затрагиваемых данных и галочкой."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        target_institute_id = (get_param(dict(request.args), 'target_institute_id') or '').strip()
        if not target_institute_id:
            return build_redirect_with_message(error='Выберите институт для переноса', url='/admin/catalogs')
        resolved, error = resolve_group_move_target(request, value_id, target_institute_id)
        if error is not None:
            return error
        group, target = resolved

        storage = get_storage(request.app)
        old_institute = storage.get_catalog_value(group['parent_id'])
        if old_institute is None:
            return build_redirect_with_message(error='Группа или институт не найдены', url='/admin/catalogs')

        return await render(
            template_name=jinja_env.get_template('admin_catalog_group_move.html'),
            context={
                'request': request,
                'value_id': group['id'],
                'group_value': group['value'],
                'old_institute': old_institute['value'],
                'target_institute': target['value'],
                'target_institute_id': target['id'],
                'records_count': storage.count_records_using(
                    'group', group['value'], parent_value=old_institute['value']
                ),
                'students_count': storage.count_students_using_group_pair(group['value'], old_institute['value']),
            },
        )

    @app.post('/admin/catalogs/group/<value_id>/move')
    async def catalog_group_move(request: Request, value_id: str):
        """Выполнение переноса группы: справочник + записи + карточки студентов
        одной транзакцией в storage; подтверждение — обязательной галочкой."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        target_institute_id = get_form_value(request, 'target_institute_id').strip()
        resolved, error = resolve_group_move_target(request, value_id, target_institute_id)
        if error is not None:
            return error
        group, target = resolved

        # Без галочки — назад на страницу подтверждения, ничего не меняя.
        back_url = f'/admin/catalogs/group/{value_id}/move?target_institute_id={target_institute_id}'
        if not checkbox_to_bool(get_form_value(request, 'confirm')):
            return redirect(to=back_url)

        storage = get_storage(request.app)
        old_institute = storage.get_catalog_value(group['parent_id'])
        if old_institute is None:
            return build_redirect_with_message(error='Группа или институт не найдены', url='/admin/catalogs')
        try:
            counts = storage.move_catalog_group(group['id'], target['id'])
        except ValueError as exc:
            return build_redirect_with_message(error=str(exc), url='/admin/catalogs')

        log_audit_event(
            request,
            'catalog_group_moved',
            {
                'group': group['value'],
                'old_institute': old_institute['value'],
                'new_institute': target['value'],
                'students_updated': counts['students_updated'],
                'competitions_updated': counts['competitions_updated'],
                'group_id': group['id'],
            },
        )
        return build_redirect_with_message(
            message=(
                f'Группа «{group["value"]}» перенесена: «{old_institute["value"]}» → «{target["value"]}»; '
                f'обновлено записей — {counts["competitions_updated"]}, '
                f'карточек студентов — {counts["students_updated"]}'
            ),
            url='/admin/catalogs',
        )

    # --- Course / Education, Phase A: уровни образования (admin CRUD). ---

    @app.post('/admin/education-levels')
    async def create_education_level(request: Request):
        """Добавить уровень образования; длительность по умолчанию —
        необязательная (NULL = «не задана», курс считается без границы)."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        name = get_form_value(request, 'name').strip()
        if not name:
            return build_redirect_with_message(error='Название уровня обязательно', url='/admin/catalogs')
        duration, duration_error = parse_optional_int_form(request, 'default_duration_years', label='Длительность')
        if duration_error is not None:
            return build_redirect_with_message(error=duration_error, url='/admin/catalogs')

        storage = get_storage(request.app)
        try:
            storage.create_education_level(name, duration)
        except ValueError as exc:
            return build_redirect_with_message(error=str(exc), url='/admin/catalogs')
        return build_redirect_with_message(message=f'Уровень «{name}» добавлен', url='/admin/catalogs')

    def resolve_education_level(request: Request, level_id: str) -> tuple[dict | None, str | None]:
        """Уровень образования по id: (уровень, ошибка-текст/None)."""
        try:
            numeric_level_id = int(level_id)
        except ValueError:
            return None, text(body='Invalid level id', status=400)
        level = get_storage(request.app).get_education_level(numeric_level_id)
        if level is None:
            return None, build_redirect_with_message(error='Уровень образования не найден', url='/admin/catalogs')
        return level, None

    @app.post('/admin/education-levels/<level_id>/duration')
    async def update_education_level_duration(request: Request, level_id: str):
        """Inline-правка длительности по умолчанию (пусто = убрать)."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        level, error = resolve_education_level(request, level_id)
        if error is not None:
            return error

        duration, duration_error = parse_optional_int_form(request, 'default_duration_years', label='Длительность')
        if duration_error is not None:
            return build_redirect_with_message(error=duration_error, url='/admin/catalogs')
        try:
            get_storage(request.app).update_education_level(level['id'], duration)
        except ValueError as exc:
            return build_redirect_with_message(error=str(exc), url='/admin/catalogs')
        return build_redirect_with_message(message='Длительность уровня сохранена', url='/admin/catalogs')

    @app.post('/admin/education-levels/<level_id>/hide')
    async def hide_education_level(request: Request, level_id: str):
        """Скрыть уровень: не выбирается в новых учебных данных групп,
        остаётся у групп, где уже задан."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        level, error = resolve_education_level(request, level_id)
        if error is not None:
            return error

        get_storage(request.app).set_education_level_active(level['id'], False)
        return build_redirect_with_message(message='Уровень скрыт', url='/admin/catalogs')

    @app.post('/admin/education-levels/<level_id>/unhide')
    async def unhide_education_level(request: Request, level_id: str):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        level, error = resolve_education_level(request, level_id)
        if error is not None:
            return error

        get_storage(request.app).set_education_level_active(level['id'], True)
        return build_redirect_with_message(message='Уровень снова виден', url='/admin/catalogs')

    @app.post('/admin/education-levels/<level_id>/delete')
    async def delete_education_level(request: Request, level_id: str):
        """Удалить уровень — только если ни одна группа на него не ссылается."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        level, error = resolve_education_level(request, level_id)
        if error is not None:
            return error

        storage = get_storage(request.app)
        try:
            storage.delete_education_level(level['id'])
        except ValueError as exc:
            return build_redirect_with_message(error=str(exc), url='/admin/catalogs')
        log_audit_event(
            request,
            'education_level_deleted',
            {'level_id': level['id'], 'name': level['name']},
        )
        return build_redirect_with_message(message='Уровень удалён', url='/admin/catalogs')

    # --- Course / Education, Phase A: учебные данные группы. ---

    @app.get('/admin/catalogs/group/<value_id>/academic')
    async def catalog_group_academic_page(request: Request, value_id: str):
        """Учебные данные группы: уровень образования, год поступления,
        длительность. Год из названия группы — ТОЛЬКО предзаполнение."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        group, institute, error = resolve_group_academic_target(request, value_id)
        if error is not None:
            return error

        storage = get_storage(request.app)
        stored = storage.get_group_academic(group['id'])
        suggested_year = (
            parse_group_admission_year(group['value'])
            if stored is None or stored.get('admission_year') is None
            else None
        )
        return await render(
            template_name=jinja_env.get_template('admin_catalog_group_academic.html'),
            context={
                'request': request,
                'value_id': group['id'],
                'group_value': group['value'],
                'institute_value': institute['value'],
                'levels': storage.list_education_levels(),
                'stored': stored,
                'suggested_admission_year': suggested_year,
                'suggested_evidence': parse_group_admission_year_evidence(group['value'])
                if suggested_year is not None
                else None,
                'duration_hint': group_academic_duration_hint(stored),
                'course_preview': group_academic_course_preview(stored),
                'min_admission_year': MIN_ADMISSION_YEAR,
                'max_admission_year': MAX_ADMISSION_YEAR,
                'min_duration_years': MIN_DURATION_YEARS,
                'max_duration_years': MAX_DURATION_YEARS,
                **get_flash_args(request),
            },
        )

    @app.post('/admin/catalogs/group/<value_id>/academic')
    async def catalog_group_academic_save(request: Request, value_id: str):
        """Сохранить учебные данные группы одной upsert-транзакцией."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        group, institute, error = resolve_group_academic_target(request, value_id)
        if error is not None:
            return error
        back_url = f'/admin/catalogs/group/{group["id"]}/academic'

        raw_level = get_form_value(request, 'education_level_id').strip()
        education_level_id = None
        if raw_level:
            education_level_id, level_error = parse_optional_int_form(
                request, 'education_level_id', label='Уровень образования'
            )
            if level_error is not None:
                return build_redirect_with_message(error=level_error, url=back_url)
        admission_year, year_error = parse_optional_int_form(request, 'admission_year', label='Год поступления')
        if year_error is not None:
            return build_redirect_with_message(error=year_error, url=back_url)
        duration_override, duration_error = parse_optional_int_form(
            request, 'duration_years_override', label='Длительность обучения'
        )
        if duration_error is not None:
            return build_redirect_with_message(error=duration_error, url=back_url)

        try:
            get_storage(request.app).upsert_group_academic(
                group['id'],
                education_level_id=education_level_id,
                admission_year=admission_year,
                duration_years_override=duration_override,
            )
        except ValueError as exc:
            return build_redirect_with_message(error=str(exc), url=back_url)

        log_audit_event(
            request,
            'group_academic_updated',
            {
                'group_id': group['id'],
                'group': group['value'],
                'institute': institute['value'],
                'education_level_id': education_level_id,
                'admission_year': admission_year,
                'duration_years_override': duration_override,
            },
        )
        return build_redirect_with_message(
            message=f'Учебные данные группы «{group["value"]}» сохранены.',
            url=back_url,
        )

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
                link_target=parse_link_target_form(
                    request, field_type=field_type, custom_fields=storage.get_custom_fields()
                ),
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
        # №24: старое значение привязки нужно для audit-события при изменении.
        old_field = next(
            (field for field in storage.get_custom_fields(include_inactive=True) if field.field_id == numeric_field_id),
            None,
        )
        new_link_target = parse_link_target_form(
            request,
            field_type=field_type,
            custom_fields=storage.get_custom_fields(),
            exclude_key=old_field.key if old_field else None,
        )
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
            link_target=new_link_target,
        )
        if old_field is not None and old_field.link_target != new_link_target:
            log_audit_event(
                request,
                'field_settings_changed',
                {
                    'fields': {
                        old_field.key: {
                            'link_target': {'old': old_field.link_target, 'new': new_link_target},
                        }
                    }
                },
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
        invalid_username_message = username_error(username)
        if invalid_username_message:
            return build_redirect_with_message(error=invalid_username_message, url='/admin/users')
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

    @app.post('/admin/levels')
    async def create_level(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        name = get_form_value(request, 'name').strip()
        if not name:
            return build_redirect_with_message(error='Название уровня обязательно', url='/admin/catalogs')
        # Уровень всегда хранится в нижнем регистре (так нормализуются импорт
        # и записи) — ручное добавление приведено к тому же виду. Именно ручное
        # добавление без нормализации было источником регистровых дублей (№1,
        # docs/feedback-live.md).
        name = name.lower()
        existing_levels = get_storage(request.app).get_level_names(include_inactive=True)
        # №1.1: дубль (в том числе регистровый — имя уже в lower) — отказ.
        if any(item.lower() == name for item in existing_levels):
            return build_redirect_with_message(error='Такой уровень уже существует', url='/admin/catalogs')

        get_storage(request.app).create_level(name)
        return build_redirect_with_message(message='Уровень добавлен', url='/admin/catalogs')

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

    @app.post('/admin/levels/<level_id>/hard-delete')
    async def hard_delete_level(request: Request, level_id: str):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        try:
            numeric_level_id = int(level_id)
        except ValueError:
            return text(body='Invalid level id', status=400)

        storage = get_storage(request.app)
        level = next((item for item in storage.list_levels() if item['id'] == numeric_level_id), None)
        if level is None:
            return build_redirect_with_message(error='Уровень не найден', url='/admin/catalogs')

        records_count = storage.count_records_using('level', level['name'])
        if records_count > 0:
            return build_redirect_with_message(
                error='У уровня есть записи — скройте или переименуйте', url='/admin/catalogs'
            )

        storage.hard_delete_level(numeric_level_id)
        log_audit_event(
            request,
            'catalog_value_deleted',
            {'category': 'level', 'value': level['name'], 'level_id': numeric_level_id},
        )
        return build_redirect_with_message(message='Уровень удалён', url='/admin/catalogs')

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

    # --- Карточки студентов (Student Identity v1, Phase 1 — фундамент). ---
    #
    # Отдельная таблица актуальных данных студента (ФИО/пол/институт/группа/курс)
    # и его псевдонимов ФИО. В Phase 1 карточки живут ИЗОЛИРОВАННО: записи
    # соревнований, аккаунты, импорт, отчёты и merge про них не знают
    # (student_ref_id не пишется, авто-создания/авто-связей нет). Правка карточки
    # меняет ТОЛЬКО актуальные «профильные» данные, исторические записи не
    # перезаписываются.

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
                'unlinked_records_count': storage.count_participations_without_event(),
                # Course/Education Phase A: записи без снимка года поступления.
                'without_admission_year_count': storage.count_participations_without_admission_year(),
                'stats': stats,
                **get_flash_args(request),
            },
        )

    @app.get('/admin/maintenance/calendar-links')
    async def admin_maintenance_calendar_links_page(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        storage = get_storage(request.app)
        preview = decorate_calendar_link_preview(
            storage.calendar_link_backfill_preview(),
            {event['id']: event for event in storage.list_calendar_events()},
        )
        return await render(
            template_name=jinja_env.get_template('admin_maintenance_links.html'),
            context={
                'request': request,
                'preview': preview,
                **get_flash_args(request),
            },
        )

    @app.post('/admin/maintenance/calendar-links/apply')
    async def apply_maintenance_calendar_links(request: Request):
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        counters, items = get_storage(request.app).apply_calendar_link_backfill()
        log_audit_event(
            request,
            'participation_batch_linked',
            {
                'linked': counters['linked'],
                'skipped': counters['skipped'],
                'items': items,
            },
        )
        if counters['skipped']:
            message = (
                f'Связывание завершено: связано {counters["linked"]}, '
                f'пропущено {counters["skipped"]} (уже связаны или соревнование не найдено).'
            )
        else:
            message = f'Связывание завершено: связано {counters["linked"]}.'
        return build_redirect_with_message(message=message, url='/admin/maintenance/calendar-links')

    # --- Course / Education, Phase A: backfill снимка года поступления. ---

    def decorate_admission_year_preview(preview: dict) -> dict:
        """Строки предпросмотра с датами-подписями и «уликой» парсера."""

        def decorate_record_row(row: dict) -> dict:
            return {
                **row,
                'record_date_label': format_date_range(
                    datetime.fromisoformat(row['date']),
                    None,
                ),
                'evidence': parse_group_admission_year_evidence(row['group']),
            }

        return {
            'counters': preview['counters'],
            'safe': [decorate_record_row(row) for row in preview['safe']],
            'manual': [decorate_record_row(row) for row in preview['manual']],
            'unresolved': [decorate_record_row(row) for row in preview['unresolved']],
            'apply_confirm_text': (
                f'Заполнить год поступления у {preview["counters"]["safe"]} записей? '
                'Обновится только снимок года поступления в записях о соревнованиях.'
            ),
        }

    @app.get('/admin/maintenance/admission-years')
    async def admin_maintenance_admission_years_page(request: Request):
        """Предпросмотр заполнения года поступления у записей без снимка:
        классификация по историческому названию группы в самой записи."""
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error
        storage = get_storage(request.app)
        preview = decorate_admission_year_preview(storage.group_admission_backfill_preview())
        return await render(
            template_name=jinja_env.get_template('admin_maintenance_admission_years.html'),
            context={
                'request': request,
                'preview': preview,
                **get_flash_args(request),
            },
        )

    @app.post('/admin/maintenance/admission-years/apply')
    async def apply_maintenance_admission_years(request: Request):
        """Заполнить год поступления ТОЛЬКО у однозначных (SAFE) записей.

        Классификация пересчитывается в момент применения; записи, которые
        успели получить год между предпросмотром и кликом, пропускаются
        (guarded UPDATE), пакет не ломается.
        """
        auth_error = require_admin(request)
        if auth_error is not None:
            return auth_error

        counters = get_storage(request.app).apply_group_admission_backfill()
        log_audit_event(
            request,
            'group_admission_backfill_applied',
            {
                'applied': counters['applied'],
                'skipped': counters['skipped'],
                'matched': counters['matched'],
            },
        )
        message = f'Годы поступления заполнены: {counters["applied"]} записей.'
        if counters['skipped']:
            message += f' пропущено {counters["skipped"]} (год уже задан или данные изменились).'
        return build_redirect_with_message(message=message, url='/admin/maintenance/admission-years')

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
