#!/usr/bin/env python3
"""gather_google.py — deterministically fetch Gmail (last 24h inbox + a small `in:sent` lane) +
Calendar (next 3 days) via Sotto's packaged Google CLI (`adapters/sotto/google_api.py`), normalized to the shapes
compose_brief expects, and write /tmp/sotto_gmail.json + /tmp/sotto_cal.json.

WHY: the brief skill used to tell the agent to "use the Google tools" and write the files by
hand. The agent kept skipping it → local-only briefs (0 emails), the #1 quality failure. The Google
interface is a CLI:
    google_api.py gmail search "newer_than:1d" --max N   -> [{id,threadId,from,subject,date,snippet,labels}]
    google_api.py gmail get MESSAGE_ID                    -> full message (body, headers, labels)
    google_api.py calendar list --start ISO --end ISO     -> [{id,summary,start,end,location,description,htmlLink}]
Running it deterministically (one command the skill invokes) makes Google ALWAYS get married with local.

The daily gather runs TWO Gmail searches: the inbox window (`newer_than:1d`, --max) and a small sent
lane (`in:sent newer_than:1d`, --sent-max, default 15). Sent rows are merged into the same array
(deduped by id) carrying isSent:true + a SENT label, which is the shape both style_extract's --gmail
adapter and render_local's _is_sent_email read. Without the sent lane the Learn step's "email voice"
had nothing to learn from, and email had no outgoing lane for the draft-diff matcher.

Usage: python3 gather_google.py [--gmail-out P] [--cal-out P] [--max N] [--bodies N]
                                [--sent-max N | --skip-sent]
       python3 gather_google.py --ensure-deps    (setup-time: ONLY run the googleapiclient self-heal)
       python3 gather_google.py --attendee-comms /tmp/sotto_research_in.json \
                                [--comms-out /tmp/sotto_attendee_comms.json]
           (meeting-prep: per-attendee Gmail threads — for each attendee email, search
           `from:<email> OR to:<email> newer_than:30d` and write
           {"<email>": [{date,subject,snippet,from_me}]} — the private context the prep marries
           with web research. Degrades to an empty {} without Google.)
Exits 0 even on failure (writes empty files + a WARNING line) so the brief still runs.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import getaddresses

# The attachment lane's caps and its converter, imported from their OWNER in _shared/lib. The three
# numbers are defined once, there, and read here — the fetch side and the render side sharing one
# set of constants is what keeps "3 per email, 8MB, 3,500 chars" from becoming two different claims.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from attachments import (  # noqa: E402
    MAX_ATTACHMENTS_PER_EMAIL, MAX_ATTACHMENT_BYTES, apply_attachment_budget, convert_attachment,
)
from gmail_read import (fetch_message as _native_fetch_message, gmail_service as _shared_gmail_service,
                        google_service as _shared_google_service,
                        token_path as _shared_token_path)  # noqa: E402

from textutil import first_nonempty as _pick, format_address as _addr_str  # noqa: E402
from timeutil import configured_user_email  # noqa: E402
from calendar_gather import calendar_gather, calendar_result  # noqa: E402

BODY_FETCH_WORKERS = 5   # concurrent full-body fetches, each with its own client/transport
BRIEF_THREAD_CHARS = 4000  # same opening/latest excerpt as prep, per selected inbox thread
# google_api paces metadata reads at four starts per second, so 20 rows need <= 4.75 s of pacing and
# leave the rest of the 12 s child deadline for interpreter start, token refresh and the list call.
# A busy inbox then returns a capped `partial` receipt instead of being killed into `unavailable`.
PULSE_REFRESH_MAX_MESSAGES = 20
PULSE_REFRESH_TIMEOUT_SECONDS = 12

# Sent lane (roadmap Step 2 item 0): a second, SMALL `in:sent newer_than:1d` search alongside the
# inbox one. Two consumers, both of which need the user's own outgoing EMAIL and had no source for it:
#   • style_extract.py --gmail (_adapt_gmail) — keys off isSent/the SENT label and feeds the
#     work_email bucket. The Learn step already passes /tmp/sotto_gmail.json; until now that file was
#     an inbox search, so the email voice never actually learned anything.
#   • Step 3's draft-diff matcher — email is the one channel with no outgoing lane in the funnel
#     (queue.jsonl carries is_from_me for iMessage/WhatsApp only).
# Kept small on purpose: it's exhaust, not brief content, and it costs wall clock in the brief path.
SENT_MAX = 15            # sent messages fetched per daily gather
SENT_BODIES = 10         # of those, how many get a full-body fetch (snippets are too thin to learn voice from)

# Stale sent lane (roadmap, Sep 2026): "you emailed them four days ago and got nothing". One
# `in:sent` search over a bounded window, then one threads.get per candidate thread to ask: is the
# LAST message on the thread still yours? Rows go to the gmail payload as `stale_threads`. Silence
# alone is context, not a debt: the brief model may propose a waiting_on only for a specific
# outstanding request or deliverable, with its evidence.
STALE_SILENT_DAYS = 3        # silent this long → stale (the same clock the chase lane uses)
STALE_MAX_DAYS = 14          # …and no older: two weeks of silence is a dead thread, not a debt
STALE_MAX_THREADS = 20       # threads.get calls per gather, newest first

# --attendee-comms mode (meeting-prep): per-attendee Gmail searches, same concurrency pattern.
ATTENDEE_COMMS_CAP = 15        # unique attendee emails searched per run
ATTENDEE_COMMS_WORKERS = 5     # concurrent per-attendee searches
ATTENDEE_COMMS_MAX_PER = 5     # messages kept per attendee
ATTENDEE_THREAD_CAP = 2       # exact IDs from the current attendee search, focused prep only
ATTENDEE_THREAD_MESSAGES = 8  # opening two + latest six, never an unbounded thread dump
ATTENDEE_THREADS_TOTAL_CHARS = 24000  # one focused-run budget, shared across attendees/threads
ATTENDEE_THREAD_CHARS = 8000  # includes attribution; shared by both context consumers
ATTENDEE_COMMS_TIMEOUT = 30    # seconds per gmail search


def _diag(msg: str) -> None:
    """Persist to $SOTTO_DATA/logs/compose_brief.log (served at /debug/brief-log) so a 0-email gather
    is DIAGNOSABLE — execute_code stderr only reaches the agent, not Railway's logs. Best-effort."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
        from sotto_log import diag
        diag(msg)
    except Exception:
        print(msg, file=sys.stderr)


def _find_google_api():
    """Locate Sotto's packaged Google CLI: the image path, or its source-checkout twin."""
    source = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..',
                                          'adapters', 'sotto', 'google_api.py'))
    for candidate in (source, '/app/adapters/sotto/google_api.py'):
        if os.path.isfile(candidate):
            return candidate
    return None


class GoogleCLIError(RuntimeError):
    """Failure from the trusted Google provider CLI, distinct from local runtime errors."""


def _refresh_google_token():
    """Refresh through Sotto's locked compare-and-write before the provider CLI reads a token."""
    here = os.path.dirname(os.path.abspath(__file__))
    source = os.path.abspath(os.path.join(here, '..', '..', '..', 'adapters', 'sotto', 'google_setup.py'))
    for setup in (source, '/app/adapters/sotto/google_setup.py'):
        if os.path.isfile(setup):
            result = subprocess.run([sys.executable or 'python3', setup, '--check'],
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise GoogleCLIError('Google token check failed')
            return
    # The Google client must not silently refresh and overwrite a reconnect outside the
    # locked compare-and-write path.
    raise GoogleCLIError('Sotto Google setup implementation is missing')


_HTTP_STATUS = re.compile(r"(?:HttpError\s+|HTTP\s*|Error\s+|(?:http_)?status(?: code)?\s*[=: ]\s*)(403|429)\b", re.I)
_QUOTA_REASON = re.compile(r"\b(?:userRateLimitExceeded|rateLimitExceeded|quotaExceeded)\b|"
                           r"\bquota\s+exceeded\b|\bTotal Query Cost Units per minute per user\b", re.I)


def _source_error(exc):
    """Return only a bounded code; provider text never enters a source receipt."""
    response = getattr(exc, 'resp', None)
    status = getattr(response, 'status', None)
    if isinstance(exc, GoogleCLIError):
        match = _HTTP_STATUS.search(str(exc))
        status = int(match.group(1)) if match else None
    if status == 429 or (status == 403 and _QUOTA_REASON.search(str(exc))):
        return 'rate_limited'
    return type(exc).__name__


def _run(api, args, timeout=60):
    py = sys.executable or "python3"
    r = subprocess.run([py, api, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise GoogleCLIError(r.stderr.strip() or f"google_api {' '.join(args)} failed")
    from google_cli import decode_output
    return decode_output(r.stdout, args)


def _ensure_google_deps():
    """Guarantee google_api.py's interpreter can import googleapiclient. The image bakes it (Dockerfile),
    but if the brief's python (sys.executable here — the SAME one _run/google_api.py use) differs from the
    build python, the import is still missing → every fetch dies with ModuleNotFoundError and the brief
    falls to local-only. So self-heal ONCE in the exact interpreter, deterministically — instead of the
    agent improvising `pip install` mid-brief (which caused thin briefs + duplicate-retry sends).
    Best-effort: returns True if importable after the attempt.

    FAST PATH: an in-process import (we ARE sys.executable — the same interpreter _run launches
    google_api.py with), so the healthy case costs zero subprocess work. Only a missing module
    falls through to the pip self-heal (the load-bearing backstop — see CARRYOVER #532/#534).
    Setup runs `--ensure-deps` so the heal happens during onboarding, not mid-brief."""
    try:
        import googleapiclient  # noqa: F401
        return True
    except Exception:
        pass
    py = sys.executable or "python3"
    _diag("[gather_google] googleapiclient missing in the brief's python — installing once (deterministic)…")
    try:
        subprocess.run([py, "-m", "pip", "install", "--quiet", "--no-cache-dir",
                        "google-api-python-client", "google-auth", "google-auth-oauthlib",
                        "google-auth-httplib2"], capture_output=True, timeout=240)
        ok = subprocess.run([py, "-c", "import googleapiclient"], capture_output=True, timeout=15).returncode == 0
        _diag(f"[gather_google] googleapiclient install {'OK' if ok else 'FAILED'}")
        return ok
    except Exception as e:  # noqa: BLE001
        _diag(f"[gather_google] googleapiclient install error: {e}")
        return False


def _as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        for k in ("messages", "emails", "events", "items", "results"):
            if isinstance(v.get(k), list):
                return v[k]
    return []






def normalize_email(it: dict, full: dict) -> dict:
    """Map a Gmail item (google_api CLI OR raw API) → compose_brief's email shape.
    Tolerant of field-name variants so the SAME brief works whichever host provided the data."""
    labels = _pick(full, "labels", "labelIds") or _pick(it, "labels", "labelIds", "label_ids") or []
    labels = [str(x).upper() for x in labels] if isinstance(labels, list) else []
    normalized = {
        "id": _pick(it, "id", "message_id", "messageId"),
        "threadId": _pick(it, "threadId", "thread_id") or _pick(full, "threadId", "thread_id"),
        "from": _addr_str(_pick(full, "from", "sender", "from_address") or _pick(it, "from", "sender")),
        "to": _addr_str(_pick(full, "to", "recipient") or _pick(it, "to", "recipient")),
        "cc": _addr_str(_pick(full, "cc") or _pick(it, "cc")),
        "subject": _pick(full, "subject", "title") or _pick(it, "subject", "title") or "",
        "date": _pick(full, "date", "internalDate", "received_at") or _pick(it, "date", "internalDate", "received_at") or "",
        "snippet": _pick(it, "snippet", "preview", "body_preview") or _pick(full, "snippet", "preview") or "",
        "body": _pick(full, "body", "text", "content", "plain_text") or _pick(it, "body", "text") or "",
        "labelIds": labels,
        "isSent": "SENT" in labels,
    }
    # Native recipient evidence for owner-sent introductions; never infer it from body prose.
    bcc = _addr_str(_pick(full, 'bcc') or _pick(it, 'bcc'))
    if bcc:
        normalized['bcc'] = bcc
    if full.get('thread_context'):
        normalized['thread_context'] = full['thread_context']
    return normalized


def normalize_event(e: dict) -> dict:
    """Map a calendar event (google_api CLI OR raw API) → compose_brief's event
    shape (start as a string). Tolerant of field-name variants across hosts."""
    def _t(*keys):
        v = _pick(e, *keys)
        return (v.get("dateTime") or v.get("date") or "") if isinstance(v, dict) else (v or "")
    normalized = {
        "id": _pick(e, "id", "event_id", "eventId"),
        "summary": _pick(e, "summary", "title", "name") or "",
        "start": _t("start", "start_time", "startTime"),
        "end": _t("end", "end_time", "endTime"),
        "location": _pick(e, "location") or "",
        "description": _pick(e, "description", "notes") or "",
        "meetingLink": _pick(e, "hangoutLink", "meetingLink", "conferenceLink", "htmlLink", "link", "url") or "",
        "attendees": _pick(e, "attendees", "participants") or [],
        # The user's OWN answer to the invite — accepted / declined / tentative / needsAction — read
        # off the attendee flagged `self`. A meeting you declined is not on your day; one you
        # haven't answered is an ask. Nothing read this field before (Sep 2026).
        "my_response": my_response(e),
    }
    # Preserve exact provider linkage when present. These are opaque identifiers only; callers may
    # use an exact Gmail thread binding but must not infer one from a shared attendee or title.
    for key in ("threadId", "thread_id", "iCalUID", "recurringEventId", "originalStartTime",
                "status", "transparency", "eventType", "conferenceData", "hangoutLink", "htmlLink", "calendar_id", "organizer", "_calendar_primary"):
        if e.get(key) not in (None, "", [], {}):
            normalized[key] = e[key]
    if e.get('calendar_id') and normalized.get('id'):
        # Downstream gather/cache/offer identities are calendar-scoped. Typed Google reads
        # retain the raw provider id + calendar_id pair for the existing reviewed writer.
        import hashlib
        normalized['provider_event_id'] = normalized['id']
        normalized['id'] = 'gcal_' + hashlib.sha256(json.dumps([e['calendar_id'], normalized['id']], separators=(',', ':')).encode()).hexdigest()
    return normalized


def my_response(e: dict) -> str:
    """The self attendee's responseStatus, lowercased ("" when the event carries none)."""
    for a in (e.get("attendees") or []) if isinstance(e, dict) else []:
        if isinstance(a, dict) and (((e.get("calendar_id") in (None, "", "primary") or e.get("_calendar_primary") is True) and a.get("self")) or
                str(a.get("email") or "").lower() == configured_user_email() != ""):
            return str(a.get("responseStatus") or "").strip().lower()
    return ""


class GmailRows(list):
    """Usable rows plus per-search coverage facts lost by a plain list."""

    rate_limited = False
    capped = False


def _fetch_body(api, mid, quota_stop=None):
    """One full-message fetch; retain a snippet if this individual read fails."""
    if quota_stop is not None and quota_stop.is_set():
        return mid, None
    try:
        if _token_path():
            service = _gmail_service()
            try:
                return mid, _native_fetch_message(service, str(mid))
            finally:
                close = getattr(service, "close", None)
                if callable(close):
                    close()
        return mid, _run(api, ["gmail", "get", str(mid)], timeout=30)
    except Exception as error:
        if quota_stop is not None and _source_error(error) == 'rate_limited':
            quota_stop.set()
        return mid, None


def _fetch_thread_bodies(tid, mids, quota_stop):
    """Replace the cohort's message reads with one exact-thread read, not another round trip.

    The target messages retain their own bodies, IDs and attachment payloads. Older thread
    excerpts are separately labelled background, never evidence attributed to the current ID.
    """
    from gmail_read import message_fields
    from source_context import allowed
    if quota_stop.is_set() or not allowed('gmail'):
        return {}
    service = None
    try:
        service = _gmail_service()
        thread = service.users().threads().get(userId='me', id=tid, format='full').execute()
        if not allowed('gmail'):
            return {}
        context, inventory = _bounded_thread_context(thread, tid, set(mids), BRIEF_THREAD_CHARS,
                                                     rendered_ids=set(mids))
        if context is None:
            return {}
        messages = {str(m['id']): m for m in thread['messages'] if isinstance(m, dict)
                    and str(m.get('id')) in inventory and m.get('threadId') == tid}
        result = {mid: message_fields(messages[mid]) for mid in mids if mid in messages}
        if result:
            result[next(iter(result))]['thread_context'] = context
        return result
    except Exception as error:
        if _source_error(error) == 'rate_limited':
            quota_stop.set()
        return {}  # preserve the labelled partial search result; no hidden retry
    finally:
        if service is not None and callable(getattr(service, 'close', None)):
            try:
                service.close()
            except Exception:
                pass


# ── the attachment lane ─────────────────────────────────────────────────────────────────────────
# An attachment Sotto can read becomes text under its email; one it can't is named, never guessed.
#
# WHY THIS DOESN'T GO THROUGH THE HOST CLI — the same reason google_action.py's `gmail-draft`
# doesn't, and by the precedent that file set: the packaged Google CLI is a narrow compatibility
# surface with no attachments verb (its gmail verbs are search/get/send/reply), and its `gmail get`
# returns the flattened message rather than a client a MIME walk can page. The granted token
# already carries the Gmail read scope, so this reads straight from the Gmail API using the SAME
# token file the CLI authenticates with. `_gmail_service` below is that one shared client builder;
# google_action.py imports it from here rather than keeping a second copy.
#
# The native full-body read already returns the MIME payload, so the attachment lane reuses it and
# makes no second `messages().get`. Only parts Gmail hands back as an `attachmentId` need an
# `attachments().get`; an email with no attachments makes no attachment-only call.


def _token_path() -> str:
    """The Google token file — the SAME one google_api.py authenticates with
    ($SOTTO_HOME/google_token.json, written by Sotto's google_setup.py). "" when Google isn't
    connected."""
    return _shared_token_path()


def _gmail_service(*, timeout=None):
    return _shared_gmail_service(**({"timeout": timeout} if timeout is not None else {}))


def _google_service(api, version):
    """A client on the host's existing credentials. Scopes are NOT passed (setup.py's own
    rule: the user may have granted a subset, and passing them makes refresh fail with
    invalid_scope)."""
    return _shared_google_service(api, version)


def _attachment_parts(payload) -> list:
    """Every part of a Gmail MIME tree that carries a filename, depth-first in message order.

    A filename is what makes a part an attachment — an inline text/html alternative has none. The
    walk is recursive because multipart/mixed wrapping multipart/alternative is the ordinary shape
    of a real email, and a flat scan of `payload["parts"]` misses everything one level down."""
    out = []
    if not isinstance(payload, dict):
        return out
    name = str(payload.get("filename") or "").strip()
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    if name:
        out.append({"filename": name,
                    "mime": str(payload.get("mimeType") or ""),
                    "size": int(body.get("size") or 0),
                    "attachment_id": body.get("attachmentId") or "",
                    "inline_data": body.get("data") or ""})
    for part in payload.get("parts") or []:
        out.extend(_attachment_parts(part))
    return out


def _attachment_bytes(service, mid: str, part: dict) -> bytes:
    """The part's raw bytes: Gmail's inlined copy when it sent one, otherwise one
    `attachments().get`. b"" when neither is available."""
    raw = part.get("inline_data") or ""
    if not raw and part.get("attachment_id"):
        att = service.users().messages().attachments().get(
            userId="me", messageId=str(mid), id=str(part["attachment_id"])).execute()
        raw = (att or {}).get("data") or ""
    if not raw:
        return b""
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _fetch_attachments(service, mid: str, payload=None, quota_stop=None) -> list:
    """One message → its attachment rows, ready to hang on the normalized email.

    EVERY attachment is named. The first MAX_ATTACHMENTS_PER_EMAIL that are also under
    MAX_ATTACHMENT_BYTES are fetched and converted; the rest are named with the reason they weren't
    ("too large to read", "over the 3-attachment limit"). A message with no attachments costs
    nothing beyond the one metadata call and returns []."""
    if payload is None:
        msg = service.users().messages().get(userId="me", id=str(mid), format="full").execute()
        payload = (msg or {}).get("payload") or {}
    parts = _attachment_parts(payload)
    rows, converted = [], 0
    for part in parts:
        name = part["filename"]
        if quota_stop is not None and quota_stop.is_set():
            rows.append({"filename": name, "unreadable": "could not be downloaded"})
            continue
        if part["size"] > MAX_ATTACHMENT_BYTES:
            rows.append({"filename": name, "unreadable": "too large to read"})
            continue
        if converted >= MAX_ATTACHMENTS_PER_EMAIL:
            rows.append({"filename": name,
                         "unreadable": f"over the {MAX_ATTACHMENTS_PER_EMAIL}-attachment limit"})
            continue
        converted += 1
        try:
            data = _attachment_bytes(service, mid, part)
        except Exception as error:  # noqa: BLE001  (one failed file stays named)
            if quota_stop is not None and _source_error(error) == 'rate_limited':
                quota_stop.set()
            rows.append({"filename": name, "unreadable": "could not be downloaded"})
            continue
        rows.append(convert_attachment(name, data))
    return rows


def _attachments_for(mids: list, payloads=None, quota_stop=None) -> dict:
    """{message_id: [attachment rows]} for the bodies-cohort inbox messages that have any.

    NEVER RAISES. Google not connected, the client libs missing, an API error, one bad message —
    every one of them means the brief runs with fewer attachments, never that the gather dies. That
    is the fail-toward-silence bar; the brief's own source-availability line is where a broken
    source speaks up."""
    if not mids or not _token_path():
        return {}
    def _one(mid):
        if quota_stop is not None and quota_stop.is_set():
            return mid, []
        # googleapiclient's httplib2 transport is not thread-safe. A shared client
        # corrupted concurrent TLS reads in the first live Cloud gather (SIGSEGV).
        # Each worker owns and closes its connection; cohort ordering stays unchanged.
        service = None
        try:
            service = _gmail_service()
            payload = (payloads or {}).get(mid)
            if quota_stop is not None:
                return mid, _fetch_attachments(service, mid, payload, quota_stop=quota_stop)
            return mid, (_fetch_attachments(service, mid, payload) if payload is not None
                         else _fetch_attachments(service, mid))
        except Exception as error:  # noqa: BLE001
            if quota_stop is not None and _source_error(error) == 'rate_limited':
                quota_stop.set()
            return mid, []
        finally:
            if service is not None:
                close = getattr(service, "close", None)
                if callable(close):
                    close()

    out = {}
    with ThreadPoolExecutor(max_workers=min(BODY_FETCH_WORKERS, len(mids))) as ex:
        for mid, rows in ex.map(_one, mids):
            if rows:
                out[mid] = rows
    return out


def _search_gmail(api, query: str, max_n: int, bodies: int, timeout: int = 60,
                  attachments: bool = False, thread_context: bool = False):
    """One Gmail search → normalized rows, with full bodies for the top `bodies` hits (and, when
    `attachments` is on, their attachments converted to Markdown under them)."""
    items = _as_list(_run(api, ["gmail", "search", query, "--max", str(max_n)], timeout=timeout))
    # Snippets are thin; fetch full bodies for the top N — CONCURRENTLY (the pattern proven in
    # research_attendees.py). Sequentially this was up to N × 30s of the brief's wall clock.
    # Output order is preserved: `full` is a lookup, the emit loop below follows `items`.
    mids = [it.get("id") for it in items[:bodies] if it.get("id")]
    full = {}
    quota_stop = threading.Event()
    thread_groups = {}
    if thread_context and _token_path():
        for item in items[:bodies]:
            if item.get('id') and item.get('threadId'):
                thread_groups.setdefault(str(item['threadId']), []).append(item['id'])
        if thread_groups:
            with ThreadPoolExecutor(max_workers=min(BODY_FETCH_WORKERS, len(thread_groups))) as ex:
                for found in ex.map(lambda pair: _fetch_thread_bodies(*pair, quota_stop), thread_groups.items()):
                    full.update(found)
    covered = {mid for group in thread_groups.values() for mid in group}
    mids_without_thread = [mid for mid in mids if mid not in covered]
    if mids_without_thread:
        with ThreadPoolExecutor(max_workers=min(BODY_FETCH_WORKERS, len(mids_without_thread))) as ex:
            for mid, msg in ex.map(lambda m: _fetch_body(api, m, quota_stop), mids_without_thread):
                if msg is not None:
                    full[mid] = msg
    # The attachment lane rides the SAME cohort as the bodies: an email thin enough to be
    # snippet-only is not one the brief is reading closely enough to need its files. The fetches
    # run concurrently, then the per-brief budget is spent deterministically in cohort order —
    # concurrency decides when the bytes arrive, never who gets the budget.
    payloads = {mid: msg.get("payload") for mid, msg in full.items()
                if isinstance(msg, dict) and isinstance(msg.get("payload"), dict)}
    atts = (_attachments_for(mids, payloads, quota_stop)
            if attachments and not quota_stop.is_set() else {})
    if atts:
        budgeted = apply_attachment_budget([atts.get(m) or [] for m in mids])
        atts = {m: rows for m, rows in zip(mids, budgeted) if rows}
    rows = GmailRows()
    rows.rate_limited = quota_stop.is_set()
    rows.capped = max_n > 0 and len(items) >= max_n
    for it in items:
        if not isinstance(it, dict):
            continue
        e = normalize_email(it, full.get(it.get("id"), {}))
        if it.get('id') in covered and it.get('id') not in full:
            e['thread_context'] = 'Exact thread unavailable; only the current search excerpt was read.'
        # Only a native MIME read proves this is a complete body, not a search snippet.
        if isinstance((full.get(it.get('id')) or {}).get('payload'), dict):
            e['_body_complete'] = True
        got = atts.get(it.get("id"))
        if got:
            e["attachments"] = got
        rows.append(e)
    return rows


# A 1-day search answers in seconds; a 42-day × 400-result backfill (the Golden Corpus) paged past
# 60s on the owner's real mailbox and came back EMPTY. The window knows which one it is.
BACKFILL_TIMEOUT = 600
# One Gmail search clamps at ~500 results and one calendar list pages at ~25 events — a single
# query can never backfill a busy 42-day window (the owner's 800-max ask came back exactly 500,
# reaching ~5 days). Backfills slice the window into date-bounded queries and dedup by id.
GMAIL_SLICE_DAYS = 7
CAL_SLICE_DAYS = 2


def _window_timeout(days: int) -> int:
    return 60 if days <= 1 else BACKFILL_TIMEOUT


def _sliced_gmail(api, base_query: str, max_n: int, bodies: int, days: int,
                  attachments: bool = False):
    """Date-bounded weekly searches (after:/before:, upper bound exclusive), newest first, deduped
    by message id (boundary days overlap on purpose), bodies budget spread across slices so every
    week labels rich — not just the newest."""
    end = datetime.date.today() + datetime.timedelta(days=2)
    start = end - datetime.timedelta(days=days + 2)
    bounds = []
    hi = end
    while hi > start:
        lo = max(start, hi - datetime.timedelta(days=GMAIL_SLICE_DAYS))
        bounds.append((lo, hi))
        hi = lo
    per = max(0, bodies // len(bounds))
    out, seen = GmailRows(), set()
    for i, (lo, hi) in enumerate(bounds):
        q = f"{base_query} after:{lo:%Y/%m/%d} before:{hi:%Y/%m/%d}".strip()
        try:
            rows = _search_gmail(api, q, max_n, per + (bodies % len(bounds) if i == 0 else 0),
                                 timeout=BACKFILL_TIMEOUT, attachments=attachments)
        except Exception as error:
            if out and _source_error(error) == 'rate_limited':
                out.rate_limited = True
                break
            raise
        out.capped = out.capped or getattr(rows, 'capped', False)
        for e in rows:
            mid = str(e.get("id") or "")
            if mid and mid in seen:
                continue
            seen.add(mid)
            out.append(e)
        if getattr(rows, 'rate_limited', False):
            out.rate_limited = True
            break
    return out


def gather_gmail(api, max_n: int, bodies: int, days: int = 1, *, attachments: bool = True):
    """The inbox lane — the ONE lane that carries attachments (the sent lane below does not)."""
    if days <= 1:
        return _search_gmail(api, "newer_than:1d", max_n, bodies, attachments=attachments,
                             thread_context=True)
    return _sliced_gmail(api, "", max_n, bodies, days, attachments=attachments)


def mark_sent(e: dict) -> dict:
    """Force the sent markers on a row the `in:sent` QUERY already guarantees is outgoing.

    Both downstream readers accept either signal — style_extract._adapt_gmail and
    render_local._is_sent_email each test `isSent` OR "SENT" in labels — but search results from
    some hosts carry no labels at all, which would leave a sent message looking inbound. The query
    is the ground truth here, so set BOTH and the two shapes can never disagree."""
    e["isSent"] = True
    labels = e.get("labelIds") or []
    if not isinstance(labels, list):
        labels = []
    if "SENT" not in labels:
        labels = [*labels, "SENT"]
    e["labelIds"] = labels
    return e


def gather_sent(api, max_n: int = SENT_MAX, bodies: int = SENT_BODIES, days: int = 1):
    """The user's own outgoing mail from the window — same normalized shape as the inbox rows,
    with isSent/SENT guaranteed. Failures are the caller's to swallow (the gather never dies on it).

    NO ATTACHMENTS, deliberately: this lane exists to teach the style fingerprint the user's email
    VOICE and to close loops against what they sent. Converting the files they attached to their own
    mail would cost API calls and prompt budget to tell them what they already know."""
    rows = (_search_gmail(api, "in:sent newer_than:1d", max_n, bodies) if days <= 1
            else _sliced_gmail(api, "in:sent", max_n, bodies, days))
    result = GmailRows(mark_sent(e) for e in rows)
    result.rate_limited = getattr(rows, 'rate_limited', False)
    result.capped = getattr(rows, 'capped', False)
    return result


def merge_sent(inbox: list, sent: list) -> list:
    """inbox + sent, deduped by message id. `newer_than:1d` has no `in:` operator, so Gmail already
    returns some sent mail in the inbox lane — a naive concat would double-count those (and hand
    style_extract the same sample twice). Rows already present keep their position but GAIN the sent
    markers, since the sent lane's query is the authoritative signal for direction."""
    by_id = {}
    for e in inbox:
        if isinstance(e, dict) and e.get("id"):
            by_id[str(e["id"])] = e
    out = list(inbox)
    for e in sent:
        if not isinstance(e, dict):
            continue
        prior = by_id.get(str(e.get("id"))) if e.get("id") else None
        if prior is not None:
            mark_sent(prior)
            # the sent lane may have fetched a body the inbox lane didn't
            if e.get("body") and not prior.get("body"):
                prior["body"] = e["body"]
            continue
        out.append(e)
    return out


# ── the stale sent lane ─────────────────────────────────────────────────────────────────────────

def _header(msg: dict, name: str) -> str:
    for h in ((msg.get("payload") or {}).get("headers") or []):
        if isinstance(h, dict) and str(h.get("name", "")).lower() == name.lower():
            return str(h.get("value") or "")
    return ""


def stale_from_threads(candidates: list, threads: dict, now=None) -> list:
    """The pure half: `candidates` are normalized sent rows (newest first), `threads` maps
    threadId → the Gmail threads.get(format=metadata) payload. A thread is STALE when its last
    message is still the user's (SENT label) and that message is at least STALE_SILENT_DAYS old.
    Output rows are what compose_brief and render_local read: {threadId, to, toEmail, subject,
    sentDate, daysSinceSent, snippet}, oldest silence first."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out, seen = [], set()
    for row in candidates:
        tid = str(row.get("threadId") or "")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        thread = threads.get(tid) or {}
        msgs = [m for m in (thread.get("messages") or []) if isinstance(m, dict)]
        if not msgs:
            continue
        msgs.sort(key=lambda m: int(m.get("internalDate") or 0))
        last = msgs[-1]
        labels = {str(x).upper() for x in (last.get("labelIds") or [])}
        if "SENT" not in labels:
            continue                          # somebody answered after the user's last word
        try:
            sent_at = datetime.datetime.fromtimestamp(int(last.get("internalDate") or 0) / 1000.0,
                                                      tz=datetime.timezone.utc)
        except (ValueError, OverflowError, OSError):
            continue
        days = (now - sent_at).days
        if days < STALE_SILENT_DAYS or days > STALE_MAX_DAYS:
            continue
        to = _header(last, "To") or _addr_str(row.get("to"))
        cc = _header(last, "Cc")
        addrs = [a for _n, a in getaddresses([h for h in (to, cc) if h.strip()]) if a]
        if not addrs:
            continue
        out.append({"threadId": tid, "to": to, "toEmail": addrs[0].lower(), "cc": cc,
                    "subject": _header(last, "Subject") or str(row.get("subject") or ""),
                    "sentDate": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "daysSinceSent": days,
                    "snippet": str(last.get("snippet") or row.get("snippet") or "")[:300]})
    out.sort(key=lambda r: -int(r["daysSinceSent"]))
    return out


def gather_stale_sent(api, service=None, now=None) -> list:
    """The I/O half: the bounded `in:sent` search through the host CLI, then threads.get(metadata)
    per candidate through the same Gmail client the attachment lane uses. Any failure is the
    caller's to swallow — a stale lane that breaks costs the lane, never the brief."""
    q = f"in:sent older_than:{STALE_SILENT_DAYS}d newer_than:{STALE_MAX_DAYS}d -in:chats"
    items = _as_list(_run(api, ["gmail", "search", q, "--max", str(STALE_MAX_THREADS * 2), "--ids-only"]))
    rows = [normalize_email(it, {}) for it in items if isinstance(it, dict)]
    tids = []
    for r in rows:
        tid = str(r.get("threadId") or "")
        if tid and tid not in tids:
            tids.append(tid)
    tids = tids[:STALE_MAX_THREADS]
    if not tids:
        return GmailRows()
    svc = service or _gmail_service()
    threads, rate_limited = {}, False
    try:
        for tid in tids:
            try:
                thread = svc.users().threads().get(
                    userId="me", id=tid, format="metadata",
                    metadataHeaders=["From", "To", "Cc", "Subject"]).execute()
            except Exception as error:  # noqa: BLE001 — one unreadable thread costs only itself
                if _source_error(error) == 'rate_limited':
                    rate_limited = True
                    break
                continue
            # Most candidates need only the last message, already in threads.get.
            # Keep the original candidate fallback when that message lacks a field.
            row = next(row for row in rows if str(row.get('threadId') or '') == tid)
            messages = [m for m in ((thread or {}).get('messages') or []) if isinstance(m, dict)]
            if messages:
                last = max(messages, key=lambda m: int(m.get('internalDate') or 0))
                if not (_header(last, 'To') and _header(last, 'Subject') and last.get('snippet')):
                    candidate = next((m for m in messages if m.get('id') == row.get('id')), None)
                    if (candidate is None
                            or (not _header(last, 'To') and not _header(candidate, 'To'))
                            or (not _header(last, 'Subject') and not _header(candidate, 'Subject'))
                            or (not last.get('snippet') and not candidate.get('snippet'))):
                        try:
                            candidate = svc.users().messages().get(userId='me', id=str(row['id']), format='metadata',
                                metadataHeaders=['From', 'To', 'Subject', 'Date']).execute()
                        except Exception as error:
                            if _source_error(error) == 'rate_limited':
                                rate_limited = True
                                break  # no incomplete fallback, and no later provider reads
                            raise
                    row['to'] = _header(candidate, 'To')
                    row['subject'] = _header(candidate, 'Subject')
                    row['snippet'] = str(candidate.get('snippet') or '')
            threads[tid] = thread
    finally:
        if service is None:
            close = getattr(svc, 'close', None)
            if callable(close):
                close()
    result = GmailRows(stale_from_threads(rows, threads, now))
    result.rate_limited = rate_limited
    result.threads = threads  # current-gather inventory only; never serialized or cached on disk
    return result


def gather_calendar(api, back_days: int = 0, service=None, observation=None):
    """Next 3 days, plus `back_days` of history — the daily gather looks only forward; the Golden
    Corpus backfill (--window-days) needs the meetings that already happened. History comes in
    CAL_SLICE_DAYS windows deduped by id: the calendar CLI pages at ~25 events per list."""
    from calendar_context import meeting_events

    now = datetime.datetime.now(datetime.timezone.utc)
    end = now + datetime.timedelta(days=3)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    if observation is not None:
        observation.update(coverage={"since": (now - datetime.timedelta(days=max(0, back_days))).strftime(fmt),
                                     "until": end.strftime(fmt)}, complete=False)
    # Read through the same granted credentials as attachments, keeping Google's complete
    # event shape (attendees/RSVPs, every page) through our existing normalizer. The CLI
    # remains the path when no token file is present.
    if service is not None or _token_path():
        owned = service is None
        service = service if service is not None else _google_service('calendar', 'v3')
        try:
            from google_calendar_read import read_service
            rows = read_service(service, (now - datetime.timedelta(days=max(0, back_days))).strftime(fmt), end.strftime(fmt))
            if observation is not None:
                observation.update(complete=rows.complete, calendars=rows.calendars)
            return meeting_events([normalize_event(raw) for raw in rows], configured_user_email())
        finally:
            if owned:
                service.close()
    if back_days <= 0:
        value = _run(api, ["calendar", "list", "--start", now.strftime(fmt), "--end", end.strftime(fmt)])
        if observation is not None:
            observation['complete'] = not isinstance(value, dict) or value.get('complete') is True
        return meeting_events([normalize_event(e) for e in _as_list(value)], configured_user_email())
    out, seen = [], set()
    if observation is not None:
        observation['complete'] = True
    lo = now - datetime.timedelta(days=back_days)
    while lo < end:
        hi = min(lo + datetime.timedelta(days=CAL_SLICE_DAYS), end)
        value = _run(api, ["calendar", "list", "--start", lo.strftime(fmt), "--end", hi.strftime(fmt)], timeout=BACKFILL_TIMEOUT)
        if observation is not None and isinstance(value, dict) and value.get('complete') is not True:
            observation['complete'] = False
        for e in (normalize_event(x) for x in _as_list(value)):
            key = str(e.get("id") or "") or json.dumps(e, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
        lo = hi
    return meeting_events(out, configured_user_email())


def _attendee_emails_from_file(path: str) -> list:
    """Unique lowercased attendee emails (input order, capped at ATTENDEE_COMMS_CAP) from an
    attendee-list file. Accepts BOTH shapes the skills produce:
      • /tmp/sotto_research_in.json — [{name,email}, …] (select_attendees.py; {"attendees":[…]} too)
      • a calendar-derived list — events carrying attendees:[{email,…}] (gather_google's own
        /tmp/sotto_cal.json shape), or bare "a@b.com" strings.
    Unreadable/empty file → []."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("attendees") or data.get("events") or data.get("items") or []
    if not isinstance(data, list):
        return []
    out, seen = [], set()

    def _add(em):
        em = (em or "").strip().lower() if isinstance(em, str) else ""
        if em and "@" in em and em not in seen:
            seen.add(em)
            out.append(em)

    for it in data:
        if isinstance(it, str):
            _add(it)
        elif isinstance(it, dict):
            if it.get("email"):
                _add(it.get("email"))
            else:  # calendar event shape — pull each attendee's email
                for a in it.get("attendees") or []:
                    _add(a.get("email") if isinstance(a, dict) else a)
    if len(out) > ATTENDEE_COMMS_CAP:
        _diag(f"[gather_google] attendee-comms capping {len(out)} → {ATTENDEE_COMMS_CAP}")
    return out[:ATTENDEE_COMMS_CAP]


def _sender_mute():
    """The owner's explicit sender mutes as a predicate, via the same matcher compose applies.

    None when nothing is muted; unreadable preferences mute nothing, as in compose."""
    try:
        import preferences  # noqa: PLC0415
        muted = list(preferences.load_explicit().get('mute_senders') or [])
    except Exception:  # noqa: BLE001
        return None
    from textutil import _sender_addr
    return (lambda sender: preferences.sender_is_muted(_sender_addr(sender), muted)) if muted else None


def _bounded_thread_context(thread, tid, expected, thread_chars, rendered_ids=frozenset(), *, muted_names=()):
    """One shared, dated thread excerpt for prep and brief readers; never action authority."""
    from gmail_read import message_fields
    from personal_context import conversation_message
    if not isinstance(thread, dict) or thread.get('id') != tid:
        return None, set()
    messages = thread.get('messages')
    if not isinstance(messages, list):
        return None, set()
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    usable = []
    for message in messages:
        if (not isinstance(message, dict) or message.get('threadId') != tid
                or not message.get('id') or {'TRASH', 'SPAM', 'DRAFT'} & set(message.get('labelIds') or [])):
            continue
        stamp = str(message.get('internalDate') or '')
        if not stamp.isdigit() or int(stamp) > now:
            continue
        usable.append(message)
    usable.sort(key=lambda message: int(message['internalDate']))
    inventory = {str(message['id']) for message in usable}
    excluded = len(usable) != len(messages) or not expected <= inventory
    if muted_names:
        def name_muted(message):
            fields = message_fields(message)
            return any(name.strip().casefold() in muted_names for name, _ in getaddresses([
                _addr_str(fields.get(key)) for key in ('from', 'to', 'cc')]))
        audible = [message for message in usable if not name_muted(message)]
        excluded = excluded or len(audible) != len(usable)
        usable = audible
    muted = _sender_mute()
    if muted is not None:
        # A muted sender's words never reach a prep or brief reader through thread background,
        # before rendering; the owner's own SENT messages are never muted.
        audible = [message for message in usable if 'SENT' in (message.get('labelIds') or [])
                   or not muted(_addr_str(message_fields(message).get('from')))]
        excluded = excluded or len(audible) != len(usable)
        usable = audible
    # The brief already renders these exact messages in full. Spend the background
    # budget on the missing conversation, including middle-message agreements.
    usable = [message for message in usable if str(message['id']) not in rendered_ids]
    limit = min(ATTENDEE_THREAD_MESSAGES, max(2, thread_chars // 400))
    opening = 2 if limit >= 4 else 1
    selected = usable if len(usable) <= limit else usable[:opening] + usable[-(limit-opening):]
    if not selected:
        return (('Exact thread background partial; some messages were withheld.' if excluded else '')
                if inventory else None), inventory
    clipped = len(selected) != len(usable) or excluded
    blocks = []
    # Equal bounded space keeps the opening and newest correction even on a long thread.
    budget = (thread_chars - 200) // len(selected)
    for raw in selected:
        full = message_fields(raw)
        sender = _addr_str(full.get('from'))[:200]
        direction = 'owner sent' if 'SENT' in (full.get('labels') or []) else 'sender ' + sender
        header = f"email {str(full.get('date') or '')[:40]} [{raw['id']}] ({direction}): "
        body = str(full.get('body') or full.get('snippet') or '')
        if len(header) > budget - 64:
            header = header[:budget - 64]
            clipped = True
        room = budget - len(header) - 1
        excerpt = conversation_message({'source': 'gmail', 'id': raw['id'], 'body': body},
                                       text_limit=room, preserve_ending=True)
        clipped = clipped or excerpt['text_clipped']
        blocks.append(header + excerpt['text'])
    heading = 'Exact Gmail thread, bounded read' + (' (partial; some content omitted)' if clipped else '') + ':\n'
    return heading + '\n'.join(blocks), inventory


def _expand_attendee_threads(rows, quota_stop, thread_chars=ATTENDEE_THREAD_CHARS, *, deadline=None, known_context=None, sources=('gmail',), observation=None, muted_names=(), rendered_ids=frozenset()):
    """Reuse the native thread reader for bounded focused-prep context, no retained cache."""
    from source_context import allowed
    permitted = lambda: all(allowed(source) for source in sources)
    if not permitted():
        return []
    tids = list(dict.fromkeys(row['thread_id'] for row in rows if row.get('thread_id')))[:ATTENDEE_THREAD_CAP]
    expanded, inventories, partial, service = {}, {}, set(tids), None
    try:
        for tid in tids:
            if not permitted():
                return []
            if (known_context or {}).get(tid):
                expanded[tid] = known_context[tid][:thread_chars]
                inventories[tid] = None  # reuse provides no native inventory; preserve sibling snippets
                partial.discard(tid)
                if observation is not None:
                    observation['reused_threads'] = observation.get('reused_threads', 0) + 1
                continue
            if quota_stop.is_set() or (deadline is not None and time.monotonic() >= deadline):
                continue  # A later exact cached context needs no new read or time allowance.
            try:
                if deadline is not None:
                    if service is not None:
                        service.close()
                    service = _gmail_service(timeout=max(0.1, deadline - time.monotonic()))
                elif service is None:
                    service = _gmail_service()  # each attendee worker owns its transport
                if not permitted():
                    return []
                if observation is not None:
                    observation['requests'] += 1
                thread = service.users().threads().get(userId='me', id=tid, format='full').execute()
                if not permitted():
                    return []
                context, inventory = _bounded_thread_context(thread, tid,
                    {row.get('message_id') for row in rows if row.get('thread_id') == tid and row.get('message_id')},
                    thread_chars, rendered_ids=rendered_ids, muted_names=muted_names)
                if context is None:
                    continue
                expanded[tid] = context
                inventories[tid] = inventory
                partial.discard(tid)
            except Exception as error:
                if observation is not None:
                    observation['thread_reads_failed'] = observation.get('thread_reads_failed', 0) + 1
                if _source_error(error) == 'rate_limited':
                    quota_stop.set()
                    break
                # Preserve current search snippets, clearly marked as incomplete.
    finally:
        if service is not None and callable(getattr(service, 'close', None)):
            try:
                service.close()
            except Exception:
                pass
    if not permitted():
        return []
    result, seen = [], set()
    for row in rows:
        tid = row.get('thread_id')
        if tid in expanded:
            if tid in seen:
                if inventories[tid] is not None:
                    if row.get('message_id') in inventories[tid]:
                        continue
                    row = {**row, 'thread_context': 'Thread read omitted this message; current search snippet only.'}
            else:
                seen.add(tid)
                row = {**row, 'thread_context': expanded[tid]}
        elif tid in partial:
            row = {**row, 'thread_context': 'Full thread unavailable; only the current search snippet was read.'}
        result.append(row)
    return result


def _fetch_attendee_comms(api, email: str, *, expand_threads=False, quota_stop=None, thread_chars=ATTENDEE_THREAD_CHARS) -> tuple:
    """One per-attendee Gmail search (30d window, both directions). A failure just means that
    attendee gets no thread context. Only SENT proves the owner authored a message; another
    participant in an introduction thread is not the owner. Unattributed direction stays null,
    with the actual sender retained for attribution and mute checks."""
    if expand_threads:
        from source_context import allowed
        if not allowed('gmail') or quota_stop is not None and quota_stop.is_set():
            return email, []
    try:
        query = (f"{{from:{email} to:{email} cc:{email}}} newer_than:30d -in:trash -in:spam" if expand_threads
                 else f"{{from:{email} to:{email}}} newer_than:30d")
        items = _as_list(_run(api, ["gmail", "search", query,
                                    "--max", str(ATTENDEE_COMMS_MAX_PER)],
                              timeout=ATTENDEE_COMMS_TIMEOUT))
    except Exception:
        return email, []
    rows = _attendee_comms_rows(items, {email})
    return email, _expand_attendee_threads(rows, quota_stop or threading.Event(), thread_chars) if expand_threads else rows


def _attendee_comms_rows(items, emails):
    """The same attributed search projection for focused prep and ordinary brief background."""
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        labels = _pick(it, "labels", "labelIds", "label_ids") or []
        labels = [str(x).upper() for x in labels] if isinstance(labels, list) else []
        senders = getaddresses([_addr_str(_pick(it, "from", "sender", "from_address") or "")])
        name, sender = senders[0] if len(senders) == 1 else ('', '')
        sender = sender.lower().strip()
        row = {
            "date": str(_pick(it, "date", "internalDate", "received_at") or ""),
            "subject": str(_pick(it, "subject", "title") or ""),
            "snippet": str(_pick(it, "snippet", "preview", "body_preview") or ""),
            "from_me": True if "SENT" in labels else (False if sender in emails else None),
            "sender_name": name,
            "sender_identifier": sender,
        }
        message_id = str(_pick(it, "id", "messageId", "message_id") or "")
        thread_id = str(_pick(it, "threadId", "thread_id") or "")
        if message_id:
            row['message_id'] = message_id
        if thread_id:
            row['thread_id'] = thread_id
        rows.append(row)
    rows = rows[:ATTENDEE_COMMS_MAX_PER]
    return rows


def gather_attendee_comms(api, attendees_path: str, *, expand_threads=False) -> dict:
    """{attendee_email: [{date,subject,snippet,from_me}]} for every unique attendee email in the
    list file (cap 15), searched concurrently. Attendees whose search found nothing are omitted."""
    emails = _attendee_emails_from_file(attendees_path)
    if not emails:
        return {}
    out = {}
    quota_stop = threading.Event()
    thread_chars = min(ATTENDEE_THREAD_CHARS, ATTENDEE_THREADS_TOTAL_CHARS // (len(emails) * ATTENDEE_THREAD_CAP))
    with ThreadPoolExecutor(max_workers=min(ATTENDEE_COMMS_WORKERS, len(emails))) as ex:
        for email, rows in ex.map(lambda e: _fetch_attendee_comms(api, e, expand_threads=expand_threads, quota_stop=quota_stop, thread_chars=thread_chars), emails):
            if rows:
                out[email] = rows
    if expand_threads:
        from source_context import allowed
        if not allowed('gmail'):
            return {}
    return out


def gather_brief_meeting_context(api, events, known_messages=()):
    """One optional 15s best-effort deadline: one search and at most two thread reads total.

    This is dated background for the existing brief model, never native action evidence.
    Calendar chooses the cohort, so both grants gate every read and later projection.
    """
    from calendar_context import accepted_or_organized, prep_meeting_events, work_attendees
    from source_context import allowed
    from render_local import _meeting_context_mutes
    from preferences import sender_is_muted
    import relevance
    mutes = _meeting_context_mutes()
    now = datetime.datetime.now(datetime.timezone.utc)
    deadline = time.monotonic() + 15
    permitted = lambda: allowed('calendar') and allowed('gmail')
    receipt = {'status': 'partial', 'complete': False, 'requests': 0, 'attendees': 0,
               'sampled_attendees': 0, 'messages': 0}
    owner = configured_user_email()
    targets, attendees = [], []
    for event in prep_meeting_events(events, owner):
        if (not isinstance(event.get('id'), str) or not event['id'].strip()
                or not accepted_or_organized(event, owner)):
            continue
        try:
            start = datetime.datetime.fromisoformat(str(event.get('start') or '').replace('Z', '+00:00'))
            if start.tzinfo is None or not 0 <= (start - now).total_seconds() <= 72 * 3600:
                continue
        except ValueError:
            continue
        people = [person for person in work_attendees(event, owner)
                  if person['name'].strip().casefold() not in mutes['mute_people']
                  and not sender_is_muted(person['email'], mutes['mute_senders'])
                  and not any(char in person['email'] for char in ('\\', '"', '{', '}', ':'))]
        if people:
            targets.append(({'event_id': event.get('id'), 'start': event.get('start'),
                             'title': str(event.get('summary') or '')[:200]}, {p['email'] for p in people}))
            attendees.extend(person['email'] for person in people)
    attendees = list(dict.fromkeys(attendees))
    receipt['attendees'] = len(attendees)
    sampled = attendees[:ATTENDEE_COMMS_CAP]
    receipt['sampled_attendees'] = len(sampled)
    receipt['omitted_attendees'] = max(0, len(attendees) - len(sampled))
    if not sampled or not permitted():
        receipt['status'] = 'not_applicable' if not sampled else 'disabled'
        return None, receipt
    # Only unquoted, non-operator work addresses reach this quoted Gmail query.
    terms = ['%s:%s' % (field, json.dumps(email)) for email in sampled for field in ('from', 'to', 'cc')]
    query = '{' + ' '.join(terms) + '} newer_than:30d -in:trash -in:spam'
    try:
        receipt['requests'] = 1
        items = _as_list(_run(api, ['gmail', 'search', query, '--max', str(ATTENDEE_COMMS_MAX_PER)],
                              timeout=max(0.1, deadline - time.monotonic())))
        if not permitted():
            return None, {**receipt, 'status': 'disabled'}
        selected_items, matches = [], []
        native_ids = {str(row.get('id')) for row in known_messages if isinstance(row, dict) and row.get('id')}
        mute = _sender_mute()
        for item in items[:ATTENDEE_COMMS_MAX_PER]:
            if (not isinstance(item, dict) or str(item.get('id') or '') in native_ids
                    or relevance.is_automated_assistant(item)
                    or (mute and mute(_addr_str(item.get('from'))))):
                continue
            addresses = {address.casefold() for _, address in getaddresses([
                _addr_str(item.get(field)) for field in ('from', 'to', 'cc')])}
            matched = addresses.intersection(sampled)
            if matched:
                selected_items.append(item)
                matches.append(sorted(matched))
        rows = _attendee_comms_rows(selected_items, set(sampled))
        for row, matched in zip(rows, matches):
            row['attendee_identifiers'] = matched
        known = {} if any(mutes.values()) else {str(row.get('threadId')): row['thread_context'] for row in known_messages
                 if isinstance(row, dict) and row.get('threadId') and row.get('thread_context')}
        # The shared reader receives only this aggregate cohort; its two-thread cap is not multiplied.
        rows = _expand_attendee_threads(rows, threading.Event(), deadline=deadline, known_context=known,
                                       sources=('calendar', 'gmail'), observation=receipt, muted_names=mutes['mute_people'], rendered_ids=native_ids)
        if not permitted():
            return None, {**receipt, 'status': 'disabled', 'messages': 0}
        if _meeting_context_mutes() != mutes:
            return None, {**receipt, 'status': 'unavailable', 'error': 'preferences_changed', 'messages': 0}
        receipt['messages'] = len(rows)
        receipt['message_limit_reached'] = len(items) >= ATTENDEE_COMMS_MAX_PER
        receipt['thread_limit'] = ATTENDEE_THREAD_CAP
        receipt['omitted_threads'] = max(0, len({row['thread_id'] for row in rows if row.get('thread_id')})
                                         - ATTENDEE_THREAD_CAP)
        receipt['budget_exhausted'] = time.monotonic() >= deadline
        selected_targets = [event for event, addresses in targets if addresses.intersection(sampled)]
        receipt['omitted_events'] = len(targets) - len(selected_targets[:ATTENDEE_COMMS_CAP])
        context = {'sources': ['calendar', 'gmail'], 'mutes': mutes, 'events': selected_targets[:ATTENDEE_COMMS_CAP],
                   'messages': rows, 'receipt': receipt}
        # Bound all excerpt and metadata characters together, retaining an honest partial receipt.
        while len(json.dumps(context, ensure_ascii=False)) > ATTENDEE_THREADS_TOTAL_CHARS and rows:
            rows.pop()
            receipt['messages'] = len(rows)
            receipt['content_omitted'] = True
        return context, receipt
    except Exception as error:
        return None, {**receipt, 'status': 'unavailable', 'error': _source_error(error)}


CONTINUATION_REJECTED_REASONS = frozenset({
    'INVALID_ARGUMENT', 'FAILED_PRECONDITION', 'invalidArgument', 'failedPrecondition',
    'invalid', 'badRequest'})


def _continuation_rejected(error, page_token) -> bool:
    """Google rejected the page token we resumed from: the window is fine, its cursor is dead.

    Read only the structured status/reason — a provider message can quote private content, so it
    never leaves this function. Without a page token nothing in the request can have gone stale,
    so a 400 there is an ordinary deterministic rejection of the request itself.
    """
    if not page_token:
        return False
    status = getattr(getattr(error, 'resp', None), 'status', None)
    status = status if status is not None else getattr(error, 'status_code', None)
    try:
        if int(status) != 400:
            return False
        detail = json.loads(getattr(error, 'content', None) or b'{}').get('error') or {}
        reasons = {str(detail.get('status') or '')}
        reasons.update(str(item.get('reason') or '') for item in detail.get('errors') or []
                       if isinstance(item, dict))
    except (AttributeError, TypeError, ValueError):
        return False
    return bool(reasons & CONTINUATION_REJECTED_REASONS)


def history_page(since: int, until: int, page_token: str = '', limit: int = 100, service=None, now=None):
    """Frozen-window Gmail history through the existing credential builder; resume real API pages.

    Both directions are searched together. No attachments or writes. A failed get fails the page,
    so the caller never advances its checkpoint over missing messages.
    """
    from source_context import HistoryContinuationError, HistoryProtocolError, allowed
    if not allowed('gmail'):
        raise RuntimeError('Gmail history is not enabled')
    epoch = int((now or datetime.datetime.now(datetime.timezone.utc)).timestamp())
    if (not all(isinstance(v, int) and not isinstance(v, bool) for v in (since, until, limit))
            or not 1 <= limit <= 100 or since > until or since < epoch - 366*86400 or until > epoch + 60):
        # The same typed rejection the Bridge adapter raises, so one deterministic protocol
        # failure has one implementation and the caller parks it instead of retrying hourly.
        raise HistoryProtocolError('invalid_history_window')
    own_service = service is None
    service = service or _gmail_service()
    try:
        params = {'userId': 'me', 'q': f'after:{since} before:{until + 1}', 'maxResults': limit}
        if page_token:
            params['pageToken'] = page_token
        try:
            page = service.users().messages().list(**params).execute()
        except Exception as error:
            if _continuation_rejected(error, page_token):
                raise HistoryContinuationError() from error
            raise
        messages = []
        for item in page.get('messages', []):
            full = _native_fetch_message(service, item['id'])
            at = datetime.datetime.fromtimestamp(int(full['internalDate']) / 1000, datetime.timezone.utc)
            if not since <= at.timestamp() < until + 1:
                continue
            full.update({'body': full.get('body', '')[:8000], 'date': at.isoformat()})
            messages.append(normalize_email(full, full))
        if not allowed('gmail'):
            raise RuntimeError('Gmail consent changed during history read')
        return {'source': 'gmail', 'status': 'ok', 'rows': messages,
                'next_cursor': page.get('nextPageToken'), 'complete': not page.get('nextPageToken'),
                'since': since, 'until': until}
    finally:
        if own_service:
            service.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gmail-out", default="/tmp/sotto_gmail.json")
    ap.add_argument("--cal-out", default="/tmp/sotto_cal.json")
    ap.add_argument("--source-results-out", help="write per-source status/completeness receipts beside legacy arrays")
    ap.add_argument("--pulse-since-epoch", type=int,
                    help="bounded attended pulse refresh: one both-direction metadata query since this observation")
    ap.add_argument("--loop-local", help="effective local snapshot for bounded open-obligation evidence refresh")
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--bodies", type=int, default=12)
    ap.add_argument("--window-days", dest="window_days", type=int, default=1,
                    help="how many days back to gather (default 1 — the daily brief window). "
                         "The Golden Corpus backfill uses e.g. 42; calendar gains the same "
                         "history alongside its usual 3-day lookahead.")
    ap.add_argument("--skip-gmail", action="store_true", help="calendar only (e.g. meeting-prep)")
    ap.add_argument("--skip-calendar", action="store_true", help="gmail only")
    ap.add_argument("--sent-max", type=int, default=SENT_MAX,
                    help=f"cap for the in:sent lane (default {SENT_MAX}; 0 disables it)")
    ap.add_argument("--skip-sent", action="store_true",
                    help="skip the in:sent lane (the email-voice/draft-diff exhaust)")
    ap.add_argument("--skip-stale", action="store_true",
                    help="skip the stale-sent lane (emails you sent that nobody answered)")
    ap.add_argument("--ensure-deps", action="store_true",
                    help="ONLY run the googleapiclient self-heal (setup-time), no gather")
    ap.add_argument("--attendee-comms", dest="attendee_comms",
                    help="attendee-list JSON ([{name,email}] or calendar-derived) — ONLY gather "
                         "per-attendee Gmail threads (30d, both directions) to --comms-out")
    ap.add_argument("--expand-threads", action="store_true", help="focused prep: bounded exact Gmail thread context")
    ap.add_argument("--comms-out", dest="comms_out", default="/tmp/sotto_attendee_comms.json",
                    help="output file for --attendee-comms (default /tmp/sotto_attendee_comms.json)")
    a = ap.parse_args()
    from source_context import allowed, source_result
    import jsonstore
    observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    source_results = {source: source_result('skipped' if skipped else 'unavailable', observed_at=observed_at)
                      for source, skipped in (('gmail', a.skip_gmail), ('calendar', a.skip_calendar))}

    def write_source_results():
        if a.source_results_out:
            jsonstore.write_atomic(a.source_results_out, source_results)

    def mark_gmail_lane_rate_limited():
        # A sent/stale lane hit the quota after the inbox read. Merge, never replace: a complete
        # inbox becomes partial but keeps its coverage window; a failed inbox keeps its own status
        # and error; an existing, more specific error wins.
        result = source_results['gmail']
        if result['status'] == 'ok':
            result.update(status='partial', complete=False, error='rate_limited')
        elif result['status'] == 'partial':
            result.setdefault('error', 'rate_limited')

    if a.pulse_since_epoch is not None:
        # Foreground pulses reuse canonical memory; history traversal stays in memory_cycle.
        # One metadata-only query, both directions, with a strict child-process deadline.
        epoch = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        if not epoch - 366 * 86400 <= a.pulse_since_epoch <= epoch:
            ap.error('pulse observation is out of bounds')
        rows, receipt = [], source_result('unavailable', observed_at=observed_at)
        if allowed('gmail'):
            try:
                api = _find_google_api()
                if not api:
                    raise GoogleCLIError('Google reader unavailable')
                rows = _search_gmail(api, f'after:{a.pulse_since_epoch} before:{epoch + 1}',
                                     PULSE_REFRESH_MAX_MESSAGES, 0, timeout=PULSE_REFRESH_TIMEOUT_SECONDS)
                capped = bool(getattr(rows, 'capped', len(rows) >= PULSE_REFRESH_MAX_MESSAGES))
                receipt = source_result('partial' if capped else 'ok', observed_at=observed_at,
                    complete=not capped, since=datetime.datetime.fromtimestamp(
                        a.pulse_since_epoch, datetime.timezone.utc).isoformat(), until=observed_at)
            except Exception as error:  # noqa: BLE001 — receipt contains only a bounded code
                receipt = source_result('unavailable', observed_at=observed_at, error=_source_error(error))
        if not allowed('gmail'):
            rows, receipt = [], source_result('disabled', observed_at=observed_at)
        jsonstore.write_atomic(a.gmail_out, list(rows))
        jsonstore.write_atomic(a.cal_out, [])
        source_results['gmail'] = receipt
        write_source_results()
        print(json.dumps({'emails': len(rows), 'status': receipt['status']}))
        return


    if a.ensure_deps:
        # Setup-time heal: pay the (up to 240s) pip install during onboarding, so the first brief's
        # in-line self-heal (the backstop) finds everything already importable and costs ~nothing.
        ok = _ensure_google_deps()
        msg = f"[gather_google] --ensure-deps: googleapiclient {'OK' if ok else 'STILL MISSING (see log)'}"
        print(msg)
        _diag(msg)
        return

    if a.attendee_comms:
        # Per-attendee Gmail threads (meeting-prep). Same fail-empty discipline as the main gather:
        # any failure → empty {} + WARNING, exit 0 — the prep still runs, just without threads.
        comms, err = {}, None
        api = _find_google_api()
        if not api:
            err = ("google_api.py not found — Sotto's packaged Google CLI is missing; "
                   "attendee threads unavailable")
        else:
            _ensure_google_deps()
            try:
                _refresh_google_token()
                comms = gather_attendee_comms(api, a.attendee_comms, expand_threads=a.expand_threads)
            except Exception as e:  # noqa: BLE001
                comms, err = {}, f"attendee-comms: {e}"
        with open(a.comms_out, "w", encoding="utf-8") as f:
            json.dump(comms, f)
        msg = (f"[gather_google] attendee comms for {len(comms)} attendee(s) → {a.comms_out}")
        if err:
            msg += f"  (WARNING: {err})"
        print(msg)
        _diag(msg)
        return

    api = _find_google_api()
    emails, sent, events, stale, err = [], [], [], [], None
    loop_emails, loop_texts, loop_receipt = [], [], None
    meeting_context, meeting_receipt = None, None
    gmail_rate_limited = False  # bound even when the inbox gate and the loop gate disagree
    if not api:
        err = "google_api.py not found — Sotto's packaged Google CLI is missing"
    else:
        _diag(f"[gather_google] using {api}")
        _ensure_google_deps()   # guarantee googleapiclient in THIS interpreter before any fetch
        try:
            _refresh_google_token()
        except Exception as e:  # noqa: BLE001 — both sources keep their "unavailable" receipt
            api, err = None, f"google token: {e}"
        with calendar_gather(api, max(0, a.window_days - 1), gather_calendar,
                             enabled=not a.skip_calendar) as calendar_future:
            if api and not a.skip_gmail and allowed('gmail'):
                try:
                    emails = gather_gmail(api, a.max, a.bodies, days=a.window_days)
                    capped = (getattr(emails, 'capped') if isinstance(emails, GmailRows)
                              else a.max > 0 and len(emails) >= a.max)
                    gmail_rate_limited = getattr(emails, 'rate_limited', False)
                    source_results['gmail'] = source_result('partial' if capped or gmail_rate_limited else 'ok',
                        complete=not capped and not gmail_rate_limited,
                        since=(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=a.window_days)).isoformat(),
                        until=observed_at, error='rate_limited' if gmail_rate_limited else None)
                except Exception as e:  # noqa: BLE001
                    err = f"gmail: {e}"
                    error_code = _source_error(e)
                    gmail_rate_limited = error_code == 'rate_limited'
                    source_results["gmail"] = source_result("unavailable", error=error_code)
                # Sent lane is exhaust, not brief content — its own try so a failure here can never
                # cost the brief its inbox.
                if not gmail_rate_limited and not a.skip_sent and a.sent_max > 0:
                    try:
                        sent = gather_sent(api, a.sent_max, min(SENT_BODIES, a.sent_max), days=a.window_days)
                        gmail_rate_limited = getattr(sent, 'rate_limited', False)
                        if gmail_rate_limited:
                            mark_gmail_lane_rate_limited()
                    except Exception as e:  # noqa: BLE001
                        err = (err + f"; sent: {e}") if err else f"sent: {e}"
                        gmail_rate_limited = _source_error(e) == 'rate_limited'
                        if gmail_rate_limited:
                            mark_gmail_lane_rate_limited()
                # The stale-sent lane: its own try, and only on the daily window (a backfill has no
                # "today" to be stale against).
                if not gmail_rate_limited and not a.skip_stale and a.window_days <= 1:
                    try:
                        stale = gather_stale_sent(api)
                        gmail_rate_limited = getattr(stale, 'rate_limited', False)
                        if gmail_rate_limited:
                            mark_gmail_lane_rate_limited()
                    except Exception as e:  # noqa: BLE001
                        err = (err + f"; stale: {e}") if err else f"stale: {e}"
                        gmail_rate_limited = _source_error(e) == 'rate_limited'
                        if gmail_rate_limited:
                            mark_gmail_lane_rate_limited()
            if (api and a.loop_local and not a.skip_gmail and allowed('gmail')
                    and not gmail_rate_limited):
                # Read evidence, never infer completion here. The same merged rows go to both the
                # brief model and its evidence validator. Nothing is persisted outside usual inputs.
                try:
                    import ledger_io
                    import loop_context
                    from source_context import bridge_call, project_local
                    with open(a.loop_local, encoding='utf-8') as handle:
                        loop_local = project_local(json.load(handle))
                    loops = ledger_io.load_active()
                    if loops:
                        service = _gmail_service()
                        try:
                            loop_updates, loop_receipt = loop_context.refresh_context(
                                loops, loop_local, service, normalize_email, allowed=allowed,
                                known_messages=[*emails, *sent], known_threads=getattr(stale, 'threads', {}),
                                classify_error=_source_error,
                                read_messages=lambda identifier, timeout: bridge_call(
                                    'get_messages', {'identifier': identifier, 'limit': loop_context.MAX_MESSAGES},
                                    timeout=timeout))
                            loop_emails, loop_texts = loop_updates['emails'], loop_updates['imessage']
                        finally:
                            service.close()
                        if loop_receipt.get('error') == 'rate_limited':
                            mark_gmail_lane_rate_limited()
                except Exception:  # optional context never costs the main gather or exposes source text
                    loop_receipt = {'status': 'unavailable', 'error': 'read_failed', 'complete': False}
            if calendar_future is not None:
                events, source_results['calendar'], cal_error = calendar_result(calendar_future, _source_error)
                if cal_error:
                    err = f'{err}; {cal_error}' if err else cal_error


    if (api and a.loop_local and a.window_days == 1 and not a.skip_gmail and not a.skip_calendar
            and not gmail_rate_limited and allowed('gmail') and allowed('calendar')):
        try:
            meeting_context, meeting_receipt = gather_brief_meeting_context(api, events, [*emails, *sent])
        except Exception:  # An optional context failure must preserve the ordinary source files.
            meeting_receipt = {'status': 'unavailable', 'error': 'read_failed', 'complete': False}

    # Recheck grants after I/O; a concurrent revocation must also discard staged results.
    for source in ('gmail', 'calendar'):
        if not allowed(source):
            source_results[source] = source_result('disabled')
            if source == 'gmail':
                emails, sent, stale, loop_emails, loop_texts = [], [], [], [], []
                loop_receipt = {"status": "disabled", "complete": False}
            else:
                events = []
    if loop_receipt and not all(allowed(source) for source in loop_receipt.get('sources', ['gmail'])):
        loop_emails, loop_texts = [], []
        loop_receipt.update(status='disabled', messages=0)
    if loop_texts and a.loop_local:
        try:
            from render_local import message_evidence_id
            from source_context import project_local
            # Preserve the ordinary snapshot and its coverage. Supplemental historical rows do not
            # make that source-wide window complete or replace a newer message with a cached copy.
            with open(a.loop_local, encoding='utf-8') as handle:
                loop_local = project_local(json.load(handle))
            existing = list(loop_local.get('imessage') or [])
            seen = {message_evidence_id(message, 'imessage') for message in existing if isinstance(message, dict)}
            loop_local['imessage'] = existing + [message for message in loop_texts
                if message_evidence_id(message, 'imessage') not in seen]
            import jsonstore
            jsonstore.write_atomic(a.loop_local, project_local(loop_local))
        except Exception:
            # Optional supplemental history must not cost the ordinary Gmail/calendar gather.
            loop_receipt['imessage_error'] = 'context_merge_failed'
            loop_receipt.setdefault('error', 'context_merge_failed')
            loop_receipt['messages'] = len(loop_emails)
            loop_receipt.setdefault('message_counts', {})['imessage'] = 0
    if meeting_receipt is not None:
        source_results['gmail']['meeting_context'] = meeting_receipt
    if not allowed('gmail') or not allowed('calendar'):
        meeting_context = None
        if meeting_receipt is not None:
            meeting_receipt.update(status='disabled', messages=0)
    if loop_receipt is not None:
        source_results['gmail']['loop_context'] = loop_receipt
    write_source_results()

    # Email-window honesty: any inbox search hitting its per-query cap may have more results.
    # Wrap the array in a metadata envelope
    # ({"emails": [...], "truncated_at": N}) that compose_brief and triage_queue both accept, so the
    # brief's coverage/source-availability line can say "(inbox window truncated at N — more arrived)".
    # An un-truncated gather keeps the plain-array format (full back-compat).
    # NOTE: measured on the INBOX lane only, BEFORE the sent merge — the sent rows would otherwise
    # push the count past --max and either mask or fake a truncated window.
    truncated_at = a.max if (not a.skip_gmail and a.max > 0 and
                             (getattr(emails, 'capped') if isinstance(emails, GmailRows)
                              else len(emails) >= a.max)) else None
    emails = merge_sent(emails, sent)
    # Prefer richer recent rows on collision; the extra lane only fills missing evidence.
    if loop_emails:
        from loop_context import merge_context
        emails = merge_context(emails, loop_emails)
    n_sent = sum(1 for e in emails if e.get("isSent"))
    if truncated_at or stale or meeting_context:
        gmail_payload = {"emails": emails}
        if meeting_context:
            gmail_payload["meeting_context"] = meeting_context
        if truncated_at:
            gmail_payload["truncated_at"] = truncated_at
            if a.window_days > 1:
                gmail_payload["truncation_scope"] = "date_slices"
                gmail_payload["truncation_note"] = (
                    "(email history is partial — some date ranges reached the read limit)")
            else:
                gmail_payload["truncation_note"] = f"(inbox window truncated at {truncated_at} — more arrived)"
        if stale:
            gmail_payload["stale_threads"] = stale
    else:
        gmail_payload = emails
    with open(a.gmail_out, "w", encoding="utf-8") as f:
        json.dump(gmail_payload, f)
    with open(a.cal_out, "w", encoding="utf-8") as f:
        json.dump(events, f)
    msg = (f"[gather_google] {len(emails)} emails ({n_sent} sent, {len(stale)} stale sent threads), "
           f"{len(events)} events → {a.gmail_out}, {a.cal_out}")
    if truncated_at:
        msg += "  " + gmail_payload["truncation_note"]
    if err:
        msg += f"  (WARNING: {err})"
    print(msg)
    _diag(msg)   # also persist to /debug/brief-log so a 0-email gather's REASON is visible on the box


if __name__ == "__main__":
    main()
