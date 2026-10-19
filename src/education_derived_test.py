"""Group-derived education: historical reads, immutable legacy bytes and SQL parity.

All people/groups below are wholly fictional synthetic fixtures.
"""
import json
import sqlite3
from datetime import date
from time import perf_counter

import pytest

from src.education import group_course
from src.education import parse_group_admission_year
from src.models.competition import Competition
from src.records import build_competition
from src.records import competition_to_export_row
from src.records import get_base_field_settings
from src.storage.helpers import participation_content_key
from src.storage.sqlite import SQLiteAdapter


@pytest.fixture
def storage(tmp_path):
    adapter = SQLiteAdapter(str(tmp_path / 'synthetic-education.sqlite3'))
    yield adapter
    adapter.connection.close()


def synthetic_record(group='Синтетика-24А', when='2026-09-01', **extra):
    fields = dict(
        student_id='synthetic-id',
        student_name='Синтетический Участник Альфа',
        student_sex='М',
        institute='Синтетический институт',
        group=group,
        course='FORGED LEGACY COURSE',
        sport='Тестовый спорт',
        date=when,
        level='тестовый уровень',
        name='Синтетическое соревнование',
        position=1,
    )
    fields.update(extra)
    return Competition(**fields)


@pytest.mark.parametrize(
    'group,year',
    [('Синт-24А', 2024), ('А-23Б-24В', 2023), ('А-243', None), ('А–24', None), ('Выпуск', None), ('', None)],
)
def test_confirmed_first_exact_group_token(group, year):
    assert parse_group_admission_year(group) == year


def test_derived_dates_and_unknown_are_not_manual_fallbacks():
    assert group_course('Синт-24А', date(2026, 8, 31)) == 2
    assert group_course('Синт-24А', date(2026, 9, 1)) == 3
    assert group_course('Синт-27А', '2026-09-01T00:00:00') is None
    assert group_course('Неизвестная', '2026-09-01') is None
    assert group_course('Синт-24А', 'invalid') is None
    record = synthetic_record(admission_year=1999)
    assert (record.admission_year, record.course) == (2024, 3)
    assert synthetic_record(group='Неизвестная').course is None


def test_record_form_ignores_missing_and_malformed_course(storage):
    values = {
        'ФИО': 'Синтетический Участник Бета',
        'Пол': 'Ж',
        'Институт': '',
        'Группа': 'Синт-24А',
        'Вид спорта': '',
        'Дата': '01.09.2026',
        'Уровень соревнований': '',
        'Название соревнований': '',
        'Место': '',
    }
    # Simulate a preexisting configurable manual-course setting without migrating it.
    storage.connection.execute("UPDATE field_settings SET value_type='text', required=1 WHERE key='course'")
    storage.connection.commit()
    assert get_base_field_settings(storage)['course']['required'] is False
    for supplied in (None, '', 'arbitrary old text', {'invalid': 'legacy type'}):
        source = dict(values)
        if supplied is not None:
            source['Курс'] = supplied
        assert build_competition(source, custom_fields=[], manual_input=True, storage=storage).course == 3


def test_schema_and_legacy_storage_bytes_are_inert_and_preserved(storage):
    storage.save_competitions([synthetic_record()])
    connection = storage.connection
    connection.execute("UPDATE competitions SET course='Legacy text untouched', admission_year=1999 WHERE id=1")
    connection.commit()
    before_schema = connection.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall()
    before = tuple(connection.execute('SELECT course, admission_year FROM competitions').fetchone())
    # Every ordinary hydration is based on the own historical source facts.
    record = storage.get_competition_by_id(1)
    assert (record.course, record.admission_year) == (3, 2024)
    exported = competition_to_export_row(record, [])
    assert exported['Курс'] == 3
    storage.update_competition('1', record.model_copy(update={'position': 2, 'course': 77, 'admission_year': 2000}))
    assert tuple(connection.execute('SELECT course, admission_year FROM competitions').fetchone()) == before
    assert connection.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall() == before_schema
    with pytest.raises(ValueError, match='отключено'):
        storage.apply_group_admission_backfill()
    assert tuple(connection.execute('SELECT course, admission_year FROM competitions').fetchone()) == before
    storage.save_competitions([synthetic_record(group='Unknown')])
    raw = tuple(connection.execute('SELECT course, admission_year FROM competitions WHERE id=2').fetchone())
    assert raw == ('', None)
    assert storage.get_competition_by_id(2).course is None


def test_current_card_transfer_and_profile_do_not_change_history(storage, monkeypatch):
    monkeypatch.setattr('src.periods.today_in_timezone', lambda zone: date(2026, 10, 19))
    card = storage.create_student('Синтетическая Карточка Альфа', 'Ж', '', 'Синт-25А', '99')
    assert storage.get_student_by_id(card)['course'] == 2
    assert storage.connection.execute('SELECT course FROM students WHERE id=?', (card,)).fetchone()[0] is None
    storage.connection.execute("UPDATE students SET course='Legacy card untouched' WHERE id=?", (card,))
    storage.save_competitions([synthetic_record(student_ref_id=card, when='2025-08-31')])
    storage.update_student(card, 'Синтетическая Карточка Альфа', 'Ж', '', 'Синт-26Б', '77')
    assert storage.get_student_by_id(card)['course'] == 1
    assert storage.get_competition_by_id(1).course == 1
    assert storage.get_competition_by_id(1).admission_year == 2024
    assert (
        storage.connection.execute('SELECT course FROM students WHERE id=?', (card,)).fetchone()[0]
        == 'Legacy card untouched'
    )
    # No profile source course is written or accepted, even with a forged payload.
    storage.connection.execute(
        'INSERT INTO users(username,password_hash,role,profile_data) '
        "VALUES ('synthetic_profile','synthetic-inert-hash','athlete','{}')"
    )
    user = storage.connection.execute("SELECT id FROM users WHERE username='synthetic_profile'").fetchone()[0]
    storage.set_profile(
        user,
        {'student_name': 'Синтетический Профиль Альфа', 'group': 'Синт-25А', 'course': '77', 'admission_year': 1999},
    )
    raw_profile = json.loads(
        storage.connection.execute('SELECT profile_data FROM users WHERE id=?', (user,)).fetchone()[0]
    )
    assert 'course' not in raw_profile and 'admission_year' not in raw_profile
    assert storage.get_profile(user)['course'] == 2


def test_catalog_year_cannot_override_group_or_rewrite_old_bytes(storage):
    storage.add_catalog_value('institute', 'Синтетический институт')
    # Established APIs return ids; find exact row to avoid coupling the fixture.
    parent = storage.find_catalog_row('institute', 'Синтетический институт')
    storage.add_catalog_value('group', 'Синт-24А', parent_id=parent['id'])
    group = storage.find_catalog_row('group', 'Синт-24А', parent_id=parent['id'])
    storage.upsert_group_academic(group['id'], admission_year=2001, duration_years_override=4)
    assert storage.connection.execute('SELECT admission_year FROM group_academic').fetchone()[0] is None
    storage.connection.execute(
        'UPDATE group_academic SET admission_year=1999 WHERE group_catalog_value_id=?', (group['id'],)
    )
    storage.connection.commit()
    storage.upsert_group_academic(group['id'], admission_year='forged', duration_years_override=3)
    assert storage.connection.execute('SELECT admission_year FROM group_academic').fetchone()[0] == 1999
    assert storage.get_group_academic(group['id'])['admission_year'] == 2024
    assert storage.resolve_group_admission_year('', 'Синт-24А') == 2024
    assert storage.get_student_education_context('Синтетический институт', 'Синт-24А')['admission_year'] == 2024
    assert storage.get_student_education_context('', 'Синт-23А')['admission_year'] == 2023
    assert storage.get_student_education_context('', 'Unknown')['admission_year'] is None


def test_reports_options_filters_groups_summary_and_dedup_use_calculated_values(storage):
    storage.save_competitions(
        [synthetic_record(), synthetic_record(when='2026-08-31'), synthetic_record(group='Unknown', position=0)]
    )
    storage.connection.execute("UPDATE competitions SET course='Legacy impossible', admission_year=1999")
    storage.connection.commit()
    assert storage.get_report_course_options() == ['2', '3']
    assert storage.get_report_admission_year_options() == ([2024], True)
    for course, expected in [('2', 1), ('3', 1), ('Legacy impossible', 0)]:
        assert storage.get_report_summary(course=course).count_participation == expected
        assert sum(row.count_participation for row in storage.get_filtered(course=course)) == expected
        assert sum(row.count_participation for row in storage.get_grouped_report('course', course=course)) == expected
    assert storage.get_report_summary(admission_year='1999').count_participation == 0
    assert storage.get_report_summary(admission_year='none').count_participation == 1
    assert {row.slice_value for row in storage.get_grouped_report('course')} == {None, 2, 3}
    args = ['Synthetic', '100 м', 'М', 'Synthetic institute', 'Синт-24А', 'old course', 1, '', {}]
    left = participation_content_key(*args)
    args[5] = 'different arbitrary course'
    assert participation_content_key(*args) == left


def test_report_sql_udfs_support_readonly_connection_without_schema_objects(storage, tmp_path):
    storage.save_competitions([synthetic_record()])
    storage.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    path = storage.connection.execute('PRAGMA database_list').fetchone()[2]
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    connection.create_function('education_admission_year', 1, parse_group_admission_year, deterministic=True)
    connection.create_function('education_course', 2, group_course, deterministic=True)
    assert connection.execute('SELECT education_course("group",date) FROM competitions').fetchone()[0] == 3
    assert not connection.execute("SELECT name FROM sqlite_master WHERE sql LIKE '%education_course%'").fetchall()
    connection.close()
    storage.connection.execute('PRAGMA query_only=ON')
    try:
        assert storage.get_report_summary(course='3').count_participation == 1
        assert storage.get_filtered(course='3')[0].course == 3
        assert storage.get_report_admission_year_options() == ([2024], False)
    finally:
        storage.connection.execute('PRAGMA query_only=OFF')


def test_synthetic_8000_row_report_has_no_n_plus_one(storage, record_property):
    records = [
        synthetic_record(
            group=f'Синт-{20 + i % 7:02d}А',
            student_id=f'synthetic-{i}',
            student_name=f'Синтетический Участник {i}',
            when=f'2026-{1 + i % 12:02d}-10',
            position=i % 6,
        )
        for i in range(8000)
    ]
    storage.save_competitions(records)
    selects = []
    storage.connection.set_trace_callback(
        lambda sql: selects.append(sql) if sql.lstrip().upper().startswith('SELECT') else None
    )
    start = perf_counter()
    rows = storage.get_grouped_report('course', course='3')
    summary = storage.get_report_summary(course='3')
    elapsed = perf_counter() - start
    storage.connection.set_trace_callback(None)
    assert sum(row.count_participation for row in rows) == summary.count_participation
    assert len(selects) == 2
    plan = [
        tuple(row)
        for row in storage.connection.execute(
            'EXPLAIN QUERY PLAN SELECT education_course("group",date),COUNT(*) FROM competitions '
            'WHERE review_status=? GROUP BY education_course("group",date)',
            ('approved',),
        )
    ]
    record_property('report_8000_rows_seconds', elapsed)
    record_property('report_query_plan', repr(plan))
    assert elapsed < 10, f'8k synthetic report unexpectedly slow: {elapsed:.3f}s; {plan}'


def test_event_transaction_recheck_ignores_legacy_course_without_touching_existing_rows(storage):
    from src.storage.helpers import EventParticipationConflictError

    event = storage.create_calendar_event('Синтетический кубок', '2026-09-01', None, '', '', [])
    original = synthetic_record(calendar_event_id=event, discipline='100 м')
    storage.apply_event_participation_batch(event, [original])
    storage.connection.execute("UPDATE competitions SET course='Arbitrary legacy course', admission_year=1999")
    storage.connection.commit()
    participant = storage.list_calendar_event_participants(event)[0]
    assert participant['course'] == 3
    pending = original.model_copy(update={'course': 'Different arbitrary course'})
    with pytest.raises(EventParticipationConflictError, match='identical cardless'):
        storage.apply_event_participation_batch(event, [pending])
    assert storage.connection.execute('SELECT COUNT(*) FROM competitions').fetchone()[0] == 1
    assert tuple(storage.connection.execute('SELECT course,admission_year FROM competitions').fetchone()) == (
        'Arbitrary legacy course',
        1999,
    )
    # Restricted existing-result update still preserves every source/compatibility field.
    storage.apply_event_participation_batch(event, [], [(1, 2, 'Синтетический результат')])
    updated = storage.get_competition_by_id(1)
    assert (updated.course, updated.admission_year, updated.position, updated.result) == (
        3,
        2024,
        2,
        'Синтетический результат',
    )
    assert tuple(storage.connection.execute('SELECT course,admission_year FROM competitions').fetchone()) == (
        'Arbitrary legacy course',
        1999,
    )
