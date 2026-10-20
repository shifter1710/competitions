"""Test-only real-HTTP scenarios; never imported by the application runtime."""
import asyncio
import os
import runpy
import socket
from contextlib import contextmanager
from importlib.metadata import version

from sanic import Sanic
from sanic.exceptions import MethodNotSupported
from sanic_ext.extensions.openapi.builders import OperationStore
from sanic_ext.extensions.openapi.builders import SpecificationBuilder
from sanic_testing.reusable import ReusableClient
from sanic_testing.testing import SanicTestClient


def prepare_auto_http_handlers(application):
    """Run the pinned extension's original idempotent route preparation early.

    Its normal startup listener remains registered and runs again. Before app
    startup, adding HEAD/OPTIONS avoids recompiling signals for each new route.
    This deliberately reads one private registration list, guarded by the
    installed versions, registered callback provenance and default flags.
    """
    expected_versions = {'sanic': '25.12.1', 'sanic-ext': '25.12.0', 'sanic-testing': '24.6.0'}
    if any(version(package) != expected for package, expected in expected_versions.items()):
        raise RuntimeError('HTTP preparation requires the verified pinned Sanic/testing/extension versions')
    if application.state.is_started:
        raise RuntimeError('HTTP preparation requires an app that has not started')
    expected_flags = {
        'HTTP_AUTO_HEAD': True,
        'HTTP_AUTO_OPTIONS': True,
        'HTTP_AUTO_TRACE': False,
        'OAS': True,
        'OAS_IGNORE_HEAD': True,
        'OAS_IGNORE_OPTIONS': True,
    }
    if any(application.config.get(key, expected) is not expected for key, expected in expected_flags.items()):
        raise RuntimeError('HTTP preparation requires the verified default HTTP/OpenAPI flags')
    application.ext
    if any(application.config.get(key) is not expected for key, expected in expected_flags.items()):
        raise RuntimeError('HTTP preparation requires the verified default HTTP/OpenAPI flags')
    listeners = [
        future.listener
        for future in application._future_listeners
        if future.event == 'before_server_start'
        and getattr(future.listener, '__module__', None) == 'sanic_ext.extensions.http.methods'
        and getattr(future.listener, '__qualname__', None) == 'add_auto_handlers.<locals>._add_handlers'
    ]
    if len(listeners) != 1:
        raise RuntimeError('HTTP preparation requires exactly one original automatic-method listener')
    listeners[0](application)


@contextmanager
def isolated_openapi():
    """Keep sanic-ext's two process-wide builders local to a scenario.

    Both installed builder classes offer reset(), but no restore API. Preserve
    precisely their singleton references so existing decorator metadata keeps
    its identity; never mutate an existing builder or a Sanic router.
    """
    builders = (OperationStore, SpecificationBuilder)
    previous = [builder._singleton for builder in builders]
    for builder in builders:
        builder.reset()
    try:
        yield
    finally:
        for builder, original in zip(builders, previous):
            builder.reset()
            builder._singleton = original


@contextmanager
def fresh_application():
    """Execute the real application assembly without reusing finalized routers."""
    from src import main

    registered = Sanic.get_app(main.app.name)
    previous_test_mode = Sanic.test_mode
    previous_log_extra = os.environ.get('SANIC_LOG_EXTRA')
    with isolated_openapi():
        Sanic.unregister_app(registered)
        try:
            yield runpy.run_path(main.__file__)['app']
        finally:
            try:
                Sanic.unregister_app(registered)
                Sanic.register_app(registered)
                Sanic.test_mode = previous_test_mode
            finally:
                # setup_logging may resolve Default by writing this one key.
                if previous_log_extra is None:
                    os.environ.pop('SANIC_LOG_EXTRA', None)
                else:
                    os.environ['SANIC_LOG_EXTRA'] = previous_log_extra


class ScenarioClient(ReusableClient):
    """One lazy server per test, with SanicTestClient's HTTP request semantics.

    The installed ReusableClient appends capture middleware for every request,
    leaving callbacks on finalized routes, and persists its HTTPX cookie jar.
    Capture once at registration and use the installed fresh-session transport
    instead. No Sanic router/signal collections are deleted or reset manually.
    """

    _local_request = SanicTestClient._local_request
    get_new_session = SanicTestClient.get_new_session
    _error_handler = SanicTestClient._error_handler

    def __init__(self, app, *, prepare_methods=True):
        self.app = app
        self.host = '127.0.0.1'
        self.port = None
        self._first_request = None
        self._gather_request = False
        self._initialized = False
        self._closed = False
        self._socket = None
        self._loop = None
        self._previous_loop = None
        self._previous_test_mode = Sanic.test_mode
        self._prepare_methods = prepare_methods
        self.app.on_request(self._capture_first_request, priority=10000)
        self.app.exception(MethodNotSupported)(self._error_handler)

    def _capture_first_request(self, request):
        if self._gather_request and self._first_request is None:
            self._first_request = request

    def _ensure_started(self):
        if self._closed:
            raise RuntimeError('HTTP scenario has already been closed')
        if self._initialized:
            return
        # Sanic's runner installs its loop even when one is supplied. Reading
        # the standard policy's existing slot avoids get_event_loop() creating
        # an unrelated loop merely to save it. Other policies use their public
        # accessor (the supported runtime uses the standard Python policy).
        policy = asyncio.get_event_loop_policy()
        if isinstance(policy, asyncio.DefaultEventLoopPolicy):
            self._previous_loop = policy._local._loop
        else:
            try:
                self._previous_loop = policy.get_event_loop()
            except RuntimeError:
                self._previous_loop = None
        self._loop = asyncio.new_event_loop()
        self._socket = socket.socket()
        self._socket.bind(('127.0.0.1', 0))
        port = self._socket.getsockname()[1]
        super().__init__(self.app, port=port, loop=self._loop, server_kwargs={'sock': self._socket})
        self._initialized = True
        try:
            if self._prepare_methods:
                prepare_auto_http_handlers(self.app)
            super().run()
        except BaseException:
            self.close()
            raise

    def _sanic_endpoint_test(self, method='get', uri='/', gather_request=True, *args, **kwargs):
        self._ensure_started()
        self._first_request = None
        self._gather_request = gather_request
        try:
            _, response = super()._sanic_endpoint_test(method, uri, False, *args, **kwargs)
            return self._first_request if gather_request else None, response
        finally:
            self._gather_request = False
            self._first_request = None

    def _close_loop(self):
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            _, unfinished = self._loop.run_until_complete(asyncio.wait(pending, timeout=2))
            if unfinished:
                raise RuntimeError('HTTP scenario tasks refused cancellation')
            # Retrieve exceptions so actual task errors cannot disappear silently.
            for task in pending:
                if not task.cancelled() and task.exception() is not None:
                    raise RuntimeError('HTTP scenario task failed during shutdown') from task.exception()
        self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        self._loop.run_until_complete(self._loop.shutdown_default_executor())
        if asyncio.all_tasks(self._loop):
            raise RuntimeError('HTTP scenario left pending event-loop tasks')

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._initialized:
                if self.app.state.is_started:
                    super().stop()
                else:
                    self._run(self._session.aclose())
                    self._session = None
        finally:
            try:
                if self._loop is not None:
                    try:
                        self._close_loop()
                    finally:
                        self._loop.close()
            finally:
                if self._socket is not None:
                    self._socket.close()
                if self._initialized:
                    self._server_co.close()
                if self._loop is not None:
                    asyncio.set_event_loop(self._previous_loop)
                Sanic.test_mode = self._previous_test_mode
                # These installed functools caches are shared by Router
                # instances and key on self, retaining otherwise closed apps.
                # Clear only the two transparent lookup caches, not routes,
                # signals, middleware or application data.
                self.app.router.get.cache_clear()
                self.app.router.find_route_by_view_name.cache_clear()
