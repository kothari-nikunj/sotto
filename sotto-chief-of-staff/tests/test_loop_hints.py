"""More "maybe done" hints (calls, the person a task is for, Granola meetings); none of them closes a task."""
import importlib.util
import os

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
spec = importlib.util.spec_from_file_location(
    "loop_check_hints", os.path.join(ROOT, "morning-brief", "scripts", "loop_check.py"))
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)
import loop_checks  # noqa: E402
from delivery_effects import loop_version  # noqa: E402


def _setup(tmp_path, monkeypatch, **fields):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    directory = tmp_path / "knowledge/continuity"
    directory.mkdir(parents=True, exist_ok=True)
    row = {"anchor_key": "k", "status": "open", "action_type": "reply", "channel": "imessage",
           "contact_name": "Wesley", "contact_identifier": "+14155550101",
           "created_at": "2026-11-01 10:00:00", "summary": "Did you reply to Marco?", **fields}
    path = directory / "k.md"
    path.write_text("---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    return row, path


def _call(when, **extra):
    return {"phone": "+14155550101", "timestamp": when, "call_type": "phone", "is_outgoing": True,
            "is_answered": False, "duration_seconds": 300, **extra}


def _note(row):
    return loop_checks.read().get(loop_version(row))


def test_a_connected_call_after_the_ask_is_a_hint_and_never_a_close(tmp_path, monkeypatch):
    row, path = _setup(tmp_path, monkeypatch)
    before = path.read_bytes()
    assert lc.run({"local": {"calls": [_call("2026-11-03 09:00:00")]}}) == {"checked": 1, "closed": 0}
    assert _note(row) == {"basis": "call", "at": "2026-11-03T09:00:00+00:00", "source": "calls"}
    assert path.read_bytes() == before
    assert loop_checks.note_for(row, loop_checks.read()) == "maybe done: you spoke by phone on 2026-11-03"


def test_a_missed_call_or_their_later_text_gives_no_call_hint(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    missed = _call("2026-11-03 09:00:00", is_outgoing=False, duration_seconds=0)
    assert lc.run({"local": {"calls": [missed]}})["checked"] == 0
    later = {"handle": "+14155550101", "timestamp": "2026-11-03 10:00:00", "is_from_me": False, "text": "and?"}
    assert lc.run({"local": {"calls": [_call("2026-11-03 09:00:00")], "imessage": [later]}})["checked"] == 0


def test_writing_to_the_person_the_task_is_for_is_a_hint(tmp_path, monkeypatch):
    row, _path = _setup(tmp_path, monkeypatch, deliver_to="marco@sparo.example")
    sent = {"from": "me@fpv.example", "to": "Marco <marco@sparo.example>", "labelIds": ["SENT"],
            "date": "Sun, 01 Nov 2026 18:00:00 +0000", "subject": "Sparo"}
    assert lc.run({"local": {"emails": [sent]}})["checked"] == 1
    assert _note(row)["basis"] == "sent_to_them" and _note(row)["source"] == "gmail"
    assert "the person this was for" in loop_checks.note_for(row, loop_checks.read())


def test_a_text_to_the_person_the_task_is_for_counts_but_a_group_message_does_not(tmp_path, monkeypatch):
    row, _path = _setup(tmp_path, monkeypatch, deliver_to="+14155550199")
    text = {"handle": "+14155550199", "timestamp": "2026-11-02 08:00:00", "is_from_me": True, "text": "Anil?"}
    assert lc.run({"local": {"imessage": [{**text, "is_group_chat": True}]}})["checked"] == 0
    assert lc.run({"local": {"imessage": [text]}})["checked"] == 1
    assert _note(row)["basis"] == "sent_to_them"


def test_a_granola_meeting_with_them_after_the_ask_is_a_hint(tmp_path, monkeypatch):
    """The gather's own shape (map_meeting): day-only `date`, exact time in `start`, passed to the
    loop check as the Learn step's --granola file, never read from the Bridge snapshot."""
    import gather_granola
    row, _path = _setup(tmp_path, monkeypatch, channel="gmail", contact_identifier="wesley@fpv.example",
                        created_at="2026-11-04 10:00:00")
    meeting = gather_granola.map_meeting({"id": "g1", "title": "Sparo", "start_time": "2026-11-04T14:30:00+00:00",
                                          "attendees": [{"email": "me@fpv.example"}, {"email": "wesley@fpv.example"}]})
    assert meeting["date"] == "2026-11-04" and meeting["start"]
    assert lc.run({"local": {"granola_meetings": [meeting]}})["checked"] == 0   # not a snapshot field
    assert lc.run({"local": {}}, granola={"meetings": [meeting]})["checked"] == 1
    assert _note(row) == {"basis": "met", "at": "2026-11-04T14:30:00+00:00", "source": "granola"}
    early = gather_granola.map_meeting({"id": "g0", "title": "Sparo", "start_time": "2026-11-04T08:00:00+00:00",
                                        "attendees": [{"email": "wesley@fpv.example"}]})
    loop_checks.record({})
    assert lc.run({"local": {}}, granola=[early])["checked"] == 0              # same day, before the ask
    import source_context
    monkeypatch.setattr(source_context, "allowed", lambda source: source != "granola")
    loop_checks.record({})
    assert lc.run({"local": {}}, granola={"meetings": [meeting]})["checked"] == 0


def test_the_learn_step_hands_the_granola_file_to_the_loop_check():
    import learn_step
    step = next(s for s in learn_step.STEPS if s[0] == "loopcheck")
    args = type("A", (), {"continuity": "/tmp/c.json", "granola": "/tmp/g.json"})()
    assert step[2](args) == ["/tmp/c.json", "--granola", "/tmp/g.json"]


def test_the_owner_is_never_the_person_a_task_is_for(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@fpv.example")
    sent = {"from": "Me <me+fpv@fpv.example>", "to": "Someone <x@other.example>", "cc": "me+fpv@fpv.example",
            "labelIds": ["SENT"], "date": "Sun, 01 Nov 2026 18:00:00 +0000", "subject": "unrelated"}
    for target in ("me@fpv.example", "me+fpv@fpv.example"):
        row, _path = _setup(tmp_path, monkeypatch, deliver_to=target)
        lc.run({"local": {"emails": [sent]}})
        assert _note(row) is None, target


def test_an_intro_names_only_its_target():
    prompt = open(os.path.join(ROOT, "morning-brief", "references", "extraction-prompt.md"), encoding="utf-8").read()
    assert "For an intro, `deliverTo` is the person being introduced, never the person to check with first." in prompt
    import compose_brief
    described = compose_brief._ACTION_ITEM_SCHEMA["properties"]["deliverTo"]["description"]
    assert "for an intro, only the person being introduced" in described


def test_a_phone_number_as_the_intro_target_never_closes_an_intro(tmp_path, monkeypatch):
    """Only a verified email target on To/Cc closes an intro (#977); a phone target and intro wording
    give at most a hint, never a close."""
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@fpv.example")
    row, path = _setup(tmp_path, monkeypatch, channel="gmail", contact_identifier="adel@example.com",
                       fulfillment="intro", deliver_to="+14155550199", source="gmail",
                       source_refs=[{"sourceType": "email", "sourceId": "m0", "snippet": "intro?"}])
    before = path.read_bytes()
    sent = {"messageId": "m1", "from": "me@fpv.example", "to": "jackson@trainloop.example, adel@example.com",
            "labelIds": ["SENT"], "date": "Mon, 02 Nov 2026 18:00:00 +0000", "subject": "Intro: Jackson / Adel"}
    result = lc.run({"local": {"emails": [sent]}})
    assert result["closed"] == 0 and path.read_bytes() == before
    text = {"handle": "+14155550199", "timestamp": "2026-11-03 08:00:00", "is_from_me": True, "text": "Meet Adel?"}
    assert lc.run({"local": {"emails": [sent], "imessage": [text]}}) == {"checked": 1, "closed": 0}
    assert _note(row)["basis"] == "sent_to_them"


def test_a_call_the_owner_rejected_as_a_close_is_not_a_hint_either(tmp_path, monkeypatch):
    call = _call("2026-11-03 09:00:00")
    call_id, _text = lc.cr._render_local.call_evidence(call, "calls")
    _setup(tmp_path, monkeypatch, rejected_signals=[{"basis": "called", "source_id": call_id}])
    assert lc.run({"local": {"calls": [call]}})["checked"] == 0


def test_one_delivery_closes_the_wait_and_opens_your_next_step(tmp_path, monkeypatch):
    """Morris sends the issues list (closing what you were waiting on) and asks for your positions:
    the same message is completion evidence for one task and request evidence for the next."""
    from datetime import datetime
    row, _path = _setup(tmp_path, monkeypatch, action_type="waiting_on", status="waiting",
                        channel="gmail", contact_name="Thomas", contact_identifier="tmorris@law.example",
                        summary="Issues list coming by Monday", created_at="2026-11-06 10:00:00")
    text = ("Attached is our issues list for the financing documents. "
            "Please send your positions on these points before we mark up the documents.")
    mail = {"id": "m7", "messageId": "m7", "threadId": "t7", "from": "Thomas <tmorris@law.example>",
            "to": "me@fpv.example", "labelIds": ["INBOX"], "date": "Sat, 07 Nov 2026 09:00:00 +0000",
            "subject": "Issues list", "body": text}
    cr = lc.cr
    update = {"loopId": "k", "loopVersion": loop_version(row), "status": "resolved",
              "evidence": [{"sourceType": "email", "sourceId": "m7",
                            "snippet": "Attached is our issues list for the financing documents."}]}
    update["criticReceipt"] = cr.record_critic_approval(update)
    follow_on = {"type": "reply", "channel": "gmail", "contactName": "Thomas",
                 "contactIdentifier": "tmorris@law.example", "newObligation": True,
                 "contextSummary": "Send your positions on the issues list",
                 "evidence": [{"sourceType": "email", "sourceId": "m7",
                               "snippet": "Please send your positions on these points before we mark up the documents."}]}
    out = cr.resolve({"today": "2026-11-08", "local": {"emails": [mail]}, "new_actions": [follow_on],
                      "loop_updates": [update]}, datetime(2026, 11, 8, 9))
    assert [r["anchor_key"] for r in out["resolved"]] == ["k"]
    owed = [r for r in out["active"] if not cr.is_waiting_on(r.get("action_type"))]
    assert len(owed) == 1 and "positions" in owed[0]["summary"]


def test_the_prompt_defines_transitions_and_withdrawn_asks():
    prompt = open(os.path.join(ROOT, "morning-brief", "references", "extraction-prompt.md"), encoding="utf-8").read()
    assert "**When one task leads to the next.**" in prompt
    assert "a settled time is not a booked meeting" in prompt
    assert "withdraws an ask" in prompt
