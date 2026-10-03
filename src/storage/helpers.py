"""Общие константы, исключения и функции хранилища Competitions.

Используется всеми модулями src/storage; src/storage/sqlite.py переэкспортирует
имена для совместимости импортов (`from src.storage.sqlite import ...`).
Перенесено из src/storage/sqlite.py без изменения поведения (Architecture v2.1)."""
import json
from typing import Collection
from typing import Iterable

from src.models.competition import Competition

# Справочники значений ведут себя как уровни: записи остаются свободным
# текстом, справочник — только подсказки (docs/data-model-decisions.md,
# «Справочники значений»). Уровни живут в своей таблице и сюда не входят.
CATALOG_CATEGORIES: tuple[str, ...] = ('sport', 'institute')

# Категории переименования значений справочника (решение 2026-09-13,
# docs/data-model-decisions.md «Переименование значений справочников»):
# уровень живёт в levels, остальные — в catalog_values. Колонка записей
# называется так же («group» — ключевое слово SQL, экранируется в запросах).
RENAME_CATEGORIES: tuple[str, ...] = ('level', 'sport', 'institute', 'group')

# P3 Runtime Identity: режимы идентичности кабинета атлета (app_settings,
# ключ identity_mode). 'dual' — читать И легаси-хеш ФИО, И стабильную связь
# student_ref_id (режим по умолчанию и на проде); 'ref' — только owner и
# student_ref_id (после ручной проверки отчётом, guard см.
# set_identity_mode_guarded).
IDENTITY_MODES: tuple[str, ...] = ('dual', 'ref')
IDENTITY_MODE_DEFAULT = 'dual'

# Ревалидация полей карточки при создании из storage (create_student_and_
# link_participation): допустимый «пол» — только М/Ж или пусто. Зеркалит
# STUDENT_SEX_OPTIONS из src/main.py (роут не импортируется сюда, чтобы не
# создавать цикл); изменение набора — синхронно в обоих местах.
STUDENT_SEX_VALUES: frozenset[str] = frozenset({'', 'М', 'Ж'})

# Сортировка списка карточек (GET-параметр sort страницы «Студенты»):
# белый список GET-значение → колонка ORDER BY. Идентификаторы ORDER BY
# никогда не собираются из сырого пользовательского ввода; main.py
# использует тот же словарь для разбора параметров и ссылок заголовков.
STUDENT_SORT_COLUMNS: dict[str, str] = {
    'name': 'full_name',
    'institute': 'institute',
    'group': 'group_name',
}

# Срезы отчёта (замечание №19, docs/data-model-decisions.md «Расширение
# отчётов»): группировка ВСЕГДА по данным записи — исторический факт на
# момент соревнования, смена группы/института в профиле строки не склеивает.
# Ключи синхронны с REPORT_SLICES в src/main.py. Выражения — колонки таблицы
# competitions; «год» — первые 4 символа ISO-даты записи.
REPORT_GROUPINGS: dict[str, tuple[str, ...]] = {
    'student': ('student_id', 'student_name', 'student_sex', 'institute', '"group"', 'course'),
    'group': ('"group"',),
    'institute': ('institute',),
    'course': ('course',),
    'sport': ('sport',),
    'level': ('level',),
    'year': ('CAST(substr(date, 1, 4) AS INTEGER)',),
}
DEFAULT_REPORT_GROUPING = 'student'

# Метрики считаются в SQL (SUM CASE по строкам внутри группы), не в питоне.
REPORT_METRIC_SELECTS: tuple[str, ...] = (
    'COUNT(*) AS count_participation',
    'SUM(CASE WHEN position = 1 THEN 1 ELSE 0 END) AS count_wins',
    'SUM(CASE WHEN position >= 1 AND position <= 3 THEN 1 ELSE 0 END) AS count_prizes',
)

# Выборка записи соревнований: общий SELECT для get_competitions и
# get_competitions_page (серверные фильтры/пагинация главной, прототип 02).
# P3 Runtime Identity: student_ref_id входит в выборку — dual-read кабинет
# атлета читает стабильную связь с карточкой наравне с легаси-ключом.
COMPETITION_SELECT_SQL = '''
    SELECT
        id,
        student_id,
        student_name,
        student_sex,
        institute,
        "group",
        course,
        sport,
        date,
        date_to,
        level,
        name,
        position,
        created_at,
        extra_data,
        review_status,
        owner_id,
        review_comment,
        student_ref_id,
        discipline,
        result,
        calendar_event_id,
        admission_year
    FROM competitions
    '''

# Вставка записей соревнований: общий SQL для одиночного сохранения и импорта.
# student_ref_id пишется только из модели (явный выбор/создание карточки
# студента); по умолчанию поле None → NULL, существующие пути не меняются
# (P3: записанное значение читает кабинет атлета, режим dual/ref).
# Wave 1 P1: discipline/result/calendar_event_id проводятся так же — None
# → NULL, runtime поля не читает.
COMPETITION_INSERT_SQL = '''
    INSERT INTO competitions (
        student_id,
        student_name,
        student_sex,
        institute,
        "group",
        course,
        sport,
        date,
        date_to,
        level,
        name,
        position,
        created_at,
        extra_data,
        review_status,
        owner_id,
        review_comment,
        student_ref_id,
        discipline,
        result,
        calendar_event_id,
        admission_year
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    '''


class EventParticipationConflictError(Exception):
    """Повторная проверка батча участий события (P4) нашла коллизию: участие
    с тем же ключом уже существует в БД или вставляется этим же батчем
    (intra-batch first-wins). Транзакция откатывается целиком; предпросмотр
    нужно обновить и заново разобрать строки."""


class CalendarEventDuplicateError(Exception):
    """Guard точных дублей событий календаря (2026-09-27,
    docs/data-model-decisions.md «Duplicate Calendar Events guard»):
    событие с тем же identity-ключом (event_identity_key) уже существует.
    existing — найденный дубль {id, name, date, date_to, sport, level}.
    Новое событие не создаётся/правка не применяется; аудит успеха роуты
    для этого пути не пишут (блок — не успех)."""

    def __init__(self, existing: dict):
        super().__init__(f'calendar event duplicate: id={existing.get("id")}')
        self.existing = existing


def dedup_text(value) -> str:
    """Текст ключа дедупликации участий (P4): strip + casefold."""
    if value is None:
        return ''
    return str(value).strip().casefold()


def dedup_place(value) -> str:
    """Место/курс в ключе (P4): 0 и пустое значение — «нет значения»
    (0 = «ждёт результата»), остальное — строкой (место может быть text)."""
    if value is None:
        return ''
    text = str(value).strip()
    return '' if text in ('', '0') else text.casefold()


def dedup_discipline(value) -> str:
    """Дисциплина в ключе дедупликации (P5a/P4): пробелы схлопываются,
    регистр не важен. Единая реализация для реестра и участий события —
    src/main.py::normalize_discipline_for_dedup делегирует сюда."""
    if not value:
        return ''
    return ' '.join(str(value).split()).casefold()


def event_identity_key(name, date, date_to, sport, level) -> tuple:
    """Ключ точного дубля события календаря (guard дублей 2026-09-27):
    нормализованное название (dedup_discipline — пробелы/регистр), даты —
    первые 10 символов (толерантность 'YYYY-MM-DD' и '…T00:00:00'), вид
    спорта и уровень — dedup_text. NULL и '' неразличимы (пустое = нет
    значения). Существующие дубли БД guard не трогает — только новые
    create/update."""
    return (
        dedup_discipline(name),
        (date or '')[:10],
        (date_to or '')[:10],
        dedup_text(sport),
        dedup_text(level),
    )


def participation_content_key(
    student_name,
    discipline,
    sex,
    institute,
    group,
    course,
    position,
    result,
    extra_data,
    exclude_custom_keys: Collection[str] = (),
) -> tuple:
    """Полный контент участия для сравнения ТОЧНЫХ дублей (P4).

    Контент-ключ CARDLESS-строк: ФИО + дисциплина (ключ повторов без
    карточки) + пол/институт/группа/курс/место/результат + custom-значения
    (сравниваются только непустые — пустая ячейка и отсутствие колонки
    неразличимы). Одна реализация для классификации предпросмотра
    (src/main.py) и серверной проверки apply_event_participation_batch:
    стороны обязаны сравнивать символ в символ.

    QA D2 (F′ v2): discipline/result сравниваются ЭФФЕКТИВНЫМИ значениями
    (base ?? legacy-фолбэк в extra_data коллидирующего кастома), поэтому
    сырые значения тех же коллидирующих кастомов в customs НЕ участвуют —
    exclude_custom_keys выкидывает их СИММЕТРИЧНО у существующей строки и
    вставляемой (легаси-строка до P4 несёт дисциплину только в extra_data,
    вставляемая — только в базовой колонке). Остальные customs — как раньше."""
    exclude = {str(key) for key in exclude_custom_keys}
    customs = tuple(
        sorted(
            (str(key), dedup_text(value))
            for key, value in (extra_data or {}).items()
            if dedup_text(value) and str(key) not in exclude
        )
    )
    return (
        dedup_text(student_name),
        dedup_discipline(discipline),
        dedup_text(sex),
        dedup_text(institute),
        dedup_text(group),
        dedup_place(course),
        dedup_place(position),
        dedup_text(result),
        customs,
    )


def competition_insert_records(
    competitions: Iterable[Competition],
    review_status: str,
    owner_id: int | None,
) -> list[tuple]:
    """Параметры для executemany по COMPETITION_INSERT_SQL."""
    return [
        (
            item.student_id,
            item.student_name,
            item.student_sex,
            item.institute,
            item.group,
            item.course,
            item.sport,
            item.date.isoformat(),
            item.date_to.isoformat() if item.date_to else None,
            item.level,
            item.name,
            item.position,
            item.created_at.isoformat(),
            json.dumps(item.extra_data, ensure_ascii=False),
            review_status,
            owner_id,
            '',
            item.student_ref_id,
            item.discipline,
            item.result,
            item.calendar_event_id,
            # Course/Education Phase A: снимок года поступления; storage
            # заполняет его до вставки (_fill_admission_year_snapshots).
            item.admission_year,
        )
        for item in competitions
    ]


# Дефолты настроек базовых полей (лёгкий реестр полей, решение 2026-09-13):
# value_type + required, воспроизводящие сегодняшнее поведение валидации.
# Синхронно с BASE_FIELD_SETTING_DEFAULTS в src/main.py.
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


# P5b: лимит списков предпросмотра массового связывания (matched/ambiguous/
# unmatched); счётчики при этом полные — страницы-группы показывают первые
# CALENDAR_LINK_PREVIEW_CAP строк, остальное владелец разбирает поиском/фильтром.
CALENDAR_LINK_PREVIEW_CAP = 200
