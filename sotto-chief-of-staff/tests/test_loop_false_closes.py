"""An automatic close of a real task is the worst outcome, so each of these inputs must leave the
task open (at most a "maybe done" note). From the Nov 2026 adversarial review of the loop signals."""
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
spec = importlib.util.spec_from_file_location(
    "loop_check_false_closes", os.path.join(ROOT, "morning-brief", "scripts", "loop_check.py"))
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)
import compose_brief  # noqa: E402
import compose_brief as cb  # noqa: E402
import continuity_resolve as cr  # noqa: E402
import loop_checks  # noqa: E402
import signal_closes  # noqa: E402
from delivery_effects import loop_version  # noqa: E402
from render_local import call_evidence  # noqa: E402

ASK = "2026-11-03 10:00:00"
NOW = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)


def _setup(tmp_path, monkeypatch, key="k", **fields):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    directory = tmp_path / "knowledge/continuity"
    directory.mkdir(parents=True, exist_ok=True)
    row = {"anchor_key": key, "status": "open", "action_type": "reply", "channel": "gmail",
           "contact_name": "Mina", "contact_identifier": "mina@example.org", "created_at": ASK,
           "summary": "Acme intro call scheduling",
           "source_refs": [{"sourceType": "email", "sourceId": "g-1", "snippet": "Monday 9 or 10?"}],
           **fields}
    (directory / f"{key}.md").write_text("---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    return row


def _row(tmp_path, key="k"):
    return yaml.safe_load((tmp_path / "knowledge/continuity" / f"{key}.md").read_text().split("---")[1])


def _write(tmp_path, row, key="k"):
    row = {k: v for k, v in row.items() if k != "_path"}
    (tmp_path / "knowledge/continuity" / f"{key}.md").write_text(
        "---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")


def _event(**overrides):
    return {"id": "ev-1", "summary": "Acme", "status": "confirmed",
            "created": "2026-11-04T18:00:00Z", "start": "2026-11-09T11:00:00-08:00",
            "attendees": [{"email": "mina@example.org", "responseStatus": "accepted"},
                          {"email": "me@example.com", "responseStatus": "accepted", "self": True}],
            "organizer": {"email": "me@example.com", "self": True}, **overrides}


def _run(local, **payload):
    return lc.run({"local": local, **payload}, now=NOW)


def _call(**o):
    return {"phone": "+1 (415) 555-0123", "timestamp": "2026-11-05 08:00:00", "is_outgoing": False,
            "is_answered": True, "call_type": "phone", "duration_seconds": 125, **o}


# P1 ── one meeting closes two different scheduling tasks for the same person
def test_p1_one_meeting_must_not_close_two_tasks(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, key="a", fulfillment="schedule", summary="Schedule the Acme intro call")
    _setup(tmp_path, monkeypatch, key="b", fulfillment="schedule",
           summary="Set up the board-prep session with Mina and counsel")
    _run({}, events=[_event()])
    closed = [k for k in ("a", "b") if _row(tmp_path, k)["status"] == "resolved"]
    assert closed == [], f"a meeting two tasks could claim closed {closed}"
    notes = loop_checks.read()
    assert all(notes[loop_version(_row(tmp_path, k))]["basis"] == "meeting" for k in ("a", "b"))


# P2 ── a large unrelated one-off meeting (asker is 1 of 40 invitees, someone else organised it)
def test_p2_large_unrelated_event_must_not_close(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, fulfillment="schedule")
    crowd = [{"email": f"p{i}@lp.example.com", "responseStatus": "needsAction"} for i in range(38)]
    event = _event(summary="LP annual meeting webinar", organizer={"email": "events@lp.example.com"},
                   attendees=[{"email": "mina@example.org", "responseStatus": "needsAction"},
                              {"email": "me@example.com", "responseStatus": "tentative", "self": True}, *crowd])
    _run({}, events=[event])
    assert _row(tmp_path)["status"] == "open"


# P3 ── the same phone call gets a different evidence id after a deploy, so an undone close fires again
def test_p3_undone_call_close_must_not_refire_after_revision_change(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, action_type="call_back", channel="phone", contact_name="Adel",
           contact_identifier="+14155550123", summary="Call Adel back",
           source_refs=[{"sourceType": "imessage", "sourceId": "m-1", "snippet": "call me"}])
    monkeypatch.setenv("SOTTO_SKILL_COMPOSITION_PROOF", json.dumps({"source_revision": "a" * 64}))
    assert _run({"calls": [_call()]})["closed"] == 1
    row = _row(tmp_path)
    signal_closes.reopen(row, "2026-11-09", now=NOW)            # the owner says: wrong, reopen
    _write(tmp_path, row)
    assert _run({"calls": [_call()]})["closed"] == 0   # same revision: stays rejected (works)
    monkeypatch.setenv("SOTTO_SKILL_COMPOSITION_PROOF", json.dumps({"source_revision": "b" * 64}))
    assert _run({"calls": [_call()]})["closed"] == 0, "the SAME call closed the task again after undo"


# P3b ── same call, but the Mac reader adds/changes any field on the call row (e.g. resolved name)
def test_p3b_undone_call_close_must_not_refire_when_row_gains_a_field(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, action_type="call_back", channel="phone", contact_name="Adel",
           contact_identifier="+14155550123", summary="Call Adel back",
           source_refs=[{"sourceType": "imessage", "sourceId": "m-1", "snippet": "call me"}])
    assert _run({"calls": [_call()]})["closed"] == 1
    row = _row(tmp_path)
    signal_closes.reopen(row, "2026-11-09", now=NOW)
    _write(tmp_path, row)
    assert _run({"calls": [_call(contact_name="Adel B")]})["closed"] == 0


# P4 ── a dialed call closes a callback only when it lasted 30 seconds or more (owner, Nov 11)
def test_p4_short_dialed_call_must_not_close(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, action_type="call_back", channel="phone", contact_name="Adel",
           contact_identifier="+14155550123", summary="Call Adel back",
           source_refs=[{"sourceType": "imessage", "sourceId": "m-1", "snippet": "call me"}])
    for seconds in (1, 29):   # voicemail pickup / pocket dial
        assert _run({"calls": [_call(is_outgoing=True, is_answered=False, duration_seconds=seconds)]}) == {
            "checked": 1, "closed": 0}
    assert _row(tmp_path)["status"] == "open"
    assert _run({"calls": [_call(is_outgoing=True, is_answered=False, duration_seconds=30)]})["closed"] == 1
    assert _row(tmp_path)["status"] == "resolved"


# P5 ── intro undone; the owner's next message on the same thread closes it again
CARDS = [{"name": "Adel", "phones": ["+14155550123"], "emails": ["adel@example.com"]},
         {"name": "Jack", "emails": ["jack@example.net"]}]


def _mail(mid, date, **o):
    return {"id": mid, "messageId": mid, "threadId": "t-1", "from": "me@example.com",
            "to": "jack@example.net", "cc": "adel@example.com", "labelIds": ["SENT"],
            "date": date, "subject": "Adel / Jack", "body": "x", **o}


def test_p5_undone_intro_must_not_refire_on_same_thread(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, channel="imessage", contact_name="Adel", contact_identifier="+14155550123",
           summary="Intro Adel to Jack", fulfillment="intro", deliver_to="jack@example.net",
           source_refs=[{"sourceType": "imessage", "sourceId": "m-1", "snippet": "intro me?"}])
    mails = [_mail("e-1", "Sat, 07 Nov 2026 17:05:38 -0000"),
             _mail("e-2", "Sat, 07 Nov 2026 18:00:00 -0000", threadId="t-2", subject="Adel, meet Jack")]
    assert _run({"contacts": CARDS, "emails": mails})["closed"] == 1
    row = _row(tmp_path)
    signal_closes.reopen(row, "2026-11-09", now=NOW)
    _write(tmp_path, row)
    assert _run({"contacts": CARDS, "emails": mails})["closed"] == 0, "closed again from the owner's next mail"


# P5b ── a reply-all on an existing thread that merely has both people on it (not an intro)
def test_p5b_reply_all_promising_a_later_intro_must_not_close(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, channel="imessage", contact_name="Adel", contact_identifier="+14155550123",
           summary="Intro Adel to Jack", fulfillment="intro", deliver_to="jack@example.net",
           source_refs=[{"sourceType": "imessage", "sourceId": "m-1", "snippet": "intro me?"}])
    mail = _mail("e-9", "Sat, 07 Nov 2026 17:05:38 -0000", subject="Re: Demo day logistics",
                 to="organizers@demo.example.com", cc="adel@example.com, jack@example.net, guest@example.com",
                 body="Parking is behind the building. See everyone Thursday.")
    _run({"contacts": CARDS, "emails": [mail]})
    assert _row(tmp_path)["status"] == "open"


def test_a_mail_that_is_not_the_first_on_its_thread_is_not_an_intro(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, channel="imessage", contact_name="Adel", contact_identifier="+14155550123",
           summary="Intro Adel to Jack", fulfillment="intro", deliver_to="jack@example.net",
           source_refs=[{"sourceType": "imessage", "sourceId": "m-1", "snippet": "intro me?"}])
    earlier = {"id": "e-0", "messageId": "e-0", "threadId": "t-1", "from": "guest@example.com",
               "to": "me@example.com", "labelIds": ["INBOX"], "date": "Sat, 07 Nov 2026 16:00:00 -0000",
               "subject": "Demo day logistics", "body": "Where do we park?"}
    sent = _mail("e-1", "Sat, 07 Nov 2026 17:05:38 -0000", subject="Demo day logistics")
    assert _run({"contacts": CARDS, "emails": [earlier, sent]})["closed"] == 0
    assert _run({"contacts": CARDS, "emails": [sent]})["closed"] == 1


# P6 ── undo, then a second (redial) call in the same snapshot closes it again
def test_p6_undone_call_then_redial_same_minute(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, action_type="call_back", channel="phone", contact_name="Adel",
           contact_identifier="+14155550123", summary="Call Adel back",
           source_refs=[{"sourceType": "imessage", "sourceId": "m-1", "snippet": "call me"}])
    calls = [_call(duration_seconds=4), _call(timestamp="2026-11-05 08:01:00", duration_seconds=3)]
    assert _run({"calls": calls})["closed"] == 1
    row = _row(tmp_path)
    signal_closes.reopen(row, "2026-11-09", now=NOW)
    _write(tmp_path, row)
    assert _run({"calls": calls})["closed"] == 0


# P7 ── a task reopened by a NEW, different ask keeps the old ask's `fulfillment`
def test_p7_reopened_row_with_new_ask_keeps_stale_fulfillment(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, fulfillment="schedule")
    assert _run({}, events=[_event()])["closed"] == 1
    ask = {"id": "g-2", "messageId": "g-2", "from": "mina@example.org", "to": "me@example.com",
           "labelIds": ["INBOX"], "date": "Fri, 06 Nov 2026 09:00:00 -0000",
           "subject": "Re: Acme", "body": "Can you send me the signed term sheet by Friday?"}
    action = {"type": "reply", "channel": "gmail", "contactName": "Mina", "contactIdentifier": "mina@example.org",
              "loopId": "k", "contextSummary": "Send Mina the signed term sheet",
              "contextAsk": "Can you send me the signed term sheet by Friday?", "fulfillment": "none",
              "deliverable": "Send Mina the signed term sheet", "owedBy": "owner",
              "evidence": [{"sourceType": "email", "sourceId": "g-2",
                            "snippet": "Can you send me the signed term sheet by Friday?"}]}
    cr.resolve({"today": "2026-11-08", "local": {}, "emails": [ask], "new_actions": [action]}, NOW)
    row = _row(tmp_path)
    assert row["status"] == "open" and "term sheet" in row["summary"]
    # an unrelated one-off meeting booked later with the same person
    later = _event(id="ev-2", summary="Coffee", created="2026-11-07T10:00:00Z")
    _run({}, events=[_event(), later])
    after = _row(tmp_path)
    assert after["status"] == "open", (
        f"'{after['summary']}' was closed as {after.get('resolution')} (stale fulfillment={row.get('fulfillment')})")


# ── merges ──
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")


def _inbound(mid, date, body, **o):
    return {"id": mid, "messageId": mid, "from": "dana@example.org", "to": "me@example.com",
            "labelIds": ["INBOX"], "date": date, "subject": "hi", "body": body, **o}


def _only(tmp_path):
    files = list((tmp_path / "knowledge/continuity").glob("*.md"))
    return [yaml.safe_load(f.read_text().split("---")[1]) for f in files]


def _capture_doc_task(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    ask = _inbound("g-1", "Tue, 03 Nov 2026 10:00:00 -0000", "Can you send me the pricing document?")
    action = {"type": "reply", "channel": "gmail", "contactName": "Dana", "contactIdentifier": "dana@example.org",
              "contextSummary": "Send Dana the pricing document",
              "contextAsk": "Can you send me the pricing document?", "newObligation": True,
              "deliverable": "Send Dana the pricing document", "owedBy": "owner", "fulfillment": "none",
              "evidence": [{"sourceType": "email", "sourceId": "g-1",
                            "snippet": "Can you send me the pricing document?"}]}
    cr.resolve({"today": "2026-11-03", "local": {}, "emails": [ask], "new_actions": [action]},
               datetime(2026, 11, 3, 12, tzinfo=timezone.utc))
    rows = _only(tmp_path)
    assert len(rows) == 1 and rows[0]["status"] == "open", rows
    return ask, rows[0]


def test_m1_schedule_ask_merged_onto_a_deliverable_row_closes_the_deliverable(tmp_path, monkeypatch):
    ask, row = _capture_doc_task(tmp_path, monkeypatch)
    # Two days later Dana also asks for a meeting. Ordinary extraction (no newObligation, no
    # loopId) lands on the same base row; `adopt_fulfillment` copies fulfillment=schedule onto it.
    later = _inbound("g-2", "Thu, 05 Nov 2026 10:00:00 -0000", "Also, can we find 30 minutes next week?")
    action = {"type": "reply", "channel": "gmail", "contactName": "Dana", "contactIdentifier": "dana@example.org",
              "contextSummary": "Find 30 minutes with Dana", "contextAsk": "can we find 30 minutes next week?",
              "fulfillment": "schedule", "deliverable": "Book 30 minutes with Dana", "owedBy": "owner"}
    cr.resolve({"today": "2026-11-05", "local": {}, "emails": [ask, later], "new_actions": [action]},
               datetime(2026, 11, 5, 12, tzinfo=timezone.utc))
    rows = _only(tmp_path)
    assert len(rows) == 1
    # A meeting created BEFORE the scheduling ask (Nov 4) but after the row's created_at (Nov 3).
    event = {"id": "ev-1", "summary": "Unrelated", "status": "confirmed", "created": "2026-11-04T08:00:00Z",
             "start": "2026-11-10T11:00:00Z",
             "attendees": [{"email": "dana@example.org", "responseStatus": "accepted"},
                           {"email": "me@example.com", "responseStatus": "accepted", "self": True}]}
    lc.run({"local": {}, "events": [event]}, now=NOW)
    after = _only(tmp_path)[0]
    assert after["status"] == "open", (
        f"'{after['summary']}' closed as {after.get('resolution')}; the document was never sent")


def test_m2_missed_call_merged_onto_a_deliverable_row_closes_it_on_any_call(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    directory = tmp_path / "knowledge/continuity"
    directory.mkdir(parents=True)
    row = {"status": "open", "action_type": "reply", "channel": "imessage", "contact_name": "Dana",
           "contact_identifier": "+14155550144", "created_at": "2026-11-03 08:00:00",
           "summary": "Send Dana the pricing document"}
    row["anchor_key"] = cr.compute_anchor_key(row)
    (directory / (cr._safe(row["anchor_key"]) + ".md")).write_text(
        "---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    earlier = {"phone": "+14155550144", "timestamp": "2026-11-04 05:00:00", "is_outgoing": True,
               "is_answered": False, "call_type": "phone", "duration_seconds": 40}
    missed = {"phone": "+14155550144", "timestamp": "2026-11-04 06:00:00", "is_outgoing": False,
              "is_answered": False, "call_type": "phone", "duration_seconds": 0}
    missed_id, missed_quote = call_evidence(missed, "calls")
    action = {"type": "call_back", "channel": "phone", "contactName": "Dana",
              "contactIdentifier": "+14155550144", "contextSummary": "Return Dana's missed call",
              "fulfillment": "callback",
              "evidence": [{"sourceType": "calls", "sourceId": missed_id, "snippet": missed_quote}]}
    local = {"calls": [earlier, missed]}
    cr.resolve({"today": "2026-11-04", "local": local, "new_actions": [action]},
               datetime(2026, 11, 4, 9), resolve_existing=False)
    merged = _only(tmp_path)
    assert len(merged) == 1
    # The only connected call happened BEFORE the missed call (05:00 < 06:00).
    lc.run({"local": local}, now=NOW)
    after = _only(tmp_path)[0]
    assert after["status"] == "open", (
        f"'{after['summary']}' closed as {after.get('resolution')} by a call made before the missed call")


# ── the notice, and after a close ──
EVENING = {"type": "evening", "local": {}}


def test_n1_the_notice_must_say_which_task_was_closed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, fulfillment="schedule", summary="Send Mina the signed term sheet")
    lc.run({"local": {}, "events": [_event()]}, now=NOW)
    lines = compose_brief._receipt_lines(EVENING, "2026-11-08", "+00:00", now=NOW + timedelta(hours=8))
    assert any("term sheet" in line for line in lines), lines


def test_n2_two_closes_for_one_person_must_be_distinguishable(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, key="a", fulfillment="schedule", summary="Schedule the Acme intro call")
    assert _run({}, events=[_event()])["closed"] == 1
    _setup(tmp_path, monkeypatch, key="b", fulfillment="schedule", summary="Set up board prep with Mina")
    assert _run({}, events=[_event()])["closed"] == 0, "a meeting that closed one task closed a second"
    assert _run({}, events=[_event(), _event(id="ev-2")])["closed"] == 1
    lines = compose_brief._receipt_lines(EVENING, "2026-11-08", "+00:00", now=NOW + timedelta(hours=8))
    clauses = lines[0].split(" — ", 1)[1].rstrip(".").split("; ")
    assert len(clauses) == len(set(clauses)) == len(("a", "b")), lines
    assert "Acme intro call" in lines[0] and "board prep" in lines[0]


def test_n3_a_close_not_named_within_seven_days_must_still_be_named(tmp_path, monkeypatch):
    """No evening brief accepted for a week (travel, delivery outage, evening brief off)."""
    _setup(tmp_path, monkeypatch, fulfillment="schedule")
    lc.run({"local": {}, "events": [_event()]}, now=NOW)
    assert "reported_at" not in _row(tmp_path)["resolution_basis"]
    later = NOW + timedelta(days=7, hours=1)
    assert compose_brief._signal_closes(later), "closed, never named to the owner, and now never will be"


def test_n5_cancelled_meeting_after_the_close_must_reopen_or_flag(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, fulfillment="schedule")
    lc.run({"local": {}, "events": [_event()]}, now=NOW)
    assert _row(tmp_path)["status"] == "resolved"
    # Next day: they declined / the meeting was cancelled before it happened.
    lc.run({"local": {}, "events": [_event(status="cancelled")]}, now=NOW + timedelta(days=1))
    row = _row(tmp_path)
    assert row["status"] == "open", "meeting cancelled before it happened; task stays closed"
    assert "rejected_signals" not in row and "rejected_before" not in row
    assert loop_checks.note_for(row, loop_checks.read()).startswith("open again: the meeting that closed this")


# ── a brief line about one task never hides another task of the same person (H2) ──
A = {"anchor_key": "reply:id:sam@example.com", "status": "open", "action_type": "reply",
     "contact_name": "Sam Rivera", "contact_identifier": "sam@example.com", "created_at": "2026-11-03 10:00:00",
     "summary": "Fundraising call", "ask": "Send Sam the data room link",
     "deliverable": "Send Sam the data room link", "owed_by": "owner"}
B = {"anchor_key": "reply:id:sam@example.com:origin:abcd", "status": "open", "action_type": "reply",
     "contact_name": "Sam Rivera", "contact_identifier": "sam@example.com", "created_at": "2026-11-05 10:00:00",
     "summary": "Sam wants a call next week", "ask": "Schedule the fundraising follow-up call",
     "deliverable": "Schedule the fundraising follow-up call", "owed_by": "owner"}
LINE_B = "- **Sam Rivera**: schedule the fundraising follow-up call"
LINE_A = "- **Sam Rivera**: send Sam the data room link"


def test_c0_control_same_rows_without_deliverable_are_held_as_ambiguous():
    """Control (main's rule): one unmarked line matching two asks proves neither."""
    a = {k: v for k, v in A.items() if k != "deliverable"}
    b = {**{k: v for k, v in B.items() if k != "deliverable"}, "summary": B["deliverable"]}
    assert cb._line_shown_task_keys(LINE_B, [a, b], "2026-11-08", None) == set()


def test_c1_line_for_task_b_must_not_count_as_showing_task_a():
    keys = cb._line_open_task_keys(LINE_B, [A, B], "2026-11-08")
    assert A["anchor_key"] not in keys, keys


def test_c2_task_a_must_stay_visible_or_in_still_open():
    md = "## Needs you\n" + LINE_B + "\n\n## Filtered\nnothing\n"
    visible = cb._visible_open_task_keys(md, [A, B], "2026-11-08")
    assert A["anchor_key"] not in visible, "task A is counted as shown, so Still open will not list it"


def test_c3_dedupe_must_not_delete_task_a_own_line():
    md = "## Needs you\n" + LINE_B + "\n" + LINE_A + "\n\n## Filtered\nnothing\n"
    out = cb._dedupe_open_task_lines(md, [A, B], "2026-11-08")
    assert "data room" in out, "task A's only line was deleted as a 'repeat' of task B's line:\n" + out


def test_c4_represented_rows_must_not_include_task_a():
    md = "## Needs you\n" + LINE_B + "\n"
    keys = {r["anchor_key"] for r in cb._represented_loop_rows([A, B], md)}
    assert A["anchor_key"] not in keys, keys


def test_c5_end_to_end_still_open_backstop_loses_task_a(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    a = {**A, "deadline": "2026-11-07"}                       # overdue = urgent
    md = "## Needs you\n" + LINE_B + "\n" + LINE_A + "\n\n## Filtered\nnothing\n"
    out = cb._append_still_open({"brief_markdown": md}, [a, B], datetime(2026, 11, 8, 9, tzinfo=timezone.utc),
                                "2026-11-08")
    assert "data room" in out["brief_markdown"], "overdue task A is nowhere in the final brief:\n" + out["brief_markdown"]
