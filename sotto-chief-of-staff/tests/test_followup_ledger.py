"""apply_commitments.py — followup commitments written deterministically into the continuity ledger."""
import glob
import hashlib
import importlib.util
import os
import sys
from datetime import datetime

import yaml

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ac = _load("apply_commitments", "followup/scripts/apply_commitments.py")
cr = _load("continuity_resolve_fl", "morning-brief/scripts/continuity_resolve.py")

NOW = datetime(2026, 7, 2, 10, 0, 0)
USER = "me@x.com"


def _ganchor(meeting_id, what, owner_is_user=True, waiting=False, source_snippet=""):
    """Exact source occurrence: meeting identity + direction + normalized commitment text."""
    role = "user" if owner_is_user else "other"
    digest = hashlib.sha256(
        f"{role}|{ac._normalized_what(source_snippet or what)}".encode()).hexdigest()[:12]
    suffix = ":waiting_on" if waiting else ""
    return f"thread:granola:{meeting_id}:{digest}{suffix}"


def _ledger_items(tmp_path):
    out = {}
    for p in glob.glob(os.path.join(str(tmp_path), "knowledge", "continuity", "*.md")):
        content = open(p, encoding="utf-8").read()
        assert content.startswith("---\n")                    # markdown + YAML frontmatter format
        fm = yaml.safe_load(content[4:content.find("\n---", 4)])
        out[fm["anchor_key"]] = fm
    return out


def test_commitment_with_email_becomes_follow_up_ledger_item(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    res = ac.apply({"commitments": [
        {"meeting": "Acme sync", "meeting_id": "m-acme", "owner": "you", "owner_is_user": True,
         "what": "send the deck", "source_snippet": "Nikunj: I'll send the deck",
         "due": "2026-07-04", "to_email": "dana@acme.com"}]}, USER, NOW)
    assert res["written"] == 1 and res["deduped"] == 0
    items = _ledger_items(tmp_path)
    it = items[_ganchor("m-acme", "send the deck",
                        source_snippet="Nikunj: I'll send the deck")]
    assert it["action_type"] == "follow_up"                   # the user owes it
    assert it["status"] == "open" and it["times_surfaced"] == 1
    assert it["deadline"] == "2026-07-04"
    assert "send the deck" in it["summary"] and "Acme sync" in it["summary"]


def test_rerun_dedupes_by_anchor_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    payload = {"commitments": [{"meeting": "Sync", "meeting_id": "m-sync", "owner": "you",
                                "owner_is_user": True, "what": "send deck",
                                "due": None, "to_email": "dana@acme.com"}]}
    ac.apply(payload, USER, NOW)
    res = ac.apply(payload, USER, NOW)
    assert res["written"] == 0 and res["deduped"] == 1
    items = _ledger_items(tmp_path)
    assert len(items) == 1
    assert items[_ganchor("m-sync", "send deck")]["times_surfaced"] == 2


def test_distinct_commitments_to_same_email_both_written(tmp_path, monkeypatch):
    # Regression: the bare contact anchor collapsed every commitment sharing a to_email onto ONE
    # key (follow_up + waiting_on share the follow_up family) and silently dropped the second.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    payload = {"commitments": [
        {"meeting": "Sync", "meeting_id": "m-sync", "owner": "you", "owner_is_user": True,
         "what": "send the deck", "due": None, "to_email": "dana@acme.com"},
        {"meeting": "Sync", "meeting_id": "m-sync", "owner": "Dana", "owner_is_user": False,
         "what": "send the contract", "due": None, "to_email": "dana@acme.com"}]}
    res = ac.apply(payload, USER, NOW)
    assert res["written"] == 2 and len(set(res["anchor_keys"])) == 2
    items = _ledger_items(tmp_path)
    assert len(items) == 2
    assert {it["action_type"] for it in items.values()} == {"follow_up", "waiting_on"}
    res2 = ac.apply(payload, USER, NOW)                       # re-applying the same payload dedupes
    assert res2["written"] == 0 and res2["deduped"] == 2
    assert res2["anchor_keys"] == res["anchor_keys"]
    assert len(_ledger_items(tmp_path)) == 2


def test_extractor_can_reconcile_commitment_with_an_existing_same_direction_loop(tmp_path, monkeypatch):
    # The LLM may suggest one existing anchor; code accepts it only when it is live and has the
    # same direction. This is the intentionally narrow semantic-dedupe boundary.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    cr.resolve({"today": "2026-07-02", "new_actions": [
        {"type": "follow_up", "channel": "gmail", "contactName": "Dana",
         "contactIdentifier": "dana@acme.com", "contextSummary": "follow up with Dana"}]}, NOW)
    res = ac.apply({"commitments": [{"meeting": "Sync", "meeting_id": "m-sync",
                                     "owner": "you", "owner_is_user": True, "what": "send deck",
                                     "existing_anchor_key": "follow_up:id:dana@acme.com",
                                     "source_snippet": "I'll send Dana the deck",
                                     "due": None, "to_email": "dana@acme.com"}]}, USER, NOW)
    assert res["written"] == 0 and res["deduped"] == 1
    items = _ledger_items(tmp_path)
    assert len(items) == 1
    assert items["follow_up:id:dana@acme.com"]["source_refs"][0]["source_id"] == "m-sync"
    assert items["follow_up:id:dana@acme.com"]["resolution_mode"] == "explicit"
    after_reply = cr.resolve({"today": "2026-07-03", "local": {"imessage": [
        {"is_from_me": True, "handle": "dana@acme.com", "timestamp": "2026-07-03 09:00:00",
         "text": "checking in"}]}}, datetime(2026, 7, 3, 10, 0, 0))
    assert after_reply["resolved"] == []
    assert [it["anchor_key"] for it in after_reply["active"]] == ["follow_up:id:dana@acme.com"]


def test_other_owner_becomes_waiting_on(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ac.apply({"commitments": [{"meeting": "Sync", "meeting_id": "m-sync", "owner": "Dana",
                               "owner_is_user": False, "what": "send the contract",
                               "due": None, "to_email": "dana@acme.com"}]}, USER, NOW)
    it = _ledger_items(tmp_path)[_ganchor("m-sync", "send the contract", False, True)]
    assert it["action_type"] == "waiting_on"
    assert it["contact_name"] == "Dana"
    assert "Dana owes" in it["summary"]


def test_no_recipient_gets_stable_synthetic_anchor(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    payload = {"commitments": [
        {"meeting": "Sync", "owner": "you", "what": "update the roadmap", "due": None, "to_email": None},
        {"meeting": "Sync", "owner": "you", "what": "book the offsite", "due": None, "to_email": None}]}
    res = ac.apply(payload, USER, NOW)
    assert res["written"] == 2                                # distinct commitments don't collapse
    assert all(k.startswith("thread:commitment:") for k in res["anchor_keys"])
    res2 = ac.apply(payload, USER, NOW)                       # …but re-runs dedupe exactly
    assert res2["written"] == 0 and res2["deduped"] == 2
    assert res2["anchor_keys"] == res["anchor_keys"]


def test_terminal_item_is_never_resurrected(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ac.apply({"commitments": [{"meeting": "Sync", "meeting_id": "m-sync", "owner": "you",
                               "owner_is_user": True, "what": "send deck",
                               "due": None, "to_email": "dana@acme.com"}]}, USER, NOW)
    key = _ganchor("m-sync", "send deck")
    items = cr._load_items()
    it = items[key]
    cr._terminate(it, "resolved", "replied", "2026-07-02")    # user handled it
    cr._persist(it)
    res = ac.apply({"commitments": [{"meeting": "Sync", "meeting_id": "m-sync", "owner": "you",
                                     "owner_is_user": True, "what": "send deck",
                                     "due": None, "to_email": "dana@acme.com"}]}, USER, NOW)
    assert res["written"] == 0 and res["skipped_terminal"] == 1
    assert _ledger_items(tmp_path)[key]["status"] == "resolved"


def test_fuzzy_due_stays_out_of_deadline(tmp_path, monkeypatch):
    # "Friday" must not become a deadline (continuity's expiry compares ISO date strings).
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ac.apply({"commitments": [{"meeting": "Sync", "meeting_id": "m-sync", "owner": "you",
                               "owner_is_user": True, "what": "intro to Alex",
                               "due": "Friday", "to_email": "alex@x.com"}]}, USER, NOW)
    it = _ledger_items(tmp_path)[_ganchor("m-sync", "intro to Alex")]
    assert it["deadline"] is None
    assert "due Friday" in it["summary"]


def test_empty_and_malformed_input_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert ac.apply({}, USER, NOW)["written"] == 0
    assert ac.apply({"commitments": [{"what": ""}, "junk", {"owner": "you"}]}, USER, NOW)["written"] == 0
    assert _ledger_items(tmp_path) == {}


def test_automatic_apply_rechecks_source_and_owner_before_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    meetings = [{"meeting_id": "m-sync", "transcript": "Dana: I'll send the deck.",
                 "attendee_emails": ["dana@acme.com"]}]
    res = ac.apply({"commitments": [
        {"meeting_id": "m-sync", "owner": "Dana", "owner_is_user": False,
         "what": "send the deck", "source_snippet": "Dana: I'll send the deck."},
        {"meeting_id": "m-sync", "owner": "you", "owner_is_user": True,
         "what": "send the deck", "source_snippet": "Dana: I'll send the deck."},
        {"meeting_id": "m-sync", "owner": "Dana", "owner_is_user": False,
         "what": "send the contract", "source_snippet": "Dana: I'll send the deck."},
        {"meeting_id": "m-sync", "owner": "Dana", "owner_is_user": False,
         "what": "wire money", "source_snippet": "Dana: I'll wire money."},
    ]}, USER, NOW, source_meetings=meetings)
    assert res["written"] == 1
    assert res["grounding"]["accepted"] == 1
    assert res["grounding"]["rejected"] == 3
    assert len(_ledger_items(tmp_path)) == 1


def test_ledger_items_surface_in_loops_query_shape(tmp_path, monkeypatch):
    # The written items are readable by the same resolver the brief runs (schema-compatible).
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ac.apply({"commitments": [{"meeting": "Sync", "meeting_id": "m-sync", "owner": "you",
                               "owner_is_user": True, "what": "send deck",
                               "due": None, "to_email": "dana@acme.com"}]}, USER, NOW)
    out = cr.resolve({"today": "2026-07-02"}, NOW)
    assert any(a["anchor_key"] == _ganchor("m-sync", "send deck") for a in out["active"])


def test_default_now_uses_user_timezone(tmp_path, monkeypatch):
    # created_at must be stamped in the USER's zone (the shared _now_local helper), not server
    # UTC: a 17:30 PT followup run is 00:30 tomorrow in UTC, skewing age_days/expiry vs every
    # other ledger writer.
    from datetime import timedelta, timezone as _tz
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "-07:00")
    seen = {}

    def fake_now_local(tz):
        seen["tz"] = tz
        return datetime(2026, 7, 1, 17, 30, 0, tzinfo=_tz(timedelta(hours=-7)))

    monkeypatch.setattr(ac.cr, "_now_local", fake_now_local)
    res = ac.apply({"commitments": [
        {"meeting": "Sync", "meeting_id": "m-sync", "owner": "you", "owner_is_user": True,
         "what": "send notes", "to_email": "dana@acme.com"}]}, USER)
    assert res["written"] == 1 and seen["tz"] == "-07:00"
    it = _ledger_items(tmp_path)[_ganchor("m-sync", "send notes")]
    assert it["created_at"] == "2026-07-01 17:30:00"  # local time, not UTC's next date


def test_same_words_in_a_later_meeting_are_a_new_occurrence(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    commitments = [{"meeting": "Weekly", "meeting_id": meeting_id, "owner": "you",
                    "owner_is_user": True, "what": "send the notes"}
                   for meeting_id in ("week-1", "week-2")]
    res = ac.apply({"commitments": commitments}, USER, NOW)
    assert res["written"] == 2
    assert len(set(res["anchor_keys"])) == 2


def test_same_source_snippet_dedupes_even_when_llm_paraphrases(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    base = {"meeting": "Weekly", "meeting_id": "week-1", "owner": "you",
            "owner_is_user": True, "source_snippet": "Nikunj: I'll send the deck tomorrow"}
    first = ac.apply({"commitments": [{**base, "what": "send the deck tomorrow"}]}, USER, NOW)
    second = ac.apply({"commitments": [{**base, "what": "share the deck with them by tomorrow"}]},
                      USER, NOW)
    assert first["anchor_keys"] == second["anchor_keys"]
    assert second["written"] == 0 and second["deduped"] == 1


def test_wrong_direction_or_invented_reconciliation_anchor_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    cr.resolve({"today": "2026-07-02", "new_actions": [
        {"type": "waiting_on", "channel": "gmail", "contactName": "Dana",
         "contactIdentifier": "dana@acme.com", "contextSummary": "Dana owes the contract"}]}, NOW)
    payload = {"commitments": [
        {"meeting": "Sync", "meeting_id": "m-wrong", "owner": "you", "owner_is_user": True,
         "what": "send deck", "existing_anchor_key": "waiting_on:id:dana@acme.com"},
        {"meeting": "Sync", "meeting_id": "m-invented", "owner": "you", "owner_is_user": True,
         "what": "send notes", "existing_anchor_key": "follow_up:id:nobody@example.com"}]}
    res = ac.apply(payload, USER, NOW)
    assert res["written"] == 2 and res["deduped"] == 0
    assert res["anchor_keys"] == [_ganchor("m-wrong", "send deck"),
                                  _ganchor("m-invented", "send notes")]


def test_explicit_owner_boolean_overrides_an_ambiguous_name(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    res = ac.apply({"commitments": [
        {"meeting_id": "m-me", "owner": "Nikunj Kothari", "owner_is_user": True,
         "what": "send the intro"},
        {"meeting_id": "m-other", "owner": "Nikunj Kothari", "owner_is_user": False,
         "what": "send the model"}]}, USER, NOW)
    items = _ledger_items(tmp_path)
    assert items[res["anchor_keys"][0]]["action_type"] == "follow_up"
    assert items[res["anchor_keys"][1]]["action_type"] == "waiting_on"
