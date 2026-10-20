"""Synthetic regression tests for the real-HTTP scenario infrastructure."""
import asyncio
import gc
import os
import socket
import sys
import weakref
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sanic import json
from sanic import redirect
from sanic import Sanic
from sanic import text

from src.http_scenarios import fresh_application
from src.http_scenarios import isolated_openapi
from src.http_scenarios import ScenarioClient


class CollectableMarker:
    """Sanic uses slots without weakrefs; follow a marker owned by its ctx."""


@pytest.fixture
def demo_app():
    with isolated_openapi():
        yield from make_demo_app()


def make_demo_app():
    app = Sanic('fictional_scenario_' + uuid4().hex)
    app.ctx.starts = 0
    app.ctx.stops = 0
    app.ctx.requests = 0
    app.ctx.storage = 'initial'

    @app.before_server_start
    async def before_start(application):
        application.ctx.starts += 1
        application.ctx.storage_at_start = application.ctx.storage

    @app.after_server_stop
    async def after_stop(application):
        application.ctx.stops += 1

    @app.on_request
    async def observe(request):
        request.app.ctx.requests += 1
        if request.path == '/blocked':
            return text('blocked', status=403)

    @app.get('/echo')
    async def echo(request):
        return json({'cookie': request.cookies.get('fictional'), 'storage': request.app.ctx.storage})

    @app.get('/redirect')
    async def redirected(request):
        return redirect('/echo')

    @app.get('/set-cookie')
    async def set_cookie(request):
        response = redirect('/echo')
        response.add_cookie('fictional', 'synthetic-value', secure=False)
        return response

    try:
        yield app
    finally:
        Sanic.unregister_app(app)


def test_scenario_starts_lazily_once_after_storage_replacement(demo_app):
    client = ScenarioClient(demo_app)
    assert demo_app.ctx.starts == 0
    demo_app.ctx.storage = 'synthetic-final'
    try:
        for _ in range(3):
            request, response = client.get('/echo')
            assert request.app is demo_app
            assert response.status == 200
            assert response.json['storage'] == 'synthetic-final'
        assert demo_app.ctx.starts == 1
        assert demo_app.ctx.requests == 3
        assert demo_app.ctx.storage_at_start == 'synthetic-final'
    finally:
        client.close()
    assert demo_app.ctx.stops == 1


def test_scenario_preserves_redirect_and_first_request_contract(demo_app):
    client = ScenarioClient(demo_app)
    try:
        request, response = client.get('/redirect')
        assert request.path == '/redirect'
        assert response.status == 200
        assert response.url.path == '/echo'
        request, response = client.get('/redirect', allow_redirects=False)
        assert request.path == '/redirect'
        assert response.status == 302
        assert response.headers['location'] == '/echo'
        request, response = client.get('/echo', gather_request=False)
        assert request is None
        assert response.status == 200
        request, response = client.get('/blocked')
        assert request.path == '/blocked'
        assert response.status == 403
    finally:
        client.close()


def test_scenario_cookies_persist_only_within_one_request_redirect_chain(demo_app):
    client = ScenarioClient(demo_app)
    try:
        request, response = client.get('/set-cookie')
        assert request.path == '/set-cookie'
        assert response.json['cookie'] == 'synthetic-value'
        _, response = client.get('/echo')
        assert response.json['cookie'] is None
        _, response = client.get('/echo', cookies={'fictional': 'explicit-synthetic-value'})
        assert response.json['cookie'] == 'explicit-synthetic-value'
        _, response = client.get('/echo')
        assert response.json['cookie'] is None
    finally:
        client.close()


def test_scenario_request_history_does_not_accumulate_routes_or_middleware(demo_app):
    client = ScenarioClient(demo_app)
    try:
        client.get('/echo')
        signals = len(demo_app.signal_router.routes)
        middleware = tuple(len(route.extra.request_middleware) for route in demo_app.router.routes)
        for _ in range(40):
            _, response = client.get('/echo')
            assert response.status == 200
        assert len(demo_app.signal_router.routes) == signals
        assert tuple(len(route.extra.request_middleware) for route in demo_app.router.routes) == middleware
        assert demo_app.ctx.starts == 1
    finally:
        client.close()


def test_scenario_releases_port_loop_and_test_mode(demo_app):
    previous_test_mode = Sanic.test_mode
    previous_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(previous_loop)
    client = ScenarioClient(demo_app)
    try:
        client.get('/echo')
        loop = client._loop
        port = client.port
        assert Sanic.test_mode is True
        client.close()
        assert loop.is_closed()
        assert asyncio.get_event_loop() is previous_loop
        assert Sanic.test_mode is previous_test_mode
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', port)) != 0
        client.close()
        with pytest.raises(RuntimeError, match='already been closed'):
            client.get('/echo')
    finally:
        client.close()
        previous_loop.close()
        asyncio.set_event_loop(None)


def test_fresh_production_assembly_repeated_startup_uses_default_recursion_limit():
    from src import main

    original = main.app
    old_limit = sys.getrecursionlimit()
    previous_test_mode = Sanic.test_mode
    sys.setrecursionlimit(1000)
    counts = []
    references = []
    try:
        for _ in range(12):
            with fresh_application() as application:
                assert application is not original
                assert Sanic.get_app(original.name) is application
                storage = Mock()
                storage.get_level_names.return_value = ['Синтетический уровень']
                storage.get_user.return_value = {'id': 1, 'role': 'admin'}
                application.ctx.storage = storage
                application.ctx.collectable_marker = CollectableMarker()
                references.append(weakref.ref(application.ctx.collectable_marker))
                client = ScenarioClient(application)
                try:
                    _, response = client.get('/healthcheck')
                    assert response.status == 200
                    counts.append(len(application.signal_router.routes))
                finally:
                    client.close()
            assert Sanic.get_app(original.name) is original
            del application, client, storage, _, response
            gc.collect()
            assert all(reference() is None for reference in references)
        assert len(set(counts)) == 1
        assert Sanic.test_mode is previous_test_mode
    finally:
        sys.setrecursionlimit(old_limit)


def test_scenario_restores_absent_current_event_loop(demo_app):
    asyncio.set_event_loop(None)
    client = ScenarioClient(demo_app)
    try:
        client.get('/echo')
    finally:
        client.close()
    with pytest.raises(RuntimeError, match='no current event loop'):
        asyncio.get_event_loop()


def test_fresh_application_restores_extension_metadata_after_failure():
    from sanic_ext.extensions.openapi.builders import OperationStore
    from sanic_ext.extensions.openapi.builders import SpecificationBuilder
    from src import main

    previous_store = OperationStore()
    previous_specification = SpecificationBuilder()
    contents = dict(previous_store)
    original = main.app
    with pytest.raises(RuntimeError, match='synthetic scenario failure'):
        with fresh_application() as application:
            assert OperationStore() is not previous_store
            assert SpecificationBuilder() is not previous_specification
            assert Sanic.get_app(original.name) is application
            raise RuntimeError('synthetic scenario failure')
    assert OperationStore._singleton is previous_store
    assert dict(previous_store) == contents
    assert SpecificationBuilder() is previous_specification
    assert Sanic.get_app(original.name) is original


def test_failed_startup_releases_owned_loop_socket_and_test_mode(demo_app):
    previous_test_mode = Sanic.test_mode

    @demo_app.before_server_start
    async def fail_startup(application):
        raise RuntimeError('synthetic startup failure')

    client = ScenarioClient(demo_app)
    with pytest.raises(RuntimeError, match='synthetic startup failure'):
        client.get('/echo')
    assert client._loop.is_closed()
    assert client._socket.fileno() == -1
    assert Sanic.test_mode is previous_test_mode


@pytest.mark.parametrize('marker', ['synthetic-first', 'synthetic-second'])
def test_http_fixture_keeps_context_and_mutable_sessions_local(http_client, marker):
    from src import auth
    from src import main
    from src import main_test
    from src.routes.auth import login_failures
    from src.routes.calendar import event_import_sessions
    from src.routes.students import student_import_sessions

    assert main.app is http_client.app
    assert main_test.app is http_client.app
    assert not hasattr(http_client.app.ctx, 'scenario_marker')
    http_client.app.ctx.scenario_marker = marker
    for state in (login_failures, event_import_sessions, student_import_sessions, auth._last_seen_touch):
        assert not state
        state[marker] = {'synthetic': True}


def handler_origin(handler):
    while hasattr(handler, 'func'):
        handler = handler.func
    return handler.__module__, handler.__qualname__, getattr(handler, '__code__', None)


def application_contract(application):
    from sanic.signals import _blank

    def middleware_contract(middleware):
        return tuple((handler_origin(item), item.priority) for item in middleware)

    real_signals = []
    for signal in application.signal_router.routes:
        if signal.name == 'sanic.__signal__.__init__':
            assert signal.handler is _blank
            assert signal.handler.__module__ == 'sanic.signals'
            assert signal.handler.__code__ is _blank.__code__
            assert signal.priority == 0
            assert signal.extra.requirements is None
        else:
            listener = getattr(signal.handler, 'keywords', {}).get('listener', signal.handler)
            real_signals.append(
                (
                    signal.name,
                    signal.priority,
                    handler_origin(listener),
                    signal.ctx.definition,
                    signal.ctx.trigger,
                    signal.ctx.exclusive,
                    signal.extra.requirements,
                )
            )

    return {
        'config': dict(application.config),
        'routes': sorted(
            (
                route.name,
                route.uri,
                tuple(sorted(route.methods)),
                tuple(sorted(route.requirements.items())),
                route.strict,
                route.unquote,
                handler_origin(route.handler),
                middleware_contract(route.extra.request_middleware),
                middleware_contract(route.extra.response_middleware),
            )
            for route in application.router.routes
        ),
        'listeners': [
            (future.event, future.priority, handler_origin(future.listener)) for future in application._future_listeners
        ],
        'signals': real_signals,
    }


def production_http_contract(prepare_methods, cors_origin):
    from src import main_test as helpers

    previous_test_mode = Sanic.test_mode
    with fresh_application() as application:
        application.config.CORS_ORIGINS = cors_origin
        storage = Mock()
        storage.get_level_names.return_value = ['Синтетический уровень']
        storage.get_user.side_effect = helpers.fake_get_user
        storage.get_profile.return_value = {}
        application.ctx.storage = storage

        async def explicit_get(request):
            return text('synthetic GET')

        async def explicit_head(request):
            return text('synthetic HEAD', status=202, headers={'X-Synthetic-Head': 'preserved'})

        async def explicit_options(request):
            return text('synthetic OPTIONS', status=203, headers={'Allow': 'GET,HEAD,OPTIONS'})

        application.add_route(explicit_get, '/synthetic-explicit', methods=['GET'], strict_slashes=True, unquote=True)
        application.add_route(explicit_head, '/synthetic-explicit', methods=['HEAD'], strict_slashes=True, unquote=True)
        application.add_route(
            explicit_options, '/synthetic-explicit', methods=['OPTIONS'], strict_slashes=True, unquote=True
        )
        application.add_route(
            explicit_get, '/synthetic-host', methods=['GET'], host='synthetic.example', name='host_get'
        )
        client = ScenarioClient(application, prepare_methods=prepare_methods)
        application.ext
        automatic = next(
            future.listener
            for future in application._future_listeners
            if future.listener.__qualname__ == 'add_auto_handlers.<locals>._add_handlers'
        )
        callbacks = []
        previous_profile = sys.getprofile()

        def observe_original_callback(frame, event, argument):
            if event == 'call' and frame.f_code is automatic.__code__:
                callbacks.append(frame.f_locals['app'])

        try:
            sys.setprofile(observe_original_callback)
            _, response = client.get('/healthcheck')
        finally:
            sys.setprofile(previous_profile)
        try:
            assert response.status == 200
            assert callbacks == [application] * (2 if prepare_methods else 1)
            assert any(future.listener is automatic for future in application._future_listeners)
            contract = application_contract(application)
            headers = helpers.get_auth_headers()
            observations = []
            for method, path, request_headers, data in (
                ('head', '/healthcheck', {}, None),
                ('options', '/healthcheck', {}, None),
                ('head', '/synthetic-explicit', headers, None),
                ('options', '/synthetic-explicit', headers, None),
                ('head', '/synthetic-explicit', {}, None),
                ('options', '/synthetic-explicit', {}, None),
                ('get', '/api/profile', headers, None),
                ('post', '/api/profile', headers, {}),
                (
                    'options',
                    '/healthcheck',
                    {
                        'Origin': 'https://synthetic.example',
                        'Access-Control-Request-Method': 'GET',
                    },
                    None,
                ),
            ):
                kwargs = {'headers': request_headers, 'allow_redirects': False}
                if data is not None:
                    kwargs['data'] = data
                _, result = getattr(client, method)(path, **kwargs)
                observations.append(
                    (
                        result.status,
                        result.body,
                        tuple(sorted(value.strip() for value in result.headers.get('Allow', '').split(',') if value)),
                        result.headers.get('X-Synthetic-Head'),
                        result.headers.get('Access-Control-Allow-Origin'),
                    )
                )
            assert [row[0] for row in observations[:8]] == [200, 204, 202, 203, 401, 401, 200, 403]
            assert observations[2][3] == 'preserved'
            assert observations[8][4] == (cors_origin or None)
            _, specification = client.get('/docs/openapi.json', headers=headers)
            assert specification.status == 200
            assert specification.json['paths']
            noops = sum(signal.name == 'sanic.__signal__.__init__' for signal in application.signal_router.routes)
            return contract, observations, specification.json, noops
        finally:
            client.close()
            assert client._loop.is_closed()
            assert client._socket.fileno() == -1
            assert Sanic.test_mode is previous_test_mode


@pytest.mark.parametrize('cors_origin', ['', 'https://synthetic.example'])
def test_prepared_and_canonical_production_startup_have_identical_http_contract(monkeypatch, cors_origin):
    from src import auth

    monkeypatch.setattr(auth, '_last_seen_touch', {})
    canonical = production_http_contract(False, cors_origin)
    prepared = production_http_contract(True, cors_origin)
    assert prepared[:3] == canonical[:3]
    assert canonical[3] >= prepared[3] >= 1


@pytest.mark.parametrize('flag', ['HTTP_AUTO_HEAD', 'HTTP_AUTO_OPTIONS', 'OAS_IGNORE_HEAD', 'OAS_IGNORE_OPTIONS'])
def test_automatic_method_preparation_rejects_changed_flags(demo_app, flag):
    from src.http_scenarios import prepare_auto_http_handlers

    demo_app.config[flag] = False
    with pytest.raises(RuntimeError, match='default HTTP/OpenAPI flags'):
        prepare_auto_http_handlers(demo_app)


@pytest.mark.parametrize('registration', ['missing', 'duplicate'])
def test_automatic_method_preparation_rejects_unrecognized_registration(demo_app, monkeypatch, registration):
    from src.http_scenarios import prepare_auto_http_handlers

    demo_app.ext
    original = list(demo_app._future_listeners)
    automatic = [
        future for future in original if future.listener.__qualname__ == 'add_auto_handlers.<locals>._add_handlers'
    ]
    assert len(automatic) == 1
    changed = (
        [future for future in original if future not in automatic]
        if registration == 'missing'
        else original + automatic
    )
    monkeypatch.setattr(demo_app, '_future_listeners', changed)
    with pytest.raises(RuntimeError, match='exactly one original'):
        prepare_auto_http_handlers(demo_app)


def test_automatic_method_preparation_rejects_version_drift_and_releases_resources(demo_app, monkeypatch):
    from src import http_scenarios

    previous_test_mode = Sanic.test_mode
    monkeypatch.setattr(http_scenarios, 'version', lambda package: 'synthetic-unverified-version')
    client = ScenarioClient(demo_app)
    with pytest.raises(RuntimeError, match='verified pinned'):
        client.get('/echo')
    assert client._loop.is_closed()
    assert client._socket.fileno() == -1
    assert Sanic.test_mode is previous_test_mode


@pytest.mark.parametrize('previous', [None, 'synthetic-existing-value'])
@pytest.mark.parametrize('failure', [False, True])
def test_fresh_application_restores_framework_owned_log_environment(monkeypatch, previous, failure):
    if previous is None:
        monkeypatch.delenv('SANIC_LOG_EXTRA', raising=False)
    else:
        monkeypatch.setenv('SANIC_LOG_EXTRA', previous)

    def scenario():
        with fresh_application():
            os.environ['SANIC_LOG_EXTRA'] = 'synthetic-framework-mutation'
            if failure:
                raise RuntimeError('synthetic owned-environment failure')

    if failure:
        with pytest.raises(RuntimeError, match='synthetic owned-environment failure'):
            scenario()
    else:
        scenario()
    assert os.environ.get('SANIC_LOG_EXTRA') == previous
