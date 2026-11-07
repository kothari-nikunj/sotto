"""Original creation helper and CLI, with only a synthetic Calendar resource."""

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import runtime_owner_calendar_tool as tool
from test_runtime_owner_calendar_tool import context, flow, request

__all__ = ["context", "flow"]


@pytest.fixture
def creation(flow, monkeypatch):
    call, _, calendar, grants, policy, data, _ = flow
    google = tool._google()
    monkeypatch.setattr(google, "_settings_email", lambda: "owner@example.com")
    attempts = []
    mode = {"lost": False, "mismatch": False, "no_link": False}

    def insert(**kwargs):
        assert kwargs["calendarId"] == "primary" and kwargs["sendUpdates"] == "all"

        def execute():
            attempts.append(copy.deepcopy(kwargs["body"]))
            calendar.event = {
                **copy.deepcopy(kwargs["body"]),
                "htmlLink": "https://calendar.google.com/event",
            }
            if mode["no_link"]:
                calendar.event.pop("htmlLink")
            if mode["lost"]:
                raise TimeoutError("synthetic loss after accepted insertion")
            result = copy.deepcopy(calendar.event)
            if mode["mismatch"]:
                result["summary"] = "different"
            if mode.get("malformed"):
                for target in (result, calendar.event):
                    if mode["malformed"] == "attendee":
                        target.setdefault("attendees", []).append(None)
                    elif mode["malformed"] == "recurrence":
                        target["recurrence"] = ["RRULE:FREQ=WEEKLY"]
                    else:
                        target["start"]["date"] = "2026-10-10"
            return result

        return SimpleNamespace(execute=execute)

    monkeypatch.setattr(calendar, "insert", insert, raising=False)
    monkeypatch.setattr(
        calendar, "get", lambda **kwargs: SimpleNamespace(execute=lambda: copy.deepcopy(calendar.event))
    )
    path = Path(__file__).parents[2] / "adapters/sotto/google_api.py"
    spec = importlib.util.spec_from_file_location("create_actual_cli", path)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)

    # In-process runner for unit isolation: original CLI parser/main and resource are real.
    # Native/package gate separately exercises the unchanged subprocess/HTTP boundary.
    def run(argv):
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                api.main(argv, service_factory=lambda *a: calendar)
            return json.loads(output.getvalue())
        except Exception:
            return {"status": "error", "error": "synthetic transport uncertainty"}

    monkeypatch.setattr(google, "_run", run)

    def prepare(guests=None, **updates):
        args = {
            "operation": "propose_create",
            "summary": "Cedar review",
            "start": "2026-10-10T10:00:00-07:00",
            "end": "2026-10-10T11:00:00-07:00",
            "attendees": ["guest@example.com"] if guests is None else guests,
            "location": "12 Main Street",
            "description": "Agenda line one\nAgenda line two",
            **updates,
        }
        proposal = request(args, operation="preview", text="Create Cedar review")
        preview = call(proposal)
        assert preview["status"] == "preview"
        review = preview["review"]
        approval = {
            "event_id": proposal["event_id"],
            "action_id": proposal["action_id"],
            "args_hash": hashlib.sha256(tool.canonical(args).encode()).hexdigest(),
            "accepted_dispatch_id": "accepted-create-review",
            "source_revision": review["source_revision"],
        }
        return request({"operation": "create", "review": review, "proposal": approval}, event="d" * 64)

    return call, prepare, calendar, attempts, mode, policy, data, google


@pytest.mark.parametrize("guests", [[], ["guest@example.com"], ["owner@example.com"]])
def test_original_create_full_fields_and_immutable_replay(creation, guests):
    call, prepare, calendar, attempts, _, policy, _, _ = creation
    body = prepare(guests)
    review = body["args"]["review"]
    assert not attempts
    assert (
        review["event_id"]
        == hashlib.sha256(
            ("sotto.calendar-create-event.v1\0" + body["args"]["proposal"]["action_id"]).encode()
        ).hexdigest()
    )
    assert "Agenda line one\nAgenda line two" in tool.render(review)
    result = call(body)
    assert result["status"] == "committed" and result["outcome"]["status"] == "created"
    assert len(attempts) == 1 and attempts[0]["id"] == review["event_id"]
    assert sorted(a["email"] for a in attempts[0].get("attendees", [])) == review["effective_attendees"]
    assert (
        calendar.event["description"] == review["description"]
        and calendar.event["location"] == review["location"]
    )
    body["operation"] = "inspect"
    policy["_source_authority"] = lambda: {"complete": False}
    assert call(body) == result and len(attempts) == 1


@pytest.mark.parametrize("failure", ["lost", "mismatch", "no_link"])
def test_uncertain_result_never_recreates(creation, failure):
    call, prepare, _, attempts, mode, _, _, _ = creation
    body = prepare()
    mode[failure] = True
    assert call(body) == {"version": 1, "status": "unknown"}
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"} and len(attempts) == 1


def test_post_acceptance_revocation_keeps_created_receipt(creation, monkeypatch):
    call, prepare, calendar, attempts, _, policy, _, _ = creation
    body = prepare()
    insert = calendar.insert

    def revoking_insert(**kwargs):
        request = insert(**kwargs)
        execute = request.execute

        def accepted():
            result = execute()
            policy["_source_authority"] = lambda: {"complete": False}
            return result

        return SimpleNamespace(execute=accepted)

    monkeypatch.setattr(calendar, "insert", revoking_insert)
    monkeypatch.setattr(calendar, "get", lambda **kwargs: pytest.fail("no private read-back after revocation"))
    result = call(body)
    assert result["status"] == "committed" and result["outcome"]["status"] == "created"
    assert result["outcome"]["html_link"] == "https://calendar.google.com/event"
    assert call(dict(body, operation="inspect")) == result and len(attempts) == 1


def test_organizer_changed_before_contact_refuses(creation, monkeypatch):
    call, prepare, _, attempts, _, _, _, google = creation
    body = prepare()
    monkeypatch.setattr(google, "_settings_email", lambda: "changed@example.com")
    assert call(body)["code"] == "target_changed" and not attempts


def test_revoked_permission_has_no_insert(creation):
    call, prepare, _, attempts, _, policy, _, _ = creation
    body = prepare()
    policy["_source_authority"] = lambda: {"complete": False}
    assert call(body)["code"] == "authority_changed" and not attempts


@pytest.mark.parametrize(
    "updates",
    [
        {"description": "bad\rtext"},
        {"location": "bad\nplace"},
        {"summary": "bad\u0085name"},
        {"attendees": ["Guest@example.com"]},
        {"description": "😀" * 2100},
    ],
)
def test_invalid_or_unrenderable_creation_refuses(creation, updates):
    _, prepare, _, attempts, _, _, _, _ = creation
    with pytest.raises(ValueError):
        prepare(**updates)
    assert not attempts


def test_unknown_organizer_is_truthful(creation, monkeypatch):
    call, prepare, _, attempts, _, _, _, google = creation
    monkeypatch.setattr(google, "_settings_email", lambda: "")
    body = prepare()
    assert "Your address is not known" in tool.render(body["args"]["review"])
    result = call(body)
    assert result["outcome"]["organizer_listed"] is False
    assert attempts[0]["attendees"] == [{"email": "guest@example.com"}]


def test_effective_guest_cap_refused_before_marker(creation):
    _, prepare, _, attempts, _, _, _, _ = creation
    with pytest.raises(AssertionError):
        prepare(sorted(f"g{i:02d}@example.com" for i in range(50)))
    assert not attempts


def test_reviewed_create_never_bare_fallback(creation, monkeypatch):
    call, prepare, _, attempts, _, _, _, google = creation
    body = prepare()
    calls = []

    def unsupported(argv):
        calls.append(argv)
        return {"status": "error", "error": "unrecognized arguments: --location"}

    monkeypatch.setattr(google, "_run", unsupported)
    assert call(body) == {"version": 1, "status": "unknown"}
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"}
    assert len(calls) == 1 and "--event-id" in calls[0] and not attempts


def test_second_organizer_check_never_contacts_after_rebase(creation, monkeypatch):
    call, prepare, _, attempts, _, _, _, google = creation
    body = prepare()
    values = iter(["owner@example.com", "owner@example.com", "changed@example.com"])
    monkeypatch.setattr(google, "_settings_email", lambda: next(values))
    assert call(body) == {"version": 1, "status": "unknown"}
    assert not attempts
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"}


def test_receipt_failure_after_create_holds_without_reinsert(creation, monkeypatch):
    call, prepare, _, attempts, _, _, _, _ = creation
    body = prepare()
    prefs = tool._preferences()
    original = prefs._durable_preferences_replace
    count = 0

    def durable(path, data):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("synthetic receipt failure before write")
        return original(path, data)

    monkeypatch.setattr(prefs, "_durable_preferences_replace", durable)
    assert call(body) == {"version": 1, "status": "unknown"}
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"} and len(attempts) == 1


def test_stable_id_implicit_retry_cannot_accept_second_event(creation, monkeypatch):
    call, prepare, calendar, attempts, _, _, _, _ = creation
    body = prepare()
    original = calendar.insert
    wire_ids = []

    def insert(**kwargs):
        request = original(**kwargs)

        def execute():
            request.execute()
            wire_ids.extend([kwargs["body"]["id"], kwargs["body"]["id"]])
            # Model lower-transport replay: fixed ID now exists, second insert gets409.
            raise RuntimeError("synthetic conflict after response loss")

        return SimpleNamespace(execute=execute)

    monkeypatch.setattr(calendar, "insert", insert)
    assert call(body) == {"version": 1, "status": "unknown"}
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"}
    assert len(attempts) == 1 and wire_ids == [body["args"]["review"]["event_id"]] * 2


@pytest.mark.parametrize("malformed", ["attendee", "recurrence", "mixed_date"])
def test_malformed_provider_event_never_commits_or_reinserts(creation, malformed):
    call, prepare, _, attempts, mode, _, _, _ = creation
    body = prepare()
    mode["malformed"] = malformed
    assert call(body) == {"version": 1, "status": "unknown"}
    body["operation"] = "inspect"
    assert call(body) == {"version": 1, "status": "unknown"} and len(attempts) == 1
