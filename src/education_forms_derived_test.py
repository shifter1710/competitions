"""Synthetic regression coverage for derived education at form/import boundaries."""
import hmac
import html
import re
from datetime import date
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import parse_qsl

import pandas as pd
import pytest

from src.auth import create_auth_cookie_value
from src.conftest import seed_fixture_levels
from src.main import app
from src.models.competition import Competition
from src.routes.admin import parse_base_field_settings_form
from src.routes.auth import read_profile_payload
from src.routes.calendar import event_import_row_display
from src.routes.calendar import event_import_row_record
from src.routes.calendar import event_participant_import_columns
from src.routes.calendar import normalize_import_position
from src.routes.calendar import parse_event_participant_rows
from src.routes.registry import _index_course_row_value
from src.routes.registry import queue_candidate_view
from src.routes.students import parse_student_import_rows
from src.routes.students import student_import_preview_context
from src.settings import settings
from src.storage.sqlite import SQLiteAdapter
from src.students import parse_student_form


@pytest.fixture
def store(tmp_path):
    storage = SQLiteAdapter(str(tmp_path / 'fictional-education.sqlite3'))
    seed_fixture_levels(storage, 'Демо уровень')
    yield storage
    storage.connection.close()


@pytest.fixture
def client(store):
    previous = app.ctx.storage
    app.ctx.storage = store
    for role in ('admin', 'editor', 'viewer', 'athlete'):
        store.create_user('fictional_education_' + role, 'unused-hash', role)
    yield app.test_client
    app.ctx.storage = previous


def auth(role='admin'):
    cookie = create_auth_cookie_value(username='fictional_education_' + role, role=role)
    headers = {'cookie': f'{settings.auth_cookie_name}={cookie}'}
    csrf = hmac.new(settings.auth_secret_key.encode(), b'csrf:' + cookie.encode(), sha256).hexdigest()
    return headers, {'csrf_token': csrf}


def test_forged_manual_course_is_ignored_without_losing_other_form_values():
    request = SimpleNamespace(
        form={
            'full_name': 'Вымышленный Студент Альфа',
            'sex': 'Ж',
            'institute': 'Демо институт',
            'group': 'Демо-24А',
            'course': 'несуществующий курс',
            'student_name': 'Вымышленный Студент Альфа',
            'student_sex': 'Ж',
        }
    )
    student, error = parse_student_form(request)
    assert error is None and student['course'] == ''
    assert student['group_name'] == 'Демо-24А' and student['institute'] == 'Демо институт'
    profile, error = read_profile_payload(request)
    assert error is None and 'course' not in profile
    assert profile['group'] == 'Демо-24А' and profile['student_sex'] == 'Ж'
    base = parse_base_field_settings_form(SimpleNamespace(form={'required_course': 'on', 'type_course': 'text'}))
    assert base['course'][1] is False


@pytest.mark.parametrize('legacy_course', [None, 99, 'Выпускник', 'произвольный мусор'])
def test_legacy_course_import_does_not_become_an_error_or_source(store, monkeypatch, legacy_course):
    monkeypatch.setattr('src.periods.today_in_timezone', lambda _name: date(2026, 9, 1))
    frame = pd.DataFrame(
        [
            {
                'ФИО': 'Вымышленный Студент Бета',
                'Пол': 'Ж',
                'Группа': 'Демо-24А',
                'Курс': legacy_course,
                'Место': 2.0,
                'Дисциплина': 'Демо 100 м',
            }
        ]
    )
    students, unknown = parse_student_import_rows(frame)
    assert students[0]['status'] == 'pending' and students[0]['course'] == 3
    assert 'Курс' not in unknown
    rows, unknown, _custom = parse_event_participant_rows(frame, [])
    assert rows[0]['status'] == 'pending' and rows[0]['course'] == ''
    assert rows[0]['position'] == '2' and rows[0]['discipline'] == 'Демо 100 м'
    assert 'Курс' not in unknown and 'Курс' not in event_participant_import_columns([])
    assert normalize_import_position(3.0) == '3'
    # Card course is current; a historical participation must never copy it.
    record, autofilled, _ = event_import_row_record(
        store,
        {**rows[0], 'course': 'старое значение'},
        {'Дата': '01.09.2024'},
        {'sex': 'Ж', 'institute': 'Демо институт', 'group_name': 'Демо-25Б', 'course': 88},
        [],
    )
    assert record['Группа'] == 'Демо-24А' and record.get('Курс', '') == ''
    assert 'Курс' not in autofilled and record['Место'] == '2'


def test_staged_student_preview_recalculates_after_group_correction(store, monkeypatch):
    monkeypatch.setattr('src.periods.today_in_timezone', lambda _name: date(2026, 9, 1))
    session = {
        'token': 'fictional',
        'filename': 'fictional.xlsx',
        'unknown_columns': [],
        'rows': [
            {
                'row_number': 2,
                'full_name': 'Вымышленный Студент Гамма',
                'sex': '',
                'institute': '',
                'group': 'Демо-24А',
                'course': 'старый текст',
                'status': 'pending',
                'error': None,
            }
        ],
    }
    assert student_import_preview_context(store, session)['groups']['new'][0]['course'] == 3
    session['rows'][0]['group'] = 'Демо-25Б'
    assert student_import_preview_context(store, session)['groups']['new'][0]['course'] == 2
    session['rows'][0]['group'] = 'Выпуск'
    assert student_import_preview_context(store, session)['groups']['new'][0]['course'] is None


def test_registry_sort_and_queue_show_historical_group_values():
    row = {'group': 'Демо-24А', 'date': '2026-08-31T00:00:00', 'course': 99, 'admission_year': 2001}
    assert _index_course_row_value(row) == 2
    assert dict(queue_candidate_view(row))['Курс'] == '2'
    row['group'] = 'Выпуск'
    assert _index_course_row_value(row) is None
    assert dict(queue_candidate_view(row))['Курс'] == ''


@pytest.mark.parametrize('url', ['/template/empty.xlsx', '/admin/people/import/template'])
def test_new_templates_omit_manual_course(client, url):
    headers, _ = auth()
    _, response = client.get(url, headers=headers)
    assert response.status == 200
    columns = pd.read_excel(BytesIO(response.body)).columns
    assert 'Курс' not in columns and 'ФИО' in columns


def test_student_form_save_preserves_legacy_bytes_and_removes_manual_control(client, store, monkeypatch):
    monkeypatch.setattr('src.periods.today_in_timezone', lambda _name: date(2026, 9, 1))
    student_id = store.create_student('Вымышленный Студент Дельта', 'Ж', 'Демо институт', 'Демо-24А', '')
    store.connection.execute('UPDATE students SET course = ? WHERE id = ?', ('неприменимый курс', student_id))
    store.connection.commit()
    headers, csrf = auth()
    _, response = client.get(f'/admin/people/{student_id}', headers=headers)
    assert response.status == 200
    assert 'name="course"' not in response.text
    assert 'неприменимый курс' not in response.text
    _, response = client.post(
        f'/admin/people/{student_id}/edit',
        headers=headers,
        data={
            **csrf,
            'full_name': 'Вымышленный Студент Дельта',
            'sex': 'М',
            'institute': 'Демо институт',
            'group': 'Демо-25Б',
            'course': '99',
        },
        allow_redirects=False,
    )
    assert response.status == 302
    current = store.get_student_by_id(student_id)
    assert current['group_name'] == 'Демо-25Б' and current['course'] == 2 and current['sex'] == 'М'
    assert (
        store.connection.execute('SELECT course FROM students WHERE id = ?', (student_id,)).fetchone()[0]
        == 'неприменимый курс'
    )


def test_obsolete_backfill_refuses_writes_with_roles_and_csrf_intact(client, store):
    headers, csrf = auth()
    calls = []
    store.apply_group_admission_backfill = lambda: calls.append('must not execute')
    _, response = client.post('/admin/maintenance/admission-years/apply', headers=headers, data=csrf)
    assert response.status == 409 and calls == []
    _, response = client.post('/admin/maintenance/admission-years/apply', headers=headers, data={})
    assert response.status == 403
    viewer, csrf = auth('viewer')
    _, response = client.post('/admin/maintenance/admission-years/apply', headers=viewer, data=csrf)
    assert response.status == 403


def test_unavailable_legacy_report_filter_remains_selected(client):
    headers, _ = auth()
    _, response = client.get('/reports?period=all&course=old-course&admission_year=2098', headers=headers)
    assert response.status == 200
    assert re.search(r'<option value="old-course" selected>', response.text)
    assert re.search(r'<option value="2098" selected>', response.text)


def test_invalid_import_other_field_keeps_derived_date_preview():
    view = event_import_row_display({'Группа': 'Демо-24А', 'Дата': '31.08.2026-01.09.2026', 'Курс': '99'}, None)
    assert view['course'] == 2  # Start date controls even for an event crossing Sep 1.


def test_profile_save_returns_current_derived_values_and_omits_stored_course(client, store, monkeypatch):
    monkeypatch.setattr('src.periods.today_in_timezone', lambda _name: date(2026, 9, 1))
    headers, csrf = auth('athlete')
    _, response = client.post(
        '/api/profile',
        headers=headers,
        data={
            **csrf,
            'student_name': 'Вымышленный Студент Профиль',
            'student_sex': 'Ж',
            'group': 'Демо-24А',
            'course': 'мусор',
        },
    )
    assert response.status == 200
    assert response.json['profile']['course'] == 3
    assert response.json['profile']['admission_year'] == 2024
    stored = store.connection.execute(
        'SELECT profile_data FROM users WHERE username = ?', ('fictional_education_athlete',)
    ).fetchone()[0]
    assert 'course' not in stored and 'admission_year' not in stored


@pytest.mark.parametrize(
    'selected,include_unknown', [('2024', False), ('2098', False), ('none', False), ('none', True)]
)
def test_admission_year_filter_has_one_correct_selected_option(client, store, selected, include_unknown):
    records = [
        Competition(
            student_id=f'fictional-admission-{index}',
            student_name=f'Вымышленный Участник {index}',
            student_sex='Ж',
            institute='Демо институт',
            group=group,
            date=datetime(2026, 9, 1),
            sport='Демо бег',
            name='Демо старт',
            level='Демо уровень',
            position=1,
        )
        for index, group in enumerate(['Демо-24А', 'Демо-27Б'] + (['Неопределимая группа'] if include_unknown else []))
    ]
    store.save_competitions(records)
    # Storage intentionally returns numeric years; form query values are strings.
    assert store.get_report_admission_year_options()[0] == [2027, 2024]
    headers, _ = auth()
    _, response = client.get(f'/reports?period=all&admission_year={selected}', headers=headers)
    assert response.status == 200
    selector = re.search(r'<select[^>]*name="admission_year"[^>]*>(.*?)</select>', response.text, re.S).group(1)
    options = re.findall(r'<option value="([^"]*)"([^>]*)>(.*?)</option>', selector)
    assert [value for value, attrs, _label in options if 'selected' in attrs] == [selected]
    assert sum(value == '2024' for value, _attrs, _label in options) == 1
    assert sum(value == selected for value, _attrs, _label in options) == 1
    selected_label = next(label for value, _attrs, label in options if value == selected)
    if selected == '2024':
        assert selected_label == '2024'
        assert 'нет доступных значений' not in selector
    elif selected == '2098':
        assert 'нет доступных значений' in selected_label
    else:
        assert selected_label == 'Не указан'
    applied = re.search(r'data-applied-query="([^"]*)"', response.text).group(1)
    assert dict(parse_qsl(html.unescape(applied)))['admission_year'] == selected
    assert f'name="admission_year" value="{selected}"' in response.text


def test_event_and_help_describe_derived_course_without_manual_defaults(client, store):
    event_id = store.create_calendar_event(
        name='Демо соревнование',
        date='2026-09-01T00:00:00',
        date_to=None,
        level='Демо уровень',
        sport='Демо бег',
        links=[],
    )
    headers, _ = auth()
    _, event = client.get(f'/calendar/{event_id}', headers=headers)
    assert event.status == 200
    text = re.sub(r'\s+', ' ', event.text)
    assert 'Пол, институт и группа подставляются только в пустые поля; значения формы имеют приоритет.' in text
    assert 'Курс рассчитывается по собственной исторической группе участия и дате начала соревнования.' in text
    assert 'группа и курс подставляются' not in text
    assert 'подскажут пол, институт, группу и курс' not in text
    _, guide = client.get('/admin/data-guide', headers=headers)
    assert guide.status == 200
    text = re.sub(r'\s+', ' ', guide.text)
    assert 'текущий курс определяются по группе и сегодняшней дате Омска, вручную их не задают.' in text
    assert 'институт, группу и курс можно заполнить позже' not in text
    assert 'Курс рассчитывается по собственной исторической группе участия и дате начала соревнования.' in text
    _, data_map = client.get('/admin/data-map', headers=headers)
    assert data_map.status == 200
    assert 'курс рассчитывается по этой исторической группе' in re.sub(r'\s+', ' ', data_map.text)
