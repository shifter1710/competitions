import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from src.models.competition import Competition
from src.models.custom_field import CustomField
from src.storage.catalogs import CatalogsMixin
from src.storage.education import EducationMixin
from src.storage.events import EventsMixin
from src.storage.gto import GtoMixin
from src.storage.helpers import BASE_FIELD_SETTING_DEFAULTS  # noqa: F401
from src.storage.helpers import CALENDAR_LINK_PREVIEW_CAP  # noqa: F401
from src.storage.helpers import CalendarEventDuplicateError  # noqa: F401
from src.storage.helpers import CATALOG_CATEGORIES  # noqa: F401
from src.storage.helpers import competition_insert_records  # noqa: F401
from src.storage.helpers import COMPETITION_INSERT_SQL  # noqa: F401
from src.storage.helpers import COMPETITION_SELECT_SQL  # noqa: F401
from src.storage.helpers import dedup_discipline  # noqa: F401
from src.storage.helpers import dedup_place  # noqa: F401
from src.storage.helpers import dedup_text  # noqa: F401
from src.storage.helpers import DEFAULT_REPORT_GROUPING  # noqa: F401
from src.storage.helpers import event_identity_key  # noqa: F401
from src.storage.helpers import EventParticipationConflictError  # noqa: F401
from src.storage.helpers import GTO_STAGE_MAX  # noqa: F401
from src.storage.helpers import GTO_STAGE_MIN  # noqa: F401
from src.storage.helpers import GTO_STAGES  # noqa: F401
from src.storage.helpers import GTO_STATUS_LABELS  # noqa: F401
from src.storage.helpers import GTO_STATUSES  # noqa: F401
from src.storage.helpers import gto_year_max  # noqa: F401
from src.storage.helpers import GTO_YEAR_MIN  # noqa: F401
from src.storage.helpers import IDENTITY_MODE_DEFAULT  # noqa: F401
from src.storage.helpers import IDENTITY_MODES  # noqa: F401
from src.storage.helpers import participation_content_key  # noqa: F401
from src.storage.helpers import RENAME_CATEGORIES  # noqa: F401
from src.storage.helpers import REPORT_GROUPINGS  # noqa: F401
from src.storage.helpers import REPORT_METRIC_SELECTS  # noqa: F401
from src.storage.helpers import STUDENT_SEX_VALUES  # noqa: F401
from src.storage.helpers import STUDENT_SORT_COLUMNS  # noqa: F401
from src.storage.misc import MiscMixin
from src.storage.participations import ParticipationsMixin
from src.storage.students import StudentsMixin
from src.storage.users import UsersMixin

# Переэкспорт из helpers для совместимости `from src.storage.sqlite import ...`

logger = logging.getLogger(__name__)


# SQLiteAdapter остаётся единственным адаптером хранения: соединение и RLock
# создаются и принадлежат его __init__ (Architecture v2.1 — доменные примеси).
# Примеси по доменам:
# MiscMixin (вложения, очередь конфликтов импорта, аудит),
# CatalogsMixin (настройки полей и справочники),
# UsersMixin (аккаунты, профили, псевдонимы ФИО, режим идентичности),
# StudentsMixin (карточки студентов),
# EventsMixin (календарь соревнований),
# EducationMixin (уровни образования, учебные данные групп, снимок года
# поступления записей и его backfill — Course/Education Phase A),
# GtoMixin (записи ГТО студентов),
# ParticipationsMixin (записи соревнований: выборки, отчёты, импорт).
# Порядок баз — только читаемость, перекрытий имён между примесями нет.
# Контракт примеси: не создаёт соединение и блокировку (self.connection /
# self._lock принадлежат SQLiteAdapter), не импортирует соседние доменные
# модули src/storage.* (кроме src.storage.helpers), междоменные вызовы —
# только через self. Тесты патчат методы на самом SQLiteAdapter — патч
# ложится раньше примесей в MRO и перехватывает вызовы через self.
class SQLiteAdapter(
    MiscMixin,
    CatalogsMixin,
    UsersMixin,
    StudentsMixin,
    EventsMixin,
    EducationMixin,
    GtoMixin,
    ParticipationsMixin,
):
    def __init__(self, database_path: str):
        db_path = Path(database_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # check_same_thread=False + lock: handlers may offload work to threads
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(
            db_path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self._create_schema()

    def _create_schema(self):
        with self._lock:
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS competitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id TEXT NOT NULL,
                    student_name TEXT NOT NULL,
                    student_sex TEXT NOT NULL,
                    institute TEXT NOT NULL,
                    "group" TEXT NOT NULL,
                    course INTEGER NOT NULL,
                    sport TEXT NOT NULL,
                    date TEXT NOT NULL,
                    level TEXT NOT NULL,
                    name TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )

            columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(competitions)').fetchall()}
            self._migrate_competitions_columns(columns)
            self._migrate_custom_fields_columns()

            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS custom_fields (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    field_type TEXT NOT NULL DEFAULT 'text',
                    required INTEGER NOT NULL DEFAULT 0,
                    show_in_table INTEGER NOT NULL DEFAULT 1,
                    show_in_export INTEGER NOT NULL DEFAULT 1,
                    show_in_template INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    link_target TEXT
                )
                '''
            )
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    uploaded_by INTEGER,
                    created_at TEXT NOT NULL
                )
                '''
            )
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS levels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1
                )
                '''
            )
            # Иерархия справочника (docs/data-model-decisions.md, «Справочники:
            # институты содержат группы»): записи категории group получают
            # parent_id на запись категории institute. Табличного UNIQUE нет —
            # одноимённые группы разных институтов сосуществуют; уникальность
            # держат частичные индексы ниже (плоские и дочерние раздельно:
            # в SQLite NULL в UNIQUE-колонке не равен NULL).
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS catalog_values (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    value TEXT NOT NULL,
                    parent_id INTEGER REFERENCES catalog_values(id),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                '''
            )
            self._migrate_catalog_values_parent()
            self.connection.execute(
                '''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_values_flat
                ON catalog_values (category, value)
                WHERE parent_id IS NULL
                '''
            )
            self.connection.execute(
                '''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_values_child
                ON catalog_values (category, parent_id, value)
                WHERE parent_id IS NOT NULL
                '''
            )
            self._populate_catalog_values()
            self._populate_catalog_hierarchy_pairs()
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    pwd_ver INTEGER NOT NULL DEFAULT 0
                )
                '''
            )
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    user_id INTEGER,
                    username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT ''
                )
                '''
            )
            # Календарь соревнований (волна A, docs/feedback-live.md №23):
            # план на год — шаблон-пресет для группы записей участников
            # (сами записи реестра не трогаются, участники — волна B).
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS calendar_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    date TEXT NOT NULL,
                    date_to TEXT,
                    level TEXT NOT NULL DEFAULT '',
                    sport TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    regulation_filename TEXT,
                    regulation_stored_name TEXT
                )
                '''
            )
            # Файл положения события (решение 2026-09-22): один файл на событие,
            # обе колонки NULL = файла нет. Аддитивная миграция легаси-таблицы.
            calendar_columns = {
                row['name'] for row in self.connection.execute('PRAGMA table_info(calendar_events)').fetchall()
            }
            if 'regulation_filename' not in calendar_columns:
                self.connection.execute('ALTER TABLE calendar_events ADD COLUMN regulation_filename TEXT')
            if 'regulation_stored_name' not in calendar_columns:
                self.connection.execute('ALTER TABLE calendar_events ADD COLUMN regulation_stored_name TEXT')
            # Несколько ссылок события (Multiple Event Links): у события 0..N
            # пар «Название + URL» в отдельной таблице; legacy-колонка url
            # сохраняется (NOT NULL DEFAULT '' — не дропаем), но новые записи
            # её не пишут. Порядок строк — (sort_order, id), детерминированный
            # порядок полей формы создания/правки.
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS calendar_event_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    calendar_event_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    url TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                '''
            )
            self.connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_calendar_event_links_event
                ON calendar_event_links (calendar_event_id, sort_order, id)
                '''
            )
            # Командные результаты соревнования (Team Results): у события 0..N
            # строк «категория + место команды» — итоги командного зачёта,
            # отдельно от личных мест участников (записей реестра). Владение —
            # Event: строки живут и умирают вместе с событием (удаление
            # события чистит их тем же commit). Логический FK по конвенции
            # проекта (PRAGMA foreign_keys не включается); без UNIQUE —
            # уникальность label внутри события держит приложение. Миграция
            # чисто аддитивная и идемпотентная, без backfill.
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS calendar_event_team_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    calendar_event_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    place INTEGER NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                '''
            )
            self.connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_calendar_event_team_results_event
                ON calendar_event_team_results (calendar_event_id, sort_order, id)
                '''
            )
            # Документы события (Event Documents): у события 0..N документов
            # (PDF/PNG/JPEG, валидация файла — в роуте) с заголовком и режимом
            # доступа. access_mode: 'all_participants' — всем Student-карточкам
            # с участием в событии (маппинги не пишутся), 'selected_students' —
            # только явно выбранным (таблица маппингов ниже). Файл лежит в
            # каталоге события data/files/calendar/<event_id>/ (рядом с
            # положением); строки живут и умирают вместе с событием (каскад
            # в delete_calendar_event тем же commit). Логический FK и
            # отсутствие CHECK — по конвенции соседних таблиц; уникальность
            # пары документ+студент держит UNIQUE-констрейнт. Миграция чисто
            # аддитивная и идемпотентная, без backfill.
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS calendar_event_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    calendar_event_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    access_mode TEXT NOT NULL,
                    uploaded_by INTEGER,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                '''
            )
            self.connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_calendar_event_documents_event
                ON calendar_event_documents (calendar_event_id, sort_order, id)
                '''
            )
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS calendar_event_document_students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    calendar_event_document_id INTEGER NOT NULL,
                    student_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (calendar_event_document_id, student_id)
                )
                '''
            )
            self.connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_calendar_event_document_students_student
                ON calendar_event_document_students (student_id)
                '''
            )
            # Backfill-and-clear: каждый непустой legacy-url становится ссылкой
            # события (label «Ссылка», В КОНЦЕ существующих — sort_order =
            # max+1), затем колонка url очищается — очищенная колонка
            # повторный запуск не триггерит (идемпотентность без
            # app_settings-флага). Guard по колонке url: в вырожденной базе
            # без неё шаг молчит. Сценарий rollback→old-code→redeploy: старый
            # код снова написал непустой url → он ДОписывается после
            # существующих ссылок события; дедуп — по точному совпадению url
            # (такая ссылка уже есть → новой строки нет, очищать колонку
            # не потеря). Всё — до общего commit _create_schema (одна
            # транзакция).
            if 'url' in calendar_columns:
                self.connection.execute(
                    '''
                    INSERT INTO calendar_event_links (calendar_event_id, label, url, sort_order, created_at)
                    SELECT id, 'Ссылка', url,
                           (
                               SELECT COALESCE(MAX(l.sort_order), -1) + 1
                               FROM calendar_event_links l
                               WHERE l.calendar_event_id = calendar_events.id
                           ),
                           ?
                    FROM calendar_events
                    WHERE TRIM(url) != ''
                      AND NOT EXISTS (
                          SELECT 1 FROM calendar_event_links l
                          WHERE l.calendar_event_id = calendar_events.id AND l.url = calendar_events.url
                      )
                    ''',
                    (datetime.utcnow().isoformat(),),
                )
                self.connection.execute("UPDATE calendar_events SET url = '' WHERE TRIM(url) != ''")
            # Лёгкий реестр полей (решение 2026-09-13, docs/data-model-decisions.md
            # «Реестр полей: лёгкая версия сейчас, полная запланирована»):
            # настройки ТИПА и ОБЯЗАТЕЛЬНОСТИ базовых полей. Дефолты отражают
            # сегодняшнее поведение; настройки опциональны построчно — при
            # отсутствии строки действует дефолт (см. populate ниже).
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS field_settings (
                    key TEXT PRIMARY KEY,
                    value_type TEXT NOT NULL DEFAULT 'text',
                    required INTEGER NOT NULL DEFAULT 0
                )
                '''
            )
            self._populate_field_settings_defaults()
            # Очередь конфликтов импорта (решение 2026-09-13, docs/data-model-
            # decisions.md «Конфликт-режим импорта: очередь на подтверждение»).
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS import_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    matched_record_id INTEGER,
                    created_by INTEGER
                )
                '''
            )
            # Карточки студентов (Student Identity v1, Phase 1 — фундамент):
            # актуальные данные и псевдонимы ФИО. Авто-связей нет: ссылки
            # записей/аккаунтов (student_ref_id) с Phase 2 заполняются только
            # вручную через сопоставление, легаси-идентичность sha256(ФИО)
            # не меняется (P3: связи читает кабинет атлета, режим dual/ref).
            # merged_into_id зарезервирована и не пишется/не читается.
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    full_name TEXT NOT NULL,
                    sex TEXT,
                    institute TEXT,
                    group_name TEXT,
                    course TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    merged_into_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                '''
            )
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS student_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (student_id, name)
                )
                '''
            )
            # Записи ГТО (Готов к труду и обороне): история ступеней и
            # результатов студента по годам. Принадлежит Student (НЕ Event
            # и не участие): identity — только стабильный students.id,
            # легаси-режим dual/ref и ФИО-хеши значения не имеют. Одна
            # запись на (student_id, year, stage) — UNIQUE держит дубль,
            # правка меняет статус той же строки. Ступень/статус/год —
            # исторический факт, из возраста не пересчитывается (DOB в
            # модели нет и не планируется). Логический FK и отсутствие
            # CHECK — по конвенции соседних таблиц (валидация в GtoMixin),
            # cascade НЕТ: удаление карточки блокируется записями ГТО.
            # Миграция чисто аддитивная и идемпотентная (паттерн
            # calendar_event_documents), без backfill.
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS student_gto_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL,
                    year INTEGER NOT NULL,
                    stage INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (student_id, year, stage)
                )
                '''
            )
            # Course / Education, Phase A: уровни образования и учебные
            # данные групп. FK объявлены декларативно (PRAGMA foreign_keys
            # приложение не включает — конвенция проекта, ссылки держит
            # код). Посев уровней — INSERT OR IGNORE (паттерн app_settings):
            # длительности по умолчанию НЕ хардкодятся (решение владельца),
            # повторные старты не дублируют и не перезаписывают.
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS education_levels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    default_duration_years INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                '''
            )
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS group_academic (
                    group_catalog_value_id INTEGER PRIMARY KEY REFERENCES catalog_values(id),
                    education_level_id INTEGER REFERENCES education_levels(id),
                    admission_year INTEGER,
                    duration_years_override INTEGER,
                    source TEXT NOT NULL DEFAULT 'manual',
                    updated_at TEXT NOT NULL
                )
                '''
            )
            self._populate_default_education_levels()
            user_columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(users)').fetchall()}
            if 'pwd_ver' not in user_columns:
                self.connection.execute('ALTER TABLE users ADD COLUMN pwd_ver INTEGER NOT NULL DEFAULT 0')
            if 'profile_data' not in user_columns:
                self.connection.execute("ALTER TABLE users ADD COLUMN profile_data TEXT NOT NULL DEFAULT '{}'")
            if 'name_aliases' not in user_columns:
                self.connection.execute("ALTER TABLE users ADD COLUMN name_aliases TEXT NOT NULL DEFAULT '[]'")
            # Присутствие пользователей (№17): успешный вход + активность.
            if 'last_login_at' not in user_columns:
                self.connection.execute('ALTER TABLE users ADD COLUMN last_login_at TEXT')
            if 'last_seen_at' not in user_columns:
                self.connection.execute('ALTER TABLE users ADD COLUMN last_seen_at TEXT')
            # Student Identity v1 (Phase 1): стабильная ссылка аккаунта на
            # карточку студента; существующие учётки НЕ мигрируются (NULL).
            if 'student_ref_id' not in user_columns:
                self.connection.execute('ALTER TABLE users ADD COLUMN student_ref_id INTEGER')
            # Event Model, Wave 1 P0 (целевая архитектура): app_settings —
            # флаги миграций. Seed identity_mode='dual' готовил Phase 3
            # dual-read; с P3 Runtime Identity флаг читается на каждый запрос
            # (get_identity_mode; нет строки/неизвестное значение → 'dual').
            # Паттерн seeding — как _populate_field_settings_defaults:
            # PRIMARY KEY + INSERT OR IGNORE, повторные старты не дублируют.
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                '''
            )
            self.connection.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('identity_mode', 'dual')")
            # Индексы ссылок participation-identity (Wave 1 P0): обычные,
            # НЕ UNIQUE — уникальность связи управляется приложением.
            # Колонки гарантированно существуют: _migrate_competitions_columns
            # выше уже добавил недостающие.
            self.connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_competitions_calendar_event_id ON competitions (calendar_event_id)'
            )
            self.connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_competitions_student_ref_id ON competitions (student_ref_id)'
            )
            self._backfill_competition_calendar_links()
            self.connection.commit()

    def _migrate_competitions_columns(self, columns: set[str]) -> None:
        """Порционная миграция легаси-таблицы записей: недостающие колонки
        добавляются ALTER'ом, существующие данные не трогаются."""
        if 'extra_data' not in columns:
            self.connection.execute("ALTER TABLE competitions ADD COLUMN extra_data TEXT NOT NULL DEFAULT '{}'")
        if 'review_status' not in columns:
            self.connection.execute(
                "ALTER TABLE competitions ADD COLUMN review_status TEXT NOT NULL DEFAULT 'approved'"
            )
        if 'owner_id' not in columns:
            self.connection.execute('ALTER TABLE competitions ADD COLUMN owner_id INTEGER')
        if 'review_comment' not in columns:
            self.connection.execute("ALTER TABLE competitions ADD COLUMN review_comment TEXT NOT NULL DEFAULT ''")
        # Даты-диапазоны (решение 2026-09-13): date_to NULL = однодневное,
        # существующие записи не трогаются — колонка рядом с date (НАЧАЛО).
        if 'date_to' not in columns:
            self.connection.execute('ALTER TABLE competitions ADD COLUMN date_to TEXT')
        # Student Identity v1, Phase 1 (фундамент): стабильная ссылка записи
        # на карточку студента. Существующие записи НЕ мигрируются — колонка
        # остаётся NULL, легаси-ключ student_id (sha256 ФИО) не меняется.
        if 'student_ref_id' not in columns:
            self.connection.execute('ALTER TABLE competitions ADD COLUMN student_ref_id INTEGER')
        # Event Model, Wave 1 P0 (целевая архитектура): дисциплина, результат
        # и явная ссылка на событие календаря — часть participation-identity
        # будущих фаз (calendar_event_id, student_ref_id, discipline).
        # Аддитивно: существующие строки NULL, runtime поля не читает.
        if 'discipline' not in columns:
            self.connection.execute('ALTER TABLE competitions ADD COLUMN discipline TEXT')
        if 'result' not in columns:
            self.connection.execute('ALTER TABLE competitions ADD COLUMN result TEXT')
        if 'calendar_event_id' not in columns:
            self.connection.execute('ALTER TABLE competitions ADD COLUMN calendar_event_id INTEGER')
        self._migrate_competitions_course_columns(columns)

    def _migrate_competitions_course_columns(self, columns: set[str]) -> None:
        """Course / Education, Phase A: замороженный снимок года поступления
        участия. Пишется при создании записи из учебных данных группы
        (строгое разрешение пары институт+группа) и backfill'ем лет; обычная
        правка записи НЕ меняет и НЕ дополняет его (историчность).
        Существующие строки остаются NULL — легаси-колонка course
        продолжает работать как фолбэк отображения."""
        if 'admission_year' not in columns:
            self.connection.execute('ALTER TABLE competitions ADD COLUMN admission_year INTEGER')

    def _migrate_custom_fields_columns(self):
        """№24 (docs/feedback-live.md): колонка link_target у кастомных полей.

        Идемпотентно: добавляется только если отсутствует; существующие
        значения (NULL) означают «показывать отдельной колонкой» — данные
        ссылок не трогаются.
        """
        columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(custom_fields)').fetchall()}
        if columns and 'link_target' not in columns:
            self.connection.execute('ALTER TABLE custom_fields ADD COLUMN link_target TEXT')
            self.connection.commit()

    def _migrate_catalog_values_parent(self):
        """Перестроить легаси-каталог без parent_id (и с табличным UNIQUE).

        Табличный UNIQUE (category, value) не даёт одноимённым группам разных
        институтов сосуществовать, поэтому таблица пересоздаётся: данные
        копируются как есть (плоские значения с parent_id NULL), старая
        удаляется. Идемпотентно: новая схема определяется по колонке parent_id.
        """
        columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(catalog_values)').fetchall()}
        if not columns or 'parent_id' in columns:
            return
        self.connection.execute('ALTER TABLE catalog_values RENAME TO catalog_values_legacy')
        self.connection.execute(
            '''
            CREATE TABLE catalog_values (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                value TEXT NOT NULL,
                parent_id INTEGER REFERENCES catalog_values(id),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            '''
        )
        self.connection.execute(
            '''
            INSERT INTO catalog_values (id, category, value, parent_id, active, created_at)
            SELECT id, category, value, NULL, active, created_at
            FROM catalog_values_legacy
            '''
        )
        self.connection.execute('DROP TABLE catalog_values_legacy')

    def _populate_catalog_values(self):
        """Наполнить справочники уникальными значениями из существующих записей.

        Идемпотентно: благодаря уникальному индексу плоских значений повторная
        инициализация не дублирует строки и не трогает скрытые. Пустые значения
        пропускаются. В вырожденных легаси-базах без колонок sport/institute
        populate молчит.
        """
        columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(competitions)').fetchall()}
        if not set(CATALOG_CATEGORIES) <= columns:
            return
        created_at = datetime.utcnow().isoformat()
        for category in CATALOG_CATEGORIES:
            self.connection.execute(
                f'''
                INSERT OR IGNORE INTO catalog_values (category, value, active, created_at)
                SELECT DISTINCT '{category}', {category}, 1, ?
                FROM competitions
                WHERE TRIM({category}) != ''
                ''',
                (created_at,),
            )

    def _populate_catalog_hierarchy_pairs(self):
        """Наполнить иерархию парами институт→группа из существующих записей.

        Запускается после populate одиночных значений, поэтому институты из
        записей уже существуют. Идемпотентно: уникальность (parent, value)
        держит дочерний частичный индекс, скрытые группы не «оживляются».
        Пары с пустым институтом или группой не создаются (без родителя
        иерархическая запись не имеет смысла). В легаси-базах без колонок
        institute/group populate молчит.
        """
        columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(competitions)').fetchall()}
        if not {'institute', 'group'} <= columns:
            return
        self.connection.execute(
            '''
            INSERT OR IGNORE INTO catalog_values (category, value, parent_id, active, created_at)
            SELECT 'group', competitions."group", institutes.id, 1, ?
            FROM competitions
            JOIN catalog_values institutes
                ON institutes.category = 'institute'
                AND institutes.parent_id IS NULL
                AND institutes.value = competitions.institute
            WHERE TRIM(competitions.institute) != '' AND TRIM(competitions."group") != ''
            ''',
            (datetime.utcnow().isoformat(),),
        )

    def _populate_field_settings_defaults(self):
        """Наполнить field_settings дефолтами, отражающими сегодняшнее поведение.

        Идемпотентно: PRIMARY KEY + INSERT OR IGNORE — настройки, изменённые
        админом, повторной инициализацией не сбрасываются. Дефолты (решение
        2026-09-13): числовые — Место и Курс, обязательные — ФИО, Дата, Курс
        (как в текущей валидации build_competition/normalize_*).
        """
        for key, (value_type, required) in BASE_FIELD_SETTING_DEFAULTS.items():
            self.connection.execute(
                'INSERT OR IGNORE INTO field_settings (key, value_type, required) VALUES (?, ?, ?)',
                (key, value_type, int(required)),
            )

    @staticmethod
    def _row_to_competition(row: sqlite3.Row) -> Competition:
        return Competition.model_validate(
            {
                '_id': str(row['id']),
                'Код студента': row['student_id'],
                'ФИО': row['student_name'],
                'Пол': row['student_sex'],
                'Институт': row['institute'],
                'Группа': row['group'],
                'Курс': row['course'],
                'Вид спорта': row['sport'],
                'Дата': row['date'],
                'Дата по': row['date_to'],
                'Уровень соревнований': row['level'],
                'Название соревнований': row['name'],
                'Место': row['position'],
                'Время создания записи (UTC)': row['created_at'],
                'extra_data': json.loads(row['extra_data'] or '{}'),
                'Статус проверки': row['review_status'],
                'Комментарий проверки': row['review_comment'],
                'student_ref_id': row['student_ref_id'],
                'discipline': row['discipline'],
                'result': row['result'],
                'calendar_event_id': row['calendar_event_id'],
                'admission_year': row['admission_year'],
            }
        )

    @staticmethod
    def _row_to_custom_field(row: sqlite3.Row) -> CustomField:
        return CustomField(
            field_id=row['id'],
            key=row['key'],
            label=row['label'],
            field_type=row['field_type'],
            required=bool(row['required']),
            show_in_table=bool(row['show_in_table']),
            show_in_export=bool(row['show_in_export']),
            show_in_template=bool(row['show_in_template']),
            sort_order=row['sort_order'],
            active=bool(row['active']),
            link_target=row['link_target'],
        )

    def vacuum(self) -> None:
        """Перестроить файл БД, чтобы освободить место после массовых удалений.

        Только для редких админских операций (очистка): VACUUM требует
        завершённых транзакций и единственного писателя.
        """
        with self._lock:
            self.connection.commit()
            self.connection.execute('VACUUM')

    def _backfill_competition_calendar_links(self) -> dict[str, int]:
        """Проставить calendar_event_id записям с однозначным пресетом.

        Event Model, Wave 1 P0: единственная автоматическая связь
        «запись → событие». Предикат — ровно семантика
        _calendar_preset_match_sql (name + date + COALESCE(date_to, '')):
        в UPDATE колонки записи пишутся полными именами
        (competitions.<col>), алиас записи — имя таблицы.

        Правила: линкуются ТОЛЬКО строки с calendar_event_id IS NULL,
        у которых пресет совпадает ровно с одним событием (unique-only,
        no-guess); уже связанные строки не трогаются. Один UPDATE
        коррелированными подзапросами (без UPDATE...FROM), выполняется
        внутри транзакции вызывающей стороны (вызов из _create_schema
        до commit).

        Возвращает счётчики: considered (NULL-строки до UPDATE), matched
        (rowcount), unmatched (остаток = NULL без единственного события),
        ambiguous (NULL-строки с более чем одним совпадением),
        already_linked (строки со ссылкой до UPDATE).

        В вырожденных легаси-базах без колонок name/date (как populate
        справочников) backfill молчит и возвращает нули.
        """
        columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(competitions)').fetchall()}
        if not {'name', 'date', 'date_to', 'calendar_event_id'} <= columns:
            return {
                'considered': 0,
                'matched': 0,
                'unmatched': 0,
                'ambiguous': 0,
                'already_linked': 0,
            }
        # Синхронно с _calendar_preset_match_sql: тот же предикат,
        # алиас записи — полное имя таблицы competitions.
        predicate = self._calendar_preset_match_sql('competitions')
        considered = int(
            self.connection.execute(
                'SELECT COUNT(*) AS total FROM competitions WHERE calendar_event_id IS NULL'
            ).fetchone()['total']
        )
        already_linked = int(
            self.connection.execute(
                'SELECT COUNT(*) AS total FROM competitions WHERE calendar_event_id IS NOT NULL'
            ).fetchone()['total']
        )
        ambiguous = int(
            self.connection.execute(
                f'''
                SELECT COUNT(*) AS total FROM competitions
                WHERE calendar_event_id IS NULL
                  AND (SELECT COUNT(*) FROM calendar_events e WHERE {predicate}) > 1
                '''
            ).fetchone()['total']
        )
        cursor = self.connection.execute(
            f'''
            UPDATE competitions
            SET calendar_event_id = (
                SELECT e.id FROM calendar_events e WHERE {predicate}
            )
            WHERE calendar_event_id IS NULL
              AND (SELECT COUNT(*) FROM calendar_events e WHERE {predicate}) = 1
            '''
        )
        matched = cursor.rowcount
        counters = {
            'considered': considered,
            'matched': matched,
            'unmatched': considered - matched - ambiguous,
            'ambiguous': ambiguous,
            'already_linked': already_linked,
        }
        # Молчание на свежей БД и повторных стартах (considered = 0):
        # INFO-строка только когда было что линковать.
        if considered > 0:
            logger.info(
                'calendar backfill: matched=%s unmatched=%s ambiguous=%s already_linked=%s',
                matched,
                counters['unmatched'],
                ambiguous,
                already_linked,
            )
        return counters
