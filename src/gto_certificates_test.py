"""Synthetic certificate authority, uploads and filesystem failure regressions."""
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote_plus

import pytest

from src.auth import create_auth_cookie_value
from src.gto_files import read_certificate
from src.gto_files import store_certificate
from src.main_test import csrf_for
from src.main_test import get_auth_headers
from src.settings import settings
from src.storage.sqlite import SQLiteAdapter

BODY = b'%PDF-1.4\nSynthetic GTO award certificate\n'


@pytest.fixture
def certificate_case(http_client, tmp_path, monkeypatch):
    storage = SQLiteAdapter(str(tmp_path / 'synthetic-certificates.sqlite3'))
    monkeypatch.setattr(settings, 'data_folder', str(tmp_path / 'synthetic-files'))
    http_client.app.ctx.storage = storage
    for name, role in (
        (settings.auth_admin_username, 'admin'),
        (settings.auth_editor_username, 'editor'),
        (settings.auth_viewer_username or 'viewer', 'viewer'),
        ('synthetic-athlete', 'athlete'),
    ):
        storage.create_user(name, 'synthetic-unused-hash', role)
    first = storage.create_student('Синтетическая Карточка Альфа', '', 'Демо институт', 'ДЕМО-26', '')
    second = storage.create_student('Синтетическая Карточка Бета', '', '', '', '')
    award, _ = storage.create_student_gto_record(first, year=2026, stage=8, status='gold')
    other, _ = storage.create_student_gto_record(second, year=2026, stage=8, status='bronze')
    account = storage.get_user('synthetic-athlete')['id']
    storage.link_user(account, first)
    yield http_client, storage, first, second, award, other, account
    storage.connection.close()


def upload(case, *, student=None, award=None, body=BODY, name='synthetic.pdf', mime='application/pdf', role='editor'):
    client, _storage, first, _second, record, _other, _account = case
    headers = get_auth_headers(role)
    return client.post(
        f'/admin/people/{student or first}/gto/{award or record}/certificates',
        headers=headers,
        data=csrf_for(headers),
        files={'file': (name, body, mime)},
        allow_redirects=False,
    )[1]


def download_url(student, award, document):
    return f'/gto/{student}/awards/{award}/certificates/{document}'


def athlete_headers():
    value = create_auth_cookie_value('synthetic-athlete', 'athlete')
    return {'cookie': f'{settings.auth_cookie_name}={value}'}


def test_distinct_certificates_scoped_to_awards_and_current_owner(certificate_case):
    client, storage, student, second, award, other, account = certificate_case
    assert upload(certificate_case).status == 302
    assert upload(certificate_case, body=BODY + b'second').status == 302
    documents = storage._list_gto_certificates(student, award)
    assert len(documents) == 2
    assert storage._list_gto_certificates(second, other) == []
    for document in documents:
        url = download_url(student, award, document['id'])
        for headers in (get_auth_headers(), get_auth_headers('editor'), get_auth_headers('viewer'), athlete_headers()):
            _, response = client.get(url, headers=headers)
            assert response.status == 200
            assert response.body.startswith(BODY)
            assert 'no-store' in response.headers['cache-control']
            assert response.headers['x-content-type-options'] == 'nosniff'
        assert client.get(download_url(second, award, document['id']), headers=get_auth_headers())[1].status == 403
        assert client.get(download_url(student, other, document['id']), headers=get_auth_headers())[1].status == 403
    _, page = client.get('/', headers=athlete_headers())
    assert all(download_url(student, award, d['id']) in page.text for d in documents)
    storage.relink_user(account, second)
    assert client.get(download_url(student, award, documents[0]['id']), headers=athlete_headers())[1].status == 403
    assert storage.get_gto_certificate_for_user(student, award, documents[0]['id'], account) is None


@pytest.mark.parametrize('role', ['viewer', 'athlete'])
def test_readonly_roles_cannot_upload_or_delete(certificate_case, role):
    client, storage, student, _second, award, _other, _account = certificate_case
    assert upload(certificate_case, role=role).status == 403
    assert upload(certificate_case).status == 302
    document = storage._list_gto_certificates(student, award)[0]
    headers = get_auth_headers(role) if role != 'athlete' else athlete_headers()
    response = client.post(
        f'/admin/people/{student}/gto/{award}/certificates/{document["id"]}/delete',
        headers=headers,
        data={**csrf_for(headers), 'confirm_delete': 'yes'},
        allow_redirects=False,
    )[1]
    assert response.status == 403
    assert len(storage._list_gto_certificates(student, award)) == 1


@pytest.mark.parametrize(
    'body,name,mime',
    [
        (b'fake', 'fake.pdf', 'application/pdf'),
        (BODY, 'fake.exe', 'application/pdf'),
        (BODY, 'fake.pdf', 'image/png'),
        (b'%PDF-' + b'x' * (5 * 1024 * 1024), 'large.pdf', 'application/pdf'),
    ],
)
def test_invalid_upload_preserves_award(certificate_case, body, name, mime):
    response = upload(certificate_case, body=body, name=name, mime=mime)
    assert response.status == 302
    assert 'admin_error=' in response.headers['location']
    _, storage, student, _, award, _, _ = certificate_case
    assert storage._list_gto_certificates(student, award) == []
    assert storage.get_student_gto_record(student, award)['status'] == 'gold'


def test_csrf_mismatched_parent_and_non_award_upload(certificate_case):
    client, storage, student, second, award, other, _account = certificate_case
    headers = get_auth_headers('editor')
    assert (
        client.post(
            f'/admin/people/{student}/gto/{award}/certificates',
            headers=headers,
            files={'file': ('synthetic.pdf', BODY, 'application/pdf')},
            allow_redirects=False,
        )[1].status
        == 403
    )
    assert upload(certificate_case, student=second, award=award).status == 404
    non_award, _ = storage.create_student_gto_record(student, year=2026, stage=8, status='participated')
    assert upload(certificate_case, award=non_award).status == 404
    assert storage._list_gto_certificates(student, award) == []
    assert storage._list_gto_certificates(second, other) == []


def test_uin_admin_only_duplicate_message_csrf_and_zeroes(certificate_case):
    client, storage, student, second, _award, _other, _account = certificate_case
    for role in ('editor', 'viewer'):
        headers = get_auth_headers(role)
        assert (
            client.post(
                f'/admin/people/{student}/gto/uin',
                headers=headers,
                data={**csrf_for(headers), 'gto_uin': '000123'},
                allow_redirects=False,
            )[1].status
            == 403
        )
    headers = get_auth_headers()
    assert (
        client.post(
            f'/admin/people/{student}/gto/uin',
            headers=headers,
            data={'gto_uin': '000123'},
            allow_redirects=False,
        )[1].status
        == 403
    )
    for identifier in (student, second):
        response = client.post(
            f'/admin/people/{identifier}/gto/uin',
            headers=headers,
            data={**csrf_for(headers), 'gto_uin': '000123'},
            allow_redirects=False,
        )[1]
        assert response.status == 302
    assert 'Этот УИН уже указан у другого студента' in unquote_plus(response.headers['location'])
    assert storage.get_student_by_id(student)['gto_uin'] == '000123'
    assert storage.get_student_by_id(second)['gto_uin'] is None
    page = client.get(f'/admin/people/{student}', headers=headers)[1]
    assert 'value="000123"' in page.text
    assert 'enctype="multipart/form-data"' in page.text


def test_db_save_failure_compensates_file_and_existing_award(certificate_case, monkeypatch):
    _client, storage, student, _second, award, _other, _account = certificate_case
    assert upload(certificate_case).status == 302
    old = storage._list_gto_certificates(student, award)
    old_files = sorted((Path(settings.data_folder) / 'files').rglob('*.pdf'))

    def fail(*args, **kwargs):
        raise RuntimeError('synthetic DB refusal')

    monkeypatch.setattr(storage, 'create_gto_certificate', fail)
    response = upload(certificate_case)
    assert 'admin_error=' in response.headers['location']
    assert storage._list_gto_certificates(student, award) == old
    assert sorted((Path(settings.data_folder) / 'files').rglob('*.pdf')) == old_files
    assert read_certificate(award, old[0]['stored_name'])[0] == BODY


@pytest.mark.parametrize('parent', ['files', 'gto', 'award', 'final'])
def test_symlinked_components_fail_closed(certificate_case, tmp_path, parent):
    client, storage, student, _, award, _, _ = certificate_case
    assert upload(certificate_case).status == 302
    document = storage._list_gto_certificates(student, award)[0]
    award_dir = Path(settings.data_folder) / 'files' / 'gto' / str(award)
    target = {
        'files': award_dir.parent.parent,
        'gto': award_dir.parent,
        'award': award_dir,
        'final': award_dir / document['stored_name'],
    }[parent]
    moved = tmp_path / 'synthetic-moved'
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=parent != 'final')
    response = client.get(download_url(student, award, document['id']), headers=get_auth_headers())[1]
    assert response.status == 404
    if parent != 'final':
        response = upload(certificate_case)
        assert response.status == 302 and 'admin_error=' in response.headers['location']
    assert len(storage._list_gto_certificates(student, award)) == 1


def test_direct_file_helpers_reject_path_and_sanitize_display_name(certificate_case):
    _, _, _, _, award, _, _ = certificate_case
    upload_file = SimpleNamespace(name='..\\folder/unsafe\r\n.pdf', body=BODY, type='application/pdf')
    fields = store_certificate(award, upload_file)
    assert fields['filename'] == 'unsafe.pdf'
    with pytest.raises(ValueError):
        read_certificate(award, '../unsafe.pdf')


def test_deletion_and_correction_keep_metadata_consistent(certificate_case):
    client, storage, student, _, award, _, _ = certificate_case
    assert upload(certificate_case).status == 302
    document = storage._list_gto_certificates(student, award)[0]
    assert storage.update_student_gto_record(student, award, year=2026, stage=8, status='participated') == (
        False,
        'certificates_require_award',
    )
    headers = get_auth_headers('editor')
    url = f'/admin/people/{student}/gto/{award}/certificates/{document["id"]}/delete'
    response = client.post(url, headers=headers, data=csrf_for(headers), allow_redirects=False)[1]
    assert 'admin_error=' in response.headers['location']
    assert len(storage._list_gto_certificates(student, award)) == 1
    response = client.post(
        url, headers=headers, data={**csrf_for(headers), 'confirm_delete': 'yes'}, allow_redirects=False
    )[1]
    assert response.status == 302
    assert storage._list_gto_certificates(student, award) == []
    with pytest.raises(FileNotFoundError):
        read_certificate(award, document['stored_name'])
    assert upload(certificate_case).status == 302
    response = client.post(
        f'/admin/people/{student}/gto/{award}/delete',
        headers=headers,
        data=csrf_for(headers),
        allow_redirects=False,
    )[1]
    assert response.status == 302
    assert storage.get_student_gto_record(student, award) is None
    assert storage.connection.execute('SELECT COUNT(*) FROM student_gto_certificates').fetchone()[0] == 0
    assert not list((Path(settings.data_folder) / 'files').rglob('*.pdf'))


@pytest.mark.parametrize('change', ['relink', 'inactive', 'role', 'password', 'metadata', 'student', 'file'])
def test_download_revalidates_authority_and_version_after_file_read(certificate_case, monkeypatch, change):
    import src.routes.gto as routes

    client, storage, student, second, award, _, account = certificate_case
    assert upload(certificate_case).status == 302
    document = storage._list_gto_certificates(student, award)[0]
    original = routes.read_certificate

    def read_then_change(record_id, stored_name):
        result = original(record_id, stored_name)
        if change == 'relink':
            storage.relink_user(account, second)
        elif change in ('inactive', 'role', 'password'):
            sql = {'inactive': 'active=0', 'role': "role='viewer'", 'password': 'pwd_ver=pwd_ver+1'}[change]
            with storage._lock:
                storage.connection.execute(f'UPDATE users SET {sql} WHERE id=?', (account,))
                storage.connection.commit()
        elif change == 'metadata':
            with storage._lock:
                storage.connection.execute(
                    'UPDATE student_gto_certificates SET size=size+1 WHERE id=?', (document['id'],)
                )
                storage.connection.commit()
        elif change == 'student':
            with storage._lock:
                storage.connection.execute('DELETE FROM students WHERE id=?', (student,))
                storage.connection.commit()
        elif change == 'file':
            path = Path(settings.data_folder) / 'files' / 'gto' / str(award) / stored_name
            path.write_bytes(BODY + b'changed')
        return result

    monkeypatch.setattr(routes, 'read_certificate', read_then_change)
    response = client.get(download_url(student, award, document['id']), headers=athlete_headers())[1]
    # A role change to viewer permits staff reading under the established matrix.
    assert response.status == (200 if change == 'role' else 409 if change in ('metadata', 'file') else 403)


class FailingCertificateCommit:
    def __init__(self, original):
        self.original = original
        self.certificate_write = False

    def execute(self, sql, *args):
        if sql.startswith('INSERT INTO student_gto_certificates '):
            self.certificate_write = True
        return self.original.execute(sql, *args)

    def rollback(self):
        self.certificate_write = False
        return self.original.rollback()

    def __getattr__(self, name):
        return getattr(self.original, name)

    def commit(self):
        if self.certificate_write:
            raise RuntimeError('synthetic failed commit')
        return self.original.commit()


@pytest.mark.parametrize('fault', ['commit', 'write', 'unlink'])
def test_partial_failures_preserve_visible_database_file_consistency(certificate_case, monkeypatch, fault):
    import src.gto_files as files
    import src.routes.gto as routes

    client, storage, student, _, award, _, _ = certificate_case
    if fault == 'commit':
        # Start the ordinary scenario before injecting a domain commit fault.
        assert client.get('/healthcheck')[1].status == 200
        original = storage.connection

        storage.connection = FailingCertificateCommit(original)
        try:
            response = upload(certificate_case)
            assert 'admin_error=' in response.headers['location']
            assert storage._list_gto_certificates(student, award) == []
            assert not original.in_transaction
            assert not list((Path(settings.data_folder) / 'files').rglob('*.pdf'))
        finally:
            storage.connection = original
    elif fault == 'write':

        def fail_fsync(*args):
            raise OSError('synthetic failed flush')

        monkeypatch.setattr(files.os, 'fsync', fail_fsync)
        response = upload(certificate_case)
        assert 'admin_error=' in response.headers['location']
        assert storage._list_gto_certificates(student, award) == []
        assert not list((Path(settings.data_folder) / 'files').rglob('*.pdf'))
    else:
        assert upload(certificate_case).status == 302
        document = storage._list_gto_certificates(student, award)[0]

        def fail_unlink(*args):
            raise OSError('synthetic failed cleanup')

        monkeypatch.setattr(routes, 'remove_certificate', fail_unlink)
        headers = get_auth_headers('editor')
        response = client.post(
            f'/admin/people/{student}/gto/{award}/certificates/{document["id"]}/delete',
            headers=headers,
            data={**csrf_for(headers), 'confirm_delete': 'yes'},
            allow_redirects=False,
        )[1]
        assert response.status == 302
        assert storage._list_gto_certificates(student, award) == []
        assert storage.get_student_gto_record(student, award) is not None
        assert client.get(download_url(student, award, document['id']), headers=headers)[1].status == 403


def test_corrupt_metadata_and_orphan_owner_fail_closed(certificate_case):
    client, storage, student, _, award, _, _ = certificate_case
    assert upload(certificate_case).status == 302
    document = storage._list_gto_certificates(student, award)[0]
    with storage._lock:
        storage.connection.execute(
            "UPDATE student_gto_certificates SET stored_name='../private.pdf' WHERE id=?", (document['id'],)
        )
        storage.connection.commit()
    assert client.get(download_url(student, award, document['id']), headers=get_auth_headers())[1].status == 404
    with storage._lock:
        storage.connection.execute(
            'UPDATE student_gto_certificates SET stored_name=?, content_type=? WHERE id=?',
            (document['stored_name'], 'text/html', document['id']),
        )
        storage.connection.commit()
    assert client.get(download_url(student, award, document['id']), headers=get_auth_headers())[1].status == 404


def test_non_regular_certificate_cannot_block_download(certificate_case):
    import os

    client, storage, student, _, award, _, _ = certificate_case
    assert upload(certificate_case).status == 302
    document = storage._list_gto_certificates(student, award)[0]
    path = Path(settings.data_folder) / 'files' / 'gto' / str(award) / document['stored_name']
    path.unlink()
    os.mkfifo(path)
    assert client.get(download_url(student, award, document['id']), headers=get_auth_headers())[1].status == 404


@pytest.mark.parametrize(
    'url',
    [
        '/admin/people/7?tab=gto#person-gto',
        '/admin/people/7#person-gto',
        '/admin/people/7?tab=gto&filter=one&filter=two#person-gto',
    ],
)
def test_flash_redirect_parameters_are_sent_before_fragment(url):
    from urllib.parse import parse_qs
    from urllib.parse import urlsplit

    from src.web import build_redirect_with_message

    response = build_redirect_with_message(error='Синтетическая ошибка', url=url)
    target = urlsplit(response.headers['location'])
    original = urlsplit(url)
    actual = parse_qs(target.query)
    assert target.fragment == 'person-gto'
    assert actual == {**parse_qs(original.query), 'admin_error': ['Синтетическая ошибка']}
    assert 'admin_error' not in target.fragment
    response = build_redirect_with_message(message='Синтетический успех', url=url)
    assert parse_qs(urlsplit(response.headers['location']).query)['admin_message'] == ['Синтетический успех']


def test_gto_native_errors_and_success_are_rendered_after_redirect(certificate_case):
    from urllib.parse import parse_qs
    from urllib.parse import urlsplit

    client, storage, student, second, award, _, _ = certificate_case
    assert storage.set_student_gto_uin(student, '000123') == (True, None)
    headers = get_auth_headers()
    duplicate = client.post(
        f'/admin/people/{second}/gto/uin',
        headers=headers,
        data={**csrf_for(headers), 'gto_uin': '000123'},
        allow_redirects=False,
    )[1]
    target = urlsplit(duplicate.headers['location'])
    error = parse_qs(target.query)['admin_error'][0]
    assert parse_qs(target.query)['tab'] == ['gto'] and target.fragment == ''
    assert 'УИН уже указан у другого студента' in error
    # A fragment is never part of the HTTP request; follow the real server URL.
    page = client.get(target.path + '?' + target.query, headers=headers)[1]
    assert page.status == 200
    assert error in page.text and 'alert-danger' in page.text
    invalid_file = upload(certificate_case, body=b'synthetic-invalid-file')
    target = urlsplit(invalid_file.headers['location'])
    error = parse_qs(target.query)['admin_error'][0]
    assert target.fragment == '' and parse_qs(target.query)['tab'] == ['gto']
    assert 'admin_message' not in parse_qs(target.query)
    page = client.get(target.path + '?' + target.query, headers=get_auth_headers('editor'))[1]
    assert error in page.text and 'alert-danger' in page.text
    assert storage._list_gto_certificates(student, award) == []
    success = upload(certificate_case)
    target = urlsplit(success.headers['location'])
    message = parse_qs(target.query)['admin_message'][0]
    assert target.fragment == '' and parse_qs(target.query)['tab'] == ['gto']
    assert 'admin_error' not in parse_qs(target.query)
    page = client.get(target.path + '?' + target.query, headers=get_auth_headers('editor'))[1]
    assert message in page.text and 'alert-success' in page.text


def test_all_gto_mutations_redirect_to_visible_page_top_flash(certificate_case):
    from urllib.parse import parse_qs
    from urllib.parse import urlsplit

    client, storage, student, _, award, _, _ = certificate_case
    headers = get_auth_headers()

    def post(path, values, flash):
        response = client.post(
            path,
            headers=headers,
            data={**csrf_for(headers), **values},
            allow_redirects=False,
        )[1]
        assert response.status == 302
        target = urlsplit(response.headers['location'])
        assert target.path == f'/admin/people/{student}'
        assert not target.fragment  # Native navigation must not scroll past the flash.
        query = parse_qs(target.query)
        assert query['tab'] == ['gto']
        assert query.get(flash)
        assert ('admin_error' in query) != ('admin_message' in query)
        return response

    base = f'/admin/people/{student}/gto'
    post(base + '/uin', {'gto_uin': '000042'}, 'admin_message')
    post(base, {'year': '', 'stage': '8', 'status': 'gold'}, 'admin_error')
    post(base, {'year': '2026', 'stage': '8', 'status': 'invalid'}, 'admin_error')
    post(base, {'year': '2026', 'stage': '8', 'status': 'silver'}, 'admin_message')
    post(f'{base}/{award}/edit', {'year': '', 'stage': '8', 'status': 'gold'}, 'admin_error')
    post(f'{base}/{award}/edit', {'year': '2026', 'stage': '8', 'status': 'invalid'}, 'admin_error')
    post(f'{base}/999999/edit', {'year': '2026', 'stage': '8', 'status': 'gold'}, 'admin_error')
    post(f'{base}/{award}/edit', {'year': '2026', 'stage': '8', 'status': 'bronze'}, 'admin_message')
    post(f'{base}/999999/delete', {}, 'admin_error')
    assert upload(certificate_case).status == 302
    document = storage._list_gto_certificates(student, award)[0]
    remove = f'{base}/{award}/certificates/{document["id"]}/delete'
    post(remove, {}, 'admin_error')
    post(remove, {'confirm_delete': 'yes'}, 'admin_message')
    post(f'{base}/{award}/delete', {}, 'admin_message')
