import json
import sqlite3
import threading
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Iterable
from typing import Sequence

from src.models.competition import Competition
from src.models.custom_field import CustomField
from src.models.http.report_row import ReportSliceRow
from src.models.http.student_info import StudentInfo
from src.settings import settings

# Справочники значений ведут себя как уровни: записи остаются свободным
# текстом, справочник — только подсказки (docs/data-model-decisions.md,
# «Справочники значений»). Уровни живут в своей таблице и сюда не входят.
CATALOG_CATEGORIES: tuple[str, ...] = ('sport', 'institute')

# Категории переименования значений справочника (решение 2026-09-13,
# docs/data-model-decisions.md «Переименование значений справочников»):
# уровень живёт в levels, остальные — в catalog_values. Колонка записей
# называется так же («group» — ключевое слово SQL, экранируется в запросах).
RENAME_CATEGORIES: tuple[str, ...] = ('level', 'sport', 'institute', 'group')

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
        review_comment
    FROM competitions
    '''

# Вставка записей соревнований: общий SQL для одиночного сохранения и импорта.
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
        review_comment
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    '''


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


class SQLiteAdapter:
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
                    created_at TEXT NOT NULL
                )
                '''
            )
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
            # не меняется. merged_into_id зарезервирована и не пишется/не
            # читается.
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

    def get_field_settings(self) -> dict[str, dict]:
        """Настройки базовых полей: key -> {'value_type', 'required'}."""
        with self._lock:
            rows = self.connection.execute('SELECT key, value_type, required FROM field_settings').fetchall()
            return {row['key']: {'value_type': row['value_type'], 'required': bool(row['required'])} for row in rows}

    def update_field_settings(self, entries: dict[str, tuple[str, bool]]) -> None:
        """Сохранить настройки базовых полей одной транзакцией.

        entries: key -> (value_type, required). Строки upsert'ятся: у поля
        без строки в таблице дефолт заменяется явной настройкой.
        """
        with self._lock:
            for key, (value_type, required) in entries.items():
                self.connection.execute(
                    '''
                    INSERT INTO field_settings (key, value_type, required)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value_type = excluded.value_type, required = excluded.required
                    ''',
                    (key, value_type, int(required)),
                )
            self.connection.commit()

    def add_import_queue_entry(
        self,
        payload: dict,
        *,
        matched_record_id: int | None,
        created_by: int | None,
    ) -> int:
        """Положить конфликтную строку импорта в очередь на подтверждение."""
        with self._lock:
            cursor = self.connection.execute(
                'INSERT INTO import_queue (created_at, payload_json, status, matched_record_id, created_by) '
                "VALUES (?, ?, 'pending', ?, ?)",
                (
                    datetime.utcnow().isoformat(),
                    json.dumps(payload, ensure_ascii=False),
                    matched_record_id,
                    created_by,
                ),
            )
            self.connection.commit()
            return cursor.lastrowid

    def list_import_queue(self, status: str = 'pending') -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, created_at, payload_json, status, matched_record_id, created_by '
                'FROM import_queue WHERE status = ? ORDER BY id ASC',
                (status,),
            ).fetchall()
            entries = []
            for row in rows:
                entry = dict(row)
                entry['payload'] = json.loads(row['payload_json'])
                entries.append(entry)
            return entries

    def get_import_queue_entry(self, entry_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, created_at, payload_json, status, matched_record_id, created_by '
                'FROM import_queue WHERE id = ?',
                (entry_id,),
            ).fetchone()
            if row is None:
                return None
            entry = dict(row)
            entry['payload'] = json.loads(row['payload_json'])
            return entry

    def set_import_queue_status(self, entry_id: int, status: str) -> None:
        with self._lock:
            self.connection.execute(
                'UPDATE import_queue SET status = ? WHERE id = ?',
                (status, entry_id),
            )
            self.connection.commit()

    def count_import_queue(self, status: str = 'pending') -> int:
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM import_queue WHERE status = ?',
                (status,),
            ).fetchone()
            return row['total']

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
    ) -> Iterable[Competition]:
        with self._lock:
            scope_clauses, scope_params = self._competition_scope_clauses(owner_id, student_id_hashes)
            where_clause = f'WHERE {" AND ".join(scope_clauses)}\n' if scope_clauses else ''
            query = f'{COMPETITION_SELECT_SQL}{where_clause}ORDER BY date ASC, created_at ASC'
            rows = self.connection.execute(query, scope_params).fetchall()
            return [self._row_to_competition(row) for row in rows]

    @staticmethod
    def _competition_scope_clauses(
        owner_id: int | None,
        student_id_hashes: Sequence[str],
    ) -> tuple[list[str], list[object]]:
        """Видимость записей (кабинет атлета): свои по owner_id и/или по хешам
        привязанных ФИО. Как в get_competitions: без owner_id ограничение
        не применяется — модератор видит весь реестр."""
        if owner_id is None:
            return [], []
        if student_id_hashes:
            placeholders = ', '.join('?' for _ in student_id_hashes)
            return [f'(owner_id = ? OR student_id IN ({placeholders}))'], [owner_id, *student_id_hashes]
        return ['owner_id = ?'], [owner_id]

    def _competition_filter_clauses(
        self,
        *,
        owner_id: int | None,
        student_id_hashes: Sequence[str] = (),
        name: str = '',
        institute: str = '',
        sport: str = '',
        level: str = '',
        review_status: str = '',
        unapproved_only: bool = False,
        date_from: str = '',
        date_to: str = '',
    ) -> tuple[str, list[object]]:
        """Общий WHERE серверных фильтров главной (прототип 02): видимость +
        ФИО (подстрока), институт/вид спорта/уровень (точное совпадение),
        статус проверки и период дат (дд.мм.гггг, как в фильтрах отчёта)."""
        clauses, params = self._competition_scope_clauses(owner_id, student_id_hashes)

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

    def get_custom_fields(self, include_inactive: bool = False) -> list[CustomField]:
        with self._lock:
            if include_inactive:
                rows = self.connection.execute(
                    '''
                    SELECT *
                    FROM custom_fields
                    ORDER BY active DESC, sort_order ASC, label ASC
                    '''
                ).fetchall()
            else:
                rows = self.connection.execute(
                    '''
                    SELECT *
                    FROM custom_fields
                    WHERE active = 1
                    ORDER BY sort_order ASC, label ASC
                    '''
                ).fetchall()
            return [self._row_to_custom_field(row) for row in rows]

    def create_custom_field(
        self,
        key: str,
        label: str,
        field_type: str,
        required: bool,
        show_in_table: bool,
        show_in_export: bool,
        show_in_template: bool,
        sort_order: int,
        link_target: str | None = None,
    ) -> None:
        with self._lock:
            self.connection.execute(
                '''
                INSERT INTO custom_fields (
                    key,
                    label,
                    field_type,
                    required,
                    show_in_table,
                    show_in_export,
                    show_in_template,
                    sort_order,
                    link_target
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    key,
                    label,
                    field_type,
                    int(required),
                    int(show_in_table),
                    int(show_in_export),
                    int(show_in_template),
                    sort_order,
                    link_target,
                ),
            )
            self.connection.commit()

    def update_custom_field(
        self,
        field_id: int,
        label: str,
        field_type: str,
        required: bool,
        show_in_table: bool,
        show_in_export: bool,
        show_in_template: bool,
        sort_order: int,
        active: bool,
        link_target: str | None = None,
    ) -> None:
        with self._lock:
            self.connection.execute(
                '''
                UPDATE custom_fields
                SET
                    label = ?,
                    field_type = ?,
                    required = ?,
                    show_in_table = ?,
                    show_in_export = ?,
                    show_in_template = ?,
                    sort_order = ?,
                    active = ?,
                    link_target = ?
                WHERE id = ?
                ''',
                (
                    label,
                    field_type,
                    int(required),
                    int(show_in_table),
                    int(show_in_export),
                    int(show_in_template),
                    sort_order,
                    int(active),
                    link_target,
                    field_id,
                ),
            )
            self.connection.commit()

    def disable_custom_field(self, field_id: int) -> None:
        with self._lock:
            self.connection.execute(
                'UPDATE custom_fields SET active = 0 WHERE id = ?',
                (field_id,),
            )
            self.connection.commit()

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
                    extra_data = ?
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
        """Записи с датой соревнования ДО указанной (для архива перед очисткой)."""
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT
                    id, student_id, student_name, student_sex, institute, "group", course,
                    sport, date, date_to, level, name, position, created_at, extra_data,
                    review_status, owner_id, review_comment
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

    def count_attachments(self) -> int:
        with self._lock:
            row = self.connection.execute('SELECT COUNT(*) AS total FROM attachments').fetchone()
            return row['total']

    def delete_all_competitions(self) -> int:
        """Wipe all competition records (admin maintenance action). Returns deleted row count."""
        with self._lock:
            cursor = self.connection.execute('DELETE FROM competitions')
            self.connection.commit()
            return cursor.rowcount

    def delete_all_attachments(self) -> int:
        """Wipe all attachment rows (admin maintenance action). Returns deleted row count."""
        with self._lock:
            cursor = self.connection.execute('DELETE FROM attachments')
            self.connection.commit()
            return cursor.rowcount

    def get_attachments_for_records(self, record_ids: Sequence[int]) -> list[dict]:
        """Вложения указанных записей (для архива и подсчёта перед очисткой по дате)."""
        ids = [int(record_id) for record_id in record_ids]
        if not ids:
            return []
        placeholders = ', '.join('?' for _ in ids)
        with self._lock:
            rows = self.connection.execute(
                f'SELECT id, record_id, filename, stored_name, content_type, size '
                f'FROM attachments WHERE record_id IN ({placeholders}) ORDER BY record_id ASC, id ASC',
                ids,
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_attachments_for_records(self, record_ids: Sequence[int]) -> int:
        """Удалить строки вложений указанных записей (очистка по дате). Возвращает число удалённых."""
        ids = [int(record_id) for record_id in record_ids]
        if not ids:
            return 0
        placeholders = ', '.join('?' for _ in ids)
        with self._lock:
            cursor = self.connection.execute(
                f'DELETE FROM attachments WHERE record_id IN ({placeholders})',
                ids,
            )
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

    def get_user(self, username: str) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, username, password_hash, role, active, pwd_ver FROM users WHERE username = ?',
                (username,),
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, username, password_hash, role, active, pwd_ver FROM users WHERE id = ?',
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, username, role, active, name_aliases, last_login_at, last_seen_at '
                'FROM users ORDER BY username ASC'
            ).fetchall()
            users = []
            for row in rows:
                user = dict(row)
                user['name_aliases'] = json.loads(row['name_aliases'] or '[]')
                users.append(user)
            return users

    def create_user(self, username: str, password_hash: str, role: str) -> None:
        with self._lock:
            self.connection.execute(
                'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
                (username, password_hash, role),
            )
            self.connection.commit()

    def set_user_password(self, user_id: int, password_hash: str) -> None:
        with self._lock:
            self.connection.execute(
                '''
                UPDATE users
                SET password_hash = ?, pwd_ver = pwd_ver + 1
                WHERE id = ?
                ''',
                (password_hash, user_id),
            )
            self.connection.commit()

    def set_user_active(self, user_id: int, active: bool) -> None:
        with self._lock:
            self.connection.execute(
                'UPDATE users SET active = ? WHERE id = ?',
                (int(active), user_id),
            )
            self.connection.commit()

    def set_user_last_login(self, user_id: int, when: datetime | None = None) -> None:
        """Зафиксировать успешный вход (№17): last_login_at и last_seen_at.

        Вход — тоже активность, поэтому last_seen обновляется вместе с входом;
        история входов остаётся и в аудите (login_success).
        """
        with self._lock:
            timestamp = (when or datetime.utcnow()).isoformat()
            self.connection.execute(
                'UPDATE users SET last_login_at = ?, last_seen_at = ? WHERE id = ?',
                (timestamp, timestamp, user_id),
            )
            self.connection.commit()

    def touch_user_seen(self, user_id: int, throttle_seconds: int = 60) -> bool:
        """Обновить last_seen_at, если он старше throttle_seconds (№17).

        Троттлинг в самом UPDATE — без отдельного чтения: условие WHERE не
        пропустит запись чаще раза в окно, поэтому «два запроса подряд» дают
        один UPDATE. Возвращает, была ли строка обновлена.
        """
        now = datetime.utcnow()
        cutoff = (now - timedelta(seconds=throttle_seconds)).isoformat()
        with self._lock:
            cursor = self.connection.execute(
                'UPDATE users SET last_seen_at = ? ' 'WHERE id = ? AND (last_seen_at IS NULL OR last_seen_at < ?)',
                (now.isoformat(), user_id, cutoff),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def count_records_by_owner(self, owner_id: int) -> int:
        """Сколько записей соревнований привязано к аккаунту владельца."""
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM competitions WHERE owner_id = ?',
                (owner_id,),
            ).fetchone()
            return row['total']

    def delete_user(self, user_id: int) -> int:
        """Удалить аккаунт, сохранив записи как исторические факты.

        Записи не удаляются: owner_id обнуляется, строки остаются в таблице,
        отчётах и выгрузках. Псевдонимы ФИО живут в строке пользователя
        (users.name_aliases) и исчезают вместе с аккаунтом. Аудит-журнал не
        трогается (append-only, события хранят username текстом). См.
        docs/data-model-decisions.md «Удаление пользователей». Возвращает
        число отвязанных записей.
        """
        with self._lock:
            cursor = self.connection.execute(
                'UPDATE competitions SET owner_id = NULL WHERE owner_id = ?',
                (user_id,),
            )
            detached = cursor.rowcount
            self.connection.execute('DELETE FROM users WHERE id = ?', (user_id,))
            self.connection.commit()
            return detached

    def get_level_names(self, include_inactive: bool = False) -> list[str]:
        with self._lock:
            if include_inactive:
                rows = self.connection.execute('SELECT name FROM levels ORDER BY sort_order ASC, name ASC').fetchall()
            else:
                rows = self.connection.execute(
                    'SELECT name FROM levels WHERE active = 1 ORDER BY sort_order ASC, name ASC'
                ).fetchall()
            return [row['name'] for row in rows]

    def list_levels(self) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, name, sort_order, active FROM levels ORDER BY active DESC, sort_order ASC, name ASC'
            ).fetchall()
            return [dict(row) for row in rows]

    def create_level(self, name: str, sort_order: int = 0) -> None:
        with self._lock:
            self.connection.execute(
                'INSERT INTO levels (name, sort_order) VALUES (?, ?)',
                (name, sort_order),
            )
            self.connection.commit()

    def hard_delete_level(self, level_id: int) -> None:
        """Физическое удаление уровня (№20): допустимо только для уровня
        без записей — проверка на стороне роута."""
        with self._lock:
            self.connection.execute('DELETE FROM levels WHERE id = ?', (level_id,))
            self.connection.commit()

    def disable_level(self, level_id: int) -> None:
        with self._lock:
            self.connection.execute(
                'UPDATE levels SET active = 0 WHERE id = ?',
                (level_id,),
            )
            self.connection.commit()

    def list_catalog(self, category: str) -> list[str]:
        """Активные значения справочника (подсказки для форм), по алфавиту."""
        with self._lock:
            rows = self.connection.execute(
                'SELECT value FROM catalog_values WHERE category = ? AND active = 1 ORDER BY value ASC',
                (category,),
            ).fetchall()
            return [row['value'] for row in rows]

    def list_catalog_all(self, category: str) -> list[dict]:
        """Все значения справочника (для админки): активные сверху, по алфавиту."""
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, category, value, parent_id, active FROM catalog_values '
                'WHERE category = ? ORDER BY active DESC, value ASC',
                (category,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_catalog_tree(self) -> list[dict]:
        """Институты с их группами для страницы «Справочники».

        Каждый институт — запись list_catalog_all('institute') с counters
        records_count (все записи с этим институтом) и списком groups
        (записи категории group с parent_id на институт, у каждой свой
        records_count — по паре институт+группа).
        """
        with self._lock:
            institutes = self.list_catalog_all('institute')
            group_rows = self.connection.execute(
                'SELECT id, category, value, parent_id, active FROM catalog_values '
                "WHERE category = 'group' ORDER BY active DESC, value ASC"
            ).fetchall()
        groups_by_parent: dict[int, list[dict]] = {}
        for row in group_rows:
            groups_by_parent.setdefault(row['parent_id'], []).append(dict(row))
        tree = []
        for institute in institutes:
            groups = groups_by_parent.get(institute['id'], [])
            for group in groups:
                group['records_count'] = self.count_records_using(
                    'group', group['value'], parent_value=institute['value']
                )
            tree.append(
                {
                    **institute,
                    'records_count': self.count_records_using('institute', institute['value']),
                    'groups': groups,
                }
            )
        return tree

    def get_group_options_by_institute(self) -> dict[str, list[str]]:
        """Активные группы по институтам для подсказок форм: институт → группы.

        Скрытые группы (active = 0) в подсказки не попадают; институт
        присутствует в карте, даже если скрыт сам, — его группы остаются
        подсказкой при точно введённом названии института.
        """
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT institutes.value AS institute, groups.value AS "group"
                FROM catalog_values AS groups
                JOIN catalog_values AS institutes ON institutes.id = groups.parent_id
                WHERE groups.category = 'group' AND groups.active = 1
                ORDER BY institutes.value ASC, groups.value ASC
                '''
            ).fetchall()
        options: dict[str, list[str]] = {}
        for row in rows:
            options.setdefault(row['institute'], []).append(row['group'])
        return options

    def find_catalog_row(
        self,
        category: str,
        value: str,
        *,
        parent_id: int | None = None,
    ) -> dict | None:
        """Запись справочника по значению без учёта регистра (№1.1).

        Для группы (parent_id задан) поиск среди детей этого родителя:
        уникальность группы — по паре (институт, группа). Возвращает первую
        подходящую строку или None.
        """
        value = value.strip()
        if not value:
            return None
        # Сравнение регистра — в Python: lower() в SQLite работает только
        # с ASCII и не приводит кириллицу (ИСИ/иси).
        with self._lock:
            if parent_id is None:
                rows = self.connection.execute(
                    'SELECT id, category, value, parent_id, active FROM catalog_values '
                    'WHERE category = ? AND parent_id IS NULL ORDER BY id ASC',
                    (category,),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    'SELECT id, category, value, parent_id, active FROM catalog_values '
                    'WHERE category = ? AND parent_id = ? ORDER BY id ASC',
                    (category, parent_id),
                ).fetchall()
        for row in rows:
            if row['value'].lower() == value.lower():
                return dict(row)
        return None

    def find_catalog_canonical(
        self,
        category: str,
        value: str,
        *,
        parent_id: int | None = None,
    ) -> str | None:
        """Каноническое написание значения справочника без учёта регистра (№1.1)."""
        row = self.find_catalog_row(category, value, parent_id=parent_id)
        return row['value'] if row else None

    def find_unique_group_institute(self, group: str) -> str | None:
        """Институт для группы №19а (docs/feedback-live.md).

        Если имя группы (без учёта регистра) встречается ровно у одного
        института в иерархии справочников — возвращается каноническое имя
        института. Неизвестная группа или одно имя в разных институтах —
        None (институт остаётся пустым, свободный ввод не ломаем).
        Скрытые группы (active = 0) не участвуют.
        """
        group = group.strip()
        if not group:
            return None
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT institutes.value AS institute, groups.value AS "group"
                FROM catalog_values AS groups
                JOIN catalog_values AS institutes ON institutes.id = groups.parent_id
                WHERE groups.category = 'group' AND groups.active = 1
                ORDER BY institutes.value ASC, groups.value ASC
                '''
            ).fetchall()
        matches = {row['institute'] for row in rows if row['group'].lower() == group.lower()}
        if len(matches) == 1:
            return matches.pop()
        return None

    def find_level_canonical(self, name: str) -> str | None:
        """Каноническое написание уровня без учёта регистра (№1.1).

        Уровень живёт в levels, а не в catalog_values — поэтому отдельный
        поиск. Каноническое написание имеет приоритет над нижнекейсовой
        нормализацией записи (см. apply_catalog_canonical_values).
        """
        name = name.strip()
        if not name:
            return None
        # Сравнение регистра — в Python (lower() в SQLite не берёт кириллицу).
        with self._lock:
            rows = self.connection.execute(
                'SELECT name FROM levels ORDER BY id ASC',
            ).fetchall()
        for row in rows:
            if row['name'].lower() == name.lower():
                return row['name']
        return None

    def search_athletes(self, query: str, limit: int = 8) -> list[dict]:
        """№23доп (docs/feedback-live.md): варианты атлетов по подстроке ФИО.

        Источник — объединение профилей (users.profile_data) и ПОСЛЕДНЕЙ
        записи с таким ФИО. Сравнение регистра — в Python: lower() в SQLite
        не приводит кириллицу (тот же подход, что в find_catalog_canonical).
        Возвращает до `limit` вариантов, отсортированных по алфавиту; поля
        пустые значения не включает.
        """
        query = query.strip().lower()
        if not query:
            return []
        athletes = self._known_athletes()
        matches = [entry for name, entry in athletes.items() if query in name.lower()]
        matches.sort(key=lambda entry: entry['name'].lower())
        return matches[:limit]

    def find_athlete_fields(self, name: str) -> dict:
        """№23доп: известные поля атлета по ТОЧНОМУ ФИО (без учёта регистра).

        Тот же источник, что search_athletes. Пустых значений в ответе нет:
        вызывающая сторона (автозаполнение импорта) подставляет только
        непустые поля и не перезаписывает заполненные.
        """
        name = name.strip().lower()
        if not name:
            return {}
        return self._known_athletes().get(name, {})

    _ATHLETE_FIELD_MAP: tuple[tuple[str, str], ...] = (
        ('student_sex', 'sex'),
        ('institute', 'institute'),
        ('group', 'group'),
        ('course', 'course'),
    )

    @classmethod
    def _athlete_entry_from_record(cls, row: sqlite3.Row) -> dict:
        entry = {'name': row['student_name']}
        for source_key, result_key in cls._ATHLETE_FIELD_MAP:
            value = row[source_key]
            if value is not None and str(value).strip():
                entry[result_key] = str(value).strip()
        return entry

    @classmethod
    def _athlete_entry_from_profile(cls, profile_data: str) -> dict | None:
        try:
            profile = json.loads(profile_data or '{}')
        except (TypeError, ValueError):
            return None
        if not isinstance(profile, dict):
            return None
        entry = {'name': str(profile.get('student_name') or '').strip()}
        if not entry['name']:
            return None
        for source_key, result_key in cls._ATHLETE_FIELD_MAP:
            value = profile.get(source_key)
            if value is not None and str(value).strip():
                entry[result_key] = str(value).strip()
        return entry

    def _known_athletes(self) -> dict[str, dict]:
        """Атлеты по ФИО: профиль приоритетнее, пробелы добирает последняя запись.

        Профили обходятся первыми (сознательно заполненные данные), затем
        записи от последней к первой: первое встреченное непустое значение
        записи — из последней записи. Ничего не перезаписывается: слияние
        только дозаполняет пробелы (_merge_athlete_entry).
        """
        merged: dict[str, dict] = {}
        with self._lock:
            profile_rows = self.connection.execute('SELECT profile_data FROM users').fetchall()
            # Последняя запись с таким ФИО: created_at DESC; id DESC —
            # тай-брейк для записей одной секунды (импорт).
            record_rows = self.connection.execute(
                'SELECT student_name, student_sex, institute, "group", course '
                'FROM competitions ORDER BY created_at DESC, id DESC'
            ).fetchall()
        for row in profile_rows:
            entry = self._athlete_entry_from_profile(row['profile_data'])
            if entry is not None:
                self._merge_athlete_entry(merged, entry)
        for row in record_rows:
            self._merge_athlete_entry(merged, self._athlete_entry_from_record(row))
        return merged

    @staticmethod
    def _merge_athlete_entry(merged: dict[str, dict], entry: dict) -> None:
        """Дозаполнить вариант атлета первыми встреченными непустыми значениями."""
        key = entry['name'].lower()
        target = merged.setdefault(key, {'name': entry['name']})
        for result_key in ('sex', 'institute', 'group', 'course'):
            if result_key not in target and result_key in entry:
                target[result_key] = entry[result_key]

    def add_catalog_value(self, category: str, value: str, parent_id: int | None = None) -> None:
        """Добавить значение в справочник; дубликат игнорируется без ошибки.

        Для групп parent_id указывает на запись института; уникальность пары
        (parent, value) держит дочерний частичный индекс.
        """
        value = value.strip()
        if not value:
            return
        with self._lock:
            self.connection.execute(
                'INSERT OR IGNORE INTO catalog_values (category, value, parent_id, active, created_at) '
                'VALUES (?, ?, ?, 1, ?)',
                (category, value, parent_id, datetime.utcnow().isoformat()),
            )
            self.connection.commit()

    def ensure_catalog_pair(self, institute: str, group: str) -> None:
        """Пара институт→группа из записи/импорта попадает в иерархию справочника.

        Институт обязан существовать (создаётся при отсутствии), группа кладётся
        с parent_id на него; повторная пара игнорируется. Плоские (без
        родителя) значения не трогаются — см. docs/data-model-decisions.md.
        """
        institute = institute.strip()
        group = group.strip()
        if not institute or not group:
            return
        with self._lock:
            self.add_catalog_value('institute', institute)
            parent = self.connection.execute(
                'SELECT id FROM catalog_values ' "WHERE category = 'institute' AND value = ? AND parent_id IS NULL",
                (institute,),
            ).fetchone()
            if parent is None:
                return
            self.connection.execute(
                'INSERT OR IGNORE INTO catalog_values (category, value, parent_id, active, created_at) '
                "VALUES ('group', ?, ?, 1, ?)",
                (group, parent['id'], datetime.utcnow().isoformat()),
            )
            self.connection.commit()

    def get_catalog_value(self, value_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, category, value, parent_id, active FROM catalog_values WHERE id = ?',
                (value_id,),
            ).fetchone()
            return dict(row) if row else None

    def hide_catalog_value(self, value_id: int) -> None:
        with self._lock:
            self.connection.execute('UPDATE catalog_values SET active = 0 WHERE id = ?', (value_id,))
            self.connection.commit()

    def unhide_catalog_value(self, value_id: int) -> None:
        with self._lock:
            self.connection.execute('UPDATE catalog_values SET active = 1 WHERE id = ?', (value_id,))
            self.connection.commit()

    def delete_catalog_value(self, value_id: int) -> None:
        with self._lock:
            self.connection.execute('DELETE FROM catalog_values WHERE id = ?', (value_id,))
            self.connection.commit()

    def rename_catalog_value(
        self,
        category: str,
        old_value: str,
        new_value: str,
        *,
        parent_value: str | None = None,
        update_records: bool = True,
    ) -> int:
        """Переименовать значение справочника одной транзакцией.

        Решение 2026-09-13 (docs/data-model-decisions.md «Переименование
        значений справочников»): записи хранят значения текстом, поэтому
        справочник и записи меняются вместе — иначе справочник и записи
        расходятся, а следующий импорт возвращает старое значение (значения
        сеются из записей). update_records=False — переименовать только
        справочник (осознанное расхождение, например для архивных значений).

        Уровень меняется в levels, остальные категории — в catalog_values;
        группа — по паре институт+группа (parent_value — её институт),
        одноимённые группы других институтов не затрагиваются. Возвращает
        число обновлённых записей (0 при update_records=False). Конфликт
        нового имени с существующим значением категории (для группы — в том
        же институте) или отсутствие старого — ValueError без изменений.
        """
        if category not in RENAME_CATEGORIES:
            raise ValueError(f'Неизвестная категория справочника: {category}')
        old_value = old_value.strip()
        new_value = new_value.strip()
        parent_value = (parent_value or '').strip()
        if not old_value or not new_value:
            raise ValueError('Значение не может быть пустым')
        if old_value == new_value:
            raise ValueError('Новое имя совпадает со старым')

        with self._lock:
            try:
                if category == 'level':
                    self._rename_level_row(old_value, new_value)
                else:
                    self._rename_catalog_row(category, old_value, new_value, parent_value)
                updated = 0
                if update_records:
                    if category == 'group':
                        cursor = self.connection.execute(
                            'UPDATE competitions SET "group" = ? WHERE "group" = ? AND institute = ?',
                            (new_value, old_value, parent_value),
                        )
                    else:
                        cursor = self.connection.execute(
                            f'UPDATE competitions SET {category} = ? WHERE {category} = ?',
                            (new_value, old_value),
                        )
                    updated = cursor.rowcount
                self.connection.commit()
                return updated
            except BaseException:
                self.connection.rollback()
                raise

    def _rename_level_row(self, old_value: str, new_value: str) -> None:
        """Строка таблицы levels: конфликт по UNIQUE(name), затем UPDATE."""
        conflict = self.connection.execute('SELECT 1 FROM levels WHERE name = ?', (new_value,)).fetchone()
        if conflict is not None:
            raise ValueError(f'Уровень «{new_value}» уже существует')
        cursor = self.connection.execute('UPDATE levels SET name = ? WHERE name = ?', (new_value, old_value))
        if not cursor.rowcount:
            raise ValueError(f'Уровень «{old_value}» не найден')

    def _rename_catalog_row(self, category: str, old_value: str, new_value: str, parent_value: str) -> None:
        """Строка catalog_values: плоские значения по (category, value),
        группы — по паре (parent_id института, value)."""
        if category == 'group':
            if not parent_value:
                raise ValueError('Для переименования группы нужен её институт')
            parent = self.connection.execute(
                'SELECT id FROM catalog_values ' "WHERE category = 'institute' AND parent_id IS NULL AND value = ?",
                (parent_value,),
            ).fetchone()
            if parent is None:
                raise ValueError(f'Институт «{parent_value}» не найден')
            conflict = self.connection.execute(
                "SELECT 1 FROM catalog_values WHERE category = 'group' AND value = ? AND parent_id = ?",
                (new_value, parent['id']),
            ).fetchone()
            if conflict is not None:
                raise ValueError(f'Группа «{new_value}» уже есть в институте «{parent_value}»')
            cursor = self.connection.execute(
                'UPDATE catalog_values SET value = ? ' "WHERE category = 'group' AND value = ? AND parent_id = ?",
                (new_value, old_value, parent['id']),
            )
        else:
            conflict = self.connection.execute(
                'SELECT 1 FROM catalog_values WHERE category = ? AND value = ? AND parent_id IS NULL',
                (category, new_value),
            ).fetchone()
            if conflict is not None:
                raise ValueError(f'Значение «{new_value}» уже есть в справочнике')
            cursor = self.connection.execute(
                'UPDATE catalog_values SET value = ? WHERE category = ? AND value = ? AND parent_id IS NULL',
                (new_value, category, old_value),
            )
        if not cursor.rowcount:
            raise ValueError(f'Значение «{old_value}» не найдено')

    def count_records_using(self, category: str, value: str, parent_value: str | None = None) -> int:
        """Сколько записей соревнований содержат это значение справочника.

        Для группы parent_value — её институт: считаются записи с парой
        институт+группа (одна и та же группа в разных институтах — разные
        записи справочника). Без parent_value считается плоское вхождение.
        """
        # 'level' не входит в CATALOG_CATEGORIES, но таблица уровней своя —
        # для счётчика на странице справочников колонка записей та же.
        if category not in (*CATALOG_CATEGORIES, 'level', 'group'):
            return 0
        with self._lock:
            if category == 'group' and parent_value:
                row = self.connection.execute(
                    'SELECT COUNT(*) AS total FROM competitions ' 'WHERE "group" = ? AND institute = ?',
                    (value, parent_value),
                ).fetchone()
            else:
                # "group" — ключевое слово SQL, колонка только в кавычках
                column = '"group"' if category == 'group' else category
                row = self.connection.execute(
                    f'SELECT COUNT(*) AS total FROM competitions WHERE {column} = ?',
                    (value,),
                ).fetchone()
            return row['total']

    def count_child_groups(self, institute_row_id: int) -> int:
        """Сколько групп справочника прикреплено к институту (включая скрытые)."""
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM catalog_values ' "WHERE category = 'group' AND parent_id = ?",
                (institute_row_id,),
            ).fetchone()
            return row['total']

    def get_competition_review(self, record_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, review_status, owner_id, student_id FROM competitions WHERE id = ?',
                (record_id,),
            ).fetchone()
            return dict(row) if row else None

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

    def create_attachment(
        self,
        record_id: int,
        filename: str,
        stored_name: str,
        content_type: str,
        size: int,
        uploaded_by: int | None,
    ) -> int:
        from datetime import datetime as dt

        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT INTO attachments (
                    record_id, filename, stored_name, content_type, size, uploaded_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    filename,
                    stored_name,
                    content_type,
                    size,
                    uploaded_by,
                    dt.utcnow().isoformat(),
                ),
            )
            self.connection.commit()
            return cursor.lastrowid

    def get_attachments(self, record_id: int | None = None) -> list[dict]:
        with self._lock:
            if record_id is None:
                rows = self.connection.execute(
                    'SELECT id, record_id, filename, stored_name, content_type, size '
                    'FROM attachments ORDER BY record_id ASC, id ASC'
                ).fetchall()
            else:
                rows = self.connection.execute(
                    'SELECT id, record_id, filename, stored_name, content_type, size '
                    'FROM attachments WHERE record_id = ? ORDER BY id ASC',
                    (record_id,),
                ).fetchall()
            return [dict(row) for row in rows]

    def get_attachment(self, attachment_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, record_id, filename, stored_name, content_type, size ' 'FROM attachments WHERE id = ?',
                (attachment_id,),
            ).fetchone()
            return dict(row) if row else None

    def delete_attachment(self, attachment_id: int) -> None:
        with self._lock:
            self.connection.execute('DELETE FROM attachments WHERE id = ?', (attachment_id,))
            self.connection.commit()

    def get_profile(self, user_id: int) -> dict:
        with self._lock:
            row = self.connection.execute(
                'SELECT profile_data FROM users WHERE id = ?',
                (user_id,),
            ).fetchone()
            if row is None:
                return {}
            return json.loads(row['profile_data'] or '{}')

    def set_profile(self, user_id: int, profile: dict) -> None:
        with self._lock:
            self.connection.execute(
                'UPDATE users SET profile_data = ? WHERE id = ?',
                (json.dumps(profile, ensure_ascii=False), user_id),
            )
            self.connection.commit()

    def get_name_aliases(self, user_id: int) -> list[str]:
        with self._lock:
            row = self.connection.execute(
                'SELECT name_aliases FROM users WHERE id = ?',
                (user_id,),
            ).fetchone()
            if row is None:
                return []
            return json.loads(row['name_aliases'] or '[]')

    def add_name_alias(self, user_id: int, name: str) -> None:
        name = name.strip()
        if not name:
            return
        with self._lock:
            row = self.connection.execute(
                'SELECT name_aliases FROM users WHERE id = ?',
                (user_id,),
            ).fetchone()
            if row is None:
                return
            aliases = json.loads(row['name_aliases'] or '[]')
            if name not in aliases:
                aliases.append(name)
                self.connection.execute(
                    'UPDATE users SET name_aliases = ? WHERE id = ?',
                    (json.dumps(aliases, ensure_ascii=False), user_id),
                )
                self.connection.commit()

    def carry_name_aliases(self, from_name: str, to_name: str) -> int:
        """After a student merge, add `to_name` to accounts that have `from_name` alias.

        Keeps athlete dashboards intact: records rewritten to the new name hash
        stay visible via the new alias. `from_name` is never removed
        (aliases list only grows). Returns the number of updated accounts.
        """
        from_name = from_name.strip()
        to_name = to_name.strip()
        if not from_name or not to_name or from_name == to_name:
            return 0
        with self._lock:
            rows = self.connection.execute('SELECT id, name_aliases FROM users').fetchall()
            updated = 0
            for row in rows:
                aliases = json.loads(row['name_aliases'] or '[]')
                if from_name in aliases and to_name not in aliases:
                    aliases.append(to_name)
                    self.connection.execute(
                        'UPDATE users SET name_aliases = ? WHERE id = ?',
                        (json.dumps(aliases, ensure_ascii=False), row['id']),
                    )
                    updated += 1
            if updated:
                self.connection.commit()
            return updated

    def add_audit_event(
        self,
        user_id: int | None,
        username: str,
        action: str,
        details: str = '',
    ) -> None:
        """Append a security audit event. The log is append-only by design."""
        with self._lock:
            self.connection.execute(
                'INSERT INTO audit_log (created_at, user_id, username, action, details) VALUES (?, ?, ?, ?, ?)',
                (datetime.utcnow().isoformat(), user_id, username, action, details),
            )
            self.connection.commit()

    @staticmethod
    def _audit_filter_clauses(
        username: str | None = None,
        action: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> tuple[str, list[object]]:
        """Общие условия фильтра журнала (даты — ISO 'YYYY-MM-DD', сравнение по DATE(created_at))."""
        clauses = []
        params: list[object] = []
        if username:
            clauses.append('username LIKE ?')
            params.append(f'%{username}%')
        if action:
            clauses.append('action = ?')
            params.append(action)
        if date_from:
            clauses.append('DATE(created_at) >= ?')
            params.append(date_from)
        if date_to:
            clauses.append('DATE(created_at) <= ?')
            params.append(date_to)
        where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
        return where, params

    def get_audit_events(
        self,
        limit: int = 200,
        offset: int = 0,
        username: str | None = None,
        action: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """Страница журнала (новые сверху) с фильтрами и смещением пагинации."""
        with self._lock:
            where, params = self._audit_filter_clauses(username, action, date_from, date_to)
            rows = self.connection.execute(
                f'SELECT id, created_at, user_id, username, action, details '
                f'FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?',
                (*params, int(limit), max(0, int(offset))),
            ).fetchall()
            return [dict(row) for row in rows]

    def count_audit_events(
        self,
        username: str | None = None,
        action: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> int:
        with self._lock:
            where, params = self._audit_filter_clauses(username, action, date_from, date_to)
            row = self.connection.execute(f'SELECT COUNT(*) AS total FROM audit_log {where}', params).fetchone()
            return row['total']

    def list_audit_actions(self) -> list[str]:
        """Различные действия журнала — для выпадающего фильтра."""
        with self._lock:
            rows = self.connection.execute('SELECT DISTINCT action FROM audit_log ORDER BY action ASC').fetchall()
            return [row['action'] for row in rows]

    def count_records_by_student_hash(self, student_id_hash: str) -> int:
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM competitions WHERE student_id = ?',
                (student_id_hash,),
            ).fetchone()
            return row['total']

    def get_student_names(self) -> list[str]:
        """Unique student names as stored in records, sorted alphabetically."""
        with self._lock:
            rows = self.connection.execute(
                'SELECT DISTINCT student_name FROM competitions ORDER BY student_name ASC'
            ).fetchall()
            return [row['student_name'] for row in rows]

    def merge_students(self, old_hash: str, new_hash: str, new_name: str | None = None) -> int:
        with self._lock:
            if new_name:
                cursor = self.connection.execute(
                    '''
                    UPDATE competitions
                    SET student_id = ?, student_name = ?
                    WHERE student_id = ?
                    ''',
                    (new_hash, new_name, old_hash),
                )
            else:
                cursor = self.connection.execute(
                    'UPDATE competitions SET student_id = ? WHERE student_id = ?',
                    (new_hash, old_hash),
                )
            self.connection.commit()
            return cursor.rowcount

    # ---- Карточки студентов (Student Identity v1, Phase 1 — фундамент) ----
    #
    # Карточка — АКТУАЛЬНЫЕ данные студента (ФИО, пол, институт, группа,
    # курс) и его псевдонимы ФИО. Связи записей/аккаунтов с карточкой
    # (student_ref_id) заполняет только ручное сопоставление (Phase 2);
    # импорт/отчёты/merge работают по легаси-ключу sha256(ФИО) как раньше.
    # Физического удаления студентов нет — только active=0.

    def create_student(
        self,
        full_name: str,
        sex: str,
        institute: str,
        group_name: str,
        course: str,
    ) -> int:
        """Создать карточку студента; возвращает id новой строки."""
        with self._lock:
            now = datetime.utcnow().isoformat()
            cursor = self.connection.execute(
                'INSERT INTO students (full_name, sex, institute, group_name, course, active, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, 1, ?, ?)',
                (full_name, sex, institute, group_name, course, now, now),
            )
            self.connection.commit()
            return cursor.lastrowid

    def get_student_by_id(self, student_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, full_name, sex, institute, group_name, course, active, merged_into_id, '
                'created_at, updated_at FROM students WHERE id = ?',
                (student_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_students(self, search: str = '') -> list[dict]:
        """Все карточки: активные сверху, по алфавиту; search — подстрока ФИО
        (LIKE, как фильтр ФИО в отчётах)."""
        with self._lock:
            params: list[object] = []
            where = ''
            if search:
                where = 'WHERE full_name LIKE ?'
                params.append(f'%{search}%')
            rows = self.connection.execute(
                f'SELECT id, full_name, sex, institute, group_name, course, active, merged_into_id, '
                f'created_at, updated_at FROM students {where} ORDER BY active DESC, full_name ASC',
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def update_student(
        self,
        student_id: int,
        full_name: str,
        sex: str,
        institute: str,
        group_name: str,
        course: str,
    ) -> bool:
        """Обновить актуальные данные карточки (updated_at меняется).
        Возвращает False, если студента с таким id нет."""
        with self._lock:
            cursor = self.connection.execute(
                'UPDATE students SET full_name = ?, sex = ?, institute = ?, group_name = ?, course = ?, '
                'updated_at = ? WHERE id = ?',
                (full_name, sex, institute, group_name, course, datetime.utcnow().isoformat(), student_id),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def set_student_active(self, student_id: int, active: bool) -> bool:
        """Включить/выключить карточку (физического удаления нет)."""
        with self._lock:
            cursor = self.connection.execute(
                'UPDATE students SET active = ? WHERE id = ?',
                (int(active), student_id),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def add_student_alias(self, student_id: int, name: str) -> bool:
        """Добавить псевдоним ФИО; False — пустое имя, нет такого студента
        или дубль пары (student_id, name) по UNIQUE."""
        name = name.strip()
        if not name:
            return False
        with self._lock:
            if self.get_student_by_id(student_id) is None:
                return False
            try:
                self.connection.execute(
                    'INSERT INTO student_aliases (student_id, name, created_at) VALUES (?, ?, ?)',
                    (student_id, name, datetime.utcnow().isoformat()),
                )
                self.connection.commit()
            except sqlite3.IntegrityError:
                self.connection.rollback()
                return False
            return True

    def remove_student_alias(self, student_id: int, alias_id: int) -> bool:
        """Удалить псевдоним в пределах карточки: WHERE id = ? AND student_id = ?."""
        with self._lock:
            cursor = self.connection.execute(
                'DELETE FROM student_aliases WHERE id = ? AND student_id = ?',
                (alias_id, student_id),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def list_student_aliases(self, student_id: int) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, student_id, name, created_at FROM student_aliases WHERE student_id = ? ORDER BY id',
                (student_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    # ---- Сопоставление данных (Student Identity v1, Phase 2). ----
    #
    # Ручное заполнение стабильных связей student_ref_id существующих
    # записей/аккаунтов с карточками. Кандидаты — ТОЛЬКО предложения:
    # ничего не связывается автоматически. Привязка меняет ТОЛЬКО
    # student_ref_id: снимки данных в записях, легаси-ключ
    # student_id (sha256 ФИО) и рабочие workflow не трогаются.

    def count_student_reconciliation(self) -> dict:
        """Сводные счётчики сопоставления: всего/связано/без связи."""
        with self._lock:
            records_total = self.connection.execute('SELECT COUNT(*) AS total FROM competitions').fetchone()['total']
            records_linked = self.connection.execute(
                'SELECT COUNT(*) AS total FROM competitions WHERE student_ref_id IS NOT NULL'
            ).fetchone()['total']
            athlete_users_total = self.connection.execute(
                "SELECT COUNT(*) AS total FROM users WHERE role = 'athlete'"
            ).fetchone()['total']
            athlete_users_linked = self.connection.execute(
                "SELECT COUNT(*) AS total FROM users WHERE role = 'athlete' AND student_ref_id IS NOT NULL"
            ).fetchone()['total']
        return {
            'records_total': records_total,
            'records_linked': records_linked,
            'records_unlinked': records_total - records_linked,
            'athlete_users_total': athlete_users_total,
            'athlete_users_linked': athlete_users_linked,
            'athlete_users_unlinked': athlete_users_total - athlete_users_linked,
        }

    def count_unlinked_competitions(self, search: str = '') -> int:
        """Число записей без карточки студента (с тем же поиском, что список)."""
        with self._lock:
            params: list[object] = []
            where = 'WHERE student_ref_id IS NULL'
            if search:
                where += ' AND student_name LIKE ?'
                params.append(f'%{search}%')
            row = self.connection.execute(
                f'SELECT COUNT(*) AS total FROM competitions {where}',
                params,
            ).fetchone()
            return row['total']

    def list_unlinked_competitions(self, search: str = '', limit: int = 50, offset: int = 0) -> list[dict]:
        """Записи без карточки студента: по ФИО, затем по дате, затем по id."""
        with self._lock:
            params: list[object] = []
            where = 'WHERE student_ref_id IS NULL'
            if search:
                where += ' AND student_name LIKE ?'
                params.append(f'%{search}%')
            rows = self.connection.execute(
                f'''
                SELECT
                    id, student_name, student_sex, institute, "group", course,
                    sport, date, date_to, name, level, position, review_status
                FROM competitions
                {where}
                ORDER BY student_name ASC, date ASC, id ASC
                LIMIT ? OFFSET ?
                ''',
                [*params, int(limit), int(offset)],
            ).fetchall()
            return [dict(row) for row in rows]

    def list_unlinked_athlete_users(self) -> list[dict]:
        """Аккаунты атлетов без карточки студента, по логину.

        Без пагинации: аккаунтов немного, а карточки развёрнутые.
        """
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, username, active, profile_data, name_aliases FROM users '
                "WHERE role = 'athlete' AND student_ref_id IS NULL ORDER BY username ASC"
            ).fetchall()
            return [
                {
                    'id': row['id'],
                    'username': row['username'],
                    'active': row['active'],
                    'profile_data': json.loads(row['profile_data'] or '{}'),
                    'name_aliases': json.loads(row['name_aliases'] or '[]'),
                }
                for row in rows
            ]

    def get_unlinked_athlete_user(self, user_id: int) -> dict | None:
        """Один аккаунт атлета без карточки по id (для создания студента
        из профиля): None — нет такого, роль не athlete или уже привязан."""
        with self._lock:
            row = self.connection.execute(
                'SELECT id, username, active, profile_data, name_aliases FROM users '
                "WHERE id = ? AND role = 'athlete' AND student_ref_id IS NULL",
                (int(user_id),),
            ).fetchone()
            if row is None:
                return None
            return {
                'id': row['id'],
                'username': row['username'],
                'active': row['active'],
                'profile_data': json.loads(row['profile_data'] or '{}'),
                'name_aliases': json.loads(row['name_aliases'] or '[]'),
            }

    def find_student_candidates(self, name: str) -> list[dict]:
        """Кандидаты-карточки для ФИО из записи/профиля — ТОЛЬКО предложения.

        Точное совпадение (strip, регистр важен — как легаси-ключ
        sha256(ФИО)) по full_name или псевдониму; только активные карточки.
        Одна строка на карточку: совпадение по full_name приоритетнее
        совпадения по псевдониму. Порядок — по алфавиту ФИО.
        """
        name = (name or '').strip()
        if not name:
            return []
        with self._lock:
            full_matches = self.connection.execute(
                'SELECT id, full_name, sex, institute, group_name, course '
                'FROM students WHERE active = 1 AND full_name = ? ORDER BY full_name ASC, id ASC',
                (name,),
            ).fetchall()
            alias_matches = self.connection.execute(
                '''
                SELECT s.id, s.full_name, s.sex, s.institute, s.group_name, s.course, a.name AS alias_name
                FROM students s
                JOIN student_aliases a ON a.student_id = s.id
                WHERE s.active = 1 AND a.name = ?
                ORDER BY s.full_name ASC, s.id ASC, a.name ASC
                ''',
                (name,),
            ).fetchall()
        candidates: list[dict] = [
            {
                'student_id': row['id'],
                'full_name': row['full_name'],
                'sex': row['sex'],
                'institute': row['institute'],
                'group_name': row['group_name'],
                'course': row['course'],
                'match_type': 'full_name',
                'alias_name': None,
            }
            for row in full_matches
        ]
        seen_ids = {candidate['student_id'] for candidate in candidates}
        for row in alias_matches:
            if row['id'] in seen_ids:
                continue
            seen_ids.add(row['id'])
            candidates.append(
                {
                    'student_id': row['id'],
                    'full_name': row['full_name'],
                    'sex': row['sex'],
                    'institute': row['institute'],
                    'group_name': row['group_name'],
                    'course': row['course'],
                    'match_type': 'alias',
                    'alias_name': row['alias_name'],
                }
            )
        return candidates

    def link_competitions(self, record_ids: Sequence[int], student_id: int) -> tuple[int, str | None]:
        """Атомарно привязать записи к карточке: либо все, либо ничего.

        Проверки и UPDATE — под одной блокировкой, без изменений при любой
        ошибке. Возвращает (число привязанных, код ошибки или None):
        student_not_found / student_inactive / records_not_found /
        already_linked.
        """
        ids = sorted({int(record_id) for record_id in record_ids})
        if not ids:
            return 0, 'records_not_found'
        with self._lock:
            student = self.connection.execute(
                'SELECT active FROM students WHERE id = ?',
                (int(student_id),),
            ).fetchone()
            if student is None:
                return 0, 'student_not_found'
            if not student['active']:
                return 0, 'student_inactive'
            placeholders = ', '.join('?' for _ in ids)
            found = self.connection.execute(
                f'SELECT COUNT(*) AS total FROM competitions WHERE id IN ({placeholders})',
                ids,
            ).fetchone()['total']
            if found != len(ids):
                return 0, 'records_not_found'
            unlinked = self.connection.execute(
                f'SELECT COUNT(*) AS total FROM competitions '
                f'WHERE id IN ({placeholders}) AND student_ref_id IS NULL',
                ids,
            ).fetchone()['total']
            if unlinked != len(ids):
                return 0, 'already_linked'
            cursor = self.connection.execute(
                f'UPDATE competitions SET student_ref_id = ? '
                f'WHERE id IN ({placeholders}) AND student_ref_id IS NULL',
                [int(student_id), *ids],
            )
            self.connection.commit()
            return cursor.rowcount, None

    def unlink_competition(self, record_id: int) -> int | None:
        """Снять связь записи с карточкой; прежний student_ref_id (или None,
        если записи нет либо связи и так не было)."""
        with self._lock:
            row = self.connection.execute(
                'SELECT student_ref_id FROM competitions WHERE id = ?',
                (int(record_id),),
            ).fetchone()
            if row is None or row['student_ref_id'] is None:
                return None
            self.connection.execute(
                'UPDATE competitions SET student_ref_id = NULL WHERE id = ?',
                (int(record_id),),
            )
            self.connection.commit()
            return row['student_ref_id']

    def relink_competition(self, record_id: int, student_id: int) -> tuple[int | None, str | None]:
        """Перепривязать запись на другую карточку (можно и с NULL — тогда
        это привязка). Возвращает (прежний ref или None, ошибка или None)."""
        with self._lock:
            student = self.connection.execute(
                'SELECT active FROM students WHERE id = ?',
                (int(student_id),),
            ).fetchone()
            if student is None:
                return None, 'student_not_found'
            if not student['active']:
                return None, 'student_inactive'
            row = self.connection.execute(
                'SELECT student_ref_id FROM competitions WHERE id = ?',
                (int(record_id),),
            ).fetchone()
            if row is None:
                return None, 'records_not_found'
            self.connection.execute(
                'UPDATE competitions SET student_ref_id = ? WHERE id = ?',
                (int(student_id), int(record_id)),
            )
            self.connection.commit()
            return row['student_ref_id'], None

    def link_user(self, user_id: int, student_id: int) -> tuple[int, str | None]:
        """Привязать аккаунт атлета к карточке. Два аккаунта МОГУТ указывать
        на одну карточку (дубликаты — вопрос модерации, не запрета).

        Коды ошибок: user_not_found / user_not_athlete / user_already_linked /
        student_not_found / student_inactive.
        """
        with self._lock:
            user = self.connection.execute(
                'SELECT role, student_ref_id FROM users WHERE id = ?',
                (int(user_id),),
            ).fetchone()
            if user is None:
                return 0, 'user_not_found'
            if user['role'] != 'athlete':
                return 0, 'user_not_athlete'
            if user['student_ref_id'] is not None:
                return 0, 'user_already_linked'
            student = self.connection.execute(
                'SELECT active FROM students WHERE id = ?',
                (int(student_id),),
            ).fetchone()
            if student is None:
                return 0, 'student_not_found'
            if not student['active']:
                return 0, 'student_inactive'
            cursor = self.connection.execute(
                'UPDATE users SET student_ref_id = ? WHERE id = ? AND student_ref_id IS NULL',
                (int(student_id), int(user_id)),
            )
            self.connection.commit()
            return cursor.rowcount, None

    def unlink_user(self, user_id: int) -> int | None:
        """Снять связь аккаунта с карточкой; прежний student_ref_id (или
        None, если аккаунта нет либо связи и так не было)."""
        with self._lock:
            row = self.connection.execute(
                'SELECT student_ref_id FROM users WHERE id = ?',
                (int(user_id),),
            ).fetchone()
            if row is None or row['student_ref_id'] is None:
                return None
            self.connection.execute('UPDATE users SET student_ref_id = NULL WHERE id = ?', (int(user_id),))
            self.connection.commit()
            return row['student_ref_id']

    def relink_user(self, user_id: int, student_id: int) -> tuple[int | None, str | None]:
        """Перепривязать аккаунт атлета на другую карточку (можно и с NULL).
        Возвращает (прежний ref или None, ошибка или None)."""
        with self._lock:
            user = self.connection.execute(
                'SELECT role, student_ref_id FROM users WHERE id = ?',
                (int(user_id),),
            ).fetchone()
            if user is None:
                return None, 'user_not_found'
            if user['role'] != 'athlete':
                return None, 'user_not_athlete'
            student = self.connection.execute(
                'SELECT active FROM students WHERE id = ?',
                (int(student_id),),
            ).fetchone()
            if student is None:
                return None, 'student_not_found'
            if not student['active']:
                return None, 'student_inactive'
            self.connection.execute(
                'UPDATE users SET student_ref_id = ? WHERE id = ?',
                (int(student_id), int(user_id)),
            )
            self.connection.commit()
            return user['student_ref_id'], None

    def linked_records_count(self, student_id: int) -> int:
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM competitions WHERE student_ref_id = ?',
                (int(student_id),),
            ).fetchone()
            return row['total']

    def list_linked_records(self, student_id: int, limit: int = 50) -> list[dict]:
        """Записи, привязанные к карточке (свежие сверху). student_name —
        снимок из самой записи, а не актуальное ФИО карточки."""
        with self._lock:
            rows = self.connection.execute(
                '''
                SELECT id, student_name, sport, date, date_to, name
                FROM competitions
                WHERE student_ref_id = ?
                ORDER BY date DESC, id DESC
                LIMIT ?
                ''',
                (int(student_id), int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def linked_athlete_users(self, student_id: int) -> list[dict]:
        """Аккаунты атлетов, привязанные к карточке, по логину."""
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, username, active FROM users '
                "WHERE role = 'athlete' AND student_ref_id = ? ORDER BY username ASC",
                (int(student_id),),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_competition_student_ref(self, record_id: int) -> tuple[bool, int | None]:
        """(запись существует, её student_ref_id) — для страниц сопоставления."""
        with self._lock:
            row = self.connection.execute(
                'SELECT student_ref_id FROM competitions WHERE id = ?',
                (int(record_id),),
            ).fetchone()
            if row is None:
                return False, None
            return True, row['student_ref_id']

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

    def create_calendar_event(
        self,
        name: str,
        date: str,
        date_to: str | None,
        level: str,
        sport: str,
        url: str,
    ) -> int:
        with self._lock:
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
                'SELECT id, name, date, date_to, level, sport, url, created_at' ' FROM calendar_events WHERE id = ?',
                (event_id,),
            ).fetchone()
            return dict(row) if row else None

    def update_calendar_event(
        self,
        event_id: int,
        name: str,
        date: str,
        date_to: str | None,
        level: str,
        sport: str,
        url: str,
    ) -> None:
        with self._lock:
            self.connection.execute(
                'UPDATE calendar_events'
                ' SET name = ?, date = ?, date_to = ?, level = ?, sport = ?, url = ?'
                ' WHERE id = ?',
                (name, date, date_to, level, sport, url, event_id),
            )
            self.connection.commit()

    def count_calendar_event_participants(self, event_id: int) -> int:
        """Записи реестра, совпадающие с пресетом (name + date + date_to)."""
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM calendar_events e, competitions c'
                f' WHERE e.id = ? AND {self._calendar_preset_match_sql()}',
                (event_id,),
            ).fetchone()
            return row['total']

    def delete_calendar_event(self, event_id: int) -> None:
        with self._lock:
            self.connection.execute('DELETE FROM calendar_events WHERE id = ?', (event_id,))
            self.connection.commit()

    def list_calendar_events(self, sport: str = '') -> list[dict]:
        """Все события по хронологии; рядом — счётчики участников по пресету:
        participant_count — все совпавшие записи реестра, no_result_count —
        из них с position = 0 («без результата»)."""
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
                LEFT JOIN competitions c ON {self._calendar_preset_match_sql()}
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
        """Записи реестра — участники события по пресету (name + date + date_to).

        Волна B (прототип 16): записи остаются обычными записями реестра
        (никаких FK), совпадение — тот же пресет, что и в счётчиках.
        Сначала с результатом, затем «ждут результата», внутри — по ФИО.
        """
        with self._lock:
            rows = self.connection.execute(
                f'''
                SELECT
                    c.id AS record_id,
                    c.student_name,
                    c.student_sex,
                    c.institute,
                    c."group" AS group_name,
                    c.course,
                    c.position
                FROM calendar_events e, competitions c
                WHERE e.id = ? AND {self._calendar_preset_match_sql()}
                ORDER BY
                    CASE WHEN c.position = 0 THEN 1 ELSE 0 END,
                    c.student_name ASC
                ''',
                (event_id,),
            ).fetchall()
            return [dict(row) for row in rows]
