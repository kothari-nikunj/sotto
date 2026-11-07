"""The receiver retention call is local, authenticated, and never a new timer."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location('runtime_retention_test', Path(__file__).with_name('runtime_retention.py'))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, _limit):
        return self.body


def test_posts_only_to_explicit_loopback_with_private_bearer_and_returns_counts():
    seen = []
    result = {'replies': 1, 'dispatches': 2, 'actions': 3, 'runs': 4, 'events': 5}
    opener = SimpleNamespace(open=lambda request, timeout: (
        seen.append((request, timeout)) or Response(json.dumps(result).encode())))
    cutoffs = {'delivery_before': '2026-06-01T00:00:00Z', 'action_before': '2026-03-01T00:00:00Z'}
    assert runtime.prune('http://127.0.0.1:8789', 'x' * 32, cutoffs, opener=opener) == result
    request, timeout = seen[0]
    assert request.full_url == 'http://127.0.0.1:8789/v1/retention'
    assert request.get_method() == 'POST' and timeout == 20
    assert request.get_header('Authorization') == 'Bearer ' + 'x' * 32
    assert json.loads(request.data) == cutoffs


@pytest.mark.parametrize('url', [
    'https://127.0.0.1:8789', 'http://localhost:8789', 'http://127.0.0.1',
    'http://user@127.0.0.1:8789', 'http://127.0.0.1:8789/path',
    'http://127.0.0.1:8789?token=x',
])
def test_non_loopback_or_ambiguous_url_never_contacts_service(url):
    opener = SimpleNamespace(open=lambda *_a, **_k: pytest.fail('network attempted'))
    with pytest.raises(runtime.RetentionCallError):
        runtime.prune(url, 'x' * 32, {}, opener=opener)


def test_accepts_the_exact_node_response_shape_including_sdk_expired():
    # application.ts returns the five journal counts plus `sdkExpired`; rejecting it made every
    # nightly sweep record a retention error.
    result = {'replies': 0, 'dispatches': 1, 'actions': 0, 'runs': 2, 'events': 0, 'sdkExpired': 3}
    opener = SimpleNamespace(open=lambda *_a, **_k: Response(json.dumps(result).encode()))
    cutoffs = {'delivery_before': '2026-06-01T00:00:00Z', 'action_before': '2026-03-01T00:00:00Z'}
    assert runtime.prune('http://127.0.0.1:8789', 'x' * 32, cutoffs, opener=opener) == result
    for bad in ({**result, 'sdkExpired': -1}, {**result, 'sdkExpired': True}, {**result, 'other': 0}):
        opener = SimpleNamespace(open=lambda *_a, body=bad, **_k: Response(json.dumps(body).encode()))
        with pytest.raises(runtime.RetentionCallError):
            runtime.prune('http://127.0.0.1:8789', 'x' * 32, cutoffs, opener=opener)


def test_missing_ack_or_malformed_counts_is_an_error_without_secret_echo():
    cutoffs = {'delivery_before': '2026-06-01T00:00:00Z', 'action_before': '2026-03-01T00:00:00Z'}
    for body in (b'not-json', b'{}', b'{"replies":true}', b'x' * 4097):
        opener = SimpleNamespace(open=lambda *_a, **_k: Response(body))
        with pytest.raises(runtime.RetentionCallError) as error:
            runtime.prune('http://127.0.0.1:8789', 'x' * 32, cutoffs, opener=opener)
        assert 'x' * 32 not in str(error.value)


def test_receiver_hook_prunes_through_the_runtime_without_creating_another_timer(monkeypatch):
    receiver_spec = importlib.util.spec_from_file_location('receiver_retention_test', Path(__file__).with_name('receiver.py'))
    receiver = importlib.util.module_from_spec(receiver_spec)
    receiver_spec.loader.exec_module(receiver)
    cutoffs = {'delivery_before': '2026-06-01T00:00:00Z', 'action_before': '2026-03-01T00:00:00Z'}
    seen = []
    monkeypatch.setattr(receiver.RUNTIME_RETENTION, 'prune', lambda *args: (seen.append(args) or {}))
    monkeypatch.setenv('SOTTO_CHAT_RUNTIME_URL', 'http://127.0.0.1:8789')
    monkeypatch.setenv('SOTTO_CHAT_RUNTIME_TOKEN', 't' * 32)
    assert receiver.RETENTION.HOOKS['runtime_prune'](cutoffs) == {}
    assert seen == [('http://127.0.0.1:8789', 't' * 32, cutoffs)]
