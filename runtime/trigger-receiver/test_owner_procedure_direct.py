"""Exact owner rule review, original canonical append, immutable recovery."""

import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

import runtime_owner_preference_tool as tool
from test_runtime_owner_preference_tool import context, request, call

__all__ = ["context"]


@pytest.fixture
def direct(context, monkeypatch):
    writer = tool._preferences()
    monkeypatch.setattr(tool, "_preferences", lambda: writer)
    from runtime_owner_loop import _load_writer

    _load_writer()
    import master_file

    def prepare(rule="Always put the decision first.", event="a" * 64):
        proposal = request(
            {"operation": "propose-procedure", "rule": rule},
            operation="preview",
            event=event,
            text="My rule: " + rule,
        )
        preview = call(proposal, context)
        assert preview["status"] == "preview"
        encoded = json.dumps(proposal["args"], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        approval = {
            "event_id": proposal["event_id"],
            "action_id": proposal["action_id"],
            "args_hash": hashlib.sha256(encoded.encode()).hexdigest(),
            "accepted_dispatch_id": "accepted-rule-review",
            "source_revision": "owner-only-context-v1",
        }
        return request(
            {"operation": "append-procedure", "review": preview["review"], "proposal": approval},
            event="d" * 64,
            text="Yes.",
        )

    return writer, master_file, prepare


def test_original_append_preserves_sections_and_uncached_context(direct, context):
    writer, master, prepare = direct
    master.set_section("About", "Owner context")
    master.append_section("Procedures", "Earlier rule.")
    before = Path(master.path()).read_bytes()
    body = prepare()
    assert Path(master.path()).read_bytes() == before
    result = call(body, context)
    assert result["status"] == "committed" and result["outcome"]["rule"] == body["args"]["review"]["rule"]
    assert master.read().count("Always put the decision first.") == 1
    assert "Owner context" in master.read() and "Earlier rule." in master.read()
    assert result["outcome"]["rule"] in master.render_for_prompt()
    Path(master.path()).unlink()
    assert call(dict(body, operation="inspect"), context) == result
    saved = writer.jsonstore.read(writer._path())["owner_preference_receipts"]
    writer.add("vip_people", "Dana")
    assert writer.jsonstore.read(writer._path())["owner_preference_receipts"] == saved


@pytest.mark.parametrize(
    "rule",
    [
        "## About",
        "two\nlines",
        "bad\tcontrol",
        "bad\u0085break",
        "bad\u2028break",
        "bad\u2029break",
        " leading",
    ],
)
def test_invalid_rule_never_writes(direct, context, rule):
    _, master, _ = direct
    body = request({"operation": "propose-procedure", "rule": rule}, operation="preview", text="Rule " + rule)
    with pytest.raises(ValueError):
        call(body, context)
    assert not Path(master.path()).exists()


def test_exact_owner_span_review_bound_and_unicode_transport(direct, context):
    _, master, prepare = direct
    unrelated = request(
        {"operation": "propose-procedure", "rule": "Invented rule"}, operation="preview", text="My own words"
    )
    assert call(unrelated, context)["code"] == "owner_span_unavailable"
    rule = "Use clear language " + ("界" * 1800) + ("😀" * 1000)
    body = prepare(rule)
    assert len(json.dumps(body, ensure_ascii=False).encode()) < 65536
    result = call(body, context)
    assert result["status"] == "committed" and len(json.dumps(result, ensure_ascii=False).encode()) < 65536
    assert rule in master.read()
    overflow = "😀" * 1999
    with pytest.raises(ValueError, match="review too long"):
        prepare(overflow)


def test_capacity_refuses_without_trim_and_retains_original_diagnostic(direct, context):
    _, master, prepare = direct
    name = "A" * 220
    master.set_section(name, "X" * 7700)
    before = Path(master.path()).read_bytes()
    body = prepare("R" * 300)
    result = call(body, context)
    assert result["code"] == "master_capacity"
    assert result["capacity"] == {
        "cap": 8000,
        "largest_section": {"name": "A" * 200, "chars": 7702, "name_truncated": True},
    }
    assert Path(master.path()).read_bytes() == before
    assert call(dict(body, operation="inspect"), context) == result


@pytest.mark.parametrize("failure", ["append_after_write", "master_sync", "receipt"])
def test_uncertain_write_is_held_never_appended_again(direct, context, monkeypatch, failure):
    writer, master, prepare = direct
    body = prepare()
    append = master.append_section
    count = []

    def observed(*args):
        count.append(True)
        result = append(*args)
        if failure == "append_after_write":
            raise OSError("synthetic lost append result")
        return result

    monkeypatch.setattr(master, "append_section", observed)
    if failure == "master_sync":
        sync = writer._sync_existing_preferences

        def fail(path):
            if str(path) == master.path():
                raise OSError("synthetic master sync failure")
            return sync(path)

        monkeypatch.setattr(writer, "_sync_existing_preferences", fail)
    if failure == "receipt":
        write = writer._durable_preferences_replace
        writes = []

        def fail(path, data):
            writes.append(True)
            if len(writes) == 2:
                raise OSError("synthetic receipt failure")
            return write(path, data)

        monkeypatch.setattr(writer, "_durable_preferences_replace", fail)
    assert call(body, context) == {"version": 1, "status": "unknown"}
    assert master.read().count(body["args"]["review"]["rule"]) == 1
    assert call(dict(body, operation="inspect"), context) == {"version": 1, "status": "unknown"}
    assert call(body, context) == {"version": 1, "status": "unknown"} and len(count) == 1


def test_distinct_confirmation_and_proposal_consumed_once(direct, context):
    _, master, prepare = direct
    body = prepare()
    same = request(body["args"], event="a" * 64, text="Yes")
    assert call(same, context)["status"] == "rejected" and not Path(master.path()).exists()
    assert call(body, context)["status"] == "committed"
    second = request(body["args"], event="e" * 64, text="Yes")
    assert (
        call(second, context)["status"] == "rejected"
        and master.read().count(body["args"]["review"]["rule"]) == 1
    )


def test_old_proposal_stays_consumed_after_scalar_receipt_churn(direct, context):
    writer, master, prepare = direct
    body = prepare()
    committed = call(body, context)
    assert committed["status"] == "committed"
    for i in range(writer.RECEIPTS_KEPT + 50):
        proof = {
            "domain": "sotto.owner-preference-tool.v1",
            "event_id": f"{i + 1:064x}",
            "action_id": f"{i + 1001:064x}",
            "payload_hash": "a" * 64,
            "text_hash": "b" * 64,
            "identity_digest": "c" * 64,
            "args_hash": "d" * 64,
            "args": {"operation": "clear-tone"},
            "admitted_at_ms": 1,
            "provider_thread_id": "owner-thread",
        }
        writer.admitted_owner_preference(proof)
    saved = json.loads(Path(writer._path()).read_text())["owner_preference_receipts"]
    assert body["event_id"] in saved
    assert call(dict(body, operation="inspect"), context) == committed
    another_event = request(body["args"], event="e" * 64, text="Yes")
    assert call(another_event, context)["code"] == "target_unavailable"
    assert master.read().count(body["args"]["review"]["rule"]) == 1


def test_independent_confirmed_appends_preserve_each_other(direct, context):
    _, master, prepare = direct
    one = prepare("First rule.")
    two = prepare("Second rule.", event="b" * 64)
    two = request(two["args"], event="e" * 64, text="Yes")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda body: call(body, context), (one, two)))
    assert all(result["status"] == "committed" for result in results)
    assert "First rule." in master.read() and "Second rule." in master.read()


def test_lone_surrogate_rule_refuses_before_any_effect(direct):
    writer, master, _ = direct
    with pytest.raises(ValueError):
        writer.procedure_rule_review("bad\ud800", master)
    assert not Path(master.path()).exists()


def test_existing_unreadable_master_refuses_before_marker(direct, context, monkeypatch):
    import builtins

    writer, master, prepare = direct
    master.set_section("About", "Existing owner context")
    body = prepare()
    original = Path(master.path()).read_bytes()
    open_file = builtins.open

    def denied(path, *args, **kwargs):
        if str(path) == master.path():
            raise PermissionError("synthetic unreadable canonical file")
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    assert call(body, context)["code"] == "target_unavailable"
    monkeypatch.setattr(builtins, "open", open_file)
    assert Path(master.path()).read_bytes() == original
    assert body["event_id"] not in writer.jsonstore.read(writer._path(), {}).get(
        "owner_preference_receipts", {}
    )


def test_committed_response_loss_reopen_and_malformed_receipt(direct, context, monkeypatch):
    writer, master, prepare = direct
    body = prepare()
    result = call(body, context)  # caller loses response after durable commit
    append = master.append_section
    monkeypatch.setattr(
        master, "append_section", lambda *args: pytest.fail("must not repeat original writer")
    )
    monkeypatch.setattr(tool, "_preferences", lambda: writer)
    assert call(dict(body, operation="inspect"), context) == result
    assert master.read().count(body["args"]["review"]["rule"]) == 1
    data = writer.jsonstore.read(writer._path())
    data["owner_preference_receipts"][body["event_id"]]["outcome"]["rule"] = "Never actually saved."
    writer.jsonstore.write_atomic(writer._path(), data)
    with pytest.raises(ValueError, match="receipt invalid"):
        call(dict(body, operation="inspect"), context)
    monkeypatch.setattr(master, "append_section", append)


def test_changed_event_proof_conflicts_with_saved_receipt(direct, context):
    _, _, prepare = direct
    body = prepare()
    assert call(body, context)["status"] == "committed"
    changed = request(body["args"], operation="inspect", event=body["event_id"], text="Confirm")
    assert call(changed, context)["code"] == "event_conflict"


def test_master_durability_precedes_committed_receipt(direct, context, monkeypatch):
    writer, master, prepare = direct
    body = prepare()
    sync = writer._sync_existing_preferences
    write = writer._durable_preferences_replace
    master_synced = []

    def tracked_sync(path):
        result = sync(path)
        if str(path) == master.path():
            master_synced.append(True)
        return result

    def tracked_write(path, data):
        saved = data["owner_preference_receipts"][body["event_id"]]
        if saved["status"] == "committed":
            assert master_synced == [True]
        return write(path, data)

    monkeypatch.setattr(writer, "_sync_existing_preferences", tracked_sync)
    monkeypatch.setattr(writer, "_durable_preferences_replace", tracked_write)
    assert call(body, context)["status"] == "committed"
