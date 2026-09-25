import hashlib
import sqlite3
from datetime import datetime

import pytest

from src.conftest import FailingStudentsDeleteConnection
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
    # P3: review несёт и стабильную связь — ветка ref в user_owns_record.
    assert review == {
        'id': record_id,
        'review_status': 'pending',
        'owner_id': 7,
        'student_id': expected_hash,
        'student_ref_id': None,
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

    # Переименование уровня — единый механизм справочников (решение 2026-09-13)
    updated = adapter.rename_catalog_value('level', 'внутривузовские', 'внутривузовские (обновлено)')
    assert updated == 0  # записей с этим уровнем в базе нет
    assert 'внутривузовские (обновлено)' in adapter.get_level_names()
    adapter.disable_level(2)
    assert 'межвузовские' not in adapter.get_level_names()
    assert 'межвузовские' in adapter.get_level_names(include_inactive=True)


def test_hard_delete_level(adapter):
    from src.main import DEFAULT_LEVELS, seed_levels

    seed_levels(adapter)
    adapter.create_level('пустой уровень')
    empty_id = next(item['id'] for item in adapter.list_levels() if item['name'] == 'пустой уровень')
    adapter.hard_delete_level(empty_id)
    assert 'пустой уровень' not in adapter.get_level_names(include_inactive=True)

    # Уровень с записями: счётчик > 0 — отказ решает роут; hard_delete_level
    # сам по себе просто удаляет строку таблицы levels.
    adapter.save_competitions([make_competition('Студент', datetime(2026, 1, 1))])
    used_level = DEFAULT_LEVELS[0]
    assert adapter.count_records_using('level', used_level) > 0


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


def test_find_unique_group_institute(adapter):
    # №19a: институт по группе подставляется только при однозначном имени.
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-101')
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-102')
    adapter.ensure_catalog_pair('ФМА', 'ПГС-101')  # одноимённая группа другого института
    adapter.ensure_catalog_pair('АДИ', 'ША-777')

    # Однозначная группа (регистр не важен) — канонический институт.
    assert adapter.find_unique_group_institute('пгс-102') == 'ИСИ'
    assert adapter.find_unique_group_institute('ША-777') == 'АДИ'
    # Одно имя в разных институтах — неоднозначно, None.
    assert adapter.find_unique_group_institute('ПГС-101') is None
    # Неизвестная и пустая группа — None.
    assert adapter.find_unique_group_institute('НЕТ-ТАКОЙ') is None
    assert adapter.find_unique_group_institute('') is None
    assert adapter.find_unique_group_institute('   ') is None


def test_find_unique_group_institute_ignores_hidden(adapter):
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-101')
    adapter.ensure_catalog_pair('ФМА', 'ПГС-201')
    isi = next(inst for inst in adapter.list_catalog_tree() if inst['value'] == 'ИСИ')
    hidden_group = next(group for group in isi['groups'] if group['value'] == 'ПГС-101')
    adapter.hide_catalog_value(hidden_group['id'])
    assert adapter.find_unique_group_institute('ПГС-101') is None


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


# --- Переименование значений справочников (решение 2026-09-13) ---


def test_rename_level_updates_table_and_records(adapter):
    regional = make_competition('Регионов Регион', datetime(2026, 1, 1))
    regional.level = 'региональные'
    urban = make_competition('Городов Город', datetime(2026, 2, 1))
    urban.level = 'городские'
    adapter.save_competitions([regional, urban, make_competition('Базов Базовый', datetime(2026, 3, 1))])
    adapter.create_level('региональные')

    updated = adapter.rename_catalog_value('level', 'региональные', 'областные')

    # И таблица уровней, и записи (уровень в записях — текст)
    assert updated == 1
    names = set(adapter.get_level_names(include_inactive=True))
    assert 'областные' in names
    assert 'региональные' not in names
    levels_in_records = {comp.level for comp in adapter.get_competitions()}
    assert levels_in_records == {'областные', 'городские', 'внутривузовские'}


def test_rename_group_updates_records_only_within_institute(adapter):
    first = make_competition('Первый', datetime(2026, 1, 1))  # ИСИ / ПГС-101
    second = make_competition('Второй', datetime(2026, 1, 2))  # ИСИ / ПГС-101
    third = make_competition('Третий', datetime(2026, 1, 3))  # ФМА / ПГС-101 — та же группа, другой институт
    third.institute = 'ФМА'
    adapter.save_competitions([first, second, third])
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-101')
    adapter.ensure_catalog_pair('ФМА', 'ПГС-101')

    updated = adapter.rename_catalog_value('group', 'ПГС-101', 'ПГС-101А', parent_value='ИСИ')

    assert updated == 2
    # Справочник: пара (ИСИ+ПГС-101) переименована, одноимённая группа ФМА не тронута
    tree = {inst['value']: {group['value'] for group in inst['groups']} for inst in adapter.list_catalog_tree()}
    assert tree == {'ИСИ': {'ПГС-101А'}, 'ФМА': {'ПГС-101'}}
    # Записи: обновлены только записи института ИСИ
    pairs_in_records = {(comp.institute, comp.group) for comp in adapter.get_competitions()}
    assert pairs_in_records == {('ИСИ', 'ПГС-101А'), ('ФМА', 'ПГС-101')}


def test_rename_without_update_records_leaves_records(adapter):
    skier = make_competition('Лыжников Лыжник', datetime(2026, 1, 1))
    skier.sport = 'Лыжи'
    adapter.save_competitions([skier])
    adapter.add_catalog_value('sport', 'Лыжи')

    updated = adapter.rename_catalog_value('sport', 'Лыжи', 'Горные лыжи', update_records=False)

    assert updated == 0
    assert adapter.list_catalog('sport') == ['Горные лыжи']
    assert {comp.sport for comp in adapter.get_competitions()} == {'Лыжи'}


def test_rename_rejects_conflicts_and_missing_values(adapter):
    adapter.add_catalog_value('sport', 'Бег')
    adapter.add_catalog_value('sport', 'Лыжи')
    adapter.save_competitions([make_competition('Бегунов Бегун', datetime(2026, 1, 1))])  # спорт «Бег»
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-101')
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-102')
    adapter.create_level('городские')
    adapter.create_level('региональные')

    # конфликт с существующим значением той же категории
    with pytest.raises(ValueError, match='уже есть'):
        adapter.rename_catalog_value('sport', 'Бег', 'Лыжи')
    # конфликт уровней
    with pytest.raises(ValueError, match='уже существует'):
        adapter.rename_catalog_value('level', 'городские', 'региональные')
    # конфликт групп — только внутри своего института
    with pytest.raises(ValueError, match='уже есть'):
        adapter.rename_catalog_value('group', 'ПГС-101', 'ПГС-102', parent_value='ИСИ')
    # одноимённая группа другого института — другое значение справочника,
    # конфликта нет (уникальность (parent, value), а не просто value)
    adapter.ensure_catalog_pair('ФМА', 'ПГС-101')
    assert adapter.rename_catalog_value('group', 'ПГС-101', 'ПГС-102', parent_value='ФМА') == 0

    # отсутствующие значения и мусорные аргументы
    with pytest.raises(ValueError):
        adapter.rename_catalog_value('sport', 'Плавание', 'Прыжки')
    with pytest.raises(ValueError):
        adapter.rename_catalog_value('level', 'несуществующий', 'новый')
    with pytest.raises(ValueError):
        adapter.rename_catalog_value('group', 'ПГС-101', 'ПГС-103')  # без института
    with pytest.raises(ValueError):
        adapter.rename_catalog_value('group', 'ПГС-101', 'ПГС-103', parent_value='АДИ')  # институт не найден
    with pytest.raises(ValueError):
        adapter.rename_catalog_value('sport', 'Бег', 'Бег')  # то же имя
    with pytest.raises(ValueError):
        adapter.rename_catalog_value('sport', 'Бег', '   ')  # пустое имя
    with pytest.raises(ValueError):
        adapter.rename_catalog_value('unknown', 'Бег', 'Прыжки')

    # Отказ ничего не меняет: справочник и записи как были
    assert {row['value'] for row in adapter.list_catalog_all('sport')} == {'Бег', 'Лыжи'}
    assert {comp.sport for comp in adapter.get_competitions()} == {'Бег'}
    assert set(adapter.get_level_names(include_inactive=True)) >= {'городские', 'региональные'}


def test_import_after_rename_with_update_does_not_resurrect_old_value(adapter):
    """Инцидент 2026-09-13: импорт возвращал переименованный уровень.

    Справочники сеются из записей: после переименования с обновлением в
    записях старого значения нет, поэтому повторный импорт того же файла
    (все строки — дубли, вставки нет) его не возвращает. Файл с реально
    новыми строками и старым уровнем — вернёт: значение снова есть в записях.
    """
    rows = [make_import_row('Регионов Регион Регионович', 0, level='региональные')]
    adapter.import_competitions(rows)
    assert 'региональные' in adapter.get_level_names(include_inactive=True)

    assert adapter.rename_catalog_value('level', 'региональные', 'областные') == 1

    adapter.import_competitions([])  # повторный импорт: все строки — дубли
    names = set(adapter.get_level_names(include_inactive=True))
    assert 'региональные' not in names
    assert 'областные' in names

    fresh = [make_import_row('Новенький Новый Новичкович', 4, level='региональные')]
    adapter.import_competitions(fresh)
    assert 'региональные' in adapter.get_level_names(include_inactive=True)


# --- Перенос группы в другой институт (решение 2026-09-22) ---


def _catalog_ids(adapter):
    isi = adapter.find_catalog_row('institute', 'ИСИ')
    fma = adapter.find_catalog_row('institute', 'ФМА')
    group = adapter.find_catalog_row('group', 'ПГС-101', parent_id=isi['id'])
    return isi, fma, group


def test_count_students_using_group_pair(adapter):
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-101')
    adapter.create_student('Студентов Студент', 'М', 'ИСИ', 'ПГС-101', '2')
    adapter.create_student('Другов Друг', 'Ж', 'ФМА', 'ПГС-101', '1')
    adapter.create_student('Пустов Пуст', 'Ж', '', 'ПГС-101', '3')

    assert adapter.count_students_using_group_pair('ПГС-101', 'ИСИ') == 1
    assert adapter.count_students_using_group_pair('ПГС-101', 'ФМА') == 1
    assert adapter.count_students_using_group_pair('ПГС-101', 'АДИ') == 0
    assert adapter.count_students_using_group_pair('', 'ИСИ') == 0
    assert adapter.count_students_using_group_pair('ПГС-101', '') == 0


def test_move_catalog_group_updates_hierarchy_records_and_students(adapter):
    """Счастливый путь: справочник + записи + карточки одной транзакцией.
    Цель — институт БЕЗ одноимённой группы (иначе конфликт). Отбор — точная
    пара институт+группа: одноимённая группа другого института и карточки
    без института не затрагиваются."""
    moved = make_competition('Переносимой Перенос', datetime(2026, 1, 1))  # ИСИ / ПГС-101
    same_name_other = make_competition('Другой Друг', datetime(2026, 1, 2))  # ФМА / ПГС-101
    same_name_other.institute = 'ФМА'
    adapter.save_competitions([moved, same_name_other])
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-101')
    adapter.ensure_catalog_pair('ФМА', 'ПГС-101')  # одноимённая группа в другом институте
    adapter.add_catalog_value('institute', 'АДИ')  # цель переноса — без ПГС-101
    adapter.create_student('Студентов Студент', 'М', 'ИСИ', 'ПГС-101', '2')
    adapter.create_student('Другов Друг', 'Ж', 'ФМА', 'ПГС-101', '1')
    adapter.create_student('Пустов Пуст', 'Ж', '', 'ПГС-101', '3')

    isi, _, group = _catalog_ids(adapter)
    adi = adapter.find_catalog_row('institute', 'АДИ')
    counts = adapter.move_catalog_group(group['id'], adi['id'])

    assert counts == {'students_updated': 1, 'competitions_updated': 1}
    # Иерархия: группа теперь под АДИ; у ИСИ групп нет, ФМА не тронута
    tree = {inst['value']: {item['value'] for item in inst['groups']} for inst in adapter.list_catalog_tree()}
    assert tree == {'АДИ': {'ПГС-101'}, 'ИСИ': set(), 'ФМА': {'ПГС-101'}}
    # Записи: переносимая — АДИ, одноимённая чужая осталась в ФМА
    pairs = {(comp.institute, comp.group) for comp in adapter.get_competitions()}
    assert pairs == {('АДИ', 'ПГС-101'), ('ФМА', 'ПГС-101')}
    # Карточки: переносимая — АДИ, остальные не тронуты
    by_name = {row['full_name']: row['institute'] for row in adapter.list_students()}
    assert by_name['Студентов Студент'] == 'АДИ'
    assert by_name['Другов Друг'] == 'ФМА'
    assert by_name['Пустов Пуст'] == ''
    # Счётчики пар после переноса
    assert adapter.count_students_using_group_pair('ПГС-101', 'АДИ') == 1


def test_move_catalog_group_duplicate_in_target_rolls_back(adapter):
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-101')
    adapter.ensure_catalog_pair('ФМА', 'ПГС-101')  # конфликт имени в цели
    adapter.save_competitions([make_competition('Переносимой Перенос', datetime(2026, 1, 1))])
    adapter.create_student('Студентов Студент', 'М', 'ИСИ', 'ПГС-101', '2')

    isi, fma, group = _catalog_ids(adapter)
    catalog_before = adapter.connection.execute(
        'SELECT id, category, value, parent_id FROM catalog_values ORDER BY id'
    ).fetchall()
    students_before = adapter.connection.execute(
        'SELECT id, institute, group_name FROM students ORDER BY id'
    ).fetchall()

    with pytest.raises(ValueError, match='уже есть'):
        adapter.move_catalog_group(group['id'], fma['id'])

    assert (
        catalog_before
        == adapter.connection.execute(
            'SELECT id, category, value, parent_id FROM catalog_values ORDER BY id'
        ).fetchall()
    )
    assert (
        students_before
        == adapter.connection.execute('SELECT id, institute, group_name FROM students ORDER BY id').fetchall()
    )
    assert adapter.get_competitions()[0].institute == 'ИСИ'


def test_move_catalog_group_rejects_unknown_and_same_institute(adapter):
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-101')
    adapter.ensure_catalog_pair('ФМА', 'ТД-303')
    isi, fma, group = _catalog_ids(adapter)

    # Тот же институт
    with pytest.raises(ValueError, match='уже относится'):
        adapter.move_catalog_group(group['id'], isi['id'])
    # Неизвестная группа
    with pytest.raises(ValueError, match='не найдены'):
        adapter.move_catalog_group(999999, fma['id'])
    # Цель — не институт
    td = adapter.find_catalog_row('group', 'ТД-303', parent_id=fma['id'])
    with pytest.raises(ValueError, match='не найдены'):
        adapter.move_catalog_group(group['id'], td['id'])
    # Цель не существует
    with pytest.raises(ValueError, match='не найдены'):
        adapter.move_catalog_group(group['id'], 999999)
    # «Группа» без родителя (id института как группа)
    with pytest.raises(ValueError, match='не найдены'):
        adapter.move_catalog_group(isi['id'], fma['id'])


def test_move_catalog_group_hidden_group_movable(adapter):
    """Скрытая группа переносится: активность — свойство видимости, не прав."""
    adapter.ensure_catalog_pair('ИСИ', 'ПГС-101')
    adapter.ensure_catalog_pair('ФМА', 'ТД-303')
    isi, fma, group = _catalog_ids(adapter)
    adapter.hide_catalog_value(group['id'])

    counts = adapter.move_catalog_group(group['id'], fma['id'])

    assert counts == {'students_updated': 0, 'competitions_updated': 0}
    moved_row = adapter.get_catalog_value(group['id'])
    assert moved_row['parent_id'] == fma['id'] and moved_row['active'] == 0
    # Дерево показывает и скрытые группы: ПГС-101 теперь под ФМА (скрыта)
    tree = {inst['value']: {item['value'] for item in inst['groups']} for inst in adapter.list_catalog_tree()}
    assert tree == {'ИСИ': set(), 'ФМА': {'ПГС-101', 'ТД-303'}}
    assert 'ПГС-101' not in adapter.get_group_options_by_institute().get('ФМА', [])


# --- Файл положения события календаря: миграция калонок regulation_* ---


def test_calendar_events_regulation_columns_migrated(tmp_path):
    import sqlite3

    db_path = tmp_path / 'legacy-calendar.sqlite3'
    connection = sqlite3.connect(db_path)
    connection.execute(
        '''
        CREATE TABLE calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            date TEXT NOT NULL,
            date_to TEXT,
            level TEXT NOT NULL DEFAULT '',
            sport TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        '''
    )
    connection.execute(
        'INSERT INTO calendar_events (name, date, level, sport, url, created_at) '
        "VALUES ('Кросс', '2026-06-25', 'внутривузовские', 'Бег', '', '2026-01-01T00:00:00')"
    )
    connection.commit()
    connection.close()

    adapter = SQLiteAdapter(str(db_path))
    columns = {row['name'] for row in adapter.connection.execute('PRAGMA table_info(calendar_events)').fetchall()}
    assert {'regulation_filename', 'regulation_stored_name'} <= columns
    # Существующее событие: обе колонки NULL = файла нет, данные не тронуты
    event = adapter.get_calendar_event(1)
    assert event['name'] == 'Кросс'
    assert event['regulation_filename'] is None
    assert event['regulation_stored_name'] is None

    adapter.set_calendar_regulation(1, 'p.pdf', 'abc.pdf')
    event = adapter.get_calendar_event(1)
    assert (event['regulation_filename'], event['regulation_stored_name']) == ('p.pdf', 'abc.pdf')

    adapter.clear_calendar_regulation(1)
    event = adapter.get_calendar_event(1)
    assert event['regulation_filename'] is None and event['regulation_stored_name'] is None


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


def test_delete_competition_with_attachments(adapter):
    # M4: удаление записи админом тянет за собой её вложения — одна
    # транзакция, соседние записи не задеты.
    adapter.save_competitions(
        [
            make_competition('Первый', datetime(2026, 1, 1)),
            make_competition('Второй', datetime(2026, 2, 1)),
        ]
    )
    for record_id, filename in ((1, 'a.png'), (1, 'b.png'), (2, 'c.png')):
        adapter.create_attachment(
            record_id=record_id,
            filename=filename,
            stored_name=filename,
            content_type='image/png',
            size=10,
            uploaded_by=None,
        )
    assert adapter.count_attachments() == 3

    assert adapter.delete_competition_with_attachments(1) == 2

    assert [comp.student_name for comp in adapter.get_competitions()] == ['Второй']
    assert adapter.count_attachments() == 1
    assert [attachment['record_id'] for attachment in adapter.get_attachments()] == [2]

    # повторное удаление — без эффекта и без ошибок
    assert adapter.delete_competition_with_attachments(1) == 0


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

    # 600 строк: объём данных должен доминировать над размером пустой схемы
    # (таблицы растут — field_settings/import_queue и др., порог 0.5 иначе
    # становится хрупким).
    adapter.save_competitions(
        [make_competition(f'Спортсменов Номер {index}', datetime(2025, index % 12 + 1, 1)) for index in range(600)]
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
    new_rows = rows[:10]

    # Значения сеются ИЗ ВСТАВЛЕННЫХ ЗАПИСЕЙ (решение 2026-09-13): строки,
    # отсеянные как дубли, справочник не пополняют — иначе следующий импорт
    # того же файла возвращал бы значения, переименованные с обновлением
    # записей (инцидент 2026-09-13).
    duplicate_row = make_import_row('Дублей Дублий Дублиевич', 3, sport='Уникальный спорт')

    adapter.import_competitions(new_rows, owner_id=7)

    assert adapter.count_competitions() == len(new_rows)
    saved = adapter.get_competitions()
    assert {row.student_name for row in saved} == {row.student_name for row in new_rows}
    owners = {row['owner_id'] for row in adapter.connection.execute('SELECT DISTINCT owner_id FROM competitions')}
    assert owners == {7}

    levels = set(adapter.get_level_names(include_inactive=True))
    assert {'внутривузовские', 'межвузовские'} <= levels
    sports = set(adapter.list_catalog('sport'))
    assert {f'Спорт-{index % 13}' for index in range(10)} <= sports
    assert 'Уникальный спорт' not in sports  # значение строки-дубля не сеется
    institutes = set(adapter.list_catalog('institute'))
    assert {f'ИПК-{index % 7}' for index in range(10)} <= institutes
    # Пары институт→группа сложены иерархией: группа под своим институтом.
    options = adapter.get_group_options_by_institute()
    assert 'Г-3' in options['ИПК-3']
    # Строка-дубль не вставлена — её уникального спорта в справочнике нет.
    assert duplicate_row.student_name not in {row.student_name for row in saved}


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
        adapter.import_competitions(rows)
    monkeypatch.undo()

    # Либо все строки импорта, либо ничего: откатились и записи, и справочники.
    assert adapter.count_competitions() == 0
    assert 'Спорт-0' not in adapter.list_catalog('sport')
    assert 'ИПК-0' not in adapter.list_catalog('institute')
    assert 'внутривузовские' not in adapter.get_level_names(include_inactive=True)

    # Адаптер жив: следующий импорт проходит целиком.
    adapter.import_competitions(rows)
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

    adapter.import_competitions(rows)

    # Ориентир производительности без хрупких таймингов: один импорт — один
    # COMMIT (раньше справочники коммитились на каждую строку).
    assert counting.commits == 1
    assert adapter.count_competitions() == 500


# --- Серверные фильтры и пагинация главной (прототип 02) ---


def seed_page_records(adapter: SQLiteAdapter) -> None:
    records = [
        make_competition('Иванов Иван', datetime(2024, 3, 1)),
        make_competition('Петров Пётр', datetime(2024, 6, 1)),
        make_competition('Сидоров Сидор', datetime(2025, 1, 1)),
    ]
    adapter.save_competitions(records)
    adapter.save_competitions(
        [make_competition('Ждунов Ждун', datetime(2025, 2, 1))],
        review_status='pending',
        owner_id=7,
    )
    adapter.save_competitions(
        [make_competition('Отклонов Отклон', datetime(2025, 3, 1))],
        review_status='rejected',
        owner_id=7,
    )


def test_competitions_page_count_matches_rows(adapter):
    seed_page_records(adapter)

    total = adapter.count_competitions_filtered()
    page = adapter.get_competitions_page(limit=2, offset=0)
    assert total == 5
    assert [item.student_name for item in page] == ['Иванов Иван', 'Петров Пётр']

    second_page = adapter.get_competitions_page(limit=2, offset=2)
    assert [item.student_name for item in second_page] == ['Сидоров Сидор', 'Ждунов Ждун']

    # Страница за пределами — пустая, счётчик не меняется.
    assert adapter.get_competitions_page(limit=2, offset=100) == []
    assert adapter.count_competitions_filtered() == 5


def test_competitions_page_filters_combine(adapter):
    seed_page_records(adapter)

    assert adapter.count_competitions_filtered(name='Иван') == 1
    assert adapter.count_competitions_filtered(institute='ИСИ') == 5
    assert adapter.count_competitions_filtered(review_status='pending') == 1
    assert adapter.count_competitions_filtered(review_status='approved') == 3
    assert adapter.count_competitions_filtered(date_from='01.01.2025', date_to='31.12.2025') == 3
    # Комбинация: из записей 2025 года только подтверждённые.
    assert (
        adapter.count_competitions_filtered(date_from='01.01.2025', date_to='31.12.2025', review_status='approved') == 1
    )

    filtered = adapter.get_competitions_page(name='Сидоров')
    assert [item.student_name for item in filtered] == ['Сидоров Сидор']


def test_competitions_page_unapproved_only_counts_non_approved(adapter):
    seed_page_records(adapter)

    assert adapter.count_competitions_filtered(unapproved_only=True) == 2
    assert adapter.count_competitions_filtered(unapproved_only=True, owner_id=7) == 2
    assert adapter.count_competitions_filtered(unapproved_only=True, review_status='pending') == 1


def test_competitions_page_owner_scope_matches_get_competitions(adapter):
    seed_page_records(adapter)

    scoped = list(adapter.get_competitions(owner_id=7))
    page = adapter.get_competitions_page(owner_id=7)
    assert {item.student_name for item in scoped} == {item.student_name for item in page}
    assert adapter.count_competitions_filtered(owner_id=7) == len(scoped)


# --- №1.1: регистронезависимые справочники (канонические подстановки) ---


def test_find_catalog_canonical_returns_existing_casing(adapter):
    adapter.add_catalog_value('sport', 'Бег')
    adapter.add_catalog_value('institute', 'ИСИ')

    assert adapter.find_catalog_canonical('sport', 'бег') == 'Бег'
    assert adapter.find_catalog_canonical('institute', 'иси') == 'ИСИ'
    # Точное совпадение и отсутствие значения
    assert adapter.find_catalog_canonical('sport', 'Бег') == 'Бег'
    assert adapter.find_catalog_canonical('sport', 'Плавание') is None


def test_find_catalog_canonical_group_scoped_to_institute(adapter):
    adapter.add_catalog_value('institute', 'ИСИ')
    adapter.add_catalog_value('institute', 'ИГНА')
    institute_id = adapter.find_catalog_row('institute', 'ИСИ')['id']
    other_id = adapter.find_catalog_row('institute', 'ИГНА')['id']
    adapter.add_catalog_value('group', 'ПГС-101', parent_id=institute_id)

    assert adapter.find_catalog_canonical('group', 'пгс-101', parent_id=institute_id) == 'ПГС-101'
    # В другом институте такой группы нет
    assert adapter.find_catalog_canonical('group', 'пгс-101', parent_id=other_id) is None


def test_find_level_canonical_case_insensitive(adapter):
    adapter.create_level('внутривузовские')

    assert adapter.find_level_canonical('Внутривузовские') == 'внутривузовские'
    assert adapter.find_level_canonical('межвузовские') is None


def test_add_catalog_value_still_ignores_exact_duplicate(adapter):
    adapter.add_catalog_value('sport', 'Бег')
    adapter.add_catalog_value('sport', 'Бег')

    assert adapter.list_catalog('sport') == ['Бег']


# Даты-диапазоны (решение 2026-09-13, docs/data-model-decisions.md
# «Даты-диапазоны: визуально одно, под капотом два»).


def make_range_competition(name: str, date: datetime, date_to: datetime | None) -> Competition:
    competition = make_competition(name, date)
    competition.date_to = date_to
    return competition


def test_migration_adds_date_to_to_existing_db(tmp_path):
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
    connection.execute(
        'INSERT INTO competitions (student_id, student_name, student_sex, institute, '
        '"group", course, sport, date, level, name, position, created_at) '
        "VALUES ('id1', 'Легаси Лев', 'М', 'ИСИ', 'ПГС-101', 2, 'Бег', '2026-01-10T00:00:00', "
        "'внутривузовские', 'Кубок', 1, '2026-01-11T00:00:00')"
    )
    connection.commit()
    connection.close()

    adapter = SQLiteAdapter(str(db_path))
    columns = {row['name'] for row in adapter.connection.execute('PRAGMA table_info(competitions)')}
    assert 'date_to' in columns
    # Существующие записи — однодневные: date_to NULL, данные не тронуты.
    record = adapter.get_competitions()[0]
    assert record.date_to is None
    assert record.student_name == 'Легаси Лев'


def test_date_range_roundtrip(adapter):
    adapter.save_competitions([make_range_competition('Диапазон Дима', datetime(2026, 6, 25), datetime(2026, 6, 27))])
    record = adapter.get_competitions()[0]
    assert record.date.date().isoformat() == '2026-06-25'
    assert record.date_to.date().isoformat() == '2026-06-27'


def test_period_filter_matches_date_from_or_date_to(adapter):
    adapter.save_competitions(
        [
            # Внутри периода только дата НАЧАЛА
            make_range_competition('Начало Николай', datetime(2026, 6, 25), datetime(2026, 7, 10)),
            # Внутри периода только дата ОКОНЧАНИЯ
            make_range_competition('Конец Константин', datetime(2026, 5, 1), datetime(2026, 6, 3)),
            # Обе даты вне периода
            make_range_competition('Мимо Михаил', datetime(2026, 1, 1), datetime(2026, 1, 5)),
            # Однодневная внутри периода
            make_competition('Один Олег', datetime(2026, 6, 20)),
        ]
    )

    rows = adapter.get_grouped_report('sport', date_from='01.06.2026', date_to='30.06.2026')
    metrics = {row.slice_value: row.count_participation for row in rows}
    assert metrics == {'Бег': 3}


def test_page_filter_period_includes_range_records(adapter):
    adapter.save_competitions(
        [
            make_range_competition('Конец Константин', datetime(2026, 5, 1), datetime(2026, 6, 3)),
            make_range_competition('Мимо Михаил', datetime(2026, 1, 1), datetime(2026, 1, 5)),
        ]
    )

    page = adapter.get_competitions_page(date_from='01.06.2026', date_to='30.06.2026')
    assert [record.student_name for record in page] == ['Конец Константин']
    assert adapter.count_competitions_filtered(date_from='01.06.2026', date_to='30.06.2026') == 1


def test_records_sorted_by_date_from(adapter):
    adapter.save_competitions(
        [
            make_competition('Поздний Пётр', datetime(2026, 9, 1)),
            make_range_competition('Ранний Роман', datetime(2026, 3, 1), datetime(2026, 3, 5)),
        ]
    )

    page = adapter.get_competitions_page()
    assert [record.student_name for record in page] == ['Ранний Роман', 'Поздний Пётр']


# --- Лёгкий реестр полей (волна 3, docs/data-model-decisions.md) ---


def test_field_settings_defaults_reflect_current_behavior(adapter):
    settings = adapter.get_field_settings()
    assert settings['student_name'] == {'value_type': 'text', 'required': True}
    assert settings['date'] == {'value_type': 'text', 'required': True}
    assert settings['course'] == {'value_type': 'number', 'required': True}
    assert settings['position'] == {'value_type': 'number', 'required': False}
    for key in ('student_sex', 'institute', 'group', 'sport', 'level', 'name'):
        assert settings[key] == {'value_type': 'text', 'required': False}


def test_field_settings_defaults_idempotent(adapter):
    adapter.update_field_settings({'course': ('text', False)})
    # Повторная инициализация (как при обновлении легаси-БД) не сбрасывает.
    adapter._populate_field_settings_defaults()
    assert adapter.get_field_settings()['course'] == {'value_type': 'text', 'required': False}


def test_text_course_stored_and_read_back(adapter):
    adapter.update_field_settings({'course': ('text', False)})
    adapter.save_competitions([make_competition('Текстовый Курс', datetime(2026, 5, 1))])
    # Перезапишем курс текстом напрямую: колонка целочисленная, но SQLite
    # хранит строку — реестр полей разрешает «Выпускник 2025/26».
    adapter.connection.execute(
        "UPDATE competitions SET course = 'Выпускник 2025/26' WHERE student_name = 'Текстовый Курс'"
    )
    adapter.connection.commit()
    record = list(adapter.get_competitions())[0]
    assert record.course == 'Выпускник 2025/26'


# --- Очередь конфликтов импорта (волна 3, №6/№8) ---


def test_import_queue_roundtrip(adapter):
    entry_id = adapter.add_import_queue_entry(
        {'student_name': 'Кандидат К', 'course': 2},
        matched_record_id=42,
        created_by=7,
    )
    entries = adapter.list_import_queue('pending')
    assert len(entries) == 1
    entry = entries[0]
    assert entry['id'] == entry_id
    assert entry['status'] == 'pending'
    assert entry['matched_record_id'] == 42
    assert entry['created_by'] == 7
    assert entry['payload'] == {'student_name': 'Кандидат К', 'course': 2}
    assert adapter.count_import_queue('pending') == 1

    adapter.set_import_queue_status(entry_id, 'accepted')
    assert adapter.list_import_queue('pending') == []
    assert adapter.get_import_queue_entry(entry_id)['status'] == 'accepted'


def test_get_competition_by_id(adapter):
    adapter.save_competitions([make_competition('Поиск По Id', datetime(2026, 5, 1))])
    record = list(adapter.get_competitions())[0]
    found = adapter.get_competition_by_id(int(record.record_id))
    assert found is not None
    assert found.student_name == 'Поиск По Id'
    assert adapter.get_competition_by_id(999999) is None


# --- №23доп (docs/feedback-live.md): резолвер атлета в хранилище ---


def make_athlete_record(
    name: str,
    date: datetime,
    *,
    sex: str = 'М',
    institute: str = 'ИСИ',
    group: str = 'ПГС-101',
    course: int = 2,
) -> Competition:
    record = make_competition(name, date)
    record.student_sex = sex
    record.institute = institute
    record.group = group
    record.course = course
    return record


def test_search_athletes_substring_case_insensitive(adapter):
    adapter.save_competitions(
        [
            make_athlete_record('Иванов Иван', datetime(2026, 3, 1)),
            make_athlete_record('Петров Пётр', datetime(2026, 3, 2), institute='ИМИ', group='СБ-202'),
        ]
    )
    # lower() в SQLite не приводит кириллицу — сравнение в Python.
    results = adapter.search_athletes('ИВА')
    assert [entry['name'] for entry in results] == ['Иванов Иван']
    assert results[0] == {'name': 'Иванов Иван', 'sex': 'М', 'institute': 'ИСИ', 'group': 'ПГС-101', 'course': '2'}


def test_search_athletes_empty_query(adapter):
    adapter.save_competitions([make_athlete_record('Иванов Иван', datetime(2026, 3, 1))])
    assert adapter.search_athletes('') == []
    assert adapter.search_athletes('   ') == []


def test_search_athletes_limit_and_sort(adapter):
    adapter.save_competitions([make_athlete_record(f'Атлет {number}', datetime(2026, 3, 1)) for number in range(12)])
    results = adapter.search_athletes('Атлет')
    assert len(results) == 8
    names = [entry['name'] for entry in results]
    assert names == sorted(names, key=lambda value: value.lower())


def test_find_athlete_fields_takes_latest_record(adapter):
    adapter.save_competitions(
        [
            make_athlete_record('Козлов Кирилл', datetime(2026, 1, 1), institute='ИСИ', group='ПГС-101'),
            # Последняя запись — её институт/группа и попадают в ответ.
            make_athlete_record('Козлов Кирилл', datetime(2026, 6, 1), institute='ИМИ', group='ТД-303'),
        ]
    )
    fields = adapter.find_athlete_fields('козлов кирилл')
    assert fields['institute'] == 'ИМИ'
    assert fields['group'] == 'ТД-303'


def test_known_athlete_merges_profile_and_record(adapter):
    adapter.save_competitions(
        [make_athlete_record('Смирнов Сергей', datetime(2026, 2, 1), sex='М', group='ПГС-101', course=3)]
    )
    # Профиль: пол/институт/группа заполнены, курс НЕ заполнен.
    adapter.create_user('smirnov', 'x', 'athlete')
    adapter.set_profile(
        1, {'student_name': 'Смирнов Сергей', 'student_sex': 'М', 'institute': 'ИЭиТ', 'group': 'Э-201'}
    )
    fields = adapter.find_athlete_fields('Смирнов Сергей')
    # Профиль приоритетнее, курс добирается из последней записи.
    assert fields == {'name': 'Смирнов Сергей', 'sex': 'М', 'institute': 'ИЭиТ', 'group': 'Э-201', 'course': '3'}


# №24 (docs/feedback-live.md): настройка link_target у link-полей.
def test_link_target_column_added_to_legacy_db(tmp_path):
    """Идемпотентная миграция: у легаси-таблицы custom_fields без link_target
    колонка добавляется, повторная инициализация не дублирует её."""
    import sqlite3

    db_path = tmp_path / 'legacy-fields.sqlite3'
    connection = sqlite3.connect(db_path)
    connection.execute(
        '''
        CREATE TABLE custom_fields (
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
    connection.commit()
    connection.close()

    adapter = SQLiteAdapter(str(db_path))
    columns = {row['name'] for row in adapter.connection.execute('PRAGMA table_info(custom_fields)')}
    assert 'link_target' in columns


def test_link_target_saved_and_read(adapter):
    adapter.create_custom_field(
        key='comp_link',
        label='Ссылка на соревнование',
        field_type='url',
        required=False,
        show_in_table=True,
        show_in_export=True,
        show_in_template=True,
        sort_order=0,
        link_target='name',
    )
    field = adapter.get_custom_fields()[0]
    assert field.link_target == 'name'

    adapter.update_custom_field(
        field_id=field.field_id,
        label='Ссылка на соревнование',
        field_type='url',
        required=False,
        show_in_table=True,
        show_in_export=True,
        show_in_template=True,
        sort_order=0,
        active=True,
        link_target=None,
    )
    assert adapter.get_custom_fields()[0].link_target is None


# ---- Календарь соревнований (волна A, docs/feedback-live.md №23) ----


def make_calendar_record(
    name: str,
    date: datetime,
    date_to: datetime | None,
    position: int = 1,
    calendar_event_id: int | None = None,
) -> Competition:
    competition = make_competition(name, date)
    competition.name = name
    competition.date = date
    competition.date_to = date_to
    competition.position = position
    competition.calendar_event_id = calendar_event_id
    return competition


def test_calendar_event_crud(adapter):
    event_id = adapter.create_calendar_event(
        name='Осенний кросс СибАДИ',
        date='2026-09-12',
        date_to=None,
        level='внутривузовские',
        sport='Бег',
        url='https://example.com',
    )
    assert event_id

    event = adapter.get_calendar_event(event_id)
    assert event['name'] == 'Осенний кросс СибАДИ'
    assert event['date'] == '2026-09-12'
    assert event['date_to'] is None

    adapter.update_calendar_event(
        event_id,
        name='Осенний кросс СибАДИ',
        date='2026-09-12',
        date_to='2026-09-13',
        level='региональные',
        sport='Бег',
        url='',
    )
    event = adapter.get_calendar_event(event_id)
    assert event['date_to'] == '2026-09-13'
    assert event['level'] == 'региональные'

    adapter.delete_calendar_event(event_id)
    assert adapter.get_calendar_event(event_id) is None


def test_calendar_list_counts_participants_by_preset(adapter):
    """Счётчики (P2, id-first): N — записи со ссылкой calendar_event_id,
    M — из них с position=0 («без результата»). NULL-legacy-строки, совпавшие
    с пресетом по случайности, не считаются; блокировщик удаления при этом
    консервативен (OR — см. test_calendar_delete_blocker_counts_link_or_preset)."""
    # Даты хранятся в том же ISO-формате, что и в записях реестра
    # (datetime.isoformat(), 'YYYY-MM-DDTHH:MM:SS') — совпадение по строке.
    event_id = adapter.create_calendar_event(
        name='Кросс',
        date='2026-09-12T00:00:00',
        date_to='2026-09-13T00:00:00',
        level='',
        sport='Бег',
        url='',
    )
    adapter.save_competitions(
        [
            # Связана, без результата
            make_calendar_record(
                'Кросс', datetime(2026, 9, 12), datetime(2026, 9, 13), position=0, calendar_event_id=event_id
            ),
            make_calendar_record(
                'Кросс', datetime(2026, 9, 12), datetime(2026, 9, 13), position=0, calendar_event_id=event_id
            ),
            # Связана, с результатом
            make_calendar_record(
                'Кросс', datetime(2026, 9, 12), datetime(2026, 9, 13), position=1, calendar_event_id=event_id
            ),
            # NULL-legacy: пресет совпадает, ссылки нет — в счётчики /calendar
            # и список участников события не входит
            make_calendar_record('Кросс', datetime(2026, 9, 12), datetime(2026, 9, 13), position=0),
            # Другое название (связь есть, но с другим событием не совпадает
            # и в этом тесте одна)
            make_calendar_record('Кубок', datetime(2026, 9, 12), datetime(2026, 9, 13), position=0),
        ],
        review_status='approved',
        owner_id=None,
    )
    events = adapter.list_calendar_events()
    assert len(events) == 1
    assert events[0]['id'] == event_id
    assert events[0]['participant_count'] == 3
    assert events[0]['no_result_count'] == 2

    participants = adapter.list_calendar_event_participants(event_id)
    assert len(participants) == 3


def test_calendar_list_matches_oneday_preset_exactly(adapter):
    """Однодневное событие (date_to NULL): участники — только связанные
    записи; многодневная NULL-legacy-строка с тем же началом не подхватывается
    (P2 — состав участников читается по ссылке, не по пресету)."""
    event_id = adapter.create_calendar_event(
        name='Кубок', date='2026-09-12T00:00:00', date_to=None, level='', sport='', url=''
    )
    adapter.save_competitions(
        [
            make_calendar_record('Кубок', datetime(2026, 9, 12), None, position=0, calendar_event_id=event_id),
            make_calendar_record('Кубок', datetime(2026, 9, 12), datetime(2026, 9, 14), position=0),
        ],
        review_status='approved',
        owner_id=None,
    )
    events = adapter.list_calendar_events()
    assert events[0]['participant_count'] == 1
    assert events[0]['no_result_count'] == 1
    assert len(adapter.list_calendar_event_participants(event_id)) == 1


def test_calendar_list_orders_chronologically_and_filters_by_sport(adapter):
    adapter.create_calendar_event(
        name='Поздний', date='2026-10-17T00:00:00', date_to=None, level='', sport='Волейбол', url=''
    )
    adapter.create_calendar_event(
        name='Ранний', date='2026-09-12T00:00:00', date_to=None, level='', sport='Бег', url=''
    )
    assert [event['name'] for event in adapter.list_calendar_events()] == ['Ранний', 'Поздний']
    assert [event['name'] for event in adapter.list_calendar_events(sport='Бег')] == ['Ранний']
    assert adapter.list_calendar_events(sport='Шахматы') == []


# ---- Event Model, Wave 1 P2: стабильная связь «запись → событие» ----


def test_calendar_participants_list_reads_by_link_only(adapter):
    """Список участников события — ТОЛЬКО записи со ссылкой calendar_event_id
    (id-first). NULL-legacy-строка с совпадающим пресетом не показывается
    на странице события и не попадает в счётчики."""
    event_id = adapter.create_calendar_event(
        name='Кросс', date='2026-09-12T00:00:00', date_to=None, level='', sport='Бег', url=''
    )
    other_event_id = adapter.create_calendar_event(
        name='Другой', date='2026-09-12T00:00:00', date_to=None, level='', sport='Бег', url=''
    )
    adapter.save_competitions(
        [
            make_calendar_record('Иванов Иван', datetime(2026, 9, 12), None, position=1, calendar_event_id=event_id),
            # С результатом — выше «ждущих результата» (position = 0)
            make_calendar_record('Петров Пётр', datetime(2026, 9, 12), None, position=0, calendar_event_id=event_id),
            # NULL-строка с ТОЧНО тем же пресетом — НЕ участник (id-first)
            make_calendar_record('Кросс', datetime(2026, 9, 12), None, position=1),
            # Связана с другим событием — НЕ участник этого
            make_calendar_record(
                'Козлов Козьма', datetime(2026, 9, 12), None, position=1, calendar_event_id=other_event_id
            ),
        ],
        review_status='approved',
        owner_id=None,
    )
    participants = adapter.list_calendar_event_participants(event_id)
    assert [participant['student_name'] for participant in participants] == ['Иванов Иван', 'Петров Пётр']
    assert participants[1]['position'] == 0
    # Счётчики /calendar — те же участники
    events = {event['id']: event for event in adapter.list_calendar_events()}
    assert events[event_id]['participant_count'] == 2
    assert events[event_id]['no_result_count'] == 1


def test_calendar_delete_blocker_counts_link_or_preset(adapter):
    """Блокировщик удаления — консервативный OR (решение A): считаются записи
    по ссылке ИЛИ по пресету. Ложный блокирующий отказ лучше молчаливого
    удаления события, у которого остались записи-двойники пресета."""
    # Событие без связанных записей, но с NULL-строкой по пресету
    # (make_calendar_record делает название записи = первому аргументу)
    preset_only_id = adapter.create_calendar_event(
        name='Кубок', date='2026-01-10T00:00:00', date_to=None, level='', sport='Бег', url=''
    )
    adapter.save_competitions([make_calendar_record('Кубок', datetime(2026, 1, 10), None, position=1)])
    assert adapter.list_calendar_event_participants(preset_only_id) == []
    assert adapter.count_calendar_event_participants(preset_only_id) == 1

    # Событие со связанной записью, пресет которой уже разошёлся
    linked_id = adapter.create_calendar_event(
        name='Другое событие', date='2026-02-01T00:00:00', date_to=None, level='', sport='', url=''
    )
    adapter.save_competitions(
        [make_calendar_record('Связан Сергей', datetime(2026, 3, 5), None, position=1, calendar_event_id=linked_id)]
    )
    assert adapter.count_calendar_event_participants(linked_id) == 1

    # Совсем пустое событие — блокировки нет
    empty_id = adapter.create_calendar_event(
        name='Пустое', date='2026-04-01T00:00:00', date_to=None, level='', sport='', url=''
    )
    assert adapter.count_calendar_event_participants(empty_id) == 0


def test_update_calendar_event_syncs_linked_records(adapter):
    """Правка события синхронизирует 5 event-owned полей ТОЛЬКО связанных
    записей и возвращает их число; NULL-сосед с тем же старым пресетом
    не трогается."""
    event_id = adapter.create_calendar_event(
        name='Кросс',
        date='2026-09-12T00:00:00',
        date_to='2026-09-13T00:00:00',
        level='внутривузовские',
        sport='Бег',
        url='',
    )
    adapter.save_competitions(
        [
            make_calendar_record(
                'Иванов Иван', datetime(2026, 9, 12), datetime(2026, 9, 13), position=1, calendar_event_id=event_id
            ),
            make_calendar_record(
                'Петров Пётр', datetime(2026, 9, 12), datetime(2026, 9, 13), position=0, calendar_event_id=event_id
            ),
            # NULL-сосед с ТОЧНО тем же пресетом — синхронизация его не
            # меняет (состав синхронизации — по ссылке, не по пресету)
            make_calendar_record('Кросс', datetime(2026, 9, 12), datetime(2026, 9, 13), position=1),
        ],
        review_status='approved',
        owner_id=None,
    )

    synced = adapter.update_calendar_event(
        event_id,
        name='Осенний кросс',
        date='2026-10-01T00:00:00',
        date_to=None,
        level='региональные',
        sport='Лыжи',
        url='https://example.com',
    )
    assert synced == 2

    event = adapter.get_calendar_event(event_id)
    assert event['name'] == 'Осенний кросс'
    assert event['date'] == '2026-10-01T00:00:00'
    assert event['date_to'] is None
    assert event['level'] == 'региональные'
    assert event['sport'] == 'Лыжи'

    records = adapter.get_competitions()
    by_name = {record.student_name: record for record in records}
    for name in ('Иванов Иван', 'Петров Пётр'):
        record = by_name[name]
        assert record.name == 'Осенний кросс'
        assert record.date == datetime(2026, 10, 1, 0, 0)
        assert record.date_to is None
        assert record.level == 'региональные'
        assert record.sport == 'Лыжи'
        assert record.calendar_event_id == event_id
        # Не event-owned поля синхронизацией не затираются
        assert record.position == (1 if name == 'Иванов Иван' else 0)
    legacy = by_name['Кросс']
    assert legacy.name == 'Кросс'
    assert legacy.date == datetime(2026, 9, 12, 0, 0)
    assert legacy.date_to == datetime(2026, 9, 13, 0, 0)
    assert legacy.level == 'внутривузовские'
    assert legacy.sport == 'Бег'
    assert legacy.calendar_event_id is None


def test_update_calendar_event_rollback_on_failure(adapter):
    """Сбой синхронизации откатывает и правку самого события (одна
    транзакция): остаётся прежнее состояние — событие и записи неизменны."""
    event_id = adapter.create_calendar_event(
        name='Кросс',
        date='2026-09-12T00:00:00',
        date_to=None,
        level='внутривузовские',
        sport='Бег',
        url='',
    )
    adapter.save_competitions(
        [make_calendar_record('Кросс', datetime(2026, 9, 12), None, position=1, calendar_event_id=event_id)]
    )

    class FailingSyncConnection:
        """Все запросы проходят в реальное соединение, но UPDATE связанных
        записей (второй шаг транзакции) падает — сбой ПОСЛЕ правки события,
        до commit."""

        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, parameters=()):
            if sql.strip().startswith('UPDATE competitions'):
                raise RuntimeError('Injected sync failure')
            return self._connection.execute(sql, parameters)

        def commit(self):
            self._connection.commit()

        def rollback(self):
            self._connection.rollback()

    real_connection = adapter.connection
    adapter.connection = FailingSyncConnection(real_connection)
    with pytest.raises(RuntimeError):
        adapter.update_calendar_event(
            event_id,
            name='Осенний кросс',
            date='2026-10-01T00:00:00',
            date_to=None,
            level='региональные',
            sport='Лыжи',
            url='',
        )
    adapter.connection = real_connection

    event = adapter.get_calendar_event(event_id)
    assert event['name'] == 'Кросс'
    assert event['date'] == '2026-09-12T00:00:00'
    assert event['level'] == 'внутривузовские'
    assert event['sport'] == 'Бег'
    record = adapter.get_competitions()[0]
    assert record.name == 'Кросс'
    assert record.date == datetime(2026, 9, 12, 0, 0)
    assert record.level == 'внутривузовские'
    assert record.sport == 'Бег'


# ---- Карточки студентов (Student Identity v1, Phase 1 — фундамент) ----


def make_legacy_db_with_data(tmp_path):
    """Легаси-база без таблиц студентов и колонок student_ref_id: записи
    и пользователи с данными, которые миграция обязана сохранить."""
    db_path = tmp_path / 'legacy-students.sqlite3'
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
            created_at TEXT NOT NULL
        )
        '''
    )
    connection.execute(
        '''
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        '''
    )
    connection.execute(
        'INSERT INTO competitions (student_id, student_name, student_sex, institute, "group", course, '
        "sport, date, level, name, position, created_at) VALUES ('hash-1', 'Иванов Иван', 'М', 'ИСИ', "
        "'ПГС-101', 2, 'Бег', '2026-01-10T00:00:00', 'внутривузовские', 'Кубок', 1, '2026-01-11T00:00:00')"
    )
    connection.execute(
        "INSERT INTO users (username, password_hash, role, active) VALUES ('anna', 'scrypt$x', 'athlete', 1)"
    )
    connection.commit()
    connection.close()
    return db_path


def test_students_tables_created_for_legacy_db(tmp_path):
    db_path = make_legacy_db_with_data(tmp_path)

    adapter = SQLiteAdapter(str(db_path))
    tables = {row['name'] for row in adapter.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {'students', 'student_aliases'} <= tables

    competition_columns = {row['name'] for row in adapter.connection.execute('PRAGMA table_info(competitions)')}
    assert 'student_ref_id' in competition_columns
    user_columns = {row['name'] for row in adapter.connection.execute('PRAGMA table_info(users)')}
    assert 'student_ref_id' in user_columns

    # Данные легаси-базы не тронуты; новые колонки у существующих строк — NULL
    competition_row = adapter.connection.execute('SELECT * FROM competitions WHERE id = 1').fetchone()
    assert competition_row['student_name'] == 'Иванов Иван'
    assert competition_row['student_ref_id'] is None
    user_row = adapter.connection.execute('SELECT * FROM users WHERE username = \'anna\'').fetchone()
    assert user_row['role'] == 'athlete'
    assert user_row['student_ref_id'] is None


def test_schema_reinit_idempotent_keeps_students(tmp_path):
    db_path = str(tmp_path / 'reinit.sqlite3')
    adapter = SQLiteAdapter(db_path)
    student_id = adapter.create_student('Петров Пётр Петрович', 'М', 'ИМИ', 'СБ-202', '1')
    adapter.add_student_alias(student_id, 'Петров П.П.')

    adapter = SQLiteAdapter(db_path)
    student = adapter.get_student_by_id(student_id)
    assert student['full_name'] == 'Петров Пётр Петрович'
    assert [alias['name'] for alias in adapter.list_student_aliases(student_id)] == ['Петров П.П.']

    tables = [row[0] for row in adapter.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert tables.count('students') == 1
    assert tables.count('student_aliases') == 1


def test_student_crud_lifecycle(adapter):
    first = adapter.create_student('Иванов Иван Иванович', 'М', 'ИСИ', 'ПГС-101', '2')
    second = adapter.create_student('Абрамов Артём Артёмович', '', '', '', '')

    student = adapter.get_student_by_id(first)
    assert student['full_name'] == 'Иванов Иван Иванович'
    assert student['sex'] == 'М'
    assert student['group_name'] == 'ПГС-101'
    assert student['active'] == 1
    assert student['merged_into_id'] is None
    assert student['created_at'] == student['updated_at']
    assert adapter.get_student_by_id(999999) is None

    # Список: активные сверху, внутри — по алфавиту
    adapter.set_student_active(second, False)
    names = [item['full_name'] for item in adapter.list_students()]
    assert names == ['Иванов Иван Иванович', 'Абрамов Артём Артёмович']

    # Правка меняет данные и updated_at, но не created_at
    created_at = student['created_at']
    assert adapter.update_student(first, 'Иванов Иван Иванович', '', 'ИСИ', 'ПГС-102', '3')
    updated = adapter.get_student_by_id(first)
    assert updated['institute'] == 'ИСИ'
    assert updated['group_name'] == 'ПГС-102'
    assert updated['course'] == '3'
    assert updated['created_at'] == created_at
    assert updated['updated_at'] >= created_at
    assert adapter.update_student(999999, 'Никто', '', '', '', '') is False

    assert adapter.set_student_active(second, True)
    assert adapter.get_student_by_id(second)['active'] == 1
    assert adapter.set_student_active(999999, False) is False

    # Поиск — подстрока ФИО (как фильтр ФИО отчётов)
    assert [item['id'] for item in adapter.list_students(search='Иванов')] == [first]
    assert adapter.list_students(search='Неттакого') == []


def test_student_duplicate_full_name_allowed(adapter):
    """Полные тёзки — отдельные карточки: проверки уникальности ФИО нет."""
    first = adapter.create_student('Сидоров Сидор Сидорович', 'М', 'ИСИ', 'ПГС-101', '1')
    second = adapter.create_student('Сидоров Сидор Сидорович', 'М', 'ИСИ', 'ПГС-101', '1')
    assert first != second
    assert len(adapter.list_students()) == 2


def test_create_students_batch_returns_ids_in_order(adapter):
    """Массовое создание (Phase 2.5, импорт): id — в порядке входных строк."""
    ids = adapter.create_students(
        [
            ('Иванов Иван Иванович', 'М', 'ИСИ', 'ПГС-101', '2'),
            ('Петров Пётр Петрович', 'Ж', '', '', ''),
            ('Сидоров Сидор Сидорович', '', 'ИМИ', 'СБ-202', '1'),
        ]
    )
    assert len(ids) == 3
    assert len(set(ids)) == 3
    stored = [adapter.get_student_by_id(student_id) for student_id in ids]
    assert [student['full_name'] for student in stored] == [
        'Иванов Иван Иванович',
        'Петров Пётр Петрович',
        'Сидоров Сидор Сидорович',
    ]
    assert stored[0]['group_name'] == 'ПГС-101'
    assert stored[1]['institute'] == ''
    assert stored[2]['active'] == 1
    assert adapter.create_students([]) == []


def test_create_students_atomic_rollback_on_failure(adapter):
    """Сбой любой строки откатывает весь батч: «либо все, либо ничего»."""
    adapter.create_student('Существующий Студент', 'М', '', '', '')
    with pytest.raises(sqlite3.IntegrityError):
        adapter.create_students(
            [
                ('Иванов Иван Иванович', 'М', 'ИСИ', 'ПГС-101', '2'),
                # NULL full_name — нарушение NOT NULL: батч должен откатиться.
                (None, '', '', '', ''),
                ('Петров Пётр Петрович', 'М', 'ИСИ', 'ПГС-102', '1'),
            ]
        )
    names = [student['full_name'] for student in adapter.list_students()]
    assert names == ['Существующий Студент']


def test_student_alias_add_remove_and_per_student_duplicate_rejected(adapter):
    student_id = adapter.create_student('Иванов Иван Иванович', 'М', '', '', '')

    assert adapter.add_student_alias(student_id, 'Иванов И.И.')
    assert adapter.add_student_alias(student_id, '  Иванов И.И.  ') is False  # дубль после strip
    assert adapter.add_student_alias(student_id, '   ') is False  # пустое имя
    assert adapter.add_student_alias(999999, 'Призрак') is False  # нет такого студента

    aliases = adapter.list_student_aliases(student_id)
    assert [alias['name'] for alias in aliases] == ['Иванов И.И.']

    assert adapter.remove_student_alias(student_id, aliases[0]['id'])
    assert adapter.list_student_aliases(student_id) == []
    assert adapter.remove_student_alias(student_id, aliases[0]['id']) is False


def test_student_alias_same_name_for_different_students_allowed(adapter):
    """Одно написание ФИО может быть псевдонимом у разных карточек
    (UNIQUE — только на пару student_id+name)."""
    first = adapter.create_student('Иванов Иван Иванович', 'М', '', '', '')
    second = adapter.create_student('Иванов Иван Иванович', 'М', '', '', '')

    assert adapter.add_student_alias(first, 'Иванов И.И.')
    assert adapter.add_student_alias(second, 'Иванов И.И.')
    assert len(adapter.list_student_aliases(first)) == 1
    assert len(adapter.list_student_aliases(second)) == 1


def test_student_update_does_not_touch_competitions(adapter):
    """Правка карточки студента не меняет записи реестра (включая
    student_ref_id): строка competitions байт-в-байт та же до и после."""
    adapter.save_competitions([make_competition('Иванов Иван', datetime(2026, 2, 1))])
    student_id = adapter.create_student('Иванов Иван', 'М', 'ИСИ', 'ПГС-101', '2')

    before = adapter.connection.execute('SELECT * FROM competitions WHERE id = 1').fetchone()
    assert before is not None
    assert before['student_ref_id'] is None

    adapter.update_student(student_id, 'Иванов Иван Иванович', 'Ж', 'ИМИ', 'СБ-202', '4')
    adapter.set_student_active(student_id, False)
    adapter.add_student_alias(student_id, 'Иванов И.И.')
    adapter.remove_student_alias(student_id, 1)

    after = adapter.connection.execute('SELECT * FROM competitions WHERE id = 1').fetchone()
    assert tuple(after) == tuple(before)
    assert after['student_ref_id'] is None


def test_delete_student_success_with_aliases(adapter):
    """Hard delete пустой карточки: строка и её псевдонимы исчезают;
    одноимённый псевдоним ДРУГОЙ карточки не трогается; неактивная
    карточка без связей тоже удаляется."""
    first = adapter.create_student('Иванов Иван Иванович', 'М', 'ИСИ', 'ПГС-101', '2')
    second = adapter.create_student('Сидор Сидор Сидорович', 'М', '', '', '')
    adapter.add_student_alias(first, 'Иванов И.И.')
    adapter.add_student_alias(second, 'Иванов И.И.')
    inactive = adapter.create_student('Спящий Студент', 'Ж', '', '', '')
    adapter.set_student_active(inactive, False)

    assert adapter.delete_student(first) == ('ok', {})
    assert adapter.get_student_by_id(first) is None
    assert adapter.list_student_aliases(first) == []
    # Псевдоним другой карточки с тем же именем на месте
    assert [alias['name'] for alias in adapter.list_student_aliases(second)] == ['Иванов И.И.']

    assert adapter.delete_student(inactive) == ('ok', {})
    assert [student['id'] for student in adapter.list_students()] == [second]


def test_delete_student_not_found(adapter):
    assert adapter.delete_student(999999) == ('not_found', {})
    assert adapter.delete_student(0) == ('not_found', {})


def test_delete_student_blocked_by_any_link(adapter):
    """Блок при ЛЮБЫХ связях: записи, аккаунты (включая легаси-строку без
    роли athlete), слитые карточки — при блоке не меняется ни одна строка."""
    student_id = adapter.create_student('Иванов Иван', 'М', 'ИСИ', 'ПГС-101', '2')
    adapter.add_student_alias(student_id, 'Иванов И.И.')

    # 1) записи соревнований
    adapter.save_competitions(
        [
            make_competition('Старое ФИО', datetime(2026, 1, 10)),
            make_competition('Старое ФИО', datetime(2026, 2, 20)),
        ]
    )
    adapter.link_competitions([1, 2], student_id)
    assert adapter.delete_student(student_id) == (
        'blocked',
        {'records': 2, 'athlete_users': 0, 'merged_children': 0},
    )
    assert adapter.get_student_by_id(student_id) is not None
    assert [alias['name'] for alias in adapter.list_student_aliases(student_id)] == ['Иванов И.И.']

    # 2) аккаунт атлета
    adapter.unlink_competition(1)
    adapter.unlink_competition(2)
    anna_id = make_athlete_user(adapter, 'anna')
    adapter.link_user(anna_id, student_id)
    assert adapter.delete_student(student_id) == (
        'blocked',
        {'records': 0, 'athlete_users': 1, 'merged_children': 0},
    )

    # 2а) легаси-строка без роли athlete с тем же ref — тоже блокер
    # (счётчик без фильтра роли: link_user пишет только athlete, но
    # прямые записи в БД накрываются тем же запретом)
    adapter.unlink_user(anna_id)
    adapter.create_user('chief', 'scrypt$x', 'admin')
    chief_id = adapter.get_user('chief')['id']
    adapter.connection.execute(
        'UPDATE users SET student_ref_id = ? WHERE id = ?',
        (student_id, chief_id),
    )
    adapter.connection.commit()
    assert adapter.delete_student(student_id) == (
        'blocked',
        {'records': 0, 'athlete_users': 1, 'merged_children': 0},
    )
    adapter.connection.execute('UPDATE users SET student_ref_id = NULL WHERE id = ?', (chief_id,))
    adapter.connection.commit()

    # 3) карточка, слитая в эту (merged_into_id)
    child = adapter.create_student('Дубль Дублёв', 'М', '', '', '')
    adapter.connection.execute(
        'UPDATE students SET merged_into_id = ? WHERE id = ?',
        (student_id, child),
    )
    adapter.connection.commit()
    assert adapter.delete_student(student_id) == (
        'blocked',
        {'records': 0, 'athlete_users': 0, 'merged_children': 1},
    )
    assert adapter.get_student_by_id(child) is not None

    # Снятие блокера открывает удаление; карточка-дубль не тронута
    adapter.connection.execute('UPDATE students SET merged_into_id = NULL WHERE id = ?', (child,))
    adapter.connection.commit()
    assert adapter.delete_student(student_id) == ('ok', {})
    assert adapter.get_student_by_id(child) is not None


def test_delete_student_rolls_back_on_failure_between_deletes(adapter, monkeypatch):
    """Сбой между DELETE псевдонимов и DELETE карточки откатывает всё:
    карточка и псевдонимы на месте, соединение остаётся рабочим."""
    student_id = adapter.create_student('Иванов Иван', 'М', 'ИСИ', 'ПГС-101', '2')
    adapter.add_student_alias(student_id, 'Иванов И.И.')
    adapter.add_student_alias(student_id, 'Иванов И.')

    monkeypatch.setattr(adapter, 'connection', FailingStudentsDeleteConnection(adapter.connection))
    with pytest.raises(RuntimeError):
        adapter.delete_student(student_id)
    monkeypatch.undo()

    student = adapter.get_student_by_id(student_id)
    assert student is not None and student['full_name'] == 'Иванов Иван'
    assert [alias['name'] for alias in adapter.list_student_aliases(student_id)] == [
        'Иванов И.И.',
        'Иванов И.',
    ]
    # Соединение живо: повторная попытка без сбоя проходит
    assert adapter.delete_student(student_id) == ('ok', {})
    assert adapter.get_student_by_id(student_id) is None


def test_manual_entry_still_works_on_db_with_students(tmp_path):
    """Регрессия: ручной ввод/импорт работают в базе с таблицей студентов —
    nullable-колонка student_ref_id остаётся незадействованной."""
    adapter = SQLiteAdapter(str(tmp_path / 'with-students.sqlite3'))
    adapter.create_student('Иванов Иван Иванович', 'М', 'ИСИ', 'ПГС-101', '2')

    adapter.save_competitions([make_competition('Иванов Иван Иванович', datetime(2026, 3, 1))])
    saved = adapter.get_competitions()[0]
    assert saved.student_name == 'Иванов Иван Иванович'
    row = adapter.connection.execute('SELECT student_ref_id FROM competitions WHERE id = 1').fetchone()
    assert row['student_ref_id'] is None


# ---- Сопоставление данных (Student Identity v1, Phase 2) ----


def make_athlete_user(
    adapter: SQLiteAdapter, username: str, profile: dict | None = None, aliases: list[str] = ()
) -> int:
    """Аккаунт атлета с профилем и псевдонимами; возвращает id."""
    adapter.create_user(username, 'scrypt$x', 'athlete')
    user = adapter.get_user(username)
    if profile:
        adapter.set_profile(user['id'], profile)
    for alias in aliases:
        adapter.add_name_alias(user['id'], alias)
    return user['id']


def test_reconciliation_counters(adapter):
    assert adapter.count_student_reconciliation() == {
        'records_total': 0,
        'records_linked': 0,
        'records_unlinked': 0,
        'athlete_users_total': 0,
        'athlete_users_linked': 0,
        'athlete_users_unlinked': 0,
    }

    adapter.save_competitions(
        [
            make_competition('Иванов Иван', datetime(2026, 1, 10)),
            make_competition('Петров Пётр', datetime(2026, 2, 10)),
        ]
    )
    student_id = adapter.create_student('Иванов Иван', 'М', 'ИСИ', 'ПГС-101', '2')
    anna_id = make_athlete_user(adapter, 'anna')
    boris_id = make_athlete_user(adapter, 'boris')
    adapter.create_user('chief', 'scrypt$x', 'admin')

    assert adapter.count_student_reconciliation() == {
        'records_total': 2,
        'records_linked': 0,
        'records_unlinked': 2,
        'athlete_users_total': 2,
        'athlete_users_linked': 0,
        'athlete_users_unlinked': 2,
    }

    count, error = adapter.link_competitions([1], student_id)
    assert (count, error) == (1, None)
    linked, error = adapter.link_user(anna_id, student_id)
    assert (linked, error) == (1, None)

    assert adapter.count_student_reconciliation() == {
        'records_total': 2,
        'records_linked': 1,
        'records_unlinked': 1,
        'athlete_users_total': 2,
        'athlete_users_linked': 1,
        'athlete_users_unlinked': 1,
    }
    assert boris_id


def test_unlinked_competitions_list_pagination_search_and_ordering(adapter):
    """Порядок — ФИО, затем дата, затем id; поиск — подстрока ФИО;
    привязанные записи в список не попадают."""
    adapter.save_competitions(
        [
            make_competition('Петров Пётр', datetime(2026, 3, 1)),
            make_competition('Иванов Иван', datetime(2026, 2, 10)),
            make_competition('Иванов Иван', datetime(2026, 1, 5)),
            make_competition('Иванов Иван', datetime(2026, 1, 5)),  # дубль-строка в реестре
            make_competition('Иванов Пётр', datetime(2026, 4, 1)),
        ]
    )
    assert [row['id'] for row in adapter.list_unlinked_competitions()] == [3, 4, 2, 5, 1]

    # Пагинация: limit/offset по тому же порядку
    assert [row['id'] for row in adapter.list_unlinked_competitions(limit=2, offset=0)] == [3, 4]
    assert [row['id'] for row in adapter.list_unlinked_competitions(limit=2, offset=2)] == [2, 5]
    assert [row['id'] for row in adapter.list_unlinked_competitions(limit=2, offset=4)] == [1]
    assert adapter.list_unlinked_competitions(limit=2, offset=6) == []

    # Поиск — подстрока ФИО
    assert [row['id'] for row in adapter.list_unlinked_competitions(search='Иванов')] == [3, 4, 2, 5]
    assert [row['id'] for row in adapter.list_unlinked_competitions(search='Петров Пётр')] == [1]
    assert adapter.list_unlinked_competitions(search='Неттаков') == []
    assert adapter.count_unlinked_competitions() == 5
    assert adapter.count_unlinked_competitions(search='Иванов') == 4

    # Привязанная запись исчезает из списка и счётчика
    student_id = adapter.create_student('Иванов Иван', 'М', '', '', '')
    adapter.link_competitions([3], student_id)
    assert [row['id'] for row in adapter.list_unlinked_competitions()] == [4, 2, 5, 1]
    assert adapter.count_unlinked_competitions() == 4

    row = adapter.list_unlinked_competitions(limit=1)[0]
    assert set(row) == {
        'id',
        'student_name',
        'student_sex',
        'institute',
        'group',
        'course',
        'sport',
        'date',
        'date_to',
        'name',
        'level',
        'position',
        'review_status',
    }
    assert row['student_name'] == 'Иванов Иван'
    assert row['date'].startswith('2026-01-05')


def test_find_student_candidates_exact_full_name_and_alias(adapter):
    """Точное совпадение БЕЗ учёта регистра (strip + casefold) по full_name
    или псевдониму; full_name приоритетнее псевдонима."""
    student_id = adapter.create_student('Иванов Иван Иванович', 'М', 'ИСИ', 'ПГС-101', '2')
    adapter.add_student_alias(student_id, 'Иванов И.И.')

    full = adapter.find_student_candidates('Иванов Иван Иванович')
    assert full == [
        {
            'student_id': student_id,
            'full_name': 'Иванов Иван Иванович',
            'sex': 'М',
            'institute': 'ИСИ',
            'group_name': 'ПГС-101',
            'course': '2',
            'match_type': 'full_name',
            'alias_name': None,
        }
    ]

    # Совпадение по псевдониму — та же карточка, но с match_type/alias_name
    by_alias = adapter.find_student_candidates('Иванов И.И.')
    assert by_alias[0]['student_id'] == student_id
    assert by_alias[0]['match_type'] == 'alias'
    assert by_alias[0]['alias_name'] == 'Иванов И.И.'

    # Регистр НЕ важен: другой регистр ФИО/псевдонима — кандидат,
    # отображается каноническое ФИО карточки
    upper = adapter.find_student_candidates('ИВАНОВ ИВАН ИВАНОВИЧ')
    assert [candidate['student_id'] for candidate in upper] == [student_id]
    assert upper[0]['match_type'] == 'full_name'
    assert upper[0]['full_name'] == 'Иванов Иван Иванович'
    lower_alias = adapter.find_student_candidates('иванов и.и.')
    assert lower_alias[0]['student_id'] == student_id
    assert lower_alias[0]['match_type'] == 'alias'
    assert lower_alias[0]['alias_name'] == 'Иванов И.И.'

    # Несколько псевдонимов-регистровых вариантов — одна строка на карточку
    adapter.add_student_alias(student_id, 'ИВАНОВ И.И.')
    two_aliases = adapter.find_student_candidates('ИвАнОв и.И.')
    assert len(two_aliases) == 1
    assert two_aliases[0]['student_id'] == student_id
    assert two_aliases[0]['alias_name'] in {'Иванов И.И.', 'ИВАНОВ И.И.'}

    # full_name приоритетнее псевдонима и при регистровых вариациях
    twin_id = adapter.create_student('Смирнов Семён Семёнович', 'М', '', '', '')
    adapter.add_student_alias(twin_id, 'СМИРНОВ СЕМЁН СЕМЁНОВИЧ')
    twin = adapter.find_student_candidates('смирнов СЕМЁН Семёнович')
    assert twin[0]['student_id'] == twin_id
    assert twin[0]['match_type'] == 'full_name'
    assert twin[0]['alias_name'] is None

    # strip и пустая строка
    assert adapter.find_student_candidates('  Иванов И.И.  ')[0]['student_id'] == student_id
    assert adapter.find_student_candidates('   ') == []

    # Точность: подстрока и другое написание (не только регистр) не совпадают
    assert adapter.find_student_candidates('Иванов') == []
    assert adapter.find_student_candidates('Иванов Иван Ивановч') == []
    assert adapter.find_student_candidates('Иванов Иван Иванович мл.') == []
    assert adapter.find_student_candidates('Петров Иван Иванович') == []


def test_find_student_candidates_exclude_inactive_and_list_namesakes(adapter):
    # Неактивная карточка — не кандидат (ни по ФИО, ни по псевдониму)
    inactive_id = adapter.create_student('Сидоров Сидор Сидорович', 'М', '', '', '')
    adapter.add_student_alias(inactive_id, 'Сидоров С.С.')
    adapter.set_student_active(inactive_id, False)
    assert adapter.find_student_candidates('Сидоров Сидор Сидорович') == []
    assert adapter.find_student_candidates('Сидоров С.С.') == []

    # Полные тёзки — отдельные карточки, обе в кандидатах
    first = adapter.create_student('Козлов Кирилл Кириллович', 'М', 'ИСИ', 'ПГС-101', '1')
    second = adapter.create_student('Козлов Кирилл Кириллович', 'М', 'ИМИ', 'СБ-202', '2')
    candidates = adapter.find_student_candidates('Козлов Кирилл Кириллович')
    assert [candidate['student_id'] for candidate in candidates] == [first, second]
    assert len({candidate['institute'] for candidate in candidates}) == 2

    # Тёзки с регистровыми вариациями написания — тоже все, id разные
    third = adapter.create_student('КОЗЛОВ КИРИЛЛ КИРИЛЛОВИЧ', 'Ж', 'ИПЭ', 'Э-303', '3')
    mixed_case = adapter.find_student_candidates('козлов Кирилл КИРИЛЛОВИЧ')
    assert sorted(candidate['student_id'] for candidate in mixed_case) == sorted([first, second, third])
    assert len({candidate['institute'] for candidate in mixed_case}) == 3
    assert {candidate['match_type'] for candidate in mixed_case} == {'full_name'}
    # Отображается каноническое ФИО каждой карточки — как она записана
    assert {candidate['full_name'] for candidate in mixed_case} == {
        'Козлов Кирилл Кириллович',
        'КОЗЛОВ КИРИЛЛ КИРИЛЛОВИЧ',
    }


def test_link_competitions_atomic_rejects_already_linked(adapter):
    """Массовая привязка — «всё или ничего»: при частично занятом наборе
    не меняется НИ одна запись."""
    adapter.save_competitions(
        [
            make_competition('Иванов Иван', datetime(2026, 1, 10)),
            make_competition('Иванов Иван', datetime(2026, 2, 10)),
        ]
    )
    first = adapter.create_student('Иванов Иван', 'М', '', '', '')
    second = adapter.create_student('Другой Студент', 'Ж', '', '', '')

    count, error = adapter.link_competitions([1], first)
    assert (count, error) == (1, None)

    count, error = adapter.link_competitions([1, 2], second)
    assert (count, error) == (0, 'already_linked')
    refs = {
        row['id']: row['student_ref_id']
        for row in adapter.connection.execute('SELECT id, student_ref_id FROM competitions')
    }
    assert refs == {1: first, 2: None}

    # Повторная привязка той же записи — тоже already_linked
    assert adapter.link_competitions([1], first) == (0, 'already_linked')


def test_link_competitions_rejects_inactive_missing_student_and_records(adapter):
    adapter.save_competitions([make_competition('Иванов Иван', datetime(2026, 1, 10))])
    inactive_id = adapter.create_student('Неактивный', 'М', '', '', '')
    adapter.set_student_active(inactive_id, False)
    active_id = adapter.create_student('Иванов Иван', 'М', '', '', '')

    # Карточка проверяется первой: не существует / неактивна
    assert adapter.link_competitions([1], inactive_id) == (0, 'student_inactive')
    assert adapter.link_competitions([1], 999999) == (0, 'student_not_found')
    # Затем записи: не существует / пустой набор
    assert adapter.link_competitions([999999], active_id) == (0, 'records_not_found')
    assert adapter.link_competitions([], active_id) == (0, 'records_not_found')
    # Ничего не изменилось
    row = adapter.connection.execute('SELECT student_ref_id FROM competitions WHERE id = 1').fetchone()
    assert row['student_ref_id'] is None

    # Дубликаты id в наборе схлопываются
    assert adapter.link_competitions([1, 1], active_id) == (1, None)


def test_unlink_and_relink_competition_return_old_ref(adapter):
    adapter.save_competitions([make_competition('Иванов Иван', datetime(2026, 1, 10))])
    first = adapter.create_student('Иванов Иван', 'М', '', '', '')
    second = adapter.create_student('Новый Студент', 'Ж', '', '', '')

    assert adapter.unlink_competition(999999) is None
    assert adapter.unlink_competition(1) is None  # связи и так нет

    count, error = adapter.link_competitions([1], first)
    assert (count, error) == (1, None)
    assert adapter.relink_competition(1, second) == (first, None)
    assert (
        adapter.connection.execute('SELECT student_ref_id FROM competitions WHERE id = 1').fetchone()['student_ref_id']
        == second
    )
    assert adapter.unlink_competition(1) == second

    # Relink разрешён и без текущей связи — работает как привязка
    assert adapter.relink_competition(1, first) == (None, None)

    adapter.set_student_active(second, False)
    assert adapter.relink_competition(1, second) == (None, 'student_inactive')
    assert adapter.relink_competition(1, 999999) == (None, 'student_not_found')
    assert adapter.relink_competition(999999, first) == (None, 'records_not_found')


def test_user_link_validates_role_and_null_state(adapter):
    student_id = adapter.create_student('Иванов Иван', 'М', '', '', '')
    other_student = adapter.create_student('Другой', 'Ж', '', '', '')
    anna_id = make_athlete_user(adapter, 'anna')
    adapter.create_user('chief', 'scrypt$x', 'admin')
    chief_id = adapter.get_user('chief')['id']

    assert adapter.link_user(999999, student_id) == (0, 'user_not_found')
    assert adapter.link_user(chief_id, student_id) == (0, 'user_not_athlete')
    assert adapter.link_user(anna_id, 999999) == (0, 'student_not_found')

    inactive_id = adapter.create_student('Спящий', 'М', '', '', '')
    adapter.set_student_active(inactive_id, False)
    assert adapter.link_user(anna_id, inactive_id) == (0, 'student_inactive')

    assert adapter.link_user(anna_id, student_id) == (1, None)
    assert adapter.link_user(anna_id, student_id) == (0, 'user_already_linked')
    assert adapter.link_user(anna_id, other_student) == (0, 'user_already_linked')

    # Два аккаунта атлета МОГУТ смотреть на одну карточку
    boris_id = make_athlete_user(adapter, 'boris')
    assert adapter.link_user(boris_id, student_id) == (1, None)

    assert adapter.unlink_user(anna_id) == student_id
    assert adapter.unlink_user(anna_id) is None
    assert adapter.unlink_user(999999) is None

    assert adapter.relink_user(boris_id, other_student) == (student_id, None)
    assert adapter.relink_user(999999, other_student) == (None, 'user_not_found')
    assert adapter.relink_user(chief_id, other_student) == (None, 'user_not_athlete')
    assert adapter.relink_user(boris_id, 999999) == (None, 'student_not_found')
    adapter.set_student_active(other_student, False)
    assert adapter.relink_user(boris_id, other_student) == (None, 'student_inactive')


def test_reconciliation_ops_keep_snapshots_and_legacy_keys(adapter):
    """Привязки меняют ТОЛЬКО student_ref_id: строки записей и пользователей
    байт-в-байт совпадают до и после (кроме самой ссылки)."""
    adapter.save_competitions([make_competition('Иванов Иван', datetime(2026, 1, 10))])
    student_id = adapter.create_student('Иванов Иван', 'М', 'ИСИ', 'ПГС-101', '2')
    anna_id = make_athlete_user(
        adapter,
        'anna',
        profile={'student_name': 'Иванов Иван', 'student_sex': 'М', 'institute': 'ИСИ'},
        aliases=['Иванов И.И.'],
    )

    competition_before = adapter.connection.execute('SELECT * FROM competitions WHERE id = 1').fetchone()
    user_before = adapter.connection.execute('SELECT * FROM users WHERE id = ?', (anna_id,)).fetchone()

    adapter.link_competitions([1], student_id)
    adapter.link_user(anna_id, student_id)
    adapter.relink_competition(1, student_id)
    adapter.unlink_competition(1)
    adapter.unlink_user(anna_id)

    competition_after = adapter.connection.execute('SELECT * FROM competitions WHERE id = 1').fetchone()
    user_after = adapter.connection.execute('SELECT * FROM users WHERE id = ?', (anna_id,)).fetchone()

    def without_ref(row):
        return tuple(value for key, value in zip(row.keys(), tuple(row)) if key != 'student_ref_id')

    assert without_ref(competition_after) == without_ref(competition_before)
    assert without_ref(user_after) == without_ref(user_before)
    # Легаси-ключ и снимки не тронуты даже в связанном состоянии
    adapter.link_competitions([1], student_id)
    adapter.link_user(anna_id, student_id)
    competition_linked = adapter.connection.execute('SELECT * FROM competitions WHERE id = 1').fetchone()
    user_linked = adapter.connection.execute('SELECT * FROM users WHERE id = ?', (anna_id,)).fetchone()
    assert competition_linked['student_id'] == competition_before['student_id']
    assert competition_linked['student_name'] == competition_before['student_name']
    assert user_linked['name_aliases'] == user_before['name_aliases']
    assert user_linked['profile_data'] == user_before['profile_data']


def test_linked_records_and_users_queries(adapter):
    adapter.save_competitions(
        [
            make_competition('Старое ФИО', datetime(2026, 1, 10)),
            make_competition('Старое ФИО', datetime(2026, 2, 20)),
            make_competition('Чужой', datetime(2026, 3, 30)),
        ]
    )
    student_id = adapter.create_student('Новое ФИО', 'М', '', '', '')
    anna_id = make_athlete_user(adapter, 'anna')
    boris_id = make_athlete_user(adapter, 'boris')
    adapter.link_competitions([1, 2], student_id)
    adapter.link_user(anna_id, student_id)
    adapter.link_user(boris_id, student_id)
    adapter.set_user_active(boris_id, False)

    assert adapter.linked_records_count(student_id) == 2
    records = adapter.list_linked_records(student_id)
    # Свежие сверху; student_name — снимок записи, а не ФИО карточки
    assert [record['id'] for record in records] == [2, 1]
    assert records[0]['student_name'] == 'Старое ФИО'
    assert records[0]['sport'] == 'Бег'
    assert records[0]['name'] == 'Кубок'
    assert set(records[0]) == {'id', 'student_name', 'sport', 'date', 'date_to', 'name'}
    # Лимит: показывается не больше limit записей
    assert len(adapter.list_linked_records(student_id, limit=1)) == 1

    users = adapter.linked_athlete_users(student_id)
    assert [(user['id'], user['username'], user['active']) for user in users] == [
        (anna_id, 'anna', 1),
        (boris_id, 'boris', 0),
    ]
    assert adapter.linked_records_count(999999) == 0
    assert adapter.linked_athlete_users(999999) == []


# --- Подсказки автодополнения из карточек студентов (participant entry). ---


def test_search_student_suggestions_substring_casefold_and_order(adapter):
    """Активные карточки по подстроке ФИО: casefold-подстрока в Python,
    алфавит + id, лимит; находятся и студенты без истории участий."""
    first = adapter.create_student('Иванов Иван Иванович', 'М', 'ИСЭиУ', 'ЭБ-241', '2')
    second = adapter.create_student('Иванов Игнат Игоревич', 'Ж', 'ИСИ', 'ПГС-101', '1')
    adapter.create_student('Петров Пётр Петрович', 'М', '', '', '')

    matches = adapter.search_student_suggestions('иванов')
    assert [item['student_id'] for item in matches] == sorted([first, second])
    assert matches[0] == {
        'student_id': first,
        'name': 'Иванов Иван Иванович',
        'sex': 'М',
        'institute': 'ИСЭиУ',
        'group': 'ЭБ-241',
        'course': '2',
    }

    # Регистр запроса не важен, тёзки различимы по student_id.
    upper = adapter.search_student_suggestions('ИВАНОВ ИГ')
    assert [item['student_id'] for item in upper] == [second]
    # Пустой запрос и пробелы — пустой ответ.
    assert adapter.search_student_suggestions('') == []
    assert adapter.search_student_suggestions('   ') == []
    # Лимит обрезает список.
    for index in range(10):
        adapter.create_student(f'Сидоров Сидор {index:02d}', 'М', '', '', '')
    assert len(adapter.search_student_suggestions('Сидоров', limit=4)) == 4


def test_search_student_suggestions_exclude_inactive(adapter):
    inactive_id = adapter.create_student('Иванов Иван Иванович', 'М', 'ИСЭиУ', 'ЭБ-241', '2')
    adapter.set_student_active(inactive_id, False)
    assert adapter.search_student_suggestions('Иванов') == []


# --- «Найти студента»: подстрочный поиск карточек (предпросмотр импорта
# участников события). ---


def test_search_student_candidates_substring_priority_and_limit(adapter):
    """Подстрока query (casefold) по full_name ИЛИ любому псевдониму; одна
    запись на карточку (ФИО-совпадение приоритетнее псевдонима); порядок —
    ФИО-совпадения, затем псевдонимные, внутри по (full_name, id); лимит;
    пустой/пробельный запрос → []. Формат ответа — как у
    find_student_candidates."""
    ivanov = adapter.create_student('Иванов Иван Иванович', 'М', 'ИСЭиУ', 'ЭБ-241', '2')
    nikolaev = adapter.create_student('Николаев Николай Николаевич', 'Ж', 'ИСИ', 'ПГС-101', '1')
    sidorov = adapter.create_student('Сидоров Сидор Сидорович', 'М', 'ИМИ', 'СБ-202', '3')
    kolyadin = adapter.create_student('Колядин Коля Колядыч', 'М', '', '', '')
    for student_id in (nikolaev, sidorov, kolyadin):
        adapter.add_student_alias(student_id, 'Коля')

    # Подстрока ФИО; метаданные — как у кандидатов find_student_candidates.
    assert adapter.search_student_candidates('ванов ив') == [
        {
            'student_id': ivanov,
            'full_name': 'Иванов Иван Иванович',
            'sex': 'М',
            'institute': 'ИСЭиУ',
            'group_name': 'ЭБ-241',
            'course': '2',
            'match_type': 'full_name',
            'alias_name': None,
        }
    ]

    # «Колядин» совпадает и по ФИО, и по псевдониму — ОДНА запись с
    # match_type='full_name'; далее псевдонимные по (full_name, id).
    matches = adapter.search_student_candidates('коля')
    assert [(item['student_id'], item['match_type'], item['alias_name']) for item in matches] == [
        (kolyadin, 'full_name', None),
        (nikolaev, 'alias', 'Коля'),
        (sidorov, 'alias', 'Коля'),
    ]

    # Лимит (дефолт 8) и явный лимит; пустой/пробельный запрос — не поиск.
    for index in range(10):
        adapter.create_student(f'Сидоров Сидор {index:02d}', 'М', '', '', '')
    assert len(adapter.search_student_candidates('Сидоров')) == 8
    assert len(adapter.search_student_candidates('Сидоров', limit=3)) == 3
    assert adapter.search_student_candidates('') == []
    assert adapter.search_student_candidates('   ') == []


def test_search_student_candidates_exclude_inactive(adapter):
    student_id = adapter.create_student('Иванов Иван Иванович', 'М', 'ИСЭиУ', 'ЭБ-241', '2')
    adapter.add_student_alias(student_id, 'Ваня')
    adapter.set_student_active(student_id, False)
    # Неактивная карточка не находится ни по ФИО, ни по псевдониму.
    assert adapter.search_student_candidates('иван') == []
    assert adapter.search_student_candidates('ваня') == []


# ---- P3 Runtime Identity: dual-read кабинета атлета и режим dual/ref. ----
#
# Gate-фиксы: student_ref_id читается общим SELECT записей и SELECT'ами
# пользователей; режим identity_mode и guard переключения — см.
# docs/data-model-decisions.md, Phase 3. Синтетические данные.


def test_competition_select_round_trips_student_ref_id(adapter):
    record = make_competition('Иванов Иван', datetime(2026, 1, 10))
    record.student_ref_id = 7
    adapter.save_competitions([record])

    by_id = adapter.get_competition_by_id(1)
    assert by_id.student_ref_id == 7
    listed = adapter.get_competitions()
    assert [item.student_ref_id for item in listed] == [7]

    # Правка записи не сбрасывает стабильную связь (её меняют только
    # link/unlink/relink сопоставления).
    by_id.discipline = 'Бег 100 м'
    by_id.result = '11.2'
    adapter.update_competition('1', by_id)
    assert adapter.get_competition_by_id(1).student_ref_id == 7

    # NULL-связь существующих записей тоже читается как None
    adapter.save_competitions([make_competition('Петров Пётр', datetime(2026, 2, 10))])
    assert adapter.get_competition_by_id(2).student_ref_id is None


def test_user_select_round_trips_student_ref_id(adapter):
    student_id = adapter.create_student('Иванов Иван', 'М', '', '', '')
    user_id = make_athlete_user(adapter, 'anna')

    assert adapter.get_user('anna')['student_ref_id'] is None
    assert adapter.get_user_by_id(user_id)['student_ref_id'] is None

    assert adapter.link_user(user_id, student_id) == (1, None)
    assert adapter.get_user('anna')['student_ref_id'] == student_id
    assert adapter.get_user_by_id(user_id)['student_ref_id'] == student_id


def test_identity_mode_default_set_and_guarded_flip(adapter):
    # Свежая БД: seed dual; чтение без кэша видит и ручную замену строки
    assert adapter.get_identity_mode() == 'dual'
    adapter.connection.execute("UPDATE app_settings SET value = 'ref' WHERE key = 'identity_mode'")
    adapter.connection.commit()
    assert adapter.get_identity_mode() == 'ref'

    # Прямая установка валидирует значение
    adapter.set_identity_mode('dual')
    with pytest.raises(ValueError):
        adapter.set_identity_mode('single')
    assert adapter.get_identity_mode() == 'dual'

    # Неизвестное значение в БД безопасно откатывается к dual
    adapter.connection.execute("UPDATE app_settings SET value = 'broken' WHERE key = 'identity_mode'")
    adapter.connection.commit()
    assert adapter.get_identity_mode() == 'dual'

    with pytest.raises(ValueError):
        adapter.set_identity_mode_guarded('broken')


def test_identity_mode_default_on_legacy_db(tmp_path):
    # Легаси-БД без app_settings: чтение режима не падает, дефолт dual
    db_path = tmp_path / 'legacy.sqlite3'
    connection = sqlite3.connect(db_path)
    connection.execute('CREATE TABLE competitions (id INTEGER PRIMARY KEY)')
    connection.commit()
    connection.close()
    adapter = SQLiteAdapter(str(db_path))
    assert adapter.get_identity_mode() == 'dual'


def test_scope_clauses_dual_and_ref_modes(adapter):
    """Видимость кабинета: dual = owner OR ref OR хеши; ref = owner OR ref."""
    student_id = adapter.create_student('Иванов Иван', 'М', '', '', '')
    user_id = make_athlete_user(adapter, 'anna', profile={'student_name': 'Иванов Иван'})
    adapter.link_user(user_id, student_id)
    hashes = adapter.athlete_name_hashes(user_id)

    # (a) чужая запись, связанная с карточкой Анны: ref-ветка
    linked = make_competition('Иванов Иван', datetime(2026, 1, 10))
    linked.student_ref_id = student_id
    # (b) чужая запись без связи, совпадающая по ФИО: легаси-хеш
    hash_only = make_competition('Иванов Иван', datetime(2026, 2, 10))
    hash_only.student_id = hashes[0]
    # (c) чужая запись другого человека: не видна ни в одном режиме
    alien = make_competition('Петров Пётр', datetime(2026, 3, 10))
    # (d) своя по owner_id без совпадений ФИО
    own = make_competition('Своё Имя', datetime(2026, 4, 10))
    adapter.save_competitions([linked, hash_only, alien, own])
    adapter.connection.execute('UPDATE competitions SET owner_id = ? WHERE id = 4', (user_id,))
    adapter.connection.commit()

    def visible_ids(mode):
        return sorted(
            int(item.record_id)
            for item in adapter.get_competitions(
                owner_id=user_id,
                student_id_hashes=hashes,
                student_ref_id=student_id,
                identity_mode=mode,
            )
        )

    assert visible_ids('dual') == [1, 2, 4]
    assert visible_ids('ref') == [1, 4]

    # Счётчик с теми же параметрами согласован со списком
    assert (
        adapter.count_competitions_visible(
            owner_id=user_id, student_id_hashes=hashes, student_ref_id=student_id, identity_mode='dual'
        )
        == 3
    )
    assert (
        adapter.count_competitions_visible(
            owner_id=user_id, student_id_hashes=hashes, student_ref_id=student_id, identity_mode='ref'
        )
        == 2
    )

    # Модератор (без owner_id) видит всё; режим не влияет
    assert len(adapter.get_competitions()) == 4


def test_count_hash_only_visible_and_guarded_flip(adapter):
    """Guard без waiver: ref запрещён, пока есть hash-only записи; после
    привязки — разрешён; возврат на dual — всегда."""
    student_id = adapter.create_student('Иванов Иван', 'М', '', '', '')
    user_id = make_athlete_user(adapter, 'anna', profile={'student_name': 'Иванов Иван'})
    adapter.link_user(user_id, student_id)
    hashes = adapter.athlete_name_hashes(user_id)

    record = make_competition('Иванов Иван', datetime(2026, 1, 10))
    record.student_id = hashes[0]
    adapter.save_competitions([record])

    # Хеш-совпадение без owner и без связи с карточкой Анны — hash-only
    assert adapter.count_hash_only_visible() == 1

    ok, count = adapter.set_identity_mode_guarded('ref')
    assert (ok, count) == (False, 1)
    assert adapter.get_identity_mode() == 'dual'

    # Привязка записи к карточке Анны обнуляет счётчик — flip проходит
    assert adapter.link_competitions([1], student_id) == (1, None)
    assert adapter.count_hash_only_visible() == 0
    ok, count = adapter.set_identity_mode_guarded('ref')
    assert (ok, count) == (True, 0)
    assert adapter.get_identity_mode() == 'ref'

    # Возврат на dual разрешён всегда, даже при наличии hash-only записей
    adapter.unlink_competition(1)
    assert adapter.count_hash_only_visible() == 1
    ok, count = adapter.set_identity_mode_guarded('dual')
    assert (ok, count) == (True, 1)
    assert adapter.get_identity_mode() == 'dual'


def test_identity_verification_data_counts(adapter):
    """Отчёт проверки: per-user ветки видимости, hash-only, риск тёзки."""
    anna_student = adapter.create_student('Анна Аннова', 'Ж', '', '', '')
    namesake_student = adapter.create_student('Анна Аннова', 'Ж', '', '', '')
    anna_id = make_athlete_user(adapter, 'anna', profile={'student_name': 'Анна Аннова'})
    make_athlete_user(adapter, 'boris', profile={'student_name': 'Борис Борисов'})
    adapter.link_user(anna_id, anna_student)
    anna_hashes = adapter.athlete_name_hashes(anna_id)

    # Своя по owner; по связи (чужая, ref=карточка Анны); hash-only;
    # запись тёзки (ФИО Анны, но связана с другой карточкой).
    own = make_competition('Своя', datetime(2026, 1, 1))
    by_ref = make_competition('Запись по связи', datetime(2026, 2, 1))
    by_ref.student_ref_id = anna_student
    hash_only = make_competition('Анна Аннова', datetime(2026, 3, 1))
    hash_only.student_id = anna_hashes[0]
    namesake = make_competition('Анна Аннова', datetime(2026, 4, 1))
    namesake.student_ref_id = namesake_student
    namesake.student_id = anna_hashes[0]
    adapter.save_competitions([own, by_ref, hash_only, namesake])
    adapter.connection.execute('UPDATE competitions SET owner_id = ? WHERE id = 1', (anna_id,))
    adapter.connection.commit()

    data = adapter.identity_verification_data()
    anna_row = next(row for row in data['users'] if row['username'] == 'anna')
    assert anna_row['visible_by_owner'] == 1
    assert anna_row['visible_by_ref'] == 1
    assert anna_row['visible_by_hash'] == 2  # hash_only + namesake
    # Обе хеш-записи пропадут из кабинета Анны в ref: одна без связи,
    # вторая (тёзка) связана с ДРУГОЙ карточкой.
    assert anna_row['hash_only_visible'] == 2
    assert anna_row['namesake_risk'] == 1
    boris_row = next(row for row in data['users'] if row['username'] == 'boris')
    assert boris_row['hash_only_visible'] == 0

    assert data['hash_only_total'] == 2
    assert data['disappearing_total'] == 2
    assert [item['id'] for item in data['disappearing']] == [3, 4]
    assert {item['username'] for item in data['disappearing']} == {'anna'}
    assert data['users_total'] == 2


def test_athlete_name_hashes_matches_request_formula(adapter):
    """Хеши storage-у — та же формула, что student_hashes_for_request:
    профиль + псевдонимы, без дублей."""
    user_id = make_athlete_user(
        adapter,
        'anna',
        profile={'student_name': 'Иванов Иван'},
        aliases=['Иванов И.И.', 'Иванов Иван'],
    )
    expected = [hashlib.sha256(name.encode()).hexdigest() for name in ('Иванов И.И.', 'Иванов Иван')]
    assert adapter.athlete_name_hashes(user_id) == expected
    assert adapter.athlete_name_hashes(999999) == []


# ---- Event Model, Wave 1 P0/P1 (целевая архитектура) ----
#
# Фундамент participation-identity (calendar_event_id, student_ref_id,
# discipline): колонки существуют и проводятся через storage, runtime их
# не читает. Синтетические данные.


def make_wave1_legacy_db(tmp_path):
    """Легаси-база Wave 1: competitions без discipline/result/
    calendar_event_id (полная схема до волны), calendar_events без
    regulation-колонок; без строк — данные вставляет тест."""
    db_path = tmp_path / 'legacy-wave1.sqlite3'
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
            date_to TEXT,
            level TEXT NOT NULL,
            name TEXT NOT NULL,
            position INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            extra_data TEXT NOT NULL DEFAULT '{}',
            review_status TEXT NOT NULL DEFAULT 'approved',
            owner_id INTEGER,
            review_comment TEXT NOT NULL DEFAULT '',
            student_ref_id INTEGER
        )
        '''
    )
    connection.execute(
        '''
        CREATE TABLE calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            date TEXT NOT NULL,
            date_to TEXT,
            level TEXT NOT NULL DEFAULT '',
            sport TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        '''
    )
    connection.commit()
    connection.close()
    return db_path


LEGACY_ROW_COLUMNS = (
    'student_id, student_name, student_sex, institute, "group", course, '
    'sport, date, date_to, level, name, position, created_at, extra_data, '
    'review_status, owner_id, review_comment, student_ref_id'
)


def insert_wave1_legacy_competition(db_path):
    """Одна легаси-запись («Легаси Лев», «Кубок», 10.01.2026) — данные,
    которые миграция обязана сохранить."""
    row = (
        'id-1',
        'Легаси Лев',
        'М',
        'ИСИ',
        'ПГС-101',
        2,
        'Бег',
        '2026-01-10T00:00:00',
        None,
        'внутривузовские',
        'Кубок',
        1,
        '2026-01-11T00:00:00',
        '{}',
        'approved',
        None,
        '',
        None,
    )
    connection = sqlite3.connect(db_path)
    connection.execute(
        f'INSERT INTO competitions ({LEGACY_ROW_COLUMNS}) ' f'VALUES ({", ".join("?" for _ in row)})',
        row,
    )
    connection.commit()
    connection.close()
    return row


def test_wave1_fresh_db_schema(adapter):
    # Свежая БД: три новые колонки, app_settings с identity_mode='dual',
    # оба индекса ссылок (обычные, не UNIQUE).
    columns = {row['name'] for row in adapter.connection.execute('PRAGMA table_info(competitions)')}
    assert {'discipline', 'result', 'calendar_event_id'} <= columns

    settings_row = adapter.connection.execute("SELECT value FROM app_settings WHERE key = 'identity_mode'").fetchone()
    assert settings_row is not None
    assert settings_row['value'] == 'dual'
    total = adapter.connection.execute('SELECT COUNT(*) AS total FROM app_settings').fetchone()
    assert total['total'] == 1

    indexes = {row['name'] for row in adapter.connection.execute('PRAGMA index_list(competitions)')}
    assert {
        'idx_competitions_calendar_event_id',
        'idx_competitions_student_ref_id',
    } <= indexes


def test_wave1_migration_adds_columns_to_legacy_db(tmp_path):
    db_path = make_wave1_legacy_db(tmp_path)
    insert_wave1_legacy_competition(db_path)

    adapter = SQLiteAdapter(str(db_path))
    columns = {row['name'] for row in adapter.connection.execute('PRAGMA table_info(competitions)')}
    assert {'discipline', 'result', 'calendar_event_id'} <= columns
    tables = {row['name'] for row in adapter.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert 'app_settings' in tables


def test_wave1_migration_preserves_legacy_rows(tmp_path):
    db_path = make_wave1_legacy_db(tmp_path)
    row = insert_wave1_legacy_competition(db_path)

    adapter = SQLiteAdapter(str(db_path))
    stored = adapter.connection.execute(f'SELECT {LEGACY_ROW_COLUMNS} FROM competitions WHERE id = 1').fetchone()
    assert tuple(stored) == row


def test_wave1_legacy_rows_new_fields_null(tmp_path):
    db_path = make_wave1_legacy_db(tmp_path)
    insert_wave1_legacy_competition(db_path)

    adapter = SQLiteAdapter(str(db_path))
    raw = adapter.connection.execute(
        'SELECT discipline, result, calendar_event_id FROM competitions WHERE id = 1'
    ).fetchone()
    assert raw['discipline'] is None
    assert raw['result'] is None
    assert raw['calendar_event_id'] is None

    record = adapter.get_competitions()[0]
    assert record.student_name == 'Легаси Лев'
    assert record.discipline is None
    assert record.result is None
    assert record.calendar_event_id is None


def test_wave1_reinit_idempotent(tmp_path):
    db_path = str(tmp_path / 'wave1-reinit.sqlite3')
    adapter = SQLiteAdapter(db_path)
    event_id = adapter.create_calendar_event(
        name='Кубок', date='2026-01-10T00:00:00', date_to=None, level='', sport='Бег', url=''
    )
    record = make_competition('Легаси Лев', datetime(2026, 1, 10))
    record.discipline = 'Бег 100 м'
    record.result = '11.2'
    record.calendar_event_id = event_id
    adapter.save_competitions([record])

    adapter = SQLiteAdapter(db_path)
    stored = adapter.get_competitions()[0]
    assert stored.discipline == 'Бег 100 м'
    assert stored.result == '11.2'
    assert stored.calendar_event_id == event_id

    total = adapter.connection.execute('SELECT COUNT(*) AS total FROM app_settings').fetchone()
    assert total['total'] == 1
    mode = adapter.connection.execute("SELECT value FROM app_settings WHERE key = 'identity_mode'").fetchone()
    assert mode['value'] == 'dual'


def test_wave1_backfill_links_unique_preset(adapter):
    event_id = adapter.create_calendar_event(
        name='Кубок', date='2026-01-10T00:00:00', date_to=None, level='', sport='Бег', url=''
    )
    adapter.save_competitions([make_competition('Легаси Лев', datetime(2026, 1, 10))])

    counters = adapter._backfill_competition_calendar_links()
    assert counters['considered'] == 1
    assert counters['matched'] == 1
    assert counters['unmatched'] == 0
    assert counters['ambiguous'] == 0
    assert counters['already_linked'] == 0
    assert adapter.get_competitions()[0].calendar_event_id == event_id


def test_wave1_backfill_without_match_keeps_null(adapter):
    adapter.create_calendar_event(
        name='Другой кубок', date='2026-02-01T00:00:00', date_to=None, level='', sport='', url=''
    )
    adapter.save_competitions([make_competition('Легаси Лев', datetime(2026, 1, 10))])

    counters = adapter._backfill_competition_calendar_links()
    assert counters['matched'] == 0
    assert counters['unmatched'] == 1
    assert counters['ambiguous'] == 0
    assert adapter.get_competitions()[0].calendar_event_id is None


def test_wave1_backfill_ambiguous_preset_skipped(adapter):
    for _ in range(2):
        adapter.create_calendar_event(
            name='Кубок', date='2026-01-10T00:00:00', date_to=None, level='', sport='', url=''
        )
    adapter.save_competitions([make_competition('Легаси Лев', datetime(2026, 1, 10))])

    counters = adapter._backfill_competition_calendar_links()
    assert counters['matched'] == 0
    assert counters['ambiguous'] == 1
    assert counters['unmatched'] == 0
    assert adapter.get_competitions()[0].calendar_event_id is None


def test_wave1_backfill_does_not_touch_linked_rows(tmp_path):
    db_path = str(tmp_path / 'wave1-linked.sqlite3')
    adapter = SQLiteAdapter(db_path)
    adapter.create_calendar_event(name='Кубок', date='2026-01-10T00:00:00', date_to=None, level='', sport='Бег', url='')
    other_event_id = adapter.create_calendar_event(
        name='Другое событие', date='2026-02-01T00:00:00', date_to=None, level='', sport='', url=''
    )
    adapter.save_competitions([make_competition('Легаси Лев', datetime(2026, 1, 10))])
    # Заранее выставленная ссылка (raw SQL — как будто сделана link/unlink
    # будущих фаз), намеренно НЕ совпадающая с пресетом записи.
    adapter.connection.execute('UPDATE competitions SET calendar_event_id = ? WHERE id = 1', (other_event_id,))
    adapter.connection.commit()

    adapter = SQLiteAdapter(db_path)
    assert adapter.get_competitions()[0].calendar_event_id == other_event_id


def test_wave1_fields_roundtrip_crud(adapter):
    event_id = adapter.create_calendar_event(
        name='Кубок', date='2026-01-10T00:00:00', date_to=None, level='', sport='Бег', url=''
    )
    record = make_competition('Раунд Ростислав', datetime(2026, 1, 10))
    record.discipline = 'Бег 100 м'
    record.result = '11.2'
    record.calendar_event_id = event_id
    adapter.save_competitions([record])

    stored = adapter.get_competitions()[0]
    assert stored.discipline == 'Бег 100 м'
    assert stored.result == '11.2'
    assert stored.calendar_event_id == event_id

    by_id = adapter.get_competition_by_id(int(stored.record_id))
    assert by_id.discipline == 'Бег 100 м'
    assert by_id.result == '11.2'
    assert by_id.calendar_event_id == event_id

    page = adapter.get_competitions_page(limit=10)
    assert page[0].discipline == 'Бег 100 м'
    assert page[0].result == '11.2'
    assert page[0].calendar_event_id == event_id

    stored.discipline = 'Эстафета 4х100'
    stored.result = '42.1'
    adapter.update_competition(stored.record_id, stored)
    updated = adapter.get_competition_by_id(int(stored.record_id))
    assert updated.discipline == 'Эстафета 4х100'
    assert updated.result == '42.1'
    # calendar_event_id общий update не трогает — управляемое поле link/unlink.
    assert updated.calendar_event_id == event_id

    imported = make_competition('Импорт Игорь', datetime(2026, 2, 1))
    imported.discipline = 'Прыжки в длину'
    imported.result = '7.10'
    imported.calendar_event_id = event_id
    adapter.import_competitions([imported])
    imported_stored = [item for item in adapter.get_competitions() if item.student_name == 'Импорт Игорь'][0]
    assert imported_stored.discipline == 'Прыжки в длину'
    assert imported_stored.result == '7.10'
    assert imported_stored.calendar_event_id == event_id


# --- P5b: явные связки «запись → событие» (link / create+link / preview / apply). ---


def test_link_participation_to_event_error_codes(adapter):
    """Несуществующая запись/событие — явные коды, БД не меняется."""
    event_id = adapter.create_calendar_event(
        name='Кросс', date='2026-09-12T00:00:00', date_to=None, level='', sport='', url=''
    )

    event, error = adapter.link_participation_to_event(999999, event_id)
    assert (event, error) == (None, 'record_not_found')

    adapter.save_competitions([make_competition('Легаси Лев', datetime(2026, 9, 12))])
    record_id = int(adapter.get_competitions()[0].record_id)
    event, error = adapter.link_participation_to_event(record_id, 999999)
    assert (event, error) == (None, 'event_not_found')
    assert adapter.get_competitions()[0].calendar_event_id is None


def test_link_participation_race_returns_already_linked(adapter):
    """Guarded UPDATE: повторное связывание уже связанной записи не
    перезаписывает ни ссылку, ни поля."""
    first_id = adapter.create_calendar_event(
        name='Первый кросс', date='2026-09-12T00:00:00', date_to=None, level='А', sport='Бег', url=''
    )
    second_id = adapter.create_calendar_event(
        name='Второй кросс', date='2026-10-01T00:00:00', date_to=None, level='Б', sport='Лыжи', url=''
    )
    adapter.save_competitions([make_competition('Легаси Лев', datetime(2026, 9, 12))])
    record_id = int(adapter.get_competitions()[0].record_id)
    # Ссылка уже стоит (как будто её поставил другой запрос между SELECT и
    # UPDATE) — намеренно без синхронизации полей.
    adapter.connection.execute('UPDATE competitions SET calendar_event_id = ? WHERE id = ?', (first_id, record_id))
    adapter.connection.commit()

    event, error = adapter.link_participation_to_event(record_id, second_id)
    assert (event, error) == (None, 'already_linked')
    record = adapter.get_competitions()[0]
    assert record.calendar_event_id == first_id
    # Поля записи при отказе не проиграны во «Второй кросс».
    assert record.name == 'Кубок'
    assert record.sport == 'Бег'


def test_create_calendar_event_and_link_atomic_rollback(adapter):
    """Сбой между INSERT события и UPDATE записи откатывает ВСЁ: события-
    сироты не остаётся, запись не связана."""
    adapter.save_competitions([make_competition('Легаси Лев', datetime(2026, 9, 12))])
    record_id = int(adapter.get_competitions()[0].record_id)

    class FailingUpdateConnection:
        """INSERT события проходит, guarded UPDATE записи (второй шаг
        транзакции) падает — сбой между шагами, до commit."""

        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, parameters=()):
            if sql.strip().startswith('UPDATE competitions'):
                raise RuntimeError('Injected failure between INSERT and UPDATE')
            return self._connection.execute(sql, parameters)

        def commit(self):
            self._connection.commit()

        def rollback(self):
            self._connection.rollback()

    real_connection = adapter.connection
    adapter.connection = FailingUpdateConnection(real_connection)
    with pytest.raises(RuntimeError):
        adapter.create_calendar_event_and_link(
            name='Осенний кросс',
            date='2026-10-01T00:00:00',
            date_to=None,
            level='',
            sport='',
            url='',
            record_id=record_id,
        )
    adapter.connection = real_connection

    assert adapter.list_calendar_events() == []
    assert adapter.get_competitions()[0].calendar_event_id is None


def test_create_calendar_event_and_link_checks_record_first(adapter):
    """Несуществующая/уже связанная запись отклоняется ДО вставки события —
    события-сироты не появляется."""
    event_id, error = adapter.create_calendar_event_and_link(
        name='Сирота', date='2026-10-01T00:00:00', date_to=None, level='', sport='', url='', record_id=999999
    )
    assert (event_id, error) == (None, 'record_not_found')
    assert adapter.list_calendar_events() == []

    linked_id = adapter.create_calendar_event(
        name='Первый кросс', date='2026-09-12T00:00:00', date_to=None, level='', sport='', url=''
    )
    adapter.save_competitions([make_competition('Легаси Лев', datetime(2026, 9, 12))])
    record_id = int(adapter.get_competitions()[0].record_id)
    adapter.connection.execute('UPDATE competitions SET calendar_event_id = ? WHERE id = ?', (linked_id, record_id))
    adapter.connection.commit()

    event_id, error = adapter.create_calendar_event_and_link(
        name='Сирота', date='2026-10-01T00:00:00', date_to=None, level='', sport='', url='', record_id=record_id
    )
    assert (event_id, error) == (None, 'already_linked')
    assert len(adapter.list_calendar_events()) == 1


def test_calendar_link_backfill_preview_groups_and_cap(adapter):
    """Предпросмотр: группы matched/ambiguous/unmatched + уже связанные;
    списки капнутся (200), счётчики останутся полными."""
    matched_event = adapter.create_calendar_event(
        name='Кубок', date='2026-01-10T00:00:00', date_to=None, level='', sport='', url=''
    )
    adapter.save_competitions([make_competition('Легаси Лев', datetime(2026, 1, 10))])

    for _ in range(2):
        adapter.create_calendar_event(
            name='Дубль', date='2026-02-01T00:00:00', date_to=None, level='', sport='', url=''
        )
    daniel = make_competition('Двойной Данила', datetime(2026, 2, 1))
    daniel.name = 'Дубль'
    adapter.save_competitions([daniel])

    # 205 записей без события — unmatched-список упрётся в кап.
    adapter.save_competitions(
        [make_competition(f'Без события {index:03d}', datetime(2026, 3, 1)) for index in range(205)]
    )

    linked_event = adapter.create_calendar_event(
        name='Связанный', date='2026-04-01T00:00:00', date_to=None, level='', sport='', url=''
    )
    adapter.save_competitions([make_competition('Связан Сергей', datetime(2026, 4, 1))])
    adapter.connection.execute(
        'UPDATE competitions SET calendar_event_id = ? WHERE student_name = ?',
        (linked_event, 'Связан Сергей'),
    )
    adapter.connection.commit()

    preview = adapter.calendar_link_backfill_preview()
    assert preview['counters'] == {
        'considered': 207,
        'matched': 1,
        'unmatched': 205,
        'ambiguous': 1,
        'already_linked': 1,
    }
    assert preview['matched'] == [
        {
            'record_id': 1,
            'student_name': 'Легаси Лев',
            'name': 'Кубок',
            'date': '2026-01-10T00:00:00',
            'date_to': None,
            'sport': 'Бег',
            'level': 'внутривузовские',
            'event_id': matched_event,
            'event_name': 'Кубок',
        }
    ]
    ambiguous = preview['ambiguous'][0]
    assert ambiguous['student_name'] == 'Двойной Данила'
    assert ambiguous['candidate_count'] == 2
    assert ambiguous['candidate_names'] == ['Дубль', 'Дубль']
    assert len(preview['unmatched']) == 200


def test_apply_calendar_link_backfill_links_only_matched(adapter):
    """Apply: линкуются ТОЛЬКО однозначные matched с синхронизацией 5 полей;
    ambiguous/unmatched/уже связанные не трогаются."""
    event_id = adapter.create_calendar_event(
        name='Кубок',
        date='2026-01-10T00:00:00',
        date_to='2026-01-12T00:00:00',
        level='региональные',
        sport='Лыжи',
        url='',
    )
    for _ in range(2):
        adapter.create_calendar_event(
            name='Дубль', date='2026-02-01T00:00:00', date_to=None, level='', sport='', url=''
        )
    matched = make_calendar_record('Легаси Лев', datetime(2026, 1, 10), datetime(2026, 1, 12))
    matched.name = 'Кубок'
    ambiguous = make_calendar_record('Двойной Данила', datetime(2026, 2, 1), None)
    ambiguous.name = 'Дубль'
    unmatched = make_calendar_record('Одинокий Олег', datetime(2027, 5, 5), None)
    adapter.save_competitions([matched, ambiguous, unmatched])

    counters, items = adapter.apply_calendar_link_backfill()
    assert counters == {'matched': 1, 'linked': 1, 'skipped': 0}
    assert items == [{'record_id': 1, 'event_id': event_id, 'event_name': 'Кубок'}]

    by_name = {record.student_name: record for record in adapter.get_competitions()}
    linked = by_name['Легаси Лев']
    assert linked.calendar_event_id == event_id
    # Синхронизация 5 полей события.
    assert linked.name == 'Кубок'
    assert linked.sport == 'Лыжи'
    assert linked.date == datetime(2026, 1, 10)
    assert linked.date_to == datetime(2026, 1, 12)
    assert linked.level == 'региональные'
    assert by_name['Двойной Данила'].calendar_event_id is None
    assert by_name['Одинокий Олег'].calendar_event_id is None


def test_apply_calendar_link_backfill_skips_row_linked_after_selection(adapter):
    """Гонка внутри пакета: запись связали между выборкой пар и UPDATE —
    guarded UPDATE даёт rowcount 0, строка пропускается, пакет не валится."""
    adapter.create_calendar_event(name='Кубок', date='2026-01-10T00:00:00', date_to=None, level='', sport='', url='')
    other_event = adapter.create_calendar_event(
        name='Другое', date='2026-06-01T00:00:00', date_to=None, level='', sport='', url=''
    )
    adapter.save_competitions([make_competition('Легаси Лев', datetime(2026, 1, 10))])
    record_id = int(adapter.get_competitions()[0].record_id)

    class RacingLinkConnection:
        """Выборка пар проходит, а сразу ПОСЛЕ неё (до цикла UPDATE)
        запись успевает связать другой запрос — UPDATE ... IS NULL даёт 0."""

        def __init__(self, connection):
            self._connection = connection
            self._raced = False

        def execute(self, sql, parameters=()):
            result = self._connection.execute(sql, parameters)
            if not self._raced and 'FROM competitions c' in sql:
                self._raced = True
                self._connection.execute(
                    'UPDATE competitions SET calendar_event_id = ? WHERE id = ?', (other_event, record_id)
                )
            return result

        def commit(self):
            self._connection.commit()

        def rollback(self):
            self._connection.rollback()

    real_connection = adapter.connection
    adapter.connection = RacingLinkConnection(real_connection)
    counters, items = adapter.apply_calendar_link_backfill()
    adapter.connection = real_connection

    assert counters == {'matched': 1, 'linked': 0, 'skipped': 1}
    assert items == []
    # Ссылка «гонщика» устояла, поля не перезаписаны.
    record = adapter.get_competitions()[0]
    assert record.calendar_event_id == other_event
    assert record.name == 'Кубок'
