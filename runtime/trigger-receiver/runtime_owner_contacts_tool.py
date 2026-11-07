"""Fully reviewed Contacts changes through the original Google People writer."""

import hashlib
import json
import re
from types import SimpleNamespace

from fresh_routines import YES
import owner_google_effect
from owner_google_effect import HEX, RefusalError, google as _google, readback_allowed as _readback_allowed
from runtime_owner_calendar_tool import _text
from installed_writers import preferences_for
from owner_tool_admission import validate_tool_admission, UnavailableError
from contact_snapshot import CONTACT_MASK, RESOURCE, canonical, snapshot, validate_fields, validate_snapshot
_preferences = preferences_for(__file__)

KIND = "owner_contacts_tool"
DOMAIN = "sotto.owner-contacts-tool.v1"


def _authority(policy, google, revision=None):
    return owner_google_effect.authority(policy, google, revision, source="google_contacts",
        grants=("contacts_read", "contacts_write"), refusal="contacts_permission_unavailable")


def _args(args):
    if type(args) is not dict or args.get("operation") not in (
        "propose_create",
        "propose_update",
        "propose_delete",
    ):
        raise ValueError("Contacts proposal invalid")
    op = args["operation"][8:]
    expected = (
        {"operation"}
        | ({"resource_name"} if op != "create" else set())
        | ({"fields"} if op != "delete" else set())
    )
    if set(args) != expected:
        raise ValueError("Contacts proposal invalid")
    if op != "create" and (
        type(args["resource_name"]) is not str
        or len(args["resource_name"]) > 256
        or not RESOURCE.fullmatch(args["resource_name"])
    ):
        raise ValueError("Contacts resource invalid")
    if op != "delete":
        validate_fields(args["fields"])
    return op


def _get(people, resource):
    person = people.get(
        resourceName=resource, personFields=CONTACT_MASK, sources=["READ_SOURCE_TYPE_CONTACT"]
    ).execute()
    value = snapshot(person)
    if value["resource_name"] != resource:
        raise RefusalError("invalid_target")
    return value


def _review(args, revision, before=None):
    op = _args(args)
    review = {
        "operation": op,
        "resource_name": None if op == "create" else args["resource_name"],
        "before": before,
        "fields": {} if op == "delete" else args["fields"],
        "source_revision": revision,
    }
    review["review_hash"] = hashlib.sha256(canonical(review).encode()).hexdigest()
    _validate_review(review)
    if len(render(review).encode("utf-16-le")) // 2 > 16000:
        raise RefusalError("review_unavailable")
    return review


FIELD_LABELS = (
    ("names", "Name"),
    ("emailAddresses", "Email addresses"),
    ("phoneNumbers", "Phone numbers"),
    ("organizations", "Organizations"),
    ("birthdays", "Birthdays"),
    ("addresses", "Addresses"),
    ("biographies", "Notes"),
)
KEY_LABELS = {
    "givenName": "First name",
    "familyName": "Last name",
    "displayName": "Full name",
    "value": "Value",
    "type": "Label",
    "formattedValue": "Full address",
    "metadata": "Additional details",
    "etag": "Version",
}


def _label(key):
    if key in KEY_LABELS:
        return KEY_LABELS[key]
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key).replace("_", " ")
    return value[:1].upper() + value[1:]


def _scalar(value):
    if value is None:
        return "(not set)"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if not value:
        return "(empty)"
    escapes = {"\n": r"\n", "\r": r"\r", "\t": r"\t", "\b": r"\b", "\f": r"\f", "\\": r"\\"}
    return "".join(
        escapes.get(
            c, "\\u%04x" % ord(c) if ord(c) < 32 or 127 <= ord(c) <= 159 or c in "\u2028\u2029" else c
        )
        for c in value
    )


def _detail(value, indent):
    lines = []
    entries = (
        sorted(value.items())
        if type(value) is dict
        else [(f"Entry {i + 1}", item) for i, item in enumerate(value)]
    )
    for key, item in entries:
        label = _scalar(_label(key)) if type(value) is dict else key
        prefix = " " * indent + label + ":"
        if type(item) in (dict, list):
            if not item:
                lines.append(prefix + (" No details" if type(item) is dict else " None"))
            else:
                lines.append(prefix)
                lines.extend(_detail(item, indent + 2))
        else:
            lines.append(prefix + " " + _scalar(item))
    return lines


def _field_lines(fields):
    lines = []
    for key, label in FIELD_LABELS:
        if key not in fields:
            continue
        if fields[key]:
            lines.append(label + ":")
            lines.extend(_detail(fields[key], 2))
        else:
            lines.append(label + ": None")
    return lines


def render(review):
    op = review["operation"]
    lines = [{"create": "Add", "update": "Update", "delete": "Delete"}[op] + " Google contact"]
    if review["resource_name"]:
        lines.append("Contact ID: " + review["resource_name"])
    if review["before"] is not None:
        lines.append("Current details:")
        lines.extend(_field_lines(review["before"]["fields"]))
    if op != "delete":
        lines.append("New details:" if op == "create" else "Requested changes:")
        lines.extend(_field_lines(review["fields"]))
    if op == "update":
        lines.append(
            "The listed details will replace the current details in those categories. Other details stay unchanged."
        )
    lines.extend(
        ["", "Should I " + {"create": "add", "update": "update", "delete": "delete"}[op] + " this contact?"]
    )
    return "\n".join(lines)


def _validate_review(review):
    if (
        type(review) is not dict
        or set(review) != {"operation", "resource_name", "before", "fields", "source_revision", "review_hash"}
        or review["operation"] not in ("create", "update", "delete")
        or any(
            type(review[k]) is not str or not HEX.fullmatch(review[k])
            for k in ("source_revision", "review_hash")
        )
    ):
        raise ValueError("Contacts review invalid")
    op = review["operation"]
    if op == "create":
        if review["resource_name"] is not None or review["before"] is not None:
            raise ValueError("Contacts create target invalid")
    else:
        validate_snapshot(review["before"])
        if review["resource_name"] != review["before"]["resource_name"]:
            raise ValueError("Contacts review target invalid")
    validate_fields(review["fields"], empty=op == "delete")
    if op == "delete" and review["fields"] != {}:
        raise ValueError("Contacts delete fields invalid")
    if (
        review["review_hash"]
        != hashlib.sha256(
            canonical({k: v for k, v in review.items() if k != "review_hash"}).encode()
        ).hexdigest()
        or len(canonical(review).encode()) > 32768
    ):
        raise ValueError("Contacts review hash or bound invalid")


def _receipt(proof, review, resource):
    return {
        "version": 1,
        "status": "committed",
        **{k: proof[k] for k in ("action_id", "event_id", "payload_hash", "args_hash")},
        "outcome": {
            "operation": review["operation"],
            "status": {"create": "created", "update": "updated", "delete": "deleted"}[review["operation"]],
            "resource_name": resource,
            "review_hash": review["review_hash"],
        },
    }


def perform(
    body, *, bearer, root, policy, data_root, capability, activation, telegram_verifier=None, chat_client=None
):
    proof = validate_tool_admission(
        body,
        operations=("preview", "apply", "inspect"),
        kind=KIND,
        domain=DOMAIN,
        bearer=bearer,
        root=root,
        capability=capability,
        policy=policy,
        data_root=data_root,
        activation=activation,
        telegram_verifier=telegram_verifier,
        chat_client=chat_client,
    )
    args = body["args"]
    google = _google()
    try:
        if body["operation"] == "preview":
            op = _args(args)
            revision = _authority(policy, google)
            before = (
                None
                if op == "create"
                else _get(google._google_service("people", "v1").people(), args["resource_name"])
            )
            review = _review(args, revision, before)
            _authority(policy, google, revision)
            return {
                "version": 1,
                "status": "preview",
                "event_id": proof["event_id"],
                "payload_hash": proof["payload_hash"],
                "review": review,
            }
        if type(args) is not dict or set(args) != {"operation", "review", "proposal"}:
            raise ValueError("Contacts action invalid")
        review, proposal = args["review"], args["proposal"]
        _validate_review(review)
        if args["operation"] != review["operation"]:
            raise ValueError("Contacts operation mismatch")
        if (
            type(proposal) is not dict
            or set(proposal)
            != {"event_id", "action_id", "args_hash", "accepted_dispatch_id", "source_revision"}
            or any(
                type(proposal[k]) is not str or not HEX.fullmatch(proposal[k])
                for k in ("event_id", "action_id", "args_hash", "source_revision")
            )
        ):
            raise ValueError("Contacts approval invalid")
        _text(proposal["accepted_dispatch_id"], 160)
        original = {"operation": "propose_" + review["operation"]}
        if review["operation"] != "create":
            original["resource_name"] = review["resource_name"]
        if review["operation"] != "delete":
            original["fields"] = review["fields"]
        encoded = canonical(original)
        action = hashlib.sha256(
            json.dumps(
                [proposal["event_id"], KIND, encoded], ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if (
            proposal["args_hash"] != hashlib.sha256(encoded.encode()).hexdigest()
            or proposal["action_id"] != action
            or proposal["source_revision"] != review["source_revision"]
        ):
            raise ValueError("Contacts proposal proof invalid")
        prefs = _preferences()
        path = prefs._path()
        tag = "contacts_" + review["operation"]

        def load():
            data = prefs.jsonstore.read(path, default={}, strict=True)
            records = data.setdefault("owner_preference_receipts", {})
            if type(records) is not dict:
                raise UnavailableError("Contacts receipts unavailable")
            return data, records

        def saved_response(saved):
            if type(saved) is not dict or saved.get("tag") != tag or saved.get("proof") != proof:
                raise RefusalError("event_conflict")
            if saved.get("review") != review or saved.get("proposal") != proposal:
                raise UnavailableError("Contacts receipt unavailable")
            if saved.get("status") == "rejected" and set(saved) == {
                "tag", "proof", "review", "proposal", "status"
            }:
                return {"version": 1, "status": "rejected", "code": "permission_unavailable"}
            if saved.get("status") == "contacting" and set(saved) == {
                "tag",
                "proof",
                "review",
                "proposal",
                "status",
            }:
                return {"version": 1, "status": "unknown"}
            receipt = saved.get("receipt")
            resource = receipt.get("outcome", {}).get("resource_name") if type(receipt) is dict else None
            if (
                saved.get("status") != "committed"
                or set(saved) != {"tag", "proof", "review", "proposal", "status", "receipt"}
                or type(resource) is not str
                or not RESOURCE.fullmatch(resource)
                or review["operation"] != "create"
                and resource != review["resource_name"]
                or receipt != _receipt(proof, review, resource)
            ):
                raise UnavailableError("Contacts receipt unavailable")
            return receipt

        def reserved(records):
            return any(
                type(v) is dict
                and v.get("tag") in ("contacts_create", "contacts_update", "contacts_delete")
                and type(v.get("proposal")) is dict
                and v["proposal"].get("action_id") == proposal["action_id"]
                for v in records.values()
            )

        with prefs.jsonstore.lock(path):
            _, records = load()
            if proof["event_id"] in records:
                result = saved_response(records[proof["event_id"]])
                prefs._sync_existing_preferences(path)
                return result
            if body["operation"] == "inspect":
                return {"version": 1, "status": "not_found"}
            if reserved(records):
                raise RefusalError("event_conflict")
        if not YES.fullmatch(body["text"].strip()) or proposal["event_id"] == proof["event_id"]:
            raise RefusalError("review_unavailable")
        _authority(policy, google, review["source_revision"])
        people = google._google_service("people", "v1").people()
        before = None if review["operation"] == "create" else _get(people, review["resource_name"])
        if _review(original, review["source_revision"], before) != review:
            raise RefusalError("target_changed")
        _authority(policy, google, review["source_revision"])
        with prefs.jsonstore.lock(path):
            data, records = load()
            if proof["event_id"] in records:
                result = saved_response(records[proof["event_id"]])
                prefs._sync_existing_preferences(path)
                return result
            if reserved(records):
                raise RefusalError("event_conflict")
            saved = {
                "tag": tag,
                "proof": proof,
                "review": review,
                "proposal": proposal,
                "status": "contacting",
            }
            records[proof["event_id"]] = saved
            prefs._durable_preferences_replace(path, data)
        def reject_gated():
            # _gated returns refused only before calling the provider. Persist this
            # terminal no-effect result so inspect cannot mistake it for contact loss.
            with prefs.jsonstore.lock(path):
                data, records = load()
                if records.get(proof["event_id"]) != saved:
                    return {"version": 1, "status": "unknown"}
                records[proof["event_id"]] = {**saved, "status": "rejected"}
                prefs._durable_preferences_replace(path, data)
            return {"version": 1, "status": "rejected", "code": "permission_unavailable"}

        try:
            try:
                _authority(policy, google, review["source_revision"])
            except RefusalError:
                # Consent changed after the reservation but before any provider contact:
                # a terminal no-effect result, never an unknown the owner must chase.
                return reject_gated()
            a = SimpleNamespace(
                cmd="contacts-" + review["operation"],
                resource_name=review["resource_name"] or "",
                fields_json=canonical(review["fields"]),
            )
            result, refused = google._gated(
                a.cmd,
                {} if review["operation"] == "create" else {"resource_name": a.resource_name},
                google._action_payload(a),
                lambda: google._native_action(a, reviewed_contact=review["before"]),
                False,
            )
            expected = {"create": "created", "update": "updated", "delete": "deleted"}[review["operation"]]
            resource = result.get("resource_name") if type(result) is dict else None
            if refused:
                return reject_gated()
            if (
                type(result) is not dict
                or result.get("status") != expected
                or type(resource) is not str
                or not RESOURCE.fullmatch(resource)
            ):
                return {"version": 1, "status": "unknown"}
            if review["operation"] != "create" and resource != review["resource_name"]:
                return {"version": 1, "status": "unknown"}
            if review["operation"] != "delete" and _readback_allowed(
                lambda: _authority(policy, google, review["source_revision"])
            ):
                # Provider normalization is allowed; success comes from the original
                # writer result, and this fresh read verifies the same contact still exists.
                # A later authority change skips only this private read, never the accepted write.
                _get(people, resource)
            receipt = _receipt(proof, review, resource)
            with prefs.jsonstore.lock(path):
                data, records = load()
                if records.get(proof["event_id"]) != saved:
                    return {"version": 1, "status": "unknown"}
                records[proof["event_id"]] = {**saved, "status": "committed", "receipt": receipt}
                prefs._durable_preferences_replace(path, data)
            return receipt
        except Exception:
            return {"version": 1, "status": "unknown"}
    except RefusalError as error:
        return {"version": 1, "status": "rejected", "code": error.code}
