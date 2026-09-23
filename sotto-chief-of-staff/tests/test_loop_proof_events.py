"""Observed ledger decisions survive later writes without claiming unmeasured error rates."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("continuity_proof_test", ROOT / "morning-brief/scripts/continuity_resolve.py")
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)
import log_outcome  # noqa: E402
import release_one_proof  # noqa: E402
import knowledge_edit  # noqa: E402
import retune_apply  # noqa: E402


class Clock(datetime):
    instant = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz or timezone.utc)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.delenv("SOTTO_DELIVERY_RUN_ID", raising=False)
    monkeypatch.setattr(log_outcome, "datetime", Clock)
    Clock.instant = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
    return tmp_path


def row():
    return {"anchor_key": "thread:private-thread", "created_at": "2026-09-21T11:00:00Z",
            "status": "open", "summary": "Private agreement details", "action_type": "follow_up",
            "contact_identifier": "private@example.com", "contact_name": "Private Person"}


def events(root):
    return [json.loads(line) for line in (root / "outcomes.jsonl").read_text().splitlines()]


def test_saved_transitions_are_content_free_and_idempotent(state):
    item = row()
    cr._persist(item)
    first = item["proof_transition"]["event_id"]
    cr._persist(item)
    assert len(events(state)) == 1
    Clock.instant += timedelta(hours=1)
    cr._terminate(item, "dismissed", "user_dismissed", "2026-09-21")
    cr._persist(item)
    cr._persist(item)
    retained = events(state)
    assert len(retained) == 2 and retained[-1]["event_id"] != first
    assert all(event["ts"].endswith("Z") for event in retained)
    assert retained[-1]["actor"] == "user"
    raw = (state / "outcomes.jsonl").read_text()
    for private in ("private@example.com", "Private Person", "Private agreement", "private-thread"):
        assert private not in raw


def test_failed_projection_does_not_undo_saved_correction_and_retry_recovers(state, monkeypatch):
    item = row()
    cr._persist(item)
    real = log_outcome.log
    monkeypatch.setattr(log_outcome, "log", lambda rec: (_ for _ in ()).throw(OSError("disk")))
    Clock.instant += timedelta(hours=1)
    cr._terminate(item, "dismissed", "user_dismissed", "2026-09-21")
    cr._persist(item)
    persisted = cr._load_items()[item["anchor_key"]]
    assert persisted["status"] == "dismissed"
    report = release_one_proof.build_report(now=Clock.instant)
    assert report["correlations"]["quick_dismissal_to_false_capture"]["loops_dismissed_within_24h_of_observed_capture"] == 1
    monkeypatch.setattr(log_outcome, "log", real)
    cr._persist(persisted)
    assert len(events(state)) == 2
    assert release_one_proof.build_report(now=Clock.instant) == report


def test_rejected_completion_then_manual_resolution_is_review_candidate(state, monkeypatch):
    item = row()
    cr._persist(item)
    monkeypatch.setenv("SOTTO_DELIVERY_RUN_ID", "a" * 32)
    proposal = {"loopId": item["anchor_key"], "status": "resolved", "evidence": [{"text": "private source words"}]}
    stamp = Clock.instant.isoformat()
    log_outcome.record_loop_proposal(proposal, item, "rejected", "unsupported_evidence", stamp)
    log_outcome.record_loop_proposal(proposal, item, "rejected", "unsupported_evidence", stamp)
    Clock.instant += timedelta(hours=1)
    assert knowledge_edit.op_loop(item["anchor_key"], "resolved", today="2026-09-21")["ok"]
    report = release_one_proof.build_report(now=Clock.instant)
    count = report["correlations"]["proposal_to_later_manual_resolution"]
    assert count["rejected_completion_proposals"] == 1
    assert count["loops_later_manually_resolved"] == 1
    assert count["status"] == "observed"
    assert count["error_rate"] is None
    assert "private source words" not in (state / "outcomes.jsonl").read_text()


def test_local_proposal_counts_are_labeled_as_candidate_observations(state):
    item = row()
    cr._persist(item)
    proposal = {"status": "resolved"}
    log_outcome.record_loop_proposal(proposal, item, "rejected", "unsupported_evidence",
                                     Clock.instant.isoformat())
    Clock.instant += timedelta(seconds=1)
    log_outcome.record_loop_proposal(proposal, item, "rejected", "unsupported_evidence",
                                     Clock.instant.isoformat())
    report = release_one_proof.build_report(now=Clock.instant)
    correlation = report["correlations"]["proposal_to_later_manual_resolution"]
    assert correlation["status"] == "candidate_observations"
    assert correlation["rejected_completion_proposals"] == 2
    assert report["correlation_observations"]["proposal_dedupe"] == "partial_local_candidates"


def test_new_obligation_cannot_pay_off_old_rejected_proposal(state):
    item = row()
    cr._persist(item)
    log_outcome.record_loop_proposal({"status": "resolved"}, item, "rejected", "unsupported_evidence", Clock.instant.isoformat())
    Clock.instant += timedelta(hours=1)
    cr._terminate(item, "resolved", "replied", "2026-09-21")
    cr._persist(item)
    item.update(status="open", created_at="2026-09-21T14:00:00Z")
    item.pop("resolution", None)
    Clock.instant += timedelta(hours=1)
    cr._persist(item)
    Clock.instant += timedelta(hours=1)
    cr._terminate(item, "resolved", "user_resolved", "2026-09-21")
    cr._persist(item)
    count = release_one_proof.build_report(now=Clock.instant)["correlations"]["proposal_to_later_manual_resolution"]
    assert count["loops_later_manually_resolved"] == 0


def test_repeated_parking_cycles_are_distinct_but_retries_are_not(state):
    item = row()
    cr._persist(item)
    for status in ("parked", "open", "parked", "open"):
        Clock.instant += timedelta(minutes=1)
        item["status"] = status
        cr._persist(item)
        cr._persist(item)
    assert retune_apply.apply("dismiss", item["anchor_key"])["ok"]
    assert len(events(state)) == 6
    report = release_one_proof.build_report(now=Clock.instant)
    assert report["correlations"]["resurfacing_after_terminal_or_parked_state"]["reopened_loops_later_dismissed"] == 1
    assert report["correlation_observations"]["status"] == "observed_only"


def test_resolver_rejection_has_known_identity_without_evidence_text(state):
    item = row()
    cr._persist(item)
    cr.resolve({"loop_updates": [{"loopId": item["anchor_key"], "status": "resolved",
                                "loopVersion": "wrong", "evidence": [{"text": "secret"}]}]},
               Clock.instant, merge=False)
    proposal = next(r for r in events(state) if r["outcome"] == "loop_proposal")
    assert proposal["reason"] == "stale_revision" and proposal["loop_identity"]
    assert "secret" not in json.dumps(proposal)


def test_report_skips_malformed_event_fields(state):
    (state / "outcomes.jsonl").write_text(json.dumps({
        "schema": 1, "outcome": "loop_transition", "ts": Clock.instant.isoformat(),
        "event_id": "f" * 64, "loop_identity": ["bad"], "from_status": ["bad"]}) + "\n")
    report = release_one_proof.build_report(now=Clock.instant)
    assert report["correlation_observations"]["malformed_or_unreadable"] == 1
    assert report["correlations"]["proposal_to_later_manual_resolution"]["status"] == "unavailable"


def test_failed_ledger_replace_never_claims_saved_transition(state, monkeypatch):
    item = row()
    cr._persist(item)
    cr._terminate(item, "resolved", "user_resolved", "2026-09-21")
    monkeypatch.setattr(cr.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        cr._persist(item)
    assert cr._load_items()[item["anchor_key"]]["status"] == "open"
    assert len(events(state)) == 1


def test_user_lock_and_manual_capture_are_not_automatic_error_candidates(state):
    item = {**row(), "channel": "manual", "resolution_mode": "explicit"}
    cr._persist(item)
    log_outcome.record_loop_proposal({"status": "resolved"}, item, "rejected", "protected_loop", Clock.instant.isoformat())
    Clock.instant += timedelta(hours=1)
    assert knowledge_edit.op_loop(item["anchor_key"], "resolved", today="2026-09-21")["ok"]
    report = release_one_proof.build_report(now=Clock.instant)
    assert report["correlations"]["proposal_to_later_manual_resolution"]["rejected_completion_proposals"] == 0
    item = {**row(), "channel": "manual", "anchor_key": "manual:other"}
    cr._persist(item)
    assert retune_apply.apply("dismiss", item["anchor_key"])["ok"]
    report = release_one_proof.build_report(now=Clock.instant)
    assert report["correlations"]["quick_dismissal_to_false_capture"]["loops_dismissed_within_24h_of_observed_capture"] == 0
