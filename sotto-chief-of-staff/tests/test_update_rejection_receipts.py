"""Rejected completion proposals explain the hold without logging private source text."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("receipt_continuity", ROOT / "morning-brief/scripts/continuity_resolve.py")
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)
from delivery_effects import loop_version


@pytest.mark.parametrize("reason", ["unknown_loop", "stale_revision", "unsupported_evidence"])
def test_rejected_proposal_has_content_free_reason(tmp_path, monkeypatch, capsys, reason):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "UTC")
    row = {"anchor_key": "follow_up:id:maya@example.test", "contact_identifier": "maya@example.test",
           "contact_name": "Maya", "action_type": "reply", "channel": "email", "status": "open",
           "summary": "Send the signed contract", "created_at": "2026-09-10"}
    cr._persist(row)
    proposal = {"loopId": row["anchor_key"], "loopVersion": loop_version(row), "status": "resolved",
                "evidence": [{"sourceType": "email", "sourceId": "unknown", "snippet": "private secret quote"}]}
    if reason == "unknown_loop":
        proposal["loopId"] = "missing"
    elif reason == "stale_revision":
        proposal["loopVersion"] = "stale"
    out = cr.resolve({"today": "2026-09-18", "loopUpdates": [proposal]},
                     datetime(2026, 9, 18, 18), resolve_existing=False)
    assert not out["resolved"] and len(out["active"]) == 1
    diagnostic = capsys.readouterr().err
    assert f'"{reason}": 1' in diagnostic
    assert "private secret quote" not in diagnostic and "maya@example.test" not in diagnostic


def test_rejection_counts_survive_real_continuity_subprocess_in_learn_receipt(tmp_path, monkeypatch):
    import learn_step

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    payload = tmp_path / 'continuity.json'
    payload.write_text(json.dumps({'today': '2026-09-18', 'loopUpdates': [
        {'loopId': 'missing-a', 'evidence': [{'snippet': 'private first quote'}]},
        {'loopId': 'missing-b', 'evidence': [{'snippet': 'private second quote'}]},
    ]}))
    step = learn_step._run_one(str(ROOT / 'morning-brief/scripts/continuity_resolve.py'),
                               ['--merge-only', str(payload)])
    assert step['status'] == 'ok'
    assert '"unknown_loop": 2' in step['detail']
    assert 'private' not in step['detail'] and 'missing-' not in step['detail']
