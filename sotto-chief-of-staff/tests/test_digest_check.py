"""digest_check.py — the adaptive midday digest gate: threshold, dedup/order/cap, stamp window."""
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location(
    "dc", os.path.join(ROOT, "event-triage", "scripts", "digest_check.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

NOW = datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc)


def _entry(i, sender="Sarah Chen", cls="ambient", minutes_ago=0, text=None):
    ts = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ts": ts, "verdict_class": cls, "sender": sender,
            "event": {"source": "imessage", "rowid": i, "handle": "+14155551234",
                      "text": text or f"message {i}"}}


def _write_queue(tmp_path, entries):
    d = tmp_path / "events"
    d.mkdir(parents=True, exist_ok=True)
    (d / "queue.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_below_threshold_stays_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_DIGEST_MIN", raising=False)
    _write_queue(tmp_path, [_entry(i, sender=f"P{i}") for i in range(7)])   # 7 < default 8
    out = dc.check(dc.entries_since(dc.read_stamp()))
    assert out == {"deliver": False}


def test_threshold_delivers_with_dedup_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_DIGEST_MIN", raising=False)
    entries = [
        _entry(1, sender="Sarah Chen", minutes_ago=50, text="old sarah"),
        _entry(2, sender="Dhruv", minutes_ago=40),
        _entry(3, sender="Sarah Chen", minutes_ago=30, text="new sarah"),
        _entry(4, sender="Maya", minutes_ago=25),
        _entry(5, sender="Alex", minutes_ago=20),
        _entry(6, sender="Priya", minutes_ago=15),
        _entry(7, sender="Tom", minutes_ago=10),
        _entry(8, sender="Lena", minutes_ago=5),
    ]
    _write_queue(tmp_path, entries)
    out = dc.check(dc.entries_since(dc.read_stamp()))
    assert out["deliver"] is True
    senders = [it["sender"] for it in out["items"]]
    assert senders[0] == "Lena"                                # newest first
    assert senders.count("Sarah Chen") == 1                    # deduped by sender…
    sarah = next(it for it in out["items"] if it["sender"] == "Sarah Chen")
    assert sarah["preview"] == "new sarah"                     # …keeping the NEWEST entry


def test_signal_entries_never_count_toward_the_threshold(tmp_path, monkeypatch):
    """is_from_me signals are ledger fodder — 20 of them must not trigger a digest."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_DIGEST_MIN", raising=False)
    entries = [_entry(i, sender=f"S{i}", cls="signal") for i in range(20)]
    entries += [_entry(100 + i, sender=f"A{i}") for i in range(3)]
    _write_queue(tmp_path, entries)
    out = dc.check(dc.entries_since(dc.read_stamp()))
    assert out == {"deliver": False}


def test_items_capped_at_6(tmp_path, monkeypatch):
    """The skill renders at most 6 lines — hand it at most 6 items (Sprint 0 §2d), newest first."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_DIGEST_MIN", raising=False)
    _write_queue(tmp_path, [_entry(i, sender=f"Person {i}", minutes_ago=60 - i) for i in range(20)])
    out = dc.check(dc.entries_since(dc.read_stamp()))
    assert out["deliver"] is True and len(out["items"]) == 6
    assert [it["sender"] for it in out["items"]] == [f"Person {i}" for i in range(19, 13, -1)]


def test_threshold_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_DIGEST_MIN", "2")
    _write_queue(tmp_path, [_entry(1, sender="A"), _entry(2, sender="B")])
    out = dc.check(dc.entries_since(dc.read_stamp()))
    assert out["deliver"] is True and len(out["items"]) == 2


def test_stamp_windows_the_queue(tmp_path, monkeypatch):
    """Entries queued BEFORE the last digest stamp are already digested — only newer ones count."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_DIGEST_MIN", "3")
    old = [_entry(i, sender=f"Old {i}", minutes_ago=120) for i in range(8)]
    _write_queue(tmp_path, old)
    assert dc.read_stamp() is None                             # never stamped → everything counts
    assert dc.check(dc.entries_since(dc.read_stamp()))["deliver"] is True
    dc.write_stamp(NOW - timedelta(minutes=60))                # digest delivered an hour ago
    got = dc.read_stamp()
    assert got == NOW - timedelta(minutes=60)                  # round-trips as aware UTC
    assert dc.check(dc.entries_since(dc.read_stamp())) == {"deliver": False}
    # fresh entries after the stamp count again
    _write_queue(tmp_path, old + [_entry(50 + i, sender=f"New {i}", minutes_ago=5) for i in range(3)])
    out = dc.check(dc.entries_since(dc.read_stamp()))
    assert out["deliver"] is True
    assert all(it["sender"].startswith("New") for it in out["items"])


def test_cold_sender_flood_never_trips_heavy_day(tmp_path, monkeypatch):
    """Sprint 0 §2a: the threshold counts SIGNAL. A flood of cold-sender email ("unknown"), group
    chatter and error entries must not fire a digest when only a couple of known-sender items exist."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_DIGEST_MIN", raising=False)
    entries = [_entry(i, sender=f"noreply{i}@spam.example", cls="unknown") for i in range(12)]
    entries += [_entry(100 + i, sender="Founders Chat", cls="group") for i in range(6)]
    entries += [_entry(200, sender="", cls="error")]
    entries += [_entry(300 + i, sender=f"Friend {i}", cls="ambient") for i in range(3)]  # 3 < 8
    _write_queue(tmp_path, entries)
    assert dc.check(dc.entries_since(dc.read_stamp())) == {"deliver": False}


def test_unknown_named_senders_do_not_count_even_when_ambient(tmp_path, monkeypatch):
    """A phone-shaped or "Unknown" sender is not a known person — their ambient entries are noise
    for the threshold, whatever Tier 1 thought of the content."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_DIGEST_MIN", "3")
    entries = [_entry(i, sender=f"+1415555{1000 + i}", cls="ambient") for i in range(5)]
    entries += [_entry(50, sender="Unknown", cls="ambient"), _entry(51, sender="Unknown", cls="ambient")]
    entries += [_entry(60, sender="Sarah Chen", cls="ambient"), _entry(61, sender="Dhruv", cls="quiet")]
    _write_queue(tmp_path, entries)
    assert dc.check(dc.entries_since(dc.read_stamp())) == {"deliver": False}   # 2 known < 3


def test_deferred_agent_classes_count_and_lead_the_items(tmp_path, monkeypatch):
    """quiet/cooldown/stale from known senders count toward the threshold and sort ABOVE known
    ambient (they were judged interrupt-worthy once); newest first within each band."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_DIGEST_MIN", "4")
    entries = [                                                      # append order = oldest first
        _entry(2, sender="Sarah Chen", cls="stale", minutes_ago=30),
        _entry(4, sender="Priya", cls="cooldown", minutes_ago=20),
        _entry(3, sender="Dhruv", cls="quiet", minutes_ago=10),
        _entry(1, sender="Maya", cls="ambient", minutes_ago=5),      # newest overall, but ambient
    ]
    _write_queue(tmp_path, entries)
    out = dc.check(dc.entries_since(dc.read_stamp()))
    assert out["deliver"] is True
    senders = [it["sender"] for it in out["items"]]
    assert senders == ["Dhruv", "Priya", "Sarah Chen", "Maya"]       # deferred first, newest first


def test_excluded_classes_still_ride_below_the_fold(tmp_path, monkeypatch):
    """unknown/group/error never count toward the threshold, but when a digest DOES deliver they
    may appear in items — after every known-sender item — and never push past the 6-item cap."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_DIGEST_MIN", "3")
    entries = [                                                                  # oldest first
        _entry(5, sender="Maya", cls="stale", minutes_ago=50),
        _entry(3, sender="Sarah Chen", cls="ambient", minutes_ago=40),
        _entry(4, sender="Dhruv", cls="ambient", minutes_ago=30),
        _entry(2, sender="Founders Chat", cls="group", minutes_ago=2),
        _entry(1, sender="cold@vendor.example", cls="unknown", minutes_ago=1),   # newest of all
    ]
    _write_queue(tmp_path, entries)
    out = dc.check(dc.entries_since(dc.read_stamp()))
    assert out["deliver"] is True
    senders = [it["sender"] for it in out["items"]]
    assert senders == ["Maya", "Dhruv", "Sarah Chen", "cold@vendor.example", "Founders Chat"]
    # …and the cap still bites across bands
    extra = [_entry(10 + i, sender=f"Extra {i}", cls="ambient", minutes_ago=10 + i) for i in range(4)]
    _write_queue(tmp_path, entries + extra)
    out = dc.check(dc.entries_since(dc.read_stamp()))
    assert len(out["items"]) == 6
    assert all(it["class"] in ("stale", "ambient") for it in out["items"])   # below-fold squeezed out


def test_silent_run_still_stamps_the_window(tmp_path, monkeypatch):
    """Sprint 0 §2b: a quiet-day check must advance last_digest.txt too — otherwise the window
    grows unbounded and stale entries eventually fire a bogus 'heavy day'."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_DIGEST_MIN", raising=False)
    _write_queue(tmp_path, [_entry(1, sender="Sarah Chen")])       # 1 < 8 → silent
    assert dc.read_stamp() is None
    out = dc.run_check(NOW)
    assert out == {"deliver": False}
    assert dc.read_stamp() == NOW                                  # stamped anyway
    # the old entry is now behind the stamp — it can never accumulate into a later trip
    assert dc.entries_since(dc.read_stamp()) == []


def test_delivering_run_stamps_too(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_DIGEST_MIN", "2")
    _write_queue(tmp_path, [_entry(1, sender="A", minutes_ago=10), _entry(2, sender="B", minutes_ago=5)])
    out = dc.run_check(NOW)
    assert out["deliver"] is True
    assert dc.read_stamp() == NOW
    assert dc.run_check(NOW) == {"deliver": False}                 # same entries never re-digested


def test_missing_queue_and_garbage_lines_are_harmless(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert dc.entries_since(None) == []                        # no queue file yet
    d = tmp_path / "events"
    d.mkdir(parents=True)
    good = json.dumps(_entry(1, sender="A"))
    (d / "queue.jsonl").write_text("not json\n" + good + "\n[]\n")
    entries = dc.entries_since(None)
    assert len(entries) == 1 and entries[0]["sender"] == "A"   # one bad line never poisons the read


def test_garbage_stamp_reads_as_none(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    d = tmp_path / "events"
    d.mkdir(parents=True)
    (d / "last_digest.txt").write_text("not-a-timestamp")
    assert dc.read_stamp() is None
