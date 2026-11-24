"""Three owner-approved signals close a task on their own (Nov 8, 2026): an intro email with the
asker and the named, known intro target on To/Cc, a meeting booked with them after a scheduling ask,
and a connected phone call after a callback ask. Everything else stays open; a plain reply stays a
"maybe done" hint. The owner can reopen a wrong close, and that exact signal never closes it again."""
import importlib.util
import os
from datetime import datetime, timezone

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
spec = importlib.util.spec_from_file_location(
    "loop_check_close", os.path.join(ROOT, "morning-brief", "scripts", "loop_check.py"))
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)
import loop_checks  # noqa: E402
from delivery_effects import loop_version  # noqa: E402

ASK = "2026-11-03 10:00:00"
NOW = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)


def _setup(tmp_path, monkeypatch, key="k", **fields):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    directory = tmp_path / "knowledge/continuity"
    directory.mkdir(parents=True, exist_ok=True)
    row = {"anchor_key": key, "status": "open", "action_type": "reply", "channel": "imessage",
           "contact_name": "Adel", "contact_identifier": "+12025550175", "created_at": ASK,
           "summary": "Intro to Jackson for marketing advice",
           "source_refs": [{"sourceType": "imessage", "sourceId": "m-1", "snippet": "intro me?"}],
           **fields}
    path = directory / f"{key}.md"
    path.write_text("---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    return row, path


def _row(tmp_path, key="k"):
    return yaml.safe_load((tmp_path / "knowledge/continuity" / f"{key}.md").read_text().split("---")[1])


CARD = {"name": "Adel", "phones": ["+12025550175"], "emails": ["adel@example.com"]}
JACKSON = {"name": "Jackson", "emails": ["jackson@trainloop.example"]}
CARDS = [CARD, JACKSON]
INTRO = {"fulfillment": "intro", "deliver_to": "jackson@trainloop.example"}


def _intro(**overrides):
    return {"id": "e-1", "messageId": "e-1", "from": "me@fpv.example", "to": "jackson@trainloop.example",
            "cc": "adel@example.com", "labelIds": ["SENT"], "date": "Sat, 07 Nov 2026 17:05:38 -0000",
            "subject": "Jackson / Adel", "body": "Over to you two. Adel is out until the 22nd!", **overrides}


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@fpv.example")


def _run(local, **payload):
    return lc.run({"local": local, **payload}, now=NOW)


# ── intro ─────────────────────────────────────────────────────────────────────────────────────────

def test_an_intro_email_with_the_asker_and_target_closes_the_intro_ask(tmp_path, monkeypatch):
    row, _path = _setup(tmp_path, monkeypatch, **INTRO)
    assert _run({"contacts": CARDS, "emails": [_intro()]}) == {"checked": 0, "closed": 1}
    closed = _row(tmp_path)
    assert closed["status"] == "resolved" and closed["resolution"] == "intro_sent"
    assert closed["resolution_source_refs"] == [{"sourceType": "email", "sourceId": "e-1"}]
    assert closed["resolution_basis"]["basis"] == "intro_sent"
    assert closed["resolution_basis"]["source"] == "gmail"
    assert closed["resolution_loop_version"] == loop_version(row)
    assert closed["summary"] == row["summary"]
    assert "Over to you" not in str(closed)          # no message text in the ledger
    assert loop_checks.read() == {}


@pytest.mark.parametrize("email", [
    _intro(cc="", to="jackson@trainloop.example"),                     # asker not on it
    _intro(to="adel@example.com", cc=""),                              # nobody to introduce
    _intro(date="Mon, 02 Nov 2026 09:00:00 -0000"),                    # before the ask
    _intro(labelIds=["INBOX"], **{"from": "jackson@trainloop.example"}),  # not sent by the owner
    _intro(labelIds=["DRAFT"]),                                         # a draft proves nothing
    _intro(to="me@fpv.example"),                                       # only the owner besides them
    # A promise to intro later, copying an assistant, is not the introduction (review of #977).
    _intro(to="adel@example.com", cc="assistant@fpv.example", subject="Re: marketing help",
           body="Happy to intro you to Jackson next week."),
    # Quoted ask text is not intro wording, and a colleague is not the target.
    _intro(to="adel@example.com", cc="ops@fpv.example", subject="Re: hi",
           body="Sounds good, talk soon.\n\nOn Tue Adel wrote:\n> could you intro me to Jackson?"),
])
def test_other_emails_do_not_close_an_intro_ask(tmp_path, monkeypatch, email):
    _setup(tmp_path, monkeypatch, **INTRO)
    assert _run({"contacts": CARDS, "emails": [email]})["closed"] == 0
    assert _row(tmp_path)["status"] == "open"


@pytest.mark.parametrize("fields,cards", [
    ({"fulfillment": "intro"}, CARDS),                                  # no named target
    (INTRO, [CARD]),                                                    # target unknown to Sotto
    ({"fulfillment": "intro", "deliver_to": "me@fpv.example"}, CARDS),  # the owner is no target
])
def test_an_intro_closes_only_on_a_named_known_target(tmp_path, monkeypatch, fields, cards):
    row, _path = _setup(tmp_path, monkeypatch, **fields)
    email = _intro(to="jackson@trainloop.example, me@fpv.example")
    assert _run({"contacts": cards, "emails": [email]})["closed"] == 0
    assert _row(tmp_path)["status"] == "open"


def test_a_named_intro_target_must_be_on_the_email(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, fulfillment="intro", deliver_to="anil@example.com")
    cards = [*CARDS, {"name": "Anil", "emails": ["anil@example.com"]}]
    assert _run({"contacts": cards, "emails": [_intro()]})["closed"] == 0
    assert _run({"contacts": cards, "emails": [_intro(to="Anil <anil@example.com>")]})["closed"] == 1


# ── scheduling ────────────────────────────────────────────────────────────────────────────────────

def _event(**overrides):
    return {"id": "ev-1", "summary": "[Confirmed] FPV Ventures / Ivo", "status": "confirmed",
            "created": "2026-11-04T18:00:00Z", "start": "2026-11-09T11:00:00-08:00",
            "attendees": [{"email": "mk@example.net", "responseStatus": "accepted"},
                          {"email": "me@fpv.example", "responseStatus": "accepted", "self": True}],
            "organizer": {"email": "me@fpv.example", "self": True}, **overrides}


def _schedule_row(tmp_path, monkeypatch, **fields):
    return _setup(tmp_path, monkeypatch, **{
        "channel": "gmail", "contact_name": "Min-Kyu", "contact_identifier": "mk@example.net",
        "summary": "Ivo intro call scheduling",
        "source_refs": [{"sourceType": "email", "sourceId": "g-1", "snippet": "Monday 9 or 10?"}],
        **fields})


def test_a_meeting_booked_after_a_scheduling_ask_closes_it(tmp_path, monkeypatch):
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    assert _run({}, events=[_event()]) == {"checked": 0, "closed": 1}
    closed = _row(tmp_path)
    assert closed["resolution"] == "scheduled_meeting"
    assert closed["resolution_source_refs"] == [{"sourceType": "calendar", "sourceId": "ev-1"}]
    assert closed["closed_at"].startswith("2026-11-04T18:00:00")


@pytest.mark.parametrize("fulfillment, closed", [({"fulfillment": "none"}, 0), ({}, 1)])
def test_explicit_none_on_new_scheduling_capture_withholds_automatic_close(tmp_path, monkeypatch, fulfillment, closed):
    import continuity_resolve as cr

    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    ask = {"id": "g-1", "threadId": "g-thread", "from": "Min-Kyu <mk@example.net>",
           "to": "me@fpv.example", "labelIds": ["INBOX"], "date": "2026-11-03T10:00:00+00:00",
           "body": "Send me your availability, but please do not book yet."}
    action = {"type": "propose_times", "channel": "gmail", "contactName": "Min-Kyu",
              "contactIdentifier": "mk@example.net", "newObligation": True, **fulfillment,
              "contextSummary": "Send Min-Kyu availability without booking",
              "contextAsk": ask["body"], "emailMessageId": "g-1",
              "evidence": [{"sourceType": "email", "sourceId": "g-1", "snippet": ask["body"]}]}
    cr.resolve({"today": "2026-11-08", "local": {"emails": [ask]}, "new_actions": [action]},
               NOW, resolve_existing=False)
    row, = cr._load_items().values()
    assert row["action_type"] == "propose_times"
    assert _run({}, events=[_event()])["closed"] == closed
    assert cr._load_items()[row["anchor_key"]].get("fulfillment") == fulfillment.get("fulfillment")


@pytest.mark.parametrize("other", [{}, {"fulfillment": "schedule"}])
def test_explicit_none_remains_after_a_duplicate_task_is_folded(other):
    from signal_closes import fold_fulfillment

    keep = dict(other)
    fold_fulfillment(keep, {"fulfillment": "none"})
    assert keep == {"fulfillment": "none"}
    fold_fulfillment(keep, other)
    assert keep == {"fulfillment": "none"}


def test_a_legacy_propose_times_row_is_a_scheduling_ask(tmp_path, monkeypatch):
    _schedule_row(tmp_path, monkeypatch, action_type="propose_times")
    assert _run({}, events=[_event()])["closed"] == 1


@pytest.mark.parametrize("event", [
    _event(recurringEventId="series-1"),                                # a standing meeting
    _event(attendees=[{"email": "mk@example.net", "responseStatus": "declined"},
                      {"email": "me@fpv.example", "responseStatus": "accepted", "self": True}]),
    _event(status="cancelled"),
    _event(attendees=[{"email": "mk@example.net", "responseStatus": "accepted"},
                      {"email": "me@fpv.example", "responseStatus": "needsAction", "self": True}]),
    _event(created="2026-11-01T09:00:00Z"),                            # booked before the ask
    {k: v for k, v in _event().items() if k != "created"},              # booking time unknown
    _event(start="2026-11-02T11:00:00-08:00"),                         # happened before the ask
    _event(attendees=[{"email": "someone@else.example", "responseStatus": "accepted"},
                      {"email": "me@fpv.example", "responseStatus": "accepted", "self": True}]),
])
def test_other_meetings_do_not_close_a_scheduling_ask(tmp_path, monkeypatch, event):
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    assert _run({}, events=[event])["closed"] == 0
    assert _row(tmp_path)["status"] == "open"


def test_a_meeting_alone_never_closes_an_ordinary_reply(tmp_path, monkeypatch):
    row, _path = _schedule_row(tmp_path, monkeypatch)
    assert _run({}, events=[_event()]) == {"checked": 0, "closed": 0}   # not a scheduling ask: no hint either
    assert _row(tmp_path)["status"] == "open"
    assert loop_version(row) not in loop_checks.read()


# ── callback ──────────────────────────────────────────────────────────────────────────────────────

def _call(**overrides):
    return {"phone": "+1 (202) 555-0175", "timestamp": "2026-11-05 08:00:00", "is_outgoing": True,
            "is_answered": False, "call_type": "phone", "duration_seconds": 125, **overrides}


def test_a_connected_call_after_a_callback_ask_closes_it(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, action_type="call_back", channel="phone", summary="Call Adel back")
    assert _run({"calls": [_call(duration_seconds=29)]}) == {"checked": 1, "closed": 0}   # a short dialed call is only a hint
    assert _run({"calls": [_call(is_outgoing=False, is_answered=True)]})["closed"] == 1
    closed = _row(tmp_path)
    assert closed["resolution"] == "called"
    assert closed["resolution_source_refs"][0]["sourceType"] == "calls"


@pytest.mark.parametrize("local", [
    {"calls": [_call(is_outgoing=False, is_answered=False, duration_seconds=0)]},   # missed
    {"calls": [_call(duration_seconds=0)]},                                          # unanswered dial
    {"calls": [_call(timestamp="2026-11-02 08:00:00")]},                             # before the ask
    {"whatsapp_calls": [{"jid": "12025550175@s.whatsapp.net", "timestamp": "2026-11-05 08:00:00",
                         "is_outgoing": True, "is_missed": False}]},                 # WhatsApp
])
def test_other_calls_do_not_close_a_callback(tmp_path, monkeypatch, local):
    _setup(tmp_path, monkeypatch, fulfillment="callback")
    assert _run(local)["closed"] == 0
    assert _row(tmp_path)["status"] == "open"


# ── who is never auto-closed ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fields", [
    {"group_id": "chat123", "contact_identifier": "chat123"},
    {"status": "waiting", "action_type": "waiting_on"},
    {"resolution_mode": "explicit"},
    {"source": "user_added", "channel": "manual", "source_thread_id": "manual:1", "source_refs": None},
])
def test_groups_waiting_explicit_and_owner_tasks_are_never_auto_closed(tmp_path, monkeypatch, fields):
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule", **fields)
    assert _run({}, events=[_event()])["closed"] == 0


def test_a_revoked_source_closes_nothing(tmp_path, monkeypatch):
    import source_context
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    monkeypatch.setattr(source_context, "allowed", lambda source: source != "calendar")
    assert _run({}, events=[_event()])["closed"] == 0
    monkeypatch.setattr(source_context, "allowed", lambda source: source != "gmail")
    assert _run({}, events=[_event()])["closed"] == 0                 # the ask's own source


def test_a_reopened_task_stays_open_until_new_evidence(tmp_path, monkeypatch):
    """The person coming back reopens an automatic close; the old meeting cannot close it again."""
    import continuity_resolve as cr
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    assert _run({}, events=[_event()])["closed"] == 1
    ask = {"id": "g-2", "messageId": "g-2", "from": "mk@example.net", "to": "me@fpv.example",
           "labelIds": ["INBOX"], "date": "Fri, 06 Nov 2026 09:00:00 -0000",
           "subject": "Re: Ivo", "body": "Can we move it to Tuesday instead?"}
    action = {"type": "reply", "channel": "gmail", "contactName": "Min-Kyu",
              "contactIdentifier": "mk@example.net", "loopId": "k", "contextSummary": "Move the Ivo call",
              "contextAsk": "Can we move it to Tuesday instead?", "fulfillment": "schedule",
              "evidence": [{"sourceType": "email", "sourceId": "g-2",
                            "snippet": "Can we move it to Tuesday instead?"}]}
    cr.resolve({"today": "2026-11-08", "local": {}, "emails": [ask], "new_actions": [action]}, NOW)
    assert _row(tmp_path)["status"] == "open"
    assert _run({}, events=[_event()])["closed"] == 0
    assert _row(tmp_path)["status"] == "open"


# ── capture and the brief line ────────────────────────────────────────────────────────────────────

def test_extraction_carries_fulfillment_and_a_recapture_never_arms_an_existing_task(tmp_path, monkeypatch):
    import continuity_resolve as cr
    import compose_brief
    props = compose_brief._ACTION_ITEM_SCHEMA["properties"]
    assert props["fulfillment"]["enum"] == ["intro", "schedule", "callback", "none"]
    assert "deliverTo" in props
    assert cr._normalize_action({"fulfillment": "intro", "deliverTo": "anil@example.com"})[
        "fulfillment"] == "intro"
    assert cr._normalize_action({"fulfillment": "invent"})["fulfillment"] is None
    _schedule_row(tmp_path, monkeypatch)
    action = {"type": "reply", "channel": "gmail", "contactName": "Min-Kyu",
              "contactIdentifier": "mk@example.net", "loopId": "k", "contextSummary": "Ivo call",
              "contextAsk": "pick a time", "fulfillment": "schedule"}
    cr.resolve({"today": "2026-11-08", "local": {}, "new_actions": [action]}, NOW)
    assert "fulfillment" not in _row(tmp_path)        # the recapture did not write this row's ask
    assert _row(tmp_path)["summary"] == "Ivo intro call scheduling"


def test_the_calendar_gather_keeps_the_booking_time():
    import gather_google
    event = gather_google.normalize_event({"id": "x", "summary": "s", "created": "2026-11-04T18:00:00Z",
                                           "start": {"dateTime": "2026-11-09T11:00:00-08:00"}})
    assert event["created"] == "2026-11-04T18:00:00Z"


def test_the_evening_brief_names_an_automatic_close_once_after_delivery(tmp_path, monkeypatch):
    import compose_brief
    import delivery_effects
    from datetime import timedelta
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    _run({}, events=[_event()])
    evening = {"type": "evening", "local": {}}
    lines = compose_brief._receipt_lines(evening, "2026-11-08", "+00:00", now=NOW + timedelta(hours=8))
    assert lines == ["Closed 1 task — Min-Kyu is on the calendar, so I closed \"Ivo intro call scheduling\" (tell me if that's wrong)."]
    out = compose_brief._append_receipts({"brief_markdown": "## Filtered\nNothing else.\n"}, evening,
                                         now=NOW + timedelta(hours=8))
    notices = [e for e in out["_parking_notices"] if e["kind"] == "closure_notice"]
    assert [e["anchor_key"] for e in notices] == ["k"]
    # Not yet delivered: a later brief still names it. Once accepted, never again.
    assert compose_brief._signal_closes(NOW + timedelta(hours=20))
    assert delivery_effects.finalize(notices, {"accepted_at": (NOW + timedelta(hours=8)).timestamp()})
    assert compose_brief._signal_closes(NOW + timedelta(hours=20)) == []
    assert compose_brief._receipt_lines(evening, "2026-11-09", "+00:00", now=NOW + timedelta(hours=32)) == []


def test_the_morning_brief_also_names_an_automatic_close_until_a_brief_naming_it_is_accepted(tmp_path, monkeypatch):
    import compose_brief
    import delivery_effects
    from datetime import timedelta
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    _run({}, events=[_event()])
    md = "Good morning\n\n## Filtered\nNothing else.\n"
    out = compose_brief._append_receipts({"brief_markdown": md}, {"type": "morning"}, now=NOW + timedelta(hours=1))
    line = ("Closed on its own: Min-Kyu is on the calendar, so I closed \"Ivo intro call scheduling\""
            " (tell me if that's wrong).")
    assert out["brief_markdown"] == f"Good morning\n\n{line}\n\n## Filtered\nNothing else.\n"
    notices = out["_parking_notices"]
    assert [(e["kind"], e["anchor_key"]) for e in notices] == [("closure_notice", "k")]
    # Undelivered: the next brief still names it. Once one naming it is accepted, never again.
    assert compose_brief._append_receipts({"brief_markdown": md}, {"type": "morning"},
                                          now=NOW + timedelta(hours=2))["brief_markdown"] != md
    assert delivery_effects.finalize(notices, {"accepted_at": (NOW + timedelta(hours=1)).timestamp()})
    for kind in ("morning", "evening"):
        again = compose_brief._append_receipts({"brief_markdown": md}, {"type": kind}, now=NOW + timedelta(hours=26))
        assert again == {"brief_markdown": md} or "Closed on its own" not in again["brief_markdown"]
    welcome = {"brief_markdown": md}
    _schedule_row(tmp_path, monkeypatch, key="w", fulfillment="schedule")
    assert compose_brief._append_receipts(dict(welcome), {"type": "welcome"}, now=NOW) == welcome


@pytest.mark.parametrize("edition", ["morning", "evening"])
def test_close_notice_needs_both_current_source_grants(tmp_path, monkeypatch, edition):
    import compose_brief
    import delivery_effects
    import source_context
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    assert _run({}, events=[_event()])["closed"] == 1
    when = NOW.replace(hour=17)
    draft = {"brief_markdown": "## Filtered\nNothing else.\n"}

    allowed = source_context.allowed
    monkeypatch.setattr(source_context, "allowed", lambda source: source != "gmail" and allowed(source))
    assert "Ivo intro call scheduling" not in compose_brief._append_receipts(
        dict(draft), {"type": edition}, now=when)["brief_markdown"]

    monkeypatch.setattr(source_context, "allowed", allowed)
    pending = compose_brief._append_receipts(dict(draft), {"type": edition}, now=when)
    notice, = pending["_parking_notices"]
    assert notice["sources"] == ["calendar", "gmail"]
    assert delivery_effects.valid([notice], when.timestamp())
    monkeypatch.setattr(source_context, "allowed", lambda source: source != "calendar" and allowed(source))
    assert not delivery_effects.valid([notice], when.timestamp())
    assert not _row(tmp_path)["resolution_basis"].get("reported_at")


def test_a_precomposed_edition_is_invalid_after_another_delivers_the_close(tmp_path, monkeypatch):
    import compose_brief
    import delivery_effects
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    assert _run({}, events=[_event()])["closed"] == 1
    when = NOW.replace(hour=17)
    draft = {"brief_markdown": "## Filtered\nNothing else.\n"}
    morning, evening = [compose_brief._append_receipts(dict(draft), {"type": edition},
                                                       now=when)["_parking_notices"]
                        for edition in ("morning", "evening")]
    assert delivery_effects.valid(evening, when.timestamp())
    assert delivery_effects.finalize(morning, {"accepted_at": when.timestamp()})
    assert not delivery_effects.valid(evening, when.timestamp())


def test_later_brief_omits_only_the_closure_with_an_uncertain_receipt(tmp_path, monkeypatch):
    import compose_brief
    import jsonstore
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    assert _run({}, events=[_event()])["closed"] == 1
    first = NOW.replace(hour=11)
    later = NOW.replace(hour=17)
    draft = {"brief_markdown": "# Other work\n"}
    notice, = compose_brief._append_receipts(dict(draft), {"type": "morning"}, now=first)["_parking_notices"]
    outbox = str(tmp_path / "events/outbox.json")
    existing = {"id": "morning", "status": "pending", "acceptance": "unknown",
                "payload": {"effects": [notice]}}
    jsonstore.write_atomic(outbox, {"rows": [existing]})
    later_brief = compose_brief._append_receipts(dict(draft), {"type": "evening"}, now=later)
    assert later_brief["brief_markdown"] == draft["brief_markdown"]
    assert not later_brief.get("_parking_notices")
    assert not _row(tmp_path)["resolution_basis"].get("reported_at")
    existing["acceptance"] = "not_attempted"
    jsonstore.write_atomic(outbox, {"rows": [existing]})
    assert "Ivo intro call scheduling" in compose_brief._append_receipts(
        dict(draft), {"type": "evening"}, now=later)["brief_markdown"]


def test_auto_close_keeps_provenance_fresh(tmp_path, monkeypatch):
    """Chat and MCP still show a closed, stamped row and its evidence (review of #977)."""
    import continuity_resolve as cr
    import delivery_effects
    from ledger_provenance import digest_frontmatter
    row, path = _setup(tmp_path, monkeypatch, **INTRO)
    row["content_provenance"] = {"version": 1, "kind": "sources", "sources": ["imessage"],
                                 "digest": digest_frontmatter(row)}
    path.write_text("---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    assert _run({"contacts": CARDS, "emails": [_intro()]})["closed"] == 1
    closed = _row(tmp_path)
    assert cr._valid_provenance(closed) == ("sources", frozenset({"imessage", "gmail"}))
    notice = {"kind": "closure_notice", "anchor_key": "k", "loop_version": loop_version(closed)}
    assert delivery_effects.finalize([notice], {"accepted_at": NOW.timestamp()})
    assert _row(tmp_path)["resolution_basis"]["reported_at"]
    assert cr._valid_provenance(_row(tmp_path)) is not None


def test_a_later_owner_close_after_a_reopen_is_reported_normally(tmp_path, monkeypatch):
    import compose_brief as cb
    _setup(tmp_path, monkeypatch, status="resolved", resolution="user_resolved",
           resolved_at="2026-11-12", closed_at="2026-11-12T10:00:00+00:00",
           resolution_basis={"basis": "intro_sent", "source": "gmail", "at": "2026-11-07T17:05:38+00:00",
                             "recorded_at": "2026-11-08T09:00:00+00:00"})
    _chased, closed = cb._ledger_receipts("2026-11-12")
    assert [e["anchor_key"] for e in closed] == ["k"]


def test_the_owner_can_reopen_a_wrong_close_and_that_signal_never_closes_it_again(tmp_path, monkeypatch):
    import signal_closes
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    assert _run({}, events=[_event()])["closed"] == 1
    closed = _row(tmp_path)
    assert signal_closes.is_signal_close(closed)
    reopened = dict(closed)
    signal_closes.reopen(reopened, "2026-11-09", now=NOW)
    assert reopened["status"] == "open" and reopened["reopened_at"] == "2026-11-09"
    assert reopened["rejected_before"] == NOW.isoformat()
    assert not any(key.startswith("resolution") or key in ("resolved_at", "closed_at") for key in reopened)
    assert reopened["rejected_signals"] == [{"basis": "scheduled_meeting", "source_id": "ev-1"}]
    path = tmp_path / "knowledge/continuity" / "k.md"
    path.write_text("---\n" + yaml.safe_dump(reopened, sort_keys=False) + "---\n")
    assert _run({}, events=[_event()])["closed"] == 0                  # the same meeting
    assert _row(tmp_path)["status"] == "open"
    assert _run({}, events=[_event(id="ev-2")])["closed"] == 0         # booked before the undo
    assert _run({}, events=[_event(id="ev-3", created="2026-11-08T10:00:00Z")])["closed"] == 1  # booked after it
    assert len(list((tmp_path / "knowledge/continuity").glob("*.md"))) == 1


def test_reopen_after_the_person_comes_back_drops_the_signal_basis(tmp_path, monkeypatch):
    import continuity_resolve as cr
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    _run({}, events=[_event()])
    ask = {"id": "g-2", "messageId": "g-2", "from": "mk@example.net", "to": "me@fpv.example",
           "labelIds": ["INBOX"], "date": "Fri, 06 Nov 2026 09:00:00 -0000",
           "subject": "Re: Ivo", "body": "Can we move it to Tuesday instead?"}
    action = {"type": "reply", "channel": "gmail", "contactName": "Min-Kyu", "contactIdentifier": "mk@example.net",
              "loopId": "k", "contextSummary": "Move the Ivo call", "contextAsk": "Can we move it?",
              "evidence": [{"sourceType": "email", "sourceId": "g-2", "snippet": "Can we move it to Tuesday instead?"}]}
    cr.resolve({"today": "2026-11-08", "local": {}, "emails": [ask], "new_actions": [action]}, NOW)
    assert _row(tmp_path)["status"] == "open" and "resolution_basis" not in _row(tmp_path)


def test_obligation_history_carries_the_binding_needed_to_reopen(tmp_path, monkeypatch):
    import loops_query
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    _run({}, events=[_event()])
    closed = _row(tmp_path)
    from delivery_effects import loop_identity
    found = loops_query.topic_history(str(tmp_path), ["gmail", "calendar"], topic="Ivo")
    entry = next(i for i in found["items"] if i["anchor_key"] == "k")
    assert entry["loop_version"] == loop_version(closed) and entry["loop_identity"] == loop_identity(closed)
    assert entry["closed_by"] == "scheduled_meeting"


def test_every_automatic_close_is_named_even_on_a_busy_day(tmp_path, monkeypatch):
    """Grouped counts would hide a close the owner may need to undo, yet mark it as shown."""
    import compose_brief
    from datetime import timedelta
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    _run({}, events=[_event()])
    for i, name in enumerate(["Ron", "Ada", "Ben", "Cy"]):
        _setup(tmp_path, monkeypatch, key=f"c{i}", contact_name=name, status="resolved",
               resolution="delivered", resolved_at="2026-11-08", summary="a thing")
    lines = compose_brief._receipt_lines({"type": "evening", "local": {}}, "2026-11-08", "+00:00",
                                         now=NOW + timedelta(hours=8))
    assert lines == ["Closed 5 tasks — 4 they delivered; Min-Kyu is on the calendar, so I closed \"Ivo intro call scheduling\" (tell me if that's wrong)."]


def _calendar_gather(event, calendar, *, primary):
    from types import SimpleNamespace
    import gather_google as gg

    def list_events(**options):
        assert options['showDeleted'] is True
        return SimpleNamespace(execute=lambda: {"items": [event]})

    service = SimpleNamespace(
        calendarList=lambda: SimpleNamespace(list=lambda **kwargs: SimpleNamespace(
            execute=lambda: {"items": [{"id": calendar, "accessRole": "owner", "primary": primary}]})),
        events=lambda: SimpleNamespace(list=list_events),
    )
    observation = {}
    events = gg.gather_calendar("unused.py", service=service, observation=observation, loop_cancellations=True)
    return {"events": events, "calendar_cancellations": observation["loop_cancellations"]}


@pytest.mark.parametrize("after_start", [False, True])
def test_native_calendar_tombstone_reopens_only_before_the_meeting(tmp_path, monkeypatch, after_start):
    from datetime import timedelta
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule")
    event = _event(start={"dateTime": "2026-11-09T11:00:00-08:00"})
    lc.run(_calendar_gather(event, "primary", primary=True), now=NOW)
    assert _row(tmp_path)["status"] == "resolved"
    evidence = _calendar_gather({"id": event["id"], "status": "cancelled"}, "primary", primary=True)
    assert evidence["events"] == []
    lc.run(evidence, now=NOW + timedelta(days=2 if after_start else 1))
    assert _row(tmp_path)["status"] == ("resolved" if after_start else "open")


def test_calendar_self_on_a_shared_calendar_does_not_prove_owner_attendance(tmp_path, monkeypatch):
    counterpart = "counterpart@example.com"
    _schedule_row(tmp_path, monkeypatch, fulfillment="schedule", contact_identifier=counterpart)
    event = _event(
        start={"dateTime": "2026-11-09T11:00:00-08:00"},
        attendees=[{"email": counterpart, "self": True, "responseStatus": "accepted"},
                   {"email": "other@example.com", "responseStatus": "accepted"}],
        organizer={"email": counterpart, "self": True},
    )
    evidence = _calendar_gather(event, counterpart, primary=False)
    assert len(evidence["events"]) == 1
    assert lc.run(evidence, now=NOW)["closed"] == 0
    assert _row(tmp_path)["status"] == "open"
