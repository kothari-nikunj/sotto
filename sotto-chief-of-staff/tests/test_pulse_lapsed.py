"""relationship_pulse.py — longitudinal history + lapsed ("fully lost touch") detection."""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))

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
    assert out["history"]["Bob"]["last_contact"] == (NOW - timedelta(days=2)).strftime("%Y-%m-%d")


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
    assert state["history"]["Bob"]["interactions"] == 2           # current window snapshotted
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
    assert out["history"]["Bob"]["interactions"] == 20            # peak, not the window count of 1
    assert out["history"]["Bob"]["last_contact"] == (NOW - timedelta(days=2)).strftime("%Y-%m-%d")


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
