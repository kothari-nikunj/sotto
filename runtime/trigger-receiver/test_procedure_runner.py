"""Shared procedures honor the real CLI shapes and stage effects without sending."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
from types import SimpleNamespace

import pytest
import procedure_runner

PACK = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'


@pytest.mark.parametrize('case', ['eligible', 'disabled', 'expired', 'missing', 'guard_only',
                                  'eligibility_only', 'partial', 'duplicate_guard', 'unknown', 'malformed'])
def test_proactive_consumes_real_scanner_guards_before_composing(case, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_NUDGE_BUDGET', '0' if case == 'disabled' else '10')
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    monkeypatch.syspath_prepend(str(PACK / '_shared/scripts'))
    import delivery_effects
    import source_context
    import compose_notification
    monkeypatch.setattr(source_context, 'read_local', lambda: {})
    deadline = time.time() + (-60 if case == 'expired' else 600)
    # Use the scanner's actual producer, including its global nudge preference guard.
    effects = delivery_effects.for_bundle({'events': [{'event': {'valid_until': deadline}}]})['effects']
    if case == 'missing':
        effects = []
    elif case == 'guard_only':
        effects = [{'kind': 'unsolicited_nudge'}]
    elif case == 'eligibility_only':
        effects = [e for e in effects if e['kind'] == 'eligibility']
    elif case == 'duplicate_guard':
        effects.append({'kind': 'unsolicited_nudge'})
    elif case == 'unknown':
        effects.append({'kind': 'unrecognized'})
    elif case == 'malformed':
        effects.append(None)
    nudges = [{'kind': 'chase', 'detail': 'Synthetic follow-up'}]
    if case == 'partial':
        nudges.append({'kind': 'meeting_prep', 'detail': 'Synthetic meeting'})
    result = {'nudges': nudges, 'quiet': False, '_eligibility': effects}

    def invoke(argv, **kwargs):
        output = 'Calendar gathered\n' if Path(argv[1]).name == 'gather_google.py' else json.dumps(result)
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    monkeypatch.setattr(procedure_runner.subprocess, 'run', invoke)
    composed = []
    def compose(kind, items):
        composed.append((kind, items))
        return 'Synthetic follow-up'
    monkeypatch.setattr(compose_notification, 'compose', compose)
    request = {'pack': str(PACK), 'kind': 'proactive'}
    if case not in ('eligible', 'disabled', 'expired'):
        with pytest.raises(RuntimeError, match='eligibility'):
            procedure_runner.run(request)
    else:
        assert procedure_runner.run(request) == ('Synthetic follow-up' if case == 'eligible' else 'NO_NUDGES')
    assert composed == ([('proactive', nudges)] if case == 'eligible' else [])


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
      'item_ids': ['digest-item-a'],
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
        assert effects[0] == {'kind': 'digest_accept', 'item_ids': result['item_ids']}
        assert result['effects'][0] in effects
        assert {'kind': 'eligibility', 'valid_until': result['valid_until']} in effects


def test_digest_child_budget_exit_is_typed_and_other_child_exit_is_not(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    from work_queue import MODEL_BUDGET_EXIT
    monkeypatch.setattr(procedure_runner.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=MODEL_BUDGET_EXIT, stdout='', stderr='private'))
    with pytest.raises(procedure_runner.ProcedureModelBudgetError):
        procedure_runner.run({'pack': str(PACK), 'kind': 'digest'})
    # The same exit from a gatherer is just an ordinary child error.
    with pytest.raises(RuntimeError, match='gather_google.py exit 77') as error:
        procedure_runner.run({'pack': str(PACK), 'kind': 'proactive'})
    assert not isinstance(error.value, procedure_runner.ProcedureModelBudgetError)


def test_procedure_cli_emits_budget_exit_only_for_typed_denial(monkeypatch):
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    from gemini_transport import ModelBudgetUnavailableError
    monkeypatch.setattr(sys, 'argv', ['procedure_runner.py', json.dumps({'pack': str(PACK), 'kind': 'digest'})])
    def denied(_request):
        raise ModelBudgetUnavailableError('private')
    monkeypatch.setattr(procedure_runner, 'run', denied)
    from work_queue import MODEL_BUDGET_EXIT
    assert procedure_runner.main() == MODEL_BUDGET_EXIT
    def child_denied(_request):
        raise procedure_runner.ProcedureModelBudgetError('private')
    monkeypatch.setattr(procedure_runner, 'run', child_denied)
    assert procedure_runner.main() == MODEL_BUDGET_EXIT
    def transient(_request):
        raise RuntimeError('private')
    monkeypatch.setattr(procedure_runner, 'run', transient)
    assert procedure_runner.main() == 1


@pytest.mark.parametrize('arguments,diagnostic', [([], 'IndexError'), (['{'], 'JSONDecodeError'),
                                                   ([json.dumps({'kind': 'digest'})], 'KeyError')])
def test_malformed_request_cli_reports_original_error_without_pack_import(arguments, diagnostic):
    child = subprocess.run([sys.executable, str(Path(procedure_runner.__file__)), *arguments],
                           capture_output=True, text=True, timeout=20)
    assert child.returncode == 1
    assert child.stdout == ''
    assert child.stderr.strip() == f'[procedure_runner] {diagnostic}'


@pytest.mark.parametrize('url,body,terminal', [
    ('https://proxy.example/native/v1beta/models/gemini-3.8-flash:generateContent',
     b'{"error":{"code":"sotto_budget_exhausted"}}', True),
    ('https://proxy.example/native/v1beta/models/gemini-3.8-flash:generateContent',
     b'{"error":{"code":"other_payment_error"}}', False),
    ('https://unrelated.example/native/v1beta/models/gemini-3.8-flash:generateContent',
     b'{"error":{"code":"sotto_budget_exhausted"}}', False),
])
def test_proactive_compose_preserves_only_validated_proxy_402(
        tmp_path, monkeypatch, url, body, terminal):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'fixture-token')
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    monkeypatch.syspath_prepend(str(PACK / '_shared/scripts'))
    import source_context
    import delivery_effects
    import compose_notification
    import gemini
    from work_queue import MODEL_BUDGET_EXIT
    monkeypatch.setattr(source_context, 'read_local', lambda: {})
    monkeypatch.setattr(compose_notification, 'enrich', lambda items, now: items)
    proof = delivery_effects.for_bundle({'events': [{'event': {'valid_until': time.time() + 600}}]})['effects']
    result = {'nudges': [{'kind': 'chase', 'key': 'fixture-chase', 'detail': 'Reply is due'}],
              'quiet': False, '_eligibility': proof}
    def child(argv, **kwargs):
        output = 'Calendar gathered\n' if Path(argv[1]).name == 'gather_google.py' else json.dumps(result)
        return SimpleNamespace(returncode=0, stdout=output, stderr='')
    monkeypatch.setattr(procedure_runner.subprocess, 'run', child)
    requests = []
    def denied(request, timeout):
        requests.append(request.full_url)
        raise urllib.error.HTTPError(url, 402, 'fixture', {}, io.BytesIO(body))
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', denied)
    monkeypatch.setattr(sys, 'argv', ['procedure_runner.py', json.dumps({'pack': str(PACK), 'kind': 'proactive'})])
    assert procedure_runner.main() == (MODEL_BUDGET_EXIT if terminal else 1)
    assert requests == ['https://proxy.example/native/v1beta/models/gemini-3.8-flash:generateContent']
