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


def test_demoted_scheduling_ask_counts_toward_threshold(tmp_path, monkeypatch):
    """A scheduling_ask demoted at triage queues as class 'stale' (+ held_class) — nothing filters
    the new class out of the digest: the row counts and the extra held_class field is harmless."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    entries = [dict(_entry(i, sender=f"P{i}", cls="stale"), held_class="scheduling_ask")
               for i in range(8)]
    _write_queue(tmp_path, entries)
    out = dc.check(dc.entries_since(dc.read_stamp()))
    assert out["deliver"] is True
    assert len(out["items"]) == 6                              # cap holds; rows all counted


def test_budget_and_snooze_held_rows_count_as_deferred_actionable(tmp_path, monkeypatch):
    """The two classes the Editor's volume controls create ('budget' = the daily interrupt cap,
    'snoozed' = the user's cadence lever) are the digest's job: neither promotes back through the
    release valve, so the digest is their way out. They count toward the threshold AND sort ahead
    of born-ambient chatter, exactly like quiet/cooldown/stale."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_DIGEST_MIN", raising=False)
    entries = [_entry(i, sender=f"A{i}", cls="ambient", minutes_ago=10) for i in range(6)]
    entries += [dict(_entry(90, sender="Sarah Chen", cls="budget", minutes_ago=40),
                     held_class="urgent"),
                dict(_entry(91, sender="Dhruv Patel", cls="snoozed", minutes_ago=50),
                     held_class="scheduling_ask"),
                # the in-meeting hold's class joins them: it DOES promote via the valve, but a day
                # spent entirely in rooms is exactly the day the digest exists for
                dict(_entry(92, sender="Ana Ruiz", cls="meeting_hold", minutes_ago=55),
                     held_class="actionable")]
    _write_queue(tmp_path, entries)
    out = dc.check(dc.entries_since(dc.read_stamp()))
    assert out["deliver"] is True                              # 9 counted signals, not 6
    assert {it["sender"] for it in out["items"][:3]} == {"Sarah Chen", "Dhruv Patel", "Ana Ruiz"}
    held = {"budget", "snoozed", "meeting_hold"}
    assert held <= dc.COUNT_CLASSES and held <= dc.ACTIONABLE_CLASSES

def _proactive_entry(kind, title, cls="budget", minutes_ago=10):
    """A held proactive nudge, exactly as proactive_scan queues it: Sotto's own voice, with the
    nudge's title standing in for a sender."""
    ts = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ts": ts, "verdict_class": cls, "sender": title, "held_class": kind,
            "event": {"source": "proactive", "kind": kind, "key": f"{kind}:1", "text": title}}


def test_sottos_own_held_nudges_never_trip_the_heavy_day_gate(tmp_path, monkeypatch):
    """The gate measures "real signals from people you know". A birthday reminder and a tidy-up
    offer are neither — counting them as known senders inflated the threshold with Sotto's own
    voice, on a day that had seven actual signals."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_DIGEST_MIN", raising=False)
    entries = [_entry(i, sender=f"P{i}", minutes_ago=20) for i in range(7)]     # 7 < 8
    entries += [_proactive_entry("birthday", "Jordan's birthday is today"),
                _proactive_entry("retune_offer", "Your open-loops list is getting heavy")]
    _write_queue(tmp_path, entries)
    assert dc.check(dc.entries_since(dc.read_stamp())) == {"deliver": False}


def test_a_delivered_digest_still_carries_them_below_the_fold(tmp_path, monkeypatch):
    """They are real held nudges — when a digest DOES deliver they ride along under their own line,
    ranked below the people."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_DIGEST_MIN", "2")
    _write_queue(tmp_path, [_entry(1, sender="Sarah Chen", cls="budget", minutes_ago=30),
                            _entry(2, sender="Dhruv Patel", cls="quiet", minutes_ago=20),
                            _proactive_entry("chase", "Acme — the contract", minutes_ago=10)])
    out = dc.check(dc.entries_since(dc.read_stamp()))
    assert out["deliver"] is True
    assert [it["sender"] for it in out["items"]][-1] == "Acme — the contract"   # below the fold


# ── Brief-side digest stamp (Sprint 0 §2c — roadmap Step 2 item 0) ────────────────────────────────

def test_advance_stamp_moves_the_window_forward(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    dc.advance_stamp(NOW)
    assert dc.read_stamp() == NOW
    dc.advance_stamp(NOW + timedelta(hours=1))
    assert dc.read_stamp() == NOW + timedelta(hours=1)


def test_advance_stamp_never_rewinds(tmp_path, monkeypatch):
    """A re-run / eval / backfilled compose must not replay items the user already saw."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    dc.advance_stamp(NOW)
    dc.advance_stamp(NOW - timedelta(hours=6))
    assert dc.read_stamp() == NOW
    dc.advance_stamp(NOW)                       # equal is a no-op too
    assert dc.read_stamp() == NOW


def test_advance_stamp_never_raises(monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", "/proc/definitely-unwritable")
    dc.advance_stamp(NOW)                       # best-effort, like write_stamp


def test_brief_stamp_shrinks_the_digest_window(tmp_path, monkeypatch):
    """The whole point of §2c: a heavy morning that the 6:30a brief already covered must NOT also
    trip the 12:30 digest. Same queue, same threshold — only the stamp changes the answer."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_DIGEST_MIN", raising=False)
    brief_at = NOW - timedelta(hours=6)                          # 06:30
    pre = [_entry(i, sender=f"P{i}", minutes_ago=420) for i in range(10)]   # before the brief
    post = [_entry(50 + i, sender=f"Q{i}", minutes_ago=60) for i in range(3)]
    _write_queue(tmp_path, pre + post)

    # no stamp → the whole pile counts and the digest fires (today's un-fixed behavior)
    assert dc.check(dc.entries_since(dc.read_stamp()))["deliver"] is True

    # the brief stamps → only the 3 post-brief events remain in window → silence
    dc.advance_stamp(brief_at)
    assert dc.entries_since(dc.read_stamp()) == post
    assert dc.check(dc.entries_since(dc.read_stamp())) == {"deliver": False}


def _brief_marker():
    spec_bm = importlib.util.spec_from_file_location(
        "bm_stamp", os.path.join(ROOT, "_shared", "scripts", "brief_marker.py"))
    bm = importlib.util.module_from_spec(spec_bm)
    spec_bm.loader.exec_module(bm)
    return bm


def test_the_claim_that_wins_delivery_advances_the_digest_stamp(tmp_path, monkeypatch):
    """Wiring test: the stamp rides the deliver-once claim, not compose. The claim that WINS moves
    the window; a claim that LOSES (the other path already delivered) must not move it again."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.delenv("SOTTO_DIGEST_MIN", raising=False)
    bm = _brief_marker()
    _write_queue(tmp_path, [_entry(i, sender=f"P{i}", minutes_ago=400) for i in range(10)])

    assert dc.read_stamp() is None
    assert bm.claim("morning") is True
    stamped = dc.read_stamp()
    assert stamped is not None                                   # the delivering brief moved it
    assert (datetime.now(timezone.utc) - stamped).total_seconds() < 120
    # everything in the queue predates the brief → the 12:30 digest stays silent
    assert dc.run_check(datetime.now(timezone.utc)) == {"deliver": False}
    # the losing claim doesn't stamp (advance_stamp is forward-only, but it isn't even called)
    dc.write_stamp(stamped - timedelta(hours=3))
    assert bm.claim("morning") is False
    assert dc.read_stamp() == stamped - timedelta(hours=3)
    # evening briefs stamp too (tomorrow's window starts at last night's brief)
    assert bm.claim("evening") is True
    assert dc.read_stamp() >= stamped


def test_compose_without_a_claim_never_stamps(tmp_path, monkeypatch):
    """The bug this moved to fix: an on-demand 9am compose that never wins the claim must not
    swallow the 6:30–9:00 digest window."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_DIGEST_MIN", raising=False)
    import compose_brief as cb
    _write_queue(tmp_path, [_entry(i, sender=f"P{i}", minutes_ago=30) for i in range(10)])
    cb._archive_brief({"brief_text": "good morning"}, "morning")
    assert dc.read_stamp() is None                               # composing is not delivering
    assert dc.check(dc.entries_since(dc.read_stamp()))["deliver"] is True
