"""Loop visibility is accepted-delivery provenance, never model proposal activity."""
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone

import yaml


HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cr = _load("cr_delivery_surface", "morning-brief/scripts/continuity_resolve.py")
de = _load("de_delivery_surface", "_shared/lib/delivery_effects.py")
scan = _load("scan_delivery_surface", "_shared/scripts/retune_scan.py")


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")


def _write(tmp_path, key="loop", **values):
    directory = tmp_path / "knowledge" / "continuity"
    directory.mkdir(parents=True, exist_ok=True)
    row = {"anchor_key": key, "status": "open", "action_type": "reply",
           "channel": "gmail", "contact_name": "Maya", "contact_identifier": "maya@example.com",
           "summary": "send the deck", "created_at": "2026-09-17", **values}
    path = directory / f"{key}.md"
    path.write_text("---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    return row, path


def _fm(path):
    return yaml.safe_load(path.read_text().split("---\n")[1])


def test_capture_deduplicates_without_claiming_delivery(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    action = {"type": "reply", "channel": "gmail", "contactName": "Maya",
              "contactIdentifier": "maya@example.com", "contextSummary": "send the deck"}
    cr.resolve({"today": "2026-09-18", "new_actions": [action], "local": {}},
               datetime(2026, 9, 18, 9))
    out = cr.resolve({"today": "2026-09-18", "new_actions": [action], "local": {}},
                     datetime(2026, 9, 18, 10))
    assert len(out["active"]) == 1
    assert "times_surfaced" not in out["active"][0]
    assert "delivery_surface" not in out["active"][0]


def test_accepted_delivery_counts_once_and_replay_is_idempotent(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row, path = _write(tmp_path)
    effect = {"kind": "loop_surfaced", "anchor_key": "loop",
              "loop_version": de.loop_version(row), "delivery_id": "a" * 32}
    receipt = {"accepted_at": 1_789_742_400}

    assert de.valid([effect], now=1_789_742_399)
    assert de.finalize([effect], receipt)
    assert de.finalize([effect], receipt)
    provenance = _fm(path)["delivery_surface"]
    assert provenance["schema"] == 1 and "count" not in provenance
    assert len(provenance["delivery_keys"]) == 1
    assert "provider-message-1" not in json.dumps(provenance)


def test_stage_binds_surface_effect_to_stable_outbox_run(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    run = "c" * 32
    monkeypatch.setenv("SOTTO_DELIVERY_RUN_ID", run)
    assert de.stage([{"kind": "loop_surfaced", "anchor_key": "loop", "loop_version": "v"}])
    staged = json.loads((tmp_path / "events" / f"delivery-effects-{run}.json").read_text())
    assert staged["effects"] == [{"kind": "loop_surfaced", "anchor_key": "loop",
                                   "loop_version": "v", "delivery_id": run}]


def test_restated_loop_still_counts_but_a_different_debt_does_not(tmp_path, monkeypatch):
    """Between staging and the channel's acceptance a row can be restated (the follow-up merge
    appends evidence, a deadline edit, the evening's pass while a morning brief waits on a slow
    channel). Same identity = the debt the user read about = counted. A different debt under the
    same anchor (a fresh ask restarted the clock) is not."""
    _env(tmp_path, monkeypatch)
    row, path = _write(tmp_path)
    effect = {"kind": "loop_surfaced", "anchor_key": "loop", "loop_identity": de.loop_identity(row),
              "loop_version": de.loop_version(row), "delivery_id": "b" * 32}
    restated = _fm(path)
    restated["summary"] = "send the deck and the pricing appendix"
    restated["deadline"] = "2026-09-30"
    restated["source_refs"] = [{"source": "granola", "id": "m1"}]
    path.write_text("---\n" + yaml.safe_dump(restated, sort_keys=False) + "---\n")
    assert de.valid([effect], now=1_789_742_399)  # bookkeeping cannot cancel the whole brief
    assert de.finalize([effect], {"accepted_at": 1_789_742_400})
    assert de.delivered_surface_count(_fm(path)) == 1

    different = _fm(path)
    different["created_at"] = "2026-09-20"          # a fresh ask restarted the clock
    different["source_message_id"] = "new-message"
    path.write_text("---\n" + yaml.safe_dump(different, sort_keys=False) + "---\n")
    later = {**effect, "delivery_id": "c" * 32}
    assert de.finalize([later], {"accepted_at": 1_789_828_800})
    assert de.delivered_surface_count(_fm(path)) == 1


def test_legacy_effect_without_identity_still_binds_its_exact_version(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row, path = _write(tmp_path)
    effect = {"kind": "loop_surfaced", "anchor_key": "loop",
              "loop_version": de.loop_version(row), "delivery_id": "b" * 32}
    changed = _fm(path)
    changed["summary"] = "a different task"
    path.write_text("---\n" + yaml.safe_dump(changed, sort_keys=False) + "---\n")
    assert de.finalize([effect], {"accepted_at": 1_789_742_400})
    assert "delivery_surface" not in _fm(path)


def test_closed_loop_surface_effect_also_cannot_cancel_delivery(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row, path = _write(tmp_path)
    effect = {"kind": "loop_surfaced", "anchor_key": "loop",
              "loop_version": de.loop_version(row), "delivery_id": "b" * 32}
    changed = _fm(path)
    changed["status"] = "resolved"
    path.write_text("---\n" + yaml.safe_dump(changed, sort_keys=False) + "---\n")
    assert de.valid([effect], now=1_789_742_399)
    assert de.finalize([effect], {"accepted_at": 1_789_742_400})
    assert "delivery_surface" not in _fm(path)


def test_retune_ignores_legacy_inflation_until_delivery_provenance(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _row, path = _write(tmp_path, created_at="2026-09-18", times_surfaced=99)
    now = datetime(2026, 9, 18, 18, tzinfo=timezone.utc)
    assert scan.scan(now=now)["stale_loops"] == []

    current = _fm(path)
    current["delivery_surface"] = {"schema": 1, "count": 3,
                                   "first_at": "2026-09-18T09:00:00+00:00",
                                   "last_at": "2026-09-18T17:00:00+00:00",
                                   "delivery_keys": ["a", "b", "c"]}
    path.write_text("---\n" + yaml.safe_dump(current, sort_keys=False) + "---\n")
    stale = scan.scan(now=now)["stale_loops"]
    assert stale[0]["times_surfaced"] == 3
    assert stale[0]["surface_count_provenance"] == "accepted_delivery"


def test_loop_update_summary_explicitly_reports_zero(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    cr.resolve({"today": "2026-09-18", "new_actions": [], "local": {}},
               datetime(2026, 9, 18, 9))
    line = next(line for line in capsys.readouterr().err.splitlines()
                if line.startswith("[continuity_resolve] loop update outcomes: "))
    summary = json.loads(line.split(": ", 1)[1])
    assert summary == {"accepted": 0, "rejected": 0, "rejected_by_reason": {}, "total": 0}


def test_learned_counterpart_latency_delays_default_but_not_deadline(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    directory = tmp_path / "knowledge"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "relationship_state.json").write_text(json.dumps({"history": {"person-1": {
        "name": "Maya", "engagement": {"counterpart_reply_lag_days": 6,
        "counterpart_reply_lag_expires": "2026-11-01T00:00:00+00:00",
        "last_owner_message_at": "2026-09-17T10:00:00+00:00"}}}}))
    row = {"anchor_key": "waiting", "status": "waiting", "action_type": "waiting_on",
           "canonical_id": "person-1", "contact_name": "Maya", "created_at": "2026-09-17"}
    ref = datetime(2026, 9, 20, 9)
    assert not cr.chase_due(row, "2026-09-20", ref)  # ordinary three-day default was today
    assert cr.chase_due(row, "2026-09-23", datetime(2026, 9, 23, 9))

    row["deadline"] = "2026-09-21"
    learned = cr._learned_chase_after(
        row, datetime(2026, 9, 20, 10, tzinfo=timezone.utc),
        datetime(2026, 9, 20, 9, tzinfo=timezone.utc))
    assert learned.date().isoformat() == "2026-09-21"


def test_new_obligation_uses_learned_duration_from_its_own_date(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    directory = tmp_path / "knowledge"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "relationship_state.json").write_text(json.dumps({"history": {"person-1": {
        "name": "Maya", "engagement": {"counterpart_reply_lag_days": 6,
        "counterpart_reply_lag_expires": "2026-11-04T00:00:00+00:00",
        "last_owner_message_at": "2026-09-01T09:00:00+00:00"}}}}))
    row = {"anchor_key": "waiting", "status": "waiting", "action_type": "waiting_on",
           "canonical_id": "person-1", "contact_name": "Maya", "created_at": "2026-09-15"}
    assert not cr.chase_due(row, "2026-09-18", datetime(2026, 9, 18, 9))
    assert cr.chase_due(row, "2026-09-21", datetime(2026, 9, 21, 9))
    row["deadline"] = "2026-09-19"
    learned = cr._learned_chase_after(
        row, datetime(2026, 9, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 15, 0, tzinfo=timezone.utc))
    assert learned.date().isoformat() == "2026-09-19"


def test_delivered_overdue_chase_keeps_ordinary_cooldown(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    directory = tmp_path / "knowledge"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "relationship_state.json").write_text(json.dumps({"history": {"person-1": {
        "name": "Maya", "engagement": {"counterpart_reply_lag_days": 7,
        "counterpart_reply_lag_expires": "2026-11-01T00:00:00+00:00",
        "last_owner_message_at": "2026-09-17T10:00:00+00:00"}}}}))
    row, _path = _write(tmp_path, action_type="waiting_on", status="waiting",
                        canonical_id="person-1",
                        created_at="2026-09-01", deadline="2026-09-10",
                        chase_pending="2026-09-17")
    result = cr.finalize_chase(row["anchor_key"], datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert result["ok"] is True
    saved = _fm(_path)
    assert saved["chase_after"] == "2026-09-20"
    assert not cr.chase_due(saved, "2026-09-18", datetime(2026, 9, 18, 12,
                                                          tzinfo=timezone.utc))


def test_evidenced_reply_after_deadline_keeps_ordinary_cooldown(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    directory = tmp_path / "knowledge"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "relationship_state.json").write_text(json.dumps({"history": {"person-1": {
        "name": "Maya", "engagement": {"counterpart_reply_lag_days": 7,
        "counterpart_reply_lag_expires": "2026-11-01T00:00:00+00:00",
        "last_owner_message_at": "2026-09-17T08:00:00+00:00"}}}}))
    row, path = _write(tmp_path, action_type="waiting_on", status="waiting",
                       canonical_id="person-1", created_at="2026-09-01",
                       deadline="2026-09-10")
    message = {"id": "reply-1", "from": "Maya <maya@example.com>",
               "date": "2026-09-17T10:00:00+00:00", "body": "Still working on it"}
    update = {"loopId": row["anchor_key"], "loopVersion": de.loop_version(row),
              "status": "waiting", "evidence": [{"sourceType": "email",
              "sourceId": "reply-1", "snippet": "Still working on it"}]}
    out = cr.resolve({"today": "2026-09-17", "loopUpdates": [update],
                      "local": {"emails": [message]}},
                     datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert out["active"][0]["chase_after"] == "2026-09-20"
    assert _fm(path)["chase_after"] == "2026-09-20"


def test_evidenced_reply_before_future_deadline_keeps_ordinary_floor(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row, path = _write(tmp_path, action_type="waiting_on", status="waiting",
                       created_at="2026-09-01", deadline="2026-09-18")
    message = {"id": "reply-1", "from": "Maya <maya@example.com>",
               "date": "2026-09-17T10:00:00+00:00", "body": "Still working on it"}
    update = {"loopId": row["anchor_key"], "loopVersion": de.loop_version(row),
              "status": "waiting", "evidence": [{"sourceType": "email",
              "sourceId": "reply-1", "snippet": "Still working on it"}]}
    out = cr.resolve({"today": "2026-09-17", "loopUpdates": [update],
                      "local": {"emails": [message]}},
                     datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert out["active"][0]["chase_after"] == "2026-09-20"
    assert _fm(path)["chase_after"] == "2026-09-20"


def test_review_candidate_effect_rechecks_then_stamps_only_on_acceptance(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    rc = importlib.import_module("review_candidates")
    now = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
    path = tmp_path / "knowledge/relationship_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    days = [(now - timedelta(days=d)).date().isoformat() for d in (2, 5, 9, 12)]
    path.write_text(json.dumps({"history": {"c_aaa111bbb222": {"name": "Alex Chen", "engagement": {
        "owner_reply_at": now.isoformat(), "attention_boost": 1,
        "attention_boost_expires": "2026-10-01T00:00:00+00:00"},
        "importance_evidence": {"active_days": days, "sent_days": days[::2], "received_days": days[1::2]}}}}))
    candidate = rc.choose(now, {})
    run = "d" * 32
    monkeypatch.setenv("SOTTO_DELIVERY_RUN_ID", run)
    assert de.stage([candidate["effect"]])
    staged = json.loads((tmp_path / "events" / f"delivery-effects-{run}.json").read_text())
    effect = staged["effects"][0]
    assert effect["delivery_id"] == run
    assert de.valid([effect], now.timestamp())
    assert de.finalize([effect], {"accepted_at": now.timestamp()})
    state = json.loads(path.read_text())
    assert state["review_candidate_offers"][0]["canonical_id"] == "c_aaa111bbb222"


def test_stale_review_candidate_never_withholds_the_brief_and_still_spends_its_cooldown(tmp_path, monkeypatch):
    """The 42-day window slid at midnight between compose and send: the question was printed, the
    brief goes out, and the cooldown is recorded anyway — the user saw the question, so it must
    not come back next Friday. Bookkeeping is never a reason to recompose."""
    _env(tmp_path, monkeypatch)
    rc = importlib.import_module("review_candidates")
    now = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
    path = tmp_path / "knowledge/relationship_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    days = [(now - timedelta(days=d)).date().isoformat() for d in (2, 5, 9, 12)]
    path.write_text(json.dumps({"history": {"c_aaa111bbb222": {"name": "Alex Chen", "engagement": {
        "owner_reply_at": now.isoformat(), "attention_boost": 1,
        "attention_boost_expires": (now + timedelta(days=1)).isoformat()},
        "importance_evidence": {"active_days": days, "sent_days": days[::2], "received_days": days[1::2]}}}}))
    candidate = rc.choose(now, {})
    monkeypatch.setenv("SOTTO_DELIVERY_RUN_ID", "e" * 32)
    assert de.stage([candidate["effect"]])
    effect = json.loads((tmp_path / "events" / f"delivery-effects-{'e' * 32}.json").read_text())["effects"][0]
    later = (now + timedelta(days=3)).timestamp()          # the boost expired before the send
    assert not rc.candidate_valid(effect, later)
    assert de.valid([effect], later)                       # never a reason to withhold the brief
    assert de.finalize([effect], {"accepted_at": later})
    offers = json.loads(path.read_text())["review_candidate_offers"]
    assert [o["canonical_id"] for o in offers] == ["c_aaa111bbb222"]
    assert rc.choose(datetime.fromtimestamp(later, timezone.utc) + timedelta(days=7), {}) is None  # not next Friday
