"""Логика записей соревнований, общая для реестра/календаря/админки.

Спецификации и настройки базовых полей, построение записи (build_competition),
канонизация справочников, ключи дедупликации, участие-хелперы событий и
экспортные строки/датафреймы. Перенесено из src/main.py без изменения
поведения (Architecture v1)."""
import hashlib
import re
from datetime import datetime
from typing import Iterable
from typing import Sequence

import pandas as pd
from pandas import isna
from sanic import Request

from src.models.competition import Competition
from src.models.custom_field import CustomField
from src.settings import settings
from src.storage.sqlite import dedup_discipline
from src.storage.sqlite import participation_content_key
from src.storage.sqlite import SQLiteAdapter
from src.web import clean_str
from src.web import format_date_range
from src.web import get_form_value
from src.web import is_http_url
from src.web import parse_date_value


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


# Event Model, P5a (docs/data-model-decisions.md «Event Model, P5a»):
# «Дисциплина» — опциональная фиксированная колонка шаблона импорта после
# базовых; «Дисциплина»/«Результат» — фиксированные колонки выгрузки реестра
# после «Курса». Значения живут в базовых discipline/result записи, а не в
# кастомных полях.
IMPORT_DISCIPLINE_COLUMN = 'Дисциплина'


INDEX_EXPORT_COLUMNS: Sequence[str] = (
    *tuple(field['label'] for field in BASE_FIELD_SPECS),
    'Дисциплина',
    'Результат',
)


def collides_with_fixed_columns(label: str, fold_labels) -> bool:
    """Label кастомного поля совпадает с фиксированной колонкой (casefold)."""
    return label.strip().casefold() in fold_labels


def select_export_custom_fields(custom_fields: Sequence[CustomField]) -> list[CustomField]:
    """Кастомные поля выгрузки реестра: show_in_export и без коллизии label
    с фиксированными «Дисциплина»/«Результат» — заголовок в выгрузке один,
    значение отдаётся из базовой колонки записи. Определение поля и его
    значения в реестре/extra_data не трогаются."""
    fold_labels = {'дисциплина', 'результат'}
    return [
        field
        for field in custom_fields
        if field.show_in_export and not collides_with_fixed_columns(field.label, fold_labels)
    ]


def select_template_custom_fields(custom_fields: Sequence[CustomField]) -> list[CustomField]:
    """Кастомные поля шаблона импорта: show_in_template и без коллизии label
    с фиксированной опциональной колонкой «Дисциплина» (в шаблоне «Результата»
    нет — кастомное поле с таким label в шаблон попадает как обычно)."""
    fold_labels = {'дисциплина'}
    return [
        field
        for field in custom_fields
        if field.show_in_template and not collides_with_fixed_columns(field.label, fold_labels)
    ]


# Лёгкий реестр полей (решение 2026-09-13, docs/data-model-decisions.md
# «Реестр полей: лёгкая версия сейчас, полная запланирована»): у каждого
# базового поля настраиваются ТИП (text/number; link базовым недоступен)
# и ОБЯЗАТЕЛЬНОСТЬ. Дефолты воспроизводят сегодняшнее поведение валидации
# (числовые — Место и Курс; обязательные — ФИО, Дата, Курс). Синхронно
# с BASE_FIELD_SETTING_DEFAULTS в src/storage/sqlite.py.
BASE_FIELD_SETTING_DEFAULTS: dict[str, tuple[str, bool]] = {
    'student_name': ('text', True),
    'student_sex': ('text', False),
    'institute': ('text', False),
    'group': ('text', False),
    'sport': ('text', False),
    'date': ('text', True),
    'level': ('text', False),
    'name': ('text', False),
    'position': ('number', False),
    'course': ('number', True),
}


BASE_FIELD_LABELS: dict[str, str] = {field['key']: field['label'] for field in BASE_FIELD_SPECS}


# ФИО и Дата — ключ дедупликации и срезы отчётов: обязательность не
# отключается (в UI задизейблена с пояснением), тип фиксированный.
ALWAYS_REQUIRED_BASE_FIELDS: frozenset[str] = frozenset({'student_name', 'date'})


BASE_FIELD_VALUE_TYPES: Sequence[str] = ('text', 'number')


FIELD_TYPE_OPTIONS: Sequence[str] = ('text', 'number', 'date', 'url')


# №24 (docs/feedback-live.md): колонки, к которым можно привязать link-поле —
# базовые текстовые колонки таблицы. Сортировка/инлайн в целевой колонке
# остаются по её тексту, ссылка только оборачивает текст в <a>.
LINK_TARGETABLE_BASE_KEYS: Sequence[str] = (
    'student_name',
    'institute',
    'group',
    'sport',
    'level',
    'name',
)


def get_base_field_settings(storage: SQLiteAdapter | None) -> dict[str, dict]:
    """Эффективные настройки базовых полей: дефолты + строки field_settings.

    Настройки опциональны построчно: поле без строки в таблице действует
    по дефолту (п.4 постановки). ФИО и Дата всегда обязательны — их
    required принудительно True (ключ дедупликации и срезы отчётов).
    """
    merged = {
        key: {'value_type': value_type, 'required': required}
        for key, (value_type, required) in BASE_FIELD_SETTING_DEFAULTS.items()
    }
    if storage is not None:
        for key, setting in storage.get_field_settings().items():
            if key in merged and setting['value_type'] in BASE_FIELD_VALUE_TYPES:
                merged[key] = {'value_type': setting['value_type'], 'required': bool(setting['required'])}
    for key in ALWAYS_REQUIRED_BASE_FIELDS:
        # ФИО и Дата: обязательность не отключается, тип фиксированный —
        # даже если в таблице оказалась иная настройка.
        default_type = BASE_FIELD_SETTING_DEFAULTS[key][0]
        merged[key] = {'value_type': default_type, 'required': True}
    return merged


def normalize_discipline_for_dedup(value) -> str:
    """Дисциплина для ключа дубля (Event Model, P5a): пробелы схлопываются,
    регистр не важен — то же написание с другими пробелами/регистром
    остаётся дублем. Хранится в записи оригинальное написание как введено;
    нормализация — только внутри ключа дедупликации.
    P4: реализация делегирует общей функции storage — ключ участий события
    и ключ реестра обязаны нормализоваться одинаково.
    """
    return dedup_discipline(value)


def parse_base_number_field(raw, *, label: str, setting: dict, empty_error: str, empty_value: int = 0):
    """number-поле базового реестра: пустое при required — отказ, без
    required — empty_value (как Место раньше давал 0)."""
    value = clean_str(raw)
    if not value:
        if setting['required']:
            raise ValueError(empty_error)
        return empty_value
    try:
        # Excel-ячейки приходят числами (в т.ч. float) — как раньше,
        # int() принимает и их; строка чистится.
        return int(value) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        raise ValueError(f'Поле "{label}" должно быть числом') from None


def parse_base_text_field(raw, *, label: str, setting: dict):
    """text-поле базового реестра: строка без требования числа
    (кейс «Курс: Выпускник 2025/26», замечания №2/№7)."""
    value = clean_str(raw)
    if not value and setting['required']:
        raise ValueError(f'Поле "{label}" обязательно')
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
        if not is_http_url(value):
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
    storage: SQLiteAdapter | None = None,
) -> Competition:
    field_settings = get_base_field_settings(storage)
    student_name = clean_str(record['ФИО'])
    if not student_name:
        raise ValueError('ФИО обязательно')

    date, date_to = parse_date_value(record['Дата'], manual_input)

    # required=1 у текстового базового поля — пустое значение = отказ
    # (как ФИО/дата сейчас). Числовые Место/Курс — в parse_base_number_field.
    level = parse_base_text_field(
        record['Уровень соревнований'],
        label=BASE_FIELD_LABELS['level'],
        setting=field_settings['level'],
    ).lower()

    competition = Competition(
        student_id=hashlib.sha256(student_name.encode()).hexdigest(),
        student_name=student_name,
        student_sex=parse_base_text_field(
            record['Пол'], label=BASE_FIELD_LABELS['student_sex'], setting=field_settings['student_sex']
        ),
        institute=parse_base_text_field(
            record['Институт'], label=BASE_FIELD_LABELS['institute'], setting=field_settings['institute']
        ),
        group=parse_base_text_field(
            record['Группа'], label=BASE_FIELD_LABELS['group'], setting=field_settings['group']
        ),
        date=date,
        date_to=date_to,
        sport=parse_base_text_field(
            record['Вид спорта'], label=BASE_FIELD_LABELS['sport'], setting=field_settings['sport']
        ),
        level=level,
        name=parse_base_text_field(
            record['Название соревнований'], label=BASE_FIELD_LABELS['name'], setting=field_settings['name']
        ),
        position=parse_base_number_field(
            record['Место'],
            label=BASE_FIELD_LABELS['position'],
            setting=field_settings['position'],
            empty_error='Поле "Место" обязательно',
        ),
        course=(
            parse_base_text_field(record['Курс'], label=BASE_FIELD_LABELS['course'], setting=field_settings['course'])
            if field_settings['course']['value_type'] != 'number'
            else parse_base_number_field(
                record['Курс'],
                label=BASE_FIELD_LABELS['course'],
                setting=field_settings['course'],
                empty_error='Курс обязателен',
            )
        ),
        # Event Model, P5a: опциональная колонка «Дисциплина» импорта/форм —
        # в базовую колонку записи, пустая ячейка → None. Если активен кастом
        # с тем же label, значение попадает и в extra_data (ниже) — двойная
        # запись, self-healing. Базовый result импортом не пишется.
        discipline=clean_str(record.get(IMPORT_DISCIPLINE_COLUMN)) or None,
        extra_data=extract_custom_field_values(record, custom_fields),
    )
    if storage is not None:
        apply_catalog_canonical_values(storage, competition)
    return competition


def apply_catalog_canonical_values(storage: SQLiteAdapter, competition: Competition) -> None:
    """№1.1 (docs/feedback-live.md): значения справочников — в каноническом регистре.

    Если значение записи отличается от значения справочника ТОЛЬКО регистром,
    в запись подставляется каноническое написание из справочника. Исторические
    записи не трогаются — замена происходит только на пути создания записи
    (ручной ввод, импорт). Уровень по-прежнему нормализуется в lower
    (build_competition), а затем каноническое написание из levels имеет
    приоритет — так записи согласованы со справочником.

    Группа ищется внутри института: уникальность группы — по паре
    (институт, группа), поэтому каноническое подставляется только когда
    канонический институт найден в иерархии.
    """
    if competition.level:
        canonical = storage.find_level_canonical(competition.level)
        if canonical:
            competition.level = canonical
    for category in ('sport', 'institute'):
        value = getattr(competition, category)
        if value:
            canonical = storage.find_catalog_canonical(category, value)
            if canonical:
                setattr(competition, category, canonical)
    if competition.institute and competition.group:
        institute = storage.find_catalog_row('institute', competition.institute)
        if institute is not None:
            canonical = storage.find_catalog_canonical('group', competition.group, parent_id=institute['id'])
            if canonical:
                competition.group = canonical
    elif competition.group and not competition.institute:
        autofill_institute_from_group(storage, competition)


def autofill_institute_from_group(storage: SQLiteAdapter, competition: Competition) -> None:
    """№19а (docs/feedback-live.md): автозаполнение института по группе.

    Если имя группы принадлежит ровно одному институту иерархии справочников,
    подставляется канонический институт (и каноническое написание группы).
    Неизвестная группа или одно имя в разных институтах — институт остаётся
    пустым: свободный ввод не ломаем.
    """
    institute_value = storage.find_unique_group_institute(competition.group)
    if not institute_value:
        return
    competition.institute = institute_value
    institute = storage.find_catalog_row('institute', institute_value)
    if institute is not None:
        canonical = storage.find_catalog_canonical('group', competition.group, parent_id=institute['id'])
        if canonical:
            competition.group = canonical


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
    # Служебная стабильная связь с карточкой студента — внутренняя колонка,
    # в Excel-выгрузки (реестр/отчёты/обслуживание) не отдаётся: формат
    # выгрузок — часть контракта импорта/экспорта и не меняется.
    row.pop('student_ref_id', None)
    # Event Model, P5a: дисциплина/результат — фиксированные колонки выгрузки
    # «Дисциплина»/«Результат» после «Курса» (порядок задаёт reindex в
    # build_index_dataframe), NULL → пустая ячейка. Служебная ссылка
    # calendar_event_id по-прежнему внутренняя и не отдаётся.
    row['Дисциплина'] = competition.discipline or ''
    row['Результат'] = competition.result or ''
    row.pop('discipline', None)
    row.pop('result', None)
    row.pop('calendar_event_id', None)
    # Даты-диапазоны: экспорт компактной строкой в существующей колонке
    # «Дата» — шаблон импорта не ломается, выгрузку можно импортировать
    # обратно (парсер диапазона работает на той же колонке).
    row['Дата'] = format_date_range(competition.date, competition.date_to)
    row.pop('Дата по', None)
    for field in export_custom_fields:
        row[field.label] = competition.extra_data.get(field.key, '')
    return row


def event_participation_effective_values(participant: dict, custom_fields: Sequence[CustomField]) -> tuple[str, str]:
    """(дисциплина, результат) СУЩЕСТВУЮЩЕГО участия для сравнения с импортом.

    Базовые колонки записи; NULL/пусто — legacy-фолбэк в extra_data активного
    кастома с коллидирующим label «Дисциплина»/«Результат» (dual-write P5a:
    записи, вставленные до P4, несут значение только в кастоме). Только
    чтение: ни запись, ни extra_data не меняются."""
    extra = participant.get('extra_data') or {}

    def effective(base, folded_label: str) -> str:
        value = str(base or '').strip()
        if value:
            return value
        for field in custom_fields:
            if field.label.strip().casefold() == folded_label:
                fallback = str(extra.get(field.key) or '').strip()
                if fallback:
                    return fallback
        return ''

    return (
        effective(participant.get('discipline'), 'дисциплина'),
        effective(participant.get('result'), 'результат'),
    )


def event_participant_identity(participant: dict, custom_fields: Sequence[CustomField]) -> tuple | None:
    """identity существующего участия: (карточка, дисциплина); None — участие
    без связи с карточкой (для него уникальность — точный контент)."""
    if participant.get('student_ref_id') is None:
        return None
    discipline, _ = event_participation_effective_values(participant, custom_fields)
    return ('ref', participant['student_ref_id'], dedup_discipline(discipline))


def event_participation_collision_custom_keys(custom_fields: Sequence[CustomField]) -> set[str]:
    """Ключи кастомов с label, коллидирующим с фиксированными колонками
    «Дисциплина»/«Результат» (QA D2, F′ v2): их сырые значения в extra_data
    НЕ участвуют в customs-части контент-ключа — дисциплина/результат
    сравниваются эффективными значениями (base ?? legacy-фолбэк), и легаси-
    строка (значение только в кастоме) не должна отличаться от вставляемой
    (значение только в базовой колонке). Тот же набор коллизий, что у
    legacy-фолбэка event_participation_effective_values и серверной
    проверки apply_event_participation_batch."""
    fold_labels = {'дисциплина', 'результат'}
    return {field.key for field in custom_fields if collides_with_fixed_columns(field.label, fold_labels)}


def event_participant_content(participant: dict, custom_fields: Sequence[CustomField]) -> tuple:
    """Полный контент существующего участия (единый participation_content_key
    storage — сравнение с контентом вставляемой записи символ в символ)."""
    discipline, result = event_participation_effective_values(participant, custom_fields)
    return participation_content_key(
        participant['student_name'],
        discipline,
        participant['student_sex'],
        participant['institute'],
        participant['group_name'],
        participant['course'],
        participant['position'],
        result,
        participant.get('extra_data') or {},
        event_participation_collision_custom_keys(custom_fields),
    )


def competition_content(competition: Competition, custom_fields: Sequence[CustomField]) -> tuple:
    """Полный контент вставляемой записи участия (та же форма ключа);
    коллидирующие кастомы исключаются симметрично существующей строке
    (QA D2 — см. event_participation_collision_custom_keys)."""
    return participation_content_key(
        competition.student_name,
        competition.discipline,
        competition.student_sex,
        competition.institute,
        competition.group,
        competition.course,
        competition.position,
        competition.result,
        competition.extra_data,
        event_participation_collision_custom_keys(custom_fields),
    )


def event_participation_matched_duplicate_guard(
    storage: SQLiteAdapter,
    event: dict,
    competition: Competition,
    *,
    exclude_record_id: int | None = None,
) -> str | None:
    """Глобальный matched-инвариант участий события (P4, QA D2): у события не
    может быть двух участий ОДНОЙ карточки с той же нормализованной
    дисциплиной. Импорт вставляет через guarded-батч (apply_event_participation_
    batch), ручной ввод идёт через save_competitions — эта проверка закрывает
    его теми же ключами: list_calendar_event_participants + identity с
    эффективной дисциплиной (legacy-фолбэк base ?? extra_data коллидирующего
    кастома) + dedup_discipline. Второй реализации нормализации нет.

    exclude_record_id (P7) — правка существующего участия: сама запись из
    сравнения исключается (смена дисциплины на собственную текущую — не
    конфликт), остальные участия проверяются как обычно.

    Правило НЕ применяется для cardless-строк и пустой дисциплины (для них
    остаются F′ v2/import semantics; глобального cardless-constraint нет).
    Возвращает текст ошибки или None (вставлять можно)."""
    discipline = dedup_discipline(competition.discipline)
    if competition.student_ref_id is None or not discipline:
        return None
    identity = ('ref', competition.student_ref_id, discipline)
    custom_fields = storage.get_custom_fields()
    for participant in storage.list_calendar_event_participants(event['id']):
        if exclude_record_id is not None and participant.get('record_id') == exclude_record_id:
            continue
        if event_participant_identity(participant, custom_fields) == identity:
            return 'Такое участие уже существует: у этого студента уже есть участие с этой дисциплиной в событии.'
    return None


def competition_duplicate_key(competition: Competition) -> tuple:
    # Event Model, P5a: дисциплина — пятый элемент ключа в нормализованном
    # виде (пробелы/регистр не различаются). Разные дисциплины в тот же день
    # — НЕ дубль: строка уходит в очередь подтверждения, а не пропускается
    # молча. Частичный ключ (ФИО+дата) не меняется.
    return (
        competition.student_name,
        competition.date.date().isoformat(),
        competition.sport,
        competition.name,
        normalize_discipline_for_dedup(competition.discipline),
    )


def ensure_catalog_values(storage: SQLiteAdapter, competitions: Iterable[Competition]):
    """Новое значение вида спорта/института из записи или импорта попадает в справочник.

    Как с уровнями: справочник только собирает подсказки и ничего не
    перезаписывает в записях (docs/data-model-decisions.md). Дубликаты
    игнорируются на стороне storage. Пара институт→группа попадает в иерархию:
    группа кладётся под свой институт (уникальность (parent, value)).

    Для одиночных записей (ручной ввод). Массовый импорт пишет справочники
    батчем той же семантикой — SQLiteAdapter._ensure_import_catalogs.
    """
    for competition in competitions:
        for category, value in (('sport', competition.sport), ('institute', competition.institute)):
            if value:
                storage.add_catalog_value(category, value)
        if competition.institute and competition.group:
            storage.ensure_catalog_pair(competition.institute, competition.group)


def competition_partial_key(competition: Competition) -> tuple:
    """Похожесть (№6/№8): ФИО + дата_начала. Совпадение пары при несовпадении
    полного ключа дедупликации — кандидат в очередь подтверждения."""
    return (competition.student_name, competition.date.date().isoformat())


def build_index_dataframe(competitions, export_custom_fields) -> pd.DataFrame:
    df = pd.DataFrame.from_records([competition_to_export_row(comp, export_custom_fields) for comp in competitions])
    df = df.reindex(columns=list(INDEX_EXPORT_COLUMNS) + [field.label for field in export_custom_fields])
    for column in df.columns:
        if df[column].dtype == object:
            df[column] = df[column].map(sanitize_spreadsheet_value)
    return df


def sanitize_spreadsheet_value(value):
    if isinstance(value, str) and value[:1] in {'=', '+', '-', '@'}:
        return f"'{value}"
    return value
