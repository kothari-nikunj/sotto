#!/usr/bin/env python3
"""
proactive_scan.py — decide what (if anything) Sotto should proactively nudge about RIGHT NOW.

This is the deterministic core of the `sotto-proactive` skill (a ~15-min cron). It is intentionally
conservative and the decision logic — quiet hours, lead times, and once-per-item dedup — lives here
(testable) so the agent only DRAFTS and DELIVERS what this returns. PRINCIPLE: auto-draft, never
auto-send; a nudge surfaces a ready draft, it never sends on the user's behalf.

Four nudge kinds:
  - meeting_prep  — an external meeting starting within the lead window that you haven't prepped
  - commitment    — a continuity open-loop whose deadline is today or overdue
  - birthday      — a saved contact whose birthday is today
  - retune_offer  — the stale-loop pile is getting heavy (you keep seeing items you don't act on);
                    offer a quick cleanup (sotto-retune). Throttled to once per cooldown window, NOT daily.

Inputs (argv JSON files; all optional except --now is derived):
  --calendar /tmp/sotto_cal.json     (gather_google calendar: [{id,summary,start,end,attendees[]}])
  --continuity /tmp/sotto_cont.json  (active open-loops: [{id,title,deadline,channel,identifier}])
  --local /tmp/sotto_local.json      (read_local: contacts[] for birthdays)
  --user-email <addr>                (to detect EXTERNAL attendees)
Env: SOTTO_DATA (state dir), SOTTO_TIMEZONE (local day/quiet-hours), SOTTO_QUIET_START/END (default 21/7),
     SOTTO_PROACTIVE_LEAD_MIN (meeting lead window, default 45), SOTTO_USER_EMAIL,
     SOTTO_RETUNE_OFFER_MIN (stale-loop threshold, default 6), SOTTO_RETUNE_OFFER_COOLDOWN_DAYS (default 7).

Output (stdout JSON): {"nudges":[{kind,key,title,detail,channel?,identifier?}], "quiet":bool, "reason"?}
Dedup: keys already nudged today are recorded in $SOTTO_DATA/proactive/<date>.json and skipped. This
script MARKS returned nudges as sent (optimistic) so a 15-min cron never repeats one — a rare missed
nudge is acceptable (the brief is the backstop); a repeated one is annoying.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

# Reuse the brief's tz + contact helpers so "today"/external/birthday logic matches the brief exactly.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shared", "scripts"))
import compose_brief as cb  # noqa: E402


def _load(path, default):
    if not path:
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _state_path(date: str) -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "proactive", f"{date}.json")


def _load_state(date: str) -> set:
    try:
        with open(_state_path(date), encoding="utf-8") as f:
            return set(json.load(f).get("nudged") or [])
    except Exception:
        return set()


def _save_state(date: str, nudged: set):
    try:
        p = _state_path(date)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"nudged": sorted(nudged)}, f)
    except Exception:
        pass


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _retune_marker() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "proactive", "retune_offer.last")


def _retune_cooldown_ok(today_str: str) -> bool:
    """True when it's been at least the cooldown window since the last retune offer (or never offered),
    so we nudge to tidy up periodically rather than every single day."""
    cooldown = _int_env("SOTTO_RETUNE_OFFER_COOLDOWN_DAYS", 7)
    try:
        with open(_retune_marker(), encoding="utf-8") as f:
            last = f.read().strip()[:10]
        days = (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")).days
        return days >= cooldown
    except Exception:
        return True   # never offered → allowed


def _stamp_retune_offer(today_str: str):
    try:
        p = _retune_marker()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(today_str)
    except Exception:
        pass


def _stale_loop_count() -> int:
    """Reuse retune_scan's exact stale definition (overdue / 3–7d / repeat-surfaced) so the offer
    triggers on the same pile the cleanup would act on. Best-effort; 0 on any error."""
    try:
        import retune_scan  # noqa: PLC0415  (sibling in _shared/scripts, already on sys.path)
        return int(retune_scan.scan().get("counts", {}).get("stale", 0))
    except Exception:
        return 0


def scan(calendar, continuity, local, user_email, now_local,
         stale_count: int = 0, retune_offer_allowed: bool = False) -> dict:
    """Pure decision (no I/O): given the inputs and the local 'now', return the nudges due now.
    Quiet hours suppress everything. Dedup is applied by the caller (main). `stale_count` /
    `retune_offer_allowed` are computed by main (they need disk + a cooldown marker)."""
    quiet_start = _int_env("SOTTO_QUIET_START", 21)   # 9pm
    quiet_end = _int_env("SOTTO_QUIET_END", 7)        # 7am
    lead = _int_env("SOTTO_PROACTIVE_LEAD_MIN", 45)
    h = now_local.hour
    # Quiet window wraps midnight (21..24 and 0..7 by default).
    in_quiet = (h >= quiet_start or h < quiet_end) if quiet_start > quiet_end else (quiet_start <= h < quiet_end)
    if in_quiet:
        return {"nudges": [], "quiet": True, "reason": f"quiet hours ({quiet_start}:00–{quiet_end}:00)"}

    user_email = (user_email or "").lower()
    user_domain = user_email.split("@")[1] if "@" in user_email else ""
    today = now_local.strftime("%Y-%m-%d")
    nudges = []

    # 1) Meeting prep — external meeting starting within the lead window (and not already started).
    if isinstance(calendar, dict):
        events = calendar.get("events") or calendar.get("items") or []
    else:
        events = calendar if isinstance(calendar, list) else []
    for e in events:
        if not isinstance(e, dict):
            continue
        st = cb._parse_ts(cb._s(e.get("start")))
        if st is None:
            continue
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        mins_away = (st.astimezone(timezone.utc) - now_local.astimezone(timezone.utc)).total_seconds() / 60.0
        if not (0 <= mins_away <= lead):
            continue
        ext = [a for a in cb._arr(e, "attendees")
               if cb._s(a.get("email")).lower() != user_email
               and not (user_domain and cb._s(a.get("email")).lower().endswith("@" + user_domain))]
        if not ext:
            continue  # internal/solo meeting — no prep nudge
        nudges.append({"kind": "meeting_prep", "key": f"mtg:{cb._s(e.get('id'))}",
                       "title": cb._s(e.get("summary")) or "Meeting",
                       "detail": f"starts in ~{int(mins_away)} min · "
                                 + ", ".join(cb._s(a.get('displayName') or a.get('email')) for a in ext[:4])})

    # 2) Commitments — an open loop whose deadline is today or overdue.
    for c in (continuity if isinstance(continuity, list) else cb._arr(continuity, "items")):
        if not isinstance(c, dict):
            continue
        dl = cb._s(c.get("deadline") or c.get("due"))[:10]
        if dl and dl <= today:
            nudges.append({"kind": "commitment", "key": f"loop:{cb._s(c.get('id')) or dl + cb._s(c.get('title'))[:20]}",
                           "title": cb._s(c.get("title")) or "Open commitment",
                           "detail": ("overdue" if dl < today else "due today"),
                           "channel": cb._s(c.get("channel")), "identifier": cb._s(c.get("identifier"))})

    # 3) Birthdays — a saved contact whose birthday is today (MM-DD).
    mmdd = now_local.strftime("%m-%d")
    for ct in cb._arr(local, "contacts"):
        if cb._s(ct.get("birthday"))[:5] == mmdd and cb._s(ct.get("name")):
            nm = cb._s(ct.get("name"))
            nudges.append({"kind": "birthday", "key": f"bday:{nm.lower()}",
                           "title": f"{nm}'s birthday is today", "detail": "send a quick note"})

    # 4) Retune offer — the stale-loop pile is heavy and the user keeps seeing items they don't act on.
    #    Offer a cleanup instead of waiting to be asked. Throttled by main's cooldown (NOT once-a-day),
    #    so this is a gentle periodic "want to tidy up?", never a daily nag.
    threshold = _int_env("SOTTO_RETUNE_OFFER_MIN", 6)
    if retune_offer_allowed and stale_count >= threshold:
        nudges.append({"kind": "retune_offer", "key": "retune_offer",
                       "title": "Your open-loops list is getting heavy",
                       "detail": f"{stale_count} items keep showing up without action — want a quick cleanup?"})
    return {"nudges": nudges, "quiet": False}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calendar")
    ap.add_argument("--continuity")
    ap.add_argument("--local")
    ap.add_argument("--user-email", dest="user_email")
    args = ap.parse_args()

    now_local = cb._now_local(cb._env_tz() or "+00:00")
    date = now_local.strftime("%Y-%m-%d")

    local = cb._unwrap_local(_load(args.local, {}))
    calendar = _load(args.calendar, [])
    continuity = _load(args.continuity, [])
    user_email = args.user_email or os.environ.get("SOTTO_USER_EMAIL", "")

    # Retune offer: the pile + its multi-day cooldown both need disk, so compute here and pass in.
    stale_count = _stale_loop_count()
    retune_ok = _retune_cooldown_ok(date)
    result = scan(calendar, continuity, local, user_email, now_local,
                  stale_count=stale_count, retune_offer_allowed=retune_ok)
    if not result.get("quiet"):
        seen = _load_state(date)
        fresh = [n for n in result["nudges"] if n["key"] not in seen]
        result["nudges"] = fresh
        if fresh:
            _save_state(date, seen | {n["key"] for n in fresh})   # mark optimistically (no repeats)
            if any(n["kind"] == "retune_offer" for n in fresh):
                _stamp_retune_offer(date)                          # start the cooldown window
    try:
        from sotto_log import diag
        diag(f"[proactive_scan] {len(result['nudges'])} nudge(s)"
             + (f" — {result.get('reason')}" if result.get("quiet") else ""))
    except Exception:
        pass
    print(json.dumps(result))


if __name__ == "__main__":
    main()
