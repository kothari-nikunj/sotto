import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('hermes_send', Path(__file__).with_name('send.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('receipt,accepted', [
    ({'success': True, 'message_id': 'provider-123'}, True),
    ({'success': True, 'skipped': True, 'message_id': 'provider-123'}, False),
    ({'success': True}, False), ({'error': 'offline'}, False),
    ({'success': True, 'message_id': ''}, False), ([], False),
])
def test_acceptance_requires_unskipped_provider_receipt(monkeypatch, receipt, accepted):
    def run(argv, **kw):
        assert argv == ['hermes', 'send', '--to', 'photon', '--json', '-f', '-']
        assert kw['input'] == 'body'
        return SimpleNamespace(returncode=0, stdout=json.dumps(receipt), stderr='')
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert module.send('body', 'photon')[0] is accepted


def test_zero_exit_with_unstructured_output_is_not_accepted(monkeypatch):
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout='Skipped', stderr=''))
    assert module.send('body', 'photon')[0] is False


def test_capability_requires_structured_send_flag_without_sending():
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='usage: hermes send', stderr='')
    assert module.send_capability(run) == (False, module.CAPABILITY_ERROR)
    assert calls == [['hermes', 'send', '--help']]


def test_unsupported_json_is_not_attempted_or_retried_as_plain_send(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=2, stdout='', stderr='unrecognized arguments: --json')
    monkeypatch.setattr(module.subprocess, 'run', run)
    ok, detail, receipt = module.send_receipt('body', 'photon')
    assert not ok and detail == module.CAPABILITY_ERROR
    assert receipt['acceptance'] == 'not_attempted'
    assert calls == [['hermes', 'send', '--to', 'photon', '--json', '-f', '-']]
