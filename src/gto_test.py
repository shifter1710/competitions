"""Тесты записей ГТО (Готов к труду и обороне): storage-уровень на реальном
SQLite-адаптере + подписи ступеней/статусов.

Все ФИО и данные синтетические; фикстура — временная БД (паттерн
src/storage_test.py::adapter).
"""
import sqlite3
import time

import pytest

from src.gto import build_gto_rows
from src.gto import build_gto_stage_options
from src.gto import gto_stage_age_label
from src.gto import gto_stage_label
from src.gto import gto_stage_option_label
from src.gto import gto_status_label
from src.storage.helpers import GTO_STATUSES
from src.storage.helpers import gto_year_max
from src.storage.sqlite import GTO_STAGES
from src.storage.sqlite import GTO_STATUS_LABELS
from src.storage.sqlite import GTO_YEAR_MIN
from src.storage.sqlite import SQLiteAdapter


@pytest.fixture
def adapter(tmp_path):
    return SQLiteAdapter(str(tmp_path / 'gto.sqlite3'))


@pytest.fixture
def student_id(adapter) -> int:
    return adapter.create_student('Иванов Иван Иванович', 'М', 'ИСИ', 'ПГС-101', '2')


def raw_gto_count(adapter, student_id: int) -> int:
    return adapter.connection.execute(
        'SELECT COUNT(*) AS total FROM student_gto_records WHERE student_id = ?',
        (student_id,),
    ).fetchone()['total']


# ---- Схема: свежая БД, легаси-БД, идемпотентность ----


def test_fresh_db_creates_gto_table_with_unique_constraint(adapter, student_id):
    """Таблица создаётся на свежей БД; UNIQUE (student_id, year, stage)
    держит дубль на уровне БД (сырой INSERT → IntegrityError)."""
    columns = {row['name'] for row in adapter.connection.execute('PRAGMA table_info(student_gto_records)')}
    assert {
        'id',
        'student_id',
        'year',
        'stage',
        'status',
        'created_at',
        'updated_at',
    } <= columns

    now = '2026-01-01T00:00:00'
    adapter.connection.execute(
        'INSERT INTO student_gto_records (student_id, year, stage, status, created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (student_id, 2026, 8, 'gold', now, now),
    )
    with pytest.raises(sqlite3.IntegrityError):
        adapter.connection.execute(
            'INSERT INTO student_gto_records (student_id, year, stage, status, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (student_id, 2026, 8, 'bronze', now, now),
        )
    adapter.connection.rollback()


def test_legacy_db_gains_gto_table_on_construction(tmp_path):
    """Легаси-база без таблицы ГТО получает её при создании адаптера
    (аддитивная миграция, паттерн calendar_event_documents)."""
    db_path = tmp_path / 'legacy.sqlite3'
    connection = sqlite3.connect(db_path)
    connection.execute(
        '''
        CREATE TABLE students (
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
    connection.commit()
    connection.close()

    adapter = SQLiteAdapter(str(db_path))
    tables = {row['name'] for row in adapter.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert 'student_gto_records' in tables

    # Идемпотентность: повторное построение адаптера — no-op, дубликатов нет
    second = SQLiteAdapter(str(db_path))
    assert second.count_student_gto_records(1) == 0


# ---- CRUD: 0/1/N, порядок, владение ----


def test_gto_crud_empty_single_many(adapter, student_id):
    assert adapter.list_student_gto_records(student_id) == []
    assert adapter.count_student_gto_records(student_id) == 0

    first, error = adapter.create_student_gto_record(student_id, year=2024, stage=3, status='bronze')
    assert error is None and first is not None
    second, error = adapter.create_student_gto_record(student_id, year=2025, stage=8, status='silver')
    assert error is None
    third, error = adapter.create_student_gto_record(student_id, year=2024, stage=8, status='registered')
    assert error is None

    assert adapter.count_student_gto_records(student_id) == 3
    record = adapter.get_student_gto_record(student_id, first)
    assert record is not None
    assert (record['year'], record['stage'], record['status']) == (2024, 3, 'bronze')

    assert adapter.delete_student_gto_record(student_id, second)
    assert adapter.count_student_gto_records(student_id) == 2
    # Повторное удаление — False (idempotent)
    assert not adapter.delete_student_gto_record(student_id, second)


def test_gto_list_ordering_year_desc_stage_asc(adapter, student_id):
    adapter.create_student_gto_record(student_id, year=2023, stage=5, status='bronze')
    adapter.create_student_gto_record(student_id, year=2025, stage=2, status='silver')
    adapter.create_student_gto_record(student_id, year=2025, stage=9, status='gold')
    adapter.create_student_gto_record(student_id, year=2025, stage=4, status='registered')

    rows = adapter.list_student_gto_records(student_id)
    assert [(row['year'], row['stage']) for row in rows] == [
        (2025, 2),
        (2025, 4),
        (2025, 9),
        (2023, 5),
    ]


def test_gto_records_scoped_by_student_id(adapter):
    """Тёзки (одно ФИО) полностью изолированы: записи принадлежат ровно
    своей карточке по стабильному id."""
    first = adapter.create_student('Тёскин Тёзка Тёзкович', 'М', '', '', '')
    second = adapter.create_student('Тёскин Тёзка Тёзкович', 'М', '', '', '')

    first_record, _ = adapter.create_student_gto_record(first, year=2025, stage=8, status='gold')
    second_record, _ = adapter.create_student_gto_record(second, year=2025, stage=8, status='bronze')
    # Одна и та же пара (год, ступень) у разных карточек — не дубль
    assert first_record != second_record

    assert [row['status'] for row in adapter.list_student_gto_records(first)] == ['gold']
    assert [row['status'] for row in adapter.list_student_gto_records(second)] == ['bronze']

    # Чужой record_id не читается и не удаётся/не правится чужой карточкой
    assert adapter.get_student_gto_record(second, first_record) is None
    assert not adapter.delete_student_gto_record(second, first_record)
    ok, error = adapter.update_student_gto_record(second, first_record, year=2026, stage=9, status='silver')
    assert (ok, error) == (False, 'not_found')
    original = adapter.get_student_gto_record(first, first_record)
    assert (original['year'], original['stage'], original['status']) == (2025, 8, 'gold')


# ---- Дубли: create и update ----


def test_gto_duplicate_create_rejected_no_row(adapter, student_id):
    record_id, error = adapter.create_student_gto_record(student_id, year=2025, stage=8, status='gold')
    assert error is None

    duplicate_id, error = adapter.create_student_gto_record(student_id, year=2025, stage=8, status='bronze')
    assert duplicate_id is None
    assert error == 'duplicate'
    assert adapter.count_student_gto_records(student_id) == 1
    # Исходная строка не тронута
    assert adapter.get_student_gto_record(student_id, record_id)['status'] == 'gold'


def test_gto_update_moves_between_pairs_and_hits_occupied(adapter, student_id):
    """Правка — полная замена тройки: свободная пара ок, занятая (год,
    ступень) — duplicate, обе строки не меняются."""
    first, _ = adapter.create_student_gto_record(student_id, year=2024, stage=3, status='bronze')
    second, _ = adapter.create_student_gto_record(student_id, year=2025, stage=8, status='silver')

    # Свободная пара: тройка меняется целиком
    ok, error = adapter.update_student_gto_record(student_id, first, year=2026, stage=9, status='gold')
    assert (ok, error) == (True, None)
    moved = adapter.get_student_gto_record(student_id, first)
    assert (moved['year'], moved['stage'], moved['status']) == (2026, 9, 'gold')

    # Занятая пара (2025, 8): дубль, строки не меняются
    ok, error = adapter.update_student_gto_record(student_id, first, year=2025, stage=8, status='registered')
    assert (ok, error) == (False, 'duplicate')
    untouched = adapter.get_student_gto_record(student_id, first)
    assert (untouched['year'], untouched['stage'], untouched['status']) == (2026, 9, 'gold')
    occupied = adapter.get_student_gto_record(student_id, second)
    assert (occupied['year'], occupied['stage'], occupied['status']) == (2025, 8, 'silver')
    assert adapter.count_student_gto_records(student_id) == 2

    # Правка «на себя» той же парой — не дубль (exclude самой записи в UPDATE
    # не нужен: UNIQUE роняет только конфликт с ДРУГОЙ строкой)
    ok, error = adapter.update_student_gto_record(student_id, second, year=2025, stage=8, status='gold')
    assert (ok, error) == (True, None)


def test_gto_update_updates_updated_at_not_student(adapter, student_id):
    """updated_at записи меняется; students.updated_at НЕ трогается."""
    record_id, _ = adapter.create_student_gto_record(student_id, year=2025, stage=8, status='registered')
    student_before = adapter.get_student_by_id(student_id)
    record_before = adapter.get_student_gto_record(student_id, record_id)
    time.sleep(0.002)

    ok, error = adapter.update_student_gto_record(student_id, record_id, year=2025, stage=8, status='gold')
    assert (ok, error) == (True, None)

    record_after = adapter.get_student_gto_record(student_id, record_id)
    assert record_after['status'] == 'gold'
    assert record_after['created_at'] == record_before['created_at']
    assert record_after['updated_at'] > record_before['updated_at']
    student_after = adapter.get_student_by_id(student_id)
    assert student_after['updated_at'] == student_before['updated_at']


def test_gto_update_not_found(adapter, student_id):
    ok, error = adapter.update_student_gto_record(student_id, 999, year=2025, stage=8, status='gold')
    assert (ok, error) == (False, 'not_found')


# ---- Валидация: коды ошибок, ничего не пишется ----


@pytest.mark.parametrize('year', [GTO_YEAR_MIN - 1, gto_year_max() + 1, True, False, '2025', 2025.0, None])
def test_gto_create_invalid_year(adapter, student_id, year):
    record_id, error = adapter.create_student_gto_record(student_id, year=year, stage=8, status='gold')
    assert record_id is None
    assert error == 'invalid_year'
    assert adapter.count_student_gto_records(student_id) == 0


@pytest.mark.parametrize('stage', [0, 19, -1, True, False, '8', 8.5, None])
def test_gto_create_invalid_stage(adapter, student_id, stage):
    record_id, error = adapter.create_student_gto_record(student_id, year=2025, stage=stage, status='gold')
    assert record_id is None
    assert error == 'invalid_stage'
    assert adapter.count_student_gto_records(student_id) == 0


@pytest.mark.parametrize('status', ['Gold', 'бронза', '', 'любой текст', None, 5])
def test_gto_create_invalid_status(adapter, student_id, status):
    record_id, error = adapter.create_student_gto_record(student_id, year=2025, stage=8, status=status)
    assert record_id is None
    assert error == 'invalid_status'
    assert adapter.count_student_gto_records(student_id) == 0


def test_gto_boundary_year_and_stage_accepted(adapter, student_id):
    """Границы диапазона валидны: 2000 и текущий+1 год; ступени 1 и 18."""
    for year in (GTO_YEAR_MIN, gto_year_max()):
        record_id, error = adapter.create_student_gto_record(student_id, year=year, stage=1, status='registered')
        assert error is None
        assert adapter.get_student_gto_record(student_id, record_id)['year'] == year
    for stage in (1, 18):
        record_id, error = adapter.create_student_gto_record(
            student_id, year=2026 if stage == 18 else 2024, stage=stage, status='participated'
        )
        assert error is None


def test_gto_create_student_not_found(adapter):
    record_id, error = adapter.create_student_gto_record(999999, year=2025, stage=8, status='gold')
    assert record_id is None
    assert error == 'student_not_found'
    assert raw_gto_count(adapter, 999999) == 0


def test_gto_update_validation_rejected_writes_nothing(adapter, student_id):
    record_id, _ = adapter.create_student_gto_record(student_id, year=2025, stage=8, status='gold')
    ok, error = adapter.update_student_gto_record(student_id, record_id, year=1999, stage=8, status='gold')
    assert (ok, error) == (False, 'invalid_year')
    ok, error = adapter.update_student_gto_record(student_id, record_id, year=2025, stage=0, status='gold')
    assert (ok, error) == (False, 'invalid_stage')
    ok, error = adapter.update_student_gto_record(student_id, record_id, year=2025, stage=8, status='platinum')
    assert (ok, error) == (False, 'invalid_status')
    # Ни один отказ не изменил строку
    record = adapter.get_student_gto_record(student_id, record_id)
    assert (record['year'], record['stage'], record['status']) == (2025, 8, 'gold')


# ---- Блокер удаления карточки ----


def test_delete_student_blocked_by_gto_and_unblocked_after_cleanup(adapter, student_id):
    record_id, _ = adapter.create_student_gto_record(student_id, year=2025, stage=8, status='gold')
    assert adapter.delete_student(student_id) == (
        'blocked',
        {'records': 0, 'athlete_users': 0, 'merged_children': 0, 'gto_records': 1},
    )
    assert adapter.get_student_by_id(student_id) is not None

    assert adapter.delete_student_gto_record(student_id, record_id)
    assert adapter.delete_student(student_id) == ('ok', {})
    assert adapter.get_student_by_id(student_id) is None


# ---- Подписи ступеней/статусов (src/gto.py) ----


def test_gto_constants_shape():
    assert GTO_STATUSES == ('registered', 'participated', 'bronze', 'silver', 'gold')
    assert GTO_STATUS_LABELS['registered'] == 'Зарегистрирован'
    assert GTO_STATUS_LABELS['gold'] == 'Золото'
    assert len(GTO_STAGES) == 18
    assert [number for number, _roman, _age in GTO_STAGES] == list(range(1, 19))
    assert GTO_STAGES[0] == (1, 'I', '6–7 лет')
    assert GTO_STAGES[7] == (8, 'VIII', '20–24')
    assert GTO_STAGES[17] == (18, 'XVIII', '70+')


def test_gto_labels():
    # Рендер таблицы: «VIII ступень (20–24)»; возрастная группа отдельно
    assert gto_stage_label(8) == 'VIII ступень (20–24)'
    assert gto_stage_age_label(8) == '20–24'
    assert gto_stage_label(1) == 'I ступень (6–7 лет)'
    assert gto_stage_age_label(1) == '6–7 лет'
    # Опция select'а: «лет» дописывается только там, где его нет
    assert gto_stage_option_label(8) == 'VIII ступень — 20–24 лет'
    assert gto_stage_option_label(1) == 'I ступень — 6–7 лет'
    assert gto_stage_option_label(18) == 'XVIII ступень — 70+ лет'
    # Статусы
    assert gto_status_label('silver') == 'Серебро'
    assert gto_status_label('participated') == 'Участвовал'


def test_build_gto_rows_and_stage_options():
    rows = build_gto_rows(
        [
            {
                'id': 5,
                'student_id': 1,
                'year': 2025,
                'stage': 8,
                'status': 'gold',
                'created_at': '2026-01-01T00:00:00',
                'updated_at': '2026-01-01T00:00:00',
            }
        ]
    )
    assert rows == [
        {
            'id': 5,
            'year': 2025,
            'stage': 8,
            'stage_label': 'VIII ступень (20–24)',
            'age_label': '20–24',
            'status': 'gold',
            'status_label': 'Золото',
        }
    ]

    options = build_gto_stage_options()
    assert len(options) == 18
    assert options[0] == {'value': 1, 'label': 'I ступень — 6–7 лет'}
    assert options[7] == {'value': 8, 'label': 'VIII ступень — 20–24 лет'}
