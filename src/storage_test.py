import hashlib
import sqlite3
from datetime import datetime

import pytest

from src.conftest import make_report_fixture
from src.conftest import make_report_unapproved_fixture
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


# --- Расширение отчётов (замечание №19): срезы × метрики × фильтры. ---
# Фикстура и ожидаемые числа — src/conftest.py (метрики пересчитаны руками).


def slice_rows(rows):
    return [(row.slice_value, row.count_participation, row.count_wins, row.count_prizes) for row in rows]


def save_report_fixture(adapter):
    adapter.save_competitions(make_report_fixture())
    for review_status, record in make_report_unapproved_fixture():
        adapter.save_competitions([record], review_status=review_status, owner_id=9)
    return adapter


def test_report_student_slice_counts_metrics_in_sql(adapter):
    # Метрики для среза «студент»: участия/победы/призовые считаются одним
    # агрегирующим запросом; неподтверждённые записи не считаются.
    # Порядок — существующий: участий по возрастанию, затем ФИО/институт.
    save_report_fixture(adapter)
    infos = adapter.get_filtered('', '', '', '', '')

    assert [
        (
            info.student_name,
            info.institute,
            info.group,
            info.course,
            info.count_participation,
            info.count_wins,
            info.count_prizes,
        )
        for info in infos
    ] == [
        ('Козлов Кирилл', 'ИМИ', 'ТД-303', 2, 1, 0, 1),
        ('Козлов Кирилл', 'ИСИ', 'ПГС-101', 1, 1, 1, 1),
        ('Сидоров Сидор', 'ИСИ', 'ПГС-102', 3, 1, 0, 0),
        ('Петров Пётр', 'ИМИ', 'СБ-202', 2, 2, 1, 2),
        ('Иванов Иван', 'ИСИ', 'ПГС-101', 1, 3, 1, 2),
    ]


def test_report_slices_group_by_record_data(adapter):
    # Каждый срез — GROUP BY по полю записи; числа пересчитаны руками по
    # фикстуре из src/conftest.py (участий по возрастанию, затем значение).
    save_report_fixture(adapter)

    assert slice_rows(adapter.get_grouped_report('group')) == [
        ('ПГС-102', 1, 0, 0),
        ('ТД-303', 1, 0, 1),
        ('СБ-202', 2, 1, 2),
        ('ПГС-101', 4, 2, 3),
    ]

    assert slice_rows(adapter.get_grouped_report('institute')) == [
        ('ИМИ', 3, 1, 3),
        ('ИСИ', 5, 2, 3),
    ]

    assert slice_rows(adapter.get_grouped_report('course')) == [
        (3, 1, 0, 0),
        (2, 3, 1, 3),
        (1, 4, 2, 3),
    ]

    assert slice_rows(adapter.get_grouped_report('sport')) == [
        ('Шахматы', 1, 0, 0),
        ('Лыжи', 2, 1, 1),
        ('Бег', 5, 2, 5),
    ]

    assert slice_rows(adapter.get_grouped_report('level')) == [
        ('межвузовские', 3, 0, 2),
        ('внутривузовские', 5, 3, 4),
    ]

    assert slice_rows(adapter.get_grouped_report('year')) == [
        (2023, 2, 1, 1),
        (2024, 3, 1, 3),
        (2025, 3, 1, 2),
    ]


def test_report_slices_share_filters_with_student_slice(adapter):
    # Фильтры институт/группа/вид спорта (новые) и период (существующий)
    # применяются к любому срезу тем же WHERE.
    save_report_fixture(adapter)

    # институт+группа по иерархии: пара сузила срез «студент» до Иванова и
    # записи Козлова из ИСИ/ПГС-101 (Козлов из ИМИ не попадает).
    infos = adapter.get_filtered('', '', '', '', '', institute='ИСИ', group='ПГС-101')
    assert [(info.student_name, info.count_participation) for info in infos] == [
        ('Козлов Кирилл', 1),
        ('Иванов Иван', 3),
    ]

    # вид спорта на срезе «институт»: ИСИ по Бегу — записи 1, 2, 7; ИМИ — 4, 8.
    assert slice_rows(adapter.get_grouped_report('institute', sport='Бег')) == [
        ('ИМИ', 2, 0, 2),
        ('ИСИ', 3, 2, 3),
    ]

    # период на срезе «год»: 2024 год целиком.
    assert slice_rows(adapter.get_grouped_report('year', date_from='01.01.2024', date_to='31.12.2024')) == [
        (2024, 3, 1, 3)
    ]

    # группа без института: то же имя группы в другом институте не мешает.
    assert slice_rows(adapter.get_grouped_report('group', group='ТД-303')) == [('ТД-303', 1, 0, 1)]


def test_report_slice_supports_custom_filters(adapter):
    save_report_fixture(adapter)
    rows = adapter.get_grouped_report('group', custom_filters=[('trainer', 'text', 'Смит')])
    assert slice_rows(rows) == [('ПГС-101', 1, 1, 1)]


def test_get_grouped_report_rejects_unknown_and_student_slices(adapter):
    save_report_fixture(adapter)
    with pytest.raises(ValueError, match='Неизвестный срез'):
        adapter.get_grouped_report('bogus')
    with pytest.raises(ValueError, match='студент'):
        adapter.get_grouped_report('student')


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


def test_wipe_competitions_and_attachments(adapter):
    adapter.save_competitions(
        [
            make_competition('Первый', datetime(2026, 1, 1)),
            make_competition('Второй', datetime(2026, 2, 1)),
        ]
    )
    adapter.create_attachment(
        record_id=1,
        filename='diploma.png',
        stored_name='stored.png',
        content_type='image/png',
        size=100,
        uploaded_by=None,
    )
    adapter.create_attachment(
        record_id=2,
        filename='protocol.pdf',
        stored_name='stored.pdf',
        content_type='application/pdf',
        size=200,
        uploaded_by=None,
    )

    assert adapter.count_competitions() == 2
    assert adapter.count_attachments() == 2

    assert adapter.delete_all_attachments() == 2
    assert adapter.count_attachments() == 0
    assert adapter.count_competitions() == 2
    assert adapter.get_attachments() == []

    assert adapter.delete_all_competitions() == 2
    assert adapter.count_competitions() == 0
    assert list(adapter.get_competitions()) == []

    # Повторная очистка пустой базы — ноль удалений, без ошибок.
    assert adapter.delete_all_competitions() == 0
    assert adapter.delete_all_attachments() == 0


def test_wipe_keeps_users_levels_fields_and_audit(adapter):
    adapter.save_competitions([make_competition('Запись', datetime(2026, 1, 1))])
    adapter.create_user('admin', 'hash', 'admin')
    adapter.create_level('городские')
    adapter.create_custom_field(
        key='trainer',
        label='Тренер',
        field_type='text',
        required=False,
        show_in_table=True,
        show_in_export=True,
        show_in_template=True,
        sort_order=0,
    )
    adapter.add_audit_event(user_id=1, username='admin', action='db_wiped')

    adapter.delete_all_competitions()
    adapter.delete_all_attachments()

    assert adapter.get_user('admin') is not None
    assert 'городские' in adapter.get_level_names()
    assert [field.label for field in adapter.get_custom_fields()] == ['Тренер']
    assert [event['action'] for event in adapter.get_audit_events()] == ['db_wiped']


def test_get_sport_names_unique_sorted(adapter):
    skier = make_competition('Лыжников', datetime(2026, 1, 1))
    skier.sport = 'Лыжи'
    first_runner = make_competition('Бегунов 1', datetime(2026, 2, 1))
    second_runner = make_competition('Бегунов 2', datetime(2026, 3, 1))
    adapter.save_competitions([skier, first_runner, second_runner])

    assert adapter.get_sport_names() == ['Бег', 'Лыжи']


LEGACY_COMPETITIONS_DDL = '''
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
        created_at TEXT NOT NULL
    )
'''


def test_catalog_migration_populates_from_existing_records(tmp_path):
    import sqlite3

    db_path = tmp_path / 'legacy.sqlite3'
    connection = sqlite3.connect(db_path)
    connection.execute(LEGACY_COMPETITIONS_DDL)
    connection.executemany(
        'INSERT INTO competitions (student_id, student_name, student_sex, institute, "group",'
        ' course, sport, date, level, name, position, created_at)'
        " VALUES ('h', ?, 'М', ?, 'ПГС-101', 2, ?, '2026-01-01', 'внутривузовские', 'Кубок', 1, '2026-01-01')",
        [
            ('Первый', 'ИСИ', 'Бег'),
            ('Второй', 'ИСИ', 'Лыжи'),
            ('Третий', 'ФМА', 'Бег'),
            ('Четвёртый', 'ФМА', ''),
        ],
    )
    connection.commit()
    connection.close()

    adapter = SQLiteAdapter(str(db_path))
    tables = {row['name'] for row in adapter.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert 'catalog_values' in tables

    # уникальные значения из записей попадают в справочники, пустые — нет
    assert adapter.list_catalog('sport') == ['Бег', 'Лыжи']
    assert adapter.list_catalog('institute') == ['ИСИ', 'ФМА']


def test_catalog_populate_is_idempotent_and_keeps_hidden(tmp_path):
    db_path = str(tmp_path / 'reopened.sqlite3')
    adapter = SQLiteAdapter(db_path)
    skier = make_competition('Лыжников', datetime(2026, 1, 1))
    skier.sport = 'Лыжи'
    adapter.save_competitions([skier, make_competition('Бегунов', datetime(2026, 2, 1))])

    # повторная инициализация подхватывает значения из существующих записей
    reopened = SQLiteAdapter(db_path)
    assert reopened.list_catalog('sport') == ['Бег', 'Лыжи']

    hidden_row = next(row for row in reopened.list_catalog_all('sport') if row['value'] == 'Лыжи')
    reopened.hide_catalog_value(hidden_row['id'])

    reopened_again = SQLiteAdapter(db_path)  # populate не дублирует и не «оживляет»
    rows = reopened_again.list_catalog_all('sport')
    assert sorted(row['value'] for row in rows) == ['Бег', 'Лыжи']
    hidden_after = next(row for row in rows if row['value'] == 'Лыжи')
    assert hidden_after['active'] == 0
    assert reopened_again.list_catalog('sport') == ['Бег']


def test_add_catalog_value_ignores_duplicates(adapter):
    adapter.add_catalog_value('sport', 'Бег')
    adapter.add_catalog_value('sport', 'Бег')  # дубликат — без ошибки
    adapter.add_catalog_value('sport', '  Лыжи  ')  # обрезается пробелами
    adapter.add_catalog_value('sport', '   ')  # пустое — игнор
    adapter.add_catalog_value('institute', 'Бег')  # та же строка в другой категории допустима

    sport_rows = adapter.list_catalog_all('sport')
    assert sorted(row['value'] for row in sport_rows) == ['Бег', 'Лыжи']
    assert adapter.list_catalog('institute') == ['Бег']


def test_catalog_hide_unhide_filters_active_values(adapter):
    adapter.add_catalog_value('sport', 'Бег')
    adapter.add_catalog_value('sport', 'Лыжи')
    skier_row = next(row for row in adapter.list_catalog_all('sport') if row['value'] == 'Лыжи')

    adapter.hide_catalog_value(skier_row['id'])
    assert adapter.list_catalog('sport') == ['Бег']  # скрытое не попадает в подсказки
    all_rows = {row['value']: row['active'] for row in adapter.list_catalog_all('sport')}
    assert all_rows == {'Бег': 1, 'Лыжи': 0}

    adapter.unhide_catalog_value(skier_row['id'])
    assert adapter.list_catalog('sport') == ['Бег', 'Лыжи']


def test_count_records_using_by_category(adapter):
    skier = make_competition('Лыжников', datetime(2026, 1, 1))
    skier.sport = 'Лыжи'
    skier.institute = 'ФМА'
    adapter.save_competitions(
        [
            skier,
            make_competition('Бегунов 1', datetime(2026, 2, 1)),
            make_competition('Бегунов 2', datetime(2026, 3, 1)),
        ]
    )

    assert adapter.count_records_using('sport', 'Бег') == 2
    assert adapter.count_records_using('sport', 'Лыжи') == 1
    assert adapter.count_records_using('institute', 'ИСИ') == 2
    assert adapter.count_records_using('institute', 'ФМА') == 1
    assert adapter.count_records_using('level', 'внутривузовские') == 3
    assert adapter.count_records_using('sport', 'Плавание') == 0
    assert adapter.count_records_using('unknown', 'Бег') == 0


def test_get_and_delete_catalog_value(adapter):
    adapter.add_catalog_value('institute', 'ИСИ')
    row = adapter.get_catalog_value(adapter.list_catalog_all('institute')[0]['id'])
    assert row == {'id': row['id'], 'category': 'institute', 'value': 'ИСИ', 'parent_id': None, 'active': 1}
    assert adapter.get_catalog_value(999) is None

    adapter.delete_catalog_value(row['id'])
    assert adapter.list_catalog_all('institute') == []
    assert adapter.get_catalog_value(row['id']) is None


# --- Иерархия справочника: институты содержат группы (№15) ---


LEGACY_CATALOG_DDL = '''
    CREATE TABLE catalog_values (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        value TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        UNIQUE (category, value)
    )
'''


def seed_legacy_catalog_records(connection, rows):
    connection.executemany(
        'INSERT INTO competitions (student_id, student_name, student_sex, institute, "group",'
        ' course, sport, date, level, name, position, created_at)'
        " VALUES ('h', ?, 'М', ?, ?, 2, 'Бег', '2026-01-01', 'внутривузовские', 'Кубок', 1, '2026-01-01')",
        rows,
    )


def test_catalog_hierarchy_migration_populates_pairs(tmp_path):
    import sqlite3

    db_path = tmp_path / 'legacy-catalog.sqlite3'
    connection = sqlite3.connect(db_path)
    connection.execute(LEGACY_COMPETITIONS_DDL)
    seed_legacy_catalog_records(
        connection,
        [
            ('Первый', 'ИСИ', 'ПГС-101'),
            ('Второй', 'ИСИ', 'ПГС-101'),  # дубль пары — группа одна
            ('Третий', 'ИСИ', 'ПГС-102'),
            ('Четвёртый', 'ФМА', 'ПГС-101'),  # одноимённая группа другого института
            ('Пятый', '', 'ПГС-777'),  # без института пара не создаётся
        ],
    )
    connection.execute(LEGACY_CATALOG_DDL)
    connection.execute(
        "INSERT INTO catalog_values (category, value, active, created_at) VALUES ('sport', 'Бег', 1, '2026-01-01')"
    )
    connection.execute(
        "INSERT INTO catalog_values (category, value, active, created_at) VALUES ('institute', 'ФМА', 1, '2026-01-01')"
    )
    connection.commit()
    connection.close()

    adapter = SQLiteAdapter(str(db_path))
    columns = {row['name'] for row in adapter.connection.execute('PRAGMA table_info(catalog_values)')}
    assert 'parent_id' in columns

    # плоские легаси-значения пережили миграцию без изменений
    sport_rows = adapter.list_catalog_all('sport')
    assert [row['value'] for row in sport_rows] == ['Бег']
    assert all(row['parent_id'] is None for row in sport_rows)

    tree = {inst['value']: {group['value'] for group in inst['groups']} for inst in adapter.list_catalog_tree()}
    assert tree == {'ИСИ': {'ПГС-101', 'ПГС-102'}, 'ФМА': {'ПГС-101'}}

    # каждая группа смотрит на запись своего института
    for institute in adapter.list_catalog_tree():
        for group in institute['groups']:
            row = adapter.get_catalog_value(group['id'])
            parent = adapter.get_catalog_value(row['parent_id'])
            assert row['category'] == 'group'
            assert parent['value'] == institute['value']

    # повторная инициализация не дублирует пары
    reopened = SQLiteAdapter(str(db_path))
    tree_again = {inst['value']: {group['value'] for group in inst['groups']} for inst in reopened.list_catalog_tree()}
    assert tree_again == tree


def test_ensure_catalog_pair_builds_hierarchy(adapter):
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-101')
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-101')  # идемпотентно
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-102')
    adapter.ensure_catalog_pair('ФМА', 'ПГС-101')  # одноимённая группа другого института
    adapter.ensure_catalog_pair('', 'ПГС-777')  # без института — игнор
    adapter.ensure_catalog_pair('АДИ', '')  # без группы — игнор

    tree = {inst['value']: {group['value'] for group in inst['groups']} for inst in adapter.list_catalog_tree()}
    assert tree == {'ИСИ': {'ПГС-101', 'ПГС-102'}, 'ФМА': {'ПГС-101'}}

    # уникальность (parent, value): прямые вставки тоже не дублируют
    isi_id = adapter.list_catalog_tree()[0]['id']
    adapter.add_catalog_value('group', 'ПГС-101', parent_id=isi_id)
    adapter.add_catalog_value('group', 'ПГС-101', parent_id=isi_id)
    group_rows = [row for row in adapter.list_catalog_all('group') if row['parent_id'] == isi_id]
    assert [row['value'] for row in group_rows] == ['ПГС-101', 'ПГС-102']


def test_group_options_by_institute_respect_hidden(adapter):
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-101')
    adapter.ensure_catalog_pair('ФМА', 'ПГС-201')
    assert adapter.get_group_options_by_institute() == {'ИСИ': ['ПГС-101'], 'ФМА': ['ПГС-201']}

    isi = next(inst for inst in adapter.list_catalog_tree() if inst['value'] == 'ИСИ')
    hidden_group = next(group for group in isi['groups'] if group['value'] == 'ПГС-101')
    adapter.hide_catalog_value(hidden_group['id'])

    # скрытая группа уходит из подсказок, но остаётся в дереве админки
    assert adapter.get_group_options_by_institute() == {'ФМА': ['ПГС-201']}
    isi_after = next(inst for inst in adapter.list_catalog_tree() if inst['value'] == 'ИСИ')
    assert any(group['value'] == 'ПГС-101' and not group['active'] for group in isi_after['groups'])


def test_count_records_using_group_counts_pairs(adapter):
    first = make_competition('Первый', datetime(2026, 1, 1))  # ИСИ / ПГС-101
    second = make_competition('Второй', datetime(2026, 1, 2))  # ИСИ / ПГС-101
    third = make_competition('Третий', datetime(2026, 1, 3))  # ИСИ / ПГС-999
    third.group = 'ПГС-999'
    fourth = make_competition('Четвёртый', datetime(2026, 1, 4))  # ФМА / ПГС-101
    fourth.institute = 'ФМА'  # та же группа, другой институт
    adapter.save_competitions([first, second, third, fourth])

    assert adapter.count_records_using('group', 'ПГС-101', parent_value='ИСИ') == 2
    assert adapter.count_records_using('group', 'ПГС-101', parent_value='ФМА') == 1
    assert adapter.count_records_using('group', 'ПГС-101') == 3  # плоское вхождение
    assert adapter.count_records_using('institute', 'ИСИ') == 3


def test_count_child_groups_for_institute_delete_guard(adapter):
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-101')
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-102')
    adapter.add_catalog_value('institute', 'ФМА')  # институт без групп

    isi = next(inst for inst in adapter.list_catalog_tree() if inst['value'] == 'ИСИ')
    assert adapter.count_child_groups(isi['id']) == 2

    adapter.delete_catalog_value(isi['groups'][0]['id'])
    assert adapter.count_child_groups(isi['id']) == 1

    fma_id = next(inst for inst in adapter.list_catalog_tree() if inst['value'] == 'ФМА')['id']
    assert adapter.count_child_groups(fma_id) == 0


# --- Присутствие пользователей: last_login_at / last_seen_at (№17) ---


def test_users_presence_columns_migrated(tmp_path):
    import sqlite3

    db_path = tmp_path / 'legacy-users.sqlite3'
    connection = sqlite3.connect(db_path)
    connection.execute(
        '''
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            pwd_ver INTEGER NOT NULL DEFAULT 0
        )
        '''
    )
    connection.execute("INSERT INTO users (username, password_hash, role) VALUES ('admin', 'x', 'admin')")
    connection.commit()
    connection.close()

    adapter = SQLiteAdapter(str(db_path))
    columns = {row['name'] for row in adapter.connection.execute('PRAGMA table_info(users)')}
    assert {'last_login_at', 'last_seen_at'} <= columns


def test_set_user_last_login_writes_login_and_seen(adapter):
    adapter.create_user('ann', 'hash', 'viewer')
    user_id = adapter.get_user('ann')['id']
    assert adapter.list_users()[0]['last_login_at'] is None

    adapter.set_user_last_login(user_id, when=datetime(2026, 9, 13, 10, 0, 0))
    user = adapter.list_users()[0]
    assert user['last_login_at'] == '2026-09-13T10:00:00'
    assert user['last_seen_at'] == '2026-09-13T10:00:00'


def test_touch_user_seen_throttled_to_one_update(adapter):
    adapter.create_user('bob', 'hash', 'viewer')
    user_id = adapter.get_user('bob')['id']

    assert adapter.touch_user_seen(user_id) is True
    first_seen = adapter.list_users()[0]['last_seen_at']
    assert first_seen is not None

    # второй вызов подряд в пределах окна — UPDATE не происходит
    assert adapter.touch_user_seen(user_id) is False
    assert adapter.list_users()[0]['last_seen_at'] == first_seen

    # когда last_seen старше окна троттлинга — обновляется снова
    adapter.connection.execute("UPDATE users SET last_seen_at = '2020-01-01T00:00:00'")
    adapter.connection.commit()
    assert adapter.touch_user_seen(user_id) is True
    assert adapter.list_users()[0]['last_seen_at'] != '2020-01-01T00:00:00'


def test_list_users_includes_name_aliases(adapter):
    adapter.create_user('anna', 'hash', 'athlete')
    user_id = adapter.get_user('anna')['id']
    adapter.add_name_alias(user_id, 'Иванова Анна Петровна')

    users = adapter.list_users()
    assert len(users) == 1
    assert users[0]['username'] == 'anna'
    assert users[0]['role'] == 'athlete'
    assert users[0]['active'] == 1
    assert users[0]['name_aliases'] == ['Иванова Анна Петровна']


# Удаление пользователя: записи — исторические факты, остаются с owner_id
# NULL; аккаунт и его псевдонимы ФИО удаляются (docs/data-model-decisions.md).
def test_delete_user_detaches_records_and_removes_account(adapter):
    adapter.create_user('sportik', 'hash', 'athlete')
    user_id = adapter.get_user('sportik')['id']
    adapter.add_name_alias(user_id, 'Спортсменов Спорт Спортович')
    adapter.save_competitions(
        [
            make_competition('Спортсменов Спорт Спортович', datetime(2026, 1, 1)),
            make_competition('Спортсменов Спорт Спортович', datetime(2026, 2, 1)),
        ],
        owner_id=user_id,
    )

    assert adapter.count_records_by_owner(user_id) == 2

    detached = adapter.delete_user(user_id)

    assert detached == 2
    assert adapter.get_user('sportik') is None
    assert adapter.get_user_by_id(user_id) is None
    assert adapter.get_name_aliases(user_id) == []
    assert not any(user['username'] == 'sportik' for user in adapter.list_users())
    # Записи живы, но отвязаны от владельца.
    assert adapter.count_competitions() == 2
    assert adapter.count_records_by_owner(user_id) == 0
    owners = [row['owner_id'] for row in adapter.connection.execute('SELECT owner_id FROM competitions')]
    assert owners == [None, None]


def test_delete_user_keeps_other_owners_records(adapter):
    adapter.create_user('sportik', 'hash', 'athlete')
    adapter.create_user('operator', 'hash', 'editor')
    sportik_id = adapter.get_user('sportik')['id']
    operator_id = adapter.get_user('operator')['id']
    adapter.save_competitions(
        [make_competition('Спортсменов Спорт Спортович', datetime(2026, 1, 1))], owner_id=sportik_id
    )
    adapter.save_competitions([make_competition('Чужой Студент', datetime(2026, 2, 1))], owner_id=operator_id)

    detached = adapter.delete_user(sportik_id)

    assert detached == 1
    assert adapter.get_user('operator') is not None
    assert adapter.count_records_by_owner(operator_id) == 1
    assert adapter.count_competitions() == 2


def test_count_records_by_owner_zero_for_missing_user(adapter):
    assert adapter.count_records_by_owner(9999) == 0


def test_delete_user_without_records_returns_zero(adapter):
    adapter.create_user('viewer1', 'hash', 'viewer')
    user_id = adapter.get_user('viewer1')['id']
    assert adapter.delete_user(user_id) == 0
    assert adapter.get_user('viewer1') is None


def test_date_wipe_selects_by_competition_date(adapter):
    # Критерий — дата соревнования, не дата создания записи: записи с разными
    # датами соревнований должны отбираться по ней (docs/data-model-decisions.md).
    adapter.save_competitions(
        [
            make_competition('Старый декабрьский', datetime(2024, 12, 31)),
            make_competition('Новый июньский', datetime(2025, 6, 1)),
            make_competition('Граничный день', datetime(2025, 6, 1)),
        ]
    )
    cutoff = datetime(2025, 1, 1)

    assert adapter.count_competitions(date_before=cutoff) == 1
    assert [comp.student_name for comp in adapter.get_competitions_before(cutoff)] == ['Старый декабрьский']

    assert adapter.delete_competitions_before(cutoff) == 1
    assert [comp.student_name for comp in adapter.get_competitions()] == [
        'Новый июньский',
        'Граничный день',
    ]
    assert adapter.count_competitions() == 2


def test_date_wipe_boundary_is_strictly_before(adapter):
    adapter.save_competitions(
        [
            make_competition('День назад', datetime(2025, 5, 31)),
            make_competition('Сама дата', datetime(2025, 6, 1)),
        ]
    )
    # «ДО 01.06.2025» — строго раньше: сама граница не удаляется
    assert adapter.delete_competitions_before(datetime(2025, 6, 1)) == 1
    assert [comp.student_name for comp in adapter.get_competitions()] == ['Сама дата']


def test_attachments_for_records_select_and_delete(adapter):
    adapter.save_competitions([make_competition('Первый', datetime(2026, 1, 1))])
    adapter.create_attachment(
        record_id=1,
        filename='a.png',
        stored_name='a.png',
        content_type='image/png',
        size=10,
        uploaded_by=None,
    )
    adapter.create_attachment(
        record_id=2,
        filename='b.png',
        stored_name='b.png',
        content_type='image/png',
        size=20,
        uploaded_by=None,
    )
    adapter.create_attachment(
        record_id=1,
        filename='c.png',
        stored_name='c.png',
        content_type='image/png',
        size=30,
        uploaded_by=None,
    )

    mine = adapter.get_attachments_for_records([1])
    assert [attachment['stored_name'] for attachment in mine] == ['a.png', 'c.png']
    assert adapter.get_attachments_for_records([]) == []
    assert adapter.get_attachments_for_records([999]) == []

    assert adapter.delete_attachments_for_records([1]) == 2
    assert adapter.count_attachments() == 1
    assert adapter.delete_attachments_for_records([]) == 0


def insert_audit_event(adapter, created_at, username, action):
    adapter.connection.execute(
        'INSERT INTO audit_log (created_at, user_id, username, action, details) VALUES (?, ?, ?, ?, ?)',
        (created_at, None, username, action, ''),
    )
    adapter.connection.commit()


def test_audit_filters_and_pagination(adapter):
    insert_audit_event(adapter, '2026-01-05T10:00:00.000000', 'anna', 'login_success')
    insert_audit_event(adapter, '2026-02-10T10:00:00.000000', 'bob', 'db_wiped')
    insert_audit_event(adapter, '2026-03-15T10:00:00.000000', 'anna', 'db_wiped')

    assert adapter.count_audit_events() == 3
    assert adapter.count_audit_events(username='anna') == 2
    assert adapter.count_audit_events(action='db_wiped') == 2
    assert adapter.count_audit_events(username='anna', action='db_wiped') == 1
    assert adapter.count_audit_events(date_from='2026-02-01') == 2
    assert adapter.count_audit_events(date_to='2026-01-31') == 1
    assert adapter.count_audit_events(date_to='2026-02-28') == 2  # верхняя граница: январь+февраль
    assert adapter.count_audit_events(date_from='2026-01-01', date_to='2026-03-31') == 3
    assert adapter.count_audit_events(username='carol') == 0

    page_one = adapter.get_audit_events(limit=2, offset=0)
    page_two = adapter.get_audit_events(limit=2, offset=2)
    assert [event['id'] for event in page_one] == [3, 2]
    assert [event['id'] for event in page_two] == [1]

    filtered = adapter.get_audit_events(limit=50, offset=0, username='anna', action='db_wiped')
    assert [event['id'] for event in filtered] == [3]
    assert (
        adapter.get_audit_events(limit=50, offset=0, date_from='2026-02-01', date_to='2026-02-28')[0]['username']
        == 'bob'
    )

    assert adapter.list_audit_actions() == ['db_wiped', 'login_success']


def test_vacuum_shrinks_database_file_after_mass_delete(tmp_path):
    from pathlib import Path

    db_path = str(tmp_path / 'vacuum.sqlite3')
    adapter = SQLiteAdapter(db_path)

    def file_size():
        adapter.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return Path(db_path).stat().st_size

    adapter.save_competitions(
        [make_competition(f'Спортсменов Номер {index}', datetime(2025, index % 12 + 1, 1)) for index in range(300)]
    )
    size_before_delete = file_size()

    adapter.delete_all_competitions()
    size_after_delete = file_size()
    assert size_after_delete >= size_before_delete * 0.9  # DELETE сам файл не сжимает

    adapter.vacuum()
    size_after_vacuum = file_size()
    assert size_after_vacuum < size_before_delete * 0.5  # место реально освобождено
    assert adapter.count_competitions() == 0


def make_import_row(name: str, index: int, **overrides) -> Competition:
    values = dict(
        student_id=f'id-{name}',
        student_name=name,
        student_sex='М',
        institute=f'ИПК-{index % 7}',
        group=f'Г-{index % 40}',
        course=1 + index % 4,
        sport=f'Спорт-{index % 13}',
        date=datetime(2026, 1 + index % 12, 1 + index % 28),
        level='внутривузовские' if index % 3 else 'межвузовские',
        name=f'Турнир {index % 17}',
        position=1 + index % 10,
    )
    values.update(overrides)
    return Competition(**values)


def test_import_competitions_autofills_levels_and_catalogs(adapter):
    rows = [make_import_row(f'Студентов Студент {index:05d}', index) for index in range(20)]
    # Строка-дубль не вставляется, но её значения попадают в справочники —
    # семантика бывших ensure_levels/ensure_catalog_values из src/main.py.
    duplicate_row = make_import_row('Дублей Дублий Дублиевич', 3, sport='Уникальный спорт')
    new_rows = rows[:10]

    adapter.import_competitions([*rows, duplicate_row], new_rows, owner_id=7)

    assert adapter.count_competitions() == len(new_rows)
    saved = adapter.get_competitions()
    assert {row.student_name for row in saved} == {row.student_name for row in new_rows}
    owners = {row['owner_id'] for row in adapter.connection.execute('SELECT DISTINCT owner_id FROM competitions')}
    assert owners == {7}

    levels = set(adapter.get_level_names(include_inactive=True))
    assert {'внутривузовские', 'межвузовские'} <= levels
    sports = set(adapter.list_catalog('sport'))
    assert {f'Спорт-{index % 13}' for index in range(20)} <= sports
    assert 'Уникальный спорт' in sports  # значение из строки-дубля тоже в справочнике
    institutes = set(adapter.list_catalog('institute'))
    assert {f'ИПК-{index % 7}' for index in range(20)} <= institutes
    # Пары институт→группа сложены иерархией: группа под своим институтом.
    options = adapter.get_group_options_by_institute()
    assert 'Г-3' in options['ИПК-3']


def test_import_competitions_is_atomic_on_failure(adapter, monkeypatch):
    rows = [make_import_row(f'Студентов Студент {index:05d}', index) for index in range(3)]

    # sqlite3.Connection не позволяет подменить метод, поэтому оборачиваем
    # соединение прокси на адаптере: записи соревнований падают посередине.
    class MidBatchFailureConnection:
        def __init__(self, connection):
            self._connection = connection

        def executemany(self, sql, records):
            if 'INSERT INTO competitions' in sql:
                # Часть строк уже вставлена, когда импорт падает.
                self._connection.executemany(sql, list(records)[:-1])
                raise sqlite3.IntegrityError('boom mid-batch')
            return self._connection.executemany(sql, records)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    monkeypatch.setattr(adapter, 'connection', MidBatchFailureConnection(adapter.connection))
    with pytest.raises(sqlite3.IntegrityError):
        adapter.import_competitions(rows, rows)
    monkeypatch.undo()

    # Либо все строки импорта, либо ничего: откатились и записи, и справочники.
    assert adapter.count_competitions() == 0
    assert 'Спорт-0' not in adapter.list_catalog('sport')
    assert 'ИПК-0' not in adapter.list_catalog('institute')
    assert 'внутривузовские' not in adapter.get_level_names(include_inactive=True)

    # Адаптер жив: следующий импорт проходит целиком.
    adapter.import_competitions(rows, rows)
    assert adapter.count_competitions() == len(rows)


def test_import_competitions_commits_once_for_500_rows(adapter, monkeypatch):
    rows = [make_import_row(f'Студентов Студент {index:05d}', index) for index in range(500)]

    class CountingCommitConnection:
        def __init__(self, connection):
            self._connection = connection
            self.commits = 0

        def commit(self):
            self.commits += 1
            return self._connection.commit()

        def __getattr__(self, name):
            return getattr(self._connection, name)

    counting = CountingCommitConnection(adapter.connection)
    monkeypatch.setattr(adapter, 'connection', counting)

    adapter.import_competitions(rows, rows)

    # Ориентир производительности без хрупких таймингов: один импорт — один
    # COMMIT (раньше справочники коммитились на каждую строку).
    assert counting.commits == 1
    assert adapter.count_competitions() == 500
