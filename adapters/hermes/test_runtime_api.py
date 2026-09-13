import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('runtime_api', Path(__file__).with_name('runtime_api.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_real_argparse_contract_keeps_prompt_after_z():
    parser = argparse.ArgumentParser()
    parser.add_argument('-z')
    parser.add_argument('-t')
    parser.add_argument('--usage-file')
    argv = m.run_argv(['hermes', '-z'], 'fixture prompt', '/tmp/fixture-usage', 'terminal')
    parsed = parser.parse_args(argv[1:])
    assert parsed.z == 'fixture prompt' and parsed.t == 'terminal'
    assert parsed.usage_file == '/tmp/fixture-usage'
    assert m.run_argv(['python3', 'worker.py'], '{}', '/tmp/usage', 'terminal') == ['python3', 'worker.py', '{}']


def test_send_receipt_contains_provider_id_without_untrusted_response_fields(monkeypatch):
    sender = m.sibling('send')
    monkeypatch.setattr(sender.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0,
        stdout=json.dumps({'success': True, 'message_id': 'fixture-message', 'body': 'private content'})))
    assert sender.send_receipt('hello', 'photon') == (True, '', {
        'acceptance': 'accepted', 'message_id': 'fixture-message', 'target': 'photon'})
    assert sender.send('hello', 'photon') == (True, '')
    monkeypatch.setattr(sender.subprocess, 'run', lambda *a, **k:
                        (_ for _ in ()).throw(subprocess.TimeoutExpired('fixture', 1)))
    assert sender.send_receipt('hello', 'photon')[2]['acceptance'] == 'unknown'


def test_trusted_sibling_module_is_cached_and_send_capability_is_exposed():
    assert m.sibling('send') is m.sibling('send')
    ok, detail = m.send_capability(lambda *a, **k: SimpleNamespace(
        returncode=0, stdout='usage: hermes send [--json]', stderr=''))
    assert ok and detail == ''


def test_session_archival_and_timezone_errors_stay_in_adapter():
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='Title Last ID\nchat now 20260907_123_abc\ncron now cron_123')
    assert m.archive_sessions(run) == 1
    assert calls[-1] == ['hermes', 'sessions', 'archive', '20260907_123_abc']
    assert m.set_timezone('America/Los_Angeles', run)
    assert calls[-1] == ['hermes', 'config', 'set', 'timezone', 'America/Los_Angeles']


def test_layout_prefers_volume_and_explicit_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'local'))
    home = tmp_path / 'local' / '.hermes'
    home.mkdir(parents=True)
    (home / '.env').write_text('TELEGRAM_BOT_TOKEN=<persisted-test-token>\n')
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    assert m.telegram_bot_token() == '<persisted-test-token>'
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '<environment-test-token>')
    assert m.telegram_bot_token() == '<environment-test-token>'
    assert m.whatsapp_credentials_paths(tmp_path / 'volume') == [
        str(tmp_path / 'volume' / 'hermes/platforms/whatsapp/session/creds.json'),
        str(home / 'platforms/whatsapp/session/creds.json')]
    skill = home / 'skills/google-workspace/scripts/google_api.py'
    skill.parent.mkdir(parents=True)
    skill.touch()
    assert m.google_api_path() == str(skill)


def test_usage_aliases_envelopes_and_corrupt_optional_report(tmp_path):
    report = tmp_path / 'usage.json'
    report.write_text(json.dumps({'totals': {'cost_usd': 9, 'prompt_tokens': 3},
                                 'usage': {'cost_usd': 2, 'completion_tokens': 4},
                                 'cost': 1, 'model': ' fixture-model ', 'input_tokens': True}))
    assert m.read_usage(report) == {'model': 'fixture-model', 'cost': 1,
                                  'input_tokens': 3, 'output_tokens': 4}
    report.write_text('invalid-json')
    assert m.read_usage(report) is None
    report.unlink()
    assert m.read_usage(report) is None
