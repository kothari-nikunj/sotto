#!/usr/bin/env python3
"""google_action.py — the WRITE side of the google-workspace CLI (send email, create/delete calendar
events), located + invoked deterministically. Read side is gather_google.py.

⚠️ This SENDS / CREATES for real. ALWAYS gate on the user's approval first (_shared/references/approval-tiers.md) —
never call it without an explicit go-ahead.

Subcommands (each prints the CLI's JSON, or {status:"error", error}):
  gmail-reply --message-id ID --body TEXT          -> reply within a thread
  gmail-send  --to ADDR --subject S --body TEXT     -> new email
  calendar-create --summary S --start ISO --end ISO [--attendees a,b] [--location L] [--description D]
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


def _settings_email() -> str:
    """The user's own address, same chain as the brief: SOTTO_USER_EMAIL (an override) → the
    `google_account_email` the Google connect derived into $SOTTO_DATA/config/settings.json.
    timeutil.configured_user_email holds the ONE copy; a bare CLI run without _shared/lib on the
    path still gets the env."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
        from timeutil import configured_user_email  # noqa: PLC0415
        return configured_user_email()
    except Exception:  # noqa: BLE001
        return (os.environ.get("SOTTO_USER_EMAIL") or "").strip().lower()


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
    exactly like calendar-create (approval-tiers.md → one_tap)."""
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
    c = sub.add_parser("calendar-create"); c.add_argument("--summary", required=True); c.add_argument("--start", required=True); c.add_argument("--end", required=True); c.add_argument("--attendees", default=""); c.add_argument("--location", default=""); c.add_argument("--description", default="")
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
        base = ["calendar", "create", "--summary", a.summary, "--start", a.start, "--end", a.end]
        if a.attendees:
            # An invite Sotto sends must look exactly like one the user sent themselves: the native
            # Calendar UI always adds the creator as a self-accepted attendee, while API-created
            # events list only the attendees passed — leaving the organizer OFF the guest list
            # ("1 guest, 1 awaiting" with no organizer row). Append the user's own address when we
            # know it (env override → the address the Google connect derived) and it isn't already
            # there; when we don't know it at all, behavior is unchanged.
            attendees = a.attendees
            self_email = _settings_email()
            listed = {x.strip().lower() for x in attendees.split(",") if x.strip()}
            self_added = bool(self_email) and self_email.lower() not in listed
            if self_added:
                attendees = f"{attendees},{self_email}"
            base += ["--attendees", attendees]
        extras = []
        if a.location:
            extras += ["--location", a.location]
        if a.description:
            extras += ["--description", a.description]
        out = _run(base + extras)
        if extras and isinstance(out, dict) and out.get("status") == "error" \
                and _looks_unsupported_subcommand(out.get("error", "")):
            # Host CLI predates --location/--description (argparse rejects unknown flags with a
            # usage dump): create bare, then best-effort patch the fields on. The event must never
            # be lost to a cosmetic flag.
            out = _run(base)
            if isinstance(out, dict) and out.get("id") and out.get("status") != "error":
                patch = ["calendar", "patch", str(out["id"])] + extras
                p = _run(patch)
                if isinstance(p, dict) and p.get("status") == "error":
                    out["location_attached"] = False
                    out["note"] = ("host google_api.py lacks location/description support — the "
                                   "invite carries no location; mention the place in your confirmation")
                else:
                    out["location_attached"] = True
        elif extras and isinstance(out, dict) and out.get("status") != "error":
            out["location_attached"] = True
        # Honest guest-list telemetry: a silent miss here is how "1 guest, 1 awaiting" invites ship.
        # The skill surfaces organizer_listed=false to the user so an unknown address (Google not
        # connected yet, or a typo'd SOTTO_USER_EMAIL) is caught on the FIRST invite, not discovered
        # in the Calendar app later.
        if a.attendees and isinstance(out, dict) and out.get("status") != "error":
            out["organizer_listed"] = self_added or (bool(self_email) and self_email.lower() in listed)
            if self_added:
                out["organizer_email"] = self_email
            elif not self_email:
                out["note"] = ("Sotto doesn't know your address yet — connect Google (or set "
                               "SOTTO_USER_EMAIL) so you appear in the invite's guest list")
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
