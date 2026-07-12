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
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ac = _load("apply_commitments", "followup/scripts/apply_commitments.py")
cr = _load("continuity_resolve_fl", "morning-brief/scripts/continuity_resolve.py")

NOW = datetime(2026, 7, 2, 10, 0, 0)
USER = "me@x.com"


def _canchor(email, owner, what):
    """The content-aware anchor for a recipient commitment: contact anchor + sha(owner|what)
    suffix, so distinct commitments to the same person never collapse."""
    return f"email:follow_up:id:{email}:c:" + hashlib.sha256(f"{owner}|{what}".encode()).hexdigest()[:12]


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
        {"meeting": "Acme sync", "owner": "you", "what": "send the deck",
         "due": "2026-07-04", "to_email": "dana@acme.com"}]}, USER, NOW)
    assert res["written"] == 1 and res["deduped"] == 0
    items = _ledger_items(tmp_path)
    # contact anchor + content hash, so distinct commitments to the same person never collapse
    it = items[_canchor("dana@acme.com", "you", "send the deck")]
    assert it["action_type"] == "follow_up"                   # the user owes it
    assert it["status"] == "open" and it["times_surfaced"] == 1
    assert it["deadline"] == "2026-07-04"
    assert "send the deck" in it["summary"] and "Acme sync" in it["summary"]


def test_rerun_dedupes_by_anchor_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    payload = {"commitments": [{"meeting": "Sync", "owner": "you", "what": "send deck",
                                "due": None, "to_email": "dana@acme.com"}]}
    ac.apply(payload, USER, NOW)
    res = ac.apply(payload, USER, NOW)
    assert res["written"] == 0 and res["deduped"] == 1
    items = _ledger_items(tmp_path)
    assert len(items) == 1
    assert items[_canchor("dana@acme.com", "you", "send deck")]["times_surfaced"] == 2


def test_distinct_commitments_to_same_email_both_written(tmp_path, monkeypatch):
    # Regression: the bare contact anchor collapsed every commitment sharing a to_email onto ONE
    # key (follow_up + waiting_on share the follow_up family) and silently dropped the second.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    payload = {"commitments": [
        {"meeting": "Sync", "owner": "you", "what": "send the deck", "due": None, "to_email": "dana@acme.com"},
        {"meeting": "Sync", "owner": "Dana", "what": "send the contract", "due": None, "to_email": "dana@acme.com"}]}
    res = ac.apply(payload, USER, NOW)
    assert res["written"] == 2 and len(set(res["anchor_keys"])) == 2
    items = _ledger_items(tmp_path)
    assert len(items) == 2
    assert {it["action_type"] for it in items.values()} == {"follow_up", "waiting_on"}
    res2 = ac.apply(payload, USER, NOW)                       # re-applying the same payload dedupes
    assert res2["written"] == 0 and res2["deduped"] == 2
    assert res2["anchor_keys"] == res["anchor_keys"]
    assert len(_ledger_items(tmp_path)) == 2


def test_commitment_does_not_merge_with_brief_created_loop(tmp_path, monkeypatch):
    # INTENTIONAL trade (see apply_commitments.py): the content-hash anchor no longer merges with a
    # brief-created `email:follow_up:id:<email>` loop — never losing a distinct commitment wins over
    # cross-source dedupe. Both items coexist; re-applying the commitment still dedupes against itself.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    cr.resolve({"today": "2026-07-02", "new_actions": [
        {"type": "follow_up", "channel": "gmail", "contactName": "Dana",
         "contactIdentifier": "dana@acme.com", "contextSummary": "follow up with Dana"}]}, NOW)
    res = ac.apply({"commitments": [{"meeting": "Sync", "owner": "you", "what": "send deck",
                                     "due": None, "to_email": "dana@acme.com"}]}, USER, NOW)
    assert res["written"] == 1 and res["deduped"] == 0
    items = _ledger_items(tmp_path)
    assert len(items) == 2
    assert "email:follow_up:id:dana@acme.com" in items                        # the brief's loop
    assert _canchor("dana@acme.com", "you", "send deck") in items             # the commitment


def test_other_owner_becomes_waiting_on(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ac.apply({"commitments": [{"meeting": "Sync", "owner": "Dana", "what": "send the contract",
                               "due": None, "to_email": "dana@acme.com"}]}, USER, NOW)
    it = _ledger_items(tmp_path)[_canchor("dana@acme.com", "Dana", "send the contract")]
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
    ac.apply({"commitments": [{"meeting": "Sync", "owner": "you", "what": "send deck",
                               "due": None, "to_email": "dana@acme.com"}]}, USER, NOW)
    key = _canchor("dana@acme.com", "you", "send deck")
    items = cr._load_items()
    it = items[key]
    cr._terminate(it, "resolved", "replied", "2026-07-02")    # user handled it
    cr._persist(it)
    res = ac.apply({"commitments": [{"meeting": "Sync", "owner": "you", "what": "send deck",
                                     "due": None, "to_email": "dana@acme.com"}]}, USER, NOW)
    assert res["written"] == 0 and res["skipped_terminal"] == 1
    assert _ledger_items(tmp_path)[key]["status"] == "resolved"


def test_fuzzy_due_stays_out_of_deadline(tmp_path, monkeypatch):
    # "Friday" must not become a deadline (continuity's expiry compares ISO date strings).
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ac.apply({"commitments": [{"meeting": "Sync", "owner": "you", "what": "intro to Alex",
                               "due": "Friday", "to_email": "alex@x.com"}]}, USER, NOW)
    it = _ledger_items(tmp_path)[_canchor("alex@x.com", "you", "intro to Alex")]
    assert it["deadline"] is None
    assert "due Friday" in it["summary"]


def test_empty_and_malformed_input_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert ac.apply({}, USER, NOW)["written"] == 0
    assert ac.apply({"commitments": [{"what": ""}, "junk", {"owner": "you"}]}, USER, NOW)["written"] == 0
    assert _ledger_items(tmp_path) == {}


def test_ledger_items_surface_in_loops_query_shape(tmp_path, monkeypatch):
    # The written items are readable by the same resolver the brief runs (schema-compatible).
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ac.apply({"commitments": [{"meeting": "Sync", "owner": "you", "what": "send deck",
                               "due": None, "to_email": "dana@acme.com"}]}, USER, NOW)
    out = cr.resolve({"today": "2026-07-02"}, NOW)
    assert any(a["anchor_key"] == _canchor("dana@acme.com", "you", "send deck") for a in out["active"])
