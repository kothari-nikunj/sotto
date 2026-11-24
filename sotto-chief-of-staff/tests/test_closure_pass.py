"""closure_pass.py — the owner's own message is judged against that person's open tasks when it is sent.

A reply is never proof: only a judge's quoted "delivered" verdict that the critic approves and the
resolver's own checks accept closes a task (the brief's strict path). Anything less is at most the
"maybe done" hint, written at send time. All people, numbers and addresses here are fictional.
"""
import importlib.util
import os
from datetime import datetime, timezone

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
spec = importlib.util.spec_from_file_location(
    "closure_pass", os.path.join(ROOT, "morning-brief", "scripts", "closure_pass.py"))
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)
import continuity_resolve as cr  # noqa: E402
import loop_checks  # noqa: E402
from delivery_effects import loop_version  # noqa: E402

RIYA = "+14155550142"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
ADDRESS = "It's 12 Example Lane, Springfield"


@pytest.fixture(autouse=True)
def _data(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    (tmp_path / "knowledge/continuity").mkdir(parents=True)


def _task(tmp_path, key="k", **fields):
    row = {"anchor_key": key, "status": "open", "action_type": "reply", "channel": "imessage",
           "contact_name": "Riya Example", "contact_identifier": RIYA, "created_at": "2026-11-03 10:00:00",
           "summary": "Riya wants to post sweets", "deliverable": "Send Riya your mailing address",
           "source_refs": [{"sourceType": "imessage", "sourceId": "m-1",
                            "snippet": "What's your mailing address?"}], **fields}
    (tmp_path / "knowledge/continuity" / f"{key}.md").write_text(
        "---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    return row


def _row(tmp_path, key="k"):
    return yaml.safe_load((tmp_path / "knowledge/continuity" / f"{key}.md").read_text().split("---")[1])


def _sent(text, handle=RIYA, when="2026-11-05 17:05:38", rowid=11, **extra):
    return {"source": "imessage", "rowid": rowid, "handle": handle, "is_from_me": True,
            "timestamp": when, "text": text, "is_group_chat": False, "chat_guid": None, **extra}


class Judge:
    """Stands in for the model: answers per task from a table keyed by the task's deliverable."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, task, messages):
        self.calls.append((task, messages))
        verdict, quote = self.answers.get(task["deliverable"], ("not", ""))
        return {"verdict": verdict, "message_id": messages[-1]["id"], "quote": quote, "reason": "test"}


def _approve(review):
    return True


def _run(events, judge=None, critic=_approve, **kwargs):
    return cp.run(events, {}, judge=judge, critic=critic if judge else None, now=NOW, **kwargs)


DELIVERS = {"Send Riya your mailing address": ("delivered", "12 Example Lane, Springfield")}


def test_an_answer_that_delivers_closes_the_task_with_its_quote(tmp_path):
    _task(tmp_path)
    judge = Judge(DELIVERS)
    assert _run([_sent(ADDRESS)], judge) == {"judged": 1, "closed": 1, "hinted": 0}
    closed = _row(tmp_path)
    assert closed["status"] == "resolved" and closed["resolution"] == "replied"
    assert closed["resolution_source_refs"][0]["snippet"] == "12 Example Lane, Springfield"
    assert closed["closed_at"].startswith("2026-11-05T17:05:38")
    task, messages = judge.calls[0]
    assert task == {"ask": "Riya wants to post sweets", "deliverable": "Send Riya your mailing address",
                    "source_quote": "What's your mailing address?"}
    assert [m["text"] for m in messages] == [ADDRESS]
    assert loop_checks.read() == {}


@pytest.mark.parametrize("text,verdict", [("will do", "not"), ("congrats!!", "partial")])
def test_an_acknowledgement_or_a_partial_answer_leaves_it_open_with_a_fresh_hint(tmp_path, text, verdict):
    row = _task(tmp_path)
    before = (tmp_path / "knowledge/continuity/k.md").read_bytes()
    judge = Judge({"Send Riya your mailing address": (verdict, text)})
    assert _run([_sent(text)], judge) == {"judged": 1, "closed": 0, "hinted": 1}
    assert (tmp_path / "knowledge/continuity/k.md").read_bytes() == before
    assert loop_checks.read()[loop_version(row)] == {
        "basis": "you_messaged", "at": "2026-11-05T17:05:38+00:00", "source": "imessage"}


def test_a_delivered_verdict_still_needs_the_critic_and_a_real_quote(tmp_path):
    _task(tmp_path)
    assert _run([_sent(ADDRESS)], Judge(DELIVERS), critic=lambda review: False) == {
        "judged": 1, "closed": 0, "hinted": 1}
    invented = Judge({"Send Riya your mailing address": ("delivered", "44 Invented Road, Shelbyville")})
    assert _run([_sent(ADDRESS)], invented) == {"judged": 1, "closed": 0, "hinted": 1}
    no_quote = Judge({"Send Riya your mailing address": ("delivered", "")})
    assert _run([_sent(ADDRESS)], no_quote) == {"judged": 1, "closed": 0, "hinted": 1}
    assert _row(tmp_path)["status"] == "open"


def test_the_critic_sees_the_quote_inside_the_message_the_owner_sent(tmp_path):
    _task(tmp_path)
    seen = []
    _run([_sent(ADDRESS)], Judge(DELIVERS), critic=lambda review: seen.append(review) or True)
    review = seen[0]
    assert review["task"] == "Send Riya your mailing address" and review["status"] == "resolved"
    assert review["evidence"] == [{
        "sourceType": "imessage", "sourceId": review["evidence"][0]["sourceId"],
        "proposed_quote": "12 Example Lane, Springfield", "original_context": ADDRESS,
        "date": "2026-11-05 17:05:38", "message_sender_role": "owner"}]


def test_an_answer_to_one_task_closes_only_that_task(tmp_path):
    _task(tmp_path)
    deck = _task(tmp_path, key="k2", summary="Riya asked for feedback on her deck",
                 deliverable="Send Riya feedback on her deck",
                 source_refs=[{"sourceType": "imessage", "sourceId": "m-2", "snippet": "Thoughts on the deck?"}])
    judge = Judge(DELIVERS)
    assert _run([_sent(ADDRESS)], judge) == {"judged": 2, "closed": 1, "hinted": 1}
    assert _row(tmp_path)["status"] == "resolved"
    assert _row(tmp_path, "k2")["status"] == "open"
    assert sorted(task["deliverable"] for task, _messages in judge.calls) == [
        "Send Riya feedback on her deck", "Send Riya your mailing address"]
    assert list(loop_checks.read()) == [loop_version(deck)]


def test_a_message_to_someone_else_or_before_the_ask_is_not_judged(tmp_path):
    _task(tmp_path)
    judge = Judge(DELIVERS)
    events = [_sent(ADDRESS, handle="+14155550199"), _sent(ADDRESS, when="2026-11-02 09:00:00")]
    assert _run(events, judge) == {"judged": 0, "closed": 0, "hinted": 0}
    assert judge.calls == []


def test_a_group_thread_never_closes_or_hints_a_direct_task(tmp_path):
    _task(tmp_path)
    judge = Judge(DELIVERS)
    room = {"is_group_chat": True, "chat_guid": "iMessage;+;chat555"}
    events = [_sent(ADDRESS, **{**room, "is_from_me": False}),   # someone else answers in the group
              _sent(ADDRESS, rowid=12, **room)]                   # the owner speaks in the group
    assert _run(events, judge) == {"judged": 0, "closed": 0, "hinted": 0}
    assert judge.calls == [] and _row(tmp_path)["status"] == "open"


@pytest.mark.parametrize("fields", [
    {"status": "waiting", "action_type": "waiting_on"},
    {"resolution_mode": "explicit"},
    {"source_refs": None, "content_provenance": None, "channel": "manual"},
])
def test_waiting_on_explicit_and_owner_tasks_are_never_touched(tmp_path, fields):
    _task(tmp_path, **fields)
    before = (tmp_path / "knowledge/continuity/k.md").read_bytes()
    judge = Judge(DELIVERS)
    assert _run([_sent(ADDRESS)], judge) == {"judged": 0, "closed": 0, "hinted": 0}
    assert judge.calls == [] and (tmp_path / "knowledge/continuity/k.md").read_bytes() == before


def test_a_second_run_changes_nothing(tmp_path):
    _task(tmp_path)
    judge = Judge(DELIVERS)
    _run([_sent(ADDRESS)], judge)
    before = (tmp_path / "knowledge/continuity/k.md").read_bytes()
    assert _run([_sent(ADDRESS)], judge) == {"judged": 0, "closed": 0, "hinted": 0}
    assert len(judge.calls) == 1 and (tmp_path / "knowledge/continuity/k.md").read_bytes() == before


def test_a_closed_task_reopens_when_they_ask_again(tmp_path):
    _task(tmp_path)
    _run([_sent(ADDRESS)], Judge(DELIVERS))
    ask = {"handle": RIYA, "timestamp": "2026-11-07 09:00:00", "is_from_me": False, "rowid": 20,
           "text": "That bounced. Can you send the full address with the zip code?"}
    action = {"type": "reply", "channel": "imessage", "contactName": "Riya Example", "contactIdentifier": RIYA,
              "loopId": "k", "contextSummary": "Riya needs the full address",
              "contextAsk": "Can you send the full address with the zip code?",
              "evidence": [{"sourceType": "imessage", "sourceId": "20",
                            "snippet": "Can you send the full address with the zip code?"}]}
    cr.resolve({"today": "2026-11-08", "local": {"imessage": [ask]}, "new_actions": [action]},
               datetime(2026, 11, 8, 9, tzinfo=timezone.utc))
    assert _row(tmp_path)["status"] == "open"


def test_without_a_judge_the_pass_only_writes_the_hint_at_send_time(tmp_path):
    row = _task(tmp_path)
    before = (tmp_path / "knowledge/continuity/k.md").read_bytes()
    assert cp.run([_sent(ADDRESS)], {}, now=NOW) == {"judged": 0, "closed": 0, "hinted": 1}
    assert (tmp_path / "knowledge/continuity/k.md").read_bytes() == before
    assert loop_checks.read()[loop_version(row)]["basis"] == "you_messaged"
    assert ADDRESS not in (tmp_path / "events/loop-checks.json").read_text()


def test_a_fresh_hint_keeps_the_other_notes(tmp_path):
    row = _task(tmp_path)
    other = {"basis": "meeting", "at": "2026-11-09T11:00:00+00:00", "source": "calendar"}
    loop_checks.record({"other-version": other})
    cp.run([_sent(ADDRESS)], {}, now=NOW)
    assert loop_checks.read() == {"other-version": other, loop_version(row): {
        "basis": "you_messaged", "at": "2026-11-05T17:05:38+00:00", "source": "imessage"}}


def test_learn_keeps_a_hint_written_after_it_read_the_notes(tmp_path, monkeypatch):
    row = _task(tmp_path)
    record = loop_checks.record

    def interleave(checks, **kwargs):
        assert _run([_sent(ADDRESS)])["hinted"] == 1
        record(checks, **kwargs)

    monkeypatch.setattr(loop_checks, "record", interleave)
    cp.loop_check.run({}, NOW)
    assert loop_checks.read()[loop_version(row)]["at"] == "2026-11-05T17:05:38+00:00"


def test_a_late_old_event_does_not_replace_the_newer_hint(tmp_path):
    row = _task(tmp_path)
    _run([_sent(ADDRESS)])
    _run([_sent("Older reply", when="2026-11-04 17:05:38", rowid=10)])
    assert loop_checks.read()[loop_version(row)]["at"] == "2026-11-05T17:05:38+00:00"


def test_hint_uses_bridge_utc_to_disambiguate_repeated_hour(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_TIMEZONE", "America/Los_Angeles")
    row = _task(tmp_path, created_at="2026-11-01T08:45:00Z")
    event = _sent(ADDRESS, when="2026-11-01 01:30:00", timestamp_utc="2026-11-01T09:30:00Z")
    event.pop("rowid")
    source_id = cr._render_local.message_evidence_id(event, "imessage")
    assert _run([event])["hinted"] == 1
    assert loop_checks.read()[loop_version(row)]["at"] == "2026-11-01T01:30:00-08:00"
    assert _run([event], Judge(DELIVERS))["closed"] == 1
    assert _row(tmp_path)["resolution_source_refs"][0]["sourceId"] == source_id


def test_triage_runs_the_pass_for_the_owners_own_message(tmp_path):
    te_spec = importlib.util.spec_from_file_location(
        "te_closure", os.path.join(ROOT, "event-triage", "scripts", "triage_event.py"))
    te = importlib.util.module_from_spec(te_spec)
    te_spec.loader.exec_module(te)
    row = _task(tmp_path)
    out = te.triage({"events": [_sent(ADDRESS)]}, now_local=datetime(2026, 11, 5, 17, 6),
                    now_utc=datetime(2026, 11, 5, 17, 6, tzinfo=timezone.utc))
    assert out["verdict"] == "queue"
    assert loop_checks.read()[loop_version(row)]["basis"] == "you_messaged"
    assert _row(tmp_path)["status"] == "open"


def test_a_close_goes_through_the_source_resolution_writer_and_keeps_provenance(tmp_path):
    from ledger_provenance import digest_frontmatter
    row = _task(tmp_path)
    row["content_provenance"] = {"version": 1, "kind": "sources", "sources": ["imessage"],
                                 "digest": digest_frontmatter(row)}
    (tmp_path / "knowledge/continuity/k.md").write_text("---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    assert _run([_sent(ADDRESS)], Judge(DELIVERS))["closed"] == 1
    assert cr._valid_provenance(_row(tmp_path)) == ("sources", frozenset({"imessage"}))


@pytest.mark.parametrize(("their_time", "hinted"), [("2026-11-05 17:30:00", 0), ("2026-11-05 16:30:00", 1)])
def test_their_newer_message_in_the_same_batch_leaves_no_hint(tmp_path, their_time, hinted):
    te_spec = importlib.util.spec_from_file_location(
        "te_closure_batch", os.path.join(ROOT, "event-triage", "scripts", "triage_event.py"))
    te = importlib.util.module_from_spec(te_spec)
    te_spec.loader.exec_module(te)
    row = _task(tmp_path)
    theirs = _sent("Still need your address, can you send it?", when=their_time, rowid=12, is_from_me=False)
    te.triage({"events": [_sent(ADDRESS), theirs]}, now_local=datetime(2026, 11, 5, 17, 31),
              now_utc=datetime(2026, 11, 5, 17, 31, tzinfo=timezone.utc))
    assert (loop_version(row) in loop_checks.read()) is bool(hinted)
    assert _row(tmp_path)["status"] == "open"
