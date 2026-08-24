"""One-shot intentions: durable, idempotent, conditional, and serviced by the existing heartbeat."""
import importlib.util
import json
import os
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *parts))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sw = _load("schedule_wakeup_test", "_shared", "scripts", "schedule_wakeup.py")
ps = _load("proactive_intentions_test", "proactive", "scripts", "proactive_scan.py")


def test_create_is_idempotent_and_cancel_is_a_transition(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    first = sw.create("2026-08-27T15:00:00-07:00", "Check on the deck", "Board deck")
    duplicate = sw.create("2026-08-27T15:00:00-07:00", "Check on the deck", "Board deck")
    assert duplicate["id"] == first["id"] and duplicate["duplicate"] is True
    assert len(sw.current()) == 1
    assert sw.transition(first["id"], "canceled")["status"] == "canceled"
    assert sw.due(datetime(2026, 8, 28, tzinfo=timezone.utc)) == []


def test_due_compares_real_instants_across_offsets(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    item = sw.create("2026-08-27T15:00:00-07:00", "Check on Sarah")
    assert sw.due(datetime(2026, 8, 27, 21, 59, tzinfo=timezone.utc)) == []
    assert [row["id"] for row in sw.due(datetime(2026, 8, 27, 22, 0, tzinfo=timezone.utc))] == [item["id"]]


def test_create_rejects_an_ambiguous_naive_time(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    with pytest.raises(ValueError, match="timezone"):
        sw.create("2026-08-27T15:00:00", "Check on Sarah")


def test_dedupe_keeps_distinct_loop_conditions(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    first = sw.create("2026-08-27T15:00:00-07:00", "Check for a reply", anchor_key="loop_a")
    second = sw.create("2026-08-27T15:00:00-07:00", "Check for a reply", anchor_key="loop_b")
    assert first["id"] != second["id"]


def test_corrupt_log_fails_closed_instead_of_forgetting_existing_intentions(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    path = tmp_path / "intentions.jsonl"
    path.write_text(json.dumps({"id": "int_existing", "status": "scheduled"}) + "\nnot-json\n")
    try:
        sw.create("2026-08-27T15:00:00-07:00", "Create a duplicate by mistake")
        assert False, "corrupt logs must stop writes"
    except ValueError:
        pass
    assert path.read_text().splitlines()[-1] == "not-json"


def test_proactive_scan_turns_due_intention_into_one_nudge():
    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    item = {"id": "int_1", "action": "Review the deck", "context": "Before the partner meeting"}
    out = ps.scan([], [], {}, "", now, intentions=[item])
    assert out["nudges"] == [{"kind": "intention", "key": "intention:int_1",
                               "intention_id": "int_1", "title": "Review the deck",
                               "detail": "Before the partner meeting"}]


def test_conditional_intention_auto_cancels_when_loop_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    item = sw.create("2026-08-20T15:00:00+00:00", "Check whether Sarah replied",
                     anchor_key="loop_sarah")
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    assert ps._intention_candidates(now, []) == []
    assert next(row for row in sw.current() if row["id"] == item["id"])["status"] == "canceled"


def test_conditional_intention_survives_while_loop_is_open(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    item = sw.create("2026-08-20T15:00:00+00:00", "Check whether Sarah replied",
                     anchor_key="loop_sarah")
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    out = ps._intention_candidates(now, [{"anchor_key": "loop_sarah"}])
    assert [row["id"] for row in out] == [item["id"]]
    assert sw.current()[0]["status"] == "scheduled"


def test_production_anchor_view_includes_things_the_user_is_waiting_on(tmp_path, monkeypatch):
    import loops_query as lq
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    item = sw.create("2026-08-20T15:00:00+00:00", "Check whether Sarah replied",
                     anchor_key="email:waiting_on:id:sarah")
    monkeypatch.setattr(lq, "query", lambda: {
        "you_owe": [],
        "waiting_on_them": [{"anchor_key": "email:waiting_on:id:sarah"}],
    })
    out = ps._intention_candidates(datetime(2026, 8, 27, tzinfo=timezone.utc))
    assert [row["id"] for row in out] == [item["id"]]


def test_conditional_intention_waits_when_ledger_is_unavailable(tmp_path, monkeypatch):
    import loops_query as lq
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    item = sw.create("2026-08-20T15:00:00+00:00", "Check whether Sarah replied",
                     anchor_key="email:waiting_on:id:sarah")
    monkeypatch.setattr(lq, "query", lambda: (_ for _ in ()).throw(OSError("volume unavailable")))
    assert ps._intention_candidates(datetime(2026, 8, 27, tzinfo=timezone.utc)) == []
    assert sw.current()[0]["id"] == item["id"]
    assert sw.current()[0]["status"] == "scheduled"
