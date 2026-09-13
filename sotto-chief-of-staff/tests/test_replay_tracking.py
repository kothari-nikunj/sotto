"""Guard the offline probe's isolation and reporting, without enshrining broken runtime behavior."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'evals/replay_tracking.py'
spec = importlib.util.spec_from_file_location('tracking_probe', SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_continuous_probe_reports_real_results_and_preserves_parent_data(tmp_path, monkeypatch):
    sentinel = tmp_path / 'knowledge/continuity/user.md'
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text('This is the user volume; no replay writes belong here.')
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    before = sentinel.read_bytes()
    result = probe.replay()
    assert result['days_advanced'] == 29
    assert [c['day'] for c in result['checkpoints']] == [0, 1, 7, 15, 28]
    # The state survives across checkpoints, including a user-confirmed disposition on day 7.
    quote = [r for r in result['checkpoints'][-1]['rows'] if r['contact_name'] == 'Dov']
    assert len(quote) == 1 and quote[0]['resolution'] == 'user_resolved'
    assert sentinel.read_bytes() == before
    assert [p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*') if p.is_file()] == ['knowledge/continuity/user.md']
    assert os.environ['SOTTO_DATA'] == str(tmp_path)
    assert os.environ['SOTTO_TIMEZONE'] == 'America/Los_Angeles'
    assert result == probe.replay()


def test_cli_fails_when_contract_is_unmet_and_emits_parseable_json():
    run = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    data = json.loads(run.stdout)
    unmet = any(not c['met'] for c in data['checks'])
    assert run.returncode == int(unmet)
    assert not run.stderr


def test_sandbox_restores_environment_on_error(monkeypatch, tmp_path):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    with pytest.raises(RuntimeError):
        with probe.sandbox() as root:
            assert root != tmp_path
            assert root.exists()
            raise RuntimeError('interrupted replay')
    assert not root.exists()
    assert os.environ['SOTTO_DATA'] == str(tmp_path)
