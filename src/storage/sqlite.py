import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from src.models.competition import Competition
from src.models.http.student_info import StudentInfo
from src.settings import settings


class SQLiteAdapter:
    def __init__(self, database_path: str):
        db_path = Path(database_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self.connection = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self):
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
        self.connection.commit()

    @staticmethod
    def _row_to_competition(row: sqlite3.Row) -> Competition:
        return Competition.parse_obj(
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
            }
        )

    def get_competitions(self) -> Iterable[Competition]:
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
                created_at
            FROM competitions
            ORDER BY created_at ASC
            '''
        ).fetchall()
        return [self._row_to_competition(row) for row in rows]

    def get_filtered(
        self,
        date_from: str,
        date_to: str,
        position: str,
        level: str,
        name: str,
    ) -> list[StudentInfo]:
        filters = []
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

    def save_competitions(self, competitions: Iterable[Competition]):
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
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            records,
        )
        self.connection.commit()

    def update_competition(self, record_id: str, competition: Competition):
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
                position = ?
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
                int(record_id),
            ),
        )
        self.connection.commit()

    def delete_competition(self, record_id: str):
        self.connection.execute('DELETE FROM competitions WHERE id = ?', (int(record_id),))
        self.connection.commit()

    def clean_db(self):
        self.connection.execute('DELETE FROM competitions')
        self.connection.commit()
