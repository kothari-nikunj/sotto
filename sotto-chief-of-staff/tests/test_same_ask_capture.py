"""One captured ask is one task: the same person's same request from the same source message
never becomes two ledger rows because extraction typed it two ways (`capture_shape.same_ask`)."""
import importlib.util
import os
from datetime import datetime

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
spec = importlib.util.spec_from_file_location(
    "cr_same_ask", os.path.join(ROOT, "morning-brief", "scripts", "continuity_resolve.py"))
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)

NOW = datetime(2026, 6, 24, 9, 0, 0)
REF = [{"sourceType": "gmail", "sourceId": "msg-100"}]
# The messages those refs cite: a first capture whose citation does not bind is never saved.
LOCAL = {"emails": [{"id": ident, "from": "robin@example.test", "isSent": False,
                     "date": "2026-06-24T08:00:00Z",
                     "body": "Please suggest some times that work for you next week. Send me the deck."}
                    for ident in ("msg-100", "msg-200")]}


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")


def _rows(tmp_path) -> list:
    d = tmp_path / "knowledge" / "continuity"
    return [yaml.safe_load(p.read_text().split("---\n")[1]) for p in sorted(d.glob("*.md"))]


def _ask(kind, ask, refs=REF, *, deliverable=None):
    return {"type": kind, "channel": "gmail", "contactName": "Robin Vale",
            "contactIdentifier": "robin@example.test", "canonical_id": "person-robin",
            "contextSummary": "Robin asked for meeting times", "ask": ask, "evidence": refs,
            "deliverable": deliverable}


TIMES_ASK = "suggest some times that work for you next week"
DELIVERABLE = "Suggest meeting times for next week"
REPLY = _ask("reply", f"Please {TIMES_ASK}, and I'll do my best to accommodate", deliverable=DELIVERABLE)
PROPOSE = _ask("propose_times", TIMES_ASK, deliverable=DELIVERABLE)


def test_one_ask_typed_two_ways_in_one_batch_is_one_task(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    cr.resolve({"today": "2026-06-24", "local": LOCAL, "new_actions": [REPLY, PROPOSE]}, NOW, resolve_existing=False)
    rows = [r for r in _rows(tmp_path) if r["status"] == "open"]
    assert [r["action_type"] for r in rows] == ["reply"]


def test_one_ask_typed_two_ways_on_a_later_run_is_one_task(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    cr.resolve({"today": "2026-06-24", "local": LOCAL, "new_actions": [PROPOSE]}, NOW, resolve_existing=False)
    cr.resolve({"today": "2026-06-24", "local": LOCAL, "new_actions": [REPLY]}, NOW, resolve_existing=False)
    rows = [r for r in _rows(tmp_path) if r["status"] == "open"]
    assert [r["action_type"] for r in rows] == ["propose_times"]


def test_two_different_requests_in_one_message_stay_two_tasks(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    cr.resolve({"today": "2026-06-24", "local": LOCAL, "new_actions": [
        _ask("reply", "send me the deck"), _ask("propose_times", "can we meet Tuesday")]},
        NOW, resolve_existing=False)
    rows = [r for r in _rows(tmp_path) if r["status"] == "open"]
    assert sorted(r["action_type"] for r in rows) == ["propose_times", "reply"]


def test_overlapping_ask_text_does_not_fold_a_compound_request(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    cr.resolve({"today": "2026-06-24", "local": LOCAL, "new_actions": [
        _ask("reply", "Please suggest two times next week and send the signed deck"),
        _ask("propose_times", "suggest two times next week")]}, NOW, resolve_existing=False)
    assert sorted(r["action_type"] for r in _rows(tmp_path)) == ["propose_times", "reply"]


def test_same_ask_text_with_two_deliverables_preserves_both_tasks(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    ask = "Suggest two times next week and send the signed deck"
    cr.resolve({"today": "2026-06-24", "local": LOCAL, "new_actions": [
        _ask("reply", ask, deliverable="Send the signed deck"),
        _ask("propose_times", ask, deliverable="Suggest two times next week")]},
        NOW, resolve_existing=False)
    assert sorted(r["deliverable"] for r in _rows(tmp_path)) == [
        "Send the signed deck", "Suggest two times next week",
    ]


def test_distinct_bound_clauses_preserve_both_tasks(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    times, deck = "Suggest two times next week.", "Send the signed deck."
    message = {"id": "two-asks", "handle": "+14155550142", "timestamp": "2026-06-24T08:00:00Z",
               "is_from_me": False, "text": f"{times} {deck}"}
    actions = [{"type": kind, "channel": "imessage", "contactName": "Robin Vale",
                "contactIdentifier": message["handle"], "newObligation": True,
                "contextAsk": ask, "deliverable": "Follow up next week", "owedBy": "owner",
                "evidence": [{"sourceType": "imessage", "sourceId": message["id"], "snippet": quote}]}
               for kind, quote, ask in [("reply", deck, f"{times} {deck}"), ("propose_times", times, times)]]
    result = cr.resolve({"today": "2026-06-24", "new_actions": actions, "local": {"imessage": [message]}},
                        NOW, resolve_existing=False)
    assert result["capture_outcomes"]["source_verified"] == len(actions)
    assert sorted(r["action_type"] for r in _rows(tmp_path)) == ["propose_times", "reply"]


def test_same_legacy_ask_keeps_retained_fulfillment_decision(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    cr.resolve({"today": "2026-06-24", "local": LOCAL, "new_actions": [
        {**_ask("reply", "Suggest two times next week."), "fulfillment": "none"},
        {**_ask("propose_times", "suggest two times next week"), "fulfillment": "schedule"}]},
        NOW, resolve_existing=False)
    row, = _rows(tmp_path)
    assert row["action_type"] == "reply"
    assert row["fulfillment"] == "none"


def test_the_same_words_from_another_message_are_not_folded_by_this_rule(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    later = _ask("propose_times", "suggest some times that work for you next week",
                 [{"sourceType": "gmail", "sourceId": "msg-200"}])
    cr.resolve({"today": "2026-06-24", "local": LOCAL, "new_actions": [REPLY, later]}, NOW, resolve_existing=False)
    rows = [r for r in _rows(tmp_path) if r["status"] == "open"]
    assert sorted(r["action_type"] for r in rows) == ["propose_times", "reply"]


def test_a_waiting_on_never_folds_into_a_task_the_owner_owes(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    message = {"id": "msg-100", "from": "robin@example.test", "isSent": False,
               "body": "I'll send meeting times. Please suggest some too.", "date": "2026-06-24T08:00:00Z"}
    cr.resolve({"today": "2026-06-24", "local": {"emails": [message]}, "new_actions": [
        REPLY, _ask("waiting_on", "suggest some times that work for you next week")]},
        NOW, resolve_existing=False)
    rows = [r for r in _rows(tmp_path) if r["status"] == "open"]
    assert sorted(r["action_type"] for r in rows) == ["reply", "waiting_on"]


def test_the_other_type_never_reopens_a_task_the_owner_closed(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    cr.resolve({"today": "2026-06-24", "local": LOCAL, "new_actions": [REPLY]}, NOW, resolve_existing=False)
    d = tmp_path / "knowledge" / "continuity"
    (path,) = d.glob("*.md")
    head, fm, body = path.read_text().split("---\n", 2)
    fm = {**yaml.safe_load(fm), "status": "dismissed", "resolution": "user_dismissed"}
    path.write_text(head + "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body)
    cr.resolve({"today": "2026-06-24", "local": LOCAL, "new_actions": [PROPOSE]}, NOW, resolve_existing=False)
    assert [r["status"] for r in _rows(tmp_path)] == ["dismissed"]


def test_without_an_ask_the_summary_never_folds_two_types(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    cr.resolve({"today": "2026-06-24", "local": LOCAL, "new_actions": [
        {**REPLY, "ask": None}, {**PROPOSE, "ask": None}]}, NOW, resolve_existing=False)
    rows = [r for r in _rows(tmp_path) if r["status"] == "open"]
    assert sorted(r["action_type"] for r in rows) == ["propose_times", "reply"]
