import importlib.util
import json
import math
from pathlib import Path

import pytest


def load():
    spec = importlib.util.spec_from_file_location('sotto_run_test', Path(__file__).with_name('run.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unattended_result_and_optional_usage_are_separate_from_delivery(tmp_path, monkeypatch, capsys):
    run = load()
    monkeypatch.setenv('SOTTO_UNATTENDED', '1')
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'receiver-job-1')
    calls = []
    class Client:
        def run(self, prompt, identifier):
            calls.append((prompt, identifier))
            return {'text': 'Prepared result', 'usage': {'inputTokens': 12, 'outputTokens': 3}}
    path = tmp_path / 'usage.json'
    assert run.main(['--usage-file', str(path), 'prepare'], client_factory=Client) == 0
    assert calls == [('prepare', 'receiver-job-1')]
    assert capsys.readouterr().out == 'Prepared result\n'
    assert json.loads(path.read_text()) == {'input_tokens': 12, 'output_tokens': 3}
    assert path.stat().st_mode & 0o077 == 0
    assert not list(tmp_path.glob('.usage-*'))
    # A missing accounting directory must not cause a successful result to be regenerated.
    assert run.main(['--usage-file', str(tmp_path / 'absent' / 'usage.json'), 'prepare'], client_factory=Client) == 0


@pytest.mark.parametrize('unattended,identity', [('', 'run'), ('0', 'run'), ('1', ''), ('1', '../run')])
def test_missing_authority_rejected_before_contact(unattended, identity, monkeypatch, capsys):
    run = load()
    monkeypatch.setenv('SOTTO_UNATTENDED', unattended)
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', identity)
    def forbidden():
        pytest.fail('must not contact runtime')
    assert run.main(['prepare'], client_factory=forbidden) == 2
    assert not capsys.readouterr().out


def test_failures_never_print_private_exception_or_partial_result(monkeypatch, capsys):
    run = load()
    monkeypatch.setenv('SOTTO_UNATTENDED', '1')
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'run')
    class Client:
        def run(self, *args):
            raise RuntimeError('private-token-and-source-body')
    assert run.main(['prepare'], client_factory=Client) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'Local unattended runtime failed\n'


def test_verified_capacity_refusal_uses_existing_unbilled_work_exit(monkeypatch, capsys):
    run = load()
    monkeypatch.setenv('SOTTO_UNATTENDED', '1')
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'receiver-job-1')
    busy = run._client_module().ModelAdmissionBusyError
    class Client:
        def run(self, *_args):
            raise busy('private proxy body')
    assert run.main(['prepare'], client_factory=Client) == 78
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'Local model admission busy\n'


def test_usage_is_bounded_optional_and_does_not_invent_cost(tmp_path):
    run = load()
    assert run.normalize_usage({'usage': {'inputTokens': {'total': 5}, 'outputTokens': 2}}) == {'input_tokens': 5, 'output_tokens': 2}
    assert run.normalize_usage({'usage': {'inputTokens': True, 'outputTokens': -1, 'cost': math.nan}}) is None
    assert run.normalize_usage({'usage': {'inputTokens': 10 ** 1000, 'outputTokens': math.inf}}) is None
    assert run.read_usage(None) is None
    path = tmp_path / 'usage.json'
    path.write_text('not-json')
    assert run.read_usage(path) is None
    path.write_text(' ' * 65537 + '{}')
    assert run.read_usage(path) is None
    path.write_text('{"input_tokens":8,"output_tokens":2}')
    assert run.read_usage(path) == {'input_tokens': 8, 'output_tokens': 2}
