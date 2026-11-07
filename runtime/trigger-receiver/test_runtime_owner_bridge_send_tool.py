import copy
import hashlib
import json
import time
from types import SimpleNamespace

import pytest
import runtime_owner_bridge_send_tool as tool
from test_runtime_owner_loop_close import context, IDENTITY, ROOT, THREAD, capability

__all__ = ["context"]


def request(args, operation="preview", event="d" * 64, text="Send hi to +15555550123"):
    encoded = tool.canonical(args)
    return {
        "version": 1,
        "identity": IDENTITY,
        "operation": operation,
        "action_id": hashlib.sha256(
            json.dumps([event, tool.KIND, encoded], ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
        "event_id": event,
        "payload_hash": "b" * 64,
        "text_hash": hashlib.sha256(text.encode()).hexdigest(),
        "text": text,
        "admitted_at_ms": int(time.time() * 1000),
        "provider_thread_id": THREAD,
        "args": args,
    }


@pytest.fixture
def flow(context, monkeypatch):
    data, policy, activation = context
    policy.update(
        SOTTO_DATA=str(data),
        SOTTO_HOME=str(data / "home"),
        SOTTO_RUNTIME_PROFILE="managed-photon-fixed-five-v1",
        _source_authority=lambda: {"complete": True, "revision": "c" * 64},
    )
    import managed

    monkeypatch.setattr(managed, "_tool_allowed", lambda *a: not (data / "revoked").exists())
    calls = []

    def rpc(body, timeout=90.0):
        if body["method"] == "tools/list":
            return {"result": {"tools": [] if (data / "off").exists() else [{"name": "send_message"}]}}
        args = body["params"]["arguments"]
        calls.append(copy.deepcopy(args))
        assert timeout == 90.0
        if (data / "response.json").exists():
            return json.loads((data / "response.json").read_text())
        if (data / "lost").exists():
            raise TimeoutError("accepted but response lost")
        digest = hashlib.sha256()
        for part in (args["channel"], args["to"], args["body"]):
            raw = part.encode()
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
        return {
            "result": {
                "structuredContent": {
                    "status": "sent",
                    "channel": args["channel"],
                    "to": args["to"],
                    "request_id": args["request_id"],
                    "payload_hash": digest.hexdigest(),
                }
            }
        }

    def call(body):
        return tool.perform(
            body,
            bearer=capability.derive(ROOT, IDENTITY),
            root=ROOT,
            policy=policy,
            data_root=data,
            capability=capability,
            activation=activation,
            relay=SimpleNamespace(mcp_call=rpc),
        )

    def prepare():
        before = request(
            {"operation": "propose_send", "channel": "imessage", "to": "+15555550123", "body": "Hi! café"}
        )
        preview = call(before)
        proposal = {
            "event_id": before["event_id"],
            "action_id": before["action_id"],
            "args_hash": hashlib.sha256(tool.canonical(before["args"]).encode()).hexdigest(),
            "accepted_dispatch_id": "accepted",
            "source_revision": "c" * 64,
        }
        after = request(
            {"operation": "send", "review": preview["review"], "proposal": proposal}, "apply", "e" * 64, "yes"
        )
        return before, after

    return data, policy, calls, call, prepare


def test_review_then_approved_send_once_and_inspect_after_revocation(flow):
    data, _, calls, call, prepare = flow
    _, after = prepare()
    assert calls == []
    result = call(after)
    assert result["status"] == "committed"
    assert len(calls) == 1
    (data / "revoked").touch()
    assert call(after) == result
    assert call({**after, "operation": "inspect"}) == result
    assert len(calls) == 1


@pytest.mark.parametrize("marker", ["off", "revoked"])
def test_switch_off_between_review_and_yes_never_sends(flow, marker):
    data, _, calls, call, prepare = flow
    _, after = prepare()
    (data / marker).touch()
    assert call(after)["status"] == "rejected"
    assert calls == []


def test_lost_response_is_held_and_never_replayed(flow):
    data, _, calls, call, prepare = flow
    _, after = prepare()
    (data / "lost").touch()
    with pytest.raises(TimeoutError):
        call(after)
    (data / "lost").unlink()
    assert call(after)["status"] == "unknown"
    assert call({**after, "operation": "inspect"})["status"] == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["body", "recipient", "thread", "no", "old", "second_yes", "same_event"])
def test_changed_or_unapproved_message_refused(flow, change):
    _, _, calls, call, prepare = flow
    before, after = prepare()
    if change == "body":
        after["args"]["review"]["body"] = "Different"
    elif change == "recipient":
        after["args"]["review"]["to"] = "+15555550999"
    elif change == "thread":
        after["provider_thread_id"] = "foreign"
    elif change == "no":
        after["text"] = "no"
    elif change == "old":
        after["admitted_at_ms"] -= 16 * 60 * 1000
    elif change == "same_event":
        after["event_id"] = before["event_id"]
    elif change == "second_yes":
        assert call(after)["status"] == "committed"
        after["event_id"] = "f" * 64
    refreshed = request(after["args"], "apply", after["event_id"], after["text"])
    after.update(action_id=refreshed["action_id"], text_hash=refreshed["text_hash"])
    try:
        assert call(after)["status"] == "rejected"
    except (ValueError, tool.UnavailableError):
        pass
    assert len(calls) == (1 if change == "second_yes" else 0)


def test_fabricated_preview_cannot_authorize_send(flow):
    data, _, calls, call, prepare = flow
    _, after = prepare()
    (data / "events/bridge-send-receipts.json").unlink()
    assert call(after)["status"] == "rejected"
    assert calls == []


@pytest.mark.parametrize(
    "response,expected",
    [
        ({"error": {"code": -32002}}, "rejected"),
        ({"result": {"isError": True, "structuredContent": {"error_code": "bridge_offline"}}}, "rejected"),
        (
            {
                "result": {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": "send_message is not enabled on this Bridge (start it with --allow-send)",
                        }
                    ],
                }
            },
            "rejected",
        ),
        ({"result": {"structuredContent": {"status": "failed"}}}, "rejected"),
        ({"result": {"structuredContent": {"status": "disabled"}}}, "rejected"),
        ({"error": {"code": -32001}}, "unknown"),
        ({"result": {"structuredContent": {"status": "request_conflict"}}}, "unknown"),
        ({"result": {"structuredContent": {"status": "unknown"}}}, "unknown"),
        ({"result": {"isError": True, "content": [{"type": "text", "text": "unexpected error"}]}}, "unknown"),
    ],
)
def test_real_relay_responses_and_stale_catalog_never_replay(flow, response, expected):
    data, _, calls, call, prepare = flow
    _, after = prepare()
    (data / "response.json").write_text(json.dumps(response))
    assert call(after)["status"] == expected
    (data / "response.json").unlink()
    assert call({**after, "operation": "inspect"})["status"] == expected
    assert call(after)["status"] == expected
    assert len(calls) == 1


def test_expired_reviews_pruned_but_uncertain_claims_never_removed(flow):
    data, _, calls, call, prepare = flow
    before, after = prepare()
    (data / 'lost').touch()
    with pytest.raises(TimeoutError):
        call(after)
    path = data / 'events/bridge-send-receipts.json'
    stored = json.loads(path.read_text())
    stored['review:' + before['action_id']]['proof']['admitted_at_ms'] -= 900001
    claim = copy.deepcopy(stored['send:' + after['action_id']])
    path.write_text(json.dumps(stored))
    fresh = request({'operation': 'propose_send', 'channel': 'imessage', 'to': '+15555550123', 'body': 'A separate review'}, event='f' * 64)
    assert call(fresh)['status'] == 'preview'
    stored = json.loads(path.read_text())
    assert 'review:' + before['action_id'] not in stored
    assert stored['send:' + after['action_id']] == claim
    assert stored['used:' + before['action_id']] == after['action_id']
    assert call(after)['status'] == 'unknown'
    assert len(calls) == 1


@pytest.mark.parametrize('answer', ['yes', 'send it', 'yes, send it'])
def test_send_confirmation_reuses_gmail_send_words_and_stores_only_hash(flow, answer):
    data, _, calls, call, prepare = flow
    before, after = prepare()
    after = request(after['args'], 'apply', 'e' * 64, answer)
    result = call(after)
    assert result['status'] == 'committed'
    path = data / 'events/bridge-send-receipts.json'
    stored = json.loads(path.read_text())
    claim = stored['send:' + after['action_id']]
    assert 'proof' not in claim and len(claim['proof_hash']) == 64
    assert 'Hi!' not in json.dumps(claim)
    assert call(after) == result
    assert len(calls) == 1


def test_legacy_full_proof_remains_replay_safe_and_compacts_on_write(flow, monkeypatch):
    data, _, calls, call, prepare = flow
    proofs = []
    validate = tool.validate_tool_admission
    def capture(*args, **kwargs):
        proof = validate(*args, **kwargs)
        proofs.append(copy.deepcopy(proof))
        return proof
    monkeypatch.setattr(tool, 'validate_tool_admission', capture)
    _, after = prepare()
    result = call(after)
    legacy = proofs[-1]
    path = data / 'events/bridge-send-receipts.json'
    stored = json.loads(path.read_text())
    claim = stored['send:' + after['action_id']]
    del claim['proof_hash']
    claim['proof'] = legacy
    path.write_text(json.dumps(stored))
    assert call(after) == result
    fresh = request({'operation': 'propose_send', 'channel': 'imessage', 'to': '+15555550123', 'body': 'Separate'}, event='f' * 64)
    assert call(fresh)['status'] == 'preview'
    assert 'proof' not in json.loads(path.read_text())['send:' + after['action_id']]
    assert call(after) == result
    assert len(calls) == 1
