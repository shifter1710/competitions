import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Collection
from typing import Iterable
from typing import Sequence

from src.models.competition import Competition
from src.models.custom_field import CustomField
from src.models.http.report_row import ReportSliceRow
from src.models.http.student_info import StudentInfo
from src.settings import settings
from src.storage.catalogs import CatalogsMixin
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
from src.storage.helpers import IDENTITY_MODE_DEFAULT  # noqa: F401
from src.storage.helpers import IDENTITY_MODES  # noqa: F401
from src.storage.helpers import participation_content_key  # noqa: F401
from src.storage.helpers import RENAME_CATEGORIES  # noqa: F401
from src.storage.helpers import REPORT_GROUPINGS  # noqa: F401
from src.storage.helpers import REPORT_METRIC_SELECTS  # noqa: F401
from src.storage.helpers import STUDENT_SEX_VALUES  # noqa: F401
from src.storage.helpers import STUDENT_SORT_COLUMNS  # noqa: F401
from src.storage.misc import MiscMixin
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
# StudentsMixin (карточки студентов).
# Порядок баз — только читаемость, перекрытий имён между примесями нет.
# Контракт примеси: не создаёт соединение и блокировку (self.connection /
# self._lock принадлежат SQLiteAdapter), не импортирует соседние доменные
# модули src.storage.* (кроме src.storage.helpers), междоменные вызовы —
# только через self. Тесты патчат методы на самом SQLiteAdapter — патч
# ложится раньше примесей в MRO и перехватывает вызовы через self.
class SQLiteAdapter(MiscMixin, CatalogsMixin, UsersMixin, StudentsMixin):
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

    def get_competition_by_id(self, record_id: int) -> Competition | None:
        """Одна запись по id — правая сторона разбора конфликта импорта."""
        with self._lock:
            row = self.connection.execute(
                f'{COMPETITION_SELECT_SQL}WHERE id = ?',
                (record_id,),
            ).fetchone()
            return self._row_to_competition(row) if row else None

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

    def get_competitions(
        self,
        owner_id: int | None = None,
        student_id_hashes: Sequence[str] = (),
        student_ref_id: int | None = None,
        identity_mode: str = IDENTITY_MODE_DEFAULT,
    ) -> Iterable[Competition]:
        with self._lock:
            scope_clauses, scope_params = self._competition_scope_clauses(
                owner_id, student_id_hashes, student_ref_id=student_ref_id, identity_mode=identity_mode
            )
            where_clause = f'WHERE {" AND ".join(scope_clauses)}\n' if scope_clauses else ''
            query = f'{COMPETITION_SELECT_SQL}{where_clause}ORDER BY date ASC, created_at ASC'
            rows = self.connection.execute(query, scope_params).fetchall()
            return [self._row_to_competition(row) for row in rows]

    @staticmethod
    def _competition_scope_clauses(
        owner_id: int | None,
        student_id_hashes: Sequence[str],
        student_ref_id: int | None = None,
        identity_mode: str = IDENTITY_MODE_DEFAULT,
    ) -> tuple[list[str], list[object]]:
        """Видимость записей (кабинет атлета, P3 dual-read): свои по owner_id,
        по стабильной связи student_ref_id и — в dual-режиме — по хешам
        привязанных ФИО (легаси). В ref-режиме легаси-ветка не применяется.
        Как раньше: без owner_id ограничение не применяется — модератор
        видит весь реестр."""
        if owner_id is None:
            return [], []
        clauses = ['owner_id = ?']
        params: list[object] = [owner_id]
        if student_ref_id is not None:
            clauses.append('student_ref_id = ?')
            params.append(student_ref_id)
        if identity_mode != 'ref' and student_id_hashes:
            placeholders = ', '.join('?' for _ in student_id_hashes)
            clauses.append(f'student_id IN ({placeholders})')
            params.extend(student_id_hashes)
        return ['(' + ' OR '.join(clauses) + ')'], params

    def _competition_filter_clauses(
        self,
        *,
        owner_id: int | None,
        student_id_hashes: Sequence[str] = (),
        student_ref_id: int | None = None,
        identity_mode: str = IDENTITY_MODE_DEFAULT,
        name: str = '',
        institute: str = '',
        sport: str = '',
        level: str = '',
        review_status: str = '',
        unapproved_only: bool = False,
        date_from: str = '',
        date_to: str = '',
    ) -> tuple[str, list[object]]:
        """Общий WHERE серверных фильтров главной (прототип 02): видимость
        (P3 dual-read: owner / student_ref_id / легаси-хеши) +
        ФИО (подстрока), институт/вид спорта/уровень (точное совпадение),
        статус проверки и период дат (дд.мм.гггг, как в фильтрах отчёта)."""
        clauses, params = self._competition_scope_clauses(
            owner_id, student_id_hashes, student_ref_id=student_ref_id, identity_mode=identity_mode
        )

        if name:
            clauses.append('student_name LIKE ?')
            params.append(f'%{name}%')
        if institute:
            clauses.append('institute = ?')
            params.append(institute)
        if sport:
            clauses.append('sport = ?')
            params.append(sport)
        if level:
            clauses.append('level = ?')
            params.append(level)
        if review_status:
            clauses.append('review_status = ?')
            params.append(review_status)
        if unapproved_only:
            # Статусы у всех записей, кроме подтверждённых (для колонки «Статус»).
            clauses.append("review_status != 'approved'")

        date_clauses, date_params = self._date_range_clauses(date_from, date_to)
        clauses.extend(date_clauses)
        params.extend(date_params)

        where_clause = f'WHERE {" AND ".join(clauses)}' if clauses else ''
        return where_clause, params

    def count_competitions_filtered(
        self,
        *,
        owner_id: int | None = None,
        student_id_hashes: Sequence[str] = (),
        student_ref_id: int | None = None,
        identity_mode: str = IDENTITY_MODE_DEFAULT,
        name: str = '',
        institute: str = '',
        sport: str = '',
        level: str = '',
        review_status: str = '',
        unapproved_only: bool = False,
        date_from: str = '',
        date_to: str = '',
    ) -> int:
        """Сколько записей видно пользователю с учётом серверных фильтров.

        Счётчик «Показано N из M» главной: M — с фильтром, без пагинации.
        unapproved_only — признак «есть не подтверждённые» (видимость колонки
        «Статус» не должна зависеть от текущей страницы/фильтра).
        """
        with self._lock:
            where_clause, params = self._competition_filter_clauses(
                owner_id=owner_id,
                student_id_hashes=student_id_hashes,
                student_ref_id=student_ref_id,
                identity_mode=identity_mode,
                name=name,
                institute=institute,
                sport=sport,
                level=level,
                review_status=review_status,
                unapproved_only=unapproved_only,
                date_from=date_from,
                date_to=date_to,
            )
            row = self.connection.execute(
                f'SELECT COUNT(*) FROM competitions {where_clause}',
                params,
            ).fetchone()
            return int(row[0])

    def get_competitions_page(
        self,
        *,
        owner_id: int | None = None,
        student_id_hashes: Sequence[str] = (),
        student_ref_id: int | None = None,
        identity_mode: str = IDENTITY_MODE_DEFAULT,
        name: str = '',
        institute: str = '',
        sport: str = '',
        level: str = '',
        review_status: str = '',
        date_from: str = '',
        date_to: str = '',
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Competition]:
        """Страница реестра с серверной фильтрацией (прототип 02).

        Те же фильтры, что у count_competitions_filtered; сортировка и
        порядок как в get_competitions (created_at ASC). limit/offset —
        пагинация главной, None возвращает всё (совпадение с get_competitions).
        """
        with self._lock:
            where_clause, params = self._competition_filter_clauses(
                owner_id=owner_id,
                student_id_hashes=student_id_hashes,
                student_ref_id=student_ref_id,
                identity_mode=identity_mode,
                name=name,
                institute=institute,
                sport=sport,
                level=level,
                review_status=review_status,
                date_from=date_from,
                date_to=date_to,
            )
            query = f'{COMPETITION_SELECT_SQL}{where_clause}\nORDER BY date ASC, created_at ASC'
            query_params = list(params)
            if limit is not None:
                query += '\nLIMIT ? OFFSET ?'
                query_params.extend([limit, offset])
            rows = self.connection.execute(query, query_params).fetchall()
            return [self._row_to_competition(row) for row in rows]

    @staticmethod
    def _build_custom_filter_clauses(
        custom_filters: Iterable[tuple[str, str, str]],
    ) -> tuple[list[str], list[object]]:
        clauses = []
        params: list[object] = []
        for key, field_type, value in custom_filters:
            column = 'json_extract(extra_data, \'$."' + key + '"\')'
            if field_type == 'number':
                clauses.append(f'CAST({column} AS INTEGER) = ?')
                params.append(int(value))
            elif field_type == 'date':
                clauses.append(column + ' = ?')
                params.append(value)
            else:
                clauses.append(f'{column} LIKE ?')
                params.append(f'%{value}%')
        return clauses, params

    def get_filtered(
        self,
        date_from: str = '',
        date_to: str = '',
        position: str = '',
        level: str = '',
        name: str = '',
        custom_filters: Iterable[tuple[str, str, str]] = (),
        *,
        institute: str = '',
        group: str = '',
        sport: str = '',
    ) -> list[StudentInfo]:
        """Отчёт по срезу «студент»: группировка по данным записи + метрики."""
        rows = self._fetch_report_rows(
            DEFAULT_REPORT_GROUPING,
            date_from=date_from,
            date_to=date_to,
            position=position,
            level=level,
            name=name,
            institute=institute,
            group=group,
            sport=sport,
            custom_filters=custom_filters,
        )
        return [
            StudentInfo(
                student_id=row['student_id'],
                student_name=row['student_name'],
                student_sex=row['student_sex'],
                institute=row['institute'],
                group=row['group'],
                course=row['course'],
                count_participation=row['count_participation'],
                count_wins=row['count_wins'],
                count_prizes=row['count_prizes'],
            )
            for row in rows
        ]

    def get_grouped_report(
        self,
        group_by: str,
        *,
        date_from: str = '',
        date_to: str = '',
        position: str = '',
        level: str = '',
        name: str = '',
        institute: str = '',
        group: str = '',
        sport: str = '',
        custom_filters: Iterable[tuple[str, str, str]] = (),
    ) -> list[ReportSliceRow]:
        """Отчёт по произвольному срезу: значение поля записи + метрики.

        group_by — ключ REPORT_GROUPINGS, кроме 'student' (у него другая
        форма строки, см. get_filtered). Фильтры те же, что у get_filtered.
        """
        if group_by == DEFAULT_REPORT_GROUPING:
            raise ValueError('Срез «студент» обрабатывает get_filtered')
        if group_by not in REPORT_GROUPINGS:
            raise ValueError(f'Неизвестный срез отчёта: {group_by}')
        rows = self._fetch_report_rows(
            group_by,
            date_from=date_from,
            date_to=date_to,
            position=position,
            level=level,
            name=name,
            institute=institute,
            group=group,
            sport=sport,
            custom_filters=custom_filters,
        )
        return [
            ReportSliceRow(
                slice_value=row['slice_value'],
                count_participation=row['count_participation'],
                count_wins=row['count_wins'],
                count_prizes=row['count_prizes'],
            )
            for row in rows
        ]

    @staticmethod
    def _date_range_clauses(date_from: str, date_to: str) -> tuple[list[str], list[object]]:
        """Условия периода отчёта: границы парсятся форматом settings.date_format.

        Даты-диапазоны (решение владельца 2026-09-13, docs/data-model-
        decisions.md): запись показывается, если ХОТЯ БЫ ОДНА из её дат
        (date_from ИЛИ date_to) попала в диапазон фильтра. Записи без
        date_to ведут себя как раньше — их единственная дата = date.
        """
        clauses = []
        params: list[object] = []
        if date_from:
            bound = datetime.strptime(date_from, settings.date_format).isoformat()
            clauses.append('(date >= ? OR COALESCE(date_to, date) >= ?)')
            params.extend([bound, bound])
        if date_to:
            bound = datetime.strptime(date_to, settings.date_format).isoformat()
            clauses.append('(date <= ? OR COALESCE(date_to, date) <= ?)')
            params.extend([bound, bound])
        return clauses, params

    def _report_filter_clauses(
        self,
        *,
        date_from: str,
        date_to: str,
        position: str,
        level: str,
        name: str,
        institute: str,
        group: str,
        sport: str,
        custom_filters: Iterable[tuple[str, str, str]],
    ) -> tuple[str, list[object]]:
        """Общий WHERE всех фильтров отчёта: период, место, уровень, ФИО,
        институт, группа, вид спорта, кастомные поля. Только approved."""
        filters = ["review_status = 'approved'"]
        params: list[object] = []

        date_clauses, date_params = self._date_range_clauses(date_from, date_to)
        filters.extend(date_clauses)
        params.extend(date_params)

        if position:
            sign = position[0]
            value = int(position[1:])
            if sign == '>':
                filters.append('position > ?')
                params.append(value)
            elif sign == '<':
                filters.append('position < ?')
                params.append(value)

        if level:
            filters.append('level = ?')
            params.append(level)

        if name:
            filters.append('student_name LIKE ?')
            params.append(f'%{name}%')

        if institute:
            filters.append('institute = ?')
            params.append(institute)

        if group:
            filters.append('"group" = ?')
            params.append(group)

        if sport:
            filters.append('sport = ?')
            params.append(sport)

        custom_clauses, custom_params = self._build_custom_filter_clauses(custom_filters)
        filters.extend(custom_clauses)
        params.extend(custom_params)

        where_clause = f'WHERE {" AND ".join(filters)}' if filters else ''
        return where_clause, params

    def _fetch_report_rows(
        self,
        group_by: str,
        *,
        date_from: str,
        date_to: str,
        position: str,
        level: str,
        name: str,
        institute: str,
        group: str,
        sport: str,
        custom_filters: Iterable[tuple[str, str, str]],
    ) -> list[sqlite3.Row]:
        """Агрегирующий запрос отчёта: GROUP BY по данным записи + метрики.

        Один WHERE для всех фильтров отчёта (см. _report_filter_clauses) и
        метрики SUM CASE — SQL считает всё сам, питон по строкам не ходит.
        """
        with self._lock:
            group_columns = REPORT_GROUPINGS[group_by]
            where_clause, params = self._report_filter_clauses(
                date_from=date_from,
                date_to=date_to,
                position=position,
                level=level,
                name=name,
                institute=institute,
                group=group,
                sport=sport,
                custom_filters=custom_filters,
            )
            if group_by == DEFAULT_REPORT_GROUPING:
                select_columns = ',\n'.join(group_columns)
                order_by = 'count_participation ASC, student_name ASC, institute ASC, "group" ASC, course ASC'
            else:
                select_columns = f'{group_columns[0]} AS slice_value'
                order_by = 'count_participation ASC, slice_value ASC'
            rows = self.connection.execute(
                f'''
                SELECT
                    {select_columns},
                    {', '.join(REPORT_METRIC_SELECTS)}
                FROM competitions
                {where_clause}
                GROUP BY
                    {', '.join(group_columns)}
                ORDER BY {order_by}
                ''',
                params,
            ).fetchall()
            return rows

    def save_competitions(
        self,
        competitions: Iterable[Competition],
        review_status: str = 'approved',
        owner_id: int | None = None,
    ):
        with self._lock:
            records = competition_insert_records(competitions, review_status, owner_id)
            self.connection.executemany(COMPETITION_INSERT_SQL, records)
            self.connection.commit()

    def import_competitions(
        self,
        new_competitions: Sequence[Competition],
        *,
        review_status: str = 'approved',
        owner_id: int | None = None,
    ) -> None:
        """Импорт одной транзакцией с одним COMMIT (находка QA №1).

        Записи вставляются, затем справочники (уровни, виды спорта, институты,
        пары институт→группа) пополняются значениями ИЗ ЗАПИСЕЙ — решение
        2026-09-13 (docs/data-model-decisions.md «Переименование значений
        справочников»): раньше справочник сеялся строками файла, включая
        пропущенные дубли, поэтому переименованное с обновлением записей
        значение возвращалось в справочник следующим импортом того же файла.
        Теперь источник — таблица записей: значение возвращается, только если
        оно реально есть в записях. Сбой на любом шаге — откат всего импорта:
        либо все строки, либо ничего (вместе со справочниками).

        `new_competitions` — то, что реально вставляется; дубли отсеивает
        вызывающая сторона до транзакции (split_import_competitions).
        """
        with self._lock:
            try:
                if new_competitions:
                    records = competition_insert_records(new_competitions, review_status, owner_id)
                    self.connection.executemany(COMPETITION_INSERT_SQL, records)
                self._sync_catalogs_from_records()
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def _event_participation_recheck_context(self, event_id: int) -> tuple[set, set, set]:
        """Живые ключи уникальности участий события (P4): (identity-ключи,
        контент-ключи, ключи коллидирующих кастомов). Вызывается ПОД _lock
        внутри батча — повторная проверка видит актуальное состояние, а не
        снимок предпросмотра.

        identity — (карточка, дисциплина) связанного участия; контент — полный
        ключ participation_content_key. Дисциплина/результат существующего
        участия читаются с legacy-фолбэком (P5a dual-write): NULL в базовой
        колонке значит «взять значение активного кастома с коллидирующим
        label из extra_data» — старые записи до P4 писали только туда.
        Ключи коллидирующих кастомов исключаются из customs-части контент-ключа
        у ОБЕИХ сторон сравнения (QA D2: эффективная дисциплина легаси-строки
        в extra_data, вставляемой — в базовой колонке)."""
        collision_labels = ('дисциплина', 'результат')
        collision_keys = [
            (field.key, field.label.strip().casefold())
            for field in self.get_custom_fields()
            if field.label.strip().casefold() in collision_labels
        ]
        collision_custom_keys = {key for key, _ in collision_keys}
        identities: set = set()
        contents: set = set()

        def effective(base, extra, folded_label: str) -> str:
            value = str(base or '').strip()
            if value:
                return value
            for key, label in collision_keys:
                if label == folded_label:
                    fallback = str(extra.get(key) or '').strip()
                    if fallback:
                        return fallback
            return ''

        for participant in self.list_calendar_event_participants(event_id):
            extra = participant.get('extra_data') or {}
            discipline = effective(participant.get('discipline'), extra, 'дисциплина')
            result = effective(participant.get('result'), extra, 'результат')
            if participant.get('student_ref_id') is not None:
                identities.add(('ref', participant['student_ref_id'], dedup_discipline(discipline)))
            contents.add(
                participation_content_key(
                    participant['student_name'],
                    discipline,
                    participant['student_sex'],
                    participant['institute'],
                    participant['group_name'],
                    participant['course'],
                    participant['position'],
                    result,
                    extra,
                    collision_custom_keys,
                )
            )
        return identities, contents, collision_custom_keys

    @staticmethod
    def _event_participation_batch_guard(
        inserts: Sequence[Competition],
        live_identities: set,
        live_contents: set,
        exclude_custom_keys: Collection[str] = (),
    ) -> None:
        """Повторная проверка insert-строк батча участий (P4) против живой БД
        и самого батча (intra-batch first-wins). Поднимает
        EventParticipationConflictError при коллизии: MATCHED — identity
        (карточка + дисциплина); CARDLESS против живой БД — точный контент,
        внутри батча — контент-ключ (ФИО + дисциплина): оба участия одного
        файла с тем же ключом сервер не вставляет (предпросмотр показывает
        их конфликтом строк). exclude_custom_keys — симметричное исключение
        коллидирующих кастомов из customs-части контент-ключа (QA D2,
        см. participation_content_key)."""
        batch_identities: set = set()
        batch_contents: set = set()
        batch_cardless_keys: set = set()
        for competition in inserts:
            content = participation_content_key(
                competition.student_name,
                competition.discipline,
                competition.student_sex,
                competition.institute,
                competition.group,
                competition.course,
                competition.position,
                competition.result,
                competition.extra_data,
                exclude_custom_keys,
            )
            if competition.student_ref_id is not None:
                identity = ('ref', competition.student_ref_id, dedup_discipline(competition.discipline))
                if identity in live_identities or identity in batch_identities:
                    raise EventParticipationConflictError('participation identity already exists')
                batch_identities.add(identity)
            else:
                cardless_key = (dedup_text(competition.student_name), dedup_discipline(competition.discipline))
                if content in live_contents or content in batch_contents:
                    raise EventParticipationConflictError('identical cardless participation already exists')
                if cardless_key in batch_cardless_keys:
                    raise EventParticipationConflictError('cardless participation key already in batch')
                batch_cardless_keys.add(cardless_key)
            batch_contents.add(content)

    def apply_event_participation_batch(
        self,
        event_id: int,
        inserts: Sequence[Competition],
        updates: Sequence[tuple[int, object, str | None]] | None = None,
        *,
        review_status: str = 'approved',
        owner_id: int | None = None,
    ) -> None:
        """Батч участий события одной транзакцией со СТРОГОЙ повторной
        проверкой уникальности (P4, паттерн import_competitions: вставки +
        синхронизация справочников из записей + один COMMIT; любой сбой —
        rollback всего батча вместе со справочниками).

        Повторная проверка под _lock (см. _event_participation_batch_guard)
        — коллизия означает, что предпросмотр устарел: сервер не позволяет
        создать оба повторных участия. `updates` — restricted-обновления
        (record_id, position, result) участий ЭТОГО события той же
        транзакцией."""
        with self._lock:
            try:
                live_identities, live_contents, collision_custom_keys = self._event_participation_recheck_context(
                    event_id
                )
                self._event_participation_batch_guard(inserts, live_identities, live_contents, collision_custom_keys)
                if inserts:
                    records = competition_insert_records(inserts, review_status, owner_id)
                    self.connection.executemany(COMPETITION_INSERT_SQL, records)
                for record_id, position, result in updates or ():
                    cursor = self.connection.execute(
                        'UPDATE competitions SET position = ?, result = ? WHERE id = ? AND calendar_event_id = ?',
                        (position, result, int(record_id), int(event_id)),
                    )
                    if not cursor.rowcount:
                        raise EventParticipationConflictError('updated participation not found in event')
                self._sync_catalogs_from_records()
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def update_event_participation_result(self, record_id: int, event_id: int, position, result: str | None) -> int:
        """P4: restricted-обновление места/результата существующего участия
        события — SET только position/result, WHERE id + calendar_event_id:
        снимок участника, custom-поля, вложения, student_ref_id и ссылка на
        событие физически недостижимы этим UPDATE. Возвращает число изменённых
        строк (0 — участие не найдено или принадлежит другому событию)."""
        with self._lock:
            cursor = self.connection.execute(
                'UPDATE competitions SET position = ?, result = ? WHERE id = ? AND calendar_event_id = ?',
                (position, result, int(record_id), int(event_id)),
            )
            self.connection.commit()
            return cursor.rowcount

    def _sync_catalogs_from_records(self) -> None:
        """Довести справочники до значений, реально присутствующих в записях.

        Тот же populate, что при инициализации БД: значения сеются SELECT'ом
        из competitions, а не из строк импортируемого файла. Поэтому после
        переименования значения с обновлением записей (rename_catalog_value)
        следующий импорт старое значение НЕ возвращает: в записях его больше
        нет — источника строки справочника нет. Идемпотентно: уникальные
        индексы и INSERT OR IGNORE не дублируют строки и не «оживляют» скрытые.
        """
        self._populate_catalog_values()
        self._populate_catalog_hierarchy_pairs()
        self.connection.execute(
            'INSERT OR IGNORE INTO levels (name, sort_order) '
            "SELECT DISTINCT level, 0 FROM competitions WHERE TRIM(level) != ''"
        )

    def update_competition(self, record_id: str, competition: Competition):
        # Wave 1 P1: discipline/result правятся общим update. calendar_event_id
        # в SET НЕ входит: ссылка на событие — управляемое поле будущих
        # link/unlink (P2), общий update записи её сохраняет.
        with self._lock:
            self.connection.execute(
                '''
                UPDATE competitions
                SET
                    student_id = ?,
                    student_name = ?,
                    student_sex = ?,
                    institute = ?,
                    "group" = ?,
                    course = ?,
                    sport = ?,
                    date = ?,
                    date_to = ?,
                    level = ?,
                    name = ?,
                    position = ?,
                    extra_data = ?,
                    discipline = ?,
                    result = ?
                WHERE id = ?
                ''',
                (
                    competition.student_id,
                    competition.student_name,
                    competition.student_sex,
                    competition.institute,
                    competition.group,
                    competition.course,
                    competition.sport,
                    competition.date.isoformat(),
                    competition.date_to.isoformat() if competition.date_to else None,
                    competition.level,
                    competition.name,
                    competition.position,
                    json.dumps(competition.extra_data, ensure_ascii=False),
                    competition.discipline,
                    competition.result,
                    int(record_id),
                ),
            )
            self.connection.commit()

    def delete_competition(self, record_id: str):
        with self._lock:
            self.connection.execute('DELETE FROM competitions WHERE id = ?', (int(record_id),))
            self.connection.commit()

    def delete_competition_with_attachments(self, record_id: int) -> int:
        """Удалить запись вместе с её вложениями (удаление записи админом).

        Одна транзакция под одной блокировкой: сначала строки вложений,
        потом сама запись. Возвращает число удалённых строк вложений.
        """
        with self._lock:
            cursor = self.connection.execute('DELETE FROM attachments WHERE record_id = ?', (int(record_id),))
            deleted_attachments = cursor.rowcount
            self.connection.execute('DELETE FROM competitions WHERE id = ?', (int(record_id),))
            self.connection.commit()
            return deleted_attachments

    def clean_db(self):
        with self._lock:
            self.connection.execute('DELETE FROM competitions')
            self.connection.commit()

    def count_competitions(self, date_before: datetime | None = None) -> int:
        """Число записей; с date_before — только записи с датой соревнования ДО неё."""
        with self._lock:
            if date_before is None:
                row = self.connection.execute('SELECT COUNT(*) AS total FROM competitions').fetchone()
            else:
                row = self.connection.execute(
                    'SELECT COUNT(*) AS total FROM competitions WHERE date < ?',
                    (date_before.isoformat(),),
                ).fetchone()
            return row['total']

    def get_competitions_before(self, date_before: datetime) -> list[Competition]:
        """Записи с датой соревнования ДО указанной (для архива перед очисткой).

        Wave 1 P1 + P3: служебные колонки (student_ref_id, discipline,
        result, calendar_event_id) нужны общему _row_to_competition;
        в сам xlsx-архив они НЕ попадают (competition_to_export_row
        вырезает) — ограничение pre-wipe архива см. docs/data-model.md.
        """
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT
                    id, student_id, student_name, student_sex, institute, "group", course,
                    sport, date, date_to, level, name, position, created_at, extra_data,
                    review_status, owner_id, review_comment, student_ref_id,
                    discipline, result, calendar_event_id
                FROM competitions
                WHERE date < ?
                ORDER BY created_at ASC
                ''',
                (date_before.isoformat(),),
            ).fetchall()
            return [self._row_to_competition(row) for row in rows]

    def delete_competitions_before(self, date_before: datetime) -> int:
        """Очистка по дате соревнования (docs/data-model-decisions.md). Возвращает число удалённых."""
        with self._lock:
            cursor = self.connection.execute('DELETE FROM competitions WHERE date < ?', (date_before.isoformat(),))
            self.connection.commit()
            return cursor.rowcount

    def delete_all_competitions(self) -> int:
        """Wipe all competition records (admin maintenance action). Returns deleted row count."""
        with self._lock:
            cursor = self.connection.execute('DELETE FROM competitions')
            self.connection.commit()
            return cursor.rowcount

    def vacuum(self) -> None:
        """Перестроить файл БД, чтобы освободить место после массовых удалений.

        Только для редких админских операций (очистка): VACUUM требует
        завершённых транзакций и единственного писателя.
        """
        with self._lock:
            self.connection.commit()
            self.connection.execute('VACUUM')

    def get_sport_names(self) -> list[str]:
        """Unique sport names as stored in records, sorted alphabetically."""
        with self._lock:
            rows = self.connection.execute('SELECT DISTINCT sport FROM competitions ORDER BY sport ASC').fetchall()
            return [row['sport'] for row in rows]

    def get_competition_review(self, record_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, review_status, owner_id, student_id, student_ref_id FROM competitions WHERE id = ?',
                (record_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_competition_student_name(self, record_id: int) -> str:
        """ФИО записи по id (для аудита удаления); нет записи — пустая строка."""
        with self._lock:
            row = self.connection.execute(
                'SELECT student_name FROM competitions WHERE id = ?',
                (record_id,),
            ).fetchone()
            return row['student_name'] if row else ''

    def set_competition_review(
        self,
        record_id: int,
        review_status: str,
        review_comment: str = '',
    ) -> None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE competitions
                SET review_status = ?, review_comment = ?
                WHERE id = ?
                """,
                (review_status, review_comment, record_id),
            )
            self.connection.commit()

    # ---- Календарь соревнований (волна A, docs/feedback-live.md №23) ----
    #
    # Событие календаря — план (пресет): название + период + уровень +
    # вид спорта + ссылка. Участники — обычные записи реестра, совпадающие
    # с пресетом по (name, date, date_to); записи хранят даты в ISO, пустой
    # date_to = однодневное (совпадение по COALESCE с обеих сторон).

    @staticmethod
    def _calendar_preset_match_sql(alias: str = 'c') -> str:
        return (
            f'({alias}.name = e.name'
            f' AND {alias}.date = e.date'
            f" AND COALESCE({alias}.date_to, '') = COALESCE(e.date_to, ''))"
        )

    def _calendar_events_starting_on(self, date) -> list:
        """Кандидаты guard'а дублей: события с той же датой начала — узкий
        предфильтр substr(date,1,10), финальное сравнение идёт ключом в
        Python. Вызывается под _lock (внутри check+write guard-методов)."""
        return self.connection.execute(
            'SELECT id, name, date, date_to, sport, level FROM calendar_events' ' WHERE substr(date, 1, 10) = ?',
            ((date or '')[:10],),
        ).fetchall()

    def _find_calendar_event_duplicate(self, name, date, date_to, sport, level, exclude_id=None) -> dict | None:
        """Точный дубль события по event_identity_key (под _lock): норма
        названия + обе даты (день, без времени) + sport/level. exclude_id
        выкидывает само событие — правка «на себя» дублем не считается."""
        key = event_identity_key(name, date, date_to, sport, level)
        for row in self._calendar_events_starting_on(date):
            if exclude_id is not None and row['id'] == exclude_id:
                continue
            row_key = event_identity_key(row['name'], row['date'], row['date_to'], row['sport'], row['level'])
            if row_key == key:
                return dict(row)
        return None

    def find_similar_calendar_event(self, name, date, date_to, sport, level, exclude_id=None) -> dict | None:
        """Похожее событие для НЕблокирующего предупреждения роута создания
        (2026-09-27): то же нормализованное название и дата начала, но
        ДРУГОЙ identity-ключ (отличаются date_to/sport/level) — вероятно,
        дубль, но гарантии нет, поэтому создание не блокируется. Только
        чтение (SQLite serialized + одно соединение — достаточно);
        возвращает первое найденное {id, name, date, date_to, sport, level}
        или None."""
        key = event_identity_key(name, date, date_to, sport, level)
        for row in self._calendar_events_starting_on(date):
            if exclude_id is not None and row['id'] == exclude_id:
                continue
            row_key = event_identity_key(row['name'], row['date'], row['date_to'], row['sport'], row['level'])
            if row_key[0] == key[0] and row_key != key:
                return dict(row)
        return None

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

    def create_calendar_event(
        self,
        name: str,
        date: str,
        date_to: str | None,
        level: str,
        sport: str,
        url: str,
    ) -> int:
        """Новое событие календаря с guard'ом точных дублей (2026-09-27).

        Проверка и INSERT — под одним _lock (атомарный check+write): событие
        с тем же event_identity_key уже есть → CalendarEventDuplicateError
        (existing — найденный дубль), запись не выполнялась, отката не
        нужно. Существующие дубли БД и повторные create того же события
        через raw SQL guard не трогает — блокируются только новые INSERT
        через этот метод."""
        with self._lock:
            duplicate = self._find_calendar_event_duplicate(name, date, date_to, sport, level)
            if duplicate is not None:
                raise CalendarEventDuplicateError(duplicate)
            cursor = self.connection.execute(
                'INSERT INTO calendar_events (name, date, date_to, level, sport, url, created_at)'
                ' VALUES (?, ?, ?, ?, ?, ?, ?)',
                (name, date, date_to, level, sport, url, datetime.utcnow().isoformat()),
            )
            self.connection.commit()
            return cursor.lastrowid

    def get_calendar_event(self, event_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, name, date, date_to, level, sport, url, created_at, '
                'regulation_filename, regulation_stored_name FROM calendar_events WHERE id = ?',
                (event_id,),
            ).fetchone()
            return dict(row) if row else None

    def set_calendar_regulation(self, event_id: int, filename: str, stored_name: str) -> None:
        """Прикрепить/заменить файл положения события (колонки + имя файла)."""
        with self._lock:
            self.connection.execute(
                'UPDATE calendar_events SET regulation_filename = ?, regulation_stored_name = ? WHERE id = ?',
                (filename, stored_name, event_id),
            )
            self.connection.commit()

    def clear_calendar_regulation(self, event_id: int) -> None:
        """Отвязать файл положения события (обе колонки в NULL)."""
        with self._lock:
            self.connection.execute(
                'UPDATE calendar_events SET regulation_filename = NULL, regulation_stored_name = NULL WHERE id = ?',
                (event_id,),
            )
            self.connection.commit()

    def update_calendar_event(
        self,
        event_id: int,
        name: str,
        date: str,
        date_to: str | None,
        level: str,
        sport: str,
        url: str,
    ) -> int:
        """Правка события + синхронизация связанных записей одной транзакцией.

        Event Model, Wave 1 P2: событие — владелец полей участия
        name/date/date_to/sport/level, правка пресета переносится в записи
        со ссылкой calendar_event_id (NULL-legacy-строки не трогаются —
        ссылки у них нет). Сбой любого шага — откат всего (паттерн
        import_competitions). Возвращает synced — число синхронизированных
        записей (rowcount второго UPDATE).

        Guard дублей (2026-09-27): правка НЕ на себя, совпадающая с чужим
        событием по event_identity_key → CalendarEventDuplicateError внутри
        try (существующий rollback-путь) — 0 изменений события и связанных
        записей, синхронизация не происходит.
        """
        with self._lock:
            try:
                duplicate = self._find_calendar_event_duplicate(name, date, date_to, sport, level, exclude_id=event_id)
                if duplicate is not None:
                    raise CalendarEventDuplicateError(duplicate)
                self.connection.execute(
                    'UPDATE calendar_events'
                    ' SET name = ?, date = ?, date_to = ?, level = ?, sport = ?, url = ?'
                    ' WHERE id = ?',
                    (name, date, date_to, level, sport, url, event_id),
                )
                cursor = self.connection.execute(
                    'UPDATE competitions'
                    ' SET name = ?, date = ?, date_to = ?, sport = ?, level = ?'
                    ' WHERE calendar_event_id = ?',
                    (name, date, date_to, sport, level, event_id),
                )
                synced = cursor.rowcount
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
            return synced

    def count_calendar_event_participants(self, event_id: int) -> int:
        """Блокировщик удаления события (консервативный OR, решение A).

        Считаются записи, связанные ссылкой calendar_event_id, ИЛИ
        совпадающие с пресетом (name + date + date_to): legacy-NULL-строки
        по пресету из участников страницы события уже исчезли (id-first
        reading), но молчаливое удаление события с записями-двойниками
        пресета хуже ложного блокирующего отказа — владелец решает случай
        вручную (unlink/перелинковка в будущих фазах).
        """
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM calendar_events e, competitions c'
                f' WHERE e.id = ? AND (c.calendar_event_id = e.id OR {self._calendar_preset_match_sql()})',
                (event_id,),
            ).fetchone()
            return row['total']

    def delete_calendar_event(self, event_id: int) -> None:
        with self._lock:
            self.connection.execute('DELETE FROM calendar_events WHERE id = ?', (event_id,))
            self.connection.commit()

    def list_calendar_events(self, sport: str = '') -> list[dict]:
        """Все события по хронологии; рядом — счётчики участников по ссылке
        calendar_event_id (P2, id-first; NULL-legacy-строки пресета не
        считаются): participant_count — все связанные записи реестра,
        no_result_count — из них с position = 0 («без результата»)."""
        with self._lock:
            params: list[object] = []
            where = ''
            if sport:
                where = 'WHERE e.sport = ?'
                params.append(sport)
            rows = self.connection.execute(
                f'''
                SELECT
                    e.id,
                    e.name,
                    e.date,
                    e.date_to,
                    e.level,
                    e.sport,
                    e.url,
                    e.created_at,
                    COUNT(c.id) AS participant_count,
                    SUM(CASE WHEN c.position = 0 THEN 1 ELSE 0 END) AS no_result_count
                FROM calendar_events e
                LEFT JOIN competitions c ON c.calendar_event_id = e.id
                {where}
                GROUP BY e.id
                ORDER BY e.date ASC, e.name ASC
                ''',
                params,
            ).fetchall()
            return [
                {
                    'id': row['id'],
                    'name': row['name'],
                    'date': row['date'],
                    'date_to': row['date_to'],
                    'level': row['level'],
                    'sport': row['sport'],
                    'url': row['url'],
                    'created_at': row['created_at'],
                    'participant_count': row['participant_count'] or 0,
                    'no_result_count': row['no_result_count'] or 0,
                }
                for row in rows
            ]

    def list_calendar_event_participants(self, event_id: int) -> list[dict]:
        """Записи реестра — участники события по ссылке calendar_event_id.

        Event Model, Wave 1 P2 (id-first): участники события — только явно
        связанные записи; NULL-legacy-строки, совпадающие с пресетом по
        случайности, на странице события не показываются (счётчик удаления
        при этом консервативно учитывает и их — OR, см.
        count_calendar_event_participants).
        Сначала с результатом, затем «ждут результата», внутри — по ФИО.
        student_ref_id — для предпросмотра импорта участников (поиск уже
        существующих участий той же карточки). P4: discipline/result/extra_data
        (распарсенный JSON) — аддитивно, для классификации повторов импорта
        (эффективные значения участия с legacy-фолбэком в extra_data).
        """
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT
                    c.id AS record_id,
                    c.student_name,
                    c.student_sex,
                    c.institute,
                    c."group" AS group_name,
                    c.course,
                    c.position,
                    c.student_ref_id,
                    c.discipline,
                    c.result,
                    c.extra_data
                FROM competitions c
                WHERE c.calendar_event_id = ?
                ORDER BY
                    CASE WHEN c.position = 0 THEN 1 ELSE 0 END,
                    c.student_name ASC
                ''',
                (event_id,),
            ).fetchall()
            return [{**dict(row), 'extra_data': json.loads(row['extra_data'] or '{}')} for row in rows]

            return [dict(row) for row in rows]

    # ---- P5b: явные связки «запись → событие» (Registry ↔ Calendar) ----
    #
    # Явное связывание существующей NULL-записи со событием календаря:
    # ссылка calendar_event_id + синхронизация 5 event-owned полей
    # (name/date/date_to/sport/level) — тот же состав, что у edit-sync
    # update_calendar_event. Правило гонок: guarded UPDATE
    # (WHERE calendar_event_id IS NULL) — запись, которую успел связать
    # кто-то другой, не перезаписывается (already_linked), существующая
    # ссылка не меняется.

    def link_participation_to_event(self, record_id: int, event_id: int) -> tuple[dict | None, str | None]:
        """Связать запись со существующим событием + синхронизация полей.

        Одна транзакция под _lock (паттерн update_calendar_event): сбой
        любого шага — откат всего. Возвращает (event, None) при успехе или
        (None, код ошибки): record_not_found / event_not_found /
        already_linked (запись уже связана — её ссылка и поля не тронуты).
        """
        with self._lock:
            try:
                record = self.connection.execute(
                    'SELECT id, calendar_event_id FROM competitions WHERE id = ?',
                    (record_id,),
                ).fetchone()
                if record is None:
                    return None, 'record_not_found'
                event = self.connection.execute(
                    'SELECT * FROM calendar_events WHERE id = ?',
                    (event_id,),
                ).fetchone()
                if event is None:
                    return None, 'event_not_found'
                cursor = self.connection.execute(
                    'UPDATE competitions'
                    ' SET calendar_event_id = ?, name = ?, date = ?, date_to = ?, sport = ?, level = ?'
                    ' WHERE id = ? AND calendar_event_id IS NULL',
                    (
                        event_id,
                        event['name'],
                        event['date'],
                        event['date_to'],
                        event['sport'],
                        event['level'],
                        record_id,
                    ),
                )
                if cursor.rowcount == 0:
                    # Между SELECT и UPDATE запись успел связать другой
                    # запрос — откатываем (SELECT читает снапшот неявной
                    # транзакции) и отказываем без перезаписи.
                    self.connection.rollback()
                    return None, 'already_linked'
                self.connection.commit()
                return dict(event), None
            except BaseException:
                self.connection.rollback()
                raise

    def create_calendar_event_and_link(
        self,
        *,
        name: str,
        date: str,
        date_to: str | None,
        level: str,
        sport: str,
        url: str,
        record_id: int,
    ) -> tuple[int | None, str | None]:
        """Создать событие календаря и сразу связать с ним запись.

        Атомарно (одна транзакция): INSERT события (без собственного
        commit, паттерн create_calendar_event) + guarded UPDATE записи
        с синхронизацией 5 полей. Проверки записи (существует + ещё NULL)
        выполняются ДО вставки, чтобы не оставлять событий-сирот; гонку на
        UPDATE закрывает rowcount (=0 → already_linked, откат всего).
        Возвращает (event_id, None) или (None, record_not_found /
        already_linked).

        Guard дублей (2026-09-27, ДОКУМЕНТИРОВАННОЕ расширение кортежа):
        точный дубль по event_identity_key уже существует →
        (existing_event_id, 'duplicate_event') — слот event_id несёт id
        СУЩЕСТВУЮЩЕГО события (роут показывает его в подсказке), новое
        событие НЕ создаётся и запись НЕ линкуется. Проверка — до INSERT,
        записей не было, откат не нужен.
        """
        with self._lock:
            try:
                record = self.connection.execute(
                    'SELECT id, calendar_event_id FROM competitions WHERE id = ?',
                    (record_id,),
                ).fetchone()
                if record is None:
                    return None, 'record_not_found'
                if record['calendar_event_id'] is not None:
                    return None, 'already_linked'
                duplicate = self._find_calendar_event_duplicate(name, date, date_to, sport, level)
                if duplicate is not None:
                    return int(duplicate['id']), 'duplicate_event'
                cursor = self.connection.execute(
                    'INSERT INTO calendar_events (name, date, date_to, level, sport, url, created_at)'
                    ' VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (name, date, date_to, level, sport, url, datetime.utcnow().isoformat()),
                )
                event_id = cursor.lastrowid
                linked = self.connection.execute(
                    'UPDATE competitions'
                    ' SET calendar_event_id = ?, name = ?, date = ?, date_to = ?, sport = ?, level = ?'
                    ' WHERE id = ? AND calendar_event_id IS NULL',
                    (event_id, name, date, date_to, sport, level, record_id),
                )
                if linked.rowcount == 0:
                    self.connection.rollback()
                    return None, 'already_linked'
                self.connection.commit()
                return event_id, None
            except BaseException:
                self.connection.rollback()
                raise

    def count_participations_without_event(self) -> int:
        """Дешёвый счётчик записей без ссылки на событие (карточка
        «Связывание с календарём» на странице обслуживания)."""
        with self._lock:
            return int(
                self.connection.execute(
                    'SELECT COUNT(*) AS total FROM competitions WHERE calendar_event_id IS NULL'
                ).fetchone()['total']
            )

    def calendar_link_backfill_preview(self) -> dict:
        """Read-only предпросмотр массового связывания NULL-записей.

        Предикат — ровно семантика стартового P0-backfill
        (_calendar_preset_match_sql: name + date + COALESCE(date_to, '')).
        Группы: matched (ровно один кандидат-событие), ambiguous (больше
        одного — вручную, автоматики не будет), unmatched (ноль).
        Списки capped (CALENDAR_LINK_PREVIEW_CAP) — счётчики при этом
        полные. Значения полей для диффа «после связывания» маршрут
        берёт из события сам (get_calendar_event).
        """
        with self._lock:
            predicate = self._calendar_preset_match_sql('c')
            rows = self.connection.execute(
                f'''
                SELECT
                    c.id AS record_id,
                    c.student_name,
                    c.name,
                    c.date,
                    c.date_to,
                    c.sport,
                    c.level,
                    (SELECT COUNT(*) FROM calendar_events e WHERE {predicate}) AS candidate_count
                FROM competitions c
                WHERE c.calendar_event_id IS NULL
                ORDER BY c.date ASC, c.id ASC
                '''
            ).fetchall()
            already_linked = int(
                self.connection.execute(
                    'SELECT COUNT(*) AS total FROM competitions WHERE calendar_event_id IS NOT NULL'
                ).fetchone()['total']
            )
            matched: list[dict] = []
            ambiguous: list[dict] = []
            unmatched: list[dict] = []
            for row in rows:
                base = {
                    'record_id': row['record_id'],
                    'student_name': row['student_name'],
                    'name': row['name'],
                    'date': row['date'],
                    'date_to': row['date_to'],
                    'sport': row['sport'],
                    'level': row['level'],
                }
                # Кандидаты строки — тот же предикат пресета, что и в
                # подсчёте выше (синхронно с _calendar_preset_match_sql):
                # коррелированная форма по id записи, один источник
                # семантики совпадения.
                candidate_where = f'competitions.id = ? AND {self._calendar_preset_match_sql("competitions")}'
                if row['candidate_count'] == 1:
                    if len(matched) < CALENDAR_LINK_PREVIEW_CAP:
                        event = self.connection.execute(
                            f'SELECT e.id AS event_id, e.name AS event_name '
                            f'FROM competitions, calendar_events e WHERE {candidate_where}',
                            (row['record_id'],),
                        ).fetchone()
                        matched.append({**base, 'event_id': event['event_id'], 'event_name': event['event_name']})
                elif row['candidate_count'] > 1:
                    if len(ambiguous) < CALENDAR_LINK_PREVIEW_CAP:
                        candidates = self.connection.execute(
                            f'SELECT e.name AS event_name '
                            f'FROM competitions, calendar_events e WHERE {candidate_where}',
                            (row['record_id'],),
                        ).fetchall()
                        ambiguous.append(
                            {
                                **base,
                                'candidate_count': row['candidate_count'],
                                'candidate_names': [candidate['event_name'] for candidate in candidates],
                            }
                        )
                elif len(unmatched) < CALENDAR_LINK_PREVIEW_CAP:
                    unmatched.append(base)
            matched_total = sum(1 for row in rows if row['candidate_count'] == 1)
            ambiguous_total = sum(1 for row in rows if row['candidate_count'] > 1)
            return {
                'counters': {
                    'considered': len(rows),
                    'matched': matched_total,
                    'unmatched': len(rows) - matched_total - ambiguous_total,
                    'ambiguous': ambiguous_total,
                    'already_linked': already_linked,
                },
                'matched': matched,
                'ambiguous': ambiguous,
                'unmatched': unmatched,
            }

    def apply_calendar_link_backfill(self) -> tuple[dict[str, int], list[dict]]:
        """Массовое связывание matched-записей (одна транзакция).

        В батч входят ТОЛЬКО строки с единственным пресет-кандидатом
        (=1) — ambiguous не линкуются никогда, unmatched нечего линковать.
        Для каждой строки — guarded UPDATE с синхронизацией 5 полей:
        rowcount = 0 (запись успели связать между предпросмотром и apply)
        — пропуск (skipped), не откат пакета. Возвращает счётчики
        {matched, linked, skipped} и список связанных
        [{record_id, event_id, event_name}].
        """
        with self._lock:
            predicate = self._calendar_preset_match_sql('c')
            pairs = self.connection.execute(
                f'''
                SELECT
                    c.id AS record_id,
                    (SELECT e.id FROM calendar_events e WHERE {predicate}) AS event_id,
                    (SELECT e.name FROM calendar_events e WHERE {predicate}) AS event_name
                FROM competitions c
                WHERE c.calendar_event_id IS NULL
                  AND (SELECT COUNT(*) FROM calendar_events e WHERE {predicate}) = 1
                ORDER BY c.id ASC
                '''
            ).fetchall()
            counters = {'matched': len(pairs), 'linked': 0, 'skipped': 0}
            items: list[dict] = []
            try:
                for pair in pairs:
                    event = self.connection.execute(
                        'SELECT name, date, date_to, sport, level FROM calendar_events WHERE id = ?',
                        (pair['event_id'],),
                    ).fetchone()
                    cursor = self.connection.execute(
                        'UPDATE competitions'
                        ' SET calendar_event_id = ?, name = ?, date = ?, date_to = ?, sport = ?, level = ?'
                        ' WHERE id = ? AND calendar_event_id IS NULL',
                        (
                            pair['event_id'],
                            event['name'],
                            event['date'],
                            event['date_to'],
                            event['sport'],
                            event['level'],
                            pair['record_id'],
                        ),
                    )
                    if cursor.rowcount == 0:
                        # Кто-то связал запись после предпросмотра — не
                        # ломаем пакет, считаем пропуском.
                        counters['skipped'] += 1
                        continue
                    counters['linked'] += 1
                    items.append(
                        {
                            'record_id': pair['record_id'],
                            'event_id': pair['event_id'],
                            'event_name': pair['event_name'],
                        }
                    )
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
            return counters, items
