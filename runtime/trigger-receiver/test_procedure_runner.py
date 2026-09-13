"""Shared procedures honor the real CLI shapes and stage effects without sending."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import procedure_runner

PACK = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'


@pytest.mark.parametrize('mode', ['managed', 'self-host'])
def test_real_digest_cli_completes_an_empty_window_without_provider_calls(tmp_path, mode):
    # Exercise the actual two-process boundary and argparse; a mocked subprocess
    # previously accepted the nonexistent --check flag and hid every digest failing.
    env = {**os.environ, 'SOTTO_DATA': str(tmp_path), 'SOTTO_DEPLOYMENT_MODE': mode,
           'SOTTO_DELIVERY_RUN_ID': 'd' * 32}
    result = subprocess.run([sys.executable, str(Path(procedure_runner.__file__)),
                             json.dumps({'pack': str(PACK), 'kind': 'digest'})],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'NO_NUDGES'
    assert (tmp_path / 'events/last_digest.txt').read_text().strip()
    assert not (tmp_path / 'events/outbox.json').exists()
    assert not list((tmp_path / 'events').glob('delivery-effects-*'))


def test_pulse_accepts_successful_gather_diagnostics_then_stages_source_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'b' * 32)
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    import source_context
    monkeypatch.setattr(source_context, 'read_local', lambda hours: {'imessage': [{'text': 'Synthetic'}]})
    calls = []
    def invoke(argv, **kwargs):
        calls.append(Path(argv[1]).name)
        if calls[-1] == 'gather_google.py':
            Path(argv[argv.index('--gmail-out') + 1]).write_text('[]')
            return SimpleNamespace(returncode=0, stdout='Saved 0 emails and 0 events\n', stderr='')
        return SimpleNamespace(returncode=0, stdout=json.dumps({'pulse_markdown': 'Your weekly relationships'}), stderr='')
    monkeypatch.setattr(procedure_runner.subprocess, 'run', invoke)
    assert procedure_runner.run({'pack': str(PACK), 'kind': 'pulse'}) == 'Your weekly relationships'
    assert calls == ['gather_google.py', 'relationship_pulse.py']
    doc = json.loads((tmp_path / ('events/delivery-effects-' + 'b' * 32 + '.json')).read_text())
    assert doc['effects'][0]['sources'] == ['imessage']


@pytest.mark.parametrize('result,expected', [
    ({'deliver': False}, 'NO_NUDGES'),
    ({'deliver': False, 'retryable': True}, None),
    ({'deliver': True, 'items': [{'sender': 'A', 'why': 'Unanswered invitation'}],
      'coverage_until': '2026-09-08T12:30:00Z',
      'effects': [{'kind': 'eligibility', 'source': 'imessage'}], 'valid_until': 2000000000},
     'Midday catch-up\n\nA: Unanswered invitation'),
])
def test_digest_silence_failure_and_acceptance_cutoff(result, expected, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'c' * 32)
    # digest_check prints its verdict and exits 75 (EX_TEMPFAIL) when the review is incomplete.
    monkeypatch.setattr(procedure_runner.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=75 if result.get('retryable') else 0,
                                        stdout=json.dumps(result), stderr=''))
    request = {'pack': str(PACK), 'kind': 'digest'}
    if expected is None:
        with pytest.raises(RuntimeError, match='incomplete'):
            procedure_runner.run(request)
    else:
        assert procedure_runner.run(request) == expected
    path = tmp_path / ('events/delivery-effects-' + 'c' * 32 + '.json')
    assert path.exists() == bool(result.get('deliver'))
    if path.exists():
        effects = json.loads(path.read_text())['effects']
        assert effects[0]['coverage_until'] == result['coverage_until']
        assert result['effects'][0] in effects
        assert {'kind': 'eligibility', 'valid_until': result['valid_until']} in effects
