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


def test_items_capped_at_12(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_DIGEST_MIN", raising=False)
    _write_queue(tmp_path, [_entry(i, sender=f"Person {i}", minutes_ago=60 - i) for i in range(20)])
    out = dc.check(dc.entries_since(dc.read_stamp()))
    assert out["deliver"] is True and len(out["items"]) == 12


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
