"""Catch import paths that work in a checkout but fail in the deployed image."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def packaged(tmp_path):
    app = tmp_path / 'app'
    receiver = app / 'trigger-receiver'
    shutil.copytree(ROOT / 'runtime/trigger-receiver', receiver,
                    ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    shutil.copytree(ROOT / 'sotto-chief-of-staff', app / 'sotto-skills',
                    ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache', 'tests', '.venv'))
    shutil.copytree(ROOT / 'adapters', app / 'adapters',
                    ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    # The image's absolute /app adapter fallback cannot exist under pytest's temp root.
    # Expose the same copied adapters at the receiver's checkout-relative lookup as well.
    (tmp_path / 'adapters').symlink_to(app / 'adapters', target_is_directory=True)
    # Mirror the actual Docker COPY statements for the bootstrap helpers.
    for line in (ROOT / 'Dockerfile').read_text().splitlines():
        if line.startswith('COPY sotto-chief-of-staff/_shared/lib/'):
            _, source, destination = line.split()
            shutil.copy2(ROOT / source, tmp_path / destination.lstrip('/'))
    return receiver


def run_check(receiver, tmp_path, mode='managed'):
    env = {**os.environ, 'SOTTO_DEPLOYMENT_MODE': mode, 'PYTHONDONTWRITEBYTECODE': '1'}
    return subprocess.run([sys.executable, '-I', '-B', str(receiver / 'check_runtime.py')],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90)


@pytest.mark.parametrize('mode', ['managed', 'self-host'])
def test_receiver_delivery_in_image_layout_without_checkout_or_pythonpath(packaged, tmp_path, mode):
    result = run_check(packaged, tmp_path, mode)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"brief_images": 4' in result.stdout and '"nudge_validity": "ok"' in result.stdout
    assert '"scheduled_nudge": "accepted"' in result.stdout
    assert '"restart_recovery": "ok"' in result.stdout
    assert '"ambiguous_gallery": "held"' in result.stdout


@pytest.mark.parametrize('regression', ['image_import', 'nonempty_scan', 'missing_receipt'])
def test_delivery_gate_rejects_regressions(packaged, tmp_path, regression):
    """Prove the gate fails for the outages, not just that its happy fixture stays green."""
    if regression == 'image_import':
        path = packaged / 'receiver.py'
        old, new = "here.parent / 'sotto-skills'", "here.parent / 'missing-skills'"
        diagnostic = 'delivery_effects.py is missing'
    elif regression == 'nonempty_scan':
        path = packaged / 'procedure_runner.py'
        old, new = "('eligibility', 'unsolicited_nudge')", "('eligibility',)"
        diagnostic = 'proactive scanner omitted eligibility'
    else:
        path = packaged / 'check_runtime.py'
        old, new = "'text': {'success': True, 'message_id': 'test-text'}", "'text': {'success': True}"
        diagnostic = 'Hermes returned no provider message ID'
    source = path.read_text()
    assert old in source
    path.write_text(source.replace(old, new, 1))
    result = run_check(packaged, tmp_path)
    assert result.returncode != 0, 'broken delivery passed the gate'
    assert diagnostic in result.stdout + result.stderr, result.stdout + result.stderr


def test_image_build_runs_the_packaged_receiver_check():
    dockerfile = (ROOT / 'Dockerfile').read_text()
    assert 'python3 -I -B /app/trigger-receiver/check_runtime.py' in dockerfile
