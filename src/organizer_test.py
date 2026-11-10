"""ORG-01: fictional organizer snapshots, ownership, migration and Excel contracts."""
import html
import sqlite3
from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO

import pandas as pd
import pytest
from openpyxl import load_workbook

from src import main_test as helpers
from src.main import app
from src.models.competition import Competition
from src.organizers import ORGANIZER_CUSTOM_COLUMN_KEY
from src.organizers import ORGANIZER_LABEL
from src.organizers import ORGANIZER_MAX_LENGTH
from src.organizers import validate_organizer
from src.records import build_competition
from src.records import build_index_dataframe
from src.records import build_link_diff_rows
from src.records import competition_duplicate_key
from src.records import get_base_field_settings
from src.records import REQUIRED_IMPORT_COLUMNS
from src.records import select_export_custom_fields
from src.records import select_template_custom_fields
from src.routes.calendar import competition_link_changes
from src.routes.calendar import event_import_custom_fields
from src.routes.calendar import event_participant_preset
from src.routes.registry import build_import_competitions
from src.routes.registry import build_index_sort_key
from src.routes.registry import split_import_competitions
from src.settings import settings
from src.storage.sqlite import SQLiteAdapter


@pytest.fixture
def storage(tmp_path):
    adapter = SQLiteAdapter(str(tmp_path / 'fictional-organizers.sqlite3'))
    yield adapter
    adapter.connection.close()


@pytest.fixture
def org_client(http_client, storage, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'data_folder', str(tmp_path))
    for role, username in (
        ('admin', settings.auth_admin_username),
        ('editor', settings.auth_editor_username),
        ('viewer', settings.auth_viewer_username or 'viewer'),
    ):
        if not storage.get_user(username):
            storage.create_user(username, 'fictional-unused-password-hash', role)
    app.ctx.storage = storage
    yield http_client


def record(**updates):
    return Competition(
        **{
            'student_id': 'fictional-organizer-hash',
            'student_name': 'Синтетик Организаторов Тестович',
            'student_sex': 'М',
            'institute': 'Вымышленный институт',
            'group': 'ТЕСТ-26',
            'sport': 'Вымышленный спорт',
            'date': datetime(2026, 10, 1),
            'level': '',
            'name': 'Вымышленный кубок',
            'position': 1,
            'organizer': 'Спортивный центр университета',
            **updates,
        }
    )


def event(storage, organizer='Демо центр Альфа', **updates):
    return storage.create_calendar_event(
        **{
            'name': 'Вымышленный кубок',
            'date': '2026-10-01T00:00:00',
            'date_to': None,
            'level': '',
            'sport': 'Вымышленный спорт',
            'links': [],
            'organizer': organizer,
            **updates,
        }
    )


def update_event(storage, event_id, organizer):
    current = storage.get_calendar_event(event_id)
    return storage.update_calendar_event(
        event_id,
        **{key: current[key] for key in ('name', 'date', 'date_to', 'sport', 'level')},
        organizer=organizer,
        links=[('Демо ссылка', 'https://example.invalid/event')],
    )


def save(storage, **updates):
    storage.save_competitions([record(**updates)])
    return storage.get_competitions()[-1]


def form(**updates):
    return {
        'student_name': 'Синтетик Организаторов Тестович',
        'student_sex': 'М',
        'institute': 'Вымышленный институт',
        'group': 'ТЕСТ-26',
        'sport': 'Вымышленный спорт',
        'date': '01.10.2026',
        'level': '',
        'name': 'Вымышленный кубок',
        'position': '1',
        'organizer': 'Демо центр',
        **updates,
    }


def post(client, path, values, role='admin', csrf=True):
    headers = helpers.get_auth_headers(role)
    return client.post(
        path,
        headers=headers,
        data={
            **(helpers.csrf_for(headers) if csrf else {}),
            **values,
        },
        allow_redirects=False,
    )[1]


@pytest.mark.parametrize('value', ['A\nB', 'A\rB', 'A\tB', 'A\x00B', 'A\x7fB', 'A\u2028B', 'A\u2029B', 'я' * 256])
def test_invalid_single_line_values_rejected_at_record_and_event_storage(storage, value):
    with pytest.raises(ValueError):
        validate_organizer(value)
    with pytest.raises(ValueError):
        storage.save_competitions([record(organizer=value)])
    with pytest.raises(ValueError):
        event(storage, value)
    assert storage.count_competitions() == 0
    assert storage.list_calendar_events() == []


@pytest.mark.parametrize(
    'value, expected',
    [
        (None, ''),
        (float('nan'), ''),
        ('', ''),
        ('  Центр  Альфа  ', 'Центр  Альфа'),
        ('я' * 255, 'я' * 255),
        ('=1+1 <>&"', '=1+1 <>&"'),
    ],
)
def test_plain_unicode_and_absence_validation(value, expected):
    assert validate_organizer(value) == expected


def test_additive_idempotent_migration_keeps_every_other_column(storage, tmp_path):
    event_id = event(storage)
    saved = save(storage, calendar_event_id=event_id, result='12,3', extra_data={'fictional': 'legacy'})
    storage.connection.execute('UPDATE competitions SET organizer=NULL WHERE id=?', (saved.record_id,))
    storage.connection.execute('UPDATE calendar_events SET organizer=NULL WHERE id=?', (event_id,))
    storage.connection.commit()
    before = {
        table: [dict(row) for row in storage.connection.execute(f'SELECT * FROM {table}')]
        for table in ('competitions', 'calendar_events')
    }
    # Build an actual old synthetic schema, retaining every unrelated business column.
    storage.connection.execute('ALTER TABLE competitions DROP COLUMN organizer')
    storage.connection.execute('ALTER TABLE calendar_events DROP COLUMN organizer')
    storage.connection.commit()
    path = storage.connection.execute('PRAGMA database_list').fetchone()['file']
    for _ in range(3):
        reopened = SQLiteAdapter(path)
        for table, rows in before.items():
            assert [dict(row) for row in reopened.connection.execute(f'SELECT * FROM {table}')] == rows
        assert reopened.get_competition_by_id(saved.record_id).organizer == ''
        assert reopened.get_calendar_event(event_id)['organizer'] is None
        reopened.connection.close()
    assert storage.connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'


def test_event_snapshots_edit_clear_and_null_link_keep_business_data(storage):
    event_id = event(storage)
    linked = save(storage, calendar_event_id=event_id, organizer='forged', result='12,3')
    independent = save(storage, student_name='Синтетик Бета Тестович', organizer='Свой центр')
    assert linked.organizer == 'Демо центр Альфа'
    assert storage.set_calendar_event_results_confirmation(event_id, True, 1) is None
    before = storage.get_report_summary().model_dump()
    assert before['count_wins'] > 0 and before['count_participation'] > 0
    assert update_event(storage, event_id, 'Демо центр Бета') == 1
    assert storage.get_competition_by_id(linked.record_id).organizer == 'Демо центр Бета'
    assert storage.get_competition_by_id(independent.record_id).organizer == 'Свой центр'
    assert storage.get_report_summary().model_dump() == before
    assert update_event(storage, event_id, '') == 1
    assert storage.get_competition_by_id(linked.record_id).organizer == ''
    update_event(storage, event_id, 'Сохранённый снимок')
    # Baseline has no public calendar-unlink workflow; model the supported NULL state.
    storage.connection.execute('UPDATE competitions SET calendar_event_id=NULL WHERE id=?', (linked.record_id,))
    storage.connection.commit()
    update_event(storage, event_id, 'Другая организация')
    independent_now = storage.get_competition_by_id(linked.record_id)
    assert independent_now.organizer == 'Сохранённый снимок'
    assert independent_now.result == '12,3'
    independent_now.organizer = 'Самостоятельный центр'
    storage.update_competition(linked.record_id, independent_now)
    assert storage.get_competition_by_id(linked.record_id).organizer == 'Самостоятельный центр'


def test_event_sync_failure_rolls_back_event_links_and_all_participations(storage):
    event_id = event(storage)
    saved = save(storage, calendar_event_id=event_id)
    before = storage.get_calendar_event(event_id)
    storage.connection.execute(
        '''CREATE TRIGGER fictional_sync_failure BEFORE UPDATE OF organizer ON competitions
        BEGIN SELECT RAISE(ABORT, 'fictional sync failed'); END'''
    )
    with pytest.raises(sqlite3.IntegrityError, match='fictional sync failed'):
        update_event(storage, event_id, 'Не сохранится')
    assert storage.get_calendar_event(event_id) == before
    assert storage.get_competition_by_id(saved.record_id).organizer == before['organizer']


def test_link_preview_and_single_batch_and_create_from_record(storage):
    saved = save(storage, organizer='Старый центр')
    event_id = event(storage, '')
    diff = build_link_diff_rows(competition_link_changes(saved, storage.get_calendar_event(event_id)))
    assert next(item for item in diff if item['key'] == 'organizer') == {
        'key': 'organizer',
        'label': ORGANIZER_LABEL.lower(),
        'old': 'Старый центр',
        'new': '',
        'clears': True,
    }
    preview = storage.calendar_link_backfill_preview()
    assert preview['matched'][0]['organizer'] == 'Старый центр'
    counters, _ = storage.apply_calendar_link_backfill()
    assert counters['linked'] == 1
    assert storage.get_competition_by_id(saved.record_id).organizer == ''
    another = save(storage, student_name='Синтетик Бета Тестович')
    linked, error = storage.link_participation_to_event(int(another.record_id), event_id)
    assert error is None and linked['organizer'] == ''
    assert storage.get_competition_by_id(another.record_id).organizer == ''
    third = save(storage, student_name='Синтетик Гамма Тестович', name='Другой кубок')
    new_id, error = storage.create_calendar_event_and_link(
        name='Другой кубок',
        date='2026-10-01T00:00:00',
        date_to=None,
        level='',
        sport='',
        links=[],
        record_id=int(third.record_id),
        organizer='Новый организатор',
    )
    assert error is None
    assert storage.get_calendar_event(new_id)['organizer'] == 'Новый организатор'
    assert storage.get_competition_by_id(third.record_id).organizer == 'Новый организатор'


def test_live_event_guard_beats_stale_import_and_inline_or_queue_storage(storage):
    event_id = event(storage, 'Старый центр')
    stale = record(calendar_event_id=event_id, organizer='Старый центр')
    update_event(storage, event_id, 'Текущий центр')
    storage.apply_event_participation_batch(event_id, [stale])
    linked = storage.get_competitions()[0]
    assert linked.organizer == 'Текущий центр'
    forged = linked.model_copy(update={'organizer': 'Подмена'})
    storage.update_competition(linked.record_id, forged)
    assert storage.get_competition_by_id(linked.record_id).organizer == 'Текущий центр'
    assert event_participant_preset(storage.get_calendar_event(event_id))[ORGANIZER_LABEL] == 'Текущий центр'


def test_excel_optional_column_roundtrip_duplicate_and_formula_safety(storage):
    assert ORGANIZER_LABEL not in REQUIRED_IMPORT_COLUMNS
    old = record().model_dump(by_alias=True)
    old['Дата'] = '01.10.2026'
    old.pop(ORGANIZER_LABEL)
    assert build_import_competitions(pd.DataFrame([old]), [], storage)[0].organizer == ''
    for value in ['', 'Спортивный центр университета', '=1+1', '+1', '-2', '@SUM(A1)']:
        source = record(organizer=value)
        frame = build_index_dataframe([source], [])
        assert list(frame.columns).count(ORGANIZER_LABEL) == 1
        buffer = BytesIO()
        frame.to_excel(buffer, index=False, engine='openpyxl')
        buffer.seek(0)
        workbook = load_workbook(buffer)
        index = list(frame.columns).index(ORGANIZER_LABEL) + 1
        assert workbook.active.cell(2, index).data_type != 'f'
        buffer.seek(0)
        imported = build_import_competitions(pd.read_excel(buffer), [], storage)[0]
        assert imported.organizer == ("'" + value if value[:1] in '=+-@' and value else value)
        assert competition_duplicate_key(source) == competition_duplicate_key(imported)
    existing = record(organizer='Сохранить')
    changed = existing.model_copy(update={'organizer': ''})
    new, skipped, queue = split_import_competitions([changed], [existing])
    assert not new and skipped == 1 and not queue


def test_sorting_full_selection_before_pages_both_directions(storage):
    values = ['я', 'Beta', '', 'alpha', 'Альфа'] * 7
    storage.save_competitions(
        [record(organizer=value, student_name=f'Вымышленный участник {i}') for i, value in enumerate(values)]
    )
    key = build_index_sort_key('organizer', [])
    for reverse in (False, True):
        full = storage.get_competitions_page(sort_key=key, sort_reverse=reverse)
        pages = [
            item
            for offset in range(0, 40, 10)
            for item in storage.get_competitions_page(
                sort_key=key,
                sort_reverse=reverse,
                offset=offset,
                limit=10,
            )
        ]
        assert [item.record_id for item in pages] == [item.record_id for item in full]
        assert [item.organizer.casefold() for item in pages] == sorted(
            [value.casefold() for value in values], reverse=reverse
        )


def add_collision(storage, key='legacy_organizer'):
    storage.create_custom_field(key, '  ОРГАНИЗАТОР  ', 'text', True, True, True, True, 0)
    return storage.get_custom_fields()


def test_reserved_collision_preserves_data_and_exempts_hidden_required(storage):
    fields = add_collision(storage, key='organizer')
    assert select_export_custom_fields(fields) == []
    assert select_template_custom_fields(fields) == []
    assert event_import_custom_fields(fields) == []
    incoming = record().model_dump(by_alias=True)
    incoming['Дата'] = '01.10.2026'
    incoming[ORGANIZER_LABEL] = 'Базовое значение'
    built = build_competition(incoming, custom_fields=fields, manual_input=True, storage=storage)
    assert built.organizer == 'Базовое значение'
    existing = record(extra_data={'organizer': 'Старое custom значение'})
    built = build_competition(incoming, custom_fields=fields, manual_input=True, storage=storage, existing=existing)
    assert built.extra_data == existing.extra_data
    storage.update_field_settings({'organizer': ('number', True)})
    assert get_base_field_settings(storage)['organizer'] == {'value_type': 'text', 'required': False}


def test_http_registry_edit_clear_omit_collision_and_escaping(org_client, storage):
    add_collision(storage)
    saved = save(storage, extra_data={'legacy_organizer': 'Сохранённый custom'})
    special = '<script>" & Демо'
    response = post(org_client, f'/competition/{saved.record_id}', form(organizer=special))
    assert response.status == 302
    assert storage.get_competition_by_id(saved.record_id).extra_data['legacy_organizer'] == 'Сохранённый custom'
    values = form()
    values.pop('organizer')
    assert post(org_client, f'/competition/{saved.record_id}', values).status == 302
    assert storage.get_competition_by_id(saved.record_id).organizer == special
    _, page = org_client.get('/', headers=helpers.get_auth_headers())
    assert page.status == 200
    assert special in html.unescape(page.text)
    assert special not in page.text
    assert page.text.count('data-column-key="organizer" data-sort-type="text"') == 1
    assert 'data-column-key="legacy_organizer"' not in page.text
    assert post(org_client, f'/competition/{saved.record_id}', form(organizer='')).status == 302
    assert storage.get_competition_by_id(saved.record_id).organizer == ''
    _, settings_page = org_client.get('/admin/fields', headers=helpers.get_auth_headers())
    assert 'Сохранённые значения не удалены' in settings_page.text
    assert (
        post(org_client, '/admin/fields/base', {'type_organizer': 'number', 'required_organizer': 'on'}).status == 302
    )
    assert get_base_field_settings(storage)['organizer'] == {'value_type': 'text', 'required': False}


def test_http_event_create_edit_manual_add_ownership_and_safe_info(org_client, storage):
    assert post(org_client, '/calendar/new', form(organizer='Демо центр')).status == 302
    event_id = storage.list_calendar_events()[0]['id']
    assert post(org_client, f'/calendar/{event_id}/participants', form(organizer='Подмена')).status == 302
    saved = storage.get_competitions()[0]
    assert saved.organizer == 'Демо центр'
    assert post(org_client, f'/competition/{saved.record_id}', form(organizer='Подмена')).status == 302
    assert storage.get_competition_by_id(saved.record_id).organizer == 'Демо центр'
    special = '<Демо> & "Центр"'
    assert post(org_client, f'/calendar/{event_id}/edit', form(organizer=special)).status == 302
    assert storage.get_competition_by_id(saved.record_id).organizer == special
    for path in ('/calendar', f'/calendar/{event_id}'):
        _, page = org_client.get(path, headers=helpers.get_auth_headers())
        assert page.status == 200
        assert special in html.unescape(page.text)
        assert special not in page.text
        assert 'maxlength="255"' in page.text
    assert post(org_client, '/calendar/new', form(name='Другой демо кубок', organizer='')).status == 302
    assert len(storage.list_calendar_events()) == 2


@pytest.mark.parametrize('role, csrf', [('viewer', True), ('admin', False)])
def test_http_writes_preserve_rbac_csrf(org_client, storage, role, csrf):
    saved = save(storage)
    before = storage.get_competition_by_id(saved.record_id).model_dump()
    assert post(org_client, f'/competition/{saved.record_id}', form(organizer='Подмена'), role, csrf).status == 403
    assert post(org_client, '/calendar/new', form(), role, csrf).status == 403
    assert storage.get_competition_by_id(saved.record_id).model_dump() == before
    assert storage.list_calendar_events() == []


@pytest.mark.parametrize('value', ['X\nY', 'я' * (ORGANIZER_MAX_LENGTH + 1)])
def test_http_rejects_invalid_values_before_persistence(org_client, storage, value):
    assert post(org_client, '/competition', form(organizer=value)).status == 400
    response = post(org_client, '/calendar/new', form(organizer=value))
    assert response.status == 400
    assert storage.count_competitions() == 0 and storage.list_calendar_events() == []


@pytest.mark.parametrize('incoming, expected', [('', 'Сохранить'), (None, 'Сохранить'), ('Новый центр', 'Новый центр')])
def test_http_queue_replace_preserves_blank_or_omitted_organizer(org_client, storage, incoming, expected):
    saved = save(storage, organizer='Сохранить')
    payload = record(name='Другой кубок', organizer=incoming or '').model_dump(mode='json')
    if incoming is None:
        payload.pop('organizer')
    entry_id = storage.add_import_queue_entry(payload, matched_record_id=int(saved.record_id), created_by=None)
    assert post(org_client, f'/admin/import-queue/{entry_id}/replace', {}).status == 302
    assert storage.get_competition_by_id(saved.record_id).organizer == expected


def test_http_queue_edit_template_and_export(org_client, storage):
    add_collision(storage)
    payload = record(
        name='Демо новая запись', organizer='Кандидат', extra_data={'legacy_organizer': 'Старое custom'}
    ).model_dump(mode='json')
    entry_id = storage.add_import_queue_entry(payload, matched_record_id=None, created_by=None)
    _, page = org_client.get('/admin/import-queue', headers=helpers.get_auth_headers())
    assert f'id="edit-{entry_id}-organizer"' in page.text
    assert post(org_client, f'/admin/import-queue/{entry_id}/edit', {'organizer': 'Правка кандидата'}).status == 302
    saved = storage.get_competitions()[0]
    assert saved.organizer == 'Правка кандидата' and saved.extra_data == payload['extra_data']
    for path in ('/template/empty.xlsx', '/export/index'):
        _, response = org_client.get(path, headers=helpers.get_auth_headers())
        assert response.status == 200
        workbook = load_workbook(BytesIO(response.body))
        labels = [cell.value for cell in workbook.active[1]]
        assert labels.count(ORGANIZER_LABEL) == 1
        assert '  ОРГАНИЗАТОР  ' not in labels


def test_http_event_import_row_bulk_and_create_student_use_latest_owner(org_client, storage):
    add_collision(storage)
    event_id = event(storage, 'Демо старый центр')
    names = [f'Синтетик Импорт {index}' for index in range(3)]
    uploaded = helpers.upload_event_xlsx(
        org_client,
        event_id,
        [{'ФИО': name, ORGANIZER_LABEL: 'Подмена из файла', '  ОРГАНИЗАТОР  ': 'Скрытый custom'} for name in names],
    )
    assert uploaded.status == 302
    token = helpers.import_session_token(uploaded)
    # A staged file must never revive an organizer changed after upload.
    update_event(storage, event_id, 'Демо текущий центр')
    assert helpers.post_event_import_action(org_client, event_id, token, 'row/2/add').status == 302
    assert (
        helpers.post_event_import_action(
            org_client,
            event_id,
            token,
            'row/3/create-student',
            {
                'full_name': names[1],
                'sex': 'М',
                'institute': 'Вымышленный институт',
                'group': 'ТЕСТ-26',
            },
        ).status
        == 302
    )
    assert helpers.get_event_preview(org_client, event_id, token).status == 200
    assert helpers.post_event_import_action(org_client, event_id, token, 'bulk-commit').status == 302
    rows = storage.get_competitions()
    assert len(rows) == 3
    assert {item.organizer for item in rows} == {'Демо текущий центр'}
    assert {item.calendar_event_id for item in rows} == {event_id}
    assert all(item.extra_data == {} for item in rows)


def test_http_linked_queue_replace_cannot_override_organizer(org_client, storage):
    event_id = event(storage, 'Владелец события')
    saved = save(storage, calendar_event_id=event_id)
    payload = record(name='Демо кандидат', organizer='Подмена кандидата').model_dump(mode='json')
    entry_id = storage.add_import_queue_entry(payload, matched_record_id=int(saved.record_id), created_by=None)
    assert post(org_client, f'/admin/import-queue/{entry_id}/replace', {}).status == 302
    assert storage.get_competition_by_id(saved.record_id).organizer == 'Владелец события'


def test_same_key_custom_cannot_override_organizer_sort_and_rename_restores_values(org_client, storage):
    fields = add_collision(storage, key='organizer')
    first = save(storage, organizer='Zulu', extra_data={'organizer': 'Alpha legacy'})
    second = save(storage, student_name='Синтетик Бета', organizer='Alpha', extra_data={'organizer': 'Zulu legacy'})
    key = build_index_sort_key('organizer', fields)
    assert [item.record_id for item in storage.get_competitions_page(sort_key=key)] == [
        second.record_id,
        first.record_id,
    ]
    field = fields[0]
    storage.update_custom_field(field.field_id, 'Демо прежний организатор', 'text', True, True, True, True, 0, True)
    _, page = org_client.get('/', headers=helpers.get_auth_headers())
    assert 'Демо прежний организатор' in page.text
    assert page.text.count('data-column-key="organizer" data-sort-type="text"') == 1
    assert f'data-column-key="{ORGANIZER_CUSTOM_COLUMN_KEY}"' in page.text
    assert 'Alpha legacy' in page.text and 'Zulu legacy' in page.text
    assert storage.get_competition_by_id(first.record_id).extra_data == {'organizer': 'Alpha legacy'}
    custom_sort = build_index_sort_key(ORGANIZER_CUSTOM_COLUMN_KEY, storage.get_custom_fields())
    assert [item.record_id for item in storage.get_competitions_page(sort_key=custom_sort)] == [
        first.record_id,
        second.record_id,
    ]


def test_legacy_organizer_values_are_not_rewritten_on_read_startup_or_omitted_edit(storage):
    saved = save(storage)
    raw = '  Старое  значение  '
    storage.connection.execute('UPDATE competitions SET organizer=? WHERE id=?', (raw, saved.record_id))
    storage.connection.commit()
    path = storage.connection.execute('PRAGMA database_list').fetchone()['file']
    reopened = SQLiteAdapter(path)
    assert reopened.get_competition_by_id(saved.record_id).organizer == raw
    existing = reopened.get_competition_by_id(saved.record_id)
    incoming = existing.model_dump(by_alias=True)
    incoming.pop(ORGANIZER_LABEL)
    incoming['Дата'] = '01.10.2026'
    changed = build_competition(incoming, custom_fields=[], manual_input=True, existing=existing)
    changed.position = 2
    reopened.update_competition(existing.record_id, changed)
    assert reopened.get_competition_by_id(existing.record_id).organizer == raw
    reopened.connection.close()


@pytest.mark.parametrize('with_organizer', [False, True])
def test_http_registry_excel_upload_old_and_new_optional_column(org_client, storage, with_organizer):
    incoming = record().model_dump(by_alias=True)
    incoming['Дата'] = '01.10.2026'
    incoming[ORGANIZER_LABEL] = 'Демо импортированный центр'
    if not with_organizer:
        incoming.pop(ORGANIZER_LABEL)
    buffer = BytesIO()
    pd.DataFrame([incoming]).to_excel(buffer, index=False)
    headers = helpers.get_auth_headers('editor')
    _, response = org_client.post(
        '/',
        headers=headers,
        data=helpers.csrf_for(headers),
        files={
            'file': (
                'fictional-organizers.xlsx',
                buffer.getvalue(),
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            ),
        },
        allow_redirects=False,
    )
    assert response.status == 200
    assert storage.get_competitions()[0].organizer == ('Демо импортированный центр' if with_organizer else '')


def test_storage_event_edit_without_organizer_preserves_snapshot(storage):
    event_id = event(storage, 'Сохранить организатора')
    saved = save(storage, calendar_event_id=event_id)
    current = storage.get_calendar_event(event_id)
    storage.update_calendar_event(
        event_id, **{key: current[key] for key in ('name', 'date', 'date_to', 'sport', 'level')}, links=[]
    )
    assert storage.get_calendar_event(event_id)['organizer'] == 'Сохранить организатора'
    assert storage.get_competition_by_id(saved.record_id).organizer == 'Сохранить организатора'


class OrganizerTableDocument(HTMLParser):
    """Observe rendered header wiring and row values rather than helper output."""

    def __init__(self, body):
        super().__init__()
        self.headers = []
        self.rows = []
        self.row = None
        self.cell_key = None
        self.cell_text = ''
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'th' and 'data-column-key' in attrs:
            self.headers.append(attrs)
        if tag == 'tr':
            self.row = {}
        if tag == 'td' and 'data-column-key' in attrs:
            self.cell_key = attrs['data-column-key']
            self.cell_text = ''

    def handle_data(self, data):
        if self.cell_key is not None:
            self.cell_text += data

    def handle_endtag(self, tag):
        if tag == 'td' and self.cell_key is not None:
            assert self.cell_key not in self.row, f'Duplicate registry column key {self.cell_key}'
            self.row[self.cell_key] = self.cell_text.strip()
            self.cell_key = None
        if tag == 'tr' and self.row:
            self.rows.append(self.row)
            self.row = None


def test_http_three_organizer_columns_have_distinct_sort_edit_and_export_contracts(org_client, storage):
    flags = {
        'field_type': 'text',
        'show_in_table': 'on',
        'show_in_export': 'on',
        'show_in_template': 'on',
        'active': 'on',
    }
    # Both keys are reachable through the ordinary admin field APIs, without raw DB writes.
    assert post(org_client, '/admin/fields', {'label': 'organizer', **flags}).status == 302
    original = next(field for field in storage.get_custom_fields() if field.key == 'organizer')
    assert post(org_client, f'/admin/fields/{original.field_id}', {'label': ORGANIZER_LABEL, **flags}).status == 302
    assert post(org_client, '/admin/fields', {'label': 'custom__organizer', **flags}).status == 302
    assert {field.key for field in storage.get_custom_fields()} == {'organizer', 'custom__organizer'}
    first = save(
        storage, organizer='Zulu base', extra_data={'organizer': 'Alpha legacy', 'custom__organizer': 'Zulu other'}
    )
    save(
        storage,
        student_name='Синтетик Второй',
        organizer='Alpha base',
        extra_data={'organizer': 'Zulu legacy', 'custom__organizer': 'Alpha other'},
    )
    _, before_rename = org_client.get('/', headers=helpers.get_auth_headers())
    assert ORGANIZER_CUSTOM_COLUMN_KEY not in {
        item['data-column-key'] for item in OrganizerTableDocument(before_rename.text).headers
    }
    assert storage.get_competition_by_id(first.record_id).extra_data['organizer'] == 'Alpha legacy'
    renamed_label = 'Демо прежний организатор'
    assert post(org_client, f'/admin/fields/{original.field_id}', {'label': renamed_label, **flags}).status == 302
    expected_order = {
        'organizer': ['Alpha base', 'Zulu base'],
        ORGANIZER_CUSTOM_COLUMN_KEY: ['Alpha legacy', 'Zulu legacy'],
        'custom__organizer': ['Alpha other', 'Zulu other'],
    }
    for sort_key, expected in expected_order.items():
        for direction in ('asc', 'desc'):
            _, page = org_client.get(
                f'/?sort={sort_key}&order={direction}&per_page=10', headers=helpers.get_auth_headers()
            )
            assert page.status == 200
            document = OrganizerTableDocument(page.text)
            header_keys = [item['data-column-key'] for item in document.headers]
            assert len(header_keys) == len(set(header_keys))
            for column_key in expected_order:
                header = next(item for item in document.headers if item['data-column-key'] == column_key)
                assert header['data-sortable'] == 'true'
            active = next(item for item in document.headers if item['data-column-key'] == sort_key)
            assert active['aria-sort'] == ('ascending' if direction == 'asc' else 'descending')
            assert [row[sort_key] for row in document.rows if sort_key in row] == (
                expected if direction == 'asc' else expected[::-1]
            )
    changed = {
        'organizer': 'Базовое изменено',
        'custom__organizer': 'Прежнее custom изменено',
        'custom__custom__organizer': 'Другое custom изменено',
    }
    assert post(org_client, f'/competition/{first.record_id}', form(**changed)).status == 302
    saved = storage.get_competition_by_id(first.record_id)
    assert saved.organizer == changed['organizer']
    assert saved.extra_data == {
        'organizer': changed['custom__organizer'],
        'custom__organizer': changed['custom__custom__organizer'],
    }
    _, exported = org_client.get('/export/index', headers=helpers.get_auth_headers())
    frame = pd.read_excel(BytesIO(exported.body)).fillna('')
    assert len(frame.columns) == len(set(frame.columns))
    exported_first = frame.loc[frame['ФИО'] == first.student_name].iloc[0]
    assert exported_first[ORGANIZER_LABEL] == changed['organizer']
    assert exported_first[renamed_label] == changed['custom__organizer']
    assert exported_first['custom__organizer'] == changed['custom__custom__organizer']
    assert post(org_client, '/competition', form(student_name='Синтетик Третий', **changed)).status == 302
    created = next(item for item in storage.get_competitions() if item.student_name == 'Синтетик Третий')
    assert created.organizer == changed['organizer'] and created.extra_data == saved.extra_data


@pytest.mark.parametrize('new_organizer', ['Вымышленный новый центр', ''])
def test_http_create_event_from_registry_audit_matches_saved_organizer(org_client, storage, new_organizer):
    old_organizer = 'Вымышленный исходный центр'
    original = save(storage, organizer=old_organizer)
    response = post(
        org_client,
        f'/competition/{original.record_id}/link-event/new',
        form(
            name='Вымышленный новый кубок аудита',
            organizer=new_organizer,
        ),
    )
    assert response.status == 302
    linked = storage.get_competition_by_id(original.record_id)
    saved_event = storage.get_calendar_event(linked.calendar_event_id)
    assert linked.record_id == original.record_id and linked.position == original.position
    assert linked.organizer == saved_event['organizer'] == new_organizer
    audit = helpers.audit_details(storage, 'participation_linked')[0]
    assert audit['record_id'] == int(original.record_id)
    assert audit['event_id'] == saved_event['id']
    assert audit['event_created'] is True
    assert audit['changes']['organizer'] == {'old': old_organizer, 'new': saved_event['organizer']}
