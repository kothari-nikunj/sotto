#!/usr/bin/env python3
"""google_action.py — the WRITE side of the google-workspace CLI (send email, create/delete calendar
events, manage email labels and Google Contacts), located + invoked deterministically. Read side is gather_google.py.

⚠️ This SENDS / CREATES for real. ALWAYS gate on the user's approval first (_shared/references/approval-tiers.md) —
never call it without an explicit go-ahead.

Subcommands (each prints the CLI's JSON, or {status:"error", error}):
  capabilities                                   -> granted read/write capabilities (no network)
  gmail-draft --to ADDR --body TEXT [--subject S] [--thread-id TID]
                                                    -> save a Gmail DRAFT (nothing is sent)
  gmail-reply --message-id ID --body TEXT          -> reply within a thread
  gmail-send  --to ADDR --subject S --body TEXT     -> new email
  calendar-create --summary S --start ISO --end ISO [--attendees a,b] [--location L] [--description D]
  calendar-delete --event-id ID
  calendar-rsvp   --event-id ID --response accepted|declined|tentative [--calendar C] [--comment TEXT]

Every verb above except `gmail-draft` carries `--offer-bound` (see THE APPROVAL BINDING below —
the offer lane regenerates draft text in the acting session, so binding the draft verb would refuse
every legitimate yes).

`gmail-draft` is not an outbound act — it puts the text in the user's own Gmail drafts, where they
review and press send, which is why the offer surfaces prefer it to a percent-encoded `mailto:`. It
is still a WRITE to the user's account, so it sits behind the same unattended wall as every other
verb in REAL_EFFECT: created only after the user says yes in that conversation (approval-tiers.md →
`review`), never from a cron.

⚠️ THE UNATTENDED GATE — "Sotto drafts, you send" is enforced here, not asked for. The attended chat
lane gates a real effect on the user's yes in the conversation. The unattended lanes (cron briefs,
the proactive watcher, event triage) run with approvals auto-bypassed, so prompt text is not a gate:
the receiver sets `SOTTO_UNATTENDED=1` in the environment of every skill it spawns, and with it set
every verb in `REAL_EFFECT` — the outbound gmail verbs, the calendar writes, and `gmail-draft` — is
REFUSED here before any network call (exit 2, with a `fallback` naming the one safe alternative).
Read verbs are unaffected. Every attempt at a real effect, allowed or refused, leaves one
metadata-only line in `$SOTTO_DATA/events/sends.jsonl`.

⚠️ THE APPROVAL BINDING — a receipt names the bytes, and `--offer-bound` requires them. Each receipt
carries `payload_sha256`, the hash of the exact content about to leave, so "what did Sotto send?"
can be checked against what the user was shown without the ledger holding a word of it. `--offer-bound`
turns that from a record into a gate: the verb reads the cross-process offer (`pending_offer.py`),
requires the FRESH one named by `--offer-id` whose action and `payload_sha256` equal this
invocation's, refuses with exit 2 and a named reason on absence, expiry or mismatch, and consumes
the offer atomically BEFORE the act begins — so success, provider failure, timeout and crash all
need a fresh approval, and an uncertain outcome can never reuse one. That is what
makes a "sure" arriving three processes later bind to the bytes it was given, instead of to whatever
the acting session regenerated. Its limit is stated where it is claimed
(`_shared/references/approval-tiers.md`): in-session approval cannot be machined this way, because
the same agent computes both sides — there the hash makes a receipt disputable, not the act
prevented.

WHY IT DOESN'T GO THROUGH THE HOST CLI: the Hermes `google-workspace` `google_api.py` has no
`gmail draft` verb (its gmail actions are search/get/send/reply/labels/modify), and that CLI is
installed from upstream, not from this repo. This call uses the SAME token file the CLI reads.
Managed draft creation checks for a compose/modify grant first; Gmail read permission alone
does not permit drafts. Managed failures preserve the draft text, never emit a mailto fallback.
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
# The CLI locator AND the direct-to-Gmail client, both owned by the read side. Two files talk to the
# Gmail API without the host CLI — this one for `gmail-draft`, gather_google for the attachment lane
# — and they authenticate through ONE builder on ONE token file, never two copies of it.
from gather_google import _find_google_api, _gmail_service, _google_service, _token_path  # noqa: E402,F401


# ── The gate on real effects ─────────────────────────────────────────────────────────────────────
# It covers every verb that writes the user's Google account: the two that put mail in someone
# else's inbox, the three that write the calendar, and `gmail-draft` — a draft never LEAVES the
# house, but approval-tiers.md is unambiguous that no cron or proactive tick may create one ("a
# drafts folder filling itself up while the user sleeps is busywork theater"), and until Aug 31 the
# code said otherwise while claiming to enforce policy (external review). The unattended fallback
# for every verb is the same: propose it in the brief.
#
# Set by the trigger receiver in the environment of every skill it spawns for a cron / proactive /
# event-triage run — the lanes where `hermes -z` auto-bypasses approvals. Any non-empty value counts
# (fail closed: an unparseable value means unattended, never attended).
UNATTENDED_ENV = "SOTTO_UNATTENDED"

SEND_REFUSAL = {"status": "error",
                "error": "refused: unattended run — Sotto drafts, you send. Propose the reply in "
                         "the brief instead.",
                "fallback": "propose_in_brief"}

DRAFT_REFUSAL = {"status": "error",
                 "error": "refused: unattended run — a Gmail draft is created only when the user "
                          "says yes in that conversation. Propose it in the brief instead.",
                 "fallback": "propose_in_brief"}

CALENDAR_REFUSAL = {"status": "error",
                    "error": "refused: unattended run — a calendar write happens only when the user "
                             "asks for it in that conversation. Propose the event in the brief instead.",
                    "fallback": "propose_in_brief"}

ACCOUNT_REFUSAL = {"status": "error", "error": "refused: unattended run — account changes happen only when the user asks in conversation.", "fallback": "propose_in_brief"}

# verb → (its refusal when unattended, the word its receipt uses when the act succeeded).
REAL_EFFECT = {
    "gmail-draft":     (DRAFT_REFUSAL, "drafted"),
    "gmail-send":      (SEND_REFUSAL, "sent"),
    "gmail-reply":     (SEND_REFUSAL, "sent"),
    "calendar-create": (CALENDAR_REFUSAL, "written"),
    "calendar-delete": (CALENDAR_REFUSAL, "written"),
    "calendar-rsvp":   (CALENDAR_REFUSAL, "written"),
    "calendar-update": (CALENDAR_REFUSAL, "written"),
    "gmail-modify": (ACCOUNT_REFUSAL, "written"),
    "contacts-create": (ACCOUNT_REFUSAL, "written"),
    "contacts-update": (ACCOUNT_REFUSAL, "written"),
    "contacts-delete": (ACCOUNT_REFUSAL, "written"),
}

SENDS_MAX_BYTES = 4 * 1024 * 1024   # same bound as the other $SOTTO_DATA/events ledgers
SENDS_KEEP_LINES = 4000


def _unattended() -> bool:
    """True when this process was spawned by a scheduled / proactive / triage run."""
    return bool(os.environ.get(UNATTENDED_ENV))


def _sends_path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "events", "sends.jsonl")


def _pending_offer():
    """The cross-process offer store, imported from this same directory like `action_links`."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import pending_offer  # noqa: PLC0415
    return pending_offer


def _payload_hash(payload: str) -> str:
    """The hash of the exact content this verb is about to act on. `pending_offer.payload_hash` is
    the one hasher — the offering lane and the acting verb must agree byte-for-byte."""
    return _pending_offer().payload_hash((payload or "").encode("utf-8"))


def _canonical(fields: dict) -> str:
    """The bytes that identify an act with more than one field: sorted keys, no whitespace, so the
    same act hashes the same however the caller assembled it."""
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


def _event_payload(summary: str, start: str, end: str, attendees: str,
                   location: str, description: str) -> str:
    """A calendar event as its whole content — guests normalized and sorted, so a reordered guest
    list is the same invite while an added guest is not."""
    return _canonical({
        "summary": summary or "", "start": start or "", "end": end or "",
        "attendees": sorted({x.strip().lower() for x in (attendees or "").split(",") if x.strip()}),
        "location": location or "", "description": description or "",
    })


def _action_payload(a) -> str:
    """Canonical full identity of a real effect; also exposed by --print-payload for offer writers."""
    if a.cmd == 'calendar-create':
        return _event_payload(a.summary, a.start, a.end, a.attendees, a.location, a.description)
    fields = {key: value for key, value in vars(a).items()
              if key not in ('cmd', 'offer_bound', 'offer_id', 'print_payload')}
    return _canonical(fields)


def _record_send(verb: str, ident: dict, unattended: bool, result: str,
                 payload: str, offer_bound: bool, offer_id: str = "") -> None:
    """One JSONL line per real-effect ATTEMPT — allowed or refused — so "what did Sotto do?" has an
    answer that isn't a prompt's promise:
    {ts, verb, to|message_id|event_id, unattended, result, payload_sha256, offer_bound}.

    METADATA ONLY. The subject, the body and the event's text are never written here: the hash names
    WHICH bytes left without keeping any of them, so a receipt can be checked against what the user
    was shown and still reads as nothing. Best-effort and bounded — an unwritable /data volume must
    never change what the verb itself returns."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
        from sotto_log import bounded_append  # noqa: PLC0415
        receipt = {
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "verb": verb,
            **ident,
            "unattended": unattended,
            "result": result,
            "payload_sha256": _payload_hash(payload),
            "offer_bound": offer_bound,
        }
        if offer_id:
            receipt["offer_id"] = offer_id
        line = json.dumps(receipt)
        bounded_append(_sends_path(), line, SENDS_MAX_BYTES, SENDS_KEEP_LINES)
    except Exception:  # noqa: BLE001
        pass


def _gated(verb: str, ident: dict, payload: str, act, offer_bound: bool,
           offer_id: str = "") -> tuple[dict, bool]:
    """Run one real-effect verb, or refuse it. Returns (result, refused).

    Both refusals land BEFORE `act` — before the CLI, before the network — and the caller exits 2 so
    a bypassed-approval agent sees a hard failure it cannot read past. `payload` is the content about
    to leave; only its hash is ever written. A matching offer is atomically consumed before the
    act begins, so a timeout, crash or provider error cannot reuse uncertain approval."""
    refusal, ok_result = REAL_EFFECT[verb]
    if _unattended():
        _record_send(verb, ident, True, "refused", payload, offer_bound)
        return dict(refusal), True
    if offer_bound:
        try:
            _, reason = _pending_offer().claim_offer(offer_id, verb, _payload_hash(payload))
        except Exception as error:  # no effect after an uncertain approval claim
            reason = f"the pending offer could not be claimed ({type(error).__name__})"
        if reason:
            _record_send(verb, ident, False, "refused", payload, True)
            return {"status": "error", "error": f"refused: {reason}", "fallback": "re_offer"}, True
        # The approval is already consumed. Record that the external effect is starting before the
        # call, so a crash/timeout has a same-offer-id audit trail and still requires fresh consent.
        _record_send(verb, ident, False, "started", payload, True, offer_id)
    out = act()
    ok = isinstance(out, dict) and out.get("status") != "error"
    _record_send(verb, ident, False, ok_result if ok else "error", payload, offer_bound, offer_id)
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
    native = bool(_token_path())
    if native and not capabilities().get("calendar_write"):
        return {"status": "error", "error": "Calendar write permission is missing", "fallback": "request_google_consent"}
    try:
        service = _google_service("calendar", "v3") if native else None
        ev = service.events().get(calendarId=cal, eventId=event_id).execute() if native else _run(get_args)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"Calendar read failed: {exc}"}
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
    try:
        if native:
            request = service.events().patch(calendarId=cal, eventId=event_id,
                body={"attendees": new_attendees}, sendUpdates="all")
            if ev.get("etag"):
                request.headers["If-Match"] = ev["etag"]
            res = request.execute()
            if not res.get("id"):
                return {"status": "error", "error": "RSVP was not confirmed by Google"}
        else:
            res = _run(patch_args)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"RSVP patch failed: {exc}"}
    if not isinstance(res, dict) or res.get("status") == "error":
        err = res.get("error") if isinstance(res, dict) else "unexpected response"
        return {"status": "error", "error": f"RSVP patch failed: {err}"}

    return {"status": "rsvped", "event_id": event_id, "response": response,
            "summary": ev.get("summary") or "", "start": _event_start(ev)}


# ---------------------------------------------------------------------------------------------
# gmail-draft — the only path that talks to Google directly (see the module docstring for why).
# ---------------------------------------------------------------------------------------------

def capabilities() -> dict:
    """Report actual saved grants, never infer writes from a connected read source."""
    try:
        with open(_token_path(), encoding="utf-8") as stream:
            token = json.load(stream)
        raw = token.get("scopes", [])
        scopes = set(raw.split() if isinstance(raw, str) else raw)
        connected = bool(token.get("refresh_token") or token.get("token"))
    except (OSError, ValueError, TypeError, AttributeError):
        scopes, connected = set(), False
    full = "https://mail.google.com/" in scopes
    gmail = "https://www.googleapis.com/auth/gmail."
    calendar = "https://www.googleapis.com/auth/calendar"
    return {"status": "ok", "connected": connected,
            "gmail_read": connected and (full or bool(scopes & {gmail + "readonly", gmail + "modify"})),
            "gmail_draft": connected and (full or bool(scopes & {gmail + "compose", gmail + "modify"})),
            "gmail_write": connected and (full or gmail + "modify" in scopes),
            "gmail_send": connected and (full or bool(scopes & {gmail + "compose", gmail + "modify", gmail + "send"})),
            "calendar_read": connected and bool(scopes & {calendar, calendar + ".readonly", calendar + ".events"}),
            "calendar_write": connected and bool(scopes & {calendar, calendar + ".events"}),
            "contacts_read": connected and bool(scopes & {
                "https://www.googleapis.com/auth/contacts", "https://www.googleapis.com/auth/contacts.readonly"}),
            "contacts_write": connected and "https://www.googleapis.com/auth/contacts" in scopes}


def _draft_failure(error: str, *, consent=False) -> dict:
    managed = os.environ.get("SOTTO_DEPLOYMENT_MODE") == "managed"
    return {"status": "error", "error": error,
            "fallback": ("request_google_consent" if consent else "draft_text") if managed else "deep_link"}

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
    if os.environ.get("SOTTO_DEPLOYMENT_MODE") == "managed" and not capabilities()["gmail_draft"]:
        return _draft_failure("Gmail draft permission is missing. Grant draft access before saving; "
                              "the reply text is still available in chat.", consent=True)
    try:
        service = _gmail_service()
    except Exception as e:  # noqa: BLE001
        return _draft_failure(str(e))

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
        return _draft_failure(f"draft not created: {e}")
    if not isinstance(d, dict) or not d.get("id"):
        return _draft_failure("Gmail returned no draft ID; saving could not be confirmed.")
    # The offered-drafts ledger: a Gmail draft is a draft OFFERED (the user reviews and sends it
    # themselves), so it's graded by the same matcher as tap-link drafts — the sent-mail poll lane
    # provides the outcome side. Best-effort, sibling import; never costs the draft.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import action_links  # noqa: PLC0415
        action_links.record_draft("email", to, body or "", "reply" if thread_id else "send")
    except Exception:  # noqa: BLE001
        pass
    return {"status": "drafted", "draft_id": (d or {}).get("id", ""),
            "thread_id": ((d or {}).get("message") or {}).get("threadId", "") or thread_id,
            "threaded": bool(thread_id), "reply_headers": threaded_headers,
            "to": to, "subject": subject}


def _calendar_create(summary: str, start: str, end: str, attendees: str = "",
                     location: str = "", description: str = "") -> dict:
    """Create the event, with the two honesty repairs the host CLI forces on us."""
    base = ["calendar", "create", "--summary", summary, "--start", start, "--end", end]
    self_email, self_added, listed = "", False, set()
    if attendees:
        # An invite Sotto sends must look exactly like one the user sent themselves: the native
        # Calendar UI always adds the creator as a self-accepted attendee, while API-created
        # events list only the attendees passed — leaving the organizer OFF the guest list
        # ("1 guest, 1 awaiting" with no organizer row). Append the user's own address when we
        # know it (env override → the address the Google connect derived) and it isn't already
        # there; when we don't know it at all, behavior is unchanged.
        self_email = _settings_email()
        listed = {x.strip().lower() for x in attendees.split(",") if x.strip()}
        self_added = bool(self_email) and self_email.lower() not in listed
        if self_added:
            attendees = f"{attendees},{self_email}"
        base += ["--attendees", attendees]
    extras = []
    if location:
        extras += ["--location", location]
    if description:
        extras += ["--description", description]
    out = _run(base + extras)
    if extras and isinstance(out, dict) and out.get("status") == "error" \
            and _looks_unsupported_subcommand(out.get("error", "")):
        # Host CLI predates --location/--description (argparse rejects unknown flags with a
        # usage dump): create bare, then best-effort patch the fields on. The event must never
        # be lost to a cosmetic flag.
        out = _run(base)
        if isinstance(out, dict) and out.get("id") and out.get("status") != "error":
            p = _run(["calendar", "patch", str(out["id"])] + extras)
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
    if attendees and isinstance(out, dict) and out.get("status") != "error":
        out["organizer_listed"] = self_added or (bool(self_email) and self_email.lower() in listed)
        if self_added:
            out["organizer_email"] = self_email
        elif not self_email:
            out["note"] = ("Sotto doesn't know your address yet — connect Google (or set "
                           "SOTTO_USER_EMAIL) so you appear in the invite's guest list")
    return out


CONTACT_FIELDS = {"names", "emailAddresses", "phoneNumbers", "organizations", "birthdays", "addresses", "biographies"}
CONTACT_MASK = ",".join(sorted(CONTACT_FIELDS | {"metadata"}))
EVENT_FIELDS = {"summary", "start", "end", "location", "description"}


def _fields(raw: str, allowed: set) -> dict:
    fields = json.loads(raw)
    if not isinstance(fields, dict) or not fields or set(fields) - allowed:
        raise ValueError("Provide a non-empty object containing only: " + ", ".join(sorted(allowed)))
    return fields


def _native_action(a) -> dict:
    """Bounded Google writes; called only through the unattended/approval gate."""
    capability = ("contacts_write" if a.cmd.startswith("contacts-") else
                  "calendar_write" if a.cmd.startswith("calendar-") else "gmail_write")
    if not capabilities().get(capability):
        return {"status": "error", "error": capability + " permission is missing",
                "fallback": "request_google_consent"}
    try:
        if a.cmd == "gmail-modify":
            body = {"addLabelIds": [x for x in a.add_labels.split(",") if x],
                    "removeLabelIds": [x for x in a.remove_labels.split(",") if x]}
            if not any(body.values()):
                raise ValueError("Provide labels to add or remove")
            result = _gmail_service().users().messages().modify(
                userId="me", id=a.message_id, body=body).execute()
            if not result.get("id"):
                raise ValueError("Google returned no message ID; change is unconfirmed")
            return {"status": "modified", "message_id": result["id"]}
        if a.cmd == "calendar-update":
            fields = _fields(a.fields_json, EVENT_FIELDS)
            result = _google_service("calendar", "v3").events().patch(
                calendarId=a.calendar, eventId=a.event_id, body=fields, sendUpdates="all").execute()
            if not result.get("id"):
                raise ValueError("Google returned no event ID; change is unconfirmed")
            return {"status": "updated", "event_id": result["id"], "htmlLink": result.get("htmlLink", "")}
        resource = getattr(a, "resource_name", "")
        if a.cmd != "contacts-create" and (not resource.startswith("people/c") or "/" in resource[7:]):
            raise ValueError("Use the exact people/c… resourceName returned by contacts-search or contacts-get")
        fields = {} if a.cmd == "contacts-delete" else _fields(a.fields_json, CONTACT_FIELDS)
        if any(not isinstance(value, list) for value in fields.values()):
            raise ValueError("Contact fields must be arrays in Google People format")
        people = _google_service("people", "v1").people()
        if a.cmd == "contacts-delete":
            people.deleteContact(resourceName=resource).execute()
            return {"status": "deleted", "resource_name": resource}
        if a.cmd == "contacts-create":
            result = people.createContact(body=fields, personFields=CONTACT_MASK).execute()
        else:
            current = people.get(resourceName=resource, personFields=CONTACT_MASK,
                                 sources=["READ_SOURCE_TYPE_CONTACT"]).execute()
            sources = [x for x in current.get("metadata", {}).get("sources", []) if x.get("type") == "CONTACT"]
            if not sources or any(not x.get("etag") for x in sources):
                raise ValueError("Google returned no contact version; update refused")
            result = people.updateContact(resourceName=resource,
                updatePersonFields=",".join(sorted(fields)), personFields=CONTACT_MASK,
                body={**fields, "etag": current.get("etag", ""), "metadata": {"sources": sources}}).execute()
        if not result.get("resourceName"):
            raise ValueError("Google returned no contact ID; change is unconfirmed")
        return {"status": "created" if a.cmd == "contacts-create" else "updated",
                "resource_name": result["resourceName"]}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)[:600]}


def _contacts_read(a) -> dict:
    if not capabilities().get("contacts_read"):
        return {"status": "error", "error": "Google Contacts read permission is missing",
                "fallback": "request_google_consent"}
    try:
        people = _google_service("people", "v1").people()
        if a.cmd == "contacts-get":
            return {"status": "ok", "person": people.get(resourceName=a.resource_name,
                personFields=CONTACT_MASK, sources=["READ_SOURCE_TYPE_CONTACT"]).execute()}
        # Google requires a cache warmup before prefix search.
        people.searchContacts(query="", readMask=CONTACT_MASK, pageSize=30).execute()
        result = people.searchContacts(query=a.query, readMask=CONTACT_MASK, pageSize=30).execute()
        return {"status": "ok", "results": result.get("results", [])}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)[:600]}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("capabilities")
    dr = sub.add_parser("gmail-draft"); dr.add_argument("--to", required=True); dr.add_argument("--body", required=True)
    dr.add_argument("--subject", default=""); dr.add_argument("--thread-id", default="")
    r = sub.add_parser("gmail-reply"); r.add_argument("--message-id", required=True); r.add_argument("--body", required=True)
    s = sub.add_parser("gmail-send"); s.add_argument("--to", required=True); s.add_argument("--subject", default=""); s.add_argument("--body", required=True)
    c = sub.add_parser("calendar-create"); c.add_argument("--summary", required=True); c.add_argument("--start", required=True); c.add_argument("--end", required=True); c.add_argument("--attendees", default=""); c.add_argument("--location", default=""); c.add_argument("--description", default="")
    d = sub.add_parser("calendar-delete"); d.add_argument("--event-id", required=True)
    rv = sub.add_parser("calendar-rsvp"); rv.add_argument("--event-id", required=True)
    rv.add_argument("--response", required=True, choices=list(RSVP_RESPONSES))
    rv.add_argument("--calendar", default="primary"); rv.add_argument("--comment", default="")
    cm = sub.add_parser("calendar-update")
    cm.add_argument("--event-id", required=True); cm.add_argument("--calendar", default="primary")
    cm.add_argument("--fields-json", required=True)
    gm = sub.add_parser("gmail-modify")
    gm.add_argument("--message-id", required=True)
    gm.add_argument("--add-labels", default=""); gm.add_argument("--remove-labels", default="")
    cs = sub.add_parser("contacts-search"); cs.add_argument("--query", required=True)
    cg = sub.add_parser("contacts-get"); cg.add_argument("--resource-name", required=True)
    contact_parsers = []
    for verb in ("contacts-create", "contacts-update", "contacts-delete"):
        cp = sub.add_parser(verb)
        if verb != "contacts-create":
            cp.add_argument("--resource-name", required=True)
        if verb != "contacts-delete":
            cp.add_argument("--fields-json", required=True)
        contact_parsers.append(cp)
    for p in (r, s, c, d, rv, cm, gm, *contact_parsers):
        p.add_argument("--offer-bound", action="store_true",
                       help="require a fresh pending offer whose payload hash matches this content")
        p.add_argument("--offer-id", default="",
                       help="select the exact fresh delivered offer; requires --offer-bound")
        p.add_argument("--print-payload", action="store_true",
                       help="print the canonical full action payload for pending_offer --payload-file")
    a = ap.parse_args()

    if a.cmd == "capabilities":
        print(json.dumps(capabilities()))
        return

    if a.cmd in {"contacts-search", "contacts-get"}:
        print(json.dumps(_contacts_read(a)))
        return

    bound = bool(getattr(a, "offer_bound", False))
    offer_id = str(getattr(a, "offer_id", "") or "")
    if offer_id and not bound:
        ap.error("--offer-id requires --offer-bound")
    payload = _action_payload(a)
    if getattr(a, 'print_payload', False):
        sys.stdout.write(payload)
        return
    refused = False
    if a.cmd in {"calendar-update", "gmail-modify", "contacts-create", "contacts-update", "contacts-delete"}:
        ident = {key: getattr(a, key) for key in ("event_id", "message_id", "resource_name") if hasattr(a, key)}
        out, refused = _gated(a.cmd, ident, payload, lambda: _native_action(a), bound, offer_id)
    elif a.cmd == "gmail-draft":
        out, refused = _gated("gmail-draft", {"to": a.to}, a.body,
                              lambda: _gmail_draft(a.to, a.body, a.subject, a.thread_id), False)
    elif a.cmd == "gmail-reply":
        out, refused = _gated("gmail-reply", {"message_id": a.message_id}, payload,
                              lambda: _run(["gmail", "reply", a.message_id, "--body", a.body]), bound, offer_id)
    elif a.cmd == "gmail-send":
        args = ["gmail", "send", "--to", a.to, "--body", a.body]
        if a.subject:
            args += ["--subject", a.subject]
        out, refused = _gated("gmail-send", {"to": a.to}, payload, lambda: _run(args), bound, offer_id)
    elif a.cmd == "calendar-create":
        # No event id exists yet, so the receipt's target is the guest count — a number, never the
        # addresses; the hash covers what the invite actually says.
        out, refused = _gated(
            "calendar-create",
            {"attendee_count": len([x for x in a.attendees.split(",") if x.strip()])},
            payload,
            lambda: _calendar_create(a.summary, a.start, a.end, a.attendees, a.location, a.description),
            bound, offer_id)
    elif a.cmd == "calendar-delete":
        # A delete has no content — the event id is the whole act, so it is what the hash covers,
        # and the field stays uniformly present across every receipt.
        out, refused = _gated("calendar-delete", {"event_id": a.event_id}, payload,
                              lambda: _run(["calendar", "delete", a.event_id]), bound, offer_id)
    elif a.cmd == "calendar-rsvp":
        # The response is half the act — an offer to decline must not bind an accept — so the hash
        # covers it and the note that rides along to the organizer, not the event id alone.
        out, refused = _gated("calendar-rsvp", {"event_id": a.event_id},
                              payload,
                              lambda: _rsvp(a.event_id, a.response, a.calendar, a.comment), bound, offer_id)
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
