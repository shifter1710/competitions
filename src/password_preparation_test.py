"""Fixture caching must retain real crypto and honor patched preparation inputs."""
import pytest

from src import auth
from src import main_test as helpers
from src.settings import settings


@pytest.fixture(autouse=True)
def empty_preparation_cache():
    helpers.prepared_password_hash.cache_clear()
    yield
    helpers.prepared_password_hash.cache_clear()


def test_synthetic_preparation_reuses_hash_but_returns_independent_user_data():
    first = helpers.fake_get_user(settings.auth_admin_username)
    second = helpers.fake_get_user(settings.auth_admin_username)
    assert first is not second
    assert first['password_hash'] == second['password_hash']
    assert auth.verify_password(settings.auth_admin_password, first['password_hash'])
    assert not auth.verify_password('synthetic-definitely-wrong-password', first['password_hash'])
    first['active'] = 0
    assert helpers.fake_get_user(settings.auth_admin_username)['active'] == 1
    assert helpers.prepared_password_hash.cache_info().misses == 1


def test_direct_crypto_hashes_keep_random_salts_and_real_verification():
    first = auth.hash_password('synthetic-direct-crypto-password')
    second = auth.hash_password('synthetic-direct-crypto-password')
    assert first != second
    assert auth.verify_password('synthetic-direct-crypto-password', first)
    assert auth.verify_password('synthetic-direct-crypto-password', second)
    assert not auth.verify_password('synthetic-wrong-password', second)
    assert helpers.prepared_password_hash.cache_info().currsize == 0


@pytest.mark.parametrize('parameter', ['SCRYPT_N', 'SCRYPT_R', 'SCRYPT_P', 'SCRYPT_DKLEN'])
def test_preparation_cache_includes_each_hashing_parameter(monkeypatch, parameter):
    calls = []

    def synthetic_hash(password):
        parameters = tuple(getattr(auth, key) for key in ('SCRYPT_N', 'SCRYPT_R', 'SCRYPT_P', 'SCRYPT_DKLEN'))
        calls.append((password, parameters))
        return repr(calls[-1])

    monkeypatch.setattr(helpers, 'hash_password', synthetic_hash)
    first = helpers.fake_get_user(settings.auth_admin_username)['password_hash']
    assert helpers.fake_get_user(settings.auth_admin_username)['password_hash'] == first
    monkeypatch.setattr(auth, parameter, getattr(auth, parameter) + 1)
    second = helpers.fake_get_user(settings.auth_admin_username)['password_hash']
    assert second != first
    assert len(calls) == 2


def test_preparation_cache_includes_password_and_hash_callable(monkeypatch):
    monkeypatch.setattr(helpers, 'hash_password', lambda password: 'synthetic-first-' + password)
    first = helpers.fake_get_user(settings.auth_admin_username)['password_hash']
    monkeypatch.setattr(settings, 'auth_admin_password', 'synthetic-changed-password')
    second = helpers.fake_get_user(settings.auth_admin_username)['password_hash']
    assert second != first
    monkeypatch.setattr(helpers, 'hash_password', lambda password: 'synthetic-second-' + password)
    third = helpers.fake_get_user(settings.auth_admin_username)['password_hash']
    assert third != second
    assert helpers.prepared_password_hash.cache_info().misses == 3


def test_preparation_cache_observes_patched_scrypt_callable(monkeypatch):
    original = auth.hashlib.scrypt
    helpers.fake_get_user(settings.auth_admin_username)
    calls = []

    def observed_scrypt(*args, **kwargs):
        calls.append(kwargs['n'])
        return original(*args, **kwargs)

    monkeypatch.setattr(auth.hashlib, 'scrypt', observed_scrypt)
    user = helpers.fake_get_user(settings.auth_admin_username)
    assert calls == [auth.SCRYPT_N]
    assert auth.verify_password(settings.auth_admin_password, user['password_hash'])
    assert calls == [auth.SCRYPT_N, auth.SCRYPT_N]
