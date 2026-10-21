"""GTO historical migration and current identity integrity on synthetic databases."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from src.storage.sqlite import SQLiteAdapter


@pytest.fixture
def history_db(tmp_path):
    adapter = SQLiteAdapter(str(tmp_path / 'synthetic-history.sqlite3'))
    yield adapter
    adapter.connection.close()


def make_legacy(path):
    adapter = SQLiteAdapter(str(path))
    student = adapter.create_student('Синтетический Студент Образец', '', 'Учебный институт', 'ДЕМО-26', '')
    adapter.connection.execute('DROP TABLE student_gto_records')
    adapter.connection.execute(
        'CREATE TABLE student_gto_records ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL, year INTEGER NOT NULL, '
        'stage INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, '
        'UNIQUE(student_id, year, stage))'
    )
    for index, status in enumerate(('registered', 'participated', 'bronze', 'silver', 'gold', 'unknown-legacy')):
        adapter.connection.execute(
            'INSERT INTO student_gto_records VALUES (?, ?, ?, ?, ?, ?, ?)',
            (index + 10, student, 2020 + index, 8, status, 'synthetic-created', 'synthetic-updated'),
        )
    adapter.connection.execute("INSERT INTO student_gto_records VALUES (900, ?, 2026, 8, 'gold', 'x', 'x')", (student,))
    adapter.connection.execute('DELETE FROM student_gto_records WHERE id=900')
    adapter.connection.execute('CREATE INDEX synthetic_gto_status ON student_gto_records(status)')
    adapter.connection.execute('CREATE TABLE synthetic_migration_audit(id INTEGER)')
    adapter.connection.execute(
        'CREATE TRIGGER synthetic_gto_trigger AFTER INSERT ON student_gto_records '
        'BEGIN INSERT INTO synthetic_migration_audit VALUES (NEW.id); END'
    )
    adapter.connection.execute("DELETE FROM app_settings WHERE key='gto_history_v1'")
    adapter.connection.commit()
    old = [tuple(r) for r in adapter.connection.execute('SELECT * FROM student_gto_records ORDER BY id')]
    adapter.connection.close()
    return student, old


def test_legacy_preserves_all_statuses_ids_times_sequence_and_objects(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    student, old = make_legacy(path)
    adapter = SQLiteAdapter(str(path))
    assert [tuple(r)[:7] for r in adapter.connection.execute('SELECT * FROM student_gto_records ORDER BY id')] == old
    assert all(r['awarded_on'] is None for r in adapter.list_student_gto_records(student))
    names = {r[0] for r in adapter.connection.execute('SELECT name FROM sqlite_master')}
    assert {'synthetic_gto_status', 'synthetic_gto_trigger'} <= names
    record, error = adapter.create_student_gto_record(student, year=2025, stage=8, status='gold')
    assert record > 900 and error is None
    assert adapter.connection.execute('SELECT id FROM synthetic_migration_audit').fetchone()[0] == record
    adapter.connection.close()
    repeated = SQLiteAdapter(str(path))
    assert repeated.count_student_gto_records(student) == len(old) + 1
    assert repeated.connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    repeated.connection.close()


class InterruptedMigration(BaseException):
    pass


class FaultConnection:
    def __init__(self, connection, phase):
        self.connection = connection
        self.phase = phase

    def execute(self, sql, *args):
        if self.phase in sql:
            raise InterruptedMigration(self.phase)
        return self.connection.execute(sql, *args)

    def __getattr__(self, key):
        return getattr(self.connection, key)


@pytest.mark.parametrize(
    'phase',
    [
        'CREATE TABLE student_gto_history_new',
        'INSERT INTO student_gto_history_new',
        'DROP TABLE student_gto_records',
        'ALTER TABLE student_gto_history_new RENAME',
        'CREATE UNIQUE INDEX IF NOT EXISTS students_gto_uin_unique',
        "INSERT INTO app_settings(key, value) VALUES ('gto_history_v1'",
    ],
)
def test_interrupted_migration_is_atomic_and_retryable(tmp_path, phase):
    path = tmp_path / 'interrupted.sqlite3'
    student, old = make_legacy(path)

    class FailingAdapter(SQLiteAdapter):
        def _migrate_gto_history(self):
            self.connection = FaultConnection(self.connection, phase)
            super()._migrate_gto_history()

    with pytest.raises(InterruptedMigration):
        FailingAdapter(str(path))
    with sqlite3.connect(path) as connection:
        assert list(connection.execute('SELECT * FROM student_gto_records ORDER BY id')) == old
        assert connection.execute("SELECT value FROM app_settings WHERE key='gto_history_v1'").fetchone() is None
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name='student_gto_history_new'").fetchone() is None
        )
    retry = SQLiteAdapter(str(path))
    assert retry.count_student_gto_records(student) == len(old)
    retry.connection.close()


def test_uin_nullable_text_unique_trim_and_preserved_by_identity_edits(history_db):
    first = history_db.create_student('Синтетический Тёзка Пример', '', '', '', '')
    second = history_db.create_student('Синтетический Тёзка Пример', '', '', '', '')
    assert history_db.set_student_gto_uin(first, '  0000123  ') == (True, None)
    assert history_db.set_student_gto_uin(second, '0000123') == (False, 'duplicate_uin')
    assert history_db.set_student_gto_uin(second, None) == (True, None)
    assert history_db.set_student_gto_uin(second, 123) == (False, 'invalid_uin')
    assert history_db.set_student_gto_uin(second, 'abc\x00') == (False, 'invalid_uin')
    assert history_db.set_student_gto_uin(first, '0000123') == (True, None)
    assert history_db.update_student(first, 'Синтетический Тёзка Пример', '', '', 'ДЕМО-26', '')
    assert history_db.get_student_by_id(first)['gto_uin'] == '0000123'
    assert history_db.get_student_by_id(second)['gto_uin'] is None
    assert history_db.set_student_gto_uin(first, '   ') == (True, None)
    assert history_db.set_student_gto_uin(second, '0000123') == (True, None)
    # No arbitrary numeric or length policy: this is an external text identity.
    assert history_db.set_student_gto_uin(first, 'synthetic-' + '0' * 300) == (True, None)


@pytest.mark.parametrize('field', ['awards', 'uin', 'startup'])
def test_two_connections_serialize_writes(tmp_path, field):
    path = tmp_path / 'concurrent.sqlite3'
    if field == 'startup':
        student, old = make_legacy(path)
    else:
        base = SQLiteAdapter(str(path))
        ids = [base.create_student(f'Синтетическая Карточка {i}', '', '', '', '') for i in range(2)]
        base.connection.close()
    barrier = Barrier(2)

    def operation(index):
        if field == 'startup':
            barrier.wait(timeout=10)
        adapter = SQLiteAdapter(str(path))
        try:
            if field == 'startup':
                return adapter.count_student_gto_records(student)
            barrier.wait(timeout=10)
            if field == 'awards':
                return adapter.create_student_gto_record(ids[0], year=2026, stage=8, status='gold')
            return adapter.set_student_gto_uin(ids[index], '0000123')
        finally:
            adapter.connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(operation, range(2)))
    if field == 'startup':
        assert results == [len(old), len(old)]
    elif field == 'awards':
        assert all(error is None for _, error in results)
        assert len({record for record, _ in results}) == 2
    else:
        assert sorted(results, key=str) == sorted([(True, None), (False, 'duplicate_uin')], key=str)


@pytest.mark.parametrize('value', ['2026-02-30', '20260201', 20260101])
def test_invalid_award_date_never_creates_record(history_db, value):
    student = history_db.create_student('Синтетическая Карточка Дата', '', '', '', '')
    assert history_db.create_student_gto_record(student, year=2026, stage=8, status='gold', awarded_on=value) == (
        None,
        'invalid_awarded_on',
    )
    assert history_db.count_student_gto_records(student) == 0


def test_dates_chronology_and_non_awards(history_db):
    student = history_db.create_student('Синтетическая Карточка Хронология', '', '', '', '')
    for status, awarded_on in [('gold', None), ('silver', '2026-04-01'), ('bronze', '2026-02-01')]:
        assert (
            history_db.create_student_gto_record(student, year=2026, stage=8, status=status, awarded_on=awarded_on)[1]
            is None
        )
    assert [r['status'] for r in history_db.list_student_gto_records(student)] == ['bronze', 'silver', 'gold']
    assert history_db.create_student_gto_record(
        student, year=2026, stage=8, status='participated', awarded_on='2026-02-01'
    ) == (None, 'date_requires_award')


@pytest.mark.parametrize(
    'predicate,duplicates',
    [
        ("WHERE gto_uin = 'synthetic-only-value'", False),
        ("WHERE gto_uin = 'synthetic-only-value'", True),
        ('WHERE gto_uin IS NULL', False),
        ('', False),
    ],
)
def test_existing_marker_never_certifies_weaker_uin_index(tmp_path, predicate, duplicates):
    path = tmp_path / 'synthetic-invalid-index.sqlite3'
    adapter = SQLiteAdapter(str(path))
    ids = [adapter.create_student(f'Синтетическая Карточка Индекс {index}', '', '', '', '') for index in range(2)]
    adapter.connection.execute('DROP INDEX students_gto_uin_unique')
    if duplicates:
        adapter.connection.execute('UPDATE students SET gto_uin=?', ('000123',))
    adapter.connection.execute(f'CREATE UNIQUE INDEX students_gto_uin_unique ON students(gto_uin) {predicate}')
    adapter.connection.commit()
    before = [tuple(row) for row in adapter.connection.execute('SELECT * FROM students ORDER BY id')]
    old_index = adapter.connection.execute(
        "SELECT sql FROM sqlite_master WHERE name='students_gto_uin_unique'"
    ).fetchone()[0]
    assert adapter.connection.execute("SELECT value FROM app_settings WHERE key='gto_history_v1'").fetchone()[0] == '1'
    adapter.connection.close()
    with pytest.raises(RuntimeError, match='UIN uniqueness verification failed'):
        SQLiteAdapter(str(path))
    with sqlite3.connect(path) as connection:
        assert list(connection.execute('SELECT * FROM students ORDER BY id')) == before
        assert (
            connection.execute("SELECT sql FROM sqlite_master WHERE name='students_gto_uin_unique'").fetchone()[0]
            == old_index
        )
        assert len(list(connection.execute('SELECT id FROM students'))) == len(ids)
