import hashlib
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
    expected_hash = make_competition('Спортсменов', datetime(2026, 2, 1)).student_id
    assert review == {
        'id': record_id,
        'review_status': 'pending',
        'owner_id': 7,
        'student_id': expected_hash,
    }

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


def test_athlete_sees_admin_created_records_matching_profile(adapter):
    admin_record = make_competition('Сидоров Сид Сидорович', datetime(2026, 1, 1))
    adapter.save_competitions([admin_record], owner_id=1)  # создал админ
    adapter.save_competitions(
        [make_competition('Сидоров Сид Сидорович', datetime(2026, 2, 1))],
        review_status='pending',
        owner_id=5,  # добавил сам атлет
    )
    adapter.save_competitions(
        [make_competition('Чужой Человек', datetime(2026, 3, 1))],
        owner_id=1,
    )

    profile_hash = admin_record.student_id
    visible = adapter.get_competitions(owner_id=5, student_id_hashes=[profile_hash])
    names = [comp.student_name for comp in visible]
    assert names == ['Сидоров Сид Сидорович', 'Сидоров Сид Сидорович']
    assert 'Чужой Человек' not in names


def with_real_hash(competition):
    competition.student_id = hashlib.sha256(competition.student_name.encode()).hexdigest()
    return competition


def test_alias_survives_surname_change(adapter):
    old_name = 'Иванова Анна Петровна'
    new_name = 'Петрова Анна Ивановна'
    adapter.save_competitions([with_real_hash(make_competition(old_name, datetime(2026, 1, 1)))], owner_id=1)

    adapter.create_user('anna', 'hash', 'athlete')
    user_id = adapter.get_user('anna')['id']
    adapter.add_name_alias(user_id, old_name)
    adapter.add_name_alias(user_id, new_name)

    visible = adapter.get_competitions(
        owner_id=user_id,
        student_id_hashes=[hashlib.sha256(n.encode()).hexdigest() for n in adapter.get_name_aliases(user_id)],
    )
    names = [comp.student_name for comp in visible]
    assert names == [old_name]


def test_audit_log_table_created_for_legacy_db(tmp_path):
    import sqlite3

    db_path = tmp_path / 'legacy.sqlite3'
    connection = sqlite3.connect(db_path)
    connection.execute('CREATE TABLE competitions (id INTEGER PRIMARY KEY)')
    connection.commit()
    connection.close()

    adapter = SQLiteAdapter(str(db_path))
    tables = {row['name'] for row in adapter.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert 'audit_log' in tables
    columns = {row['name'] for row in adapter.connection.execute('PRAGMA table_info(audit_log)')}
    assert {'id', 'created_at', 'user_id', 'username', 'action', 'details'} <= columns


def test_audit_log_append_and_order(adapter):
    adapter.add_audit_event(user_id=1, username='admin', action='login_success', details='{}')
    adapter.add_audit_event(user_id=None, username='ghost', action='login_failed')

    events = adapter.get_audit_events()
    assert [event['action'] for event in events] == ['login_failed', 'login_success']
    assert events[0]['user_id'] is None
    assert events[0]['username'] == 'ghost'
    assert events[1]['user_id'] == 1
    assert events[1]['details'] == '{}'
    assert events[0]['created_at'] >= events[1]['created_at']


def test_audit_log_limit(adapter):
    for index in range(5):
        adapter.add_audit_event(user_id=1, username='admin', action='login_success')
    events = adapter.get_audit_events(limit=3)
    assert len(events) == 3
    assert events[0]['id'] == 5


def test_merge_students_unifies_report_grouping(adapter):
    old_name = 'Иванова Анна Петровна'
    new_name = 'Петрова Анна Ивановна'
    adapter.save_competitions(
        [
            with_real_hash(make_competition(old_name, datetime(2025, 1, 1))),
            with_real_hash(make_competition(old_name, datetime(2025, 2, 1))),
        ]
    )
    adapter.save_competitions([with_real_hash(make_competition(new_name, datetime(2026, 1, 1)))])

    old_hash = hashlib.sha256(old_name.encode()).hexdigest()
    new_hash = hashlib.sha256(new_name.encode()).hexdigest()
    assert adapter.count_records_by_student_hash(old_hash) == 2

    merged = adapter.merge_students(old_hash, new_hash, new_name=new_name)
    assert merged == 2
    assert adapter.count_records_by_student_hash(old_hash) == 0

    infos = adapter.get_filtered('', '', '', '', '')
    assert len(infos) == 1
    assert infos[0].count_participation == 3
    assert infos[0].student_name == new_name


def merge_for(adapter, old_name, new_name):
    old_hash = hashlib.sha256(old_name.encode()).hexdigest()
    new_hash = hashlib.sha256(new_name.encode()).hexdigest()
    merged = adapter.merge_students(old_hash, new_hash, new_name=new_name)
    return merged, adapter.carry_name_aliases(old_name, new_name)


def test_merge_carries_alias_to_account_with_from_name(adapter):
    # QA scenario: athlete has alias `from_name`, records under that name,
    # merge rewrites records to the new hash — dashboard must stay intact.
    old_name = 'Иванова Анна Петровна'
    new_name = 'Петрова Анна Ивановна'
    adapter.save_competitions([with_real_hash(make_competition(old_name, datetime(2025, 1, 1)))])
    adapter.create_user('anna', 'hash', 'athlete')
    user_id = adapter.get_user('anna')['id']
    adapter.add_name_alias(user_id, old_name)

    merged, aliases_updated = merge_for(adapter, old_name, new_name)
    assert merged == 1
    assert aliases_updated == 1
    aliases = adapter.get_name_aliases(user_id)
    assert old_name in aliases
    assert new_name in aliases

    # records stay visible in the athlete dashboard via the new alias
    hashes = [hashlib.sha256(n.encode()).hexdigest() for n in aliases]
    visible = adapter.get_competitions(owner_id=user_id, student_id_hashes=hashes)
    assert [comp.student_name for comp in visible] == [new_name]


def test_merge_does_not_touch_accounts_without_alias(adapter):
    old_name = 'Иванова Анна Петровна'
    new_name = 'Петрова Анна Ивановна'
    adapter.create_user('anna', 'hash', 'athlete')
    user_id = adapter.get_user('anna')['id']
    adapter.add_name_alias(user_id, 'Смирнова Ольга Сергеевна')

    _, aliases_updated = merge_for(adapter, old_name, new_name)
    assert aliases_updated == 0
    assert adapter.get_name_aliases(user_id) == ['Смирнова Ольга Сергеевна']


def test_repeated_merge_does_not_duplicate_alias(adapter):
    old_name = 'Иванова Анна Петровна'
    new_name = 'Петрова Анна Ивановна'
    adapter.create_user('anna', 'hash', 'athlete')
    user_id = adapter.get_user('anna')['id']
    adapter.add_name_alias(user_id, old_name)

    merge_for(adapter, old_name, new_name)
    _, second_run = merge_for(adapter, old_name, new_name)
    assert second_run == 0
    assert adapter.get_name_aliases(user_id) == [old_name, new_name]
