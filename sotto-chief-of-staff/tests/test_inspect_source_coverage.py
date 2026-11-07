"""The coverage inspection uses only disposable synthetic data and fails on caught external calls."""
import json
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / 'evals/inspect_source_coverage.py'


def test_inspection_preserves_inherited_data_and_reports_actual_boundaries(tmp_path, monkeypatch):
    sentinel = tmp_path / 'private-sentinel'
    sentinel.write_text('must not read or mutate this data root')
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert report['unexpected_external_operations'] == []
    assert report['after_failed_read']['last_successful_read_at'] == '2026-10-20T08:00:00+00:00'
    assert report['learning']['review_scope'] == 'in_progress'
    assert report['after_revocation'] == {'source': 'imessage', 'label': 'iMessage', 'status': 'disabled'}
    assert list(tmp_path.iterdir()) == [sentinel]
    assert sentinel.read_text() == 'must not read or mutate this data root'


def test_inspection_fails_after_caught_external_attempt():
    result = subprocess.run([sys.executable, str(SCRIPT), '--self-test-denied'], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'denied unexpected external operations' in result.stderr
    assert not result.stdout
