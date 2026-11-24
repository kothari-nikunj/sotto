"""loop_check.py — an open task you may have handled gets a check note; the ledger never changes."""
import importlib.util
import os

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
spec = importlib.util.spec_from_file_location(
    "loop_check", os.path.join(ROOT, "morning-brief", "scripts", "loop_check.py"))
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)
import loop_checks  # noqa: E402
from delivery_effects import loop_version  # noqa: E402

lq_spec = importlib.util.spec_from_file_location("lq_check", os.path.join(ROOT, "_shared", "scripts", "loops_query.py"))
lq = importlib.util.module_from_spec(lq_spec)
lq_spec.loader.exec_module(lq)


def _setup(tmp_path, monkeypatch, **fields):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    directory = tmp_path / "knowledge/continuity"
    directory.mkdir(parents=True, exist_ok=True)
    row = {"anchor_key": "k", "status": "open", "action_type": "reply", "channel": "imessage",
           "contact_name": "Koushik", "contact_identifier": "+12025550170",
           "created_at": "2026-11-03 10:00:00", "summary": "Pick a time to catch up", **fields}
    path = directory / "k.md"
    path.write_text("---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    return row, path


def _text(handle, when, mine, text="x", **extra):
    return {"handle": handle, "timestamp": when, "is_from_me": mine, "text": text, **extra}


def test_a_later_message_from_you_marks_the_task_for_checking_without_touching_the_ledger(tmp_path, monkeypatch):
    row, path = _setup(tmp_path, monkeypatch)
    before = path.read_bytes()
    local = {"imessage": [_text("+12025550170", "2026-11-05 17:05:38", True,
                                "Coffee at 1 pm tomorrow? Or 930 am on Tuesday?")]}
    assert lc.run({"local": local}) == {"checked": 1, "closed": 0}
    note = loop_checks.read()[loop_version(row)]
    assert note == {"basis": "you_messaged", "at": "2026-11-05T17:05:38+00:00", "source": "imessage"}
    assert "Coffee" not in (tmp_path / "events" / "loop-checks.json").read_text()
    assert path.read_bytes() == before
    entry = lq.query()["you_owe"][0]
    assert entry["check"]["basis"] == "you_messaged"


def test_a_reply_from_them_after_yours_clears_the_note(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    local = {"imessage": [_text("+12025550170", "2026-11-05 17:05:38", True),
                          _text("+12025550170", "2026-11-05 18:00:00", False, "Tuesday works")]}
    assert lc.run({"local": local}) == {"checked": 0, "closed": 0}
    assert "check" not in lq.query()["you_owe"][0]


def test_messages_before_the_ask_or_to_someone_else_do_not_count(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    local = {"imessage": [_text("+12025550170", "2026-11-02 09:00:00", True),
                          _text("+12025550171", "2026-11-05 09:00:00", True)]}
    assert lc.run({"local": local}) == {"checked": 0, "closed": 0}


def test_a_meeting_with_them_after_the_ask_marks_the_task(tmp_path, monkeypatch):
    owner, counterpart = "owner@example.com", "avery@example.com"
    monkeypatch.setenv("SOTTO_USER_EMAIL", owner)
    _setup(tmp_path, monkeypatch, channel="gmail", contact_identifier=counterpart, contact_name="Avery Example",
           fulfillment="schedule")
    event = {"summary": "Project planning", "status": "confirmed",
             "start": {"dateTime": "2026-11-09T11:00:00-08:00"},
             "attendees": [{"email": counterpart, "responseStatus": "accepted"},
                           {"email": owner, "responseStatus": "accepted"}]}
    assert lc.run({"local": {}, "events": [event]}) == {"checked": 1, "closed": 0}
    assert lq.query()["you_owe"][0]["check"] == {
        "basis": "meeting", "at": "2026-11-09T11:00:00-08:00", "source": "calendar"}
    # A cancelled or removed meeting drops the note on the next pass; it is never carried forward.
    assert lc.run({"local": {}, "events": [{**event, "status": "cancelled"}]}) == {"checked": 0, "closed": 0}
    unanswered = {**event, "attendees": [event["attendees"][0],
                                         {"email": owner, "responseStatus": "needsAction", "self": True}]}
    assert lc.run({"local": {}, "events": [unanswered]}) == {"checked": 0, "closed": 0}


def test_declined_or_cancelled_meetings_do_not_count(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, channel="gmail", contact_identifier="mk@example.net")
    me = {"email": "me@fpv.example", "responseStatus": "accepted", "self": True}
    declined = {"summary": "Ivo", "start": {"dateTime": "2026-11-09T11:00:00-08:00"},
                "attendees": [{"email": "mk@example.net", "responseStatus": "declined"}, me]}
    cancelled = {"summary": "Ivo", "status": "cancelled", "start": {"dateTime": "2026-11-09T11:00:00-08:00"},
                 "attendees": [{"email": "mk@example.net", "responseStatus": "accepted"}, me]}
    assert lc.run({"local": {}, "events": [declined, cancelled]}) == {"checked": 0, "closed": 0}


def test_waiting_on_tasks_are_not_marked(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, status="waiting", action_type="waiting_on")
    local = {"imessage": [_text("+12025550170", "2026-11-05 17:05:38", True)]}
    assert lc.run({"local": local}) == {"checked": 0, "closed": 0}


def test_read_receipt_on_the_latest_direct_message_is_waiting_on_context(tmp_path, monkeypatch):
    import closure_pass
    row, path = _setup(tmp_path, monkeypatch, status="waiting", action_type="waiting_on")
    before = path.read_bytes()
    msg = _text("+12025550170", "2026-11-05 17:05:38", True, date_read="2026-11-05T17:10:00Z")
    local = {"imessage": [msg]}
    assert lc.run({"local": local}) == {"checked": 1, "closed": 0}
    check = lq.query()["waiting_on_them"][0]["check"]
    assert check["basis"] == "read_no_reply"
    assert "message_at" not in check
    assert loop_checks.note_for(row, loop_checks.read()) == "they read your message on 2026-11-05"
    assert path.read_bytes() == before
    newer = _text("+12025550170", "2026-11-05 18:00:00", False, "Will check", source="imessage")
    closure_pass.run([newer], {})
    assert "check" not in lq.query()["waiting_on_them"][0]
    assert lc.run({"local": {"imessage": [msg, newer]}}) == {"checked": 0, "closed": 0}


def test_counterpart_reaction_refines_a_sent_message_hint_and_is_not_completion(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    import compose_brief
    import retune_scan
    row, path = _setup(tmp_path, monkeypatch)
    before = path.read_bytes()
    msg = _text("+12025550170", "2026-11-05 17:05:38", True, date_read="2026-11-05T17:10:00Z",
                reactions=[{"kind": "love", "is_from_me": True, "timestamp_utc": "2026-11-05T17:20:00Z"},
                           {"kind": "like", "is_from_me": False, "timestamp_utc": "2026-11-05T17:15:00Z"}])
    assert lc.run({"local": {"imessage": [msg]}}) == {"checked": 1, "closed": 0}
    check = lq.query()["you_owe"][0]["check"]
    assert (check["basis"], check["kind"]) == ("reacted", "like")
    assert loop_checks.note_for(row, loop_checks.read()) == "they reacted (like) to your message on 2026-11-05"
    line = compose_brief._open_loop_line([row])
    assert "they reacted (like)" in line and "tell me if it's done" not in line
    assert retune_scan.looks_done([row], datetime(2026, 11, 13, tzinfo=timezone.utc)) == []
    assert path.read_bytes() == before
    assert lc.run({"local": {"imessage": [{**msg, "reactions": []}]}}) == {"checked": 1, "closed": 0}
    assert lq.query()["you_owe"][0]["check"]["basis"] == "read_no_reply"


def test_protected_gmail_reader_does_not_get_imessage_hints(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, source="gmail", channel="gmail", contact_identifier="dana@example.com")
    msg = _text("dana@example.com", "2026-11-05 17:05:38", True, date_read="2026-11-05T17:10:00Z")
    assert lc.run({"local": {"imessage": [msg]}}) == {"checked": 1, "closed": 0}
    assert lq.query_permitted_rows(str(tmp_path), ["gmail", "imessage"])["you_owe"][0]["check"]["basis"] == "read_no_reply"
    assert "check" not in lq.query_permitted_rows(str(tmp_path), ["gmail"])["you_owe"][0]


def test_a_note_survives_until_they_write_back_and_drops_when_the_task_changes(tmp_path, monkeypatch):
    row, path = _setup(tmp_path, monkeypatch)
    lc.run({"local": {"imessage": [_text("+12025550170", "2026-11-05 17:05:38", True)]}})
    assert lc.run({"local": {}}) == {"checked": 1, "closed": 0}           # message left the 24h snapshot
    changed = {**row, "summary": "Pick a time to catch up next week"}
    path.write_text("---\n" + yaml.safe_dump(changed, sort_keys=False) + "---\n")
    assert "check" not in lq.query()["you_owe"][0]          # a new revision has no note
    assert lc.run({"local": {}}) == {"checked": 0, "closed": 0}


def test_the_brief_prompt_and_still_open_line_carry_the_note(tmp_path, monkeypatch):
    import compose_brief
    import render_local
    from datetime import datetime
    row, _path = _setup(tmp_path, monkeypatch)
    lc.run({"local": {"imessage": [_text("+12025550170", "2026-11-05 17:05:38", True)]}})
    prompt = render_local._format_action_ledger({"action_ledger": [row]}, datetime(2026, 11, 8, 9))
    assert "maybe done: you messaged them on 2026-11-05 (check the source; not proof)" in prompt
    line = compose_brief._open_loop_line([row], datetime(2026, 11, 8, 9))
    assert "maybe done: you messaged them on 2026-11-05; tell me if it's done" in line
    assert line.endswith("<!--loop:k-->")
    loop_checks.record({})
    assert "maybe done" not in compose_brief._open_loop_line([row], datetime(2026, 11, 8, 9))


def test_email_dates_count_and_a_reply_by_email_clears_the_note(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, channel="gmail", contact_identifier="dana@example.com")
    sent = {"from": "me@fpv.example", "to": "dana@example.com", "labelIds": ["SENT"],
            "date": "Thu, 05 Nov 2026 17:05:38 -0800", "subject": "times"}
    assert lc.run({"local": {"emails": [sent]}}) == {"checked": 1, "closed": 0}
    reply = {"from": "Dana <dana@example.com>", "to": "me@fpv.example", "labelIds": ["INBOX"],
             "date": "Fri, 06 Nov 2026 09:00:00 -0800", "subject": "Re: times"}
    assert lc.run({"local": {"emails": [reply]}}) == {"checked": 0, "closed": 0}


def test_a_note_from_a_revoked_source_is_hidden_and_not_carried(tmp_path, monkeypatch):
    import source_context
    row, _path = _setup(tmp_path, monkeypatch)
    lc.run({"local": {"imessage": [_text("+12025550170", "2026-11-05 17:05:38", True)]}})
    monkeypatch.setattr(source_context, "allowed", lambda source: source != "imessage")
    assert "check" not in lq.query()["you_owe"][0]
    assert lc.run({"local": {}}) == {"checked": 0, "closed": 0}


def test_a_note_names_its_task_when_one_line_lists_several(tmp_path, monkeypatch):
    import compose_brief
    from datetime import datetime
    row, _path = _setup(tmp_path, monkeypatch)
    other = {**row, "anchor_key": "k2", "summary": "Send the deck", "ask": "Send the deck"}
    lc.run({"local": {"imessage": [_text("+12025550170", "2026-11-05 17:05:38", True)]}})
    loop_checks.record({loop_version(other): {"basis": "you_messaged", "at": "2026-11-05", "source": "imessage"}})
    line = compose_brief._open_loop_line([row, other], datetime(2026, 11, 8, 9))
    assert "maybe done: you messaged them on 2026-11-05 (Send the deck); tell me if it's done" in line
