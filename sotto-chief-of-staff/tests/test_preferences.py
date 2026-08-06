"""preferences.py — the explicit mute/tone channel, and its coexistence with the behavioral learner."""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pr = _load("preferences", "_shared/scripts/preferences.py")
lp = _load("learn_preferences", "approval-tiers/scripts/learn_preferences.py")


def test_add_remove_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pr.add("mute_people", "Bob Smith")
    pr.add("mute_sections", "birthdays")
    pr.add("tone_notes", "keep it terse")
    ex = pr.load_explicit()
    assert ex["mute_people"] == ["Bob Smith"]
    assert ex["mute_sections"] == ["birthdays"] and ex["tone_notes"] == ["keep it terse"]
    pr.add("mute_people", "Bob Smith")            # idempotent — no dupes
    assert pr.load_explicit()["mute_people"] == ["Bob Smith"]
    pr.remove("mute_people", "Bob Smith")
    assert pr.load_explicit()["mute_people"] == []


def test_mute_sender_is_lowercased(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pr.add("mute_senders", "News@Example.COM")
    assert pr.load_explicit()["mute_senders"] == ["news@example.com"]


def test_sender_is_muted_matching():
    muted = ["news@example.com", "@marketing.acme.com", "promo.shop.com"]
    assert pr.sender_is_muted("news@example.com", muted)            # exact
    assert pr.sender_is_muted("anything@marketing.acme.com", muted)  # @domain rule
    assert pr.sender_is_muted("x@promo.shop.com", muted)            # bare-domain rule
    assert pr.sender_is_muted("x@eu.promo.shop.com", muted)         # subdomain of a domain rule
    assert not pr.sender_is_muted("ceo@example.com", muted)         # different local-part, no domain rule
    assert not pr.sender_is_muted("", muted)


def test_load_explicit_shape_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ex = pr.load_explicit()
    assert ex == {"mute_senders": [], "mute_people": [], "mute_sections": [], "tone_notes": []}


def test_learner_preserves_explicit_block(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pr.add("mute_senders", "news@example.com")        # user states a preference…
    # …then a (separate) outcome stream drives the behavioral learner, which rewrites preferences.json
    (tmp_path / "outcomes.jsonl").write_text(
        json.dumps({"contact": "a", "action_type": "draft", "outcome": "executed"}) + "\n")
    lp.learn()
    data = json.load(open(os.path.join(str(tmp_path), "preferences.json")))
    assert "deprioritization_hints" in data                         # learner wrote its half
    assert data["explicit"]["mute_senders"] == ["news@example.com"]  # and did NOT wipe the explicit half
