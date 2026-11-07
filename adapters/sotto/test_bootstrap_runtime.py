"""Bootstrap mechanics keep public health bytes and secret-free process inheritance."""
import io
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

import bootstrap_runtime as runtime
import selfhost_bootstrap as bootstrap

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "bootstrap_setup_contract", ROOT / "tools/setup_selfhost_railway.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
HEALTH_PROBE_SECONDS = 300


def test_clean_environment_removes_unrelated_values(monkeypatch):
    monkeypatch.setenv('UNRELATED_SECRET', 'private')
    monkeypatch.setenv('SOTTO_DATA', '/private-tenant')
    monkeypatch.setattr(runtime.os, 'environ', dict(runtime.os.environ))
    runtime.clean_environment()
    assert runtime.os.environ == {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
                                  'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'}


@pytest.mark.parametrize('interrupted', [False, True])
def test_health_service_wire_and_cleanup(monkeypatch, interrupted):
    responses = []
    events = []
    timers = []

    class Server:
        def __init__(self, address, handler):
            assert address == ('0.0.0.0', 8080)
            self.handler = handler

        def shutdown(self):
            events.append('shutdown')

        def server_close(self):
            events.append('closed')

        def serve_forever(self):
            assert self.daemon_threads is True
            for path in ('/health', '/health?private=1', '/other'):
                status, headers = [], []
                request = SimpleNamespace(path=path, wfile=io.BytesIO(), send_response=status.append,
                    send_header=lambda key, value: headers.append((key, value)), end_headers=lambda: None)
                self.handler.do_GET(request)
                responses.append((status, headers, request.wfile.getvalue()))
            if interrupted:
                raise RuntimeError('fixture interrupted')
            timers[0].callback()

    class Timer:
        def __init__(self, seconds, callback):
            assert seconds == setup.PREPARED_LISTENER_SECONDS
            self.callback = callback
            timers.append(self)

        def start(self):
            assert self.daemon is True
            events.append('started')

        def cancel(self):
            events.append('cancelled')

    monkeypatch.setattr(runtime, 'ThreadingHTTPServer', Server)
    monkeypatch.setattr(runtime.threading, 'Timer', Timer)
    if interrupted:
        with pytest.raises(RuntimeError, match='fixture interrupted'):
            bootstrap._health_only()
    else:
        bootstrap._health_only()
    assert responses == [([200], [('Content-Type', 'text/plain'), ('Content-Length', '2')], b'OK'),
                         ([404], [('Content-Type', 'text/plain'), ('Content-Length', '0')], b''),
                         ([404], [('Content-Type', 'text/plain'), ('Content-Length', '0')], b'')]
    assert events == (['started', 'cancelled', 'closed'] if interrupted
                      else ['started', 'shutdown', 'cancelled', 'closed'])


def test_prepared_listener_window_matches_onboarding_doc():
    guide = ' '.join((ROOT / 'docs/MAINTAINER-RAILWAY-HELPER.md').read_text().split())
    assert f'**{setup.PREPARED_LISTENER_SECONDS // 60} minutes**' in guide
    assert setup.HEALTH_WAIT_SECONDS == HEALTH_PROBE_SECONDS and 'up to five minutes' in guide
    assert 'preparation_unconfirmed' in guide
    assert setup.PreparationUnconfirmedError.stage == 'preparation_unconfirmed'
