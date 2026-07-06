"""learn_preferences.py — approval_defaults emission, clamping, explicit-block safety, no-op runs."""
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))

LP_PATH = os.path.join(ROOT, "approval-tiers", "scripts", "learn_preferences.py")


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


lp = _load("learn_preferences_defaults", "approval-tiers/scripts/learn_preferences.py")
lo = _load("log_outcome_defaults", "_shared/scripts/log_outcome.py")


def _log(tmp_path, contact, action_type, outcome, tier=None, n=1):
    for _ in range(n):
        rec = {"contact": contact, "action_type": action_type, "outcome": outcome}
        if tier:
            rec["tier"] = tier
        lo.log(rec)


def test_emits_default_with_enough_signal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _log(tmp_path, "sarah", "reply", "executed", tier="one_tap", n=3)
    prefs = lp.learn()
    assert prefs["approval_defaults"] == {"sarah|reply": "one_tap"}
    # and it landed on disk
    on_disk = json.load(open(tmp_path / "preferences.json"))
    assert on_disk["approval_defaults"] == {"sarah|reply": "one_tap"}


def test_no_default_below_min_accepts(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _log(tmp_path, "sarah", "reply", "executed", tier="one_tap", n=2)
    assert lp.learn()["approval_defaults"] == {}


def test_no_default_below_accept_rate(tmp_path, monkeypatch):
    # 3 accepted, 2 dismissed → 60% acceptance < 80% → no default.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _log(tmp_path, "sarah", "reply", "executed", tier="one_tap", n=3)
    _log(tmp_path, "sarah", "reply", "dismissed", n=2)
    assert lp.learn()["approval_defaults"] == {}


def test_auto_is_clamped_and_forbidden_never_learned(tmp_path, monkeypatch):
    # Learned defaults may relax review→one_tap, but never grant `auto` and never emit `forbidden`.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _log(tmp_path, "sarah", "reply", "executed", tier="auto", n=3)
    _log(tmp_path, "evil", "wire_money", "executed", tier="forbidden", n=3)
    defaults = lp.learn()["approval_defaults"]
    assert defaults.get("sarah|reply") == "one_tap"          # clamped, not auto
    assert "evil|wire_money" not in defaults


def test_edited_and_sent_counts_as_accepted_and_key_is_exact(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _log(tmp_path, "dhruv", "follow_up", "edited_and_sent", tier="review", n=3)
    _log(tmp_path, "dhruv", "reply", "executed", tier="one_tap", n=1)   # different action_type — no default
    defaults = lp.learn()["approval_defaults"]
    assert defaults == {"dhruv|follow_up": "review"}         # per exact (contact, action_type)


def test_accepted_without_tier_yields_no_default(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _log(tmp_path, "sarah", "reply", "executed", n=3)        # no tier recorded on the outcomes
    assert lp.learn()["approval_defaults"] == {}


def test_explicit_block_never_touched(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    (tmp_path / "preferences.json").write_text(json.dumps(
        {"explicit": {"mute_senders": ["news@example.com"], "tone_notes": ["terse"]}}))
    _log(tmp_path, "sarah", "reply", "executed", tier="one_tap", n=3)
    prefs = lp.learn()
    assert prefs["explicit"]["mute_senders"] == ["news@example.com"]
    assert prefs["explicit"]["tone_notes"] == ["terse"]
    assert prefs["approval_defaults"] == {"sarah|reply": "one_tap"}


def test_corrupt_existing_prefs_abort_without_writing(tmp_path, monkeypatch):
    # A truncated/corrupt preferences.json must NOT be papered over by the wholesale rewriter —
    # that would silently drop the user's explicit block. The learner aborts, file byte-identical.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    truncated = '{"explicit": {"mute_senders": ["news@ex'          # torn write
    (tmp_path / "preferences.json").write_text(truncated)
    _log(tmp_path, "sarah", "reply", "executed", tier="one_tap", n=3)   # real rows → would write
    assert lp.learn() == {}
    assert (tmp_path / "preferences.json").read_text() == truncated    # untouched
    assert not os.path.exists(tmp_path / "preferences.json.tmp")


def test_write_is_atomic_no_tmp_left_behind(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _log(tmp_path, "sarah", "reply", "executed", tier="one_tap", n=3)
    lp.learn()
    assert json.load(open(tmp_path / "preferences.json"))["approval_defaults"]
    assert not os.path.exists(tmp_path / "preferences.json.tmp")   # tmp+os.replace, nothing torn


def test_missing_outcomes_is_noop(tmp_path, monkeypatch):
    # No outcomes.jsonl → learn() must NOT rewrite preferences.json (would wipe learned fields).
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    before = {"approval_defaults": {"sarah|reply": "one_tap"},
              "explicit": {"mute_senders": ["news@example.com"]}, "version": 1}
    (tmp_path / "preferences.json").write_text(json.dumps(before))
    prefs = lp.learn()
    assert prefs == before
    assert json.load(open(tmp_path / "preferences.json")) == before


def test_empty_outcomes_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    (tmp_path / "outcomes.jsonl").write_text("")
    assert lp.learn() == {}
    assert not os.path.exists(tmp_path / "preferences.json")   # nothing written from zero rows


def test_cli_exits_zero_with_no_data(tmp_path):
    # The brief's Learn step runs this unconditionally — it must be safe on a cold volume.
    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    proc = subprocess.run([sys.executable, LP_PATH], env=env, capture_output=True, text=True)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}
