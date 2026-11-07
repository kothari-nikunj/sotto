"""Integration tests for the unwired owner-only continuity seam."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import continuity_resolve as cr  # noqa: E402
import knowledge_edit as ke  # noqa: E402
import ledger_io  # noqa: E402
import loops_query as loops  # noqa: E402
import owner_continuity  # noqa: E402
import protected_ledger_io  # noqa: E402
import retune_scan as retune  # noqa: E402
from ledger_provenance import (  # noqa: E402
    UnknownProvenanceError, classify_ledger_sources, digest_frontmatter, project_entries,
)

NOW = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    return tmp_path


def _one(anchor: str) -> dict:
    return next(row for row in ledger_io.load_entries() if row.get("anchor_key") == anchor)


def test_new_manual_add_is_stamped_and_visible_only_on_protected_path(data_root):
    created = ke.op_loop_add_admitted_owner_text("Send the signed deck", owner_text_span="Send the signed deck", owner_event_id="evt-1", now=NOW)
    row = _one(created["anchor_key"])
    assert row["owner_event_id"] == "evt-1"
    assert row.get("contact_name") in (None, "")
    assert row.get("contact_identifier") in (None, "")
    assert row.get("deadline") in (None, "")
    assert classify_ledger_sources(row) == ("owner", frozenset())
    owner = loops.query_owner_rows([row], now=NOW)
    assert owner["coverage"] == "owner_authored_only"
    assert owner["counts"] == {"you_owe": 1, "waiting_on_them": 0}
    scan = retune.scan_owner_rows([row], now=NOW + timedelta(days=5))
    assert scan["coverage"] == "owner_authored_only"
    assert scan["counts"]["stale"] == 1
    # The current, unprotected CLI behavior is left intact by this prototype.
    assert loops.query()["counts"]["you_owe"] == 1


def test_duplicate_add_never_backfills_legacy_row(data_root):
    created = ke.op_loop_add_admitted_owner_text("Send the signed deck", owner_text_span="Send the signed deck", owner_event_id="evt-1", now=NOW)
    row = _one(created["anchor_key"])
    row.pop("content_provenance")
    cr._persist(row)
    with pytest.raises(ke.EditError, match="invalid provenance"):
        ke.op_loop_add_admitted_owner_text("Send the signed deck", owner_text_span="Send the signed deck", owner_event_id="evt-1", now=NOW)
    legacy = _one(created["anchor_key"])
    assert "content_provenance" not in legacy
    assert loops.query_owner_rows([legacy], now=NOW)["counts"]["you_owe"] == 0


@pytest.mark.parametrize("stamped_source", [False, True])
def test_retarget_source_derived_row_never_becomes_owner_only(data_root, stamped_source):
    source = {
        "anchor_key": "thread:gmail-test|owed_by_you", "action_type": "reply",
        "channel": "email", "status": "open", "contact_name": "Ada",
        "summary": "Reply about source-backed deck", "created_at": "2026-08-01",
        "source": "gmail", "source_thread_id": "gmail-test",
        "source_refs": [{"sourceType": "gmail", "sourceId": "msg-1"}],
    }
    cr._persist(source)
    if stamped_source:
        source = _one(source["anchor_key"])
        source["content_provenance"] = {
            "version": 1, "kind": "sources", "sources": ["gmail"],
            "digest": digest_frontmatter(source),
        }
        cr._persist(source)
        assert classify_ledger_sources(_one(source["anchor_key"]))[0] == "sources"
    result = ke.op_loop_retarget(source["anchor_key"], "Wait for the signed deck",
                                 contact="Ada", direction="them", now=NOW)
    replacement = _one(result["anchor_key"])
    assert replacement["source"] == "user_correction"
    assert replacement["corrected_from"] == source["anchor_key"]
    assert "content_provenance" not in replacement
    assert loops.query_owner_rows([replacement], now=NOW)["counts"]["waiting_on_them"] == 0
    assert retune.scan_owner_rows([replacement], now=NOW + timedelta(days=5))["counts"]["stale"] == 0


def test_retarget_stamped_owner_row_is_not_automatically_restamped(data_root):
    first = ke.op_loop_add_admitted_owner_text("Send the signed deck", owner_text_span="Send the signed deck", owner_event_id="evt-1", now=NOW)
    result = ke.op_loop_retarget(first["anchor_key"], "Await signed deck",
                                 contact="Ada", direction="them", now=NOW)
    replacement = _one(result["anchor_key"])
    assert "content_provenance" not in replacement
    assert loops.query_owner_rows([replacement], now=NOW)["counts"]["waiting_on_them"] == 0


def test_stale_digest_and_legacy_are_withheld_before_counts(data_root):
    created = ke.op_loop_add_admitted_owner_text("Send the signed deck", owner_text_span="Send the signed deck", owner_event_id="evt-1", now=NOW)
    row = _one(created["anchor_key"])
    legacy = {**row, "content_provenance": None}
    changed = {**row, "summary": "Copied source content"}
    assert loops.query_owner_rows([legacy, changed], now=NOW)["counts"]["you_owe"] == 0
    assert retune.scan_owner_rows([legacy, changed], now=NOW + timedelta(days=5))["counts"]["stale"] == 0
    with pytest.raises(UnknownProvenanceError):
        classify_ledger_sources(changed)


def test_protected_owner_views_reject_ambiguous_source_key_without_partial_output(data_root):
    ke.op_loop_add_admitted_owner_text(
        "Send the signed deck", owner_text_span="Send the signed deck",
        owner_event_id="evt-duplicate-source", now=NOW,
    )
    cr._persist({
        "anchor_key": "gmail:source", "source": "gmail", "channel": "email",
        "source_thread_id": "gmail:source", "status": "open",
        "action_type": "reply", "summary": "Withheld source", "created_at": "2026-08-01",
    })
    cr._persist({
        "anchor_key": "manual:legacy", "source": "user_added", "channel": "manual",
        "source_thread_id": "manual:legacy", "status": "open",
        "action_type": "reply", "summary": "Withheld legacy", "created_at": "2026-08-01",
    })
    for view, count in (("loops", "you_owe"), ("retune", "stale")):
        result = owner_continuity.read_owner_continuity(
            str(data_root), now=NOW + timedelta(days=5), view=view)
        assert result["coverage"] == "owner_authored_only"
        assert result["counts"][count] == 1
        assert "Withheld source" not in str(result)
        assert "Withheld legacy" not in str(result)

    directory = data_root / "knowledge" / "continuity"
    owner_file = next(path for path in directory.glob("*.md")
                      if "evt-duplicate-source" in path.read_text())
    original = owner_file.read_text()
    assert "source: user_added" in original
    owner_file.write_text(original.replace("---\n", "---\nsource: gmail\n", 1))
    for view in ("loops", "retune"):
        with pytest.raises(protected_ledger_io.ProtectedLedgerUnavailableError):
            owner_continuity.read_owner_continuity(
                str(data_root), now=NOW + timedelta(days=5), view=view)


@pytest.mark.parametrize("field,value", [
    ("summary", "Copied source content"), ("contact_name", "Ada from the email"),
    ("deadline", "next Friday per the thread"), ("deadline", "2026-09-01"), ("canonical_id", "c_graph0001"),
    ("source_refs", [{"sourceType": "gmail", "sourceId": "m1"}]),
])
def test_existing_writer_content_mutation_does_not_silently_restamp(data_root, field, value):
    created = ke.op_loop_add_admitted_owner_text("Send the signed deck", owner_text_span="Send the signed deck", owner_event_id="evt-1", now=NOW)
    row = cr._load_items()[created["anchor_key"]]
    row[field] = value
    row["chase_pending"] = "2026-08-10"       # bookkeeping riding along does not launder it
    cr._persist(row)
    row = _one(created["anchor_key"])
    with pytest.raises(UnknownProvenanceError):
        classify_ledger_sources(row)
    assert loops.query_owner_rows([row], now=NOW)["counts"]["you_owe"] == 0


def test_a_closure_through_an_unproved_writer_is_not_restamped(data_root):
    created = ke.op_loop_add_admitted_owner_text("Send the signed deck", owner_text_span="Send the signed deck", owner_event_id="evt-1", now=NOW)
    ke.op_loop(created["anchor_key"], "resolved", today="2026-08-10")
    with pytest.raises(UnknownProvenanceError):
        classify_ledger_sources(_one(created["anchor_key"]))


def _owner_view(data_root, when, **kwargs):
    return owner_continuity.read_owner_continuity(str(data_root), now=when, **kwargs)


@pytest.mark.parametrize("deadline", ["2026-09-01", ""])
def test_generic_deadline_edit_refuses_proved_owner_task_without_losing_it(data_root, deadline):
    created = ke.op_loop_add_admitted_owner_text(
        "Send the signed deck", owner_text_span="Send the signed deck",
        owner_event_id="evt-1", now=NOW)
    anchor = created["anchor_key"]
    before = _one(anchor)
    with pytest.raises(ke.EditError, match="owner task.*deadline"):
        ke.op_loop_deadline(anchor, deadline)
    assert _one(anchor) == before
    assert _owner_view(data_root, NOW)["counts"]["you_owe"] == 1
    assert ke.op_loop_close_prepare_admitted_owner_anchor(
        anchor, command="/loop close " + anchor,
        owner_event_id="close-event", now=NOW)["expected_row_digest"] == before["content_provenance"]["digest"]


def test_generic_duplicate_add_cannot_change_owner_deadline(data_root):
    created = ke.op_loop_add_admitted_owner_text(
        "Send the signed deck", owner_text_span="Send the signed deck",
        owner_event_id="evt-1", now=NOW)
    anchor = created["anchor_key"]
    before = _one(anchor)
    with pytest.raises(ke.EditError, match="owner task.*deadline"):
        ke.op_loop_add("Send the signed deck", deadline="2026-09-01", now=NOW)
    assert _one(anchor) == before
    assert _owner_view(data_root, NOW)["counts"]["you_owe"] == 1


def test_bookkeeping_writes_keep_an_owner_task_visible_and_closeable(data_root):
    """PR #817 review: a delivered brief and a park/keep cycle
    are Sotto's own bookkeeping. Each used to leave the owner digest stale, so the task vanished
    from owner-only views and the owner could no longer close it."""
    from delivery_effects import loop_identity, loop_version
    created = ke.op_loop_add_admitted_owner_text("Send the signed deck", owner_text_span="Send the signed deck", owner_event_id="evt-1", now=NOW)
    anchor = created["anchor_key"]
    before = _owner_view(data_root, NOW)
    assert before["counts"]["you_owe"] == 1
    row = cr._load_items()[anchor]
    effect = {"anchor_key": anchor, "delivery_id": "run-1", "loop_version": loop_version(row),
              "loop_identity": loop_identity(row)}
    assert cr.acknowledge_loop_surfaced(effect, {"accepted_at": NOW.timestamp()})
    assert "delivery_surface" in _one(anchor)
    assert classify_ledger_sources(_one(anchor)) == ("owner", frozenset())
    assert _owner_view(data_root, NOW) == before
    pre = ke.op_loop_close_prepare_admitted_owner_anchor(
        anchor, command="/loop close " + anchor, owner_event_id="close-event", now=NOW)
    closed = ke.op_loop_close_admitted_owner_anchor(
        anchor, command="/loop close " + anchor, owner_event_id="close-event",
        expected_row_digest=pre["expected_row_digest"], now=NOW)
    assert closed["created"] is True and _one(anchor)["status"] == "resolved"


def test_an_owner_task_parks_visibly_and_keep_brings_it_back(data_root):
    import ledger_io as lio
    from delivery_effects import loop_version
    t0 = datetime(2026, 9, 1, 15, tzinfo=timezone.utc)
    anchor = ke.op_loop_add_admitted_owner_text("Renew passport", owner_text_span="Renew passport",
                                                owner_event_id="add-2", now=t0)["anchor_key"]
    row = cr._load_items()[anchor]
    warned = datetime(2026, 9, 20, 18, tzinfo=timezone.utc)
    notice = {"anchor_key": anchor, "loop_version": loop_version(row), "touch": lio.last_touch_day(row)}
    assert cr.acknowledge_parking_notice(notice, warned.timestamp())
    later = datetime(2026, 9, 22, 13, tzinfo=timezone.utc)
    cr.resolve({"items": [], "loopUpdates": []}, now=later)
    assert _one(anchor)["status"] == "parked"
    parked = _owner_view(data_root, later, include_parked=True)
    assert [entry["anchor_key"] for entry in parked["parked"]] == [anchor]
    import retune_apply
    assert retune_apply.apply("keep", anchor)["ok"] is True      # the dashboard's keep
    assert _one(anchor)["status"] == "open"
    assert classify_ledger_sources(_one(anchor)) == ("owner", frozenset())
    assert _owner_view(data_root, later)["counts"]["you_owe"] == 1


def test_a_chase_due_owner_waiting_row_never_fails_the_daily_pass(data_root):
    """PR #817 review: after a delivered brief made the digest stale, the next chase write raised
    UnknownProvenanceError out of resolve(), so every daily continuity pass failed."""
    from delivery_effects import loop_identity, loop_version
    t0 = datetime(2026, 9, 20, 15, tzinfo=timezone.utc)
    with cr._ledger_lock():
        anchor = ke._op_loop_add_unlocked("Dana to send the signed lease", contact="Dana", direction="them",
                                          now=t0, cr=cr, owner_authored=True,
                                          owner_event_id="evt-1")["anchor_key"]
    row = cr._load_items()[anchor]
    effect = {"anchor_key": anchor, "delivery_id": "brief-1", "loop_version": loop_version(row),
              "loop_identity": loop_identity(row)}
    assert cr.acknowledge_loop_surfaced(effect, {"accepted_at": t0.timestamp() + 3600})
    later = datetime(2026, 10, 7, 13, tzinfo=timezone.utc)
    cr.resolve({"items": [], "loopUpdates": []}, now=later)
    stamped = _one(anchor)
    assert stamped["chase_pending"] == "2026-10-07"
    assert classify_ledger_sources(stamped) == ("owner", frozenset())
    assert _owner_view(data_root, later)["counts"]["waiting_on_them"] == 1
    # Genuinely stale provenance is withheld but still never fails the pass or its chase.
    stamped["summary"] = "Dana to send the lease and the keys"
    cr._persist(stamped)
    cr.resolve({"items": [], "loopUpdates": []}, now=later + timedelta(days=1))
    assert _owner_view(data_root, later)["counts"]["waiting_on_them"] == 0
    assert _one(anchor)["chase_stalls"] == 1


def test_the_graph_never_rewrites_an_owner_tasks_counterpart(data_root):
    people = data_root / "knowledge" / "people"
    people.mkdir(parents=True)
    (people / "c_maya0001.md").write_text("---\nname: Maya Chen\ncanonical_id: c_maya0001\n"
                                          "identifiers:\n  - maya@example.com\n---\n\n# Maya Chen\n")
    t0 = datetime(2026, 9, 20, 15, tzinfo=timezone.utc)
    with cr._ledger_lock():
        anchor = ke._op_loop_add_unlocked("Maya to send the deck", contact="Maya",
                                          identifier="maya@example.com", direction="them", now=t0,
                                          cr=cr, owner_authored=True,
                                          owner_event_id="evt-1")["anchor_key"]
    cr.resolve({"items": [], "loopUpdates": []}, now=t0 + timedelta(days=1))
    row = _one(anchor)
    assert not row.get("canonical_id")
    assert classify_ledger_sources(row) == ("owner", frozenset())


def test_no_source_authority_is_consulted_for_owner_only(data_root):
    created = ke.op_loop_add_admitted_owner_text("Send the signed deck", owner_text_span="Send the signed deck", owner_event_id="evt-1", now=NOW)
    row = _one(created["anchor_key"])
    def forbidden(_source):
        raise AssertionError("source authority must not run for owner content")
    assert project_entries([row], forbidden) == [row]


def test_existing_model_callable_loop_add_never_stamps(data_root):
    created = ke.run(["--op", "loop-add", "--text", "Text copied from an email",
                      "--contact", "Ada"])
    row = _one(created["anchor_key"])
    assert row["source"] == "user_added" and row["channel"] == "manual"
    assert "content_provenance" not in row
    assert loops.query_owner_rows([row], now=NOW)["counts"]["you_owe"] == 0
    with pytest.raises(ke.EditError):
        ke.op_loop_add_admitted_owner_text("A paraphrase", owner_text_span="Actual owner words",
                                            owner_event_id="evt-2", now=NOW)
    with pytest.raises(ke.EditError):
        ke.op_loop_add_admitted_owner_text("Owner words", owner_text_span="Owner words",
                                            owner_event_id="bad\nevent", now=NOW)


def test_protected_readers_have_no_ambient_io(data_root, monkeypatch):
    created = ke.op_loop_add_admitted_owner_text(
        "Send the signed deck", owner_text_span="Send the signed deck",
        owner_event_id="evt-1", now=NOW,
    )
    row = _one(created["anchor_key"])

    def no_io(*_args, **_kwargs):
        raise AssertionError("protected reader touched ambient I/O")

    monkeypatch.setattr(loops.ledger_io, "load_entries", no_io)
    monkeypatch.setattr(loops.ledger_io, "load_active", no_io)
    monkeypatch.setattr(loops, "configured_tz", no_io)
    monkeypatch.setattr(loops, "_now_local", no_io)
    monkeypatch.setattr(retune.pref, "load_explicit", no_io)
    monkeypatch.setattr(retune, "configured_tz", no_io)
    monkeypatch.setattr(retune, "_now_local", no_io)
    assert loops.query_owner_rows([row], now=NOW)["counts"]["you_owe"] == 1
    scan = retune.scan_owner_rows([row], now=NOW + timedelta(days=5))
    assert scan["counts"]["stale"] == 1 and scan["current"] == {}
    with pytest.raises(ValueError):
        loops.query_owner_rows([row], now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        retune.scan_owner_rows([row], now=NOW.replace(tzinfo=None))


def test_generic_dashboard_add_stays_out_of_chat_owner_view(data_root):
    anchor = ke.op_loop_add("Send the signed deck", now=NOW)["anchor_key"]
    assert _one(anchor)["source"] == "user_added"
    with pytest.raises(UnknownProvenanceError):
        classify_ledger_sources(_one(anchor))
    assert _owner_view(data_root, NOW)["counts"]["you_owe"] == 0
    assert loops.query()["counts"]["you_owe"] == 1
