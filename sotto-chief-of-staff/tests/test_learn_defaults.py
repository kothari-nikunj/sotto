"""learn_preferences.py — approval_defaults emission, clamping, explicit-block safety, no-op runs."""
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

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


# ── The never-relax guard: a decline is `review` FOREVER ─────────────────────────────────────────
# Sending an unread "no" is the one outcome you cannot walk back, so the learned review→one_tap
# relaxation must never reach a decline — no matter how much the user accepts them.

def test_decline_never_relaxes_however_many_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _log(tmp_path, "sarah", "decline", "executed", tier="one_tap", n=10)   # 10 clean accepts
    defaults = lp.learn()["approval_defaults"]
    assert defaults["sarah|decline"] == "review"          # pinned, not relaxed
    assert defaults["sarah|decline"] != "one_tap"


def test_decline_pin_survives_the_auto_clamp(tmp_path, monkeypatch):
    # "auto" normally clamps to one_tap; on a decline it must land on review, not one_tap.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _log(tmp_path, "sarah", "decline", "executed", tier="auto", n=5)
    assert lp.learn()["approval_defaults"]["sarah|decline"] == "review"


def test_never_relax_matches_decline_variants_per_word(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    for at in ("decline_reply", "reply_decline", "decline-email", "Decline"):
        _log(tmp_path, "sarah", at, "edited_and_sent", tier="one_tap", n=3)
    defaults = lp.learn()["approval_defaults"]
    for at in ("decline_reply", "reply_decline", "decline-email", "Decline"):
        assert defaults[f"sarah|{at}"] == "review", at


def test_never_relax_does_not_leak_to_other_action_types(tmp_path, monkeypatch):
    # The guard is narrow: an ordinary reply to the same contact still relaxes as before.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _log(tmp_path, "sarah", "reply", "executed", tier="one_tap", n=3)
    _log(tmp_path, "sarah", "decline", "executed", tier="one_tap", n=3)
    defaults = lp.learn()["approval_defaults"]
    assert defaults["sarah|reply"] == "one_tap"
    assert defaults["sarah|decline"] == "review"


def test_declines_are_never_relaxed_for_any_contact(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    for contact in ("sarah", "dhruv", "unknown@example.com"):
        _log(tmp_path, contact, "decline", "executed", tier="one_tap", n=4)
    assert set(lp.learn()["approval_defaults"].values()) == {"review"}


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


# ── "A rule you delete stays deleted" (the dashboard's /api/prefs tombstones) ────────────────────
# The three behavioral lists are rebuilt from scratch on every run, so without this a rule the user
# deleted in the dashboard came back the next morning. The dashboard records each delete in the
# top-level `suppressed` list; this learner filters those values out and carries the list forward.

def test_suppressed_values_never_come_back(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _log(tmp_path, "sarah", "reply", "executed", tier="one_tap", n=3)   # → approval_defaults
    _log(tmp_path, "noise", "reply", "dismissed", n=4)                  # → deprioritization_hints
    _log(tmp_path, "chris", "reply", "edited_and_sent", tier="review", n=3)   # → edit_heavy
    learned = lp.learn()
    assert learned["approval_defaults"]["sarah|reply"] == "one_tap"
    assert "noise|reply" in learned["deprioritization_hints"]
    assert "chris|reply" in learned["edit_heavy"]

    # the user deletes all three in the dashboard → tombstones land in preferences.json
    prefs = json.load(open(tmp_path / "preferences.json"))
    prefs["suppressed"] = [{"list": "approval_defaults", "value": "sarah|reply"},
                           {"list": "deprioritization_hints", "value": "noise|reply"},
                           {"list": "edit_heavy", "value": "chris|reply"}]
    (tmp_path / "preferences.json").write_text(json.dumps(prefs))

    again = lp.learn()          # the SAME outcomes, re-learned
    assert "sarah|reply" not in again["approval_defaults"]
    assert again["deprioritization_hints"] == []
    assert again["edit_heavy"] == []
    assert again["suppressed"] == prefs["suppressed"]        # carried forward, not consumed
    assert json.load(open(tmp_path / "preferences.json"))["suppressed"] == prefs["suppressed"]


def test_suppression_is_per_list_and_tolerates_junk(tmp_path, monkeypatch):
    """A tombstone only silences the list it names, and a malformed entry is ignored rather than
    aborting the Learn run."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _log(tmp_path, "sarah", "reply", "executed", tier="one_tap", n=3)
    _log(tmp_path, "sarah", "reply", "dismissed", n=0)
    (tmp_path / "preferences.json").write_text(json.dumps({
        "suppressed": [{"list": "deprioritization_hints", "value": "sarah|reply"},
                       "not-a-dict", {"value": "no list"}, {"list": "edit_heavy"}]}))
    prefs = lp.learn()
    assert prefs["approval_defaults"] == {"sarah|reply": "one_tap"}   # a different list — untouched


def test_explicit_block_still_survives_alongside_suppressed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _log(tmp_path, "sarah", "reply", "executed", tier="one_tap", n=3)
    (tmp_path / "preferences.json").write_text(json.dumps({
        "explicit": {"mute_senders": ["news@example.com"]},
        "suppressed": [{"list": "approval_defaults", "value": "sarah|reply"}]}))
    prefs = lp.learn()
    assert prefs["explicit"] == {"mute_senders": ["news@example.com"]}
    assert prefs["approval_defaults"] == {}
