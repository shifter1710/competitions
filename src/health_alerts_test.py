"""Cached health and mocked Telegram tests; no network or production state."""
import io
import json
import urllib.error
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from src import health
from src import telegram_alerts
from src.ops_state import atomic_json
from src.ops_state import read_json
from src.ops_state import update_component


def timestamp(seconds_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def card(state_dir, key):
    return next(item for item in health.health_view(state_dir)['cards'] if item['key'] == key)


@pytest.mark.parametrize('component', list(health.TITLES))
def test_missing_or_future_checks_are_never_green(tmp_path, component):
    assert card(tmp_path, component)['status'] != 'OK'
    atomic_json(tmp_path / 'health.json', {component: {'status': 'OK', 'code': 'ok', 'checked_at': timestamp(-3600)}})
    assert card(tmp_path, component)['status'] != 'OK'


@pytest.mark.parametrize('component', list(health.TITLES))
def test_stale_check_is_not_green(tmp_path, component):
    atomic_json(
        tmp_path / 'health.json',
        {
            component: {
                'status': 'OK',
                'code': 'pass' if component == 'restore' else 'ok',
                'checked_at': timestamp(27 * 3600),
                'created_at': timestamp(27 * 3600),
                'tested_at': timestamp(10),
            }
        },
    )
    assert card(tmp_path, component)['status'] != 'OK'


@pytest.mark.parametrize('seconds_ago', [None, -3600, 27 * 3600])
@pytest.mark.parametrize('component', ['local', 's3'])
def test_last_backup_age_is_validated_even_with_fresh_check(tmp_path, component, seconds_ago):
    state = {
        component: {
            'status': 'OK',
            'code': 'verified',
            'checked_at': timestamp(),
            'created_at': None if seconds_ago is None else timestamp(seconds_ago),
        },
        's3_probe': {'status': 'OK', 'checked_at': timestamp()},
    }
    atomic_json(tmp_path / 'health.json', state)
    assert card(tmp_path, component)['status'] != 'OK'


@pytest.mark.parametrize('uploaded_at', [None, 'invalid', '2999-01-01T00:00:00+00:00'])
def test_s3_without_valid_upload_confirmation_cannot_be_green(tmp_path, uploaded_at):
    atomic_json(
        tmp_path / 'health.json',
        {
            's3': {
                'status': 'OK',
                'code': 'verified',
                'checked_at': timestamp(),
                'created_at': timestamp(10),
                'uploaded_at': uploaded_at,
            },
            's3_probe': {'status': 'OK', 'checked_at': timestamp()},
        },
    )
    assert card(tmp_path, 's3')['status'] != 'OK'


def test_cached_view_does_not_invoke_checks_and_does_not_render_raw_details(tmp_path, monkeypatch):
    private = 'synthetic-secret-fixture-do-not-render'
    atomic_json(
        tmp_path / 'health.json',
        {
            'application': {
                'status': 'CRITICAL',
                'code': private,
                'revision': private,
                'checked_at': timestamp(),
                'error': private,
                'token': private,
            },
            'local': {
                'status': 'CRITICAL',
                'code': private,
                'checked_at': timestamp(),
                'created_at': timestamp(),
                'path': private,
                'size': private,
            },
        },
    )
    monkeypatch.setattr('subprocess.run', lambda *a, **k: pytest.fail('cached view executed a command'))
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('cached view used the network'))
    monkeypatch.setattr('sqlite3.connect', lambda *a, **k: pytest.fail('cached view opened SQLite'))
    result = health.health_view(tmp_path)
    assert private not in json.dumps(result)
    assert card(tmp_path, 'application')['status'] == 'CRITICAL'
    assert 'МСК' in health.date_label(timestamp())


@pytest.mark.parametrize(
    'code,tested,status,message',
    [
        ('never_tested', None, 'UNKNOWN', 'NEVER TESTED'),
        ('restore_failed', None, 'CRITICAL', 'FAIL'),
        ('pass', 91 * 86400, 'WARNING', 'STALE'),
        ('pass', 60, 'OK', 'PASS'),
    ],
)
def test_restore_status_requires_a_real_test_timestamp(tmp_path, code, tested, status, message):
    update_component(
        tmp_path,
        'restore',
        {
            'status': 'OK',
            'code': code,
            'tested_at': timestamp(tested) if tested is not None else None,
            'scope': 'offsite',
            'steps': ['archives', 'references', 'isolated_start', 'http_smoke'],
        },
    )
    result = card(tmp_path, 'restore')
    assert result['status'] == status
    assert result['message'] == message


@pytest.mark.parametrize('integrity_at', [None, 'invalid', '2999-01-01T00:00:00+00:00'])
def test_sqlite_without_valid_integrity_timestamp_is_not_green(tmp_path, integrity_at):
    update_component(
        tmp_path, 'sqlite', {'status': 'OK', 'code': 'ok', 'available': True, 'integrity_at': integrity_at}
    )
    assert card(tmp_path, 'sqlite')['status'] != 'OK'


def test_synthetic_or_partial_restore_evidence_is_not_real_pass(tmp_path):
    update_component(
        tmp_path,
        'restore_synthetic',
        {'status': 'OK', 'code': 'synthetic_pass', 'tested_at': timestamp(), 'scope': 'synthetic'},
    )
    assert card(tmp_path, 'restore')['message'] == 'NEVER TESTED'
    update_component(
        tmp_path,
        'restore',
        {'status': 'OK', 'code': 'pass', 'tested_at': timestamp(), 'scope': 'offsite', 'steps': ['archives']},
    )
    assert card(tmp_path, 'restore')['status'] != 'OK'


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return json.dumps(self.payload).encode()


@pytest.mark.parametrize('failure', ['ok_false', 'timeout', 'malformed', 'http_500'])
def test_telegram_failures_retry_boundedly_without_leaking_token(failure, caplog):
    calls, sleeps = [], []

    def opener(request, timeout):
        calls.append(timeout)
        if failure == 'timeout':
            raise TimeoutError('synthetic-secret-token')
        if failure == 'http_500':
            raise urllib.error.HTTPError(request.full_url, 500, 'failed', None, io.BytesIO(b'{}'))
        if failure == 'malformed':
            response = FakeResponse({})
            response.read = lambda _: b'not-json'
            return response
        return FakeResponse({'ok': False})

    assert (
        telegram_alerts.send_message(
            'synthetic-secret-token',
            'synthetic-chat',
            'Synthetic alert',
            opener=opener,
            sleeper=sleeps.append,
            retries=2,
            timeout=50,
        )
        is False
    )
    assert calls == [10, 10, 10]
    assert len(sleeps) == 2
    assert 'synthetic-secret-token' not in caplog.text


@pytest.mark.parametrize('http_error', [False, True])
def test_telegram_rate_limit_then_success(http_error):
    attempts, sleeps = [], []

    def opener(request, timeout):
        attempts.append(request.data)
        if len(attempts) == 1:
            payload = {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 99}}
            if http_error:
                raise urllib.error.HTTPError(
                    request.full_url, 429, 'rate limit', None, io.BytesIO(json.dumps(payload).encode())
                )
            return FakeResponse(payload)
        return FakeResponse({'ok': True})

    assert (
        telegram_alerts.send_message(
            'synthetic-token', 'synthetic-chat', 'Synthetic alert', opener=opener, sleeper=sleeps.append
        )
        is True
    )
    assert len(attempts) == 2
    assert sleeps == [30]


@pytest.mark.parametrize(
    'enabled,token,chat_id', [(False, 'fixture', 'fixture'), (True, '', 'fixture'), (True, 'fixture', '')]
)
def test_unconfigured_telegram_never_sends(tmp_path, enabled, token, chat_id):
    telegram_alerts.notify(
        tmp_path,
        enabled=enabled,
        token=token,
        chat_id=chat_id,
        sender=lambda *a, **k: pytest.fail('unconfigured sender invoked'),
    )
    state = read_json(tmp_path / 'health.json')['alerts']
    assert state['status'] == 'UNKNOWN'
    assert state['code'] == 'disabled'


def incident_view(status='CRITICAL'):
    return {
        'cards': [
            {
                'key': 's3',
                'title': 'Копии в S3',
                'status': status,
                'message': 'S3 недоступен' if status != 'OK' else 'Проверка пройдена',
            }
        ]
    }


def age_attempts(state_dir, seconds=901):
    path = state_dir / 'alert-incidents.json'
    incidents = read_json(path)
    for incident in incidents.values():
        incident['attempted_at'] = timestamp(seconds)
    atomic_json(path, incidents)


def test_persistent_incident_dedup_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_alerts, 'health_view', lambda _: incident_view())
    messages = []

    def sender(token, chat_id, message, **kwargs):
        messages.append(message)
        return True

    options = {'enabled': True, 'token': 'synthetic-token', 'chat_id': 'synthetic-chat', 'sender': sender}
    telegram_alerts.notify(tmp_path, **options)
    telegram_alerts.notify(tmp_path, **options)
    age_attempts(tmp_path)
    telegram_alerts.notify(tmp_path, **options)
    assert len(messages) == 1
    monkeypatch.setattr(telegram_alerts, 'health_view', lambda _: incident_view('OK'))
    telegram_alerts.notify(tmp_path, **options)
    assert len(messages) == 2
    assert 'RECOVERED' in messages[-1]
    age_attempts(tmp_path)
    telegram_alerts.notify(tmp_path, **options)
    assert len(messages) == 2


def test_delivery_failure_is_pending_then_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_alerts, 'health_view', lambda _: incident_view())
    outcomes = iter([False, True])
    calls = []

    def sender(*args, **kwargs):
        calls.append(args)
        return next(outcomes)

    options = {'enabled': True, 'token': 'synthetic-token', 'chat_id': 'synthetic-chat', 'sender': sender}
    telegram_alerts.notify(tmp_path, **options)
    assert read_json(tmp_path / 'health.json')['alerts']['status'] == 'CRITICAL'
    assert 'sent_at' not in read_json(tmp_path / 'alert-incidents.json')['s3']
    telegram_alerts.notify(tmp_path, **options)
    assert len(calls) == 1
    age_attempts(tmp_path)
    telegram_alerts.notify(tmp_path, **options)
    assert len(calls) == 2
    assert read_json(tmp_path / 'health.json')['alerts']['status'] == 'OK'
    assert read_json(tmp_path / 'alert-incidents.json')['s3']['pending'] is False


def test_recovered_unsent_incident_cannot_leave_delivery_failed_forever(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_alerts, 'health_view', lambda _: incident_view())
    options = {'enabled': True, 'token': 'synthetic-token', 'chat_id': 'synthetic-chat'}
    telegram_alerts.notify(tmp_path, **options, sender=lambda *a, **k: False)
    age_attempts(tmp_path)
    monkeypatch.setattr(telegram_alerts, 'health_view', lambda _: incident_view('OK'))
    telegram_alerts.notify(tmp_path, **options, sender=lambda *a, **k: True)
    assert read_json(tmp_path / 'health.json')['alerts']['status'] != 'CRITICAL'


def test_only_two_reminders_are_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_alerts, 'health_view', lambda _: incident_view())
    messages = []

    def sender(*args, **kwargs):
        messages.append(args[2])
        return True

    options = {'enabled': True, 'token': 'synthetic-token', 'chat_id': 'synthetic-chat', 'sender': sender}
    telegram_alerts.notify(tmp_path, **options)
    for _ in range(4):
        path = tmp_path / 'alert-incidents.json'
        incidents = read_json(path)
        incidents['s3'].update(sent_at=timestamp(25 * 3600), attempted_at=timestamp(25 * 3600))
        atomic_json(path, incidents)
        telegram_alerts.notify(tmp_path, **options)
    assert len(messages) == 3


@pytest.mark.parametrize(
    'value',
    [
        None,
        [],
        {'status': []},
        {'status': 'OK', 'code': {}, 'checked_at': timestamp()},
        {
            'status': 'OK',
            'code': 'pass',
            'scope': 'offsite',
            'checked_at': timestamp(),
            'tested_at': timestamp(),
            'steps': 7,
        },
    ],
)
def test_malformed_cache_projection_never_raises_or_turns_green(tmp_path, value):
    atomic_json(tmp_path / 'health.json', {key: value for key in health.TITLES})
    assert all(item['status'] != 'OK' for item in health.health_view(tmp_path)['cards'])


def test_collector_continues_when_sanic_and_s3_are_down(tmp_path, monkeypatch):
    import sqlite3
    from scripts import health_check

    db = tmp_path / 'synthetic.sqlite3'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE synthetic (id INTEGER)')

    def unavailable(*args, **kwargs):
        raise OSError('synthetic-secret-not-to-log')

    monkeypatch.setattr(health_check.urllib.request, 'urlopen', unavailable)
    monkeypatch.setattr(health_check, 'rclone', unavailable)
    state_dir = tmp_path / 'ops'
    health_check.collect(db, tmp_path, state_dir, remote='synthetic-crypt:fixture')
    state = read_json(state_dir / 'health.json')
    assert state['application']['status'] == 'CRITICAL'
    assert state['s3_probe']['status'] == 'CRITICAL'
    assert state['sqlite']['status'] == 'OK'
    assert state['disk']['status'] == 'OK'
    assert state['restore']['code'] == 'never_tested'
    assert 'synthetic-secret-not-to-log' not in json.dumps(state)


def test_failed_systemd_unit_is_detected_even_with_fresh_previous_success(tmp_path, monkeypatch):
    import subprocess
    from scripts import health_check

    update_component(tmp_path, 'local', {'status': 'OK', 'code': 'verified', 'created_at': timestamp()})

    def run(arguments, **kwargs):
        return subprocess.CompletedProcess(
            arguments, 0, b'exit-code\n' if 'competitions-backup.service' in arguments else b'success\n', b''
        )

    monkeypatch.setattr(health_check.subprocess, 'run', run)
    health_check.check_systemd(tmp_path)
    assert card(tmp_path, 'local')['status'] == 'CRITICAL'
    assert read_json(tmp_path / 'health.json')['local']['created_at']


@pytest.mark.parametrize('seconds_ago,expected', [(60, 'UNKNOWN'), (31 * 60, 'WARNING')])
def test_backup_in_progress_has_grace_before_alert(tmp_path, seconds_ago, expected):
    atomic_json(
        tmp_path / 'health.json',
        {
            'local': {
                'status': 'WARNING',
                'code': 'backup_started',
                'checked_at': timestamp(seconds_ago),
                'created_at': timestamp(3600),
            }
        },
    )
    assert card(tmp_path, 'local')['status'] == expected


@pytest.mark.parametrize(
    'probe',
    [
        {'status': 'UNKNOWN', 'code': 'ok'},
        {'status': 'WARNING', 'code': 'ok'},
        {'status': [], 'code': 'ok'},
        {'status': 'OK'},
        {'status': 'OK', 'code': []},
        {'status': 'OK', 'code': 'unavailable'},
        [],
        None,
    ],
)
def test_s3_requires_positive_well_typed_availability_probe(tmp_path, probe):
    update_component(
        tmp_path, 's3', {'status': 'OK', 'code': 'verified', 'created_at': timestamp(), 'uploaded_at': timestamp()}
    )
    if isinstance(probe, dict):
        probe = dict(probe, checked_at=timestamp())
    state = read_json(tmp_path / 'health.json')
    state['s3_probe'] = probe
    atomic_json(tmp_path / 'health.json', state)
    assert card(tmp_path, 's3')['status'] != 'OK'
    update_component(tmp_path, 's3_probe', {'status': 'OK', 'code': 'ok'})
    assert card(tmp_path, 's3')['status'] == 'OK'


@pytest.mark.parametrize('http_error', [False, True])
@pytest.mark.parametrize(
    'payload',
    [
        None,
        [],
        'synthetic-invalid-response',
        7,
        {'ok': False, 'error_code': 429, 'parameters': []},
        {'ok': False, 'error_code': 429, 'parameters': None},
        {'ok': False, 'error_code': 429, 'parameters': 'invalid'},
        *[
            {'ok': False, 'error_code': 429, 'parameters': {'retry_after': value}}
            for value in ([], {}, None, 'invalid', float('inf'), True, -999, 999)
        ],
    ],
)
def test_malformed_telegram_rate_limit_is_bounded_and_persists_failure(
    tmp_path, monkeypatch, caplog, payload, http_error
):
    calls, sleeps = [], []

    def opener(request, timeout):
        calls.append(timeout)
        if http_error:
            raise urllib.error.HTTPError(
                request.full_url, 429, 'rate limit', None, io.BytesIO(json.dumps(payload).encode())
            )
        return FakeResponse(payload)

    def sender(token, chat_id, message, **kwargs):
        return telegram_alerts.send_message(token, chat_id, message, opener=opener, sleeper=sleeps.append, **kwargs)

    update_component(tmp_path, 'local', {'status': 'CRITICAL', 'code': 'backup_failed'})
    telegram_alerts.notify(
        tmp_path,
        enabled=True,
        token='synthetic-token-private',
        chat_id='synthetic-chat',
        retries=2,
        timeout=5,
        sender=sender,
    )
    assert calls == [5, 5, 5]
    assert len(sleeps) == 2 and all(1 <= delay <= 30 for delay in sleeps)
    state = read_json(tmp_path / 'health.json')
    assert state['alerts']['code'] == 'delivery_failed'
    assert 'last_sent_at' not in state['alerts']
    assert read_json(tmp_path / 'alert-incidents.json')['local']['pending'] is True
    assert 'synthetic-token-private' not in caplog.text
    assert 'synthetic-token-private' not in json.dumps(state)
