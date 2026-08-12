#!/usr/bin/env python3
"""google_action.py — the WRITE side of the google-workspace CLI (send email, create/delete calendar
events), located + invoked deterministically. Read side is gather_google.py.

⚠️ This SENDS / CREATES for real. ALWAYS gate on the user's approval first (_shared/references/approval-tiers.md) —
never call it without an explicit go-ahead.

Subcommands (each prints the CLI's JSON, or {status:"error", error}):
  gmail-draft --to ADDR --body TEXT [--subject S] [--thread-id TID]
                                                    -> save a Gmail DRAFT (nothing is sent)
  gmail-reply --message-id ID --body TEXT          -> reply within a thread
  gmail-send  --to ADDR --subject S --body TEXT     -> new email
  calendar-create --summary S --start ISO --end ISO [--attendees a,b] [--location L] [--description D]
  calendar-delete --event-id ID
  calendar-rsvp   --event-id ID --response accepted|declined|tentative [--calendar C] [--comment TEXT]

`gmail-draft` is the ONE subcommand that is not an outbound act: it puts the text in the user's own
Gmail drafts, where they review and press send. That is why the offer surfaces prefer it to a
percent-encoded `mailto:` — but it still runs only after the user says yes (approval-tiers.md →
`review`), never from a cron.

⚠️ THE SEND GATE — "Sotto drafts, you send" is enforced here, not asked for. The attended chat lane
gates a send on the user's yes in the conversation. The unattended lanes (cron briefs, the proactive
watcher, event triage) run with approvals auto-bypassed, so prompt text is not a gate: the receiver
sets `SOTTO_UNATTENDED=1` in the environment of every skill it spawns, and with it set `gmail-send`
and `gmail-reply` are REFUSED here before any network call (`fallback: "gmail-draft"`, exit 2).
`gmail-draft`, the calendar verbs and every read verb are unaffected — a draft never leaves the
house, and a calendar write is the user's own in-chat instruction. Every send/reply attempt, allowed
or refused, leaves one metadata-only line in `$SOTTO_DATA/events/sends.jsonl`.

WHY IT DOESN'T GO THROUGH THE HOST CLI: the Hermes `google-workspace` `google_api.py` has no
`gmail draft` verb (its gmail actions are search/get/send/reply/labels/modify), and that CLI is
installed from upstream, not from this repo. The granted token already carries `gmail.modify`, which
covers `users.drafts.create` — so this one call goes straight to the Gmail API using the SAME token
file the CLI reads. Every failure (no token, no client lib, an API error) returns
`fallback: "deep_link"` so the caller falls back to the link that always worked.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import subprocess
import sys
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gather_google import _find_google_api  # noqa: E402  (reuse the CLI locator)


# ── The send gate ────────────────────────────────────────────────────────────────────────────────
# It covers the two verbs that put mail in someone else's inbox — `gmail-send` and `gmail-reply`
# (see main()). Everything else here either stays in the user's own account (gmail-draft) or is a
# calendar write they asked for in chat.
#
# Set by the trigger receiver in the environment of every skill it spawns for a cron / proactive /
# event-triage run — the lanes where `hermes -z` auto-bypasses approvals. Any non-empty value counts
# (fail closed: an unparseable value means unattended, never attended).
UNATTENDED_ENV = "SOTTO_UNATTENDED"

REFUSAL = {"status": "error",
           "error": "refused: unattended run — Sotto drafts, you send. Use gmail-draft instead.",
           "fallback": "gmail-draft"}

SENDS_MAX_BYTES = 4 * 1024 * 1024   # same bound as the other $SOTTO_DATA/events ledgers
SENDS_KEEP_LINES = 4000


def _unattended() -> bool:
    """True when this process was spawned by a scheduled / proactive / triage run."""
    return bool(os.environ.get(UNATTENDED_ENV))


def _sends_path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "events", "sends.jsonl")


def _record_send(verb: str, ident: dict, unattended: bool, result: str) -> None:
    """One JSONL line per send/reply ATTEMPT — allowed or refused — so "what did Sotto send?" has an
    answer that isn't a prompt's promise: {ts, verb, to|message_id, unattended, result}.

    METADATA ONLY. The subject and the body are never written here: the receipt proves an outbound
    act happened, it is not a copy of the mail. Best-effort and bounded — an unwritable /data volume
    must never change what the verb itself returns."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
        from sotto_log import bounded_append  # noqa: PLC0415
        line = json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "verb": verb,
            **ident,
            "unattended": unattended,
            "result": result,
        })
        bounded_append(_sends_path(), line, SENDS_MAX_BYTES, SENDS_KEEP_LINES)
    except Exception:  # noqa: BLE001
        pass


def _gated_send(verb: str, ident: dict, cli_args: list) -> tuple[dict, bool]:
    """Run one of the two outbound verbs, or refuse it. Returns (result, refused).

    Unattended → refuse BEFORE `_run` (i.e. before the CLI, before the network); the caller exits 2
    so a bypassed-approval agent sees a hard failure, and `fallback: "gmail-draft"` tells it the one
    thing it may do instead. `ident` is the recipient (send) or the message id (reply)."""
    if _unattended():
        _record_send(verb, ident, True, "refused")
        return dict(REFUSAL), True
    out = _run(cli_args)
    ok = isinstance(out, dict) and out.get("status") != "error"
    _record_send(verb, ident, False, "sent" if ok else "error")
    return out, False


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


# ---------------------------------------------------------------------------------------------
# gmail-draft — the only path that talks to Google directly (see the module docstring for why).
# ---------------------------------------------------------------------------------------------

def _token_path() -> str:
    """The google-workspace token file — the SAME one google_api.py authenticates with
    ($HERMES_HOME/google_token.json, written by its setup.py). "" when Google isn't connected."""
    for base in (os.environ.get("HERMES_HOME", ""), os.path.expanduser("~/.hermes"), "/root/.hermes"):
        if base and os.path.isfile(os.path.join(base, "google_token.json")):
            return os.path.join(base, "google_token.json")
    return ""


def _gmail_service():
    """A Gmail client on the host's existing credentials. Scopes are NOT passed (setup.py's own
    rule: the user may have granted a subset, and passing them makes refresh fail with
    invalid_scope)."""
    path = _token_path()
    if not path:
        raise RuntimeError("Google isn't connected on this host (no google_token.json)")
    from google.oauth2.credentials import Credentials  # noqa: PLC0415
    from googleapiclient.discovery import build        # noqa: PLC0415
    return build("gmail", "v1", credentials=Credentials.from_authorized_user_file(path),
                 cache_discovery=False)


def _thread_tail(service, thread_id: str) -> dict:
    """The last message's Subject + RFC-822 Message-ID on a thread — what a reply needs to land IN
    the thread rather than beside it. {} when the thread can't be read (the draft is still made)."""
    try:
        th = service.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["Message-ID", "Subject"]).execute()
    except Exception:  # noqa: BLE001
        return {}
    msgs = [m for m in (th.get("messages") or []) if isinstance(m, dict)]
    if not msgs:
        return {}
    headers = ((msgs[-1].get("payload") or {}).get("headers")) or []
    h = {str(x.get("name", "")).lower(): str(x.get("value", "")) for x in headers if isinstance(x, dict)}
    return {"message_id": h.get("message-id", ""), "subject": h.get("subject", "")}


def _gmail_draft(to: str, body: str, subject: str = "", thread_id: str = "") -> dict:
    """Save a Gmail draft — threaded when `thread_id` is known. Never sends.

    Threading is two things at once and we do both: `threadId` on the message (what groups it in
    Gmail) and `In-Reply-To`/`References` carrying the thread's last Message-ID (what makes every
    OTHER mail client see a reply). When a thread is known its own Subject wins, `Re:`-prefixed —
    Gmail rejects a threadId whose subject doesn't match the thread."""
    try:
        service = _gmail_service()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e), "fallback": "deep_link"}

    tail = _thread_tail(service, thread_id) if thread_id else {}
    msg = MIMEText(body or "")
    msg["To"] = to
    threaded_headers = False
    if tail.get("subject"):
        s = tail["subject"]
        subject = s if s.lower().startswith("re:") else f"Re: {s}"
    if tail.get("message_id"):
        msg["In-Reply-To"] = tail["message_id"]
        msg["References"] = tail["message_id"]
        threaded_headers = True
    if subject:
        msg["Subject"] = subject

    message = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        message["threadId"] = thread_id
    try:
        d = service.users().drafts().create(userId="me", body={"message": message}).execute()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"draft not created: {e}", "fallback": "deep_link"}
    return {"status": "drafted", "draft_id": (d or {}).get("id", ""),
            "thread_id": ((d or {}).get("message") or {}).get("threadId", "") or thread_id,
            "threaded": bool(thread_id), "reply_headers": threaded_headers,
            "to": to, "subject": subject}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    dr = sub.add_parser("gmail-draft"); dr.add_argument("--to", required=True); dr.add_argument("--body", required=True)
    dr.add_argument("--subject", default=""); dr.add_argument("--thread-id", default="")
    r = sub.add_parser("gmail-reply"); r.add_argument("--message-id", required=True); r.add_argument("--body", required=True)
    s = sub.add_parser("gmail-send"); s.add_argument("--to", required=True); s.add_argument("--subject", default=""); s.add_argument("--body", required=True)
    c = sub.add_parser("calendar-create"); c.add_argument("--summary", required=True); c.add_argument("--start", required=True); c.add_argument("--end", required=True); c.add_argument("--attendees", default=""); c.add_argument("--location", default=""); c.add_argument("--description", default="")
    d = sub.add_parser("calendar-delete"); d.add_argument("--event-id", required=True)
    rv = sub.add_parser("calendar-rsvp"); rv.add_argument("--event-id", required=True)
    rv.add_argument("--response", required=True, choices=list(RSVP_RESPONSES))
    rv.add_argument("--calendar", default="primary"); rv.add_argument("--comment", default="")
    a = ap.parse_args()

    refused = False
    if a.cmd == "gmail-draft":
        out = _gmail_draft(a.to, a.body, a.subject, a.thread_id)
    elif a.cmd == "gmail-reply":
        out, refused = _gated_send("gmail-reply", {"message_id": a.message_id},
                                   ["gmail", "reply", a.message_id, "--body", a.body])
    elif a.cmd == "gmail-send":
        args = ["gmail", "send", "--to", a.to, "--body", a.body]
        if a.subject:
            args += ["--subject", a.subject]
        out, refused = _gated_send("gmail-send", {"to": a.to}, args)
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
    if refused:
        # Exit 2, not 0-with-an-error: an agent running with approvals bypassed must hit a wall it
        # cannot read past. The JSON on stdout tells it what to do instead (gmail-draft).
        sys.exit(2)


if __name__ == "__main__":
    main()
