"""The Nov 2026 pilot audit as fixtures: one fictional case per audited task and per fact flag class.

Each case states what the owner's rules expect. A plain reply never closes; only an intro email, an
accepted small one-off meeting after a scheduling ask, or an answered (or 30 s+ dialed) phone
callback closes; a WhatsApp call is a hint; Sotto never sends; no model judges. A case the code
gets wrong today is a strict xfail named after its root cause (R1..R10 tasks, F1..F6 facts, in the
audit's root-cause tables), so the fix that repairs it must flip it. Every person, number and
address here is fictional.
"""
import importlib.util
import os
from datetime import datetime, timezone

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _load(name, *path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lc = _load("loop_check_pilot", "morning-brief", "scripts", "loop_check.py")
cp = _load("closure_pass_pilot", "morning-brief", "scripts", "closure_pass.py")
pg = _load("poll_gmail_pilot", "event-triage", "scripts", "poll_gmail.py")
import continuity_resolve as cr  # noqa: E402
import fact_hygiene as fh  # noqa: E402
import knowledge as kg  # noqa: E402
import knowledge_update as ku  # noqa: E402
import loop_checks  # noqa: E402
from delivery_effects import loop_version  # noqa: E402

OWNER = "owner@example.net"
NOW = datetime(2026, 11, 12, 14, 30, tzinfo=timezone.utc)
AVERY, AVERY_MAIL, LEE_MAIL = "+12025550171", "avery@example.net", "lee@parkco.test"
CARDS = [{"name": "Avery Stone", "phones": [AVERY], "emails": [AVERY_MAIL]},
         {"name": "Lee Park", "emails": [LEE_MAIL]}]


def xfail(cause):
    return pytest.mark.xfail(strict=True, reason=cause)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setenv("SOTTO_USER_EMAIL", OWNER)
    (tmp_path / "knowledge/continuity").mkdir(parents=True)
    fh._people_cache.clear()


def _task(tmp_path, key, **fields):
    row = {"anchor_key": key, "status": "open", "action_type": "reply", "channel": "imessage",
           "contact_name": "Avery Stone", "contact_identifier": AVERY, "created_at": "2026-11-06 17:25:41",
           "summary": "Avery asked for an intro to Lee Park",
           "source_refs": [{"sourceType": "imessage", "sourceId": f"{key}-ask", "snippet": "intro me to Lee?"}],
           **fields}
    (tmp_path / "knowledge/continuity" / f"{key}.md").write_text(
        "---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    return row


def _row(tmp_path, key):
    return yaml.safe_load((tmp_path / "knowledge/continuity" / f"{key}.md").read_text().split("---")[1])


def _note(tmp_path, key):
    return loop_checks.read().get(loop_version(_row(tmp_path, key)))


def _check(local, **payload):
    return lc.run({"local": local, **payload}, now=NOW)


def _carry(row, **action):
    """The next brief carries the open row by its loopId and names what fulfills it."""
    carried = {"type": row["action_type"], "channel": row["channel"], "contactName": row["contact_name"],
               "contactIdentifier": row["contact_identifier"], "loopId": row["anchor_key"],
               "newObligation": False, "contextSummary": row["summary"], **action}
    cr.resolve({"today": "2026-11-12", "local": {"contacts": CARDS}, "new_actions": [carried]}, NOW)


def _intro_mail(mid="e-1", date="Fri, 06 Nov 2026 17:34:00 -0000", **o):
    return {"id": mid, "messageId": mid, "threadId": f"t-{mid}", "from": OWNER, "to": AVERY_MAIL,
            "cc": LEE_MAIL, "labelIds": ["SENT"], "date": date, "subject": "Avery, meet Lee",
            "body": "Connecting you two.", **o}


def _sent(text, handle=AVERY, when="2026-11-10 18:43:00", rowid=11):
    return {"source": "imessage", "rowid": rowid, "handle": handle, "is_from_me": True, "timestamp": when,
            "text": text, "is_group_chat": False, "chat_guid": None}


def _text(text, handle=AVERY, when="2026-11-10 20:39:00", mine=False, rowid=12):
    return {"rowid": rowid, "handle": handle, "is_from_me": mine, "timestamp": when, "text": text,
            "is_group_chat": False, "chat_guid": None}


def _meeting(eid="ev-1", people=("jordan@example.net",), created="2026-11-11T02:00:00Z", **o):
    attendees = [{"email": p, "responseStatus": "accepted"} for p in people]
    return {"id": eid, "summary": "Owner / Jordan", "status": "confirmed", "created": created,
            "start": "2026-11-13T13:30:00-08:00",
            "attendees": [*attendees, {"email": OWNER, "responseStatus": "accepted", "self": True}],
            "organizer": {"email": OWNER, "self": True}, **o}


def _polled(monkeypatch, *messages):
    """Owner mail exactly as the Gmail poll emits it: the in:sent lane, its own full-message fields."""
    class Result:
        def __init__(self, value):
            self.value = value

        def execute(self):
            return self.value

    class Messages:
        def list(self, **kwargs):
            sent = "in:sent" in kwargs["q"]
            return Result({"messages": [{"id": m["id"]} for m in messages] if sent else []})

    class Service:
        def users(self):
            return type("Users", (), {"messages": lambda self: Messages()})()

    full = {m["id"]: m for m in messages}
    monkeypatch.setattr(pg, "_find_google_api", lambda: "/fake/google_api.py")
    monkeypatch.setattr(pg, "gmail_service", Service)
    monkeypatch.setattr(pg, "fetch_message", lambda _service, mid: {"from": OWNER, **full[mid]})
    return pg.poll()


# ── The rows that should have closed (3) ──────────────────────────────────────────────────────────

@xfail("R1: a carried capture never sets an existing row's fulfillment, so a legacy row cannot arm")
def test_intro_legacy_row_carried_with_its_fulfillment_closes_on_the_intro_email(tmp_path):
    """Arsham shape (R1): an iMessage intro ask saved before capture fields existed."""
    row = _task(tmp_path, "intro")
    _carry(row, fulfillment="intro", deliverTo=LEE_MAIL)
    assert _check({"contacts": CARDS, "emails": [_intro_mail()]})["closed"] == 1
    assert _row(tmp_path, "intro")["resolution"] == "intro_sent"


@xfail("R2: loop_check reads only the brief's 24 h snapshot; no history pass when a row first gets a fulfillment")
def test_intro_whose_email_is_older_than_the_snapshot_closes_from_history(tmp_path):
    _task(tmp_path, "intro", fulfillment="intro", deliver_to=LEE_MAIL)
    assert _check({"contacts": CARDS})["closed"] == 1   # the 3-day-old intro email is only in Gmail history


@xfail("R7: an iMessage asker's email links only through Contacts or the graph, never from the source itself")
def test_intro_to_an_asker_known_only_by_phone_closes_once_their_email_is_captured(tmp_path):
    """Adel shape: no Contacts card links the asker's email to the phone row."""
    _task(tmp_path, "intro", fulfillment="intro", deliver_to=LEE_MAIL)
    assert _check({"contacts": CARDS[1:], "emails": [_intro_mail()]})["closed"] == 1


def test_intro_to_an_asker_known_only_by_phone_stays_open_with_a_note_today(tmp_path):
    _task(tmp_path, "intro", fulfillment="intro", deliver_to=LEE_MAIL)
    assert _check({"contacts": CARDS[1:], "emails": [_intro_mail()]})["closed"] == 0
    assert _note(tmp_path, "intro")["basis"] == "sent_to_them"


JORDAN = dict(channel="gmail", contact_name="Jordan Bell", contact_identifier="jordan@example.net",
              created_at="2026-11-10T15:42:02-08:00", summary="Jordan asked to find time next week",
              source_refs=[{"sourceType": "email", "sourceId": "g-jb", "snippet": "Can we find time?"}])


@xfail("R1: a carried capture never sets an existing row's fulfillment, so a legacy row cannot arm")
def test_schedule_legacy_reply_row_carried_with_fulfillment_closes_on_the_booked_meeting(tmp_path):
    """JB shape (R1, R10): an email scheduling ask saved as a plain `reply`."""
    row = _task(tmp_path, "jb", **JORDAN)
    _carry(row, fulfillment="schedule")
    assert _check({}, events=[_meeting()])["closed"] == 1
    assert _row(tmp_path, "jb")["resolution"] == "scheduled_meeting"


def test_schedule_row_with_its_fulfillment_closes_on_the_booked_meeting(tmp_path):
    _task(tmp_path, "jb", fulfillment="schedule", **JORDAN)
    assert _check({}, events=[_meeting()])["closed"] == 1


# ── Notes, never closes ──────────────────────────────────────────────────────────────────────────

def test_polled_owner_email_reply_marks_the_task_maybe_done_at_send_time(tmp_path, monkeypatch):
    """Chris / JB / Tina shape (R3): the Gmail poll's sent mail carries no labels of its own."""
    _task(tmp_path, "rae", channel="gmail", contact_name="Rae Quinn", contact_identifier="rae@example.net",
          created_at="2026-11-10 09:00:00", summary="Rae asked which dates work",
          source_refs=[{"sourceType": "email", "sourceId": "g-rae", "snippet": "Which dates work?"}])
    events = _polled(monkeypatch, {"id": "s-1", "threadId": "t-rae", "to": "Rae Quinn <rae@example.net>",
                                   "subject": "Re: dates", "body": "Tue or Wed after 5 works for me.",
                                   "date": "Tue, 10 Nov 2026 18:10:00 -0000"})
    assert [e["is_from_me"] for e in events] == [True]
    assert cp.run(events, {}, now=NOW) == {"judged": 0, "closed": 0, "hinted": 1}
    assert _note(tmp_path, "rae") == {"basis": "you_messaged", "at": "2026-11-10T18:10:00+00:00", "source": "gmail"}
    assert _row(tmp_path, "rae")["status"] == "open"


def test_their_newer_email_in_the_same_poll_batch_leaves_no_note(tmp_path, monkeypatch):
    """They ask again after the owner's polled reply, in the same batch: no stale "maybe done"."""
    _task(tmp_path, "rae", channel="gmail", contact_name="Rae Quinn", contact_identifier="rae@example.net",
          created_at="2026-11-10 09:00:00", summary="Rae asked which dates work",
          source_refs=[{"sourceType": "email", "sourceId": "g-rae", "snippet": "Which dates work?"}])
    sent = _polled(monkeypatch, {"id": "s-1", "threadId": "t-rae", "to": "Rae Quinn <rae@example.net>",
                                 "subject": "Re: dates", "body": "Tue or Wed after 5 works for me.",
                                 "date": "Tue, 10 Nov 2026 18:10:00 -0000"})
    again = pg._to_event({"id": "i-2"}, {"from": "Rae Quinn <rae@example.net>", "to": OWNER, "threadId": "t-rae",
                                         "subject": "Re: dates", "body": "Which dates again?",
                                         "date": "Tue, 10 Nov 2026 18:40:00 -0000"})
    assert cp.run([*sent, again], {}, now=NOW) == {"judged": 0, "closed": 0, "hinted": 0}
    assert _note(tmp_path, "rae") is None and _row(tmp_path, "rae")["status"] == "open"


@pytest.mark.parametrize("brief_in_flight", [False, True])
@pytest.mark.parametrize("owner_again", [False, True])
def test_their_newer_email_in_a_later_poll_drops_the_saved_note(tmp_path, monkeypatch, owner_again, brief_in_flight):
    """Rae shape: the owner's reply saved a note at send time; a later poll brings Rae's new ask
    (alone, or with an older owner send). The saved note drops, as at the brief; the task stays open."""
    from test_event_triage import _no_llm, _seed_snapshot, te
    _seed_snapshot(tmp_path, contacts=[{"name": "Rae Quinn", "emails": ["rae@example.net"]}])
    _no_llm(monkeypatch)
    _task(tmp_path, "rae", channel="gmail", contact_name="Rae Quinn", contact_identifier="rae@example.net",
          created_at="2026-11-10 09:00:00", summary="Rae asked which dates work",
          source_refs=[{"sourceType": "email", "sourceId": "g-rae", "snippet": "Which dates work?"}])
    first = _polled(monkeypatch, {"id": "s-1", "threadId": "t-rae", "to": "rae@example.net",
                                  "subject": "Re: dates", "body": "Monday might work.",
                                  "date": "Tue, 10 Nov 2026 18:05:00 -0000"})
    stamp = datetime(2026, 11, 10, 18, 6, tzinfo=timezone.utc)
    te.triage({"events": first}, now_local=stamp, now_utc=stamp)
    assert _note(tmp_path, "rae")["at"] == "2026-11-10T18:05:00+00:00"
    later = [pg._to_event({"id": "i-2", "threadId": "t-rae"}, {
        "from": "rae@example.net", "to": OWNER, "subject": "Re: dates",
        "body": "Monday no longer works. Could you send another date?", "date": "Tue, 10 Nov 2026 18:20:00 -0000"})]
    if owner_again:
        later += _polled(monkeypatch, {"id": "s-2", "threadId": "t-rae", "to": "rae@example.net",
                                       "subject": "Re: dates", "body": "Tuesday works for me.",
                                       "date": "Tue, 10 Nov 2026 18:10:00 -0000"})
    stamp = datetime(2026, 11, 10, 18, 30, tzinfo=timezone.utc)

    def receive_new_ask():
        te.triage({"events": later}, now_local=stamp, now_utc=stamp)
        assert _note(tmp_path, "rae") is None

    if brief_in_flight:
        record = loop_checks.record

        def record_after_new_ask(checks, *, previous):
            receive_new_ask()
            record(checks, previous=previous)

        monkeypatch.setattr(loop_checks, "record", record_after_new_ask)
        assert _check({"emails": later[1:]}) == {"checked": 1, "closed": 0}
    else:
        receive_new_ask()
    assert _note(tmp_path, "rae") is None and _row(tmp_path, "rae")["status"] == "open"


def test_owner_text_marks_the_task_maybe_done_at_send_time(tmp_path):
    """Virat shape: the plan is agreed by text and no invite exists yet."""
    _task(tmp_path, "vik", summary="Vik asked to plan a dinner", created_at="2026-11-10 12:00:00")
    assert cp.run([_sent("Works, send me your email")], {}, now=NOW)["hinted"] == 1
    assert _note(tmp_path, "vik")["basis"] == "you_messaged" and _row(tmp_path, "vik")["status"] == "open"


def test_long_email_reply_is_a_note_and_never_a_close(tmp_path):
    """Alberto (term sheets) and Tina shape: a full reply is still only a reply."""
    _task(tmp_path, "al", channel="gmail", contact_name="Alma Ruiz", contact_identifier="alma@example.net",
          created_at="2026-11-10 09:00:00", summary="Alma asked for thoughts on the term sheets", fulfillment="schedule")
    reply = {"id": "e-al", "messageId": "e-al", "threadId": "t-al", "from": OWNER, "to": "alma@example.net",
             "labelIds": ["SENT"], "date": "Tue, 10 Nov 2026 18:20:00 -0000", "subject": "Re: TSs",
             "body": "Long answer on each term sheet. " * 20}
    assert _check({"emails": [reply]})["closed"] == 0
    assert _note(tmp_path, "al")["basis"] == "you_messaged"


def test_their_acceptance_after_the_owner_offer_keeps_the_note(tmp_path):
    """Koushik shape: owner offers a time, they say yes by text, nothing is booked."""
    _task(tmp_path, "kai", summary="Kai asked to meet", action_type="propose_times", created_at="2026-11-10 12:00:00")
    local = {"imessage": [_text("Tuesday 9:30 coffee?", when="2026-11-10 17:05:00", mine=True, rowid=1),
                          _text("Let's do Tuesday!", rowid=2)]}
    assert _check(local)["closed"] == 0
    assert _note(tmp_path, "kai")["basis"] == "you_messaged"


def _kai(tmp_path, *texts, action_type="propose_times"):
    _task(tmp_path, "kai", summary="Kai asked to meet", action_type=action_type, created_at="2026-11-10 12:00:00")
    local = {"imessage": [_text("Tuesday 9:30 coffee?", when="2026-11-10 17:05:00", mine=True, rowid=1),
                          *(_text(t, when=f"2026-11-10 2{i}:00:00", rowid=2 + i) for i, t in enumerate(texts))]}
    assert _check(local)["closed"] == 0 and _row(tmp_path, "kai")["status"] == "open"
    return local


def test_their_reply_without_a_question_is_named_on_the_scheduling_note(tmp_path):
    _kai(tmp_path, "Let's do Tuesday!")
    assert _note(tmp_path, "kai") == {"basis": "you_messaged", "at": "2026-11-10T17:05:00+00:00", "source": "imessage",
                                      "replied_since": "2026-11-10T20:00:00+00:00", "replied_source": "imessage"}
    assert loop_checks.note_for(_row(tmp_path, "kai"), loop_checks.read()) == (
        "maybe done: you messaged them on 2026-11-10 and they replied since")


@pytest.mark.parametrize("texts", [("Which day works?",), ("Great", "Actually, can we do Wednesday?")])
def test_their_question_after_the_owner_offer_clears_a_scheduling_note(tmp_path, texts):
    """Nick shape on a scheduling ask: any question from them since the offer means no note."""
    _kai(tmp_path, *texts)
    assert _note(tmp_path, "kai") is None


def test_their_reply_still_clears_the_note_on_a_task_that_is_not_scheduling(tmp_path):
    _kai(tmp_path, "Sounds good", action_type="reply")
    assert _note(tmp_path, "kai") is None


def test_a_saved_scheduling_note_survives_their_later_non_question_and_drops_on_a_question(tmp_path):
    local = _kai(tmp_path)
    assert "replied_since" not in _note(tmp_path, "kai")
    _check({"imessage": [_text("See you then", when="2026-11-11 09:00:00", rowid=9)]})
    assert _note(tmp_path, "kai")["replied_since"] == "2026-11-11T09:00:00+00:00"
    assert _note(tmp_path, "kai")["at"] == local["imessage"][0]["timestamp"].replace(" ", "T") + "+00:00"
    assert cp.run([{**_text("Where again?", when="2026-11-11 10:00:00", rowid=10), "source": "imessage"}],
                  {}, now=NOW)["hinted"] == 0
    assert _note(tmp_path, "kai") is None and _row(tmp_path, "kai")["status"] == "open"


SAM_JID = "12025550176@s.whatsapp.net"
SAM = dict(channel="whatsapp", contact_name="Sam Ortiz", contact_identifier=SAM_JID, canonical_id="",
           created_at="2026-11-10 12:00:00", summary="Sam asked for a call back", action_type="call_back",
           source_refs=[{"sourceType": "whatsapp", "sourceId": "w-1", "snippet": "call me when free"}])


def _wa_call(jid=SAM_JID):
    return {"jid": jid, "timestamp": "2026-11-11 11:25:00", "is_outgoing": True, "is_missed": False}


def test_whatsapp_call_never_closes(tmp_path):
    """Surendra shape: an outgoing WhatsApp call that was not missed."""
    _task(tmp_path, "sam", **SAM)
    assert _check({"whatsapp_calls": [_wa_call()]})["closed"] == 0
    assert _row(tmp_path, "sam")["status"] == "open"


def test_whatsapp_call_is_a_call_note(tmp_path):
    _task(tmp_path, "sam", **SAM)
    _check({"whatsapp_calls": [_wa_call()]})
    assert _note(tmp_path, "sam") == {"basis": "call", "at": "2026-11-11T11:25:00+00:00", "source": "whatsapp_calls"}


def test_whatsapp_call_from_an_unlinked_lid_gives_no_note(tmp_path):
    _task(tmp_path, "sam", **SAM)
    _check({"whatsapp_calls": [_wa_call("84467100550123@lid")]})
    assert _note(tmp_path, "sam") is None


REVIEW = dict(channel="gmail", contact_name="Wren Ide", contact_identifier="wren@example.net",
              created_at="2026-11-09 09:00:00", summary="Wren asked you to review Delta Labs",
              deliver_to="kai@deltalabs.test")


def test_review_task_gets_no_meeting_note_from_a_large_group_meeting(tmp_path):
    """Diachronic shape: the person is one of seven at a group meeting."""
    _task(tmp_path, "rev", **REVIEW)
    group = _meeting(people=["wren@example.net", *(f"guest{i}@example.net" for i in range(5))],
                     summary="Portfolio sync")
    _check({}, events=[group])
    assert _note(tmp_path, "rev") is None
    assert _row(tmp_path, "rev")["status"] == "open"


def test_inbound_promise_is_never_captured_as_something_you_owe(tmp_path):
    """Lowenstein shape (R8): "the issues list is coming by Monday" is their promise, not your debt."""
    promise = {"id": "g-lm", "messageId": "g-lm", "from": "morgan@example.net", "to": OWNER, "labelIds": ["INBOX"],
               "date": "Mon, 09 Nov 2026 10:00:00 -0000", "subject": "Issues", "body": "The issues list is coming by Monday."}
    action = {"type": "reply", "channel": "gmail", "contactName": "Morgan Lowe", "contactIdentifier": "morgan@example.net",
              "contextSummary": "Morgan will send the issues list", "newObligation": True, "owedBy": "them",
              "evidence": [{"sourceType": "email", "sourceId": "g-lm", "snippet": "The issues list is coming by Monday."}]}
    cr.resolve({"today": "2026-11-09", "local": {}, "emails": [promise], "new_actions": [action]}, NOW)
    owed = [r for r in cr._load_items().values() if not cr.ledger_io.is_waiting_on(r.get("action_type"))]
    assert owed == []


PIYA = dict(contact_name="Piya Rao", contact_identifier="+12025550174", action_type="propose_times",
            created_at="2026-11-05 10:00:00", summary="Piya offered Tue or Wed after 5", deadline="2026-11-11")


CURRENT_CALENDAR = {"status": "ok", "complete": True,
                    "coverage": {"since": "2026-11-12T14:29:58Z", "until": "2026-11-15T14:29:58Z"}}


def test_scheduling_ask_whose_dates_passed_stays_open(tmp_path):
    _task(tmp_path, "piya", **PIYA)
    assert _check({})["closed"] == 0 and _row(tmp_path, "piya")["status"] == "open"


def test_scheduling_ask_whose_dates_passed_says_so(tmp_path):
    """Piyush shape."""
    _task(tmp_path, "piya", **PIYA)
    _check({}, calendar_observation=CURRENT_CALENDAR)
    assert (_note(tmp_path, "piya") or {}).get("basis") == "dates_passed"


def test_the_dates_passed_note_has_its_own_phrase_and_never_closes(tmp_path):
    _task(tmp_path, "piya", **PIYA)
    _check({}, calendar_observation=CURRENT_CALENDAR)
    assert _note(tmp_path, "piya") == {"basis": "dates_passed", "at": "2026-11-11", "source": "calendar"}
    assert loop_checks.note_for(_row(tmp_path, "piya"), loop_checks.read()) == (
        "date passed: 2026-11-11; I found no upcoming meeting I could match to this request")
    assert _row(tmp_path, "piya")["status"] == "open"


@pytest.mark.parametrize("fields", [{"deadline": "2026-11-12"}, {"deadline": "2026-11-20"}, {"deadline": None},
                                    {"action_type": "reply"}])
def test_no_dates_passed_note_before_the_deadline_or_off_a_scheduling_ask(tmp_path, fields):
    _task(tmp_path, "piya", **{**PIYA, **fields})
    _check({}, calendar_observation=CURRENT_CALENDAR)
    assert _note(tmp_path, "piya") is None


def test_a_meeting_or_your_message_outranks_dates_passed(tmp_path):
    _task(tmp_path, "jb", **{**JORDAN, "action_type": "propose_times", "deadline": "2026-11-11"})
    assert _check({}, events=[_meeting(created="2026-11-01T00:00:00Z")])["closed"] == 0
    assert _note(tmp_path, "jb")["basis"] == "meeting"
    _task(tmp_path, "piya", **PIYA)
    _check({"imessage": [_sent("Tue at 5?", handle="+12025550174")]})
    assert _note(tmp_path, "piya")["basis"] == "you_messaged"


def test_no_dates_passed_note_without_the_calendar(tmp_path, monkeypatch):
    """Without the calendar Sotto cannot say no meeting is booked."""
    import source_context
    monkeypatch.setattr(source_context, "allowed", lambda source: source != "calendar")
    _task(tmp_path, "piya", **PIYA)
    _check({}, calendar_observation=CURRENT_CALENDAR)
    assert _note(tmp_path, "piya") is None


def test_older_ask_overtaken_by_a_newer_one_stays_open(tmp_path):
    """Alberto pre-term-sheet shape: supersession is a meaning judgment, so nothing closes it."""
    _task(tmp_path, "deck", channel="gmail", contact_name="Alma Ruiz", contact_identifier="alma@example.net",
          created_at="2026-11-02 09:00:00", summary="Alma asked for deck feedback")
    newer = {"id": "g-ts", "messageId": "g-ts", "from": "alma@example.net", "to": OWNER, "labelIds": ["INBOX"],
             "date": "Tue, 10 Nov 2026 09:00:00 -0000", "subject": "TSs", "body": "We're getting term sheets."}
    assert _check({"emails": [newer]})["closed"] == 0 and _row(tmp_path, "deck")["status"] == "open"


# ── Negative controls: the six KEEP rows and the unsure one stay open ────────────────────────────

def test_a_draft_reply_is_no_note(tmp_path):
    """Ashutosh shape."""
    _task(tmp_path, "ash", channel="gmail", contact_name="Ash Mehta", contact_identifier="ash@example.net",
          created_at="2026-11-09 09:00:00", summary="Ash asked for the data room")
    draft = {"id": "d-1", "messageId": "d-1", "from": OWNER, "to": "ash@example.net", "labelIds": ["DRAFT"],
             "date": "Tue, 10 Nov 2026 10:00:00 -0000", "subject": "Re: data room", "body": "Here it is"}
    assert _check({"emails": [draft]})["closed"] == 0 and _note(tmp_path, "ash") is None


def test_no_activity_is_no_note(tmp_path):
    """Elliot and Ryan shapes: nothing sent, so nothing to check."""
    _task(tmp_path, "eli", summary="Eli asked for two names", created_at="2026-11-09 09:00:00")
    assert _check({"imessage": [_text("any names yet?", when="2026-11-10 09:00:00")]})["closed"] == 0
    assert _note(tmp_path, "eli") is None


def test_their_question_after_the_owner_offer_is_no_note(tmp_path):
    """Nick shape."""
    _task(tmp_path, "nia", summary="Nia asked for sample queries", created_at="2026-11-09 09:00:00")
    local = {"imessage": [_text("Want a few queries?", when="2026-11-10 09:00:00", mine=True, rowid=1),
                          _text("Which ones would you pick?", when="2026-11-10 10:00:00", rowid=2)]}
    assert _check(local)["closed"] == 0 and _note(tmp_path, "nia") is None


def test_a_call_with_a_colleague_of_theirs_is_no_note_and_no_close(tmp_path):
    """Manthan shape: same company, different person."""
    _task(tmp_path, "max", contact_name="Max Lund", contact_identifier="+12025550177", action_type="call_back",
          summary="Max asked for a call back", created_at="2026-11-09 09:00:00")
    call = {"phone": "+12025550178", "timestamp": "2026-11-10 11:00:00", "is_outgoing": True,
            "is_answered": True, "call_type": "phone", "duration_seconds": 300}
    assert _check({"calls": [call]})["closed"] == 0 and _note(tmp_path, "max") is None


def test_unsure_row_stays_open(tmp_path):
    """Mrutyunjay shape."""
    _task(tmp_path, "mo", contact_name="Mo Reddy", contact_identifier="+12025550179",
          summary="Mo asked about the pilot", created_at="2026-11-09 09:00:00")
    assert _check({"imessage": [_text("thanks!", handle="+12025550179")]})["closed"] == 0
    assert _row(tmp_path, "mo")["status"] == "open"


# ── R1 false-close guards: a later capture must not arm a close the original ask never asked for ──

def test_carried_fulfillment_never_overwrites_a_different_one(tmp_path):
    row = _task(tmp_path, "intro", fulfillment="intro", deliver_to=LEE_MAIL)
    _carry(row, fulfillment="schedule", deliverTo="other@parkco.test")
    stored = _row(tmp_path, "intro")
    assert (stored["fulfillment"], stored["deliver_to"]) == ("intro", LEE_MAIL)


def test_a_sibling_ask_not_bound_to_the_row_fills_nothing(tmp_path):
    """Same person, no loopId and no shared evidence: another ask must not arm this row's close."""
    _task(tmp_path, "jb", **JORDAN)
    sibling = {"type": "reply", "channel": "gmail", "contactName": "Jordan Bell",
               "contactIdentifier": "jordan@example.net", "contextSummary": "Book 30 minutes with Jordan",
               "fulfillment": "schedule"}
    cr.resolve({"today": "2026-11-12", "local": {}, "new_actions": [sibling]}, NOW)
    assert not _row(tmp_path, "jb").get("fulfillment")
    assert _check({}, events=[_meeting()])["closed"] == 0


@pytest.mark.parametrize("same_loop_id", [True, False])
def test_a_scheduling_capture_bound_to_a_review_task_never_closes_it_on_a_meeting(tmp_path, same_loop_id):
    """PR #993 review: a later capture names the open review task (exact loopId or the same email)
    but carries a separate scheduling ask; an accepted meeting with that person must not close the review."""
    row = _task(tmp_path, "legal", **{
        **JORDAN, "summary": "Jordan asked for written comments on the term sheet",
        "source_refs": [{"sourceType": "email", "sourceId": "multi-ask",
                         "snippet": "Please send written comments on the term sheet."}],
    })
    action = {
        "type": "reply", "channel": "gmail", "contactName": row["contact_name"],
        "contactIdentifier": row["contact_identifier"], "newObligation": False,
        "contextSummary": "Jordan separately asked to find time for a founder call",
        "contextAsk": "Can we find a time for a founder call?", "fulfillment": "schedule",
        "evidence": [{"sourceType": "email", "sourceId": "multi-ask",
                      "snippet": "Can we find a time for a founder call?"}],
        **({"loopId": row["anchor_key"]} if same_loop_id else {}),
    }
    cr.resolve({"today": "2026-11-12", "local": {}, "new_actions": [action]}, NOW)
    assert _check({}, events=[_meeting(eid="founder-call")])["closed"] == 0
    saved = _row(tmp_path, "legal")
    assert saved["summary"] == row["summary"] and saved["status"] == "open"
    assert not saved.get("fulfillment")


# ── Facts (root-causes 1b) ───────────────────────────────────────────────────────────────────────

FACT_NOW = datetime(2026, 11, 12, 9, 0, 0)


def _facts(name, ident, *texts):
    ku.apply({"person_updates": [{"person_name": name, "identifier": ident, "facts": [
        {"fact": text, "memory_type": "context", "confidence": 0.65, "source": "observed_message",
         "source_ref": f"gmail:{i}", "evidence_refs": [f"gmail:{i}"], "observed_date": "2026-11-01"}
        for i, text in enumerate(texts)]}]}, FACT_NOW)
    fh._people_cache.clear()
    person = kg.parse_person_file(open(kg.find_person_file(identifier=ident)).read())
    return [fact.text for _, fact in fh.readable_facts(list(person.facts.items()), person)]


def test_near_duplicate_affiliations_show_once():
    assert len(_facts("Jules Brandt", "jules@example.net", "Partner at Northstar & Co.",
                      "Partner at Northstar and Co.", "Partner at the Northstar and Co.")) == 1


@xfail("F2: a company fact filed under a person stays on the person")
def test_a_company_fact_is_not_stored_on_the_person():
    assert _facts("Jack Holm", "jack@northstarlabs.test", "Northstar Labs's office is at 1 Example Plaza.") == []


def test_a_fact_leading_with_another_graph_persons_full_name_is_rejected():
    _facts("Kira Holt", "kira@example.net")
    assert _facts("Dana Roe", "dana@example.net", "Kira Holt serves on the Delta Labs board.") == []


def test_a_fact_leading_with_another_graph_persons_first_name_is_rejected():
    _facts("Kira Holt", "kira@example.net")
    assert _facts("Dana Roe", "dana@example.net", "Kira serves on the Delta Labs board.") == []


def test_a_split_record_is_never_merged_automatically():
    """Mrutyunjay shape (F5): one record named by phone, one by name and company, no shared identifier."""
    _facts("Mo Reddy", "+12025550179", "Founder at Ledgerly.")
    _facts("Mo Reddy (Ledgerly)", "mo@ledgerly.test", "Building payments tools.")
    assert kg.find_person_file(identifier="+12025550179") != kg.find_person_file(identifier="mo@ledgerly.test")


def test_a_one_off_scheduling_arrangement_is_not_stored():
    assert _facts("Ash Mehta", "ash@example.net", "Offered Tue 10:00, got 10:15 instead.") == []
