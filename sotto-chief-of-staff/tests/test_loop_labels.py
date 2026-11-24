"""Every owner decision about a loop is a content-free label with the signals present just before it."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("continuity_labels_test", ROOT / "morning-brief/scripts/continuity_resolve.py")
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)
import log_outcome  # noqa: E402
import loop_checks  # noqa: E402
import loop_label_report  # noqa: E402
import retune_apply  # noqa: E402
from delivery_effects import loop_version  # noqa: E402

PRIVATE = ("Koushik", "+12025550170", "Pick a time", "k-private")


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.delenv("SOTTO_DELIVERY_RUN_ID", raising=False)
    monkeypatch.setattr(retune_apply, "cr", cr)
    return tmp_path


def _row(**fields):
    return {"anchor_key": "k-private", "status": "open", "action_type": "reply", "channel": "imessage",
            "contact_name": "Koushik", "contact_identifier": "+12025550170",
            "created_at": "2026-11-03 10:00:00", "summary": "Pick a time to catch up", "chased_count": 0,
            **fields}


def labels(root):
    path = root / "outcomes.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row.get("outcome") == "loop_label"]


def test_an_owner_dismissal_records_the_hint_it_had_without_content(root):
    item = _row()
    cr._persist(item)
    version = loop_version(item)
    loop_checks.record({version: {"basis": "you_messaged", "at": "2026-11-05", "source": "imessage"}})
    assert retune_apply.apply("dismiss", "k-private")["ok"]
    [label] = labels(root)
    assert label["decision"] == "dismissed" and label["loop_version"] == version
    assert label["hint"] == {"basis": "you_messaged", "source": "imessage"}
    assert label["direction"] == "you_owe" and label["channel"] == "imessage"
    assert label["action_type"] == "reply" and label["chased_count"] == 0
    raw = (root / "outcomes.jsonl").read_text()
    for private in PRIVATE:
        assert private not in raw


def test_snooze_and_keep_are_labels_and_a_repeat_save_is_not(root):
    item = _row()
    cr._persist(item)
    retune_apply.apply("snooze", "k-private", 3)
    retune_apply.apply("keep", "k-private")
    assert [label["decision"] for label in labels(root)] == ["snooze", "keep"]
    assert labels(root)[0]["hint"] is None
    cr._persist(cr._load_items()["k-private"])
    assert [label["decision"] for label in labels(root)] == ["snooze", "keep"]


@pytest.mark.parametrize("basis", ["read_no_reply", "reacted"])
def test_imessage_interaction_hints_do_not_go_into_outcome_logs(root, basis):
    item = _row()
    cr._persist(item)
    loop_checks.record({loop_version(item): {"basis": basis, "at": "2026-11-05T17:10:00Z", "source": "imessage",
                                            "message_at": "2026-11-05T17:05:38Z", "kind": "like"}})
    assert retune_apply.apply("dismiss", "k-private")["ok"]
    assert labels(root)[0]["hint"] is None


def test_sotto_bookkeeping_and_its_own_closures_are_not_owner_labels(root):
    item = _row()
    cr._persist(item)
    item["chased_count"] = 1
    cr._persist(item)
    cr._terminate(item, "resolved", "replied", "2026-11-08")
    cr._persist(item)
    assert labels(root) == []


def test_only_an_audited_owner_add_is_a_miss_label(root):
    import knowledge_edit
    knowledge_edit.op_loop_add("Send the deck", contact="Dana", direction="you")  # origin unproved
    assert labels(root) == []
    knowledge_edit.op_loop_add_admitted_owner_text("Call the bank", owner_text_span="Call the bank",
                                                   owner_event_id="evt-1", now=datetime.now(timezone.utc))
    [label] = labels(root)
    assert label["decision"] == "added" and label["hint"] is None


def test_a_retarget_is_one_label_and_its_replacement_is_not_an_add(root):
    import knowledge_edit
    old = knowledge_edit.op_loop_add("Send James the intro", contact="James", direction="you")["anchor_key"]
    knowledge_edit.op_loop_retarget(old, "James will get back to me", contact="James", direction="them")
    assert [label["decision"] for label in labels(root)] == ["retargeted"]


def test_a_malformed_chase_count_never_blocks_the_decision(root):
    cr._persist(_row(chased_count="twice"))
    assert retune_apply.apply("dismiss", "k-private")["ok"]
    assert labels(root)[0]["chased_count"] is None


def test_a_hint_from_a_revoked_source_is_not_recorded(root, monkeypatch):
    import source_context
    item = _row()
    cr._persist(item)
    loop_checks.record({loop_version(item): {"basis": "you_messaged", "at": "2026-11-05", "source": "imessage"}})
    monkeypatch.setattr(source_context, "allowed", lambda source: source != "imessage")
    retune_apply.apply("dismiss", "k-private")
    assert labels(root)[0]["hint"] is None


def test_a_label_failure_never_undoes_the_owner_decision(root, monkeypatch):
    item = _row()
    cr._persist(item)
    real = log_outcome.log
    monkeypatch.setattr(log_outcome, "log", lambda rec: (_ for _ in ()).throw(OSError("disk")))
    assert retune_apply.apply("dismiss", "k-private")["ok"]
    assert cr._load_items()["k-private"]["status"] == "dismissed"
    monkeypatch.setattr(log_outcome, "log", real)
    assert labels(root) == []


def _label(decision, basis=None, *, version, channel="imessage", hint_source="imessage"):
    return {"schema": 1, "outcome": "loop_label", "source": "owner_decision", "ts": "2026-11-09T10:00:00Z",
            "event_id": version + decision, "loop_key": "k" + version, "loop_version": version,
            "decision": decision, "direction": "you_owe", "action_type": "reply", "channel": channel,
            "hint": {"basis": basis, "source": hint_source} if basis else None}


def test_the_report_gives_hint_precision_and_unsignalled_closes(root):
    rows = [_label("resolved", "you_messaged", version="a"), _label("resolved", "you_messaged", version="b"),
            _label("keep", "you_messaged", version="c"), _label("resolved", "meeting", version="d",
                                                                hint_source="calendar"),
            _label("resolved", version="e"), _label("added", version="f"), _label("dismissed", version="g")]
    (root / "outcomes.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    report = loop_label_report.build(now=datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert report["hints"]["you_messaged"] == {"closed": 2, "kept_open": 1, "dismissed": 0, "precision": 0.67}
    assert report["hints"]["meeting"]["precision"] == 1.0
    assert report["closed_without_hint"] == 1 and report["added_by_you"] == 1
    assert report["decisions"] == len(rows)


def test_the_report_leaves_out_labels_from_a_revoked_source(root, monkeypatch):
    import source_context
    rows = [_label("resolved", "you_messaged", version="a"), _label("resolved", "meeting", version="b",
                                                                    hint_source="calendar")]
    (root / "outcomes.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(source_context, "allowed", lambda source: source not in ("imessage",))
    report = loop_label_report.build(now=datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert "you_messaged" not in report["hints"]


def test_an_owner_close_of_a_waiting_loop_names_its_direction_and_meeting_hint(root):
    item = _row(action_type="waiting_on", status="waiting", chased_count=2)
    cr._persist(item)
    loop_checks.record({loop_version(item): {"basis": "meeting", "at": "2026-11-09", "source": "calendar"}})
    cr._terminate(item, "resolved", "user_resolved", "2026-11-09")
    cr._persist(item)
    [label] = labels(root)
    assert (label["decision"], label["direction"], label["chased_count"]) == ("resolved", "waiting_on", 2)
    assert label["hint"] == {"basis": "meeting", "source": "calendar"}


def test_undoing_an_automatic_close_is_a_label_that_names_the_rejected_signal(root):
    import signal_closes
    closed = _row(status="resolved", resolution="scheduled_meeting", resolved_at="2026-11-08",
                  resolution_basis={"basis": "scheduled_meeting", "source": "calendar", "at": "2026-11-09"},
                  resolution_source_refs=[{"sourceType": "calendar", "sourceId": "ev-1"}])
    reopened = dict(closed)
    signal_closes.reopen(reopened, "2026-11-09")
    label = log_outcome.loop_label(closed, reopened)
    assert label["decision"] == "reopened"
    assert label["hint"] == {"basis": "scheduled_meeting", "source": "calendar"}
    assert log_outcome.record_loop_event(label)
    report = loop_label_report.build(datetime.now(timezone.utc))
    assert report["auto_closes_undone"] == {"scheduled_meeting": 1}
    assert "scheduled_meeting" not in report["hints"]
    for private in PRIVATE + ("ev-1",):
        assert private not in (root / "outcomes.jsonl").read_text()


def test_a_reopen_for_a_new_ask_is_not_an_owner_label(root):
    closed = _row(status="resolved", resolution="scheduled_meeting",
                  resolution_basis={"basis": "scheduled_meeting", "source": "calendar"})
    assert log_outcome.loop_label(closed, _row(status="open", reopened_at="2026-11-09")) is None


@pytest.mark.parametrize("basis,source", [("call", "calls"), ("sent_to_them", "gmail"), ("met", "granola")])
def test_a_close_records_whatever_hint_basis_the_loop_check_stored(root, basis, source, monkeypatch):
    import source_context
    monkeypatch.setattr(source_context, "allowed", lambda name: True)
    item = _row()
    cr._persist(item)
    loop_checks.record({loop_version(item): {"basis": basis, "at": "2026-11-05", "source": source}})
    assert retune_apply.apply("dismiss", "k-private")["ok"]
    [label] = labels(root)
    assert label["hint"] == {"basis": basis, "source": source}
    assert loop_label_report.build(datetime.now(timezone.utc))["hints"][basis]["dismissed"] == 1
