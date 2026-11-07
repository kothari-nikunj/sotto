"""Original ratings consumed by actual brief/relevance/drafting prompts."""

import json
from datetime import datetime, timedelta, timezone

import pytest
import runtime_owner_preference_tool as tool
from test_runtime_owner_preference_tool import context, request, call

__all__ = ["context"]


@pytest.fixture
def feedback(context, monkeypatch):
    data, policy, _ = context
    writer = tool._preferences()
    monkeypatch.setattr(tool, "_preferences", lambda: writer)
    from runtime_owner_loop import _load_writer

    _load_writer()
    import usefulness_feedback
    import personal_context
    import compose_brief
    import relevance
    import style_apply

    policy["_source_authority"] = lambda: {
        "complete": True,
        "revision": "c" * 64,
        "google_sources": ["gmail"],
        "bridge_sources": [],
        "connector_sources": [],
    }
    monkeypatch.setenv("SOTTO_DEPLOYMENT_MODE", "managed")
    monkeypatch.setenv("SOTTO_TENANT_ID", "synthetic-owner")
    config = data / "config/managed-capabilities.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        json.dumps(
            {"tenant_id": "synthetic-owner", "sources": {"gmail": {"consented": True, "connected": True}}}
        )
    )
    ref = datetime.now(timezone.utc).strftime("%Y-%m-%d") + "_morning"
    root = data / "briefs"
    root.mkdir()
    archive = root / (ref + ".json")
    text = "The school waiver is due tomorrow.\n\nThe donation drive is optional."
    archive.write_text(json.dumps({"brief_text": text, "_source_permissions": ["gmail"]}))
    marker = root / (ref.replace("_", ".") + ".delivered")
    marker.write_text("accepted-run")

    def listing():
        return call(
            request({"operation": "usefulness-list", "source_revision": "c" * 64}, operation="read"), context
        )

    def rating(excerpt="", rating="useful", event="d" * 64):
        item = listing()["items"][0]
        return request(
            {
                "operation": "usefulness",
                "rating": rating,
                "reference": ref,
                "archive_version": item["archive_version"],
                "excerpt": excerpt,
                "source_revision": "c" * 64,
            },
            event=event,
            text="That school waiver was useful.",
        )

    return (
        writer,
        usefulness_feedback,
        personal_context,
        compose_brief,
        relevance,
        style_apply,
        listing,
        rating,
        archive,
        marker,
        config,
    )


def test_actual_next_prompt_consumers_and_rating_replacement(feedback, context):
    writer, original, pc, cb, relevance, style, listing, rating, archive, _, _ = feedback
    listed = listing()
    assert listed["status"] == "usefulness_list" and len(listed["items"]) == 1
    first = "The school waiver is due tomorrow."
    second = "The donation drive is optional."
    one = call(rating(first), context)
    assert one["status"] == "committed"
    call(rating(second, "not_useful", "e" * 64), context)
    call(rating(second, "useful", "f" * 64), context)
    assert [(r["outcome"], r["item"]) for r in pc.feedback()] == [("useful", first), ("useful", second)]
    prompt = cb.build_prompt(cb._load_prompt(), {"type": "morning", "google": {"events": []}, "local": {}})
    assert first in prompt and "USER FEEDBACK ON SPECIFIC PAST OUTPUTS" in prompt
    assert first in relevance.judgment_prompt("Synthetic inbound school question")
    assert first in style.apply({"channel": "imessage"})["guidance"]
    outcomes = (archive.parent.parent / "outcomes.jsonl").read_text()
    assert first not in outcomes and second not in outcomes
    saved = writer.jsonstore.read(writer._path())["owner_preference_receipts"]
    assert first not in json.dumps(saved) and second not in json.dumps(saved)
    writer.add("vip_people", "Dana")
    assert writer.jsonstore.read(writer._path())["owner_preference_receipts"] == saved
    assert original.items()[0]["reference"] == archive.stem


@pytest.mark.parametrize("lost", ["archive", "marker", "consent", "provenance"])
def test_managed_downstream_revoke_or_archive_loss_withholds(feedback, context, lost):
    _, _, pc, cb, relevance, style, _, rating, archive, marker, config = feedback
    body = rating()
    result = call(body, context)
    assert result["status"] == "committed"
    if lost == "archive":
        archive.unlink()
    elif lost == "marker":
        marker.unlink()
    elif lost == "consent":
        config.write_text(
            json.dumps(
                {
                    "tenant_id": "synthetic-owner",
                    "sources": {"gmail": {"consented": False, "connected": True}},
                }
            )
        )
    else:
        archive.write_text(json.dumps({"brief_text": "Original text without permissions"}))
    assert pc.feedback() == []
    assert "USER FEEDBACK ON SPECIFIC PAST OUTPUTS" not in cb.build_prompt(
        cb._load_prompt(), {"type": "morning", "google": {}, "local": {}}
    )
    assert "USER FEEDBACK ON SPECIFIC PAST OUTPUTS" not in relevance.judgment_prompt("Synthetic")
    assert "USER FEEDBACK ON SPECIFIC PAST OUTPUTS" not in style.apply({"channel": "imessage"})["guidance"]
    context[1]["_source_authority"] = lambda: {"complete": False}
    assert call(dict(body, operation="inspect"), context) == result


def test_archive_change_ambiguous_excerpt_and_missing_target_have_no_write(feedback, context):
    _, _, _, _, _, _, _, rating, archive, _, _ = feedback
    body = rating("The school waiver is due tomorrow.")
    value = json.loads(archive.read_text())
    value["brief_text"] += " Changed."
    archive.write_text(json.dumps(value))
    assert call(body, context)["code"] == "target_changed"
    value["brief_text"] = "repeat repeat"
    archive.write_text(json.dumps(value))
    assert call(rating("repeat"), context)["code"] == "target_unavailable"
    assert not (archive.parent.parent / "outcomes.jsonl").exists()


def test_contacting_outcomes_sync_failure_never_reappends(feedback, context, monkeypatch):
    writer, original, _, _, _, _, _, rating, archive, _, _ = feedback
    body = rating("The school waiver is due tomorrow.")
    sync = writer._sync_existing_preferences

    def fail(path):
        if str(path) == original.log_outcome._path():
            raise OSError("synthetic outcomes sync fault")
        return sync(path)

    monkeypatch.setattr(writer, "_sync_existing_preferences", fail)
    assert call(body, context) == {"version": 1, "status": "unknown"}
    outcomes = archive.parent.parent / "outcomes.jsonl"
    before = outcomes.read_bytes()
    assert call(dict(body, operation="inspect"), context) == {"version": 1, "status": "unknown"}
    assert call(body, context) == {"version": 1, "status": "unknown"} and outcomes.read_bytes() == before


def test_strict_source_bookends_and_feedback_age(feedback, context):
    _, _, pc, _, _, _, listing, rating, archive, _, _ = feedback
    body = rating()
    context[1]["_source_authority"] = lambda: {
        "complete": True,
        "revision": "b" * 64,
        "google_sources": ["gmail"],
    }
    assert call(body, context)["code"] == "source_unavailable"
    assert listing()["code"] == "source_unavailable"
    context[1]["_source_authority"] = lambda: {
        "complete": True,
        "revision": "c" * 64,
        "google_sources": ["gmail"],
    }
    assert call(body, context)["status"] == "committed"
    assert pc.feedback(datetime.now(timezone.utc) + timedelta(days=43)) == []


def test_marker_and_source_union_bind_exact_archive_version(feedback, context):
    _, _, _, _, _, _, listing, rating, archive, marker, _ = feedback
    body = rating()
    marker.write_text("different-accepted-run")
    assert call(body, context)["code"] == "target_changed"
    value = json.loads(archive.read_text())
    value["_source_permissions"].append("granola")
    archive.write_text(json.dumps(value))
    assert listing()["items"] == []
    assert not (archive.parent.parent / "outcomes.jsonl").exists()


def test_listing_size_refuses_whole_response_without_truncating(feedback):
    _, _, _, _, _, _, listing, _, archive, _, _ = feedback
    archive.write_text(json.dumps({"brief_text": "😀" * 100000, "_source_permissions": ["gmail"]}))
    with pytest.raises(tool.UnavailableError, match="listing unavailable"):
        listing()
    assert not (archive.parent.parent / "outcomes.jsonl").exists()


def test_list_bounded_eight_and_no_write(feedback):
    _, _, _, _, _, _, listing, _, archive, _, _ = feedback
    day = datetime.now(timezone.utc)
    for offset in range(10):
        date = (day - timedelta(days=offset)).strftime("%Y-%m-%d")
        (archive.parent / (date + "_evening.json")).write_text(
            json.dumps({"brief_text": "Retained synthetic output", "_source_permissions": []})
        )
        (archive.parent / (date + ".evening.delivered")).write_text("accepted")
    before = {p.name: p.read_bytes() for p in archive.parent.iterdir()}
    assert len(listing()["items"]) == 8
    assert {p.name: p.read_bytes() for p in archive.parent.iterdir()} == before
    assert not (archive.parent.parent / "preferences.json").exists()


def test_authority_change_during_selection_never_contacts(feedback, context):
    _, _, _, _, _, _, _, rating, archive, _, _ = feedback
    body = rating()
    reads = 0

    def authority():
        nonlocal reads
        reads += 1
        return {
            "complete": True,
            "revision": "c" * 64 if reads == 1 else "e" * 64,
            "google_sources": ["gmail"],
        }

    context[1]["_source_authority"] = authority
    assert call(body, context)["code"] == "source_unavailable"
    assert not (archive.parent.parent / "outcomes.jsonl").exists()


def test_captured_grant_cannot_override_original_managed_consent(feedback, context):
    _, original, _, _, _, _, listing, rating, archive, _, config = feedback
    body = rating()
    config.write_text(
        json.dumps(
            {"tenant_id": "synthetic-owner", "sources": {"gmail": {"consented": False, "connected": True}}}
        )
    )
    assert original.items(permitted=lambda source: True, versions=True) == []
    assert listing()["items"] == []
    assert call(body, context)["code"] == "target_unavailable"
    assert not (archive.parent.parent / "outcomes.jsonl").exists()


@pytest.mark.parametrize(
    "excerpt", [" ", " The school waiver is due tomorrow.", "The school waiver is due tomorrow. "]
)
def test_admitted_excerpt_is_canonical_and_replayable(feedback, context, excerpt):
    _, _, _, _, _, _, _, rating, archive, _, _ = feedback
    body = rating(excerpt)
    with pytest.raises(ValueError, match="target invalid"):
        call(body, context)
    assert not (archive.parent.parent / "outcomes.jsonl").exists()


def test_unconfirmed_original_result_stays_unknown(feedback, context, monkeypatch):
    _, original, _, _, _, _, _, rating, archive, _, _ = feedback
    body = rating()
    record = original.record

    def unconfirmed(*args, **kwargs):
        record(*args, **kwargs)
        return {"logged": False, "outcome": "useful"}

    monkeypatch.setattr(original, "record", unconfirmed)
    assert call(body, context) == {"version": 1, "status": "unknown"}
    outcomes = archive.parent.parent / "outcomes.jsonl"
    before = outcomes.read_bytes()
    assert call(dict(body, operation="inspect"), context) == {"version": 1, "status": "unknown"}
    assert outcomes.read_bytes() == before


@pytest.mark.parametrize("chars,expected", [(600000, 200), (1100000, 503)])
def test_actual_authenticated_handler_listing_response_bound(feedback, context, monkeypatch, chars, expected):
    import threading
    import urllib.request
    import urllib.error
    from http.server import ThreadingHTTPServer
    from test_receiver import rec
    from test_runtime_owner_loop_close import ROOT, IDENTITY, capability

    _, _, _, _, _, _, _, _, archive, _, _ = feedback
    archive.write_text(json.dumps({"brief_text": "x" * chars, "_source_permissions": ["gmail"]}))
    data, policy, activation = context
    for key, value in policy.items():
        if isinstance(value, str):
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("SOTTO_RUNTIME_PROFILE", "managed-photon-fixed-five-v1")
    monkeypatch.setenv("SOTTO_MCP_TOKEN", ROOT)
    monkeypatch.setenv("SOTTO_TENANT_ID", IDENTITY["tenant_id"])
    # Authority and original consent agree on this managed synthetic owner.
    config = data / "config/managed-capabilities.json"
    config.write_text(
        json.dumps(
            {"tenant_id": IDENTITY["tenant_id"], "sources": {"gmail": {"consented": True, "connected": True}}}
        )
    )
    monkeypatch.setattr(rec, "DATA", str(data))
    monkeypatch.setattr(rec, "RUNTIME_ACTIVATION", activation)
    monkeypatch.setattr(rec, "RUNTIME_OWNER_PREFERENCE_TOOL", tool)
    monkeypatch.setattr(rec, "_runtime_source_authority", policy["_source_authority"])
    server = ThreadingHTTPServer(("127.0.0.1", 0), rec.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    body = request({"operation": "usefulness-list", "source_revision": "c" * 64}, operation="read")
    http = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/runtime/owner-preference-tool",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + capability.derive(ROOT, IDENTITY),
            "Content-Type": "application/json",
            "User-Agent": "OpenAI File Downloader, XaiImageApiFetch/1.0",
        },
    )
    try:
        try:
            response = urllib.request.urlopen(http, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            assert response.status == expected
            if expected == 200:
                assert 512 * 1024 < len(raw) < 1024 * 1024
                assert json.loads(raw)["items"][0]["text"] == "x" * chars
            else:
                assert "x" * 100 not in raw.decode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
