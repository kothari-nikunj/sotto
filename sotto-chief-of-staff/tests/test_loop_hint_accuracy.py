"""A hint must fit the ask: a calendar meeting hints only a scheduling ask, and a WhatsApp call
that was not missed hints any task you owe them. Neither ever closes a task."""
import importlib.util
import json
import os
from datetime import datetime, timezone

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
spec = importlib.util.spec_from_file_location(
    "loop_check_hint_accuracy", os.path.join(ROOT, "morning-brief", "scripts", "loop_check.py"))
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)
import loop_checks  # noqa: E402
from delivery_effects import loop_version  # noqa: E402

OWNER, MINA = "owner@example.net", "mina.park@example.net"


def _setup(tmp_path, monkeypatch, **fields):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setenv("SOTTO_USER_EMAIL", OWNER)
    directory = tmp_path / "knowledge/continuity"
    directory.mkdir(parents=True, exist_ok=True)
    row = {"anchor_key": "k", "status": "open", "action_type": "reply", "channel": "whatsapp",
           "contact_name": "Mina Park", "contact_identifier": "+12025550171",
           "created_at": "2026-11-11 10:00:00", "summary": "Call Mina back today", **fields}
    path = directory / "k.md"
    path.write_text("---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    return row, path


def _note(row):
    return loop_checks.read().get(loop_version(row))


def _wa_call(**extra):
    return {"jid": "12025550171@s.whatsapp.net", "timestamp": "2026-11-11 11:25:55",
            "timestamp_utc": "2026-11-11T11:25:55Z", "is_outgoing": True, "is_missed": False, **extra}


EVENT = {"summary": "Quarterly planning", "status": "confirmed",
         "start": {"dateTime": "2026-11-12T09:30:00+00:00"},
         "attendees": [{"email": "theo.lund@example.net", "responseStatus": "accepted"},
                       {"email": MINA, "responseStatus": "accepted"},
                       {"email": OWNER, "responseStatus": "accepted"}]}


def test_a_meeting_that_includes_them_is_no_hint_for_a_task_that_is_not_a_scheduling_ask(tmp_path, monkeypatch):
    row, _path = _setup(tmp_path, monkeypatch, channel="gmail", contact_identifier=MINA,
                        summary="Review the startup materials Mina forwarded")
    assert lc.run({"local": {}, "events": [EVENT]}) == {"checked": 0, "closed": 0}
    assert _note(row) is None


def test_the_same_meeting_still_hints_a_scheduling_ask(tmp_path, monkeypatch):
    row, path = _setup(tmp_path, monkeypatch, channel="gmail", contact_identifier=MINA,
                       fulfillment="schedule", summary="Find a time with Mina")
    before = path.read_bytes()
    assert lc.run({"local": {}, "events": [EVENT]}) == {"checked": 1, "closed": 0}
    assert _note(row) == {"basis": "meeting", "at": "2026-11-12T09:30:00+00:00", "source": "calendar"}
    assert path.read_bytes() == before


def test_a_whatsapp_call_after_a_callback_ask_is_a_hint_and_never_a_close(tmp_path, monkeypatch):
    row, path = _setup(tmp_path, monkeypatch, fulfillment="callback")
    before = path.read_bytes()
    assert lc.run({"local": {"whatsapp_calls": [_wa_call()]}}) == {"checked": 1, "closed": 0}
    assert _note(row) == {"basis": "call", "at": "2026-11-11T11:25:55+00:00", "source": "whatsapp_calls"}
    assert path.read_bytes() == before and yaml.safe_load(path.read_text().split("---")[1])["status"] == "open"
    phrase = loop_checks.note_for(row, loop_checks.read())
    assert phrase == "maybe done: there was a WhatsApp call with them on 2026-11-11"
    answered = _wa_call(is_outgoing=False)
    assert lc.run({"local": {"whatsapp_calls": [answered]}}) == {"checked": 1, "closed": 0}


def test_a_whatsapp_call_that_does_not_fit_gives_no_hint(tmp_path, monkeypatch):
    row, _path = _setup(tmp_path, monkeypatch, fulfillment="callback")
    for call in (_wa_call(is_missed=True), _wa_call(is_missed=None),
                 _wa_call(jid="12025550179@s.whatsapp.net"), _wa_call(jid="12025550171@lid"),
                 _wa_call(timestamp="2026-11-11 09:00:00", timestamp_utc="2026-11-11T09:00:00Z")):
        assert lc.run({"local": {"whatsapp_calls": [call]}}) == {"checked": 0, "closed": 0}, call
    later = {"contact_jid": "12025550171@s.whatsapp.net", "timestamp": "2026-11-11 12:00:00",
             "is_from_me": False, "text": "still waiting", "is_group_chat": False}
    assert lc.run({"local": {"whatsapp_calls": [_wa_call()], "whatsapp": [later]}})["checked"] == 0
    import source_context
    monkeypatch.setattr(source_context, "allowed", lambda source: source != "whatsapp_calls")
    assert lc.run({"local": {"whatsapp_calls": [_wa_call()]}}) == {"checked": 0, "closed": 0}
    assert _note(row) is None


def test_a_whatsapp_call_before_their_later_missed_call_gives_no_hint(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, fulfillment="callback", callback_since="2026-11-11T12:00:00+00:00")
    assert lc.run({"local": {"whatsapp_calls": [_wa_call()]}}) == {"checked": 0, "closed": 0}
    after = _wa_call(timestamp="2026-11-11 12:30:00", timestamp_utc="2026-11-11T12:30:00Z")
    assert lc.run({"local": {"whatsapp_calls": [after]}}) == {"checked": 1, "closed": 0}


def test_a_whatsapp_call_hints_any_task_you_owe_them_and_never_closes_it(tmp_path, monkeypatch):
    row, path = _setup(tmp_path, monkeypatch, summary="Send Mina the pricing document")
    before = path.read_bytes()
    assert lc.run({"local": {"whatsapp_calls": [_wa_call()]}}) == {"checked": 1, "closed": 0}
    assert _note(row) == {"basis": "call", "at": "2026-11-11T11:25:55+00:00", "source": "whatsapp_calls"}
    assert path.read_bytes() == before and yaml.safe_load(path.read_text().split("---")[1])["status"] == "open"
    _setup(tmp_path / "fresh", monkeypatch, summary="Send Mina the pricing document")
    for call in (_wa_call(is_missed=True),
                 _wa_call(timestamp="2026-11-11 09:00:00", timestamp_utc="2026-11-11T09:00:00Z")):
        assert lc.run({"local": {"whatsapp_calls": [call]}}) == {"checked": 0, "closed": 0}, call


def test_a_whatsapp_call_is_no_hint_for_a_task_you_are_waiting_on(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, action_type="waiting_on", status="waiting",
           summary="Mina to send the pricing document")
    assert lc.run({"local": {"whatsapp_calls": [_wa_call()]}}) == {"checked": 0, "closed": 0}


def test_a_saved_whatsapp_call_hint_drops_once_a_later_missed_call_moves_the_callback(tmp_path, monkeypatch):
    row, path = _setup(tmp_path, monkeypatch, fulfillment="callback")
    call = _wa_call(timestamp="2026-11-11 11:00:00", timestamp_utc="2026-11-11T11:00:00Z")
    assert lc.run({"local": {"whatsapp_calls": [call]}}) == {"checked": 1, "closed": 0}
    assert _note(row) == {"basis": "call", "at": "2026-11-11T11:00:00+00:00", "source": "whatsapp_calls"}
    lc.signal_closes.keep_callback(row, "2026-11-11T12:00:00+00:00", lc.cr._message_is_after)
    path.write_text("---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    assert lc.run({"local": {"whatsapp_calls": [call]}}) == {"checked": 0, "closed": 0}
    assert _note(row) is None and yaml.safe_load(path.read_text().split("---")[1])["status"] == "open"
    after = _wa_call(timestamp="2026-11-11 12:30:00", timestamp_utc="2026-11-11T12:30:00Z")
    assert lc.run({"local": {"whatsapp_calls": [call, after]}}) == {"checked": 1, "closed": 0}
    assert _note(row) == {"basis": "call", "at": "2026-11-11T12:30:00+00:00", "source": "whatsapp_calls"}


@pytest.mark.parametrize(("modern", "seconds"), [(False, 12), (False, 35), (True, 12), (True, 35)])
def test_a_phone_call_before_their_later_missed_call_neither_hints_nor_closes(tmp_path, monkeypatch, modern, seconds):
    original = {"phone": "+12025550171", "timestamp": "2026-11-11 10:00:00",
                "is_outgoing": False, "is_answered": False, "call_type": "phone", "duration_seconds": 0}
    initial_id, initial_quote = lc.cr._render_local.call_evidence(original, "calls")
    row, saved = _setup(tmp_path, monkeypatch, action_type="call_back", channel="phone",
                        source_refs=[{"sourceType": "calls", "sourceId": initial_id, "snippet": initial_quote}],
                        **({"fulfillment": "callback"} if modern else {}))
    prior_dial = {"phone": row["contact_identifier"], "timestamp": "2026-11-11 11:00:00",
                  "is_outgoing": True, "is_answered": False, "call_type": "phone", "duration_seconds": seconds}
    if seconds < lc.DIALED_CLOSE_SECONDS:
        assert lc.run({"local": {"calls": [prior_dial]}}) == {"checked": 1, "closed": 0}
        assert _note(row)["source"] == "calls"
    latest = {**prior_dial, "timestamp": "2026-11-11 12:00:00", "is_outgoing": False, "duration_seconds": 0}
    source_id, quote = lc.cr._render_local.call_evidence(latest, "calls")
    now = datetime(2026, 11, 11, 13, tzinfo=timezone.utc)
    action = {"type": "call_back", "channel": "phone", "loopId": row["anchor_key"],
              "contactName": row["contact_name"], "contactIdentifier": row["contact_identifier"],
              "contextSummary": row["summary"], "fulfillment": "callback",
              "evidence": [{"sourceType": "calls", "sourceId": source_id, "snippet": quote}]}
    outcome = lc.cr.resolve({"today": "2026-11-11", "local": {"calls": [latest]}, "new_actions": [action]}, now)
    assert "callback source not observed" not in str(outcome)
    assert yaml.safe_load(saved.read_text().split("---")[1])["callback_since"] == "2026-11-11 12:00:00"
    lc.run({"local": {"calls": [prior_dial, latest]}}, now=now)
    current = yaml.safe_load(saved.read_text().split("---")[1])
    assert (current["status"], _note(current)) == ("open", None)


def test_a_saved_short_dialed_call_hint_drops_once_a_later_missed_call_moves_the_callback(tmp_path, monkeypatch):
    row, path = _setup(tmp_path, monkeypatch, fulfillment="callback", channel="phone")
    dial = lambda stamp: {"phone": "+1 (202) 555-0171", "timestamp": stamp, "is_outgoing": True,  # noqa: E731
                          "is_answered": False, "call_type": "phone", "duration_seconds": 12}
    short = dial("2026-11-11 11:00:00")
    assert lc.run({"local": {"calls": [short]}}) == {"checked": 1, "closed": 0}
    assert _note(row) == {"basis": "call", "at": "2026-11-11T11:00:00+00:00", "source": "calls"}
    lc.signal_closes.keep_callback(row, "2026-11-11T12:00:00+00:00", lc.cr._message_is_after)
    path.write_text("---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    assert lc.run({"local": {"calls": [short]}}) == {"checked": 0, "closed": 0}
    assert lc.run({"local": {}}) == {"checked": 0, "closed": 0}
    assert _note(row) is None and yaml.safe_load(path.read_text().split("---")[1])["status"] == "open"
    after = dial("2026-11-11 12:30:00")
    assert lc.run({"local": {"calls": [short, after]}}) == {"checked": 1, "closed": 0}
    assert _note(row) == {"basis": "call", "at": "2026-11-11T12:30:00+00:00", "source": "calls"}


CALENDAR_NOW = datetime(2026, 11, 12, 14, 30, tzinfo=timezone.utc)
CALENDAR_CURRENT = {"status": "ok", "complete": True,
                    "coverage": {"since": "2026-11-12T14:29:58Z", "until": "2026-11-15T14:29:58Z"}}


@pytest.mark.parametrize("observation", [None, {"status": "unavailable"},
    {**CALENDAR_CURRENT, "status": "partial", "complete": False},
    {**CALENDAR_CURRENT, "coverage": {"since": "2026-11-09T00:00:00Z", "until": "2026-11-10T00:00:00Z"}},
    {**CALENDAR_CURRENT, "coverage": {"since": "not a time", "until": "2026-11-15T14:29:58Z"}}])
def test_missing_partial_or_outdated_calendar_read_cannot_assert_no_meeting(tmp_path, monkeypatch, observation):
    row, _ = _setup(tmp_path, monkeypatch, action_type="propose_times", deadline="2026-11-11")
    lc.run({"calendar_observation": observation}, now=CALENDAR_NOW)
    assert _note(row) is None


def _scheduling_exchange(tmp_path, monkeypatch):
    row, _ = _setup(tmp_path, monkeypatch, action_type="propose_times",
                    channel="imessage", source_refs=[{"sourceType": "imessage", "sourceId": "ask"}])
    address = row["contact_identifier"]
    sent = {"source": "imessage", "handle": address, "is_from_me": True,
            "timestamp": "2026-11-11T11:00:00Z", "text": "Thursday coffee?", "rowid": 101}
    inbound = {**sent, "is_from_me": False, "timestamp": "2026-11-11T12:00:00Z",
               "text": "See you then", "rowid": 102}
    local = {"imessage": [sent]}
    lc.run({"local": local}, now=CALENDAR_NOW)
    return row, local, inbound


def test_live_reply_survives_an_older_sent_only_brief(tmp_path, monkeypatch):
    import closure_pass

    row, local, event = _scheduling_exchange(tmp_path, monkeypatch)
    closure_pass.run([event], {}, now=CALENDAR_NOW)
    saved = _note(row)
    assert saved["replied_since"] == "2026-11-11T12:00:00+00:00"
    lc.run({"local": local}, now=CALENDAR_NOW)
    assert _note(row) == saved
    earlier = {**local["imessage"][0], "timestamp": "2026-11-11T11:30:00Z", "rowid": 104}
    lc.run({"local": {"imessage": [earlier]}}, now=CALENDAR_NOW)
    assert _note(row)["replied_since"] == saved["replied_since"]

    record = loop_checks.record

    def interleave(checks, **kwargs):
        closure_pass.run([{**event, "timestamp": "2026-11-11T13:00:00Z", "rowid": 103}], {},
                         now=CALENDAR_NOW)
        record(checks, **kwargs)

    monkeypatch.setattr(loop_checks, "record", interleave)
    lc.run({"local": local}, now=CALENDAR_NOW)
    assert _note(row)["replied_since"] == "2026-11-11T13:00:00+00:00"



@pytest.mark.parametrize("body", ["", "  "])
def test_an_inbound_reply_without_readable_text_clears_the_hint(tmp_path, monkeypatch, body):
    import closure_pass

    row, _, event = _scheduling_exchange(tmp_path, monkeypatch)
    assert _note(row) is not None
    closure_pass.run([{**event, "text": body}], {}, now=CALENDAR_NOW)
    assert _note(row) is None


def test_cross_source_reply_revocation_cannot_revive_unanswered_hint(tmp_path, monkeypatch):
    row, local, _ = _scheduling_exchange(tmp_path, monkeypatch)
    reply = {"id": "e-ack", "messageId": "e-ack", "from": MINA, "to": OWNER, "labelIds": ["INBOX"],
             "date": "2026-11-11T12:00:00Z", "body": "See you then"}
    local["contacts"] = [{"name": row["contact_name"], "phones": [row["contact_identifier"]], "emails": [MINA]}]
    lc.run({"local": local, "emails": [reply]}, now=CALENDAR_NOW)
    assert _note(row)["replied_source"] == "gmail"

    monkeypatch.setenv("SOTTO_PROACTIVE_CAPTURED_SOURCES", '["imessage"]')
    assert _note(row) is None
    lc.run({"local": local}, now=CALENDAR_NOW)
    assert _note(row) is None
    lc.run({"local": local}, now=CALENDAR_NOW)
    assert _note(row) is None
    later = {**local["imessage"][0], "timestamp": "2026-11-12T09:00:00Z", "rowid": 103}
    lc.run({"local": {"imessage": [later]}}, now=CALENDAR_NOW)
    assert _note(row) == {"basis": "you_messaged", "at": "2026-11-12T09:00:00+00:00", "source": "imessage"}


def test_brief_carries_authority_for_the_reply_it_used(tmp_path, monkeypatch):
    import compose_brief

    row, _, _ = _scheduling_exchange(tmp_path, monkeypatch)
    loop_checks.add(loop_version(row), {"basis": "you_messaged", "at": "2026-11-11T11:00:00Z",
        "source": "imessage", "replied_since": "2026-11-11T12:00:00Z", "replied_source": "gmail"})
    seen = []

    def model(prompt, *_args, **_kwargs):
        seen.append(prompt)
        monkeypatch.setenv("SOTTO_PROACTIVE_CAPTURED_SOURCES", '["imessage"]')
        return json.dumps({"brief_markdown": "## Filtered\nNo noise.", "actions": []})

    out = compose_brief.compose({"type": "morning", "google": {}, "local": {}}, llm=model, critic=False)
    assert "they replied since" in seen[0]
    assert out["_loop_source_permissions"] == ["gmail", "imessage"]
    assert _note(row) is None
