"""Release-one pilot counters are read-only and explicit about historical coverage."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "_shared/scripts/release_one_proof.py"
spec = importlib.util.spec_from_file_location("release_one_proof", SCRIPT)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)
NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def test_completed_local_days_exclude_today_and_require_both_exact_slots(tmp_path):
    exact = {'status': 'ok', 'proof': {'coverage': 'exact_resolver_outcomes', 'loop_updates': {
        'total': 1, 'accepted': 1, 'rejected': 0, 'rejected_by_reason': {}}}}
    for name, timestamp in [('2026-09-22.morning', '2026-09-22T13:30:00Z'),
                            ('2026-09-22.evening', '2026-09-23T00:30:00Z'),
                            ('2026-09-23.morning', '2026-09-23T13:30:00Z')]:
        _write_json(tmp_path / f'briefs/{name}.learned.json', {
            'ts': timestamp, 'steps': {'continuity': exact}})
    now = datetime(2026, 9, 24, 5, tzinfo=timezone.utc)  # Sep 23 in California
    report = proof.build_report(str(tmp_path), days=2, now=now, complete_days=True,
                                timezone_name='America/Los_Angeles')
    assert report['learn_receipts']['proposal_outcomes']['proposed'] == 2
    assert report['scheduled_coverage']['complete_exact_days'] == 1
    assert report['scheduled_coverage']['days'][0]['date'] == '2026-09-21'


def test_completed_days_follow_dst_not_a_24_hour_subtraction(tmp_path):
    report = proof.build_report(str(tmp_path), days=1,
        now=datetime(2026, 3, 9, 18, tzinfo=timezone.utc), complete_days=True,
        timezone_name='America/Los_Angeles')
    assert report['window']['since'].startswith('2026-03-08T08:00')
    assert report['window']['until'].startswith('2026-03-09T06:59:59')


def test_rolling_window_does_not_call_its_first_partial_day_complete(tmp_path):
    exact = {'status': 'ok', 'proof': {'coverage': 'exact_resolver_outcomes', 'loop_updates': {
        'total': 0, 'accepted': 0, 'rejected': 0, 'rejected_by_reason': {}}}}
    for slot, stamp in [('morning', '2026-09-22T13:30:00Z'), ('evening', '2026-09-23T00:30:00Z')]:
        _write_json(tmp_path / f'briefs/2026-09-22.{slot}.learned.json', {
            'ts': stamp, 'steps': {'continuity': exact}})
    report = proof.build_report(str(tmp_path), days=1,
        now=datetime(2026, 9, 23, 12, tzinfo=timezone.utc), timezone_name='America/Los_Angeles')
    assert report['learn_receipts']['with_exact_proposal_outcomes'] == 2
    assert report['scheduled_coverage']['complete_exact_days'] == 0


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _ledger(path, **fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + "\n".join(f"{key}: {value}" for key, value in fields.items()) + "\n---\n")


def test_exact_receipts_and_snapshot_proxies_have_separate_denominators(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _write_json(tmp_path / "briefs/2026-09-16.morning.learned.json", {
        "ts": "2026-09-16T13:30:00Z", "steps": {"continuity": {
            "status": "ok", "proof": {"coverage": "exact_resolver_outcomes", "loop_updates": {
                "total": 3, "accepted": 1, "rejected": 2,
                "rejected_by_reason": {"stale_revision": 1, "unsupported_evidence": 1}}}}}})
    _ledger(tmp_path / "knowledge/continuity/a.md", status="dismissed",
            resolution="user_dismissed", resolution_mode="explicit",
            created_at="2026-09-16T08:00:00Z", resolved_at="2026-09-16T09:00:00Z")
    report = proof.build_report(now=NOW)
    assert report["learn_receipts"]["proposal_outcomes"] == {
        "status": "available", "proposed": 3, "accepted": 1, "rejected": 2,
        "rejected_by_reason": {"stale_revision": 1, "unsupported_evidence": 1}}
    proxies = report["ledger_snapshot_proxies"]
    assert proxies["explicit_mode_terminal_rows"] == 1
    assert proxies["user_resolution_rows"] == 1
    assert proxies["closed_within_one_calendar_day_of_created_rows"] == 1
    assert report["correlations"]["quick_dismissal_to_false_capture"]["status"] == "unavailable"


def test_legacy_receipt_makes_proposal_denominator_partial_not_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _write_json(tmp_path / "briefs/legacy.learned.json", {
        "ts": "2026-09-16T13:30:00Z", "steps": {"continuity": {"status": "ok"}}})
    report = proof.build_report(now=NOW)
    assert report["learn_receipts"]["proposal_outcomes"] == {
        "status": "unavailable", "proposed": None, "accepted": None, "rejected": None,
        "rejected_by_reason": None, "missing_receipts": 1,
        "reason": "retained continuity receipts predate exact proposal counters"}
    assert report["correlations"]["proposal_to_later_manual_resolution"]["status"] == "unavailable"


def test_report_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    sentinel = tmp_path / "knowledge/continuity/sentinel.md"
    _ledger(sentinel, status="open", created_at="2026-09-01")
    before = sentinel.read_bytes()
    report = proof.build_report(now=NOW)
    assert report == proof.build_report(now=NOW)
    assert report["learn_receipts"]["proposal_outcomes"] == {
        "status": "unavailable", "reason": "no successful continuity receipts retained in window"}
    assert sentinel.read_bytes() == before
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == [sentinel]


def test_explicit_data_root_does_not_follow_or_mutate_process_environment(tmp_path, monkeypatch):
    inherited = tmp_path / "inherited"
    inspected = tmp_path / "inspected"
    monkeypatch.setenv("SOTTO_DATA", str(inherited))
    _ledger(inherited / "knowledge/continuity/wrong.md", status="open", created_at="2026-09-01")
    _ledger(inspected / "knowledge/continuity/right.md", status="resolved", resolution="replied",
            created_at="2026-09-15", resolved_at="2026-09-16")
    report = proof.build_report(str(inspected), now=NOW)
    assert report["ledger_snapshot_proxies"]["status"] == {"resolved": 1}
    assert proof.os.environ["SOTTO_DATA"] == str(inherited)


def test_failed_continuity_proof_does_not_enter_exact_denominator(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _write_json(tmp_path / "briefs/failed.learned.json", {
        "ts": "2026-09-16T13:30:00Z", "steps": {"continuity": {
            "status": "failed", "proof": {"loop_updates": {
                "total": 9, "accepted": 9, "rejected": 0, "rejected_by_reason": {}}}}}})
    report = proof.build_report(now=NOW)
    assert report["learn_receipts"]["with_exact_proposal_outcomes"] == 0
    assert report["learn_receipts"]["proposal_outcomes"]["status"] == "unavailable"


def test_malformed_exact_stats_are_coverage_gaps_not_exceptions(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _write_json(tmp_path / "briefs/malformed.learned.json", {
        "ts": "2026-09-16T13:30:00Z", "steps": {"continuity": {
            "status": "ok", "proof": {"loop_updates": {
                "total": 2, "accepted": 1, "rejected": 1,
                "rejected_by_reason": {"unsupported_evidence": "one"}}}}}})
    report = proof.build_report(now=NOW)
    assert report["learn_receipts"]["with_exact_proposal_outcomes"] == 0
    assert report["learn_receipts"]["proposal_outcomes"]["status"] == "unavailable"


def test_exact_stats_require_provenance_marker_and_real_integer_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    for name, proof_payload in {
        "missing-marker": {"loop_updates": {
            "total": 1, "accepted": 1, "rejected": 0, "rejected_by_reason": {}}},
        "wrong-marker": {"coverage": "aggregate_guess", "loop_updates": {
            "total": 1, "accepted": 1, "rejected": 0, "rejected_by_reason": {}}},
        "boolean-count": {"coverage": "exact_resolver_outcomes", "loop_updates": {
            "total": True, "accepted": True, "rejected": 0, "rejected_by_reason": {}}},
    }.items():
        _write_json(tmp_path / f"briefs/{name}.learned.json", {
            "ts": "2026-09-16T13:30:00Z",
            "steps": {"continuity": {"status": "ok", "proof": proof_payload}}})
    report = proof.build_report(now=NOW)
    receipts = report["learn_receipts"]
    assert receipts["continuity_ok"] == 3
    assert receipts["with_exact_proposal_outcomes"] == 0
    assert receipts["proposal_outcomes"]["status"] == "unavailable"
    assert receipts["proposal_outcomes"]["missing_receipts"] == 3


def test_invalid_receipt_timestamp_is_not_claimed_outside_window(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _write_json(tmp_path / "briefs/missing-ts.learned.json", {"steps": {}})
    _write_json(tmp_path / "briefs/bad-ts.learned.json", {"ts": "not-a-date", "steps": {}})
    _write_json(tmp_path / "briefs/old.learned.json", {
        "ts": "2025-01-01T00:00:00Z", "steps": {}})
    report = proof.build_report(now=NOW)
    receipts = report["learn_receipts"]
    assert receipts["invalid_timestamp"] == 2
    assert receipts["retained_outside_window"] == 1
    assert receipts["retained_in_window"] == 0


def test_malformed_continuity_metadata_is_unknown_coverage(tmp_path):
    for filename, steps in (("bad-steps", ["bad"]), ("bad-continuity", {"continuity": "bad"})):
        _write_json(tmp_path / f"briefs/{filename}.learned.json", {
            "ts": "2026-09-16T13:30:00Z", "steps": steps})
    report = proof.build_report(str(tmp_path), now=NOW)
    assert report["learn_receipts"]["invalid_step_metadata"] == 2
    assert report["learn_receipts"]["proposal_outcomes"]["status"] == "unavailable"


def test_unknown_timezone_is_a_usage_error_not_a_traceback(tmp_path, capsys):
    with pytest.raises(SystemExit) as stop:
        proof.main(['--data-root', str(tmp_path), '--days', '1', '--complete-days', '--timezone', 'Mars/Olympus'])
    assert stop.value.code == 2
    assert 'IANA zone' in capsys.readouterr().err
