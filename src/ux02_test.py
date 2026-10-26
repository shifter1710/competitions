"""UX-02 regression scenarios, using only fictional data and temporary SQLite."""
import html
import shutil
import subprocess
from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from src.auth import create_auth_cookie_value
from src.conftest import seed_fixture_levels
from src.main import app
from src.main_test import csrf_for
from src.main_test import get_auth_headers
from src.models.competition import Competition
from src.settings import settings
from src.storage.sqlite import SQLiteAdapter


CUSTOM_VALUES = {
    'note': 'Вымышленная заметка кандидата',
    'count': '0',
    'day': '21.10.2026',
    'link': 'https://example.invalid/ux02',
    'empty': '',
    'hidden': 'Активное скрытое поле',
    'discipline_shadow': 'Отдельный custom дисциплины',
    'result_shadow': 'Отдельный custom результата',
}


@pytest.fixture
def ux02_client(http_client, tmp_path):
    storage = SQLiteAdapter(str(tmp_path / 'ux02.sqlite3'))
    seed_fixture_levels(storage, 'внутривузовские')
    for username, role in (
        (settings.auth_admin_username, 'admin'),
        (settings.auth_editor_username, 'editor'),
        (settings.auth_viewer_username, 'viewer'),
        ('ux02-fictional-athlete', 'athlete'),
    ):
        storage.create_user(username, 'synthetic-hash-not-for-login', role)
    for index, (key, label, kind, required) in enumerate(
        (
            ('note', 'Вымышленная заметка', 'text', True),
            ('count', 'Вымышленное число', 'number', False),
            ('day', 'Вымышленная дата', 'date', False),
            ('link', 'Вымышленная ссылка', 'url', False),
            ('empty', 'Пустое поле', 'text', False),
            ('hidden', 'Скрытая заметка', 'text', False),
            ('discipline_shadow', 'Дисциплина', 'text', False),
            ('result_shadow', 'Результат', 'text', False),
        )
    ):
        storage.create_custom_field(key, label, kind, required, key != 'hidden', True, key != 'hidden', index)
    storage.create_custom_field('inactive', 'Архивная заметка', 'text', True, True, True, True, 99)
    inactive = next(field for field in storage.get_custom_fields() if field.key == 'inactive')
    storage.disable_custom_field(inactive.field_id)
    app.ctx.storage = storage
    yield http_client


def fictional_competition(**updates):
    values = dict(
        student_id='ux02-fictional-student-hash',
        student_name='Синтетик Альфа Тестович',
        student_sex='М',
        institute='Вымышленный институт UX02',
        group='ТЕСТ-26',
        sport='Вымышленный спорт Альфа',
        date=datetime(2026, 10, 20),
        date_to=datetime(2026, 10, 22),
        level='внутривузовские',
        name='Вымышленный кубок Альфа',
        position=4,
        discipline='Базовая дисциплина',
        extra_data=dict(CUSTOM_VALUES),
    )
    return Competition(**{**values, **updates})


def queue_candidate(**updates):
    storage = app.ctx.storage
    candidate = fictional_competition(**updates)
    owner_id = storage.get_user(settings.auth_admin_username)['id']
    return storage.add_import_queue_entry(
        candidate.model_dump(mode='json'), matched_record_id=None, created_by=owner_id
    )


class ManualForm(HTMLParser):
    def __init__(self, page, entry_id):
        super().__init__()
        self.action = f'/admin/import-queue/{entry_id}/edit'
        self.active = False
        self.values = {}
        self.select = None
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.active = attrs.get('action') == self.action
        if not self.active:
            return
        if tag == 'input' and 'name' in attrs and attrs['name'] != 'csrf_token':
            self.values[attrs['name']] = attrs.get('value', '')
        if tag == 'select':
            self.select = attrs.get('name')
        if tag == 'option' and self.select and 'selected' in attrs:
            self.values[self.select] = attrs['value']

    def handle_endtag(self, tag):
        if tag == 'form':
            self.active = False
        if tag == 'select':
            self.select = None


def post_edit(client, entry_id, values, *, role='admin', csrf=True):
    headers = get_auth_headers(role)
    return client.post(
        f'/admin/import-queue/{entry_id}/edit',
        headers=headers,
        data={**(csrf_for(headers) if csrf else {}), **values},
        allow_redirects=False,
    )[1]


def unchanged_state(storage):
    """Auth last_seen may change; business records/catalogs/queue/audit must not."""
    return {
        table: [tuple(row) for row in storage.connection.execute(f'SELECT * FROM {table} ORDER BY id')]
        for table in (
            'competitions',
            'catalog_values',
            'levels',
            'import_queue',
            'audit_log',
            'students',
            'calendar_events',
        )
    }


def test_ux02_excel_queue_form_sqlite_preserves_all_active_custom(ux02_client):
    storage = app.ctx.storage
    # Excel's fixed discipline column also populates the identically labelled custom field.
    imported_custom = {**CUSTOM_VALUES, 'discipline_shadow': 'Базовая дисциплина'}
    neighbor = fictional_competition(sport='Вымышленный спорт соседней записи', position=1)
    storage.save_competitions([neighbor])
    storage.add_catalog_value('sport', 'Вымышленный спорт Альфа')
    neighbor_before = storage.get_competitions()[0].model_dump(mode='json')
    fields = storage.get_custom_fields()
    row = {
        'ФИО': 'Синтетик Альфа Тестович',
        'Пол': 'М',
        'Институт': 'Вымышленный институт UX02',
        'Группа': 'ТЕСТ-26',
        'Курс': '',
        'Вид спорта': 'Вымышленный спорт Альфа',
        'Дата': '20-22.10.2026',
        'Уровень соревнований': 'внутривузовские',
        'Название соревнований': 'Вымышленный кубок Альфа',
        'Место': 4,
        **{field.label: imported_custom[field.key] for field in fields},
    }
    workbook = BytesIO()
    pd.DataFrame([row]).to_excel(workbook, index=False)
    headers = get_auth_headers()
    _, response = ux02_client.post(
        '/',
        headers=headers,
        data=csrf_for(headers),
        files={
            'file': (
                'fictional-ux02.xlsx',
                workbook.getvalue(),
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            )
        },
    )
    assert response.status == 200
    entry = storage.list_import_queue()[0]
    assert entry['payload']['extra_data'] == imported_custom
    _, page = ux02_client.get('/admin/import-queue', headers=headers)
    assert page.status == 200
    form = ManualForm(page.text, entry['id']).values
    assert {key: form[f'custom__{key}'] for key in imported_custom} == imported_custom
    assert f'for="edit-{entry["id"]}-custom-hidden"' in page.text
    assert 'data-native-validation-response' in page.text
    before_catalogs = unchanged_state(storage)['catalog_values']
    form['position'] = '3'
    response = post_edit(ux02_client, entry['id'], form)
    assert response.status == 302
    rows = storage.get_competitions()
    assert len(rows) == 2
    saved = next(row for row in rows if row.record_id != neighbor_before['record_id'])
    assert saved.extra_data == imported_custom
    assert saved.position == 3
    assert saved.discipline == 'Базовая дисциплина'
    assert saved.date_to == datetime(2026, 10, 22)
    assert storage.get_import_queue_entry(entry['id'])['status'] == 'accepted'
    assert storage.get_competition_by_id(int(neighbor_before['record_id'])).model_dump(mode='json') == neighbor_before
    assert unchanged_state(storage)['catalog_values'] == before_catalogs
    # Sequential retry is rejected; no second participation or decision event.
    after = unchanged_state(storage)
    assert post_edit(ux02_client, entry['id'], form).status == 409
    assert unchanged_state(storage) == after


def test_ux02_missing_fields_preserve_legacy_refs_and_label_collisions(ux02_client):
    storage = app.ctx.storage
    student_id = storage.create_student('Синтетик Альфа Тестович', 'М', 'Вымышленный институт UX02', 'ТЕСТ-26', '')
    event_id = storage.create_calendar_event(
        'Вымышленный кубок Альфа', '2026-10-20', '2026-10-22', 'внутривузовские', 'Вымышленный спорт Альфа', []
    )
    values = {**CUSTOM_VALUES, 'historical_unknown': 'Старое значение', 'inactive': 'Архивное значение'}
    entry_id = queue_candidate(extra_data=values, student_ref_id=student_id, calendar_event_id=event_id, result='12:34')
    response = post_edit(
        ux02_client,
        entry_id,
        {
            'position': '2',
            'custom__empty': '',
            'custom__link': '',
            'custom__inactive': 'ПОДМЕНА',
            'custom__historical_unknown': 'ПОДМЕНА',
            'custom__injected': 'ПОДМЕНА',
            'result': 'ПОДМЕНА',
            'student_ref_id': '999',
            'calendar_event_id': '999',
            'record_id': '999',
            'student_name': 'ПОДМЕНА',
            'date': '01.01.2000',
        },
    )
    assert response.status == 302
    saved = storage.get_competitions()[0]
    assert saved.extra_data == {**values, 'link': ''}
    assert saved.student_name == 'Синтетик Альфа Тестович'
    assert saved.student_ref_id == student_id
    assert saved.calendar_event_id == event_id
    assert saved.result == '12:34'
    assert saved.discipline == 'Базовая дисциплина'
    assert saved.record_id != '999'
    assert saved.date == datetime(2026, 10, 20)
    assert saved.date_to == datetime(2026, 10, 22)
    assert saved.extra_data['discipline_shadow'] != saved.discipline
    assert saved.extra_data['result_shadow'] != saved.result


@pytest.mark.parametrize(
    'key,bad_value',
    [
        ('custom__note', ''),
        ('custom__count', 'не число'),
        ('custom__day', '31.02.2026'),
        ('custom__link', 'javascript:alert(1)'),
        ('position', 'не место'),
        ('level', '<неизвестный уровень>'),
    ],
)
def test_ux02_validation_keeps_raw_form_pending_and_all_business_data(ux02_client, key, bad_value):
    storage = app.ctx.storage
    storage.save_competitions([fictional_competition(student_name='Синтетик Бета Тестович')])
    entry_id = queue_candidate()
    _, page = ux02_client.get('/admin/import-queue', headers=get_auth_headers())
    form = ManualForm(page.text, entry_id).values
    form.update(
        {
            'name': 'Исправленный вымышленный кубок <&>',
            'institute': 'Новый вымышленный институт',
            'custom__empty': '',
            'custom__hidden': '  Сырой ввод <&>  ',
            key: bad_value,
        }
    )
    before = unchanged_state(storage)
    response = post_edit(ux02_client, entry_id, form)
    assert response.status == 400
    assert unchanged_state(storage) == before
    returned = ManualForm(response.text, entry_id).values
    assert returned == form
    assert f'class="collapse show mt-3" id="edit-{entry_id}"' in response.text
    assert 'aria-expanded="true"' in response.text
    assert 'role="alert"' in response.text
    assert html.escape(form['name'], quote=True) in response.text
    assert 'Проверьте поля и повторите сохранение' in response.text
    # Correct only the rejected value; all other submitted input survives retry.
    returned[key] = ManualForm(page.text, entry_id).values[key]
    assert post_edit(ux02_client, entry_id, returned).status == 302
    saved = next(row for row in storage.get_competitions() if row.student_name == 'Синтетик Альфа Тестович')
    assert saved.name == form['name']
    assert saved.institute == form['institute']
    assert saved.extra_data['hidden'] == form['custom__hidden'].strip()


def test_ux02_missing_required_payload_is_validated_and_pending(ux02_client):
    storage = app.ctx.storage
    entry_id = queue_candidate(extra_data={})
    before = unchanged_state(storage)
    response = post_edit(ux02_client, entry_id, {'position': '3'})
    assert response.status == 400
    assert 'Вымышленная заметка' in response.text
    assert unchanged_state(storage) == before


def test_ux02_old_payload_optional_keys_and_duplicate_now(ux02_client):
    storage = app.ctx.storage
    candidate = fictional_competition()
    storage.save_competitions([candidate])
    payload = candidate.model_dump(mode='json')
    for key in ('result', 'student_ref_id', 'calendar_event_id', 'discipline', 'date_to'):
        payload.pop(key)
    entry_id = storage.add_import_queue_entry(payload, matched_record_id=None, created_by=None)
    # Restore the same full key (discipline is part of that key).
    response = post_edit(ux02_client, entry_id, {'discipline': 'Базовая дисциплина'})
    assert response.status == 302
    assert len(storage.get_competitions()) == 1
    assert storage.get_import_queue_entry(entry_id)['status'] == 'skipped'
    assert post_edit(ux02_client, entry_id, {}).status == 409


@pytest.mark.parametrize('role', ['editor', 'viewer', 'athlete'])
def test_ux02_queue_edit_permissions_unchanged(ux02_client, role):
    storage = app.ctx.storage
    entry_id = queue_candidate()
    before = unchanged_state(storage)
    headers = (
        get_auth_headers(role)
        if role != 'athlete'
        else {'cookie': f'{settings.auth_cookie_name}={create_auth_cookie_value("ux02-fictional-athlete", "athlete")}'}
    )
    _, response = ux02_client.post(
        f'/admin/import-queue/{entry_id}/edit',
        headers=headers,
        data={**csrf_for(headers), 'position': '2'},
        allow_redirects=False,
    )
    assert response.status == 403
    assert unchanged_state(storage) == before


@pytest.mark.parametrize('csrf_value', [None, 'invalid-synthetic-token'])
def test_ux02_queue_edit_csrf_unchanged(ux02_client, csrf_value):
    storage = app.ctx.storage
    entry_id = queue_candidate()
    before = unchanged_state(storage)
    response = post_edit(
        ux02_client, entry_id, {'position': '2', **({'csrf_token': csrf_value} if csrf_value else {})}, csrf=False
    )
    assert response.status == 403
    assert unchanged_state(storage) == before


def test_ux02_custom_edits_validate_and_clear_only_submitted_optional_values(ux02_client):
    storage = app.ctx.storage
    entry_id = queue_candidate()
    response = post_edit(
        ux02_client,
        entry_id,
        {
            'position': '2',
            'custom__note': '  Подтверждённая вымышленная заметка  ',
            'custom__count': '17.9',
            'custom__day': '23.10.2026',
            'custom__link': '',
            'custom__empty': 'Новое необязательное значение',
            'custom__discipline_shadow': 'Отдельная исправленная дисциплина',
            'custom__result_shadow': 'Отдельный исправленный результат',
        },
    )
    assert response.status == 302
    saved = storage.get_competitions()[0]
    assert saved.extra_data == {
        **CUSTOM_VALUES,
        'note': 'Подтверждённая вымышленная заметка',
        'count': '17',
        'day': '23.10.2026',
        'link': '',
        'empty': 'Новое необязательное значение',
        'discipline_shadow': 'Отдельная исправленная дисциплина',
        'result_shadow': 'Отдельный исправленный результат',
    }
    assert saved.discipline == 'Базовая дисциплина'
    assert saved.result is None


def test_ux02_registry_client_regressions():
    node = shutil.which('node') or shutil.which('nodejs')
    assert node, 'Node is required for the UX-02 registry input and manual validation regressions'
    script = Path(__file__).parents[1] / 'scripts' / 'test_registry_input_guards.cjs'
    subprocess.run([node, str(script)], check=True, capture_output=True, text=True)
