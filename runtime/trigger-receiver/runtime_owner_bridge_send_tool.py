"""Reviewed Mac messages using existing owner admission, relay and durable Mac receipts.

The raw send MCP tool remains unavailable to model/skill calls. This private route
accepts only a reviewed, separately confirmed action from the owner coordinator.
"""

import hashlib
import json
import re
from pathlib import Path
import time

from fresh_routines import YES
from runtime_owner_gmail_draft_tool import SEND_YES
from owner_tool_admission import validate_tool_admission, _text, UnavailableError
from installed_writers import preferences_for
_preferences = preferences_for(__file__)

KIND = "owner_bridge_send_tool"
DOMAIN = "sotto.owner-bridge-send-tool.v1"
MAX_REVIEW_AGE_MS = 15 * 60 * 1000


HEX = re.compile(r"^[a-f0-9]{64}$")


class RefusalError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _message(args):
    from message_targets import normalized_target

    if (
        type(args) is not dict
        or set(args) != {"operation", "channel", "to", "body"}
        or args["operation"] != "propose_send"
        or args["channel"] not in ("auto", "imessage", "sms")
    ):
        raise ValueError("invalid message review")
    _text(args["to"], bound=254, whitespace=False)
    _text(args["body"], bound=4000)
    if not normalized_target("imessage" if args["channel"] == "auto" else args["channel"], args["to"]):
        raise RefusalError("invalid_target")


def _authority(policy, data_root, relay, revision=None):
    import managed

    current = policy["_source_authority"]()
    if (
        current.get("complete") is not True
        or not HEX.fullmatch(current.get("revision", ""))
        or revision is not None
        and revision != current["revision"]
    ):
        raise RefusalError("authority_changed")
    # Discovery is cached, not proof of the Mac's live switch. Relay repeats
    # consent at dispatch; the Mac repeats its local switch before Messages.
    if policy.get("SOTTO_DEPLOYMENT_MODE") == "managed" and not managed._tool_allowed(
        data_root, "send_message", None
    ):
        raise RefusalError("permission_unavailable")
    response = relay.mcp_call({"jsonrpc": "2.0", "id": "send-capability", "method": "tools/list"})
    if not any(
        t.get("name") == "send_message"
        for t in (response or {}).get("result", {}).get("tools", [])
        if type(t) is dict
    ):
        raise RefusalError("permission_unavailable")
    return current["revision"]


def _review(args, revision):
    value = {**args, "operation": "send", "source_revision": revision}
    return {**value, "review_hash": hashlib.sha256(canonical(value).encode()).hexdigest()}


def _result(response):
    result = (response or {}).get("result", {})
    if result.get("isError") is True:
        return {}
    if type(result.get("structuredContent")) is dict:
        return result["structuredContent"]
    try:
        return json.loads(result["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        return {}


def perform(
    body,
    *,
    bearer,
    root,
    policy,
    data_root,
    capability,
    activation,
    relay,
    telegram_verifier=None,
    chat_client=None,
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
    prefs = _preferences()
    # Use the existing durable JSON store/locks; keep message receipts separate
    # from preference pruning so old uncertain sends never become retryable.
    path = str(Path(data_root) / "events" / "bridge-send-receipts.json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    def load():
        value = prefs.jsonstore.read(path, default={}, strict=True)
        if type(value) is not dict:
            raise UnavailableError("message receipts unavailable")
        return value

    def save(value):
        # Expired reviews cannot authorize a send. Preserve every send/used claim,
        # including uncertain outcomes, so cleanup never makes replay possible.
        cutoff = int(time.time() * 1000) - MAX_REVIEW_AGE_MS
        for key, record in list(value.items()):
            if key.startswith("send:") and "proof" in record:
                record["proof_hash"] = hashlib.sha256(canonical(record.pop("proof")).encode()).hexdigest()
            if key.startswith("review:") and record.get("proof", {}).get("admitted_at_ms", 0) < cutoff:
                del value[key]
        prefs._durable_preferences_replace(path, value)

    args = body["args"]
    try:
        if body["operation"] == "preview":
            _message(args)
            revision = _authority(policy, data_root, relay)
            record = {"proof": proof, "review": _review(args, revision)}
            with prefs.jsonstore.lock(path):
                data = load()
                key = "review:" + proof["action_id"]
                if key in data and data[key] != record:
                    raise RefusalError("event_conflict")
                data[key] = record
                save(data)
            return {
                "version": 1,
                "status": "preview",
                "event_id": proof["event_id"],
                "payload_hash": proof["payload_hash"],
                "review": record["review"],
            }
        if (
            type(args) is not dict
            or set(args) != {"operation", "review", "proposal"}
            or args["operation"] != "send"
        ):
            raise ValueError("invalid message confirmation")
        review, proposal = args["review"], args["proposal"]
        if (
            type(proposal) is not dict
            or set(proposal)
            != {"event_id", "action_id", "args_hash", "accepted_dispatch_id", "source_revision"}
            or any(
                type(proposal[k]) is not str or not HEX.fullmatch(proposal[k])
                for k in ("event_id", "action_id", "args_hash", "source_revision")
            )
        ):
            raise ValueError("invalid proposal")
        _text(proposal["accepted_dispatch_id"], bound=160)
        original = {"operation": "propose_send", **{k: review[k] for k in ("channel", "to", "body")}}
        _message(original)
        if review != _review(original, proposal["source_revision"]):
            raise ValueError("invalid review")
        with prefs.jsonstore.lock(path):
            data = load()
            old = data.get("send:" + proof["action_id"])
            if old is not None:
                if old.get("proof_hash", hashlib.sha256(canonical(old.get("proof")).encode()).hexdigest()) != hashlib.sha256(canonical(proof).encode()).hexdigest():
                    raise RefusalError("event_conflict")
                return old.get("receipt", {"version": 1, "status": "unknown"})
            if body["operation"] == "inspect":
                return {"version": 1, "status": "not_found"}
            parent = data.get("review:" + proposal["action_id"], {})
            p = parent.get("proof", {})
            if (
                parent.get("review") != review
                or p.get("event_id") != proposal["event_id"]
                or p.get("args_hash") != proposal["args_hash"]
                or p.get("args") != original
                or p.get("identity_digest") != proof["identity_digest"]
                or p.get("provider_thread_id") != proof["provider_thread_id"]
                or proposal["event_id"] == proof["event_id"]
                or not (YES.fullmatch(body["text"].strip()) or SEND_YES.fullmatch(body["text"].strip()))
                or not 0 <= proof["admitted_at_ms"] - p.get("admitted_at_ms", 0) <= MAX_REVIEW_AGE_MS
                or not 0 <= int(time.time() * 1000) - proof["admitted_at_ms"] <= MAX_REVIEW_AGE_MS
            ):
                raise RefusalError("review_unavailable")
            if "used:" + proposal["action_id"] in data:
                raise RefusalError("event_conflict")
            _authority(policy, data_root, relay, review["source_revision"])
            data["used:" + proposal["action_id"]] = proof["action_id"]
            data["send:" + proof["action_id"]] = {"proof_hash": hashlib.sha256(canonical(proof).encode()).hexdigest()}
            save(data)  # durable claim BEFORE any send contact; never replay unknown
        response = relay.mcp_call(
            {
                "jsonrpc": "2.0",
                "id": proof["action_id"],
                "method": "tools/call",
                "params": {
                    "name": "send_message",
                    "arguments": {
                        "channel": review["channel"],
                        "to": review["to"],
                        "body": review["body"],
                        "request_id": proof["action_id"],
                    },
                },
            },
            timeout=90.0,
        )
        result = _result(response)
        raw = (response or {}).get("result", {})
        # Only exact, pre-dispatch failures prove this attempt did not send.
        # A timeout, conflict, or unrecognized reply stays held; never replay.
        refused = (
            (response or {}).get("error", {}).get("code") == -32002
            or raw.get("isError") is True
            and (
                raw.get("structuredContent", {}).get("error_code") == "bridge_offline"
                or raw.get("content")
                == [
                    {
                        "type": "text",
                        "text": "send_message is not enabled on this Bridge (start it with --allow-send)",
                    }
                ]
            )
            or result.get("status") in ("failed", "disabled", "error", "unsupported")
        )
        if refused:
            receipt = {"version": 1, "status": "rejected", "code": "send_unavailable"}
            with prefs.jsonstore.lock(path):
                data = load()
                data["send:" + proof["action_id"]]["receipt"] = receipt
                save(data)
            return receipt
        if type(result) is not dict or result.get("status") not in ("sent", "already_sent"):
            return {"version": 1, "status": "unknown"}
        if (
            result.get("to") != review["to"]
            or result.get("channel") != review["channel"]
            or result.get("request_id") != proof["action_id"]
        ):
            return {"version": 1, "status": "unknown"}
        digest = hashlib.sha256()
        for part in (review["channel"], review["to"], review["body"]):
            encoded = part.encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        if result.get("payload_hash") != digest.hexdigest():
            return {"version": 1, "status": "unknown"}
        receipt = {
            "version": 1,
            "status": "committed",
            "action_id": proof["action_id"],
            "event_id": proof["event_id"],
            "payload_hash": proof["payload_hash"],
            "args_hash": proof["args_hash"],
            "outcome": {
                "operation": "send",
                "channel": review["channel"],
                "to": review["to"],
                "review_hash": review["review_hash"],
            },
        }
        with prefs.jsonstore.lock(path):
            data = load()
            data["send:" + proof["action_id"]]["receipt"] = receipt
            save(data)
        return receipt
    except RefusalError as error:
        return {"version": 1, "status": "rejected", "code": error.code}
