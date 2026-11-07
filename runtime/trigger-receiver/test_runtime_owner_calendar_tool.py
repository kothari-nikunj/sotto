"""Actual admitted review and original Calendar PATCH against synthetic Google resources."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import runtime_owner_calendar_tool as tool
from test_runtime_owner_loop_close import context, IDENTITY, ROOT, WHEN, THREAD, capability

__all__ = ["context"]


class Calendar:
    def __init__(self):
        self.event = {
            "id": "event-123",
            "etag": '"version1"',
            "summary": "Cedar review",
            "htmlLink": "https://calendar.google.com/event",
            "start": {"dateTime": "2026-10-06T14:00:00-07:00"},
            "end": {"dateTime": "2026-10-06T14:30:00-07:00"},
            "attendees": [{"email": "guest@example.com"}],
            "conferenceData": {"conferenceId": "keep-conference"},
            "location": "Existing address",
            "description": "Existing agenda",
        }
        self.patches = []
        self.lost = False
        self.bad_result = False
        self.gets = 0

    def events(self):
        return self

    def get(self, **kwargs):
        assert kwargs == {"calendarId": "primary", "eventId": "event-123"}

        def execute():
            self.gets += 1
            return copy.deepcopy(self.event)

        return SimpleNamespace(execute=execute)

    def patch(self, **kwargs):
        assert kwargs["calendarId"] == "primary" and kwargs["eventId"] == "event-123"
        assert kwargs["sendUpdates"] == "all" and set(kwargs["body"]) == {"start", "end"}
        request = SimpleNamespace(headers={})

        def execute():
            assert request.headers == {"If-Match": self.event["etag"]}
            self.patches.append(copy.deepcopy(kwargs))
            self.event.update(copy.deepcopy(kwargs["body"]))
            self.event["etag"] = '"version2"'
            if self.lost:
                raise TimeoutError("synthetic response loss after provider acceptance")
            result = copy.deepcopy(self.event)
            if self.bad_result:
                result["start"]["dateTime"] = "2026-10-06T10:00:00-07:00"
            return result

        request.execute = execute
        return request


def request(args, operation="apply", event="a" * 64, text="yes"):
    encoded = tool.canonical(args)
    action = hashlib.sha256(
        json.dumps([event, tool.KIND, encoded], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "version": 1,
        "operation": operation,
        "identity": IDENTITY,
        "action_id": action,
        "event_id": event,
        "payload_hash": "b" * 64,
        "text_hash": hashlib.sha256(text.encode()).hexdigest(),
        "text": text,
        "admitted_at_ms": int(WHEN.timestamp() * 1000),
        "provider_thread_id": THREAD,
        "args": args,
    }


@pytest.fixture
def flow(context, monkeypatch):
    data, policy, activation = context
    policy.update(
        SOTTO_DATA=str(data),
        SOTTO_HOME=str(data / "sotto-home"),
        SOTTO_RUNTIME_PROFILE="managed-photon-fixed-five-v1",
        _source_authority=lambda: {"complete": True, "revision": "c" * 64, "google_sources": ["calendar"]},
    )
    monkeypatch.delenv("SOTTO_UNATTENDED", raising=False)
    prefs = tool._preferences()
    monkeypatch.setattr(tool, "_preferences", lambda: prefs)
    google = tool._google()
    calendar = Calendar()
    grants = {"calendar_read": True, "calendar_write": True}
    monkeypatch.setattr(google, "capabilities", lambda: grants)
    monkeypatch.setattr(google, "_google_service", lambda *args: calendar)
    monkeypatch.setattr(tool, "_google", lambda: google)

    def call(body):
        return tool.perform(
            body,
            bearer=capability.derive(ROOT, IDENTITY),
            root=ROOT,
            policy=policy,
            data_root=data,
            capability=capability,
            activation=activation,
        )

    proposal = request(
        {
            "operation": "propose_move",
            "event_id": "event-123",
            "start": "2026-10-07T15:00:00-07:00",
            "end": "2026-10-07T15:30:00-07:00",
        },
        operation="preview",
        text="Move Cedar review tomorrow",
    )

    def prepare():
        preview = call(proposal)
        assert preview["status"] == "preview"
        review = preview["review"]
        approval = {
            "event_id": proposal["event_id"],
            "action_id": proposal["action_id"],
            "args_hash": hashlib.sha256(tool.canonical(proposal["args"]).encode()).hexdigest(),
            "accepted_dispatch_id": "accepted-full-review",
            "source_revision": review["source_revision"],
        }
        return request({"operation": "move", "review": review, "proposal": approval}, event="d" * 64)

    return call, prepare, calendar, grants, policy, data, proposal


def test_actual_original_patch_complete_review_and_committed_inspect(flow, monkeypatch):
    call, prepare, calendar, _, policy, data, _ = flow
    original = copy.deepcopy(calendar.event)
    body = prepare()
    assert not calendar.patches
    assert tool.render(body["args"]["review"]).endswith("Guests will be notified. Move this meeting?")
    result = call(body)
    assert result["status"] == "committed" and len(calendar.patches) == 1
    for key in ("id", "attendees", "conferenceData", "location", "description"):
        assert calendar.event[key] == original[key]
    assert result["outcome"]["start"] == body["args"]["review"]["new_start"]
    body["operation"] = "inspect"
    policy["_source_authority"] = lambda: {"complete": False}
    monkeypatch.setattr(
        calendar, "get", lambda **kwargs: pytest.fail("committed replay does not read Google")
    )
    assert call(body) == result and len(calendar.patches) == 1
    import preferences

    before = preferences.jsonstore.read(preferences._path())["owner_preference_receipts"]
    preferences.add("vip_people", "Dana")
    assert preferences.jsonstore.read(preferences._path())["owner_preference_receipts"] == before


@pytest.mark.parametrize("case", ["etag", "revoked", "write_scope", "all_day", "series"])
def test_stale_unsupported_and_consent_refuse_before_contact(flow, case):
    call, prepare, calendar, grants, policy, _, proposal = flow
    if case in ("all_day", "series"):
        if case == "all_day":
            calendar.event["start"] = {"date": "2026-10-06"}
        else:
            calendar.event["recurrence"] = ["RRULE:FREQ=WEEKLY"]
        assert call(proposal)["code"] == "unsupported_event"
    else:
        body = prepare()
        if case == "etag":
            calendar.event["etag"] = '"changed"'
        elif case == "revoked":
            policy["_source_authority"] = lambda: {
                "complete": True,
                "revision": "e" * 64,
                "google_sources": [],
            }
        else:
            grants["calendar_write"] = False
        assert call(body)["status"] == "rejected"
    assert not calendar.patches


@pytest.mark.parametrize("failure", ["response", "bad_result", "receipt"])
def test_postcontact_uncertainty_never_repeats_patch(flow, monkeypatch, failure):
    call, prepare, calendar, _, _, _, _ = flow
    body = prepare()
    if failure == "response":
        calendar.lost = True
    elif failure == "bad_result":
        calendar.bad_result = True
    else:
        prefs = tool._preferences()
        write = prefs._durable_preferences_replace
        count = [0]

        def fail(path, data):
            count[0] += 1
            if count[0] == 2:
                raise OSError("receipt persistence failed")
            return write(path, data)

        monkeypatch.setattr(prefs, "_durable_preferences_replace", fail)
    assert call(body) == {"version": 1, "status": "unknown"}
    assert len(calendar.patches) == 1
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"}
    body["operation"] = "apply"
    assert call(body) == {"version": 1, "status": "unknown"}
    assert len(calendar.patches) == 1


@pytest.mark.parametrize("change", ["revision", "revoked"])
def test_post_acceptance_authority_change_keeps_committed_move(flow, monkeypatch, change):
    call, prepare, calendar, _, policy, _, _ = flow
    body = prepare()
    original = calendar.patch

    def patch(**kwargs):
        request = original(**kwargs)
        execute = request.execute

        def accepted():
            result = execute()
            policy["_source_authority"] = (
                (lambda: {"complete": True, "revision": "e" * 64, "google_sources": ["calendar"]})
                if change == "revision"
                else (lambda: {"complete": False})
            )
            return result

        request.execute = accepted
        return request

    monkeypatch.setattr(calendar, "patch", patch)
    reads = calendar.gets
    result = call(body)
    assert result["status"] == "committed" and len(calendar.patches) == 1
    assert calendar.gets == reads + 1  # the adapter's pre-write read; no private read-back after the change
    assert call(dict(body, operation="inspect")) == result
    assert call(body) == result and len(calendar.patches) == 1


def test_proposal_consumed_once_and_confirmation_distinct(flow):
    call, prepare, calendar, _, _, _, _ = flow
    body = prepare()
    assert call(body)["status"] == "committed"
    second = request(body["args"], event="e" * 64)
    assert call(second)["code"] == "event_conflict" and len(calendar.patches) == 1


def test_exact_recurring_occurrence_is_supported(flow):
    call, prepare, calendar, _, _, _, _ = flow
    calendar.event["recurringEventId"] = "series-parent"
    calendar.event["originalStartTime"] = copy.deepcopy(calendar.event["start"])
    assert call(prepare())["status"] == "committed"
    assert calendar.event["recurringEventId"] == "series-parent"


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-10-07T15:00:00", "2026-10-07T16:00:00"),
        ("2026-10-07T15:00:00-07:00", "2026-10-07T14:00:00-07:00"),
    ],
)
def test_invalid_times_never_read_or_write_provider(flow, start, end):
    call, _, calendar, _, _, _, proposal = flow
    proposal = request({**proposal["args"], "start": start, "end": end}, operation="preview")
    with pytest.raises(ValueError):
        call(proposal)
    assert calendar.gets == 0 and not calendar.patches


def test_same_owner_event_cannot_confirm_its_own_review(flow):
    call, prepare, calendar, _, _, _, proposal = flow
    body = request(prepare()["args"], event=proposal["event_id"])
    assert call(body)["code"] == "review_unavailable"
    assert not calendar.patches


def test_changed_original_proof_cannot_reuse_committed_receipt(flow):
    call, prepare, calendar, _, _, _, _ = flow
    body = prepare()
    assert call(body)["status"] == "committed"
    changed = request(body["args"], event=body["event_id"], text="confirm")
    changed["operation"] = "inspect"
    assert call(changed)["code"] == "event_conflict"
    assert len(calendar.patches) == 1


def test_provider_permission_denial_holds_without_retry_or_copy(flow, monkeypatch):
    call, prepare, calendar, _, _, _, _ = flow
    body = prepare()
    original = copy.deepcopy(calendar.event)
    attempts = []

    def denied(**kwargs):
        request = SimpleNamespace(headers={})

        def execute():
            assert request.headers == {"If-Match": original["etag"]}
            attempts.append(kwargs)
            raise PermissionError("synthetic read-only attendee event")

        request.execute = execute
        return request

    monkeypatch.setattr(calendar, "patch", denied)
    assert call(body) == {"version": 1, "status": "unknown"}
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"}
    assert len(attempts) == 1 and calendar.event == original


def test_equivalent_offset_dst_result_and_original_audit(flow, monkeypatch):
    from datetime import datetime, timezone

    call, _, calendar, _, _, _, proposal = flow
    proposal = request(
        {**proposal["args"], "start": "2026-11-01T01:30:00-07:00", "end": "2026-11-01T01:30:00-08:00"},
        operation="preview",
    )
    preview = call(proposal)
    review = preview["review"]
    approval = {
        "event_id": proposal["event_id"],
        "action_id": proposal["action_id"],
        "args_hash": hashlib.sha256(tool.canonical(proposal["args"]).encode()).hexdigest(),
        "accepted_dispatch_id": "accepted-dst-review",
        "source_revision": review["source_revision"],
    }
    body = request({"operation": "move", "review": review, "proposal": approval}, event="d" * 64)
    patch = calendar.patch

    def normalized(**kwargs):
        request = patch(**kwargs)
        execute = request.execute

        def result():
            execute()
            for field in ("start", "end"):
                value = calendar.event[field]["dateTime"]
                calendar.event[field]["dateTime"] = (
                    datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
                )
            return copy.deepcopy(calendar.event)

        request.execute = result
        return request

    monkeypatch.setattr(calendar, "patch", normalized)
    result = call(body)
    assert result["status"] == "committed"
    assert result["outcome"]["start"] == review["new_start"] and result["outcome"]["end"] == review["new_end"]
    audit = [json.loads(line) for line in Path(tool._google()._sends_path()).read_text().splitlines()]
    assert (
        audit[-1]["verb"] == "calendar-update"
        and audit[-1]["event_id"] == "event-123"
        and audit[-1]["result"] == "written"
    )
    assert "summary" not in audit[-1] and "start" not in audit[-1]


def test_equivalent_instant_noop_is_refused_before_contact(flow):
    call, _, calendar, _, _, _, proposal = flow
    proposal = request(
        {**proposal["args"], "start": "2026-10-06T21:00:00Z", "end": "2026-10-06T21:30:00Z"},
        operation="preview",
    )
    assert call(proposal)["code"] == "invalid_target"
    assert not calendar.patches


def test_transport_retry_same_etag_cannot_repeat_accepted_mutation(flow, monkeypatch):
    call, prepare, calendar, _, _, _, _ = flow
    body = prepare()
    patch = calendar.patch
    wire_attempts = []

    def retrying(**kwargs):
        request = patch(**kwargs)
        execute = request.execute

        def lost_then_precondition():
            wire_attempts.append(dict(request.headers))
            execute()  # Synthetic first PATCH accepted, response lost, ETag advances.
            wire_attempts.append(dict(request.headers))
            assert request.headers["If-Match"] != calendar.event["etag"]
            raise PermissionError("synthetic repeated If-Match rejected with 412")

        request.execute = lost_then_precondition
        return request

    monkeypatch.setattr(calendar, "patch", retrying)
    assert call(body) == {"version": 1, "status": "unknown"}
    assert len(wire_attempts) == 2 and len(calendar.patches) == 1
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"}
    body["operation"] = "apply"
    assert call(body) == {"version": 1, "status": "unknown"}
    assert len(wire_attempts) == 2 and len(calendar.patches) == 1


@pytest.fixture
def detail_flow(flow, monkeypatch):
    call, _, calendar, grants, policy, data, _ = flow

    def patch(**kwargs):
        assert kwargs["calendarId"] == "primary" and kwargs["eventId"] == "event-123"
        assert kwargs["sendUpdates"] == "all" and set(kwargs["body"]) <= {
            "summary",
            "location",
            "description",
            "start",
            "end",
        }
        req = SimpleNamespace(headers={})

        def execute():
            assert req.headers == {"If-Match": calendar.event["etag"]}
            calendar.patches.append(copy.deepcopy(kwargs))
            calendar.event.update(copy.deepcopy(kwargs["body"]))
            calendar.event["etag"] = '"version2"'
            if calendar.lost:
                raise TimeoutError("accepted response lost")
            returned = copy.deepcopy(calendar.event)
            if calendar.bad_result:
                returned["description"] = "normalized to another text"
            return returned

        req.execute = execute
        return req

    monkeypatch.setattr(calendar, "patch", patch)

    def prepare(fields, *, event="a" * 64):
        proposed = request(
            {"operation": "propose_update", "event_id": "event-123", "fields": fields},
            operation="preview",
            event=event,
            text="Change the meeting details",
        )
        result = call(proposed)
        assert result["status"] == "preview"
        approval = {
            "event_id": proposed["event_id"],
            "action_id": proposed["action_id"],
            "args_hash": hashlib.sha256(tool.canonical(proposed["args"]).encode()).hexdigest(),
            "accepted_dispatch_id": "accepted-complete-detail-review",
            "source_revision": result["review"]["source_revision"],
        }
        return request(
            {"operation": "update", "review": result["review"], "proposal": approval}, event="d" * 64
        )

    return call, prepare, calendar, grants, policy, data


@pytest.mark.parametrize(
    "scope,all_day", [("single", False), ("single", True), ("occurrence", False), ("series", True)]
)
def test_details_original_conditional_patch_preserves_unrequested_fields(detail_flow, scope, all_day):
    call, prepare, calendar, _, _, _ = detail_flow
    if all_day:
        calendar.event.update(start={"date": "2026-10-06"}, end={"date": "2026-10-07"})
    if scope == "occurrence":
        calendar.event.update(
            recurringEventId="master-id", originalStartTime=copy.deepcopy(calendar.event["start"])
        )
    elif scope == "series":
        calendar.event["recurrence"] = ["RRULE:FREQ=WEEKLY"]
    before = copy.deepcopy(calendar.event)
    body = prepare(
        {"summary": "Updated title", "location": "123 Cedar Street", "description": "First line\nSecond line"}
    )
    review = body["args"]["review"]
    assert review["event_scope"] == scope and review["all_day"] is all_day
    assert review["before_fields"] == {key: before[key] for key in review["fields"]}
    assert not calendar.patches
    result = call(body)
    assert result["status"] == "committed" and result["outcome"] == {
        "operation": "update",
        "calendar_id": "primary",
        "event_id": "event-123",
        "review_hash": review["review_hash"],
        "status": "updated",
    }
    assert len(calendar.patches) == 1 and calendar.patches[0]["body"] == review["fields"]
    for field in (
        "start",
        "end",
        "attendees",
        "conferenceData",
        "recurrence",
        "recurringEventId",
        "originalStartTime",
    ):
        assert calendar.event.get(field) == before.get(field)
    assert call(dict(body, operation="inspect")) == result and call(body) == result
    assert len(calendar.patches) == 1


def test_details_explicit_clear_and_omission_are_distinct(detail_flow):
    call, prepare, calendar, _, _, _ = detail_flow
    before = copy.deepcopy(calendar.event)
    body = prepare({"summary": "", "description": ""})
    text = tool.render(body["args"]["review"])
    assert "After:\n(no title)" in text and "After:\n(empty)" in text
    assert call(body)["status"] == "committed"
    assert calendar.event["summary"] == "" and calendar.event["description"] == ""
    assert calendar.event["location"] == before["location"]


@pytest.mark.parametrize("failure", ["etag", "grant", "wrong_operation", "forged_before"])
def test_details_precontact_refusal_no_patch(detail_flow, failure):
    call, prepare, calendar, grants, _, _ = detail_flow
    body = prepare({"description": "New agenda"})
    if failure == "etag":
        calendar.event["etag"] = '"changed"'
    elif failure == "grant":
        grants["calendar_write"] = False
    elif failure == "wrong_operation":
        body = request({**body["args"], "operation": "move"}, event="d" * 64)
    else:
        review = copy.deepcopy(body["args"]["review"])
        review["before_fields"]["description"] = "Invented previous agenda"
        review["review_hash"] = hashlib.sha256(
            tool.canonical({k: v for k, v in review.items() if k != "review_hash"}).encode()
        ).hexdigest()
        body = request({**body["args"], "review": review}, event="d" * 64)
    if failure == "wrong_operation":
        with pytest.raises(ValueError):
            call(body)
    else:
        assert call(body)["status"] == "rejected"
    assert not calendar.patches


@pytest.mark.parametrize("failure", ["lost", "returned_mismatch", "readback_mismatch"])
def test_details_unknown_never_repatch_and_safe_original_read(detail_flow, monkeypatch, failure):
    call, prepare, calendar, _, _, _ = detail_flow
    body = prepare({"description": "Exact agenda"})
    if failure == "lost":
        calendar.lost = True
    elif failure == "returned_mismatch":
        calendar.bad_result = True
    else:
        original_get = calendar.get

        def get(**kwargs):
            req = original_get(**kwargs)
            execute = req.execute

            def read():
                value = execute()
                if calendar.patches:
                    value["description"] = "Changed after accepted PATCH"
                return value

            req.execute = read
            return req

        monkeypatch.setattr(calendar, "get", get)
    assert call(body)["status"] == "unknown"
    assert call(dict(body, operation="inspect"))["status"] == "unknown"
    assert call(body)["status"] == "unknown" and len(calendar.patches) == 1
    assert calendar.get(calendarId="primary", eventId="event-123").execute()["id"] == "event-123"


def test_details_full_review_and_hash_unicode_vector(detail_flow):
    call, prepare, calendar, _, _, _ = detail_flow
    calendar.event.update(
        start={"date": "2026-10-06"}, end={"date": "2026-10-07"}, recurrence=["RRULE:FREQ=MONTHLY"]
    )
    body = prepare({"summary": "Café 🌲", "description": "Agenda\nTwo"})
    review = body["args"]["review"]
    assert tool.render(review) == (
        "Update: Cedar review\nCalendar: primary\nApplies to: entire recurring series\n"
        "When: All-day 2026-10-06; ends before 2026-10-07\n\nTitle:\nBefore:\nCedar review\nAfter:\nCafé 🌲\n"
        "\nAgenda:\nBefore:\nExisting agenda\nAfter:\nAgenda\nTwo\n\nGuests will be notified. Update the entire series?"
    )
    assert (
        review["review_hash"]
        == hashlib.sha256(
            tool.canonical({k: v for k, v in review.items() if k != "review_hash"}).encode()
        ).hexdigest()
    )


def test_detail_shared_nested_unicode_hash_vector():
    review = json.loads(
        '{\n  "operation": "update",\n  "calendar_id": "primary",\n  "event_id": "event_one",\n  "etag": "\\"version1\\"",\n  "summary": "Café 🗓",\n  "html_link": "",\n  "event_scope": "single",\n  "all_day": true,\n  "start": "2026-10-07",\n  "end": "2026-10-08",\n  "recurring_event_id": null,\n  "original_start": null,\n  "before_fields": {\n    "summary": "Café 🗓",\n    "location": "Old place",\n    "description": "Old\\nagenda"\n  },\n  "fields": {\n    "summary": "Café planning 🗓",\n    "location": "",\n    "description": "First line\\nSecond line"\n  },\n  "notifications": "all",\n  "source_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",\n  "review_hash": "4d010363ed612535a5562bda97cdd8821f43d2907e3796dc1a02ea8da19e56f3"\n}\n'
    )
    tool._validate_review(review)
    assert review["review_hash"] == "4d010363ed612535a5562bda97cdd8821f43d2907e3796dc1a02ea8da19e56f3"


def test_details_current_label_uses_new_singleline_control_boundary(detail_flow):
    call, _, calendar, _, _, _ = detail_flow
    calendar.event["summary"] = "Existing\u2028injected label"
    body = request(
        {"operation": "propose_update", "event_id": "event-123", "fields": {"location": "123 Main Street"}},
        operation="preview",
    )
    assert call(body)["code"] == "review_unavailable"
    assert not calendar.patches


def combined_prepare(call, fields=None):
    proposed = request(
        {
            "operation": "propose_move_update",
            "event_id": "event-123",
            "start": "2026-10-07T15:00:00-07:00",
            "end": "2026-10-07T16:00:00-07:00",
            "fields": fields or {"location": "", "description": "New\nagenda"},
        },
        operation="preview",
        text="Move and update Cedar",
    )
    preview = call(proposed)
    approval = {
        "event_id": proposed["event_id"],
        "action_id": proposed["action_id"],
        "args_hash": hashlib.sha256(tool.canonical(proposed["args"]).encode()).hexdigest(),
        "accepted_dispatch_id": "accepted-combined-review",
        "source_revision": preview["review"]["source_revision"],
    }
    return request(
        {"operation": "move_update", "review": preview["review"], "proposal": approval}, event="d" * 64
    )


@pytest.mark.parametrize("occurrence", [False, True])
def test_combined_original_one_patch_and_historical_recovery(detail_flow, occurrence):
    call, _, calendar, _, _, _ = detail_flow
    if occurrence:
        calendar.event.update(
            recurringEventId="parent", originalStartTime={"dateTime": calendar.event["start"]["dateTime"]}
        )
    original = copy.deepcopy(calendar.event)
    body = combined_prepare(call)
    assert calendar.patches == []
    text = tool.render(body["args"]["review"])
    assert "From: " in text and "To: " in text and "Before:\nExisting address\nAfter:\n(empty)" in text
    result = call(body)
    assert result["status"] == "committed"
    assert result["outcome"]["operation"] == "move_update"
    assert result["outcome"]["start"] == body["args"]["review"]["new_start"]
    assert calendar.patches[0]["body"] == {
        "start": {"dateTime": "2026-10-07T15:00:00-07:00"},
        "end": {"dateTime": "2026-10-07T16:00:00-07:00"},
        "location": "",
        "description": "New\nagenda",
    }
    for key in ("attendees", "conferenceData", "summary", "recurringEventId", "originalStartTime"):
        assert calendar.event.get(key) == original.get(key)
    body["operation"] = "inspect"
    assert call(body) == result
    assert len(calendar.patches) == 1


@pytest.mark.parametrize("failure", ["stale", "revoked", "forged", "lost", "bad_result"])
def test_combined_refusal_or_uncertainty_never_reapplies(detail_flow, failure):
    call, _, calendar, grants, _, _ = detail_flow
    body = combined_prepare(call)
    if failure == "stale":
        calendar.event["etag"] = '"changed"'
    elif failure == "revoked":
        grants["calendar_write"] = False
    elif failure == "forged":
        review = body["args"]["review"]
        review["before_fields"]["location"] = "Forged"
        review["review_hash"] = hashlib.sha256(
            tool.canonical({k: v for k, v in review.items() if k != "review_hash"}).encode()
        ).hexdigest()
        body = request(body["args"], event="d" * 64)
    else:
        setattr(calendar, failure, True)
    result = call(body)
    assert result["status"] == ("unknown" if failure in ("lost", "bad_result") else "rejected")
    count = len(calendar.patches)
    assert count == (1 if failure in ("lost", "bad_result") else 0)
    body["operation"] = "inspect"
    call(body)
    assert len(calendar.patches) == count


@pytest.mark.parametrize("kind", ["all_day", "series", "no_op"])
def test_combined_scope_and_complete_noop_refused(detail_flow, kind):
    call, _, calendar, _, _, _ = detail_flow
    if kind == "all_day":
        calendar.event.update(start={"date": "2026-10-06"}, end={"date": "2026-10-07"})
    elif kind == "series":
        calendar.event["recurrence"] = ["RRULE:FREQ=WEEKLY"]
    args = {
        "operation": "propose_move_update",
        "event_id": "event-123",
        "start": "2026-10-06T14:00:00-07:00",
        "end": "2026-10-06T14:30:00-07:00",
        "fields": {"location": "Existing address"},
    }
    assert call(request(args, operation="preview"))["status"] == "rejected"
    assert calendar.patches == []


def test_combined_fresh_time_readback_mismatch_holds(detail_flow, monkeypatch):
    call, _, calendar, _, _, _ = detail_flow
    body = combined_prepare(call)
    original_get = calendar.get

    def get(**kwargs):
        req = original_get(**kwargs)

        def execute():
            result = req.execute()
            if calendar.patches:
                result["end"]["dateTime"] = "2026-10-07T17:00:00-07:00"
            return result

        return SimpleNamespace(execute=execute)

    monkeypatch.setattr(calendar, "get", get)
    assert call(body) == {"version": 1, "status": "unknown"}
    assert len(calendar.patches) == 1
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"}
    assert len(calendar.patches) == 1


@pytest.mark.parametrize("operation", ["move", "rsvp", "update", "move_update"])
@pytest.mark.parametrize("lost", [False, True])
def test_secondary_exact_pair_original_writer_and_recovery(flow, monkeypatch, operation, lost):
    call, _, calendar, _, policy, _, _ = flow
    selected = "team#calendar%@example.com"
    primary = copy.deepcopy(calendar.event)
    calendar.event["attendees"] = [
        {"email": "owner@example.com", "self": True, "responseStatus": "needsAction"}
    ]
    calendar.lost = lost
    monkeypatch.setattr(tool._google(), "_token_path", lambda: "synthetic-protected-token")
    gets = []

    def get(**kwargs):
        assert kwargs == {"calendarId": selected, "eventId": "event-123"}
        gets.append(kwargs)
        return SimpleNamespace(execute=lambda: copy.deepcopy(calendar.event))

    def patch(**kwargs):
        assert kwargs["calendarId"] == selected and kwargs["eventId"] == "event-123"
        assert kwargs["sendUpdates"] == "all"
        req = SimpleNamespace(headers={})

        def execute():
            assert req.headers == {"If-Match": calendar.event["etag"]}
            calendar.patches.append(copy.deepcopy(kwargs))
            calendar.event.update(copy.deepcopy(kwargs["body"]))
            calendar.event["etag"] = '"version2"'
            if lost:
                raise TimeoutError("synthetic response lost")
            return copy.deepcopy(calendar.event)

        req.execute = execute
        return req

    monkeypatch.setattr(calendar, "get", get)
    monkeypatch.setattr(calendar, "patch", patch)
    args = {"operation": "propose_" + operation, "event_id": "event-123", "calendar_id": selected}
    if operation in ("move", "move_update"):
        args.update(start="2026-10-07T15:00:00-07:00", end="2026-10-07T15:30:00-07:00")
    if operation in ("update", "move_update"):
        args["fields"] = {"description": "New agenda"}
    if operation == "rsvp":
        args["response"] = "tentative"
    proposal = request(args, operation="preview", text="Use the known team calendar")
    preview = call(proposal)
    assert preview["status"] == "preview" and selected in tool.render(preview["review"])
    assert not calendar.patches
    approval = {
        "event_id": proposal["event_id"],
        "action_id": proposal["action_id"],
        "args_hash": hashlib.sha256(tool.canonical(args).encode()).hexdigest(),
        "accepted_dispatch_id": "accepted-secondary-review",
        "source_revision": "c" * 64,
    }
    body = request(
        {"operation": operation, "review": preview["review"], "proposal": approval}, event="d" * 64
    )
    result = call(body)
    assert result["status"] == ("unknown" if lost else "committed")
    if not lost:
        assert result["outcome"]["calendar_id"] == selected
    assert len(calendar.patches) == 1 and len(gets) >= 2
    assert primary["description"] == "Existing agenda"  # independent primary resource remains untouched
    policy["_source_authority"] = lambda: {"complete": False}
    monkeypatch.setattr(calendar, "get", lambda **k: pytest.fail("historical inspect must not read provider"))
    assert call(dict(body, operation="inspect")) == result
    assert len(calendar.patches) == 1


@pytest.mark.parametrize("selector", ["primary", " bad", "has space", "bad\u0085", "bad\u2028", "x" * 1025])
def test_secondary_proposal_selector_refuses_before_google(flow, selector):
    call, _, calendar, _, _, _, proposal = flow
    args = {**proposal["args"], "calendar_id": selector}
    with pytest.raises(ValueError):
        call(request(args, operation="preview"))
    assert calendar.gets == 0 and not calendar.patches

def test_precontact_google_gate_refusal_is_terminal(flow, monkeypatch):
    call, prepare, calendar, _, policy, _, _ = flow
    body = prepare()
    google = tool._google()
    monkeypatch.setattr(google, '_gated', lambda *args, **kwargs: ({'status': 'error'}, True))
    assert call(body) == {'version': 1, 'status': 'rejected', 'code': 'permission_unavailable'}
    assert call(dict(body, operation='inspect')) == {'version': 1, 'status': 'rejected', 'code': 'permission_unavailable'}
    assert not calendar.patches


def test_authority_lost_after_reservation_is_terminal_without_contact(flow):
    # Consent revoked between the "contacting" reservation and provider contact must not
    # leave a permanent unknown: nothing was contacted, so the outcome is a definite no.
    call, prepare, calendar, _, policy, _, _ = flow
    body = prepare()
    granted, checks = policy["_source_authority"], {"n": 0}

    def revoked_on_third_check():
        checks["n"] += 1
        return granted() if checks["n"] < 3 else {"complete": False}

    policy["_source_authority"] = revoked_on_third_check
    assert call(body) == {'version': 1, 'status': 'rejected', 'code': 'permission_unavailable'}
    policy["_source_authority"] = granted
    assert call(dict(body, operation='inspect')) == {'version': 1, 'status': 'rejected', 'code': 'permission_unavailable'}
    assert call(body) == {'version': 1, 'status': 'rejected', 'code': 'permission_unavailable'}
    assert not calendar.patches
