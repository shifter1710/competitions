"""Stable GTO history, identity UIN and award-specific certificate metadata.

SQLite transactions enforce ownership even with foreign_keys disabled and with
several adapters. No name/hash identity matching is used in this domain.
"""
import re
import sqlite3
from contextlib import contextmanager
from datetime import date
from datetime import datetime

from src.storage.helpers import GTO_STAGE_MAX
from src.storage.helpers import GTO_STAGE_MIN
from src.storage.helpers import GTO_STATUSES
from src.storage.helpers import gto_year_max
from src.storage.helpers import GTO_YEAR_MIN

GTO_AWARDS = ('bronze', 'silver', 'gold')


class GtoMixin:
    @contextmanager
    def _gto_write(self):
        with self._lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def _rebuild_legacy_gto_history(self, columns, unique):
        expected = {'id', 'student_id', 'year', 'stage', 'status', 'created_at', 'updated_at', 'awarded_on'}
        if columns - expected:
            raise RuntimeError('Unrecognized legacy GTO columns; migration refused')
        objects = self.connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='student_gto_records' "
            "AND type IN ('index', 'trigger')"
        ).fetchall()
        # Rebuilding an unknown implicit constraint would silently lose it.
        if any(o['sql'] is None and o['name'] not in unique for o in objects):
            raise RuntimeError('Unrecognized legacy GTO constraint; migration refused')
        definition = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='student_gto_records'"
        ).fetchone()[0]
        if re.search(r'\b(CHECK|REFERENCES|COLLATE|DEFAULT|GENERATED)\b', definition, re.IGNORECASE):
            raise RuntimeError('Unrecognized legacy GTO constraints; migration refused')
        sequence = self.connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='student_gto_records'"
        ).fetchone()
        sequence = sequence['seq'] if sequence else 0
        preserved = [o['sql'] for o in objects if o['sql'] and o['name'] not in unique]
        fields = 'id, student_id, year, stage, status, created_at, updated_at'
        if 'awarded_on' in columns:
            fields += ', awarded_on'
        old = [tuple(r) for r in self.connection.execute(f'SELECT {fields} FROM student_gto_records ORDER BY id')]
        self.connection.execute(
            'CREATE TABLE student_gto_history_new ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL, '
            'year INTEGER NOT NULL, stage INTEGER NOT NULL, status TEXT NOT NULL, '
            'created_at TEXT NOT NULL, updated_at TEXT NOT NULL, awarded_on TEXT)'
        )
        self.connection.execute(
            f'INSERT INTO student_gto_history_new ({fields}) ' f'SELECT {fields} FROM student_gto_records'
        )
        copied = [
            tuple(r) for r in self.connection.execute(f'SELECT {fields} FROM student_gto_history_new ORDER BY id')
        ]
        if old != copied:
            raise RuntimeError('GTO history verification failed')
        self.connection.execute('DROP TABLE student_gto_records')
        self.connection.execute('ALTER TABLE student_gto_history_new RENAME TO student_gto_records')
        self.connection.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name='student_gto_records'", (sequence,)
        )
        # SQLite creates no sequence row for a never-populated new table.
        self.connection.execute(
            "INSERT INTO sqlite_sequence(name, seq) SELECT 'student_gto_records', ? "
            "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='student_gto_records')",
            (sequence,),
        )
        for sql in preserved:
            self.connection.execute(sql)

    def _migrate_gto_history(self):
        """One atomic, retryable migration; preserve issued IDs and old values."""
        with self._gto_write():
            columns = {r['name'] for r in self.connection.execute('PRAGMA table_info(student_gto_records)')}
            unique = []
            for index in self.connection.execute('PRAGMA index_list(student_gto_records)').fetchall():
                # Names come from SQLite, not request input; quote defensively.
                name = index['name'].replace('"', '""')
                fields = [r['name'] for r in self.connection.execute(f'PRAGMA index_info("{name}")')]
                if index['unique'] and fields == ['student_id', 'year', 'stage']:
                    unique.append(index['name'])
            if unique:
                self._rebuild_legacy_gto_history(columns, unique)
            elif 'awarded_on' not in columns:
                self.connection.execute('ALTER TABLE student_gto_records ADD COLUMN awarded_on TEXT')
            student_columns = {r['name'] for r in self.connection.execute('PRAGMA table_info(students)')}
            if 'gto_uin' not in student_columns:
                self.connection.execute('ALTER TABLE students ADD COLUMN gto_uin TEXT')
            self.connection.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS students_gto_uin_unique ON students(gto_uin) '
                'WHERE gto_uin IS NOT NULL'
            )
            self.connection.execute(
                'CREATE INDEX IF NOT EXISTS student_gto_history_student ON student_gto_records(student_id, year, stage)'
            )
            self.connection.execute(
                'CREATE TABLE IF NOT EXISTS student_gto_certificates ('
                'id INTEGER PRIMARY KEY AUTOINCREMENT, gto_record_id INTEGER NOT NULL, '
                'filename TEXT NOT NULL, stored_name TEXT NOT NULL UNIQUE, content_type TEXT NOT NULL, '
                'size INTEGER NOT NULL, uploaded_by INTEGER, created_at TEXT NOT NULL)'
            )
            self.connection.execute(
                'CREATE INDEX IF NOT EXISTS gto_certificates_award ON student_gto_certificates(gto_record_id)'
            )
            # A marker never hides a malformed or partially upgraded database.
            required = {
                'student_gto_records': {
                    'id',
                    'student_id',
                    'year',
                    'stage',
                    'status',
                    'created_at',
                    'updated_at',
                    'awarded_on',
                },
                'student_gto_certificates': {
                    'id',
                    'gto_record_id',
                    'filename',
                    'stored_name',
                    'content_type',
                    'size',
                    'uploaded_by',
                    'created_at',
                },
            }
            for table, fields in required.items():
                actual = {r['name'] for r in self.connection.execute(f'PRAGMA table_info({table})')}
                if not fields <= actual:
                    raise RuntimeError('GTO schema verification failed')
            self._verify_gto_uin_index()
            self.connection.execute(
                "INSERT INTO app_settings(key, value) VALUES ('gto_history_v1', '1') "
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value'
            )

    def _verify_gto_uin_index(self):
        """Do not let a marker or an existing index name certify weaker uniqueness.

        Refuse an unrecognized definition rather than silently rebuilding over
        potentially duplicated identities. The surrounding migration rolls back.
        """
        indexes = {r['name']: r for r in self.connection.execute('PRAGMA index_list(students)')}
        index = indexes.get('students_gto_uin_unique')
        fields = [r['name'] for r in self.connection.execute('PRAGMA index_info(students_gto_uin_unique)')]
        definition = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='students_gto_uin_unique'"
        ).fetchone()
        trusted = 'CREATE UNIQUE INDEX students_gto_uin_unique ON students(gto_uin) ' 'WHERE gto_uin IS NOT NULL'

        def normalized(value):
            return re.sub(r'\s+', '', value or '').casefold()

        if (
            not index
            or not index['unique']
            or not index['partial']
            or fields != ['gto_uin']
            or definition is None
            or normalized(definition['sql']) != normalized(trusted)
        ):
            raise RuntimeError('GTO UIN uniqueness verification failed; unrecognized index definition')

    def set_student_gto_uin(self, student_id, value):
        if value is not None and not isinstance(value, str):
            return False, 'invalid_uin'
        if value and any(ord(c) < 32 or ord(c) == 127 for c in value):
            return False, 'invalid_uin'
        value = (value or '').strip() or None
        try:
            with self._gto_write():
                cursor = self.connection.execute(
                    'UPDATE students SET gto_uin=?, updated_at=? WHERE id=?',
                    (value, datetime.utcnow().isoformat(), student_id),
                )
                return (True, None) if cursor.rowcount else (False, 'student_not_found')
        except sqlite3.IntegrityError:
            return False, 'duplicate_uin'

    def _assert_valid_gto_fields(self, year, stage, status, awarded_on=None):
        if not isinstance(year, int) or isinstance(year, bool) or not GTO_YEAR_MIN <= year <= gto_year_max():
            return 'invalid_year'
        if not isinstance(stage, int) or isinstance(stage, bool) or not GTO_STAGE_MIN <= stage <= GTO_STAGE_MAX:
            return 'invalid_stage'
        if status not in GTO_STATUSES:
            return 'invalid_status'
        if awarded_on is not None:
            try:
                if not isinstance(awarded_on, str) or date.fromisoformat(awarded_on).isoformat() != awarded_on:
                    return 'invalid_awarded_on'
            except ValueError:
                return 'invalid_awarded_on'
            if status not in GTO_AWARDS:
                return 'date_requires_award'
        return None

    def list_student_gto_records(self, student_id):
        with self._lock:
            rows = self.connection.execute(
                'SELECT * FROM student_gto_records WHERE student_id=? '
                'ORDER BY year DESC, stage ASC, (awarded_on IS NULL) ASC, awarded_on ASC, id ASC',
                (int(student_id),),
            ).fetchall()
            result = [dict(r) for r in rows]
            for row in result:
                row['certificates'] = self._list_gto_certificates(student_id, row['id'])
            return result

    def get_student_gto_record(self, student_id, record_id):
        with self._lock:
            row = self.connection.execute(
                'SELECT * FROM student_gto_records WHERE id=? AND student_id=?', (int(record_id), int(student_id))
            ).fetchone()
            return dict(row) if row else None

    def create_student_gto_record(self, student_id, *, year, stage, status, awarded_on=None):
        error = self._assert_valid_gto_fields(year, stage, status, awarded_on)
        if error:
            return None, error
        with self._gto_write():
            if self.get_student_by_id(int(student_id)) is None:
                return None, 'student_not_found'
            now = datetime.utcnow().isoformat()
            cursor = self.connection.execute(
                'INSERT INTO student_gto_records(student_id, year, stage, status, awarded_on, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (int(student_id), year, stage, status, awarded_on, now, now),
            )
            return int(cursor.lastrowid), None

    def update_student_gto_record(self, student_id, record_id, *, year, stage, status, awarded_on=None):
        error = self._assert_valid_gto_fields(year, stage, status, awarded_on)
        if error:
            return False, error
        with self._gto_write():
            if self.get_student_by_id(int(student_id)) is None:
                return False, 'not_found'
            if status not in GTO_AWARDS and self._list_gto_certificates(student_id, record_id):
                return False, 'certificates_require_award'
            cursor = self.connection.execute(
                'UPDATE student_gto_records SET year=?, stage=?, status=?, awarded_on=?, updated_at=? '
                'WHERE id=? AND student_id=?',
                (year, stage, status, awarded_on, datetime.utcnow().isoformat(), int(record_id), int(student_id)),
            )
            return (True, None) if cursor.rowcount else (False, 'not_found')

    def delete_student_gto_record(self, student_id, record_id):
        deleted, _certificates = self.delete_gto_record_with_certificates(student_id, record_id)
        return deleted

    def delete_gto_record_with_certificates(self, student_id, record_id):
        with self._gto_write():
            if self.get_student_gto_record(student_id, record_id) is None:
                return False, []
            certificates = self._list_gto_certificates(student_id, record_id)
            self.connection.execute('DELETE FROM student_gto_certificates WHERE gto_record_id=?', (record_id,))
            self.connection.execute(
                'DELETE FROM student_gto_records WHERE id=? AND student_id=?', (record_id, student_id)
            )
            return True, certificates

    def count_student_gto_records(self, student_id):
        with self._lock:
            return self.connection.execute(
                'SELECT COUNT(*) FROM student_gto_records WHERE student_id=?', (int(student_id),)
            ).fetchone()[0]

    def _list_gto_certificates(self, student_id, record_id):
        with self._lock:
            return [
                dict(r)
                for r in self.connection.execute(
                    'SELECT c.* FROM student_gto_certificates c '
                    'JOIN student_gto_records g ON g.id=c.gto_record_id JOIN students s ON s.id=g.student_id '
                    "WHERE g.id=? AND s.id=? AND g.status IN ('bronze', 'silver', 'gold') ORDER BY c.id",
                    (record_id, student_id),
                )
            ]

    def create_gto_certificate(self, student_id, record_id, *, filename, stored_name, content_type, size, uploaded_by):
        with self._gto_write():
            award = self.get_student_gto_record(student_id, record_id)
            if not award or award['status'] not in GTO_AWARDS or self.get_student_by_id(student_id) is None:
                return None
            cursor = self.connection.execute(
                'INSERT INTO student_gto_certificates '
                '(gto_record_id, filename, stored_name, content_type, size, uploaded_by, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (record_id, filename, stored_name, content_type, size, uploaded_by, datetime.utcnow().isoformat()),
            )
            return int(cursor.lastrowid)

    def delete_gto_certificate(self, student_id, record_id, certificate_id):
        with self._gto_write():
            if not any(c['id'] == certificate_id for c in self._list_gto_certificates(student_id, record_id)):
                return False
            return (
                self.connection.execute(
                    'DELETE FROM student_gto_certificates WHERE id=? AND gto_record_id=?', (certificate_id, record_id)
                ).rowcount
                > 0
            )

    def get_gto_certificate_for_user(self, student_id, record_id, certificate_id, user_id):
        with self._lock:
            user = self.get_user_by_id(user_id)
            if not user or not user.get('active'):
                return None
            if user['role'] not in ('admin', 'editor', 'viewer'):
                if user['role'] != 'athlete' or user.get('student_ref_id') != student_id:
                    return None
            return next(
                (c for c in self._list_gto_certificates(student_id, record_id) if c['id'] == certificate_id), None
            )
