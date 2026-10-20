"""LEVEL-01 strict catalog, synthetic runtime and historic snapshots."""
import pandas as pd
import pytest

from src import main_test as helpers
from src.levels import InvalidCompetitionLevelError
from src.levels import resolve_competition_level
from src.main import app
from src.records import build_competition
from src.routes.registry import build_import_competitions
from src.storage.sqlite import SQLiteAdapter


client = helpers.client


@pytest.fixture
def catalog(tmp_path):
    storage = SQLiteAdapter(str(tmp_path / 'fictional-levels.sqlite3'))
    storage.create_level('Демо уровень')
    storage.create_level('Скрытый уровень')
    storage.disable_level(next(item['id'] for item in storage.list_levels() if item['name'] == 'Скрытый уровень'))
    yield storage
    storage.connection.close()


def row(level='Демо уровень', name='Вымышленный участник Альфа'):
    return {
        'ФИО': name,
        'Пол': 'М',
        'Институт': 'Демо институт',
        'Группа': 'Демо-24А',
        'Вид спорта': 'Демо бег',
        'Дата': '01.10.2026',
        'Уровень соревнований': level,
        'Название соревнований': 'Вымышленный кубок',
        'Место': '1',
        'Курс': '',
    }


def make_record(storage, level='Демо уровень'):
    return build_competition(row(level), custom_fields=[], manual_input=True, storage=storage)


def save_history(storage, level):
    record = make_record(storage)
    storage.save_competitions([record])
    record = storage.get_competitions()[0]
    storage.connection.execute('UPDATE competitions SET level = ? WHERE id = ?', (level, record.record_id))
    storage.connection.commit()
    return storage.get_competition_by_id(record.record_id)


@pytest.mark.parametrize('value', ['ДЕМО УРОВЕНЬ', '  демо уровень  '])
def test_active_normalization(catalog, value):
    record = make_record(catalog, value)
    catalog.save_competitions([record])
    assert catalog.get_competitions()[0].level == 'Демо уровень'


@pytest.mark.parametrize('value', ['Произвольный уровень', 'Скрытый уровень'])
def test_unknown_and_hidden_are_rejected_without_catalog_changes(catalog, value):
    before = catalog.list_levels()
    with pytest.raises(InvalidCompetitionLevelError):
        make_record(catalog, value)
    with pytest.raises(InvalidCompetitionLevelError):
        catalog.save_competitions([make_record(catalog), make_record(catalog).model_copy(update={'level': value})])
    assert catalog.count_competitions() == 0
    assert catalog.list_levels() == before


@pytest.mark.parametrize('legacy', ['Исторический УРОВЕНЬ', 'Скрытый уровень', '', '  Старое написание  '])
def test_existing_legacy_is_preserved_exactly_only_in_its_record(catalog, legacy):
    existing = save_history(catalog, legacy)
    modified = build_competition(row(legacy), custom_fields=[], manual_input=True, storage=catalog, existing=existing)
    modified.name = 'Другое название'
    catalog.update_competition(existing.record_id, modified)
    assert catalog.get_competition_by_id(existing.record_id).level == legacy
    if legacy:
        with pytest.raises(InvalidCompetitionLevelError):
            catalog.save_competitions([modified])
    replacement = build_competition(row(), custom_fields=[], manual_input=True, storage=catalog, existing=existing)
    catalog.update_competition(existing.record_id, replacement)
    assert catalog.get_competition_by_id(existing.record_id).level == 'Демо уровень'
    with pytest.raises(InvalidCompetitionLevelError):
        catalog.update_competition(existing.record_id, replacement.model_copy(update={'level': 'Другое старое'}))


def test_optional_empty_and_nan_remain_absence(catalog):
    assert make_record(catalog, '').level == ''
    assert make_record(catalog, float('nan')).level == ''
    assert resolve_competition_level(catalog, None) == ''


def test_import_checks_every_row_before_writes_and_reports_file_row(catalog):
    before = catalog.list_levels()
    with pytest.raises(ValueError, match='строка 3.*Новый уровень'):
        build_import_competitions(pd.DataFrame([row(), row('Новый уровень', 'Вымышленный участник Бета')]), [], catalog)
    assert catalog.count_competitions() == 0
    assert catalog.list_levels() == before
    records = build_import_competitions(pd.DataFrame([row()]), [], catalog)
    catalog.import_competitions(records)
    assert catalog.count_competitions() == 1
    assert catalog.list_levels() == before
    assert 'Демо бег' in catalog.list_catalog('sport')
    with pytest.raises(InvalidCompetitionLevelError):
        catalog.import_competitions([make_record(catalog), make_record(catalog).model_copy(update={'level': 'Чужой'})])
    assert catalog.count_competitions() == 1


def test_event_legacy_edit_and_link_do_not_allow_copy_to_new_record(catalog):
    event_id = catalog.create_calendar_event('Демо событие', '2026-10-01', None, 'Демо уровень', 'Демо бег', [])
    catalog.connection.execute('UPDATE calendar_events SET level = ? WHERE id = ?', ('Старый УРОВЕНЬ', event_id))
    catalog.connection.commit()
    catalog.update_calendar_event(
        event_id, 'Демо событие изменено', '2026-10-01', None, 'Старый УРОВЕНЬ', 'Демо бег', []
    )
    assert catalog.get_calendar_event(event_id)['level'] == 'Старый УРОВЕНЬ'
    with pytest.raises(InvalidCompetitionLevelError):
        catalog.create_calendar_event('Новая копия', '2026-10-02', None, 'Старый УРОВЕНЬ', '', [])
    catalog.save_competitions([make_record(catalog)])
    record = catalog.get_competitions()[0]
    with pytest.raises(InvalidCompetitionLevelError):
        catalog.link_participation_to_event(record.record_id, event_id)
    assert catalog.get_competition_by_id(record.record_id).calendar_event_id is None
    catalog.connection.execute('UPDATE competitions SET level = ? WHERE id = ?', ('Старый УРОВЕНЬ', record.record_id))
    catalog.connection.commit()
    linked, error = catalog.link_participation_to_event(record.record_id, event_id)
    assert error is None and linked['level'] == 'Старый УРОВЕНЬ'
    catalog.update_event_participation_result(record.record_id, event_id, 2, 'Финишировал')
    assert catalog.get_competition_by_id(record.record_id).level == 'Старый УРОВЕНЬ'


def test_used_level_delete_checks_events_and_rename_updates_both_tables(catalog):
    event_id = catalog.create_calendar_event('Демо событие', '2026-10-01', None, 'Демо уровень', '', [])
    level_id = next(item['id'] for item in catalog.list_levels() if item['name'] == 'Демо уровень')
    with pytest.raises(ValueError, match='используется'):
        catalog.hard_delete_level(level_id)
    catalog.save_competitions([make_record(catalog)])
    assert catalog.count_records_using('level', 'Демо уровень') == 2
    assert catalog.rename_catalog_value('level', 'Демо уровень', 'Новый демо уровень', update_records=False) == 1
    assert catalog.get_calendar_event(event_id)['level'] == 'Новый демо уровень'
    assert catalog.get_competitions()[0].level == 'Новый демо уровень'


def test_rename_failure_rolls_back_event_catalog_and_participations(catalog, monkeypatch):
    event_id = catalog.create_calendar_event('Демо событие', '2026-10-01', None, 'Демо уровень', '', [])
    catalog.save_competitions([make_record(catalog)])
    connection = catalog.connection

    class FailOnRecords:
        def execute(self, sql, parameters=()):
            if sql.startswith('UPDATE competitions SET level'):
                raise RuntimeError('Synthetic transaction failure')
            return connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(connection, name)

    monkeypatch.setattr(catalog, 'connection', FailOnRecords())
    with pytest.raises(RuntimeError):
        catalog.rename_catalog_value('level', 'Демо уровень', 'Новый демо уровень')
    assert catalog.get_calendar_event(event_id)['level'] == 'Демо уровень'
    assert catalog.get_competitions()[0].level == 'Демо уровень'
    assert 'Демо уровень' in catalog.get_level_names()


@pytest.fixture
def level_client(client, catalog, monkeypatch):
    from src.settings import settings

    monkeypatch.setattr(settings, 'auth_viewer_username', 'viewer')
    monkeypatch.setattr(settings, 'auth_viewer_password', 'fictional-viewer-password')
    previous = app.ctx.storage
    catalog.get_user = helpers.fake_get_user
    app.ctx.storage = catalog
    yield client, catalog
    app.ctx.storage = previous


@pytest.mark.parametrize('path', ['/competition', '/calendar/new'])
@pytest.mark.parametrize('role', ['admin', 'editor'])
def test_forged_post_rejected_before_side_effects(level_client, role, path):
    client, storage = level_client
    headers = helpers.get_auth_headers(role)
    data = {
        **helpers.csrf_for(headers),
        'student_name': 'Вымышленный участник',
        'name': 'Демо событие',
        'date': '01.10.2026',
        'level': 'Неизвестный уровень',
        'sport': 'Новый демо спорт',
    }
    _, response = client.post(path, headers=headers, data=data)
    assert response.status == 400
    assert 'администратор' in response.text
    assert storage.count_competitions() == 0
    assert storage.list_calendar_events() == []
    assert 'Новый демо спорт' not in storage.list_catalog('sport')


@pytest.mark.parametrize('role', ['admin', 'editor'])
def test_valid_post_and_legacy_registry_edit(level_client, role):
    client, storage = level_client
    headers = helpers.get_auth_headers(role)
    data = {
        **helpers.csrf_for(headers),
        'student_name': 'Вымышленный участник',
        'name': 'Демо событие',
        'date': '01.10.2026',
        'level': 'Демо уровень',
        'position': '1',
    }
    _, response = client.post('/competition', headers=headers, data=data)
    assert response.status == 200
    record = storage.get_competitions()[0]
    storage.connection.execute(
        'UPDATE competitions SET level = ? WHERE id = ?', ('Исторический УРОВЕНЬ', record.record_id)
    )
    storage.connection.commit()
    data.update(level='Исторический УРОВЕНЬ', name='Изменено')
    _, response = client.post(f'/competition/{record.record_id}', headers=headers, data=data)
    assert response.status == 200
    assert storage.get_competition_by_id(record.record_id).level == 'Исторический УРОВЕНЬ'


def test_level_write_keeps_csrf_and_viewer_restrictions(level_client):
    client, storage = level_client
    headers = helpers.get_auth_headers()
    _, response = client.post(
        '/calendar/new', headers=headers, data={'name': 'Демо', 'date': '01.10.2026', 'level': 'Демо уровень'}
    )
    assert response.status == 403
    headers = helpers.get_auth_headers('viewer')
    _, response = client.post(
        '/competition', headers=headers, data={**helpers.csrf_for(headers), 'level': 'Демо уровень'}
    )
    assert response.status == 403
    assert storage.count_competitions() == 0


def test_existing_blank_survives_later_required_setting(catalog):
    existing = save_history(catalog, '')
    catalog.update_field_settings({'level': ('text', True)})
    with pytest.raises(ValueError, match='обязательно'):
        make_record(catalog, '')
    preserved = build_competition(row(''), custom_fields=[], manual_input=True, storage=catalog, existing=existing)
    catalog.update_competition(existing.record_id, preserved)
    assert catalog.get_competition_by_id(existing.record_id).level == ''


def test_startup_never_creates_arbitrary_levels_from_history(catalog):
    from src.main import seed_levels

    existing = save_history(catalog, 'Старый уровень вне справочника')
    catalog._create_schema()
    seed_levels(catalog)
    assert 'Старый уровень вне справочника' not in catalog.get_level_names(include_inactive=True)
    assert catalog.get_competition_by_id(existing.record_id).level == 'Старый уровень вне справочника'


def test_nonlevel_event_edit_preserves_each_participants_legacy_snapshot(catalog):
    event_id = catalog.create_calendar_event('Демо событие', '2026-10-01', None, 'Демо уровень', '', [])
    existing = save_history(catalog, 'Исторический уровень участия Б')
    catalog.connection.execute(
        'UPDATE calendar_events SET level = ? WHERE id = ?', ('Исторический уровень события А', event_id)
    )
    catalog.connection.execute(
        'UPDATE competitions SET calendar_event_id = ? WHERE id = ?', (event_id, existing.record_id)
    )
    catalog.connection.commit()
    catalog.update_calendar_event(
        event_id, 'Новое название', '2026-10-01', None, 'Исторический уровень события А', '', []
    )
    assert catalog.get_calendar_event(event_id)['level'] == 'Исторический уровень события А'
    assert catalog.get_competition_by_id(existing.record_id).level == 'Исторический уровень участия Б'
    catalog.update_calendar_event(event_id, 'Новое название', '2026-10-01', None, 'Демо уровень', '', [])
    assert catalog.get_calendar_event(event_id)['level'] == 'Демо уровень'
    assert catalog.get_competition_by_id(existing.record_id).level == 'Демо уровень'


def test_result_only_import_preserves_matched_own_legacy_not_event_level(catalog):
    from src.routes.calendar import event_import_pending_states
    from src.routes.calendar import event_import_update_candidate_classification

    event_id = catalog.create_calendar_event('Демо событие', '2026-10-01', None, 'Демо уровень', '', [])
    student_id = catalog.create_student('Вымышленный участник Альфа', 'М', 'Демо институт', 'Демо-24А', '')
    record = make_record(catalog)
    record.student_ref_id = student_id
    record.calendar_event_id = event_id
    record.discipline = 'Демо дистанция'
    catalog.save_competitions([record])
    record = catalog.get_competitions()[0]
    catalog.connection.execute(
        'UPDATE competitions SET level = ? WHERE id = ?', ('Исторический уровень участия Б', record.record_id)
    )
    catalog.connection.execute(
        'UPDATE calendar_events SET level = ? WHERE id = ?', ('Исторический уровень события А', event_id)
    )
    catalog.connection.commit()
    import_row = dict(
        row_number=2,
        full_name='Вымышленный участник Альфа',
        sex='М',
        institute='Демо институт',
        group='Демо-24А',
        course='',
        position='3',
        discipline='Демо дистанция',
        result='Демо результат',
        custom={},
        status='pending',
        error=None,
        student_id=student_id,
        student_forced_none=False,
        added_student_id=None,
    )
    session = {'rows': [import_row]}
    event = catalog.get_calendar_event(event_id)
    state = event_import_pending_states(catalog, event, session, [])[2]
    assert state['validation_error'] is None
    assert state['import_status'] == 'update-candidate'
    assert state['competition'].level == 'Исторический уровень участия Б'
    classification, update, error = event_import_update_candidate_classification(catalog, event, session, import_row)
    assert error is None and classification['status'] == 'update-candidate'
    catalog.update_event_participation_result(record.record_id, event_id, update.position, update.result)
    assert catalog.get_competition_by_id(record.record_id).level == 'Исторический уровень участия Б'
    import_row.update(full_name='Вымышленный новый участник Бета', student_id=None, student_forced_none=True)
    assert event_import_pending_states(catalog, event, session, [])[2]['validation_error']
