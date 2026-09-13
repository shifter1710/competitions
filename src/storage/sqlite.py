import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable
from typing import Sequence

from src.models.competition import Competition
from src.models.custom_field import CustomField
from src.models.http.student_info import StudentInfo
from src.settings import settings

# Справочники значений ведут себя как уровни: записи остаются свободным
# текстом, справочник — только подсказки (docs/data-model-decisions.md,
# «Справочники значений»). Уровни живут в своей таблице и сюда не входят.
CATALOG_CATEGORIES: tuple[str, ...] = ('sport', 'institute')


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
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS catalog_values (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    value TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE (category, value)
                )
                '''
            )
            self._populate_catalog_values()
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
            self.connection.commit()

    def _populate_catalog_values(self):
        """Наполнить справочники уникальными значениями из существующих записей.

        Идемпотентно: благодаря UNIQUE (category, value) повторная инициализация
        не дублирует строки и не трогает скрытые. Пустые значения пропускаются.
        В вырожденных легаси-базах без колонок sport/institute populate молчит.
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
        date_from: str,
        date_to: str,
        position: str,
        level: str,
        name: str,
        custom_filters: Iterable[tuple[str, str, str]] = (),
    ) -> list[StudentInfo]:
        with self._lock:
            filters = ["review_status = 'approved'"]
            params: list[object] = []

            if date_from:
                date_from_dt = datetime.strptime(date_from, settings.date_format)
                filters.append('date >= ?')
                params.append(date_from_dt.isoformat())

            if date_to:
                date_to_dt = datetime.strptime(date_to, settings.date_format)
                filters.append('date <= ?')
                params.append(date_to_dt.isoformat())

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

            custom_clauses, custom_params = self._build_custom_filter_clauses(custom_filters)
            filters.extend(custom_clauses)
            params.extend(custom_params)

            where_clause = f'WHERE {" AND ".join(filters)}' if filters else ''
            rows = self.connection.execute(
                f'''
                SELECT
                    student_id,
                    student_name,
                    student_sex,
                    institute,
                    "group",
                    course,
                    COUNT(*) AS count_participation
                FROM competitions
                {where_clause}
                GROUP BY
                    student_id,
                    student_name,
                    student_sex,
                    institute,
                    "group",
                    course
                ORDER BY count_participation ASC
                ''',
                params,
            ).fetchall()

            return [
                StudentInfo(
                    student_id=row['student_id'],
                    student_name=row['student_name'],
                    student_sex=row['student_sex'],
                    institute=row['institute'],
                    group=row['group'],
                    course=row['course'],
                    count_participation=row['count_participation'],
                )
                for row in rows
            ]

    def save_competitions(
        self,
        competitions: Iterable[Competition],
        review_status: str = 'approved',
        owner_id: int | None = None,
    ):
        with self._lock:
            records = [
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
            self.connection.executemany(
                '''
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
                ''',
                records,
            )
            self.connection.commit()

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

    def count_competitions(self) -> int:
        with self._lock:
            row = self.connection.execute('SELECT COUNT(*) AS total FROM competitions').fetchone()
            return row['total']

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
                'SELECT id, username, role, active, name_aliases FROM users ORDER BY username ASC'
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
                'SELECT id, category, value, active FROM catalog_values '
                'WHERE category = ? ORDER BY active DESC, value ASC',
                (category,),
            ).fetchall()
            return [dict(row) for row in rows]

    def add_catalog_value(self, category: str, value: str) -> None:
        """Добавить значение в справочник; дубликат игнорируется без ошибки."""
        value = value.strip()
        if not value:
            return
        with self._lock:
            self.connection.execute(
                'INSERT OR IGNORE INTO catalog_values (category, value, active, created_at) VALUES (?, ?, 1, ?)',
                (category, value, datetime.utcnow().isoformat()),
            )
            self.connection.commit()

    def get_catalog_value(self, value_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, category, value, active FROM catalog_values WHERE id = ?',
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

    def count_records_using(self, category: str, value: str) -> int:
        """Сколько записей соревнований содержат это значение справочника."""
        # 'level' не входит в CATALOG_CATEGORIES, но таблица уровней своя —
        # для счётчика на странице справочников колонка записей та же.
        if category not in (*CATALOG_CATEGORIES, 'level'):
            return 0
        with self._lock:
            row = self.connection.execute(
                f'SELECT COUNT(*) AS total FROM competitions WHERE {category} = ?',
                (value,),
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

    def get_audit_events(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, created_at, user_id, username, action, details FROM audit_log ORDER BY id DESC LIMIT ?',
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

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
