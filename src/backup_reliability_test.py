"""Failure-injection checks using only temporary, fully synthetic backup data."""
import gzip
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from scripts import restore_backup
from scripts import restore_drill
from src import backup
from src import backup_offsite
from src.ops_state import atomic_json
from src.ops_state import read_json
from src.ops_state import update_component


@pytest.fixture
def synthetic_source(tmp_path):
    database = tmp_path / 'source.sqlite3'
    files = tmp_path / 'files'
    references = {
        'record-fixture/attachment.txt',
        'calendar/event-fixture/regulation.txt',
        'calendar/event-fixture/document.txt',
    }
    with sqlite3.connect(database) as connection:
        connection.executescript(
            '''
            PRAGMA journal_mode=WAL;
            CREATE TABLE attachments(record_id TEXT, stored_name TEXT);
            CREATE TABLE calendar_events(id TEXT, regulation_stored_name TEXT);
            CREATE TABLE calendar_event_documents(calendar_event_id TEXT, stored_name TEXT);
            INSERT INTO attachments VALUES ('record-fixture', 'attachment.txt');
            INSERT INTO calendar_events VALUES ('event-fixture', 'regulation.txt');
            INSERT INTO calendar_event_documents VALUES ('event-fixture', 'document.txt');
        '''
        )
    for name in references:
        path = files / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Synthetic public-safe fixture: ' + name)
    return database, files, tmp_path / 'backups', tmp_path / 'ops', references


def make_set(fixture, **kwargs):
    database, files, output, state, _ = fixture
    return backup.run_backup(database, output, files_dir=files, state_dir=state, **kwargs)['manifest'].parent


def snapshot_tree(directory):
    return {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob('*') if p.is_file()}


def proven_cryptcheck(args):
    names = sorted(path.name for path in Path(args[1]).iterdir() if path.is_file())
    if '--combined' in args:
        Path(args[args.index('--combined') + 1]).write_text(''.join('= ' + name + '\n' for name in names))
    return f'INFO: 0 differences found\nINFO: {len(names)} matching files'.encode()


def test_verified_set_preserves_wal_and_all_reference_types(synthetic_source):
    database, _, _, state, references = synthetic_source
    # Keep WAL live during creation; the inserted row must be in the backup.
    writer = sqlite3.connect(database)
    try:
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('CREATE TABLE wal_fixture(value TEXT)')
        writer.execute("INSERT INTO wal_fixture VALUES ('synthetic-wal-value')")
        writer.commit()
        directory = make_set(synthetic_source, source='release')
        manifest = backup.verify_set(directory)
        assert manifest['status'] == 'Verified'
        assert manifest['source'] == 'release'
        restored = database.parent / 'isolated.sqlite3'
        restored.write_bytes(gzip.decompress((directory / 'database.sqlite3.gz').read_bytes()))
        assert backup.reference_names(restored) == references
        with sqlite3.connect(restored) as connection:
            assert connection.execute('SELECT value FROM wal_fixture').fetchone() == ('synthetic-wal-value',)
        with zipfile.ZipFile(directory / 'files.zip') as archive:
            assert references.issubset(archive.namelist())
        assert read_json(state / 'health.json')['local']['status'] == 'OK'
    finally:
        writer.close()


@pytest.mark.parametrize('payload', [b'not-gzip', gzip.compress(b'not-a-sqlite-database'), gzip.compress(b'x')[:-3]])
def test_corrupt_sqlite_gzip_is_rejected(tmp_path, payload):
    archive = tmp_path / 'broken.gz'
    archive.write_bytes(payload)
    with pytest.raises(backup.BackupError, match='sqlite_integrity_failed'):
        backup.verify_archive(archive)


def test_quick_check_failure_is_not_success(monkeypatch, tmp_path):
    class BrokenConnection:
        def execute(self, _):
            return self

        def fetchall(self):
            return [('synthetic integrity failure',)]

        def close(self):
            pass

    monkeypatch.setattr(backup.sqlite3, 'connect', lambda *a, **k: BrokenConnection())
    with pytest.raises(backup.BackupError, match='sqlite_integrity_failed'):
        backup.check_sqlite(tmp_path / 'synthetic.sqlite3')


@pytest.mark.parametrize('kind', ['invalid', 'crc', 'traversal'])
def test_invalid_zip_is_rejected(tmp_path, kind):
    archive = tmp_path / 'broken.zip'
    if kind == 'invalid':
        archive.write_bytes(b'not-a-zip')
    else:
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_STORED) as stream:
            stream.writestr('../outside' if kind == 'traversal' else 'fixture.txt', b'fixture-payload')
        if kind == 'crc':
            archive.write_bytes(archive.read_bytes().replace(b'fixture-payload', b'broken!-payload'))
    with pytest.raises(backup.BackupError, match='zip_verification_failed'):
        backup.verify_zip(archive)


@pytest.mark.parametrize(
    'missing',
    ['record-fixture/attachment.txt', 'calendar/event-fixture/regulation.txt', 'calendar/event-fixture/document.txt'],
)
def test_missing_referenced_file_fails_without_removing_previous_set(synthetic_source, missing):
    good = make_set(synthetic_source)
    before = snapshot_tree(good)
    (synthetic_source[1] / missing).unlink()
    with pytest.raises(backup.BackupError, match='referenced_file_missing'):
        make_set(synthetic_source)
    assert snapshot_tree(good) == before
    failures = list(synthetic_source[2].glob('failed-*.json'))
    assert len(failures) == 1
    assert read_json(failures[0])['status'] == 'Failed'
    assert len(list((synthetic_source[2] / 'sets').iterdir())) == 1
    assert read_json(synthetic_source[3] / 'health.json')['local']['status'] == 'CRITICAL'


def test_successful_repeats_do_not_apply_retention(synthetic_source):
    first = make_set(synthetic_source, keep=1)
    before = snapshot_tree(first)
    second = make_set(synthetic_source, keep=1)
    assert first != second
    assert snapshot_tree(first) == before
    assert len(list((synthetic_source[2] / 'sets').iterdir())) == 2


def test_database_only_compatibility_is_never_a_complete_or_offsite_success(synthetic_source, monkeypatch):
    database, _, output, state, _ = synthetic_source
    result = backup.run_backup(database, output, state_dir=state)
    assert result['files_archive'] is None
    directory = result['manifest'].parent
    assert read_json(result['manifest'])['mode'] == 'database_only'
    assert read_json(state / 'health.json')['local']['status'] == 'WARNING'
    with pytest.raises(backup.BackupError, match='incomplete_set'):
        backup.verify_set(directory)
    monkeypatch.setattr(backup_offsite, 'rclone', lambda *a, **k: pytest.fail('incomplete set uploaded'))
    with pytest.raises(backup.BackupError, match='incomplete_set'):
        backup_offsite.upload_fresh([], 'synthetic-crypt:backups', state, set_dir=directory)
    assert read_json(state / 'health.json')['s3']['status'] == 'CRITICAL'


def test_explicit_missing_files_directory_is_valid_only_without_references(synthetic_source, tmp_path):
    database, _, output, state, _ = synthetic_source
    nonexistent_files = tmp_path / 'not-created-files'
    with pytest.raises(backup.BackupError, match='referenced_file_missing'):
        backup.run_backup(database, output, files_dir=nonexistent_files, state_dir=state)
    with sqlite3.connect(database) as connection:
        connection.execute('DELETE FROM attachments')
        connection.execute('DELETE FROM calendar_event_documents')
        connection.execute('UPDATE calendar_events SET regulation_stored_name=NULL')
    result = backup.run_backup(database, output, files_dir=nonexistent_files, state_dir=state)
    assert not nonexistent_files.exists()
    assert backup.verify_set(result['manifest'].parent)['mode'] == 'complete'
    with zipfile.ZipFile(result['files_archive']) as archive:
        assert archive.namelist() == []
    assert read_json(state / 'health.json')['local']['status'] == 'OK'


@pytest.mark.parametrize('status', ['Started', 'Completed', 'Failed'])
def test_incomplete_manifest_cannot_be_verified(synthetic_source, status):
    directory = make_set(synthetic_source)
    manifest = read_json(directory / 'manifest.json')
    manifest['status'] = status
    atomic_json(directory / 'manifest.json', manifest)
    with pytest.raises(backup.BackupError, match='incomplete_set'):
        backup.verify_set(directory)


def test_manifest_hash_rejects_modified_artifact(synthetic_source):
    directory = make_set(synthetic_source)
    archive = directory / 'files.zip'
    archive.write_bytes(archive.read_bytes() + b'tampered')
    with pytest.raises(backup.BackupError, match='artifact_integrity_failed'):
        backup.verify_set(directory)


def test_invalid_database_records_failure_without_deleting_good_set(synthetic_source):
    good = make_set(synthetic_source)
    before = snapshot_tree(good)
    # Replace only this test's private source; no runtime database is involved.
    synthetic_source[0].write_bytes(b'synthetic-not-a-database')
    with pytest.raises(backup.BackupError):
        make_set(synthetic_source)
    assert snapshot_tree(good) == before
    assert read_json(synthetic_source[3] / 'health.json')['local']['status'] == 'CRITICAL'


def test_concurrent_database_commit_is_detected_and_retried(synthetic_source, monkeypatch):
    original = backup.backup_files
    calls = []

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if len(calls) == 1:
            with sqlite3.connect(synthetic_source[0]) as connection:
                connection.execute('CREATE TABLE concurrent_fixture(value TEXT)')
                connection.execute("INSERT INTO concurrent_fixture VALUES ('synthetic-new-value')")
        return result

    monkeypatch.setattr(backup, 'backup_files', changed)
    directory = make_set(synthetic_source)
    restored = synthetic_source[0].parent / 'db-concurrent-restore'
    restored.write_bytes(gzip.decompress((directory / 'database.sqlite3.gz').read_bytes()))
    with sqlite3.connect(restored) as connection:
        assert connection.execute('SELECT value FROM concurrent_fixture').fetchone() == ('synthetic-new-value',)
    assert len(calls) == 2


@pytest.mark.parametrize('persistent', [False, True])
def test_concurrent_file_change_retries_or_fails_safely(synthetic_source, monkeypatch, persistent):
    good = make_set(synthetic_source)
    before = snapshot_tree(good)
    original = backup.backup_files
    calls = []

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if persistent or len(calls) == 1:
            path = synthetic_source[1] / 'record-fixture/attachment.txt'
            path.write_text('Synthetic changed content ' + str(len(calls)))
        return result

    monkeypatch.setattr(backup, 'backup_files', changed)
    if persistent:
        with pytest.raises(backup.BackupError, match='source_changed'):
            make_set(synthetic_source)
        assert len(calls) == 3
    else:
        backup.verify_set(make_set(synthetic_source))
        assert len(calls) == 2
    assert snapshot_tree(good) == before


def test_offsite_immutable_retry_and_cryptcheck_before_completion(synthetic_source, monkeypatch):
    directory = make_set(synthetic_source)
    before = snapshot_tree(directory)
    commands = []

    def fail_first_check(args, config=None):
        commands.append(args)
        if args[0] == 'cryptcheck':
            raise backup.BackupError('s3_upload_failed')

    monkeypatch.setattr(backup_offsite, 'rclone', fail_first_check)
    with pytest.raises(backup.BackupError, match='s3_upload_failed'):
        backup_offsite.upload_fresh([], 'synthetic-crypt:backups', synthetic_source[3], set_dir=directory)
    assert [args[0] for args in commands] == ['copy', 'cryptcheck']
    assert '--immutable' in commands[0]
    assert not read_json(synthetic_source[3] / 'offsite-receipts.json')
    assert read_json(synthetic_source[3] / 'health.json')['s3']['status'] == 'CRITICAL'
    commands.clear()

    def successful_rclone(args, config=None):
        commands.append(args)
        if args[0] == 'cryptcheck':
            return proven_cryptcheck(args)
        return b''

    monkeypatch.setattr(backup_offsite, 'rclone', successful_rclone)
    backup_offsite.upload_fresh([], 'synthetic-crypt:backups', synthetic_source[3], set_dir=directory)
    assert [args[0] for args in commands] == ['copy', 'cryptcheck', 'copyto', 'cryptcheck']
    assert all('--immutable' in args for args in commands if args[0] in {'copy', 'copyto'})
    assert all('--size-only=false' in args and '--combined' in args for args in commands if args[0] == 'cryptcheck')
    assert read_json(synthetic_source[3] / 'health.json')['s3']['status'] == 'OK'
    assert directory.name in read_json(synthetic_source[3] / 'offsite-receipts.json')
    assert snapshot_tree(directory) == before


@pytest.mark.parametrize(
    'report',
    [
        b'',
        b'0 matching files',
        b'1 matching files',
        b'2 matching files',
        b'0 differences found\n2 hashes could not be checked\n2 matching files',
    ],
)
def test_unproven_cryptcheck_preserves_last_success_and_local_set(synthetic_source, monkeypatch, report):
    directory = make_set(synthetic_source)
    before = snapshot_tree(directory)
    old = {
        'synthetic-prior-point': {'created_at': '2026-01-01T00:00:00+00:00', 'uploaded_at': '2026-01-01T00:01:00+00:00'}
    }
    atomic_json(synthetic_source[3] / 'offsite-receipts.json', old)
    atomic_json(
        synthetic_source[3] / 'health.json',
        {
            's3': {
                'status': 'OK',
                'id': 'synthetic-prior-point',
                'created_at': old['synthetic-prior-point']['created_at'],
            }
        },
    )
    commands = []

    def rclone(args, config=None):
        commands.append(args)
        if args[0] == 'cryptcheck':
            proven_cryptcheck(args)
            return report
        return b''

    monkeypatch.setattr(backup_offsite, 'rclone', rclone)
    with pytest.raises(backup.BackupError, match='s3_verification_unproven'):
        backup_offsite.upload_fresh([], 'synthetic-crypt:backups', synthetic_source[3], set_dir=directory)
    assert not any(args[0] == 'copyto' for args in commands)
    assert read_json(synthetic_source[3] / 'offsite-receipts.json') == old
    state = read_json(synthetic_source[3] / 'health.json')['s3']
    assert state['status'] == 'CRITICAL'
    assert state['id'] == 'synthetic-prior-point'
    assert snapshot_tree(directory) == before


def test_final_cryptcheck_failure_never_publishes_success_receipt(synthetic_source, monkeypatch):
    directory = make_set(synthetic_source)
    reports = iter([b'INFO: 0 differences found\n2 matching files', b'INFO: 0 differences found\n2 matching files'])

    def rclone(args, config=None):
        if args[0] == 'cryptcheck':
            proven_cryptcheck(args)
            return next(reports)
        return b''

    monkeypatch.setattr(backup_offsite, 'rclone', rclone)
    with pytest.raises(backup.BackupError, match='s3_verification_unproven'):
        backup_offsite.upload_fresh([], 'synthetic-crypt:backups', synthetic_source[3], set_dir=directory)
    assert not read_json(synthetic_source[3] / 'offsite-receipts.json')
    assert read_json(synthetic_source[3] / 'health.json')['s3']['status'] == 'CRITICAL'


@pytest.mark.parametrize(
    'combined',
    [None, '', '= first\n', '= first\n= unexpected\n', '= first\n! second\n', '= first\n= first\n= second\n'],
)
def test_positive_summary_requires_exact_combined_hash_proof(tmp_path, monkeypatch, combined):
    (tmp_path / 'first').write_text('Synthetic fixture one')
    (tmp_path / 'second').write_text('Synthetic fixture two')

    def rclone(args, config=None):
        assert '--size-only=false' in args
        if combined is not None:
            Path(args[args.index('--combined') + 1]).write_text(combined)
        return b'INFO: 0 differences found\nINFO: 2 matching files'

    monkeypatch.setattr(backup_offsite, 'rclone', rclone)
    with pytest.raises(backup.BackupError, match='s3_verification_unproven'):
        backup_offsite.cryptcheck(tmp_path, 'synthetic-crypt:backups', 2)


@pytest.mark.parametrize('age', [None, -3600, 27 * 3600])
def test_missing_future_or_stale_backup_does_not_upload(synthetic_source, monkeypatch, age):
    directory = make_set(synthetic_source)
    manifest = read_json(directory / 'manifest.json')
    manifest['created_at'] = None if age is None else (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
    atomic_json(directory / 'manifest.json', manifest)
    commands = []
    monkeypatch.setattr(backup_offsite, 'rclone', lambda args, config=None: commands.append(args))
    with pytest.raises(backup.BackupError, match='no_fresh_local_backup'):
        backup_offsite.upload_fresh([], 'synthetic-crypt:backups', synthetic_source[3], set_dir=directory)
    assert not commands
    assert read_json(synthetic_source[3] / 'health.json')['s3']['status'] == 'CRITICAL'


def test_release_and_daily_sets_are_both_uploaded(synthetic_source, monkeypatch):
    daily = make_set(synthetic_source, source='daily')
    release = make_set(synthetic_source, source='release')
    uploaded = []
    monkeypatch.setattr(backup_offsite, 'upload_set', lambda directory, *a, **k: uploaded.append(directory))
    backup_offsite.upload_fresh([synthetic_source[2]], 'synthetic-crypt:backups', synthetic_source[3])
    assert set(uploaded) == {daily, release}


@pytest.mark.parametrize('failure', ['exit', 'timeout'])
def test_rclone_failure_exposes_only_safe_code(monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(['synthetic-private-command'], 600)
        return subprocess.CompletedProcess(args[0], 7, b'', b'synthetic-private-details')

    monkeypatch.setattr(backup_offsite.subprocess, 'run', run)
    with pytest.raises(backup.BackupError) as error:
        backup_offsite.rclone(['copy', 'synthetic-source', 'synthetic-remote'])
    assert str(error.value) in {'s3_upload_failed', 's3_unavailable'}


def test_isolated_extraction_never_records_full_restore_pass(synthetic_source, tmp_path, monkeypatch, capsys):
    directory = make_set(synthetic_source)
    destination = tmp_path / 'isolated-restore'
    monkeypatch.setattr(sys, 'argv', ['restore_backup', '--set-dir', str(directory), '--destination', str(destination)])
    assert restore_backup.main() == 0
    assert 'NOT yet verified' in capsys.readouterr().out
    assert 'restore' not in read_json(synthetic_source[3] / 'health.json')
    assert backup.reference_names(destination / 'competitions.sqlite3') == synthetic_source[4]
    with pytest.raises(FileExistsError):
        restore_backup.extract_set(directory, destination)


@pytest.mark.parametrize('trusted_hash', [None, 'incorrect', 'correct'])
def test_remote_restore_requires_independent_hash_before_trust(synthetic_source, tmp_path, monkeypatch, trusted_hash):
    original = make_set(synthetic_source)
    downloaded = tmp_path / 'downloaded-set'
    destination = tmp_path / 'downloaded-restore'
    commands = []

    def download(args, config=None):
        commands.append(args)
        for name in ('manifest.json', 'files.zip', 'database.sqlite3.gz'):
            shutil.copyfile(original / name, downloaded / name)

    monkeypatch.setattr(restore_backup, 'rclone', download)
    arguments = [
        'restore_backup',
        '--set-dir',
        str(downloaded),
        '--destination',
        str(destination),
        '--remote-set',
        'synthetic-crypt:sets/synthetic-fixture',
    ]
    if trusted_hash is not None:
        arguments += [
            '--trusted-manifest-sha256',
            backup.sha256(original / 'manifest.json') if trusted_hash == 'correct' else '0' * 64,
        ]
    monkeypatch.setattr(sys, 'argv', arguments)
    if trusted_hash is None:
        with pytest.raises(SystemExit) as result:
            restore_backup.main()
        assert result.value.code == 2
        assert not commands
    else:
        assert restore_backup.main() == (0 if trusted_hash == 'correct' else 1)
        assert len(commands) == 1 and '--immutable' in commands[0]
    marker = read_json(downloaded / '.remote-verified.json')
    assert bool(marker) == (trusted_hash == 'correct')
    assert destination.exists() == (trusted_hash == 'correct')
    assert 'restore' not in read_json(synthetic_source[3] / 'health.json')


def test_production_drill_requires_verified_remote_marker_before_start(synthetic_source, monkeypatch):
    directory = make_set(synthetic_source)
    monkeypatch.setattr(restore_drill.subprocess, 'run', lambda *a, **k: pytest.fail('untrusted drill started Docker'))
    with pytest.raises(ValueError, match='independently authenticated'):
        restore_drill.drill(directory, 'synthetic:image', synthetic_source[3], '0123456', production=True)
    assert 'restore' not in read_json(synthetic_source[3] / 'health.json')


@pytest.mark.parametrize('failure', [None, 'start', 'health'])
def test_mocked_full_drill_isolated_steps_cleanup_and_no_false_real_pass(synthetic_source, monkeypatch, failure):
    directory = make_set(synthetic_source)
    commands = []

    def run(arguments, **kwargs):
        commands.append((arguments, kwargs))
        failed = (failure == 'start' and arguments[1] == 'run') or (failure == 'health' and arguments[1] == 'exec')
        return subprocess.CompletedProcess(arguments, int(failed), b'synthetic-container', b'')

    monkeypatch.setattr(restore_drill.subprocess, 'run', run)
    monkeypatch.setattr(restore_drill.time, 'sleep', lambda _: None)
    if failure:
        with pytest.raises(RuntimeError, match='isolated_'):
            restore_drill.drill(directory, 'synthetic:image', synthetic_source[3], '0123456')
    else:
        restore_drill.drill(directory, 'synthetic:image', synthetic_source[3], '0123456')
    state = read_json(synthetic_source[3] / 'health.json')
    assert 'restore' not in state
    assert state['restore_synthetic']['status'] == ('CRITICAL' if failure else 'OK')
    start, options = commands[0]
    assert '--network' in start and start[start.index('--network') + 1] == 'none'
    assert '--read-only' in start
    assert '-p' not in start and '--publish' not in start
    assert options['env']['AUTH_SECRET_KEY'] not in ' '.join(start)
    restored = Path(start[start.index('-v') + 1].split(':/code/data')[0])
    assert not restored.exists()  # Temporary drill data is cleaned on success AND failure.
    assert commands[-1][0][:3] == ['docker', 'rm', '--force']
    assert commands[-1][0][3] == start[start.index('--name') + 1]


def test_source_change_during_inventory_gets_bounded_retry(synthetic_source, monkeypatch):
    original = backup.inventory
    calls = []

    def inventory(root):
        calls.append(True)
        if len(calls) == 1:
            raise backup.BackupError('source_changed')
        return original(root)

    monkeypatch.setattr(backup, 'inventory', inventory)
    directory = make_set(synthetic_source)
    assert backup.verify_set(directory)['status'] == 'Verified'
    assert len(calls) >= 3


@pytest.mark.parametrize('previous_status', ['OK', 'CRITICAL'])
def test_uploading_older_release_preserves_newest_confirmed_remote_point(
    synthetic_source, monkeypatch, previous_status
):
    directory = make_set(synthetic_source, source='release')
    manifest = read_json(directory / 'manifest.json')
    manifest['created_at'] = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
    atomic_json(directory / 'manifest.json', manifest)
    newest = {
        'status': previous_status,
        'code': 's3_upload_failed' if previous_status == 'CRITICAL' else 'verified',
        'id': 'synthetic-newest-point',
        'created_at': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        'uploaded_at': (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
    }
    update_component(synthetic_source[3], 's3', newest)
    monkeypatch.setattr(backup_offsite, 'upload_set', lambda *args, **kwargs: manifest)
    backup_offsite.upload_fresh([], 'synthetic-crypt:backups', synthetic_source[3], set_dir=directory)
    state = read_json(synthetic_source[3] / 'health.json')['s3']
    assert state['status'] == 'OK'
    assert {key: state[key] for key in ('id', 'created_at', 'uploaded_at')} == {
        key: newest[key] for key in ('id', 'created_at', 'uploaded_at')
    }
    assert state['last_uploaded_id'] == manifest['id']
    assert state['last_uploaded_created_at'] == manifest['created_at']
    assert state['last_attempt_at']


def test_rechecking_same_remote_point_refreshes_confirmation_without_changing_source_time(
    synthetic_source, monkeypatch
):
    directory = make_set(synthetic_source)
    manifest = read_json(directory / 'manifest.json')
    old_upload = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    update_component(
        synthetic_source[3],
        's3',
        {
            'status': 'OK',
            'code': 'verified',
            'id': manifest['id'],
            'created_at': manifest['created_at'],
            'uploaded_at': old_upload,
        },
    )
    monkeypatch.setattr(backup_offsite, 'upload_set', lambda *args, **kwargs: manifest)
    backup_offsite.upload_fresh([], 'synthetic-crypt:backups', synthetic_source[3], set_dir=directory)
    state = read_json(synthetic_source[3] / 'health.json')['s3']
    assert state['created_at'] == manifest['created_at']
    assert state['uploaded_at'] > old_upload
