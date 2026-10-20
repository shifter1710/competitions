"""Synthetic regression coverage for shared periods, calendar and linked history."""
from datetime import datetime
from datetime import timezone
from urllib.parse import urlencode

import pytest
from pydantic import ValidationError

from src.auth import create_auth_cookie_value
from src.conftest import seed_fixture_levels
from src.main import app
from src.models.competition import Competition
from src.periods import academic_years_for_ranges
from src.periods import period_filter_context
from src.periods import report_period_label
from src.periods import resolve_report_period
from src.periods import today_in_timezone
from src.periods import validate_report_period
from src.routes.calendar import safe_calendar_return
from src.settings import Settings
from src.settings import settings
from src.storage.sqlite import SQLiteAdapter


@pytest.fixture
def store(tmp_path):
    storage = SQLiteAdapter(str(tmp_path / 'synthetic.sqlite3'))
    seed_fixture_levels(storage, 'Учебный уровень')
    yield storage
    storage.connection.close()


def make_record(student_id, start, end=None, **kwargs):
    return Competition(
        student_id='fictional-hash',
        student_name='Учебный Студент Альфа',
        student_sex='М',
        institute='Учебный институт',
        group='ДЕМО-101',
        course=1,
        sport='Демо бег',
        date=datetime.fromisoformat(start),
        date_to=datetime.fromisoformat(end) if end else None,
        name='Учебное участие',
        level='Учебный уровень',
        position=1,
        student_ref_id=student_id,
        **kwargs,
    )


def seed_history(store):
    student = store.create_student('Учебный Студент Альфа', 'М', '', '', '')
    namesake = store.create_student('Учебный Студент Альфа', 'М', '', '', '')
    rows = [make_record(student, '2026-10-01') for _ in range(55)]
    rows += [make_record(student, '2024-09-01'), make_record(student, '2025-08-31')]
    rows += [make_record(student, '2024-08-01', '2025-09-15')]
    rows += [make_record(namesake, '2024-09-01'), make_record(None, '2024-09-01')]
    store.save_competitions(rows)
    return student


@pytest.mark.parametrize(
    'args',
    [
        {'period': '2024-2026'},
        {'period': '0000-0001'},
        {'period': '9999-10000'},
        {'period': 'custom'},
        {'period': 'custom', 'date_from': '31.02.2024', 'date_to': '01.03.2024'},
        {'period': 'custom', 'date_from': '02.09.2024', 'date_to': '01.09.2024'},
        {'date_from': '02.09.2024', 'date_to': '01.09.2024'},
    ],
)
def test_period_rejects_invalid_year_date_or_order(args):
    assert validate_report_period(args)


def test_shared_year_and_legacy_semantics():
    assert resolve_report_period({'period': ['2024-2025']}) == ('01.09.2024', '31.08.2025')
    assert resolve_report_period({'period': 'all', 'date_from': '01.01.2000'}) == ('', '')
    assert resolve_report_period({'date_from': ['01.01.2020']}) == ('01.01.2020', '')
    assert report_period_label({'period': '2024-2025'}) == 'Учебный год 2024/2025'
    assert period_filter_context({}, [])['period_value'] == 'all'
    assert period_filter_context({'date_from': ['01.01.2020']}, [])['period_value'] == 'custom'
    assert academic_years_for_ranges([{'date': '2024-08-31', 'date_to': '2025-09-01'}]) == [2025, 2024, 2023]


def test_calendar_overlap_is_inclusive_and_conjoined_with_sport(store):
    ids = []
    for index, (start, end) in enumerate(
        [
            ('2024-09-01', None),
            ('2025-08-31', None),
            ('2024-08-01', '2024-09-01'),
            ('2025-08-31', '2025-09-20'),
            ('2024-08-01', '2025-09-20'),
            ('2024-08-31', None),
            ('2025-09-01', None),
        ]
    ):
        ids.append(
            store.create_calendar_event(
                name=f'Демо событие {index}',
                date=start,
                date_to=end,
                sport='Демо бег',
                level='',
                links=[],
            )
        )
    store.create_calendar_event(
        name='Другой вид', date='2025-01-01', date_to=None, sport='Демо плавание', level='', links=[]
    )
    selected = store.list_calendar_events(sport='Демо бег', date_from='01.09.2024', date_to='31.08.2025')
    assert {row['id'] for row in selected} == set(ids[:5])
    assert len(store.list_calendar_events()) == 8
    assert store.list_calendar_events(sport="' OR 1=1 --") == []


def test_student_period_is_before_limit_and_identity_safe(store):
    student = seed_history(store)
    assert store.linked_records_count(student) == 58
    selected = store.list_linked_records(student, date_from='01.09.2024', date_to='31.08.2025')
    assert len(selected) == 3
    assert store.linked_records_count(student, date_from='01.09.2024', date_to='31.08.2025') == 3
    first = store.list_linked_records(student)
    second = store.list_linked_records(student, offset=50)
    assert len(first) == 50 and len(second) == 8
    assert set(row['id'] for row in first).isdisjoint(row['id'] for row in second)
    assert [row['id'] for row in first] == sorted((row['id'] for row in first), reverse=True)


@pytest.fixture
def client(store, http_client):
    previous = app.ctx.storage
    app.ctx.storage = store
    for role in ('admin', 'editor', 'viewer', 'athlete'):
        store.create_user('synthetic_' + role, 'unused-hash', role)
    yield http_client
    app.ctx.storage = previous


def headers(role):
    cookie = create_auth_cookie_value(username='synthetic_' + role, role=role)
    return {'cookie': f'{settings.auth_cookie_name}={cookie}'}


@pytest.mark.parametrize('role', ['admin', 'editor', 'viewer'])
def test_staff_viewing_controls_paging_and_period_validation(client, store, role):
    student = seed_history(store)
    event = store.create_calendar_event(
        name='Демо календарь', date='2024-09-01', date_to=None, level='', sport='Демо бег', links=[]
    )
    _, response = client.get('/calendar?period=2024-2025', headers=headers(role))
    assert response.status == 200
    assert 'id="calendar-period"' in response.text and 'Демо календарь' in response.text
    assert 'return=%2Fcalendar%3Fperiod%3D2024-2025' in response.text
    assert ('calendar-new-form' in response.text) == (role != 'viewer')
    _, response = client.get(f'/admin/people/{student}?period=2024-2025', headers=headers(role))
    assert response.status == 200
    assert 'из 3 за выбранный период' in response.text
    assert 'data-history-record=' in response.text
    _, response = client.get(f'/admin/people/{student}?offset=50', headers=headers(role))
    assert response.status == 200 and '51–58 из 58' in response.text
    assert 'Предыдущие' in response.text and 'Следующие' not in response.text
    _, response = client.get(f'/admin/people/{student}?offset=9999999', headers=headers(role))
    assert response.status == 200 and '51–58 из 58' in response.text
    _, response = client.get(f'/admin/people/{student}?period=custom&date_from=99.99.2024', headers=headers(role))
    assert response.status == 400 and 'Некорректная дата' in response.text
    _, response = client.get('/calendar?period=custom', headers=headers(role))
    assert response.status == 400 and 'укажите обе даты' in response.text
    target = f'/admin/people/{student}?period=2024-2025&offset=50&tab=history'
    _, response = client.get(f'/calendar/{event}?' + urlencode({'return': target}), headers=headers(role))
    assert (
        response.status == 200 and 'К студенту' in response.text and 'period=2024-2025&amp;offset=50' in response.text
    )


@pytest.mark.parametrize(
    'target', ['//evil.invalid', '/calendar/../admin', '/%2fexample.com', '/calendar%5cevil', '/calendar\n']
)
def test_calendar_return_rejects_unsafe_destinations(target):
    assert safe_calendar_return(target) is None


def test_calendar_and_card_athlete_denied(client, store):
    student = seed_history(store)
    for url in ('/calendar?period=2024-2025', f'/admin/people/{student}?period=2024-2025'):
        _, response = client.get(url, headers=headers('athlete'))
        assert response.status == 403


@pytest.mark.parametrize(
    'values',
    [
        {'date_from': '01.09.2026'},
        {'date_to': '01.10.2026'},
        {'date_from': '31.02.2026', 'date_to': '01.03.2026'},
        {'date_from': '02.10.2026', 'date_to': '01.10.2026'},
    ],
)
def test_calendar_invalid_custom_dates_retained_without_results(client, values):
    _, response = client.get('/calendar?' + urlencode({'period': 'custom', **values}), headers=headers('viewer'))
    assert response.status == 400
    for value in values.values():
        assert f'value="{value}"' in response.text
    assert 'id="calendar-period-error"' in response.text
    assert 'data-period-today=' in response.text


def test_calendar_equal_day_custom_period_is_valid(client, store):
    store.create_calendar_event('Учебное событие дня', '2026-09-01', None, '', '', [])
    _, response = client.get(
        '/calendar?period=custom&date_from=01.09.2026&date_to=01.09.2026', headers=headers('viewer')
    )
    assert response.status == 200 and 'Учебное событие дня' in response.text


@pytest.mark.parametrize(
    'instant,zone,expected',
    [
        ('2026-12-31T21:30:00+00:00', 'Europe/Moscow', '2027-01-01'),
        ('2026-12-31T19:30:00+00:00', 'Asia/Omsk', '2027-01-01'),
        ('2026-12-31T19:30:00+00:00', 'Europe/Moscow', '2026-12-31'),
    ],
)
def test_calendar_today_uses_explicit_zone_at_date_boundary(monkeypatch, instant, zone, expected):
    frozen = datetime.fromisoformat(instant).astimezone(timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is not None
            return frozen.astimezone(tz)

    monkeypatch.setattr('src.periods.datetime', FrozenDateTime)
    assert today_in_timezone(zone).isoformat() == expected


@pytest.mark.parametrize('invalid_zone', ['Fictional/Unavailable', '', '/invalid/absolute'])
def test_app_timezone_rejects_invalid_configuration(invalid_zone):
    with pytest.raises(ValidationError, match='APP_TIMEZONE must name an available IANA timezone'):
        Settings(_env_file=None, APP_TIMEZONE=invalid_zone)


def test_app_timezone_defaults_to_local_sibadi(monkeypatch):
    monkeypatch.delenv('APP_TIMEZONE', raising=False)
    assert Settings(_env_file=None).app_timezone == 'Asia/Omsk'


def test_calendar_renders_server_today_in_application_zone_without_changing_url_dates(client, store, monkeypatch):
    frozen = datetime.fromisoformat('2026-12-31T19:30:00+00:00')

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is not None
            return frozen.astimezone(tz)

    monkeypatch.setattr('src.periods.datetime', FrozenDateTime)
    monkeypatch.setattr(settings, 'app_timezone', 'Asia/Omsk')
    # No custom date validation is needed here, so the clock subclass cannot
    # affect server strptime validation of restored user strings.
    _, response = client.get('/calendar?period=all&date_to=01.10.2026', headers=headers('viewer'))
    assert response.status == 200
    assert 'data-period-today="01.01.2027"' in response.text
    assert 'name="date_to"' in response.text and 'value="01.10.2026"' in response.text
    student = store.create_student('Учебный студент Гамма', 'М', '', '', '')
    _, history = client.get(f'/admin/people/{student}', headers=headers('viewer'))
    assert history.status == 200 and 'data-period-today=' not in history.text
