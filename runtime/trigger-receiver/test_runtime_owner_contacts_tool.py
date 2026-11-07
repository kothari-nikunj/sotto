"""Original People writer with exact reviewed arrays and durable held receipts."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
import runtime_owner_contacts_tool as tool
from test_runtime_owner_loop_close import context, IDENTITY, ROOT, WHEN, THREAD, capability

__all__ = ["context"]


class People:
    def __init__(self):
        self.person = {
            "resourceName": "people/c1",
            "etag": "v1",
            "metadata": {"sources": [{"type": "CONTACT", "id": "1", "etag": "s1"}]},
            "names": [{"givenName": "Dana", "displayName": "Dana"}],
            "emailAddresses": [{"value": "dana@example.com", "type": "work"}],
            "phoneNumbers": [
                {"value": "+15555550123", "type": "mobile"},
                {"value": "+15555550124", "type": "home"},
            ],
        }
        self.calls = []
        self.gets = 0
        self.lost = False
        self.bad = False
        self.change_at = None

    def people(self):
        return self

    def get(self, **kw):
        assert kw == {
            "resourceName": "people/c1",
            "personFields": tool.CONTACT_MASK,
            "sources": ["READ_SOURCE_TYPE_CONTACT"],
        }

        def execute():
            self.gets += 1
            if self.change_at == self.gets:
                self.person["etag"] = "changed"
            return copy.deepcopy(self.person)

        return SimpleNamespace(execute=execute)

    def _mutate(self, kind, kw):
        def execute():
            self.calls.append((kind, copy.deepcopy(kw)))
            if kind == "delete":
                self.person = None
            else:
                if kind == "create":
                    self.person = {
                        "resourceName": "people/c1",
                        "metadata": {"sources": [{"type": "CONTACT", "id": "1", "etag": "s2"}]},
                    }
                self.person.update(
                    {k: copy.deepcopy(v) for k, v in kw["body"].items() if k not in ("etag", "metadata")}
                )
                self.person["etag"] = "v2"
                # Real People may add derived read-only fields after accepting submitted arrays.
                if self.person.get("names"):
                    self.person["names"][0]["displayName"] = "Provider normalized name"
            if self.lost:
                raise TimeoutError("synthetic accepted response lost")
            if self.bad:
                return {}
            return copy.deepcopy(self.person) if self.person else None

        return SimpleNamespace(execute=execute)

    def createContact(self, **kw):
        assert set(kw) == {"body", "personFields"} and kw["personFields"] == tool.CONTACT_MASK
        return self._mutate("create", kw)

    def updateContact(self, **kw):
        assert kw["resourceName"] == "people/c1" and kw["personFields"] == tool.CONTACT_MASK
        assert kw["body"]["etag"] == self.person["etag"]
        assert kw["body"]["metadata"]["sources"] == self.person["metadata"]["sources"]
        return self._mutate("update", kw)

    def deleteContact(self, **kw):
        assert kw == {"resourceName": "people/c1"}
        return self._mutate("delete", kw)


def request(args, operation="apply", event="a" * 64, text="yes"):
    encoded = tool.canonical(args)
    return {
        "version": 1,
        "operation": operation,
        "identity": IDENTITY,
        "action_id": hashlib.sha256(
            json.dumps([event, tool.KIND, encoded], ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
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
        _source_authority=lambda: {
            "complete": True,
            "revision": "c" * 64,
            "google_sources": ["google_contacts"],
        },
    )
    monkeypatch.delenv("SOTTO_UNATTENDED", raising=False)
    prefs = tool._preferences()
    monkeypatch.setattr(tool, "_preferences", lambda: prefs)
    google = tool._google()
    people = People()
    grants = {"contacts_read": True, "contacts_write": True}
    monkeypatch.setattr(google, "capabilities", lambda: grants)
    monkeypatch.setattr(google, "_google_service", lambda *args: people)
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

    def prepare(op="update", fields=None):
        args = {"operation": "propose_" + op}
        if op != "create":
            args["resource_name"] = "people/c1"
        if op != "delete":
            args["fields"] = (
                fields
                if fields is not None
                else {
                    "phoneNumbers": [
                        {"value": "+15555550125", "type": "mobile"},
                        {"value": "+15555550124", "type": "home"},
                    ]
                }
            )
        proposed = request(args, "preview", "d" * 64, "Please change my Google contact.")
        preview = call(proposed)
        if preview["status"] != "preview":
            return preview, None
        approval = {
            "event_id": proposed["event_id"],
            "action_id": proposed["action_id"],
            "args_hash": hashlib.sha256(tool.canonical(args).encode()).hexdigest(),
            "accepted_dispatch_id": "accepted-full-contact-review",
            "source_revision": "c" * 64,
        }
        return preview, request({"operation": op, "review": preview["review"], "proposal": approval})

    return SimpleNamespace(
        data=data,
        policy=policy,
        prefs=prefs,
        google=google,
        people=people,
        grants=grants,
        call=call,
        prepare=prepare,
    )


@pytest.mark.parametrize("op", ["create", "update", "delete"])
def test_actual_original_crud_review_then_confirm_receipt_and_reopen(flow, op):
    before = copy.deepcopy(flow.people.person)
    preview, body = flow.prepare(op, {"names": [{"givenName": "Maya"}]} if op == "create" else None)
    assert not flow.people.calls
    result = flow.call(body)
    assert (
        result["status"] == "committed"
        and result["outcome"]["status"] == {"create": "created", "update": "updated", "delete": "deleted"}[op]
    )
    assert len(flow.people.calls) == 1
    kind, kw = flow.people.calls[0]
    assert kind == op
    if op == "update":
        assert kw["updatePersonFields"] == "phoneNumbers"
        assert kw["body"]["phoneNumbers"] == preview["review"]["fields"]["phoneNumbers"]
        assert flow.people.person["emailAddresses"] == before["emailAddresses"]
    if op == "create":
        assert kw["body"] == {"names": [{"givenName": "Maya"}]}
        assert flow.people.person["names"][0]["displayName"] == "Provider normalized name"
    flow.policy["_source_authority"] = lambda: {"complete": False}
    flow.people.person = None
    assert flow.call(dict(body, operation="inspect")) == result
    assert flow.call(body) == result and len(flow.people.calls) == 1
    audit = [json.loads(line) for line in (flow.data / "events/sends.jsonl").read_text().splitlines()]
    assert audit[-1]["verb"] == "contacts-" + op


@pytest.mark.parametrize("op", ["create", "update", "delete"])
def test_actual_contact_loss_is_unknown_and_inspect_never_reinvokes(flow, op):
    _, body = flow.prepare(op)
    flow.people.lost = True
    assert flow.call(body) == {"version": 1, "status": "unknown"}
    assert len(flow.people.calls) == 1
    flow.policy["_source_authority"] = lambda: {"complete": False}
    assert flow.call(dict(body, operation="inspect")) == {"version": 1, "status": "unknown"}
    assert flow.call(body) == {"version": 1, "status": "unknown"}
    assert len(flow.people.calls) == 1


def test_stale_review_and_original_writer_second_get_guard(flow):
    _, body = flow.prepare()
    flow.people.person["etag"] = "new"
    assert flow.call(body)["code"] == "target_changed"
    assert not flow.people.calls and not flow.prefs.jsonstore.read(flow.prefs._path(), {}).get(
        "owner_preference_receipts"
    )
    # Own original fresh GET after adapter revalidation detects a late change.
    _, body = flow.prepare()
    flow.people.change_at = flow.people.gets + 2
    assert flow.call(body) == {"version": 1, "status": "unknown"}
    assert not flow.people.calls


def test_revocation_distinct_yes_and_consumption_refuse(flow):
    _, body = flow.prepare()
    assert flow.call(request(body["args"], text="not yet"))["code"] == "review_unavailable"
    same = request(body["args"], event="d" * 64)
    assert flow.call(same)["code"] == "review_unavailable"
    flow.grants["contacts_write"] = False
    assert flow.call(body)["code"] == "contacts_permission_unavailable" and not flow.people.calls
    flow.grants["contacts_write"] = True
    assert flow.call(body)["status"] == "committed"
    assert flow.call(request(body["args"], event="e" * 64))["code"] == "event_conflict"
    assert len(flow.people.calls) == 1


def test_forged_review_current_fields_with_valid_hash_refused(flow):
    _, body = flow.prepare()
    body = copy.deepcopy(body)
    body["args"]["review"]["before"]["fields"]["names"] = [{"givenName": "Other"}]
    review = body["args"]["review"]
    review["review_hash"] = hashlib.sha256(
        tool.canonical({k: v for k, v in review.items() if k != "review_hash"}).encode()
    ).hexdigest()
    body = request(body["args"])
    assert flow.call(body)["code"] == "target_changed" and not flow.people.calls


def test_post_send_revoke_skips_private_readback_but_keeps_commit(flow):
    _, body = flow.prepare()
    original = flow.people._mutate

    def mutate(kind, kw):
        request = original(kind, kw)

        def execute():
            result = request.execute()
            flow.policy["_source_authority"] = lambda: {"complete": False}
            return result

        return SimpleNamespace(execute=execute)

    flow.people._mutate = mutate
    # People accepted the reviewed write; revocation forbids only the later private read.
    result = flow.call(body)
    assert result["status"] == "committed" and result["outcome"]["resource_name"] == "people/c1"
    assert flow.people.gets == 3 and len(flow.people.calls) == 1
    assert flow.call(dict(body, operation="inspect")) == result
    assert flow.call(body) == result and len(flow.people.calls) == 1


def test_post_send_revision_change_keeps_commit(flow):
    _, body = flow.prepare()
    original = flow.people._mutate

    def mutate(kind, kw):
        request = original(kind, kw)

        def execute():
            result = request.execute()
            flow.policy["_source_authority"] = lambda: {
                "complete": True,
                "revision": "e" * 64,
                "google_sources": ["google_contacts"],
            }
            return result

        return SimpleNamespace(execute=execute)

    flow.people._mutate = mutate
    result = flow.call(body)
    assert result["status"] == "committed" and len(flow.people.calls) == 1
    assert flow.call(dict(body, operation="inspect")) == result


def test_receipt_persist_fault_keeps_unknown_without_second_write(flow, monkeypatch):
    _, body = flow.prepare()
    original = flow.prefs._durable_preferences_replace

    def replace(path, data):
        if any(v.get("status") == "committed" for v in data.get("owner_preference_receipts", {}).values()):
            raise OSError("synthetic commit failure")
        original(path, data)

    monkeypatch.setattr(flow.prefs, "_durable_preferences_replace", replace)
    assert flow.call(body) == {"version": 1, "status": "unknown"}
    assert flow.call(dict(body, operation="inspect")) == {"version": 1, "status": "unknown"}
    assert len(flow.people.calls) == 1


def test_full_render_bound_and_fields_no_truncation(flow):
    fields = {"biographies": [{"value": "x" * 12200}]}
    preview, body = flow.prepare("create", fields)
    assert preview["review"]["fields"] == fields
    assert len(tool.render(preview["review"])) < 16000 and not flow.people.calls
    with pytest.raises(ValueError):
        flow.prepare("create", {"biographies": [{"value": "x" * 12300}]})
    flow.people.person["biographies"] = [{"value": "x" * 10000}]
    preview, body = flow.prepare("update", {"biographies": [{"value": "y" * 10000}]})
    assert preview["code"] == "review_unavailable" and body is None


@pytest.mark.parametrize("value", [1.0, 1.5, float("nan"), 9007199254740992, -9007199254740992])
def test_contact_json_refuses_noncanonical_numeric_values(value):
    with pytest.raises(ValueError):
        tool.validate_fields({"birthdays": [{"date": {"year": value}}]})


def test_contact_nested_unicode_hash_and_safe_integer_vector():
    value = {
        "birthdays": [
            {"date": {"year": 2026, "month": 10, "day": 6}, "\U00010000": "astral", "\ue000": "bmp"}
        ]
    }
    assert (
        tool.canonical(value)
        == '{"birthdays":[{"date":{"day":6,"month":10,"year":2026},"\ue000":"bmp","\U00010000":"astral"}]}'
    )
    assert tool.validate_fields(value) == value


@pytest.mark.parametrize(
    "change",
    [
        {"metadata": None},
        {"resourceName": "people/c"},
        {"resourceName": "people/c" + "x" * 250},
        {"metadata": {"sources": []}},
    ],
)
def test_malformed_snapshot_refused(flow, change):
    from contact_snapshot import snapshot

    with pytest.raises(ValueError):
        snapshot({**flow.people.person, **change})


def test_unrelated_preference_receipt_preserved(flow):
    flow.prefs._durable_preferences_replace(
        flow.prefs._path(), {"owner_preference_receipts": {"old": {"tag": "old-fixture", "value": 7}}}
    )
    _, body = flow.prepare()
    assert flow.call(body)["status"] == "committed"
    assert flow.prefs.jsonstore.read(flow.prefs._path(), strict=True)["owner_preference_receipts"]["old"] == {
        "tag": "old-fixture",
        "value": 7,
    }


def test_plain_contact_review_preserves_nested_metadata_and_exact_text():
    review = {
        "operation": "create",
        "resource_name": None,
        "before": None,
        "fields": {
            "names": [{"givenName": "Dana", "familyName": "李"}],
            "emailAddresses": [{"value": "dana@example.com", "type": "work"}],
            "biographies": [{"value": "First line\nSecond line\\end", "metadata": {"primary": True}}],
        },
    }
    assert tool.render(review) == (
        "Add Google contact\nNew details:\nName:\n  Entry 1:\n    Last name: 李\n    First name: Dana\n"
        "Email addresses:\n  Entry 1:\n    Label: work\n    Value: dana@example.com\n"
        "Notes:\n  Entry 1:\n    Additional details:\n      Primary: true\n"
        "    Value: First line\\nSecond line\\\\end\n\nShould I add this contact?"
    )


def test_nested_contact_labels_and_values_cannot_inject_review_lines():
    review = {
        "operation": "create",
        "resource_name": None,
        "before": None,
        "fields": {"biographies": [{"unsafe\nkey\u2028": "one\u0085two\u2029three"}]},
    }
    text = tool.render(review)
    assert "    Unsafe\\nkey\\u2028: one\\u0085two\\u2029three" in text
    assert "\u0085" not in text and "\u2028" not in text and "\u2029" not in text


def test_precontact_google_gate_refusal_is_terminal(flow, monkeypatch):
    _, body = flow.prepare()
    monkeypatch.setattr(flow.google, '_gated', lambda *args, **kwargs: ({'status': 'error'}, True))
    assert flow.call(body) == {'version': 1, 'status': 'rejected', 'code': 'permission_unavailable'}
    assert flow.call(dict(body, operation='inspect')) == {'version': 1, 'status': 'rejected', 'code': 'permission_unavailable'}
    assert not flow.people.calls


def test_authority_lost_after_reservation_is_terminal_without_contact(flow):
    _, body = flow.prepare()
    granted, checks = flow.policy["_source_authority"], {"n": 0}

    def revoked_on_third_check():
        checks["n"] += 1
        return granted() if checks["n"] < 3 else {"complete": False}

    flow.policy["_source_authority"] = revoked_on_third_check
    assert flow.call(body) == {'version': 1, 'status': 'rejected', 'code': 'permission_unavailable'}
    flow.policy["_source_authority"] = granted
    assert flow.call(dict(body, operation='inspect')) == {'version': 1, 'status': 'rejected', 'code': 'permission_unavailable'}
    assert not flow.people.calls
