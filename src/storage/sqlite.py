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
        level,
        name,
        position,
        created_at,
        extra_data,
        review_status,
        owner_id,
        review_comment
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    active INTEGER NOT NULL DEFAULT 1
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
        )

    def get_competitions(
        self,
        owner_id: int | None = None,
        student_id_hashes: Sequence[str] = (),
    ) -> Iterable[Competition]:
        with self._lock:
            query = '''
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
            if owner_id is not None:
                if student_id_hashes:
                    placeholders = ', '.join('?' for _ in student_id_hashes)
                    query += f'WHERE (owner_id = ? OR student_id IN ({placeholders}))\n'
                    params = (owner_id, *student_id_hashes)
                else:
                    query += 'WHERE owner_id = ?\n'
                    params = (owner_id,)
                query += 'ORDER BY created_at ASC'
                rows = self.connection.execute(query, params).fetchall()
            else:
                query += 'ORDER BY created_at ASC'
                rows = self.connection.execute(query).fetchall()
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
                    sort_order
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                    active = ?
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
        """Условия периода отчёта: границы парсятся форматом settings.date_format."""
        clauses = []
        params: list[object] = []
        if date_from:
            clauses.append('date >= ?')
            params.append(datetime.strptime(date_from, settings.date_format).isoformat())
        if date_to:
            clauses.append('date <= ?')
            params.append(datetime.strptime(date_to, settings.date_format).isoformat())
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
        competitions: Sequence[Competition],
        new_competitions: Sequence[Competition],
        *,
        review_status: str = 'approved',
        owner_id: int | None = None,
    ) -> None:
        """Импорт одной транзакцией с одним COMMIT (находка QA №1).

        Раньше автопополнение справочников коммитилось на каждую строку
        (до четырёх WAL+fsync на строку — ~0,2 с/строку, заморозка event
        loop). Теперь уровни, справочники и вставка записей идут одной
        транзакцией; сбой на любом шаге — откат всего импорта: либо все
        строки, либо ничего (вместе со справочниками, без «полусостояний»).

        `competitions` — все строки файла: справочники пополняются и из
        строк-дублей (семантика бывших ensure_levels/ensure_catalog_values
        из src/main.py); `new_competitions` — то, что реально вставляется.
        """
        with self._lock:
            try:
                self._ensure_import_levels(competitions)
                self._ensure_import_catalogs(competitions)
                if new_competitions:
                    records = competition_insert_records(new_competitions, review_status, owner_id)
                    self.connection.executemany(COMPETITION_INSERT_SQL, records)
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def _ensure_import_levels(self, competitions: Sequence[Competition]) -> None:
        """Недостающие уровни из импорта — одной вставкой, без построчных коммитов."""
        known = {row['name'] for row in self.connection.execute('SELECT name FROM levels').fetchall()}
        missing = sorted({item.level for item in competitions if item.level not in known})
        if missing:
            self.connection.executemany(
                'INSERT INTO levels (name, sort_order) VALUES (?, 0)',
                [(name,) for name in missing],
            )

    def _ensure_import_catalogs(self, competitions: Sequence[Competition]) -> None:
        """Автопополнение видов спорта/институтов и пар институт→группа.

        Множествами и тремя executemany вместо вызова на каждую строку —
        семантика та же, что у add_catalog_value/ensure_catalog_pair
        (значения обрезаются, пустые пропускаются, дубликаты игнорируются,
        группа кладётся под плоский институт).
        """
        sports: set[str] = set()
        institutes: set[str] = set()
        pairs: set[tuple[str, str]] = set()
        for item in competitions:
            sport = (item.sport or '').strip()
            if sport:
                sports.add(sport)
            institute = (item.institute or '').strip()
            if institute:
                institutes.add(institute)
            group = (item.group or '').strip()
            if institute and group:
                pairs.add((institute, group))
        now = datetime.utcnow().isoformat()
        flat_rows = [
            *([('sport', value, now) for value in sorted(sports)]),
            *([('institute', value, now) for value in sorted(institutes)]),
        ]
        if flat_rows:
            self.connection.executemany(
                'INSERT OR IGNORE INTO catalog_values (category, value, parent_id, active, created_at) '
                'VALUES (?, ?, NULL, 1, ?)',
                flat_rows,
            )
        if not pairs:
            return
        pair_institutes = {institute for institute, _ in pairs}
        placeholders = ', '.join('?' for _ in pair_institutes)
        parent_rows = self.connection.execute(
            'SELECT id, value FROM catalog_values '
            f"WHERE category = 'institute' AND parent_id IS NULL AND value IN ({placeholders})",
            tuple(sorted(pair_institutes)),
        ).fetchall()
        parent_ids = {row['value']: row['id'] for row in parent_rows}
        group_rows = [
            ('group', group, parent_ids[institute], now)
            for institute, group in sorted(pairs)
            if institute in parent_ids
        ]
        if group_rows:
            self.connection.executemany(
                'INSERT OR IGNORE INTO catalog_values (category, value, parent_id, active, created_at) '
                'VALUES (?, ?, ?, 1, ?)',
                group_rows,
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
                    sport, date, level, name, position, created_at, extra_data,
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

    def rename_level(self, level_id: int, name: str) -> None:
        with self._lock:
            self.connection.execute(
                'UPDATE levels SET name = ? WHERE id = ?',
                (name, level_id),
            )
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
