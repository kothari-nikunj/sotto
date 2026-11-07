"""Admitted full RSVP reviews through the original writer, synthetic Google only."""

import copy
import hashlib
from types import SimpleNamespace

import pytest

import runtime_owner_calendar_tool as tool
from test_runtime_owner_calendar_tool import context, flow, request

__all__ = ["context", "flow"]


@pytest.fixture
def invitation(flow, monkeypatch):
    call, _, calendar, grants, policy, data, _ = flow
    calendar.event["attendees"] = [
        {"email": "owner@example.com", "self": True, "responseStatus": "needsAction", "displayName": "Owner"},
        {"email": "guest@example.com", "responseStatus": "accepted", "optional": True},
    ]
    google = tool._google()
    monkeypatch.setattr(google, "_token_path", lambda: "synthetic-protected-token")
    attempts = []

    def patch(**kwargs):
        assert kwargs["calendarId"] == "primary" and kwargs["eventId"] == "event-123"
        assert kwargs["sendUpdates"] == "all" and set(kwargs["body"]) == {"attendees"}
        req = SimpleNamespace(headers={})

        def execute():
            assert req.headers == {"If-Match": calendar.event["etag"]}
            attempts.append(copy.deepcopy(kwargs))
            calendar.event.update(copy.deepcopy(kwargs["body"]))
            calendar.event["etag"] = '"version2"'
            if calendar.lost:
                raise TimeoutError("synthetic accepted response loss")
            return copy.deepcopy(calendar.event)

        req.execute = execute
        return req

    monkeypatch.setattr(calendar, "patch", patch)

    def prepare(response="accepted", comment=None):
        proposal = request(
            {"operation": "propose_rsvp", "event_id": "event-123", "response": response, **({"comment": comment} if comment is not None else {})},
            operation="preview",
            text="Accept Cedar",
        )
        preview = call(proposal)
        assert preview["status"] == "preview"
        review = preview["review"]
        approval = {
            "event_id": proposal["event_id"],
            "action_id": proposal["action_id"],
            "args_hash": hashlib.sha256(tool.canonical(proposal["args"]).encode()).hexdigest(),
            "accepted_dispatch_id": "accepted-full-rsvp-review",
            "source_revision": review["source_revision"],
        }
        return request({"operation": "rsvp", "review": review, "proposal": approval}, event="d" * 64)

    return call, prepare, calendar, attempts, grants, policy, data


@pytest.mark.parametrize("kind", ["timed", "all_day", "occurrence", "all_day_occurrence", "series"])
@pytest.mark.parametrize("response", ["accepted", "declined", "tentative"])
def test_actual_original_rsvp_preserves_event_and_review_scope(invitation, kind, response):
    call, prepare, calendar, attempts, _, policy, _ = invitation
    if kind.startswith("all_day"):
        calendar.event.update(start={"date": "2026-10-06"}, end={"date": "2026-10-07"})
    if "occurrence" in kind:
        calendar.event["recurringEventId"] = "series-parent"
        calendar.event["originalStartTime"] = copy.deepcopy(calendar.event["start"])
    if kind == "series":
        calendar.event["recurrence"] = ["RRULE:FREQ=WEEKLY"]
    old = copy.deepcopy(calendar.event)
    body = prepare(response)
    review = body["args"]["review"]
    assert not attempts
    rendered = tool.render(review)
    if kind == "series":
        assert "entire recurring series" in rendered and "(all occurrences)" not in rendered
    if kind.startswith("all_day"):
        assert "ends before 2026-10-07" in rendered
    result = call(body)
    assert (
        result["status"] == "committed" and result["outcome"]["response"] == response and len(attempts) == 1
    )
    assert calendar.event["attendees"][1] == old["attendees"][1]
    assert calendar.event["attendees"][0] == {**old["attendees"][0], "responseStatus": response}
    assert {k: v for k, v in calendar.event.items() if k not in ("etag", "attendees")} == {
        k: v for k, v in old.items() if k not in ("etag", "attendees")
    }
    body["operation"] = "inspect"
    policy["_source_authority"] = lambda: {"complete": False}
    assert call(body) == result and len(attempts) == 1


@pytest.mark.parametrize(
    "case",
    [
        "ambiguous",
        "missing",
        "omitted",
        "duplicate_email",
        "noop",
        "bad_scope",
        "bad_response",
        "invalid_self",
    ],
)
def test_refused_provider_identity_and_scope_before_contact(invitation, case):
    call, _, calendar, attempts, _, _, _ = invitation
    if case == "ambiguous":
        calendar.event["attendees"][1]["self"] = True
    elif case == "missing":
        calendar.event["attendees"] = []
    elif case == "omitted":
        calendar.event["attendeesOmitted"] = True
    elif case == "duplicate_email":
        calendar.event["attendees"][1]["email"] = "owner@example.com"
    elif case == "noop":
        calendar.event["attendees"][0]["responseStatus"] = "accepted"
    elif case == "bad_scope":
        calendar.event["recurringEventId"] = "parent"
    elif case == "invalid_self":
        calendar.event["attendees"][0]["self"] = "true"
    else:
        calendar.event["attendees"][0]["responseStatus"] = "invented"
    proposal = request(
        {"operation": "propose_rsvp", "event_id": "event-123", "response": "accepted"}, operation="preview"
    )
    assert call(proposal)["status"] == "rejected" and not attempts


@pytest.mark.parametrize("change", ["etag", "self", "consent"])
def test_changed_review_refuses_before_contact(invitation, change):
    call, prepare, calendar, attempts, grants, _, _ = invitation
    body = prepare()
    if change == "etag":
        calendar.event["etag"] = '"changed"'
    elif change == "self":
        calendar.event["attendees"][0]["email"] = "different@example.com"
    else:
        grants["calendar_write"] = False
    assert call(body)["status"] == "rejected" and not attempts


def test_original_writer_own_get_pins_review(invitation, monkeypatch):
    call, prepare, calendar, attempts, _, _, _ = invitation
    body = prepare()
    get = calendar.get
    count = [0]

    def racing(**kwargs):
        req = get(**kwargs)
        execute = req.execute

        def result():
            count[0] += 1
            if count[0] == 2:
                calendar.event["etag"] = '"writer-race"'
            return execute()

        req.execute = result
        return req

    monkeypatch.setattr(calendar, "get", racing)
    assert call(body) == {"version": 1, "status": "unknown"} and not attempts
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"} and not attempts


def test_accepted_response_loss_never_reapplies(invitation):
    call, prepare, calendar, attempts, _, _, _ = invitation
    body = prepare()
    calendar.lost = True
    assert call(body) == {"version": 1, "status": "unknown"} and len(attempts) == 1
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"}
    body["operation"] = "apply"
    assert call(body) == {"version": 1, "status": "unknown"} and len(attempts) == 1


@pytest.mark.parametrize("change", ["id", "cancelled", "self"])
def test_writer_second_get_cannot_change_target_or_self(invitation, monkeypatch, change):
    call, prepare, calendar, attempts, _, _, _ = invitation
    body = prepare()
    get = calendar.get
    count = [0]

    def racing(**kwargs):
        req = get(**kwargs)
        execute = req.execute

        def result():
            count[0] += 1
            value = execute()
            if count[0] == 2:
                if change == "id":
                    value["id"] = "foreign-event"
                elif change == "cancelled":
                    value["status"] = "cancelled"
                else:
                    value["attendees"][0]["email"] = "foreign@example.com"
            return value

        req.execute = result
        return req

    monkeypatch.setattr(calendar, "get", racing)
    assert call(body) == {"version": 1, "status": "unknown"} and not attempts


def test_rsvp_receipt_failure_and_preferences_preservation(invitation, monkeypatch):
    call, prepare, _, attempts, _, _, _ = invitation
    body = prepare()
    prefs = tool._preferences()
    write = prefs._durable_preferences_replace
    count = [0]

    def fail(path, data):
        count[0] += 1
        if count[0] == 2:
            raise OSError("synthetic receipt loss")
        return write(path, data)

    monkeypatch.setattr(prefs, "_durable_preferences_replace", fail)
    assert call(body) == {"version": 1, "status": "unknown"} and len(attempts) == 1
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"}
    monkeypatch.setattr(prefs, "_durable_preferences_replace", write)
    before = prefs.jsonstore.read(prefs._path())["owner_preference_receipts"]
    prefs.add("vip_people", "Dana")
    assert prefs.jsonstore.read(prefs._path())["owner_preference_receipts"] == before


def test_same_if_match_transport_retry_has_one_accepted_rsvp(invitation, monkeypatch):
    call, prepare, calendar, attempts, _, _, _ = invitation
    body = prepare()
    patch = calendar.patch
    wire = []

    def retrying(**kwargs):
        req = patch(**kwargs)
        execute = req.execute

        def result():
            wire.append(dict(req.headers))
            execute()
            wire.append(dict(req.headers))
            assert req.headers["If-Match"] != calendar.event["etag"]
            raise PermissionError("synthetic repeated precondition fails")

        req.execute = result
        return req

    monkeypatch.setattr(calendar, "patch", retrying)
    assert call(body) == {"version": 1, "status": "unknown"}
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"}
    assert len(wire) == 2 and len(attempts) == 1


@pytest.mark.parametrize("old_response", ["needsAction", "accepted"])
def test_reviewed_comment_uses_original_writer_and_survives_unknown_inspection(invitation, old_response):
    call, prepare, calendar, attempts, _, _, _ = invitation
    calendar.event["attendees"][0].update(responseStatus=old_response, comment="Prior note")
    before = copy.deepcopy(calendar.event)
    body = prepare("accepted", "I can join after 3pm.")
    review = body["args"]["review"]
    assert (review["old_comment"], review["new_comment"]) == ("Prior note", "I can join after 3pm.")
    assert "Your comment before: Prior note" in tool.render(review)
    assert "Your comment after: I can join after 3pm." in tool.render(review)
    assert attempts == []
    calendar.lost = True
    assert call(body)["status"] == "unknown"
    assert len(attempts) == 1
    assert calendar.event["attendees"][1] == before["attendees"][1]
    assert calendar.event["attendees"][0] == {**before["attendees"][0], "responseStatus": "accepted", "comment": "I can join after 3pm."}
    body["operation"] = "inspect"
    assert call(body)["status"] == "unknown" and len(attempts) == 1
    body["operation"] = "apply"
    assert call(body)["status"] == "unknown" and len(attempts) == 1


def test_comment_review_rejects_changed_source_and_true_noop(invitation):
    call, prepare, calendar, attempts, _, _, _ = invitation
    calendar.event["attendees"][0].update(responseStatus="accepted", comment="Prior note")
    with pytest.raises(AssertionError):
        prepare("accepted", "Prior note")
    with pytest.raises(AssertionError):
        prepare("accepted")
    body = prepare("accepted", "New note")
    calendar.event["attendees"][0]["comment"] = "Changed while waiting"
    assert call(body)["status"] == "rejected" and attempts == []


def test_omitted_comment_preserves_legacy_review_and_existing_own_note(invitation):
    call, prepare, calendar, attempts, _, _, _ = invitation
    calendar.event["attendees"][0]["comment"] = "Keep this note"
    body = prepare()
    review = body["args"]["review"]
    assert "old_comment" not in review and "new_comment" not in review
    assert "Your comment" not in tool.render(review)
    assert call(body)["status"] == "committed" and len(attempts) == 1
    assert calendar.event["attendees"][0]["comment"] == "Keep this note"


@pytest.mark.parametrize("old", [None, "", "Existing note"])
def test_comment_readback_preserves_previous_presence_exactly(invitation, old):
    call, prepare, calendar, attempts, _, _, _ = invitation
    if old is not None:
        calendar.event["attendees"][0]["comment"] = old
    body = prepare("accepted", "  A note\nSecond line  ")
    assert call(body)["status"] == "committed" and len(attempts) == 1
    assert calendar.event["attendees"][0]["comment"] == "  A note\nSecond line  "
    assert tool._rsvped(tool._google(), calendar.event, body["args"]["review"])
    body["operation"] = "inspect"
    assert call(body)["status"] == "committed" and len(attempts) == 1


def test_explicit_empty_comment_does_not_clear_existing_note(invitation):
    call, prepare, calendar, attempts, _, _, _ = invitation
    calendar.event["attendees"][0]["comment"] = "Retain exactly"
    body = prepare("accepted", "")
    assert "Your comment after: Unchanged" in tool.render(body["args"]["review"])
    assert call(body)["status"] == "committed" and len(attempts) == 1
    assert calendar.event["attendees"][0]["comment"] == "Retain exactly"
    body["operation"] = "inspect"
    assert call(body)["status"] == "committed" and len(attempts) == 1
