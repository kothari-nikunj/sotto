"""A commitment nudge never repeats a loop the brief already named today, and a brief still being
composed counts as a brief that is about to name it (fictional people only)."""
import importlib.util
import os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "ps_overlap", os.path.join(HERE, "..", "proactive", "scripts", "proactive_scan.py"))
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)

NOW = datetime.now(timezone.utc).replace(hour=9, minute=30, second=0, microsecond=0)
YESTERDAY = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")


def _loop(key="email:reply:id:dana"):
    return {"id": f"you_owe:Dana Reyes:{key}", "title": "Dana Reyes — the budget sheet",
            "name": "Dana Reyes", "anchor_key": key, "deadline": YESTERDAY, "channel": "email",
            "identifier": "dana@example.test"}


def _kinds(**kw):
    return [n["kind"] for n in ps.scan([], [_loop()], {}, "me@example.test", NOW, **kw)["nudges"]]


def test_commitment_named_by_todays_brief_gets_no_nudge():
    assert _kinds(brief_named={"email:reply:id:dana"}) == []


def test_commitment_the_brief_did_not_name_still_nudges():
    assert _kinds(brief_named={"email:reply:id:someone-else"}) == ["commitment"]
    assert _kinds(brief_named=set()) == ["commitment"]


def test_commitment_without_a_named_record_uses_the_time_window():
    assert _kinds(brief_named=None, brief_recent=True) == []
    assert _kinds(brief_named=None, brief_recent=False) == ["commitment"]


def test_an_active_brief_job_counts_as_a_recent_brief(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    import work_queue
    assert ps._brief_job_active() is False
    work_queue.enqueue(str(tmp_path), "run", {"label": "cron:sotto-morning-brief"}, key="brief-run-1")
    assert ps._brief_job_active() is True
