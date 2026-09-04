"""relationship_pulse.py — longitudinal history + lapsed ("fully lost touch") detection."""
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location(
    "relationship_pulse_lapsed", os.path.join(ROOT, "relationship-pulse", "scripts", "relationship_pulse.py"))
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


def _msg(name, days_ago, from_me):
    ts = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return {"handle": "+1", "resolved_name": name, "is_from_me": from_me,
            "is_group_chat": False, "timestamp": ts, "text": "hi"}


def _local(*msgs):
    names = {m["resolved_name"] for m in msgs}
    return {"contacts": [{"name": n, "phones": ["+1"]} for n in names], "imessage": list(msgs)}


def _hist(out, name):
    """History is identity-keyed now (cid when the resolver has one, name as fallback) — find a
    person's entry by the display name it carries."""
    for key, h in out["history"].items():
        if key == name or (isinstance(h, dict) and h.get("name") == name):
            return h
    raise AssertionError(f"no history entry for {name}: {sorted(out['history'])}")


def test_email_is_a_relationship_channel_for_people_you_know(tmp_path, monkeypatch):
    """A mail you received is a touch from its sender; a mail you sent is a touch to each recipient
    — for people in your Contacts or graph only. Before this the pulse could not see the half of a
    relationship that lives in mail, and a no-Mac deploy said "healthy" every Monday."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    people = tmp_path / "knowledge" / "people"
    people.mkdir(parents=True)
    (people / "priya-raman.md").write_text(
        "---\nschema: 2\ncanonical_id: c_priya\nname: Priya Raman\nidentifiers:\n  - priya@acme.com\n---\n")
    d = lambda days: (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    local = {"contacts": [{"name": "Dana Roe", "emails": ["dana@acme.com"], "phones": []}],
             "emails": [
                 {"from": "Dana Roe <dana@acme.com>", "to": "me@example.com", "date": d(5), "subject": "Q"},
                 {"from": "Me <me@example.com>", "to": "Dana Roe <dana@acme.com>, priya@acme.com",
                  "date": d(9), "isSent": True},
                 {"from": "Priya Raman <priya@acme.com>", "to": "me@example.com", "date": d(4)},
                 {"from": "Cold Pitch <cold@pitch.io>", "to": "me@example.com", "date": d(3)},
                 {"from": "noreply@acme.com", "to": "me@example.com", "date": d(2)}]}
    people_seen = rp._interactions_by_contact(rp.resolve_contact_names(local))
    assert {p["name"] for p in people_seen.values()} == {"Dana Roe", "Priya Raman"}
    dana = next(p for p in people_seen.values() if p["name"] == "Dana Roe")
    assert len(dana["from_them"]) == 1 and len(dana["from_me"]) == 1 and "email" in dana["by_channel"]
    priya = next(p for p in people_seen.values() if p["name"] == "Priya Raman")
    assert priya["cid"] == "c_priya"                       # graph identity, so one row per human
    out = rp.compute(local, NOW)
    assert {q["display_name"] for q in out["attention_queue"] if q["queue_type"] == "waiting_on_you"} \
        == {"Dana Roe", "Priya Raman"}                     # they wrote last, 4–5 days ago


def test_no_history_degrades_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    out = rp.compute(_local(_msg("Bob", 1, True)), NOW)          # no history arg at all
    assert out["lapsed"] == []
    assert "lost touch" not in out["pulse_markdown"].lower()


def test_previously_regular_absent_contact_is_lapsed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    history = {"Maya": {"last_contact": "2026-05-01", "interactions": 12, "trend": "stable"}}
    out = rp.compute(_local(_msg("Bob", 1, True)), NOW, history=history)
    assert len(out["lapsed"]) == 1
    e = out["lapsed"][0]
    assert e["display_name"] == "Maya" and e["queue_type"] == "lapsed"
    assert e["last_contact"] == "2026-05-01"                      # last-known date surfaces
    assert "2026-05-01" in e["reason"]
    assert "lost touch" in out["pulse_markdown"].lower()
    assert "Maya" in out["pulse_markdown"]


def test_contact_in_current_window_is_not_lapsed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    history = {"Bob": {"last_contact": "2026-05-01", "interactions": 12, "trend": "stable"}}
    out = rp.compute(_local(_msg("Bob", 2, False)), NOW, history=history)
    assert out["lapsed"] == []
    assert _hist(out, "Bob")["last_contact"] == (NOW - timedelta(days=2)).strftime("%Y-%m-%d")


def test_one_off_past_contact_is_not_lapsed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    history = {"Stranger": {"last_contact": "2026-05-01", "interactions": 2, "trend": "stable"}}
    out = rp.compute(_local(_msg("Bob", 1, True)), NOW, history=history)
    assert out["lapsed"] == []                                    # below the "previously regular" bar


def test_lapsed_ranked_below_losing_touch(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    msgs = [_msg("Sarah", d, d % 2 == 0) for d in [60, 58, 56, 54, 52, 50, 48, 46]]
    msgs.append(_msg("Sarah", 20, False))                         # Sarah = losing_touch
    history = {"Maya": {"last_contact": "2026-04-01", "interactions": 30, "trend": "stable"}}
    out = rp.compute(_local(*msgs), NOW, history=history)
    kinds = [q["queue_type"] for q in out["attention_queue"]]
    assert "losing_touch" in kinds and "lapsed" in kinds
    assert kinds.index("lapsed") > kinds.index("losing_touch")    # lapsed always after
    md = out["pulse_markdown"]
    assert md.index("Going quiet") < md.index("Fully lost touch")


def test_lapsed_carries_graph_context_when_tracked(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    people = os.path.join(str(tmp_path), "knowledge", "people")
    os.makedirs(people, exist_ok=True)
    with open(os.path.join(people, "maya.md"), "w") as f:
        f.write("---\nschema: 1\ncanonical_id: c_m\nname: Maya\ncompany: Acme\nfacts: {}\n---\n")
    history = {"Maya": {"last_contact": "2026-05-01", "interactions": 12, "trend": "stable"}}
    out = rp.compute(_local(_msg("Bob", 1, True)), NOW, history=history)
    assert out["lapsed"][0]["graph_context"]["company"] == "Acme"
    assert "(Acme)" in out["pulse_markdown"]                      # grounded hook shown, not invented


def test_history_snapshot_written_and_carried_forward(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    history = {"Maya": {"last_contact": "2026-05-01", "interactions": 12, "trend": "stable"},
               "Ancient": {"last_contact": "2024-01-01", "interactions": 40, "trend": "stable"}}
    out = rp.compute(_local(_msg("Bob", 1, True), _msg("Bob", 3, False)), NOW, history=history)
    rp._persist_state(out)
    state = json.load(open(os.path.join(str(tmp_path), "knowledge", "relationship_state.json")))
    assert next(h for h in state["history"].values() if h.get("name") == "Bob")["interactions"] == 2           # current window snapshotted
    assert "Maya" in state["history"]                             # absent contact carried forward…
    assert "Ancient" not in state["history"]                      # …but >1y silence is pruned
    # a lapsed entry is in the persisted attention_queue so the daily brief can surface it too
    assert any(q["queue_type"] == "lapsed" for q in state["attention_queue"])
    # and _load_history round-trips for the next run
    assert rp._load_history()["Maya"]["last_contact"] == "2026-05-01"


def test_load_history_missing_or_corrupt(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert rp._load_history() == {}
    os.makedirs(os.path.join(str(tmp_path), "knowledge"), exist_ok=True)
    with open(os.path.join(str(tmp_path), "knowledge", "relationship_state.json"), "w") as f:
        f.write("{not json")
    assert rp._load_history() == {}


def test_empty_window_emits_no_lapsed_and_preserves_prior_state(tmp_path, monkeypatch):
    # A degraded/empty read (Bridge offline) must NOT mark everyone "fully lost touch" nor
    # overwrite the longitudinal state with an empty snapshot.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    history = {"Maya": {"last_contact": "2026-06-30", "interactions": 12, "trend": "stable"}}
    # seed prior state on disk
    prior = rp.compute(_local(_msg("Maya", 2, False)), NOW, history=history)
    rp._persist_state(prior)
    before = open(os.path.join(str(tmp_path), "knowledge", "relationship_state.json")).read()
    out = rp.compute({}, NOW, history=history)                    # empty window
    assert out["lapsed"] == [] and out["attention_queue"] == []
    assert out.get("degraded") is True
    assert "lost touch" not in out["pulse_markdown"].lower()
    rp._persist_state(out)                                        # must be a no-op
    after = open(os.path.join(str(tmp_path), "knowledge", "relationship_state.json")).read()
    assert after == before                                        # prior state kept


def test_present_contact_keeps_peak_interactions(tmp_path, monkeypatch):
    # A 20-interaction regular who sends ONE ping this window must not reset to 1 — they'd never
    # clear the lapsed >= LAPSED_MIN_INTERACTIONS gate again. The merge keeps the peak.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    history = {"Bob": {"last_contact": "2026-05-01", "interactions": 20, "trend": "stable"}}
    out = rp.compute(_local(_msg("Bob", 2, False)), NOW, history=history)
    assert _hist(out, "Bob")["interactions"] == 20            # peak, not the window count of 1
    assert _hist(out, "Bob")["last_contact"] == (NOW - timedelta(days=2)).strftime("%Y-%m-%d")


def test_history_entry_without_last_contact_is_dropped(tmp_path, monkeypatch):
    # Zombie guard: an absent entry whose last_contact is missing/unparseable would skip the >365d
    # prune and resurface forever — it must be dropped during the merge.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    history = {"NoDate": {"interactions": 12, "trend": "stable"},
               "BadDate": {"last_contact": "not-a-date", "interactions": 12, "trend": "stable"},
               "Maya": {"last_contact": "2026-05-01", "interactions": 12, "trend": "stable"}}
    out = rp.compute(_local(_msg("Bob", 1, True)), NOW, history=history)
    assert "NoDate" not in out["history"] and "BadDate" not in out["history"]
    assert "Maya" in out["history"]                               # parseable entries carry forward


def test_persist_state_atomic_no_tmp_left_behind(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    out = rp.compute(_local(_msg("Bob", 1, True)), NOW)
    rp._persist_state(out)
    path = os.path.join(str(tmp_path), "knowledge", "relationship_state.json")
    assert json.load(open(path))["attention_queue"] == []
    assert not os.path.exists(path + ".tmp")                      # tmp + os.replace, nothing torn


def test_healthy_message_suppressed_when_only_lapsed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    history = {"Maya": {"last_contact": "2026-05-01", "interactions": 12, "trend": "stable"}}
    out = rp.compute(_local(_msg("Bob", 1, True)), NOW, history=history)
    assert "healthy" not in out["pulse_markdown"].lower()


def test_history_survives_a_contact_rename_and_migrates_off_name_keys(tmp_path, monkeypatch):
    """The re-key's whole point (memory-moat audit F3): history keys by canonical_id, so a
    Contacts rename no longer resets a person's longitudinal record — and a pre-identity entry
    keyed by display name folds into the cid entry via the peak-merge instead of ghosting."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    # The person has a GRAPH identity — the register the seeds adopt (Tier 2's adoption rule).
    gidx = [{"canonical_id": "c_bobstable001", "display_name": "Bob",
             "identifiers": ["+1"], "confidence": "medium"}]
    history = {"Bob": {"last_contact": "2026-05-01", "interactions": 20, "trend": "stable"}}
    local = _local(_msg("Bob", 2, False))
    local["contact_index"] = gidx
    out = rp.compute(local, NOW, history=history)
    entry = _hist(out, "Bob")
    assert entry["interactions"] == 20                    # peak carried across the key migration
    assert "Bob" not in out["history"]                    # the old name key is gone, not doubled
    assert out["history"]["c_bobstable001"]["name"] == "Bob"   # keyed by the graph's id
    # The rename: same contact card (same phone → same graph id), new display name.
    local2 = _local(_msg("Bobby", 1, False))
    local2["contact_index"] = gidx
    out2 = rp.compute(local2, NOW, history=out["history"])
    assert _hist(out2, "Bobby")["interactions"] == 20     # history followed the HUMAN, not the name


def test_history_records_last_contact_per_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ts_call = (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    local = _local(_msg("Bob", 3, False))
    local["calls"] = [{"phone": "+1", "timestamp": ts_call, "is_outgoing": True,
                       "is_answered": True, "call_type": "phone"}]
    out = rp.compute(local, NOW)
    ch = _hist(out, "Bob")["channels"]
    assert ch["imessage"] == (NOW - timedelta(days=3)).strftime("%Y-%m-%d")
    assert ch["calls"] == (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
