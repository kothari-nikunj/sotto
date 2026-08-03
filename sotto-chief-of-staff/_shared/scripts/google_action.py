#!/usr/bin/env python3
"""google_action.py — the WRITE side of the google-workspace CLI (send email, create/delete calendar
events), located + invoked deterministically. Read side is gather_google.py.

⚠️ This SENDS / CREATES for real. ALWAYS gate on the user's approval first (sotto-approval-tiers) —
never call it without an explicit go-ahead.

Subcommands (each prints the CLI's JSON, or {status:"error", error}):
  gmail-reply --message-id ID --body TEXT          -> reply within a thread
  gmail-send  --to ADDR --subject S --body TEXT     -> new email
  calendar-create --summary S --start ISO --end ISO [--attendees a,b]
  calendar-delete --event-id ID
  calendar-rsvp   --event-id ID --response accepted|declined|tentative [--calendar C] [--comment TEXT]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gather_google import _find_google_api  # noqa: E402  (reuse the CLI locator)


def _run(args) -> dict:
    api = _find_google_api()
    if not api:
        return {"status": "error", "error": "google_api.py not found — is Google connected (setup.py --check)?"}
    py = sys.executable or "python3"
    try:
        r = subprocess.run([py, api, *args], capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"could not run google_api.py: {e}"}
    if r.returncode != 0:
        return {"status": "error", "error": (r.stderr or r.stdout or "failed").strip()[:600]}
    try:
        return json.loads(r.stdout or "{}")
    except Exception:
        return {"status": "ok", "raw": (r.stdout or "").strip()}


RSVP_RESPONSES = ("accepted", "declined", "tentative")


def _looks_unsupported_subcommand(err: str) -> bool:
    """The host's google_api.py may not implement `calendar get`/`patch` (see HALF-BUILT). argparse then
    rejects the subcommand with an 'invalid choice' / bare 'usage:' error on stderr — NOT a real
    'event not found'. Detect that so we can surface a clear capability error + deep-link fallback
    instead of a confusing usage dump masquerading as a missing event."""
    e = (err or "").lower()
    return "invalid choice" in e or "usage:" in e


def _self_attendee_email(event: dict) -> str:
    """The account's own address on an event (attendee flagged self:true, else a self organizer).
    Minimal reimplementation of compose_brief._self_attendee_email — the same resolution _event_link_map
    uses — so RSVP can find who "you" are without importing compose_brief or a passed userEmail."""
    for at in event.get("attendees") or []:
        if isinstance(at, dict) and at.get("self") and at.get("email"):
            return str(at["email"]).lower()
    org = event.get("organizer")
    if isinstance(org, dict) and org.get("self") and org.get("email"):
        return str(org["email"]).lower()
    return ""


def _event_start(event: dict) -> str:
    """Event start as a string, whether the CLI returns {dateTime|date} or a bare string."""
    s = event.get("start")
    if isinstance(s, dict):
        return s.get("dateTime") or s.get("date") or ""
    return s or ""


def _rsvp(event_id: str, response: str, calendar: str = "primary", comment: str = "") -> dict:
    """Set the account's own responseStatus on an event and PATCH it back.

    The Calendar API REPLACES the attendees list on patch, so we fetch the event, mutate ONLY the self
    attendee in place, and send the FULL attendees array back — preserving everyone else byte-for-byte.
    sendUpdates=all so the organizer is notified. This is a real calendar WRITE: gate on approval first,
    exactly like calendar-create (sotto-approval-tiers → one_tap)."""
    response = (response or "").lower().strip()
    if response not in RSVP_RESPONSES:
        return {"status": "error", "error": f"invalid response '{response}' — use accepted|declined|tentative"}
    cal = (calendar or "primary").strip() or "primary"

    get_args = ["calendar", "get", event_id]
    if cal != "primary":
        get_args += ["--calendar", cal]
    ev = _run(get_args)
    if isinstance(ev, dict) and ev.get("status") == "error" and _looks_unsupported_subcommand(ev.get("error", "")):
        # The host CLI can't do calendar get/patch — RSVP by API isn't available here. Distinct error
        # so the skill offers the calendar deep link instead of retrying / dumping usage at the user.
        return {"status": "error",
                "error": "host google_api.py lacks calendar get/patch — RSVP by API unavailable on this host",
                "fallback": "deep_link"}
    if not isinstance(ev, dict) or ev.get("status") == "error":
        err = ev.get("error") if isinstance(ev, dict) else "unexpected response"
        return {"status": "error", "error": f"event not found: {err}"}

    attendees = ev.get("attendees") or []
    organizer_is_self = isinstance(ev.get("organizer"), dict) and ev["organizer"].get("self")
    if not attendees:
        return {"status": "error", "error": "you're the organizer — nothing to RSVP (event has no attendees)"}

    # Locate the self attendee: prefer self:true, else match the account's own email.
    self_idx = next((i for i, at in enumerate(attendees)
                     if isinstance(at, dict) and at.get("self")), None)
    if self_idx is None:
        self_email = _self_attendee_email(ev)
        if self_email:
            self_idx = next((i for i, at in enumerate(attendees)
                             if isinstance(at, dict) and str(at.get("email") or "").lower() == self_email), None)
    if self_idx is None:
        if organizer_is_self:
            return {"status": "error", "error": "you're the organizer — nothing to RSVP"}
        return {"status": "error", "error": "you're not an attendee on this event — nothing to RSVP"}

    # Mutate ONLY the self entry (copy it); every other attendee stays the same object → byte-identical.
    updated = dict(attendees[self_idx])
    updated["responseStatus"] = response
    if comment:
        updated["comment"] = comment
    new_attendees = list(attendees)
    new_attendees[self_idx] = updated

    patch_args = ["calendar", "patch", event_id,
                  "--attendees-json", json.dumps(new_attendees),
                  "--send-updates", "all"]
    if cal != "primary":
        patch_args += ["--calendar", cal]
    res = _run(patch_args)
    if not isinstance(res, dict) or res.get("status") == "error":
        err = res.get("error") if isinstance(res, dict) else "unexpected response"
        return {"status": "error", "error": f"RSVP patch failed: {err}"}

    return {"status": "rsvped", "event_id": event_id, "response": response,
            "summary": ev.get("summary") or "", "start": _event_start(ev)}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("gmail-reply"); r.add_argument("--message-id", required=True); r.add_argument("--body", required=True)
    s = sub.add_parser("gmail-send"); s.add_argument("--to", required=True); s.add_argument("--subject", default=""); s.add_argument("--body", required=True)
    c = sub.add_parser("calendar-create"); c.add_argument("--summary", required=True); c.add_argument("--start", required=True); c.add_argument("--end", required=True); c.add_argument("--attendees", default="")
    d = sub.add_parser("calendar-delete"); d.add_argument("--event-id", required=True)
    rv = sub.add_parser("calendar-rsvp"); rv.add_argument("--event-id", required=True)
    rv.add_argument("--response", required=True, choices=list(RSVP_RESPONSES))
    rv.add_argument("--calendar", default="primary"); rv.add_argument("--comment", default="")
    a = ap.parse_args()

    if a.cmd == "gmail-reply":
        out = _run(["gmail", "reply", a.message_id, "--body", a.body])
    elif a.cmd == "gmail-send":
        args = ["gmail", "send", "--to", a.to, "--body", a.body]
        if a.subject:
            args += ["--subject", a.subject]
        out = _run(args)
    elif a.cmd == "calendar-create":
        args = ["calendar", "create", "--summary", a.summary, "--start", a.start, "--end", a.end]
        if a.attendees:
            args += ["--attendees", a.attendees]
        out = _run(args)
    elif a.cmd == "calendar-delete":
        out = _run(["calendar", "delete", a.event_id])
    elif a.cmd == "calendar-rsvp":
        out = _rsvp(a.event_id, a.response, a.calendar, a.comment)
    else:  # pragma: no cover
        out = {"status": "error", "error": f"unknown cmd {a.cmd}"}

    # Log the write (server-visible at /debug/brief-log) — useful audit of what Sotto sent/created.
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
        from sotto_log import diag
        diag(f"[google_action] {a.cmd} -> {out.get('status', 'ok')}"
             + (f" ({out['error']})" if out.get("status") == "error" else ""))
    except Exception:
        pass
    print(json.dumps(out))


if __name__ == "__main__":
    main()
