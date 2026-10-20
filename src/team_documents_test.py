"""TEAM-02 uses synthetic identities/files only; rights are never copied to recipients."""
from copy import deepcopy
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest

from src import teams_test
from src.auth import create_auth_cookie_value
from src.files import calendar_regulation_dir
from src.files import event_document_source_path
from src.main_test import audit_details
from src.main_test import csrf_for
from src.main_test import get_auth_headers
from src.settings import settings
from src.storage.sqlite import SQLiteAdapter
from src.teams_test import seed_team

team_db = teams_test.team_db
team_http = teams_test.team_http


BODY = b'%PDF-1.4\nSynthetic TEAM document\n'


def athlete_headers(name='synthetic-athlete'):
    cookie = create_auth_cookie_value(username=name, role='athlete')
    return {'cookie': f'{settings.auth_cookie_name}={cookie}'}


@pytest.fixture
def document_case(team_db, team_http, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'data_folder', str(tmp_path / 'synthetic-files'))
    event, team, student, ids = seed_team(team_db)
    team_db.change_event_team_members(event, team, ids[:2], 'add')
    user = team_db.get_user('synthetic-athlete')['id']
    assert team_db.link_user(user, student) == (1, None)
    return event, team, student, ids, user


def upload(client, event, team, **extra):
    headers = get_auth_headers('editor')
    return client.post(
        f'/calendar/{event}/documents',
        headers=headers,
        data={
            **csrf_for(headers),
            'title': 'Синтетический командный файл',
            'access_mode': 'team_members',
            'team_id': str(team),
            **extra,
        },
        files={'file': ('synthetic.pdf', BODY, 'application/pdf')},
        allow_redirects=False,
    )[1]


def stored_document(storage, event, team, **extra):
    values = dict(
        title='Синтетический документ',
        filename='synthetic.pdf',
        stored_name='0123456789abcdef0123456789abcdef.pdf',
        content_type='application/pdf',
        size=len(BODY),
        access_mode='team_members',
        team_id=team,
        student_ids=[],
    )
    values.update(extra)
    document, error = storage.create_calendar_event_document(event, **values)
    assert error is None
    path = event_document_source_path(event, values['stored_name'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(BODY)
    return document, path


def assert_access(storage, event, document, user, student, allowed):
    listing = storage.list_documents_for_student(student, user_id=user)
    assert any(row['id'] == document for row in listing) is allowed
    assert bool(storage.get_event_document_for_user(event, document, user)) is allowed


def test_one_uploaded_file_twenty_recipients_and_live_membership(document_case, team_db, team_http):
    event, team, student, ids, user = document_case
    recipients = [(user, student, 'synthetic-athlete')]
    prototype = team_db.get_competition_by_id(ids[0])
    for index in range(19):
        ref = team_db.create_student(f'Синтетический Участник Образец {index}', '', '', '', '')
        name = f'synthetic-team-user-{index}'
        team_db.create_user(name, 'synthetic-unused-hash', 'athlete')
        account = team_db.get_user(name)['id']
        team_db.link_user(account, ref)
        participation = deepcopy(prototype)
        participation.record_id = None
        participation.student_id = f'synthetic-team-identity-{index}'
        participation.student_ref_id = ref
        participation.student_name = f'Синтетический Участник Образец {index}'
        team_db.save_competitions([participation])
        record_id = team_db.connection.execute('SELECT MAX(id) FROM competitions').fetchone()[0]
        team_db.change_event_team_members(event, team, [record_id], 'add')
        recipients.append((account, ref, name))
    assert upload(team_http, event, team).status == 302
    documents = team_db.list_calendar_event_documents(event)
    assert len(documents) == 1
    document = documents[0]
    assert team_db.calendar_event_document_students(document['id']) == []
    assert audit_details(team_db, 'calendar_event_document_uploaded')[-1]['team_id'] == team
    assert len(list(calendar_regulation_dir(event).iterdir())) == 1
    for account, ref, name in recipients:
        assert_access(team_db, event, document['id'], account, ref, True)
        _, downloaded = team_http.get(
            f'/calendar/{event}/documents/{document["id"]}/download', headers=athlete_headers(name)
        )
        assert downloaded.status == 200
        assert downloaded.body == BODY
    _, cabinet = team_http.get('/', headers=athlete_headers())
    assert cabinet.status == 200
    assert cabinet.text.count(f'/documents/{document["id"]}/download') == 1
    assert 'Синтетическая команда A' in cabinet.text
    assert 'no-store' in cabinet.headers['cache-control']
    path = event_document_source_path(event, document['stored_name'])
    team_db.change_event_team_members(event, team, [ids[0]], 'remove')
    assert_access(team_db, event, document['id'], user, student, True)
    team_db.change_event_team_members(event, team, [ids[1]], 'remove')
    assert_access(team_db, event, document['id'], user, student, False)
    team_db.change_event_team_members(event, team, [ids[1]], 'add')
    assert_access(team_db, event, document['id'], user, student, True)
    assert path.read_bytes() == BODY
    assert team_db.get_calendar_event_document(event, document['id'])['stored_name'] == document['stored_name']


def test_relinks_deletions_and_other_team_are_live(document_case, team_db):
    event, team, student, ids, user = document_case
    document, _ = stored_document(team_db, event, team)
    other = team_db.create_event_team(event, 'Синтетический другой состав')
    team_db.change_event_team_members(event, other, [ids[0]], 'add')
    second = team_db.create_student('Синтетическая Другая Карточка', '', '', '', '')
    team_db.relink_competition(ids[0], second)
    assert_access(team_db, event, document, user, student, True)
    team_db.delete_competition(ids[1])
    assert_access(team_db, event, document, user, student, False)
    team_db.relink_user(user, second)
    assert_access(team_db, event, document, user, student, True)  # stale argument cannot override current account
    team_db.change_event_team_members(event, team, [ids[0]], 'remove')
    assert_access(team_db, event, document, user, second, False)  # other team cannot grant
    team_db.change_event_team_members(event, team, [ids[0]], 'add')
    team_db.unlink_user(user)
    assert_access(team_db, event, document, user, second, False)


@pytest.mark.parametrize('damage', ['participation', 'student', 'membership', 'team_event', 'document_event', 'mode'])
def test_broken_chain_fails_closed_for_list_and_download(document_case, team_db, damage):
    event, team, student, ids, user = document_case
    document, _ = stored_document(team_db, event, team)
    other = team_db.create_calendar_event('Иное синтетическое событие', '2026-10-22', None, '', '', [])
    sql, args = {
        'participation': ('DELETE FROM competitions WHERE student_ref_id=?', (student,)),
        'student': ('DELETE FROM students WHERE id=?', (student,)),
        'membership': ('DELETE FROM calendar_event_team_members WHERE team_id=?', (team,)),
        'team_event': ('UPDATE calendar_event_teams SET calendar_event_id=? WHERE id=?', (other, team)),
        'document_event': ('UPDATE calendar_event_documents SET calendar_event_id=? WHERE id=?', (other, document)),
        'mode': ('UPDATE calendar_event_documents SET access_mode=? WHERE id=?', ('future_scope', document)),
    }[damage]
    team_db.connection.execute(sql, args)
    team_db.connection.commit()
    assert_access(team_db, event, document, user, student, False)


def test_team_access_does_not_depend_on_card_status_or_review(document_case, team_db):
    event, team, student, ids, user = document_case
    document, _ = stored_document(team_db, event, team)
    team_db.connection.execute('UPDATE students SET active=0 WHERE id=?', (student,))
    team_db.connection.execute("UPDATE competitions SET review_status='pending' WHERE student_ref_id=?", (student,))
    team_db.connection.commit()
    assert_access(team_db, event, document, user, student, True)
    team_db.change_event_team_members(event, team, ids[:2], 'remove')
    team_db.change_event_team_members(event, team, [ids[2]], 'add')
    assert_access(team_db, event, document, user, student, False)  # identical name, cardless row


@pytest.mark.parametrize('mode', ['team_members', 'selected_students', 'all_participants'])
def test_missing_scope_fields_preserve_metadata_and_file_edit(document_case, team_db, team_http, mode):
    event, team, student, _, _ = document_case
    document, old = stored_document(
        team_db, event, team if mode == 'team_members' else None, access_mode=mode, student_ids=[student]
    )
    headers = get_auth_headers('editor')
    url = f'/calendar/{event}/documents/{document}/edit'
    for replacement in (False, True):
        kwargs = (
            {'files': {'file': ('replacement.pdf', BODY + b'replacement', 'application/pdf')}} if replacement else {}
        )
        _, response = team_http.post(
            url,
            headers=headers,
            data={**csrf_for(headers), 'title': 'Синтетическая правка'},
            allow_redirects=False,
            **kwargs,
        )
        assert response.status == 302
        assert 'admin_error' not in response.headers['location']
        current = team_db.get_calendar_event_document(event, document)
        assert current['access_mode'] == mode
        assert current['team_id'] == (team if mode == 'team_members' else None)
        assert team_db.calendar_event_document_students(document) == ([student] if mode == 'selected_students' else [])
    assert not old.exists()


def test_foreign_team_form_error_and_storage_fault_keep_original(document_case, team_db, team_http, monkeypatch):
    event, team, _, _, _ = document_case
    other = team_db.create_calendar_event('Синтетическое чужое событие', '2026-10-23', None, '', '', [])
    foreign = team_db.create_event_team(other, 'Чужая синтетическая команда')
    response = upload(team_http, event, foreign)
    query = parse_qs(urlsplit(response.headers['location']).query)
    assert query['document_form_team'] == [str(foreign)]
    assert team_db.list_calendar_event_documents(event) == []
    assert list(calendar_regulation_dir(event).iterdir()) == []
    document, path = stored_document(team_db, event, team)
    before = team_db.get_calendar_event_document(event, document)

    def fail(*args):
        raise RuntimeError('synthetic write failure')

    monkeypatch.setattr(team_db, '_insert_document_students', fail)
    response = upload(team_http, event, team)
    assert 'admin_error' in response.headers['location']
    headers = get_auth_headers('editor')
    _, response = team_http.post(
        f'/calendar/{event}/documents/{document}/edit',
        headers=headers,
        data={**csrf_for(headers), 'title': 'Ошибка синтетического хранилища'},
        files={'file': ('replacement.pdf', BODY + b'replacement', 'application/pdf')},
        allow_redirects=False,
    )
    assert 'admin_error' in response.headers['location']
    assert team_db.get_calendar_event_document(event, document) == before
    assert path.read_bytes() == BODY
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize('change', ['membership', 'account', 'password', 'role', 'file', 'document'])
def test_recheck_after_await_blocks_revoked_or_replaced_bytes(document_case, team_db, team_http, monkeypatch, change):
    from src.routes import calendar

    event, team, _, _, user = document_case
    document, path = stored_document(team_db, event, team)
    original = calendar.read_event_document_version
    database = team_db.connection.execute('PRAGMA database_list').fetchone()['file']

    def read_then_revoke(source):
        result = original(source)
        other = SQLiteAdapter(database)
        try:
            if change == 'file':
                path.write_bytes(BODY + b'replaced')
            else:
                sql, args = {
                    'membership': ('DELETE FROM calendar_event_team_members WHERE team_id=?', (team,)),
                    'account': ('UPDATE users SET student_ref_id=NULL WHERE id=?', (user,)),
                    'password': ('UPDATE users SET pwd_ver=pwd_ver+1 WHERE id=?', (user,)),
                    'role': ("UPDATE users SET role='unknown' WHERE id=?", (user,)),
                    'document': (
                        'UPDATE calendar_event_documents SET stored_name=? WHERE id=?',
                        ('f' * 32 + '.pdf', document),
                    ),
                }[change]
                other.connection.execute(sql, args)
                other.connection.commit()
        finally:
            other.connection.close()
        return result

    monkeypatch.setattr(calendar, 'read_event_document_version', read_then_revoke)
    _, response = team_http.get(f'/calendar/{event}/documents/{document}/download', headers=athlete_headers())
    assert response.status in (403, 409)
    assert BODY not in response.body
    assert 'no-store' in response.headers['cache-control']
    if change == 'membership':
        _, response = team_http.get(f'/calendar/{event}/documents/{document}/download', headers=athlete_headers())
        assert response.status == 403


def test_staff_download_rbac_csrf_and_symlink(document_case, team_db, team_http, tmp_path):
    event, team, _, _, _ = document_case
    document, path = stored_document(team_db, event, team)
    url = f'/calendar/{event}/documents/{document}'
    for headers in (
        athlete_headers(),
        get_auth_headers('admin'),
        get_auth_headers('editor'),
        get_auth_headers('viewer'),
    ):
        _, response = team_http.get(url + '/download', headers=headers)
        assert response.status == 200
        assert response.body == BODY
        assert 'no-store' in response.headers['cache-control']
    for role in ('viewer', 'athlete'):
        headers = athlete_headers() if role == 'athlete' else get_auth_headers(role)
        _, response = team_http.post(url + '/delete', headers=headers, data=csrf_for(headers))
        assert response.status == 403
    _, response = team_http.post(url + '/delete', headers=get_auth_headers('editor'))
    assert response.status == 403
    target = tmp_path / 'synthetic-private.pdf'
    target.write_bytes(b'private synthetic target')
    path.unlink()
    path.symlink_to(target)
    _, response = team_http.get(url + '/download', headers=athlete_headers())
    assert response.status == 404
    assert b'private synthetic target' not in response.body
    assert 'no-store' in response.headers['cache-control']


def test_document_form_errors_keep_input_and_nojs_team_selector(document_case, team_http):
    import re

    event, team, student, _, _ = document_case
    headers = get_auth_headers('editor')
    _, response = team_http.post(
        f'/calendar/{event}/documents', headers=headers,
        data={**csrf_for(headers), 'title': 'Синтетическое введённое имя', 'access_mode': 'team_members',
              'team_id': str(team), 'student_id': [str(student)]}, allow_redirects=False,
    )
    assert response.status == 302
    _, response = team_http.get(response.headers['location'], headers=headers)
    assert response.status == 200
    form = response.text.split('data-documents-form>', 1)[1].split('</form>', 1)[0]
    assert 'value="Синтетическое введённое имя"' in form
    assert re.search(r'value="team_members"\s+checked', form)
    assert re.search(r'<option value="' + str(team) + r'"\s+selected', form)
    assert re.search(r'value="' + str(student) + r'"\s+checked', form)
    assert '<div class="col-12" data-documents-team>' in form
    assert 'выберите его заново' in form
