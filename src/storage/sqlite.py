import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable

from src.models.competition import Competition
from src.models.custom_field import CustomField
from src.models.http.student_info import StudentInfo
from src.settings import settings


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
            user_columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(users)').fetchall()}
            if 'pwd_ver' not in user_columns:
                self.connection.execute('ALTER TABLE users ADD COLUMN pwd_ver INTEGER NOT NULL DEFAULT 0')
            self.connection.commit()

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

    def get_competitions(self) -> Iterable[Competition]:
        with self._lock:
            rows = self.connection.execute(
                '''
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
                ORDER BY created_at ASC
                '''
            ).fetchall()
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
                'SELECT id, username, role, active FROM users ORDER BY username ASC'
            ).fetchall()
            return [dict(row) for row in rows]

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

    def get_competition_review(self, record_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, review_status, owner_id FROM competitions WHERE id = ?',
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
