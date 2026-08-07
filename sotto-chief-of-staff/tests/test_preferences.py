"""preferences.py — the explicit mute/tone channel, and its coexistence with the behavioral learner."""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


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
    assert ex == {"mute_senders": [], "mute_people": [], "mute_sections": [], "tone_notes": [],
                  "nudge_snooze_until": ""}


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


# ── Cadence: the nudge snooze the "quieter today" verbs write ─────────────────────────────────────

from datetime import datetime, timedelta   # noqa: E402

NOW = datetime(2026, 8, 6, 11, 0)          # a Thursday, 11:00 local


def test_snooze_specs_resolve_deterministically():
    """The clock math lives in the script, never in the agent's head — every verb in
    feedback/SKILL.md §C maps to one of these specs."""
    r = lambda s: pr.resolve_snooze_spec(s, now_local=NOW)          # noqa: E731
    assert r("tomorrow") == "2026-08-07T07:00"                      # "quieter today" — quiet-end
    assert r("+2h") == "2026-08-06T13:00" == r("2h")                # "quiet for 2 hours"
    assert r("90m") == "2026-08-06T12:30"
    assert r("15:00") == "2026-08-06T15:00" == r("3pm")             # "quiet until 3"
    assert r("3") == "2026-08-06T15:00"                             # bare "3" from an awake person
    assert r("9am") == "2026-08-07T09:00"                           # already past → tomorrow
    assert r("2026-08-08T06:30") == "2026-08-08T06:30"              # explicit stamp passes through
    assert r("2026-08-08") == "2026-08-08T00:00"                    # a bare date = its midnight
    for off in ("", "off", "clear", "none", "normal", "back to normal"):
        assert r(off) == ""                                          # "back to normal"
    for bad in ("whenever", "25:00", "next fortnight"):
        try:
            r(bad); assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_tomorrow_snooze_lifts_exactly_when_quiet_hours_do(monkeypatch):
    """A snooze lifts when quiet hours do. "Quieter today" resolves to SOTTO_QUIET_END — the same
    env var (and the same default 7) the event funnel's quiet-hours rule reads — so the stamp Sotto
    reports back is the hour nudges actually resume, on any box."""
    monkeypatch.delenv("SOTTO_QUIET_END", raising=False)
    assert pr._snooze_morning_hour() == 7                            # the funnel's default
    assert pr.resolve_snooze_spec("tomorrow", now_local=NOW) == "2026-08-07T07:00"
    monkeypatch.setenv("SOTTO_QUIET_END", "6")                       # an early riser retunes it…
    assert pr.resolve_snooze_spec("quieter today", now_local=NOW) == "2026-08-07T06:00"
    for junk in ("", "  ", "nope", "24", "-1"):                      # …garbage falls back, never 0
        monkeypatch.setenv("SOTTO_QUIET_END", junk)
        assert pr.resolve_snooze_spec("rest of the day", now_local=NOW) == "2026-08-07T07:00"


def test_snooze_active_is_a_future_check_that_fails_open():
    ex = {"nudge_snooze_until": "2026-08-06T15:00"}
    assert pr.snooze_active(now_local=NOW, explicit=ex)
    assert not pr.snooze_active(now_local=NOW + timedelta(hours=5), explicit=ex)
    assert not pr.snooze_active(now_local=NOW, explicit={"nudge_snooze_until": ""})
    assert not pr.snooze_active(now_local=NOW, explicit={"nudge_snooze_until": "garbage"})
    assert not pr.snooze_active(now_local=NOW, explicit={})


def test_snooze_verbs_write_and_clear_through_the_cli(tmp_path, monkeypatch, capsys):
    """The exact commands feedback/SKILL.md tells the agent to run, end to end on disk."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    pr.add("mute_people", "Bob Smith")                               # a mute must survive intact

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["preferences.py", *argv])
        pr.main()
        return json.loads(capsys.readouterr().out)

    out = run("snooze-nudges", "tomorrow")
    until = out["nudge_snooze_until"]
    assert until.endswith("T07:00") and until > datetime.now().strftime("%Y-%m-%dT%H:%M")
    stored = json.load(open(os.path.join(str(tmp_path), "preferences.json")))
    assert stored["explicit"]["nudge_snooze_until"] == until         # same explicit block, one file
    assert stored["explicit"]["mute_people"] == ["Bob Smith"]
    assert pr.snooze_active()                                        # …and the funnel reads it live

    assert run("snooze-nudges", "+2h")["nudge_snooze_until"] != until   # "quiet for 2 hours"
    cleared = run("unsnooze-nudges")
    assert cleared["nudge_snooze_until"] == "" and cleared["mute_people"] == ["Bob Smith"]
    assert not pr.snooze_active()                                    # "back to normal"

    monkeypatch.setattr(sys, "argv", ["preferences.py", "snooze-nudges", "whenever"])
    try:
        pr.main(); assert False, "an unparseable spec must not silently write"
    except SystemExit as e:
        assert e.code == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_learner_preserves_the_snooze_scalar(tmp_path, monkeypatch):
    """The scalar rides in the same reserved block the behavioral learner must never wipe."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pr.set_scalar("nudge_snooze_until", "2026-08-08T06:00")
    (tmp_path / "outcomes.jsonl").write_text(
        json.dumps({"contact": "a", "action_type": "draft", "outcome": "executed"}) + "\n")
    lp.learn()
    data = json.load(open(os.path.join(str(tmp_path), "preferences.json")))
    assert data["explicit"]["nudge_snooze_until"] == "2026-08-08T06:00"
