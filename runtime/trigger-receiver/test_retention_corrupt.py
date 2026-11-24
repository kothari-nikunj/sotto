"""Set-aside unreadable state files age out with the sweep and go with `forget.py --receipts`."""
import importlib.util
import os
from pathlib import Path
import time

HERE = Path(__file__).parent
PACK = HERE.parents[1] / 'sotto-chief-of-staff'
COPIES = ('events/outbox.json.corrupt.1762700000', 'preferences.json.corrupt.1762700000')


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(root, rel, age_days):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"rows": [')
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_sweep_deletes_old_set_aside_copies_and_keeps_young_ones(tmp_path, monkeypatch):
    ret = _load('corrupt_retention', HERE / 'retention.py')
    monkeypatch.setitem(ret.HOOKS, 'data_root', lambda: str(tmp_path))
    old = [_write(tmp_path, rel, ret.STAGED_DAYS + 1) for rel in COPIES]
    young = _write(tmp_path, 'events/onboarding.json.corrupt.1762800000', 1)
    ret.sweep()
    assert not any(path.exists() for path in old) and young.exists()


def test_quarantine_dates_the_copy_from_when_it_was_set_aside(tmp_path):
    conn = _load('corrupt_conn', HERE / 'connectors.py')
    path = _write(tmp_path, 'events/outbox.json', 30)
    try:
        with conn.json_transaction(str(path), default={}):
            pass
    except conn.StateUnreadableError:
        pass
    [aside] = path.parent.glob('outbox.json.corrupt.*')
    assert time.time() - aside.stat().st_mtime < 60  # noqa: PLR2004 — seconds, i.e. just now


def test_forget_receipts_removes_set_aside_copies(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    forget = _load('corrupt_forget', PACK / 'tools' / 'forget.py')
    paths = [_write(tmp_path, rel, 0) for rel in COPIES]
    forget.forget({'receipts'})
    assert not any(path.exists() for path in paths)
