#!/usr/bin/env python3
"""
brief_marker.py — the single delivered-once gate that coordinates the cloud cron and the Bridge wake-push.

Both paths run the SAME brief skill (cron runs it directly; wake-push POSTs morning_ready → the receiver
runs it). Without coordination, enabling wake-push double-delivers. This is the atomic claim they share:
the brief skill calls `--claim <kind>` RIGHT BEFORE sending — exactly one process wins the O_EXCL create,
the loser sees "already" and discards its draft. (Claim before send, not at start, so a compose failure
never suppresses the day's brief — only a successful run that's about to deliver claims.)

  brief_marker.py --claim morning    → prints "claimed" (you deliver) or "already" (someone else did; stop)
  brief_marker.py --check  morning    → prints "delivered" or "open" (read-only peek)
The winning claim also advances the midday-digest window (digest_check.advance_stamp) — see
_stamp_digest_window below: the brief that DELIVERS is the one the 12:30 digest must not repeat.
Flag file: $SOTTO_DATA/briefs/<YYYY-MM-DD>.<kind>.delivered  (<kind> = morning|evening), tz from SOTTO_TIMEZONE.

THE MARKER'S CONTENT IS *WHO* CLAIMED IT — and WHO WRITES IT depends on the lane. A receiver-spawned
run (`$SOTTO_DELIVERY_RUN_ID` set) never writes here: its output travels through the receiver's
durable outbox, and THAT send seam claims the marker (writing the run id) plus the digest stamp at
the moment of actual delivery — claiming any earlier left a crash window where the day read as
delivered with nothing queued to deliver it (external review, Aug 31). For those runs `--claim` only
answers: absent/ours → "claimed", another lane's → "already". An install whose own scheduler runs
this skill in-agent still claims HERE (writing `unlabeled` — it cannot name itself), because there
this claim is the only gate there is. Empty or `unlabeled` reads as ANOTHER LANE to anyone comparing
ids. `--check` is unchanged — presence alone is still the read-only peek.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from timeutil import _now_local, configured_tz  # noqa: E402  (the user's local date)


def _path(kind: str) -> str:
    date = _now_local(configured_tz() or "+00:00").strftime("%Y-%m-%d")
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "briefs", f"{date}.{kind}.delivered")


def _stamp_digest_window() -> None:
    """Advance the midday-digest window to now (Sprint 0 §2c). Called ONLY by the claim that WINS,
    because that is the composition that actually delivers: the digest reports queue.jsonl entries
    "since the last stamp", so stamping at compose time meant an on-demand 9am run that then LOST
    the claim silently swallowed the 6:30–9:00 window. Both kinds stamp — the evening brief covers
    the day too, so tomorrow's window should start at last night's brief. advance_stamp is
    forward-only; best-effort, so a stamp failure never breaks claiming."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "..", "event-triage", "scripts"))
        import digest_check  # noqa: PLC0415
        digest_check.advance_stamp(datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001
        pass


UNLABELED = "unlabeled"   # a claimer with no run id: nobody can later prove the marker is theirs


def claim(kind: str) -> bool:
    """Atomically claim today's <kind> brief, writing WHO claimed it. True = you won (deliver);
    False = already delivered (stop).

    RECEIVER-SPAWNED runs (SOTTO_DELIVERY_RUN_ID set) do not write here. Their words leave the box
    through the receiver's durable outbox, whose send seam claims this same marker — with the digest
    stamp — at the moment of actual delivery. Writing it HERE too opened a hole (external review,
    Aug 31): a run that claimed and then died before its output reached the outbox left the day
    marked delivered with no row to retry — a silenced brief. So under the receiver this call only
    ANSWERS: marker absent or already ours → "claimed" (compose on, the seam will gate); another
    lane's → "already" (stop; the seam would supersede this copy anyway). The write below remains
    for installs whose own scheduler runs this skill in-agent — there this claim is the only gate
    there is."""
    p = _path(kind)
    run_id = os.environ.get("SOTTO_DELIVERY_RUN_ID", "")
    if run_id:
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip() == run_id
        except OSError:
            return True   # no marker yet (the normal case): deliver — the seam claims durably
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(UNLABELED)
    except FileExistsError:
        return False
    except OSError:
        # This is an authorization gate. An uncertain claim must never become permission for two
        # lanes to deliver the same brief; the next scheduler tick can retry once the volume/lock
        # is healthy.
        return False
    _stamp_digest_window()   # this run is the one that delivers → the digest window starts here
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claim")
    ap.add_argument("--check")
    a = ap.parse_args()
    if a.claim:
        print("claimed" if claim(a.claim) else "already")
    elif a.check:
        print("delivered" if os.path.exists(_path(a.check)) else "open")
    else:
        ap.error("use --claim <kind> or --check <kind>")


if __name__ == "__main__":
    main()
