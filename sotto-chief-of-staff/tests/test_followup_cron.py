"""followup_cron.py — since-last-run windowing, marker round-trip, and the silent-when-empty gate."""
import importlib.util, json, os, sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
spec = importlib.util.spec_from_file_location(
    "fc", os.path.join(ROOT, "followup", "scripts", "followup_cron.py"))
fc = importlib.util.module_from_spec(spec); spec.loader.exec_module(fc)

NOW = datetime(2026, 7, 6, 16, 45, tzinfo=timezone.utc)


def test_window_first_run_uses_default(monkeypatch):
    monkeypatch.delenv("SOTTO_FOLLOWUP_DEFAULT_HOURS", raising=False)
    assert fc.window_hours(NOW, None) == 36                 # never run → bootstrap window
    monkeypatch.setenv("SOTTO_FOLLOWUP_DEFAULT_HOURS", "24")
    assert fc.window_hours(NOW, None) == 24


def test_window_since_last_run_is_the_gap(monkeypatch):
    monkeypatch.delenv("SOTTO_FOLLOWUP_MIN_HOURS", raising=False)
    monkeypatch.delenv("SOTTO_FOLLOWUP_MAX_HOURS", raising=False)
    last = NOW - timedelta(hours=23, minutes=30)
    assert fc.window_hours(NOW, last) == 24                 # ceil(23.5h) → 24, only since last run
    assert fc.window_hours(NOW, NOW - timedelta(hours=1)) == 1


def test_window_clamps_to_max_after_long_outage(monkeypatch):
    monkeypatch.delenv("SOTTO_FOLLOWUP_MAX_HOURS", raising=False)
    last = NOW - timedelta(days=10)
    assert fc.window_hours(NOW, last) == 72                 # a 10-day gap can't scan weeks of transcripts
    monkeypatch.setenv("SOTTO_FOLLOWUP_MAX_HOURS", "48")
    assert fc.window_hours(NOW, last) == 48


def test_window_clamps_to_min_on_zero_or_skew(monkeypatch):
    monkeypatch.delenv("SOTTO_FOLLOWUP_MIN_HOURS", raising=False)
    assert fc.window_hours(NOW, NOW) == 1                   # ran seconds ago → floor at min
    assert fc.window_hours(NOW, NOW + timedelta(hours=3)) == 1  # last in the future (clock skew) → min


def test_marker_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert fc.read_last_cron() is None                     # nothing persisted yet
    fc.write_last_cron(NOW)
    got = fc.read_last_cron()
    assert got == NOW
    assert os.path.exists(os.path.join(str(tmp_path), "followup", "last_cron"))
    # and the persisted marker actually drives the next window
    later = NOW + timedelta(hours=25)
    assert fc.window_hours(later, fc.read_last_cron()) == 25


def test_marker_missing_and_garbage_are_none(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    p = fc.marker_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write("not-a-timestamp")
    assert fc.read_last_cron() is None                     # unparseable → treated as never-run


def test_is_silent_when_no_commitments_and_no_drafts():
    assert fc.is_silent({"followup_markdown": "Nothing to follow up on.", "commitments": [], "drafts": []})
    assert fc.is_silent({}) and fc.is_silent(None)         # missing/None → silent
    # anything actionable → deliver
    assert not fc.is_silent({"commitments": [{"what": "send deck"}], "drafts": []})
    assert not fc.is_silent({"commitments": [], "drafts": [{"to_email": "a@b.com"}]})


def _silent_check(path, capsys, monkeypatch):
    """Drive the --silent-check CLI path and return the single token it prints."""
    monkeypatch.setattr(sys, "argv", ["followup_cron.py", "--silent-check", str(path)])
    fc.main()
    return capsys.readouterr().out.strip()


def test_silent_check_missing_file_is_error(tmp_path, capsys, monkeypatch):
    # compose_followup never produced output → distinct 'error' (NOT 'silent') so the skill won't stamp
    # the marker and the window is re-covered next run.
    assert _silent_check(tmp_path / "nope.json", capsys, monkeypatch) == "error"


def test_silent_check_corrupt_json_is_error(tmp_path, capsys, monkeypatch):
    bad = tmp_path / "out.json"
    bad.write_text("{not valid json")
    assert _silent_check(bad, capsys, monkeypatch) == "error"


def test_silent_check_valid_empty_is_silent(tmp_path, capsys, monkeypatch):
    # a VALID run with nothing actionable is 'silent' (say nothing, but DO stamp) — not 'error'
    empty = tmp_path / "out.json"
    empty.write_text(json.dumps({"followup_markdown": "Nothing to follow up on.",
                                 "commitments": [], "drafts": []}))
    assert _silent_check(empty, capsys, monkeypatch) == "silent"


def test_silent_check_actionable_is_deliver(tmp_path, capsys, monkeypatch):
    out = tmp_path / "out.json"
    out.write_text(json.dumps({"commitments": [{"what": "send deck"}], "drafts": []}))
    assert _silent_check(out, capsys, monkeypatch) == "deliver"
