from datetime import datetime

import pytest

from src.models.competition import Competition
from src.storage.sqlite import SQLiteAdapter


def make_competition(name: str, date: datetime, extra: dict | None = None) -> Competition:
    return Competition(
        student_id=f'id-{name}',
        student_name=name,
        student_sex='М',
        institute='ИСИ',
        group='ПГС-101',
        course=2,
        sport='Бег',
        date=date,
        level='внутривузовские',
        name='Кубок',
        position=1,
        extra_data=extra or {},
    )


def make_legacy_competition(name: str, date: datetime) -> Competition:
    return make_competition(name, date)


@pytest.fixture
def adapter(tmp_path):
    return SQLiteAdapter(str(tmp_path / 'test.sqlite3'))


def test_migration_adds_review_columns_to_existing_db(tmp_path):
    import sqlite3

    db_path = tmp_path / 'legacy.sqlite3'
    connection = sqlite3.connect(db_path)
    connection.execute(
        '''
        CREATE TABLE competitions (
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
            created_at TEXT NOT NULL,
            extra_data TEXT NOT NULL DEFAULT '{}'
        )
        '''
    )
    connection.commit()
    connection.close()

    adapter = SQLiteAdapter(str(db_path))
    columns = {row['name'] for row in adapter.connection.execute('PRAGMA table_info(competitions)')}
    assert {'review_status', 'owner_id', 'review_comment'} <= columns


def test_existing_records_become_approved(adapter):
    adapter.save_competitions([make_competition('Старый', datetime(2026, 1, 1))])
    record = adapter.get_competitions()[0]
    assert record.review_status == 'approved'


def test_pending_records_excluded_from_report(adapter):
    adapter.save_competitions([make_competition('Одобренный', datetime(2026, 1, 1))])
    adapter.save_competitions(
        [make_competition('На проверке', datetime(2026, 1, 2))],
        review_status='pending',
        owner_id=7,
    )

    report_names = [info.student_name for info in adapter.get_filtered('', '', '', '', '')]
    assert report_names == ['Одобренный']

    all_names = [comp.student_name for comp in adapter.get_competitions()]
    assert set(all_names) == {'Одобренный', 'На проверке'}


def test_review_lifecycle(adapter):
    adapter.save_competitions(
        [make_competition('Спортсменов', datetime(2026, 2, 1))],
        review_status='pending',
        owner_id=7,
    )
    record_id = int(adapter.get_competitions()[0].record_id)

    review = adapter.get_competition_review(record_id)
    assert review == {'id': record_id, 'review_status': 'pending', 'owner_id': 7}

    adapter.set_competition_review(record_id, 'rejected', 'проверьте место')
    assert adapter.get_competitions()[0].review_comment == 'проверьте место'

    adapter.set_competition_review(record_id, 'approved')
    assert adapter.get_competitions()[0].review_status == 'approved'
    assert [info.student_name for info in adapter.get_filtered('', '', '', '', '')] == ['Спортсменов']


def test_levels_seed_and_directory(adapter):
    from src.main import DEFAULT_LEVELS, seed_levels

    seed_levels(adapter)
    assert adapter.get_level_names() == list(DEFAULT_LEVELS)

    adapter.create_level('всероссийские')
    assert 'всероссийские' in adapter.get_level_names()

    adapter.rename_level(1, 'внутривузовские (обновлено)')
    adapter.disable_level(2)
    assert 'межвузовские' not in adapter.get_level_names()
    assert 'межвузовские' in adapter.get_level_names(include_inactive=True)


def test_report_filters_by_custom_text_field(adapter):
    adapter.save_competitions(
        [
            make_competition('С тренером А', datetime(2026, 1, 1), {'trainer': 'Иванов'}),
            make_competition('С тренером Б', datetime(2026, 1, 2), {'trainer': 'Петров'}),
            make_competition('Без тренера', datetime(2026, 1, 3)),
        ]
    )
    infos = adapter.get_filtered('', '', '', '', '', custom_filters=[('trainer', 'text', 'Иванов')])
    assert [info.student_name for info in infos] == ['С тренером А']


def test_report_filters_by_custom_number_field(adapter):
    adapter.save_competitions(
        [
            make_competition('Год 2024', datetime(2026, 1, 1), {'season': '2024'}),
            make_competition('Год 2025', datetime(2026, 1, 2), {'season': '2025'}),
        ]
    )
    infos = adapter.get_filtered('', '', '', '', '', custom_filters=[('season', 'number', '2025')])
    assert [info.student_name for info in infos] == ['Год 2025']


def test_report_filters_by_custom_date_field(adapter):
    adapter.save_competitions(
        [
            make_competition('Заявка ранняя', datetime(2026, 1, 1), {'application': '01.01.2026'}),
            make_competition('Заявка поздняя', datetime(2026, 1, 2), {'application': '15.02.2026'}),
        ]
    )
    infos = adapter.get_filtered('', '', '', '', '', custom_filters=[('application', 'date', '15.02.2026')])
    assert [info.student_name for info in infos] == ['Заявка поздняя']
