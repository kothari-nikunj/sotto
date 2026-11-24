"""A knowledge read that hits its time bound says so, distinctly from any other failure."""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import runtime_knowledge_read as read

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('test_knowledge_read_timeout_receiver', HERE / 'receiver.py')
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)
REVISION = 'a' * 64
BODY = {'operation': 'knowledge_query', 'arguments': {'argv': ['--person', 'Dana Private']},
        'source_revision': REVISION}


def test_the_subprocess_time_bound_is_a_distinct_content_free_reason(monkeypatch):
    def slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(read.subprocess, 'run', slow)
    with pytest.raises(read.TimedOutError) as caught:
        read.perform(BODY, script='knowledge_query.py', executable=sys.executable, env={},
                     authority=lambda: {'complete': True, 'revision': REVISION})
    assert isinstance(caught.value, read.UnavailableError)
    assert str(caught.value) == 'knowledge query timed out'
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize('failure,reason', [
    (lambda: rec.RUNTIME_KNOWLEDGE_READ.TimedOutError('knowledge query timed out'), 'knowledge read timed out'),
    (lambda: rec.RUNTIME_KNOWLEDGE_READ.UnavailableError('knowledge query unavailable'), 'knowledge read unavailable'),
    (lambda: OSError('Dana Private'), 'knowledge read unavailable'),
])
def test_the_receiver_names_a_timeout_and_nothing_else(monkeypatch, failure, reason):
    def perform(*args, **kwargs):
        raise failure()
    monkeypatch.setattr(rec.RUNTIME_KNOWLEDGE_READ, 'perform', perform)
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *parts: 'knowledge_query.py')
    raw, sent = json.dumps(BODY).encode(), []
    fake = SimpleNamespace(
        path='/runtime/knowledge-read', client_address=('127.0.0.1', 1234),
        headers={'Content-Length': str(len(raw)), 'Content-Type': 'application/json'},
        rfile=io.BytesIO(raw), _authed=lambda token: token == rec.MCP_TOKEN,
        _send=lambda status, payload: sent.append((status, payload)))
    rec.Handler.do_POST(fake)
    assert sent == [(503, {'error': reason})]
