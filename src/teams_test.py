"""TEAM-01 synthetic storage, lifecycle and HTTP acceptance tests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest

from src.auth import create_auth_cookie_value
from src.main_test import csrf_for
from src.main_test import get_auth_headers
from src.models.competition import Competition
from src.settings import settings
from src.storage.sqlite import SQLiteAdapter
from src.storage.teams import team_actor
from src.storage.teams import TeamError


@pytest.fixture
def team_db(tmp_path):
    storage = SQLiteAdapter(str(tmp_path / 'teams.sqlite3'))
    yield storage
    storage.connection.close()


def seed_team(storage):
    event = storage.create_calendar_event('Синтетическое соревнование TEAM', '2026-10-20', None, '', '', [])
    team = storage.create_event_team(event, 'Синтетическая команда A')
    student = storage.create_student('Синтетиков Командный Образец', '', '', '', '')
    records = [
        Competition(
            student_id=f'synthetic-{index}',
            student_name='Синтетиков Командный Образец',
            student_sex='',
            institute='',
            group='',
            sport='',
            date=datetime(2026, 10, 20),
            level='',
            name='Синтетическое соревнование TEAM',
            position=0,
            discipline=discipline,
            student_ref_id=student if index < 2 else None,
            calendar_event_id=event,
        )
        for index, discipline in enumerate(('100 м', '200 м', 'Эстафета'))
    ]
    storage.save_competitions(records)
    ids = [int(record.record_id) for record in storage.get_competitions()]
    return event, team, student, ids


def test_membership_is_participation_scoped_multi_team_idempotent(team_db):
    event, team, _, ids = seed_team(team_db)
    other = team_db.create_event_team(event, 'Синтетическая команда B')
    assert team_db.change_event_team_members(event, team, [ids[0], ids[0]], 'add') == 1
    assert team_db.change_event_team_members(event, team, [ids[0]], 'add') == 0
    team_db.change_event_team_members(event, other, [ids[0], ids[2]], 'add')
    assert team_db.event_team_member_ids(event, team) == {ids[0]}
    assert team_db.event_team_member_ids(event, other) == {ids[0], ids[2]}
    assert len([h for h in team_db.event_team_history(event, team) if h['action'] == 'added']) == 1
    team_db.change_event_team_members(event, team, [ids[0]], 'remove')
    assert team_db.get_competition_by_id(ids[0]) is not None
    assert team_db.event_team_member_ids(event, other) == {ids[0], ids[2]}


@pytest.mark.parametrize('action', ['add', 'remove'])
def test_batch_foreign_or_missing_participation_rolls_back(team_db, action):
    event, team, _, ids = seed_team(team_db)
    team_db.change_event_team_members(event, team, [ids[0]], 'add')
    before = team_db.event_team_history(event)
    for invalid in (99999,):
        with pytest.raises(TeamError):
            team_db.change_event_team_members(event, team, [ids[0], invalid], action)
    other_event = team_db.create_calendar_event('Другой синтетический старт', '2026-10-21', None, '', '', [])
    with pytest.raises(TeamError):
        team_db.change_event_team_members(other_event, team, [ids[0]], action)
    team_db.connection.execute('UPDATE competitions SET calendar_event_id=? WHERE id=?', (other_event, ids[1]))
    team_db.connection.commit()
    with pytest.raises(TeamError):
        team_db.change_event_team_members(event, team, [ids[0], ids[1]], action)
    assert team_db.event_team_member_ids(event, team) == {ids[0]}
    assert team_db.event_team_history(event) == before


def test_effective_discipline_and_history_snapshots(team_db):
    event, team, student, ids = seed_team(team_db)
    other = team_db.create_event_team(event, 'Вторая синтетическая команда')
    team_db.create_custom_field('synthetic_disc', 'Дисциплина', 'text', False, True, True, True, 0)
    record = team_db.get_competition_by_id(ids[0])
    record.discipline = None
    record.extra_data = {'synthetic_disc': '100 м'}
    team_db.update_competition(ids[0], record)
    for selected in (team, other):
        team_db.change_event_team_members(event, selected, [ids[0]], 'add')
    record.discipline = '  100   М '
    record.position = 1
    record.result = 'synthetic result'
    team_db.update_competition(ids[0], record)
    assert team_db.event_team_member_ids(event, team) == {ids[0]}
    token = team_actor.set({'id': 99, 'username': 'synthetic-editor'})
    try:
        record.discipline = '400 м'
        team_db.update_competition(ids[0], record)
    finally:
        team_actor.reset(token)
    assert team_db.event_team_member_ids(event, team) == set()
    assert team_db.event_team_member_ids(event, other) == set()
    removed = [h for h in team_db.event_team_history(event) if h['action'] == 'removed']
    assert len(removed) == 2
    assert all(h['reason'] == 'discipline_changed' and h['actor_id'] == 99 for h in removed)
    assert all(h['discipline'].strip() == '100   М' and h['student_ref_id'] == student for h in removed)


@pytest.mark.parametrize(
    'method',
    [
        'delete_competition',
        'delete_competition_with_attachments',
        'clean_db',
        'delete_competitions_before',
        'delete_all_competitions',
    ],
)
def test_all_participation_deletion_paths_keep_history(team_db, method):
    event, team, _, ids = seed_team(team_db)
    team_db.change_event_team_members(event, team, ids, 'add')
    args = [ids[0]] if method in ('delete_competition', 'delete_competition_with_attachments') else []
    if method == 'delete_competitions_before':
        args = [datetime(2027, 1, 1)]
    getattr(team_db, method)(*args)
    remaining = team_db.event_team_member_ids(event, team)
    assert remaining == ({ids[1], ids[2]} if args == [ids[0]] else set())
    removed = [h for h in team_db.event_team_history(event) if h['action'] == 'removed']
    assert len(removed) == (1 if remaining else 3)
    assert all(h['student_name'] and h['reason'] == 'participation_deleted' for h in removed)


def test_relink_history_and_deleted_team_retained(team_db):
    event, team, _, ids = seed_team(team_db)
    team_db.change_event_team_members(event, team, [ids[0]], 'add')
    second = team_db.create_student('Синтетиков Другой Образец', '', '', '', '')
    team_db.relink_competition(ids[0], second)
    assert team_db.event_team_member_ids(event, team) == {ids[0]}
    assert team_db.event_team_history(event)[0]['action'] == 'identity_changed'
    team_db.delete_competition(ids[0])
    team_db.rename_event_team(event, team, 'Новое синтетическое имя')
    team_db.delete_event_team(event, team)
    assert team_db.get_event_team(event, team) is None
    assert team_db.event_team_history(event)[0]['action'] == 'deleted'
    assert any(h['student_name'] for h in team_db.event_team_history(event))


def test_delete_guards_and_result_team_scope(team_db):
    event, team, _, ids = seed_team(team_db)
    other = team_db.create_event_team(event, 'Другой состав')
    result, error = team_db.add_calendar_event_team_result(event, 'Эстафета', 1, team_id=team)
    assert error is None
    assert team_db.add_calendar_event_team_result(event, 'Эстафета', 2, team_id=other)[1] is None
    assert team_db.add_calendar_event_team_result(event, 'Эстафета', 2, team_id=team)[1] == 'duplicate_label'
    assert team_db.update_calendar_event_team_result(event, result, 'Эстафета', 3, team_id=team) == (True, None)
    with pytest.raises(TeamError):
        team_db.delete_event_team(event, team)
    team_db.delete_calendar_event_team_result(event, result)
    team_db.change_event_team_members(event, team, [ids[0]], 'add')
    with pytest.raises(TeamError):
        team_db.delete_event_team(event, team)


def test_history_failure_rolls_back_participation_and_membership(team_db, monkeypatch):
    event, team, _, ids = seed_team(team_db)
    team_db.change_event_team_members(event, team, [ids[0]], 'add')

    def fail(*args, **kwargs):
        raise RuntimeError('synthetic history write failure')

    monkeypatch.setattr(team_db, '_team_history', fail)
    with pytest.raises(RuntimeError):
        team_db.delete_competition(ids[0])
    assert team_db.get_competition_by_id(ids[0]) is not None
    assert team_db.event_team_member_ids(event, team) == {ids[0]}


def test_two_connections_add_deltas_and_delete_are_serialized(tmp_path):
    path = str(tmp_path / 'concurrency.sqlite3')
    first, second = SQLiteAdapter(path), SQLiteAdapter(path)
    try:
        event, team, _, ids = seed_team(first)
        barrier = Barrier(2)

        def add(storage, record):
            barrier.wait()
            return storage.change_event_team_members(event, team, [record], 'add')

        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(add, first, ids[0])
            b = pool.submit(add, second, ids[1])
            assert a.result() == b.result() == 1
        assert first.event_team_member_ids(event, team) == set(ids[:2])
        barrier = Barrier(2)

        def erase():
            barrier.wait()
            first.delete_competition(ids[0])

        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(erase)
            b = pool.submit(add, second, ids[0])
            a.result()
            try:
                b.result()
            except TeamError:
                pass
        assert first.event_team_member_ids(event, team) == {ids[1]}
    finally:
        first.connection.close()
        second.connection.close()


def reserve_document(storage, event, team, student):
    document, error = storage.create_calendar_event_document(
        event,
        title='Синтетический диплом',
        filename='test.pdf',
        stored_name='0123456789abcdef0123456789abcdef.pdf',
        content_type='application/pdf',
        size=20,
        access_mode='selected_students',
        student_ids=[student],
    )
    assert error is None
    storage.connection.execute(
        "UPDATE calendar_event_documents SET team_id=?,access_mode='team_members' WHERE id=?", (team, document)
    )
    storage.connection.commit()
    return document


def test_a_is_fail_closed_on_future_document_and_restart(tmp_path):
    path = str(tmp_path / 'rollback.sqlite3')
    storage = SQLiteAdapter(path)
    event, team, student, ids = seed_team(storage)
    storage.change_event_team_members(event, team, ids[:2], 'add')
    document = reserve_document(storage, event, team, student)
    before = storage.get_calendar_event_document(event, document)
    history = storage.event_team_history(event)
    storage.connection.close()
    for _ in range(2):
        storage = SQLiteAdapter(path)
        assert storage.list_documents_for_student(student) == []
        assert storage.update_calendar_event_document(
            event, document, title='Wrong', access_mode='all_participants', student_ids=[]
        ) == (False, 'unsupported_scope')
        assert not storage.delete_calendar_event_document(event, document)
        with pytest.raises(ValueError):
            storage.delete_calendar_event(event)
        with pytest.raises(TeamError):
            storage.delete_event_team(event, team)
        assert storage.get_calendar_event_document(event, document) == before
        assert storage.event_team_history(event) == history
        storage.connection.close()


@pytest.fixture
def team_http(http_client, team_db):
    http_client.app.ctx.storage = team_db
    for name, role in (
        (settings.auth_admin_username, 'admin'),
        (settings.auth_editor_username, 'editor'),
        (settings.auth_viewer_username or 'viewer', 'viewer'),
        ('synthetic-athlete', 'athlete'),
    ):
        team_db.create_user(name, 'synthetic-unused-hash', role)
    return http_client


def test_http_roster_rbac_csrf_and_form_errors(team_http, team_db):
    event, team, _, ids = seed_team(team_db)
    url = f'/calendar/{event}/teams/{team}'
    headers = get_auth_headers('editor')
    _, response = team_http.get(url, headers=headers)
    assert response.status == 200
    assert 'Нет доступа через кабинет' in response.text
    _, response = team_http.post(url + '/add', headers=headers, data={'competition_id': [str(ids[0])]})
    assert response.status == 403
    _, response = team_http.post(
        url + '/add', headers=headers, data={**csrf_for(headers), 'competition_id': [str(ids[0]), '99999']}
    )
    assert response.status == 400
    assert 'checked' in response.text
    assert team_db.event_team_member_ids(event, team) == set()
    _, response = team_http.post(
        url + '/add',
        headers=headers,
        data={**csrf_for(headers), 'competition_id': [str(ids[0])]},
        allow_redirects=False,
    )
    assert response.status == 302
    _, response = team_http.get(response.headers['location'], headers=headers)
    assert response.status == 200
    assert team_db.event_team_member_ids(event, team) == {ids[0]}
    assert team_db.event_team_history(event)[0]['actor_name'] == settings.auth_editor_username
    viewer = get_auth_headers('viewer')
    _, response = team_http.post(
        url + '/remove', headers=viewer, data={**csrf_for(viewer), 'competition_id': [str(ids[0])]}
    )
    assert response.status == 403
    cookie = create_auth_cookie_value(username='synthetic-athlete', role='athlete')
    athlete = {'cookie': f'{settings.auth_cookie_name}={cookie}'}
    _, response = team_http.get(url, headers=athlete)
    assert response.status == 403


def test_http_future_document_denied_and_staff_kept(team_http, team_db, tmp_path, monkeypatch):
    from src.files import calendar_regulation_dir

    monkeypatch.setattr(settings, 'data_folder', str(tmp_path / 'files'))
    event, team, student, _ = seed_team(team_db)
    document = reserve_document(team_db, event, team, student)
    team_db.link_user(team_db.get_user('synthetic-athlete')['id'], student)
    folder = calendar_regulation_dir(event)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / '0123456789abcdef0123456789abcdef.pdf').write_bytes(b'%PDF-1.4 synthetic team document')
    url = f'/calendar/{event}/documents/{document}/download'
    cookie = create_auth_cookie_value(username='synthetic-athlete', role='athlete')
    _, response = team_http.get(url, headers={'cookie': f'{settings.auth_cookie_name}={cookie}'})
    assert response.status == 403
    for role in ('admin', 'editor', 'viewer'):
        _, response = team_http.get(url, headers=get_auth_headers(role))
        assert response.status == 200


def test_legacy_field_disable_removes_memberships_atomically(team_db):
    event, team, _, ids = seed_team(team_db)
    team_db.create_custom_field('legacy_team_disc', 'Дисциплина', 'text', False, True, True, True, 0)
    record = team_db.get_competition_by_id(ids[0])
    record.discipline = None
    record.extra_data = {'legacy_team_disc': '100 м'}
    team_db.update_competition(ids[0], record)
    team_db.change_event_team_members(event, team, [ids[0]], 'add')
    field = team_db.get_custom_fields()[0]
    team_db.disable_custom_field(field.field_id)
    assert team_db.event_team_member_ids(event, team) == set()
    assert team_db.event_team_history(event)[0]['reason'] == 'discipline_changed'


def test_additive_team_migration_rollback_and_legacy_preservation(team_db):
    event, team, student, ids = seed_team(team_db)
    team_db.connection.execute('DROP TABLE calendar_event_team_members')
    team_db.connection.execute('DROP TABLE calendar_event_team_history')
    team_db.connection.execute('DROP TABLE calendar_event_teams')
    team_db.connection.execute('DROP INDEX idx_calendar_event_documents_team')
    team_db.connection.execute('DROP INDEX idx_calendar_event_team_results_team')
    team_db.connection.execute('ALTER TABLE calendar_event_documents DROP COLUMN team_id')
    team_db.connection.execute('ALTER TABLE calendar_event_team_results DROP COLUMN team_id')
    team_db.connection.commit()
    before = [tuple(row) for row in team_db.connection.execute('SELECT * FROM competitions')]
    connection = team_db.connection

    class FailingMigration:
        def execute(self, sql, parameters=()):
            if sql.startswith('ALTER TABLE calendar_event_team_results ADD'):
                raise RuntimeError('synthetic migration interruption')
            return connection.execute(sql, parameters)

    team_db.connection = FailingMigration()
    try:
        with pytest.raises(RuntimeError):
            team_db._migrate_teams()
    finally:
        team_db.connection = connection
    assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='calendar_event_teams'").fetchone()
    assert 'team_id' not in {row['name'] for row in connection.execute('PRAGMA table_info(calendar_event_documents)')}
    team_db._migrate_teams()
    team_db._migrate_teams()
    assert team_db.list_event_teams(event) == []
    assert [tuple(row) for row in connection.execute('SELECT * FROM competitions')] == before


def test_http_discipline_change_reports_committed_removal(team_http, team_db):
    event, team, _, ids = seed_team(team_db)
    team_db.change_event_team_members(event, team, [ids[0]], 'add')
    headers = get_auth_headers('editor')
    # Required registry form fields, all synthetic; event-owned values are kept.
    values = {
        'student_name': 'Синтетиков Командный Образец',
        'student_sex': 'М',
        'institute': 'Синтетический институт',
        'group': 'Синт-24А',
        'discipline': '400 м',
        'position': '0',
    }
    _, posted = team_http.post(
        f'/competition/{ids[0]}', headers=headers, data={**csrf_for(headers), **values}, allow_redirects=False
    )
    assert posted.status == 302
    _, response = team_http.get(posted.headers['location'], headers=headers)
    assert response.status == 200
    assert 'data-team-removal-notice' in response.text, (
        str(response.url),
        [(r.status_code, r.headers.get('location')) for r in response.history],
        team_db.event_team_history(event),
    )
    assert 'удалены членства в командах: 1' in response.text
    assert team_db.event_team_member_ids(event, team) == set()
    # Following requests don't inherit the previous request's count or actor.
    _, response = team_http.get('/', headers=headers)
    assert 'data-team-removal-notice' not in response.text


def test_result_error_preserves_team_label_place(team_http, team_db):
    event, team, _, _ = seed_team(team_db)
    headers = get_auth_headers('editor')
    _, response = team_http.post(
        f'/calendar/{event}/team-results',
        headers=headers,
        data={**csrf_for(headers), 'label': 'Синтетическая категория', 'team_id': str(team), 'place': '0'},
        allow_redirects=False,
    )
    assert response.status == 302
    _, response = team_http.get(response.headers['location'], headers=headers)
    assert response.status == 200
    assert 'value="Синтетическая категория"' in response.text, (
        str(response.url),
        [(r.status_code, r.headers.get('location')) for r in response.history],
    )
    assert f'value="{team}" selected' in response.text
    assert 'name="place" value="0"' in response.text
    assert not team_db.get_calendar_event(event)['team_results']
