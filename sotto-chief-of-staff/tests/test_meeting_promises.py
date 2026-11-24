"""Meeting promises become proper tasks (Nov 12, 2026). Granola names each participant from the
meeting's own invite; that name lets "they'll send Y" bind to exactly one attendee, and a meeting
row keeps its grounded deliverable and direction. A meeting promise carries the same capture fields
as a message task, so an intro, scheduling or callback promise closes on its own signal (Nov 13)."""
import glob
import importlib.util
import os
from datetime import datetime, timezone

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gg = _load("gather_granola_mp", "_shared/scripts/gather_granola.py")
ac = _load("apply_commitments_mp", "followup/scripts/apply_commitments.py")
lc = _load("loop_check_mp", "morning-brief/scripts/loop_check.py")

OWNER = "owner@example.net"
DANA = "dk@deltalabs.test"
CASEY = "casey@northstarlabs.test"
NOW = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)
END = "2026-11-05T18:00:00+00:00"


def _meeting(participants, transcript):
    """The current Granola MCP markup, through the real salvage and mapping."""
    markup = (f'<meeting id="m-1" title="Model review" date="Nov 5, 2026 5:00 PM UTC">'
              f"<known_participants>{participants}</known_participants></meeting>")
    return {**gg.map_meeting(gg._salvage_meeting_markup(markup)[0]), "end": END, "transcript": transcript}


def _rows(tmp_path):
    out = []
    for path in glob.glob(os.path.join(str(tmp_path), "knowledge", "continuity", "*.md")):
        text = open(path, encoding="utf-8").read()
        out.append(yaml.safe_load(text[4:text.find("\n---", 4)]))
    return out


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setenv("SOTTO_USER_EMAIL", OWNER)
    monkeypatch.delenv("SOTTO_USER_NAME", raising=False)


def test_granola_markup_keeps_each_participant_name_from_the_invite():
    meeting = _meeting("Owner Person (note creator) from Delta Labs (owner@example.net), "
                       f"Dana Kim from Delta Labs ({DANA}), Casey &lt;{CASEY}&gt;, lee@example.net", "")
    assert meeting["attendee_emails"] == [OWNER, DANA, CASEY, "lee@example.net"]
    assert [a["name"] for a in meeting["attendees"]] == ["Owner Person", "Dana Kim", "Casey", ""]
    mapped = gg.map_meeting({"id": "m-1", "start_time": END})
    gg._merge_content(mapped, gg._salvage_meeting_markup(
        f'<meeting id="m-1" title="t"><known_participants>Dana Kim ({DANA})</known_participants></meeting>')[0])
    assert mapped["attendees"] == [{"email": DANA, "name": "Dana Kim"}]


def _apply(meeting, **commitment):
    return ac.apply({"commitments": [{"meeting": "Model review", "meeting_id": "m-1", **commitment}]},
                    OWNER, NOW, source_meetings=[meeting])


def test_an_email_only_attendee_named_by_the_invite_owes_what_they_said(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    meeting = _meeting(f"Owner Person ({OWNER}), Dana Kim ({DANA})", "Dana: I'll send the model Friday.")
    result = _apply(meeting, owner="Dana", owner_is_user=False, what="send the model", due="Friday",
                    source_snippet="Dana: I'll send the model Friday.")
    assert result["written"] == 1
    [row] = _rows(tmp_path)
    assert row["action_type"] == "waiting_on" and row["contact_identifier"] == DANA


def test_two_attendees_who_could_have_said_it_make_no_task(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    meeting = _meeting(f"Owner Person ({OWNER}), Dana Kim ({DANA}), Dana Ruiz (dana@northstarlabs.test)",
                       "Dana: I'll send the model Friday.")
    result = _apply(meeting, owner="Dana", owner_is_user=False, what="send the model",
                    source_snippet="Dana: I'll send the model Friday.")
    assert result["written"] == 0 and result["grounding"]["reasons"]["owner"] == 1
    assert _rows(tmp_path) == []


INTRO = {"owner": "you", "owner_is_user": True, "what": "intro Dana to Casey",
         "source_snippet": "Owner: I'll intro Dana to Casey.", "fulfillment": "intro",
         "deliver_to": CASEY, "deliverable": "Intro Dana to Casey", "owed_by": "owner"}


def _intro_meeting():
    return _meeting(f"Owner Person ({OWNER}), Dana Kim ({DANA}), Casey ({CASEY})",
                    INTRO["source_snippet"])


def _apply_intro(meeting, **fields):
    return _apply(meeting, **{**INTRO, "to_email": DANA, **fields})


def test_an_owner_promise_is_a_you_owe_task_with_the_capture_fields(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    assert _apply_intro(_intro_meeting())["written"] == 1
    [row] = _rows(tmp_path)
    assert row["action_type"] == "follow_up" and row["contact_identifier"] == DANA
    assert (row["deliverable"], row["owed_by"]) == ("Intro Dana to Casey", "owner")
    assert (row["fulfillment"], row["deliver_to"]) == ("intro", CASEY)
    assert row["source_refs"] == [{"source": "granola", "source_id": "m-1",
                                   "snippet": INTRO["source_snippet"], "timestamp": END}]


def test_meeting_promise_preserves_explicit_none_and_discards_the_signal_target(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    assert _apply_intro(_intro_meeting(), fulfillment="none")["written"] == 1
    row, = _rows(tmp_path)
    assert row["fulfillment"] == "none"
    assert "deliver_to" not in row


def test_a_deliverable_whose_owed_by_contradicts_the_type_is_dropped(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    assert _apply_intro(_intro_meeting(), owed_by="them")["written"] == 1
    [row] = _rows(tmp_path)
    assert row["action_type"] == "follow_up"
    assert "deliverable" not in row and "owed_by" not in row
    assert "fulfillment" not in row and "deliver_to" not in row


def _email(date, message_id):
    return {"id": message_id, "messageId": message_id, "threadId": message_id, "from": OWNER,
            "to": CASEY, "cc": DANA, "labelIds": ["SENT"], "date": date, "subject": "Dana / Casey"}


CARDS = [{"name": "Dana Kim", "emails": [DANA], "phones": ["+12025550123"]},
         {"name": "Casey", "emails": [CASEY]}]


def test_an_intro_promised_in_a_meeting_closes_on_the_intro_email_sent_after_it(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _apply_intro(_intro_meeting())
    before = _email("Thu, 05 Nov 2026 09:00:00 -0000", "e-before")
    assert lc.run({"local": {"contacts": CARDS, "emails": [before]}}, now=NOW)["closed"] == 0
    assert _rows(tmp_path)[0]["status"] == "open"
    after = _email("Fri, 06 Nov 2026 09:00:00 -0000", "e-after")
    assert lc.run({"local": {"contacts": CARDS, "emails": [before, after]}}, now=NOW)["closed"] == 1
    [row] = _rows(tmp_path)
    assert row["status"] == "resolved" and row["resolution"] == "intro_sent"


def _promise(tmp_path, monkeypatch, line, what, **fields):
    _env(tmp_path, monkeypatch)
    meeting = _meeting(f"Owner Person ({OWNER}), Dana Kim ({DANA})", line)
    assert _apply(meeting, owner="you", owner_is_user=True, what=what, source_snippet=line,
                  **fields)["written"] == 1
    [row] = _rows(tmp_path)
    return row


def test_a_scheduling_promise_closes_when_a_small_one_off_meeting_is_booked_after_it(tmp_path, monkeypatch):
    row = _promise(tmp_path, monkeypatch, "Owner: I'll find time with Dana next week.",
                   "find time with Dana next week", fulfillment="schedule", owed_by="owner")
    assert (row["fulfillment"], row["owed_by"]) == ("schedule", "owner")
    event = {"id": "ev-1", "summary": "Dana / Owner", "status": "confirmed",
             "created": "2026-11-06T09:00:00Z", "start": "2026-11-10T17:00:00Z",
             "attendees": [{"email": DANA, "responseStatus": "accepted"},
                           {"email": OWNER, "responseStatus": "accepted", "self": True}],
             "organizer": {"email": OWNER, "self": True}}
    assert lc.run({"local": {"contacts": CARDS}, "events": [{**event, "created": "2026-11-04T09:00:00Z"}]},
                  now=NOW)["closed"] == 0
    assert lc.run({"local": {"contacts": CARDS}, "events": [event]}, now=NOW)["closed"] == 1
    assert _rows(tmp_path)[0]["resolution"] == "scheduled_meeting"


def test_a_callback_promise_closes_on_a_connected_call_after_it(tmp_path, monkeypatch):
    row = _promise(tmp_path, monkeypatch, "Owner: I'll call you tomorrow.", "call you tomorrow",
                   fulfillment="callback")
    assert row["fulfillment"] == "callback"
    call = {"phone": "+1 (202) 555-0123", "timestamp": "2026-11-06 08:00:00", "is_outgoing": False,
            "is_answered": True, "call_type": "phone", "duration_seconds": 125}
    assert lc.run({"local": {"contacts": CARDS, "calls": [{**call, "timestamp": "2026-11-04 08:00:00"}]}},
                  now=NOW)["closed"] == 0
    assert lc.run({"local": {"contacts": CARDS, "calls": [call]}}, now=NOW)["closed"] == 1
    assert _rows(tmp_path)[0]["resolution"] == "called"


def test_a_promise_with_no_capture_fields_stores_as_before(tmp_path, monkeypatch):
    row = _promise(tmp_path, monkeypatch, "Owner: I'll call you tomorrow.", "call you tomorrow")
    assert not {"fulfillment", "deliver_to", "deliverable", "owed_by"} & set(row)
    call = {"phone": "+1 (202) 555-0123", "timestamp": "2026-11-06 08:00:00", "is_outgoing": False,
            "is_answered": True, "call_type": "phone", "duration_seconds": 125}
    assert lc.run({"local": {"contacts": CARDS, "calls": [call]}}, now=NOW)["closed"] == 0


@pytest.mark.parametrize("fields,kept", [
    ({"fulfillment": "maybe", "owed_by": "nobody"}, {}),
    ({"fulfillment": "intro", "deliver_to": "lee@example.net"}, {}),  # never named
])
def test_an_invalid_capture_value_is_dropped(tmp_path, monkeypatch, fields, kept):
    row = _promise(tmp_path, monkeypatch, "Owner: I'll intro you to Casey.", "intro you to Casey", **fields)
    assert {k: row[k] for k in ("fulfillment", "deliver_to", "owed_by") if k in row} == kept


def test_the_meeting_prompt_asks_for_the_message_capture_fields():
    text = open(os.path.join(ROOT, "followup", "references", "followup-prompt.md"), encoding="utf-8").read()
    for field in ('"fulfillment"', '"deliver_to"', '"deliverable"', '"owed_by"'):
        assert field in text
    assert '"intro"' in text and '"schedule"' in text and '"callback"' in text and '"none"' in text



@pytest.mark.parametrize("label", ["Dana", "Dana Kim"])
@pytest.mark.parametrize("owner,is_user", [("you", True), ("Dana", False), ("Dana Kim", False)])
def test_owner_and_attendee_with_same_display_name_cannot_supply_a_speaker(
        tmp_path, monkeypatch, label, owner, is_user):
    _env(tmp_path, monkeypatch)
    snippet = f"{label}: I'll send the model Friday."
    meeting = _meeting(f"Dana Kim ({OWNER}), Dana Kim ({DANA})", snippet)
    assert _apply(meeting, owner=owner, owner_is_user=is_user, what="send the model",
                  source_snippet=snippet)["written"] == 0
    assert _rows(tmp_path) == []


def test_detail_fills_names_only_for_the_same_list_attendee(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    mapped = gg.map_meeting({"id": "m-1", "start_time": END, "attendees": [
        {"email": OWNER, "name": "Owner Person"}, DANA,
    ]})
    gg._merge_content(mapped, {"id": "m-1", "attendees": [
        {"email": OWNER, "name": "Other Owner"}, {"email": DANA.upper(), "name": "Dana Kim"},
        {"email": CASEY, "name": "Casey"},
    ]})
    assert mapped["attendees"] == [{"email": OWNER, "name": "Owner Person"}, {"email": DANA, "name": "Dana Kim"}]
    snippet = "Dana: I'll send the model Friday."
    assert _apply({**mapped, "transcript": snippet}, owner="Dana", owner_is_user=False,
                  what="send the model", source_snippet=snippet)["written"] == 1
    assert _rows(tmp_path)[0]["contact_identifier"] == DANA


@pytest.mark.parametrize("kind", ["intro", "callback", "schedule"])
def test_unrelated_signal_cannot_close_a_promise_to_send_the_model(tmp_path, monkeypatch, kind):
    _env(tmp_path, monkeypatch)
    snippet = "Owner: I'll send the model Friday."
    meeting = _meeting(f"Owner Person ({OWNER}), Dana Kim ({DANA}), Casey ({CASEY})", snippet)
    assert _apply_intro(meeting, what="send the model", source_snippet=snippet, fulfillment=kind)["written"] == 1
    after = _email("Fri, 06 Nov 2026 09:00:00 -0000", "e-after")
    call = {"phone": "+12025550123", "timestamp": "2026-11-06 08:00:00", "is_outgoing": True,
            "is_answered": True, "call_type": "phone", "duration_seconds": 125}
    event = {"id": "ev-1", "status": "confirmed", "created": "2026-11-06T09:00:00Z",
             "start": "2026-11-10T17:00:00Z", "organizer": {"email": OWNER, "self": True},
             "attendees": [{"email": DANA, "responseStatus": "accepted"},
                           {"email": OWNER, "responseStatus": "accepted", "self": True}]}
    assert lc.run({"local": {"contacts": CARDS, "emails": [after], "calls": [call]},
                   "events": [event]}, now=NOW)["closed"] == 0
    [row] = _rows(tmp_path)
    assert row["status"] == "open" and row["ask"] == "send the model"
    assert not {"fulfillment", "deliver_to", "deliverable"} & set(row)


def test_intro_to_someone_else_does_not_arm_closure(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    meeting = _meeting(f"Owner Person ({OWNER}), Dana Kim ({DANA}), Casey ({CASEY}), Lee (lee@example.net)",
                       INTRO["source_snippet"])
    assert _apply_intro(meeting, deliver_to="lee@example.net")["written"] == 1
    assert "fulfillment" not in _rows(tmp_path)[0]


def test_unverified_import_cannot_arm_closure(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    assert ac.apply({"commitments": [{**INTRO, "to_email": DANA}]}, OWNER, NOW)["written"] == 1
    assert "fulfillment" not in _rows(tmp_path)[0]


def test_a_connected_call_cannot_close_a_compound_promise(tmp_path, monkeypatch):
    row = _promise(tmp_path, monkeypatch, "Owner: I'll call you on Friday and send the model.",
                   "call you on Friday and send the model", fulfillment="callback")
    assert "fulfillment" not in row


def test_a_semicolon_does_not_end_the_promise(tmp_path, monkeypatch):
    row = _promise(tmp_path, monkeypatch, "Owner: I'll call you tomorrow; and send the model.",
                   "call you tomorrow", fulfillment="callback")
    assert "fulfillment" not in row


def test_someone_elses_promise_does_not_arm_an_owner_close(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    quote = "Lee: I'll intro Dana to Casey.\nOwner: I'll send the model."
    meeting = _meeting(f"Owner Person ({OWNER}), Dana Kim ({DANA}), Casey ({CASEY}), Lee (lee@example.net)",
                       quote)
    assert _apply_intro(meeting, source_snippet=quote)["written"] == 1
    assert "fulfillment" not in _rows(tmp_path)[0]


def test_an_intro_can_bind_a_non_attendee_by_quoted_email(tmp_path, monkeypatch):
    quote = f"Owner: I'll intro you to {CASEY}."
    row = _promise(tmp_path, monkeypatch, quote, f"intro you to {CASEY}",
                   fulfillment="intro", deliver_to=CASEY)
    assert (row["fulfillment"], row["deliver_to"]) == ("intro", CASEY)
