#!/usr/bin/env python3
"""gather_google.py — deterministically fetch Gmail (last 24h inbox + a small `in:sent` lane) +
Calendar (next 3 days) via Hermes' google-workspace `google_api.py`, normalized to the shapes
compose_brief expects, and write /tmp/sotto_gmail.json + /tmp/sotto_cal.json.

WHY: the brief skill used to tell the agent to "use the google-workspace tools" and write the files by
hand. The agent kept skipping it → local-only briefs (0 emails), the #1 quality failure. The real
google-workspace interface is a CLI:
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
import glob
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from email.utils import getaddresses

# The attachment lane's caps and its converter, imported from their OWNER in _shared/lib. The three
# numbers are defined once, there, and read here — the fetch side and the render side sharing one
# set of constants is what keeps "3 per email, 8MB, 3,500 chars" from becoming two different claims.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from attachments import (  # noqa: E402
    MAX_ATTACHMENTS_PER_EMAIL, MAX_ATTACHMENT_BYTES, apply_attachment_budget, convert_attachment,
)

BODY_FETCH_WORKERS = 5   # concurrent full-body fetches (each its own google_api.py subprocess)

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

# Stale sent lane (roadmap, Sep 2026): "you emailed them four days ago and got nothing" — an email
# you sent that nobody answered is a debt owed to you. One `in:sent` search over a bounded window,
# then one threads.get per candidate thread to ask the only question that matters: is the LAST
# message on the thread still yours? Rows go to the gmail payload as `stale_threads`; compose_brief
# filters them to people you know and mints the waiting_on debts deterministically.
STALE_SILENT_DAYS = 3        # silent this long → stale (the same clock the chase lane uses)
STALE_MAX_DAYS = 14          # …and no older: two weeks of silence is a dead thread, not a debt
STALE_MAX_THREADS = 20       # threads.get calls per gather, newest first

# --attendee-comms mode (meeting-prep): per-attendee Gmail searches, same concurrency pattern.
ATTENDEE_COMMS_CAP = 15        # unique attendee emails searched per run
ATTENDEE_COMMS_WORKERS = 5     # concurrent per-attendee searches
ATTENDEE_COMMS_MAX_PER = 5     # messages kept per attendee
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
    """Locate the google-workspace skill's google_api.py in the Hermes install."""
    bases = [os.environ.get("HERMES_HOME", ""), os.path.expanduser("~/.hermes"),
             "/root/.hermes", "/usr/local/lib/hermes-agent"]
    for base in bases:
        if not base:
            continue
        hits = glob.glob(os.path.join(base, "**", "google-workspace", "scripts", "google_api.py"),
                         recursive=True)
        if hits:
            return hits[0]
    return None


def _run(api, args, timeout=60):
    py = sys.executable or "python3"
    r = subprocess.run([py, api, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"google_api {' '.join(args)} failed")
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


def _pick(d: dict, *keys):
    """First non-empty value among `keys` (host-agnostic: the google-workspace CLI, a Gmail/Calendar
    MCP server, and raw Google API all name these fields slightly differently)."""
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if v not in (None, "", [], {}):
            return v
    return None


def _addr_str(v):
    """Coerce a from/to field to a string. MCP servers sometimes return {name,email} or a list."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        name, email = v.get("name") or "", v.get("email") or v.get("address") or ""
        return f"{name} <{email}>".strip() if email else (name or "")
    if isinstance(v, list):
        return ", ".join(_addr_str(x) for x in v if x)
    return ""


def normalize_email(it: dict, full: dict) -> dict:
    """Map a Gmail item (google_api CLI OR a Gmail MCP OR raw API) → compose_brief's email shape.
    Tolerant of field-name variants so the SAME brief works whichever host provided the data."""
    labels = _pick(full, "labels", "labelIds") or _pick(it, "labels", "labelIds", "label_ids") or []
    labels = [str(x).upper() for x in labels] if isinstance(labels, list) else []
    return {
        "id": _pick(it, "id", "message_id", "messageId"),
        "threadId": _pick(it, "threadId", "thread_id") or _pick(full, "threadId", "thread_id"),
        "from": _addr_str(_pick(full, "from", "sender", "from_address") or _pick(it, "from", "sender")),
        "to": _addr_str(_pick(full, "to", "recipient") or _pick(it, "to", "recipient")),
        "subject": _pick(full, "subject", "title") or _pick(it, "subject", "title") or "",
        "date": _pick(full, "date", "internalDate", "received_at") or _pick(it, "date", "internalDate", "received_at") or "",
        "snippet": _pick(it, "snippet", "preview", "body_preview") or _pick(full, "snippet", "preview") or "",
        "body": _pick(full, "body", "text", "content", "plain_text") or _pick(it, "body", "text") or "",
        "labelIds": labels,
        "isSent": "SENT" in labels,
    }


def normalize_event(e: dict) -> dict:
    """Map a calendar event (google_api CLI OR a Calendar MCP OR raw API) → compose_brief's event
    shape (start as a string). Tolerant of field-name variants across hosts."""
    def _t(*keys):
        v = _pick(e, *keys)
        return (v.get("dateTime") or v.get("date") or "") if isinstance(v, dict) else (v or "")
    return {
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


def my_response(e: dict) -> str:
    """The self attendee's responseStatus, lowercased ("" when the event carries none)."""
    for a in (e.get("attendees") or []) if isinstance(e, dict) else []:
        if isinstance(a, dict) and a.get("self"):
            return str(a.get("responseStatus") or "").strip().lower()
    return ""


def _fetch_body(api, mid):
    """One full-message fetch. A failure just means that email stays snippet-only."""
    try:
        return mid, _run(api, ["gmail", "get", str(mid)], timeout=30)
    except Exception:
        return mid, None


# ── the attachment lane ─────────────────────────────────────────────────────────────────────────
# An attachment Sotto can read becomes text under its email; one it can't is named, never guessed.
#
# WHY THIS DOESN'T GO THROUGH THE HOST CLI — the same reason google_action.py's `gmail-draft`
# doesn't, and by the precedent that file set: the Hermes `google-workspace` `google_api.py` has no
# attachments verb (its gmail actions are search/get/send/reply/labels/modify), and that CLI is
# installed from UPSTREAM, not from this repo — a subcommand added to it would be overwritten by the
# next image build. Worse, its `gmail get` discards the MIME part tree entirely (it returns only the
# flattened body), so the filenames aren't reachable through it at any price. The granted token
# already carries the Gmail read scope, so this reads straight from the Gmail API using the SAME
# token file the CLI authenticates with. `_gmail_service` below is that one shared client builder;
# google_action.py imports it from here rather than keeping a second copy.
#
# ONE EXTRA CALL, ONLY FOR THE COHORT: one `messages().get(format="full")` per bodies-cohort INBOX
# message. Attachment bytes that Gmail already inlined cost nothing more; only the ones it hands
# back as an `attachmentId` need the second `attachments().get`, so an email with no attachments
# never makes one. The sent lane is skipped entirely — it is style exhaust, not brief content.


def _token_path() -> str:
    """The google-workspace token file — the SAME one google_api.py authenticates with
    ($HERMES_HOME/google_token.json, written by its setup.py). "" when Google isn't connected."""
    for base in (os.environ.get("HERMES_HOME", ""), os.path.expanduser("~/.hermes"), "/root/.hermes"):
        if base and os.path.isfile(os.path.join(base, "google_token.json")):
            return os.path.join(base, "google_token.json")
    return ""


def _gmail_service():
    return _google_service("gmail", "v1")


def _google_service(api, version):
    """A client on the host's existing credentials. Scopes are NOT passed (setup.py's own
    rule: the user may have granted a subset, and passing them makes refresh fail with
    invalid_scope)."""
    path = _token_path()
    if not path:
        raise RuntimeError("Google isn't connected on this host (no google_token.json)")
    from google.oauth2.credentials import Credentials  # noqa: PLC0415
    from googleapiclient.discovery import build        # noqa: PLC0415
    return build(api, version, credentials=Credentials.from_authorized_user_file(path),
                 cache_discovery=False)


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


def _fetch_attachments(service, mid: str) -> list:
    """One message → its attachment rows, ready to hang on the normalized email.

    EVERY attachment is named. The first MAX_ATTACHMENTS_PER_EMAIL that are also under
    MAX_ATTACHMENT_BYTES are fetched and converted; the rest are named with the reason they weren't
    ("too large to read", "over the 3-attachment limit"). A message with no attachments costs
    nothing beyond the one metadata call and returns []."""
    msg = service.users().messages().get(userId="me", id=str(mid), format="full").execute()
    parts = _attachment_parts((msg or {}).get("payload") or {})
    rows, converted = [], 0
    for part in parts:
        name = part["filename"]
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
        except Exception:  # noqa: BLE001  (one attachment failing to download names it, nothing more)
            rows.append({"filename": name, "unreadable": "could not be downloaded"})
            continue
        rows.append(convert_attachment(name, data))
    return rows


def _attachments_for(mids: list) -> dict:
    """{message_id: [attachment rows]} for the bodies-cohort inbox messages that have any.

    NEVER RAISES. Google not connected, the client libs missing, an API error, one bad message —
    every one of them means the brief runs with fewer attachments, never that the gather dies. That
    is the fail-toward-silence bar; the brief's own source-availability line is where a broken
    source speaks up."""
    if not mids or not _token_path():
        return {}
    def _one(mid):
        # googleapiclient's httplib2 transport is not thread-safe. A shared client
        # corrupted concurrent TLS reads in the first live Cloud gather (SIGSEGV).
        # Each worker owns and closes its connection; cohort ordering stays unchanged.
        service = None
        try:
            service = _gmail_service()
            return mid, _fetch_attachments(service, mid)
        except Exception:  # noqa: BLE001
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
                  attachments: bool = False):
    """One Gmail search → normalized rows, with full bodies for the top `bodies` hits (and, when
    `attachments` is on, their attachments converted to Markdown under them)."""
    items = _as_list(_run(api, ["gmail", "search", query, "--max", str(max_n)], timeout=timeout))
    # Snippets are thin; fetch full bodies for the top N — CONCURRENTLY (the pattern proven in
    # research_attendees.py). Sequentially this was up to N × 30s of the brief's wall clock.
    # Output order is preserved: `full` is a lookup, the emit loop below follows `items`.
    mids = [it.get("id") for it in items[:bodies] if it.get("id")]
    full = {}
    if mids:
        with ThreadPoolExecutor(max_workers=min(BODY_FETCH_WORKERS, len(mids))) as ex:
            for mid, msg in ex.map(lambda m: _fetch_body(api, m), mids):
                if msg is not None:
                    full[mid] = msg
    # The attachment lane rides the SAME cohort as the bodies: an email thin enough to be
    # snippet-only is not one the brief is reading closely enough to need its files. The fetches
    # run concurrently, then the per-brief budget is spent deterministically in cohort order —
    # concurrency decides when the bytes arrive, never who gets the budget.
    atts = _attachments_for(mids) if attachments else {}
    if atts:
        budgeted = apply_attachment_budget([atts.get(m) or [] for m in mids])
        atts = {m: rows for m, rows in zip(mids, budgeted) if rows}
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        e = normalize_email(it, full.get(it.get("id"), {}))
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
    out, seen = [], set()
    for i, (lo, hi) in enumerate(bounds):
        q = f"{base_query} after:{lo:%Y/%m/%d} before:{hi:%Y/%m/%d}".strip()
        for e in _search_gmail(api, q, max_n, per + (bodies % len(bounds) if i == 0 else 0),
                               timeout=BACKFILL_TIMEOUT, attachments=attachments):
            mid = str(e.get("id") or "")
            if mid and mid in seen:
                continue
            seen.add(mid)
            out.append(e)
    return out


def gather_gmail(api, max_n: int, bodies: int, days: int = 1):
    """The inbox lane — the ONE lane that carries attachments (the sent lane below does not)."""
    if days <= 1:
        return _search_gmail(api, "newer_than:1d", max_n, bodies, attachments=True)
    return _sliced_gmail(api, "", max_n, bodies, days, attachments=True)


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
    if days <= 1:
        return [mark_sent(e) for e in _search_gmail(api, "in:sent newer_than:1d", max_n, bodies)]
    return [mark_sent(e) for e in _sliced_gmail(api, "in:sent", max_n, bodies, days)]


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
    items = _as_list(_run(api, ["gmail", "search", q, "--max", str(STALE_MAX_THREADS * 2)]))
    rows = [normalize_email(it, {}) for it in items if isinstance(it, dict)]
    tids = []
    for r in rows:
        tid = str(r.get("threadId") or "")
        if tid and tid not in tids:
            tids.append(tid)
    tids = tids[:STALE_MAX_THREADS]
    if not tids:
        return []
    svc = service or _gmail_service()
    threads = {}
    for tid in tids:
        try:
            threads[tid] = svc.users().threads().get(
                userId="me", id=tid, format="metadata",
                metadataHeaders=["From", "To", "Cc", "Subject"]).execute()
        except Exception:  # noqa: BLE001 — one unreadable thread is not a broken lane
            continue
    return stale_from_threads(rows, threads, now)


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
    # Hermes' list command drops attendees/RSVPs and does not follow nextPageToken.
    # Use the same granted credentials as attachments, keeping Google's complete event
    # shape through our existing normalizer. The CLI remains for hosts without that token.
    if service is not None or _token_path():
        owned = service is None
        service = service if service is not None else _google_service('calendar', 'v3')
        out, seen, tokens, page = [], set(), set(), None
        try:
            for _ in range(20):
                params = {'calendarId': 'primary',
                          'timeMin': (now - datetime.timedelta(days=max(0, back_days))).strftime(fmt),
                          'timeMax': end.strftime(fmt), 'singleEvents': True,
                          'orderBy': 'startTime', 'maxResults': 250}
                if page:
                    params['pageToken'] = page
                result = service.events().list(**params).execute()
                for raw in result.get('items', []):
                    if raw.get('status') == 'cancelled':
                        continue
                    event = normalize_event(raw)
                    key = str(event.get('id') or '') or json.dumps(event, sort_keys=True)
                    if key not in seen:
                        seen.add(key)
                        out.append(event)
                page = result.get('nextPageToken')
                if not page:
                    if observation is not None:
                        observation["complete"] = True
                    return meeting_events(out)
                if page in tokens:
                    raise RuntimeError('Calendar pagination repeated a page token')
                tokens.add(page)
            raise RuntimeError('Calendar window exceeds the pagination safety limit')
        finally:
            if owned:
                service.close()
    if back_days <= 0:
        items = _as_list(_run(api, ["calendar", "list", "--start", now.strftime(fmt),
                                    "--end", end.strftime(fmt)]))
        return meeting_events([normalize_event(e) for e in items])
    out, seen = [], set()
    lo = now - datetime.timedelta(days=back_days)
    while lo < end:
        hi = min(lo + datetime.timedelta(days=CAL_SLICE_DAYS), end)
        items = _as_list(_run(api, ["calendar", "list", "--start", lo.strftime(fmt),
                                    "--end", hi.strftime(fmt)], timeout=BACKFILL_TIMEOUT))
        for e in (normalize_event(x) for x in items):
            key = str(e.get("id") or "") or json.dumps(e, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
        lo = hi
    return meeting_events(out)


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


def _fetch_attendee_comms(api, email: str) -> tuple:
    """One per-attendee Gmail search (30d window, both directions). A failure just means that
    attendee gets no thread context — never fails the whole gather. from_me: the user's own address
    isn't knowable here (no auth introspection), so direction is derived from the message itself —
    the SENT label when the CLI returns labels, else whether the From header carries the ATTENDEE's
    address (if it doesn't, the user wrote it: the search guarantees the attendee is on the thread)."""
    try:
        items = _as_list(_run(api, ["gmail", "search",
                                    f"from:{email} OR to:{email} newer_than:30d",
                                    "--max", str(ATTENDEE_COMMS_MAX_PER)],
                              timeout=ATTENDEE_COMMS_TIMEOUT))
    except Exception:
        return email, []
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        labels = _pick(it, "labels", "labelIds", "label_ids") or []
        labels = [str(x).upper() for x in labels] if isinstance(labels, list) else []
        frm = _addr_str(_pick(it, "from", "sender", "from_address") or "").lower()
        rows.append({
            "date": str(_pick(it, "date", "internalDate", "received_at") or ""),
            "subject": str(_pick(it, "subject", "title") or ""),
            "snippet": str(_pick(it, "snippet", "preview", "body_preview") or ""),
            "from_me": ("SENT" in labels) or bool(frm and email not in frm),
        })
    return email, rows[:ATTENDEE_COMMS_MAX_PER]


def gather_attendee_comms(api, attendees_path: str) -> dict:
    """{attendee_email: [{date,subject,snippet,from_me}]} for every unique attendee email in the
    list file (cap 15), searched concurrently. Attendees whose search found nothing are omitted."""
    emails = _attendee_emails_from_file(attendees_path)
    if not emails:
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=min(ATTENDEE_COMMS_WORKERS, len(emails))) as ex:
        for email, rows in ex.map(lambda e: _fetch_attendee_comms(api, e), emails):
            if rows:
                out[email] = rows
    return out


def normalize_mcp(gmail_raw_path, cal_raw_path, sent_raw_path=None):
    """HOST-AGNOSTIC fallback: when the google-workspace CLI isn't the auth path (e.g. the host has
    Google connected as a Gmail/Calendar MCP server, as OpenClaw or some Hermes setups do), the agent
    dumps the RAW MCP tool results to files and we normalize them to the SAME shape the CLI path emits.
    Keeps field-mapping deterministic instead of asking the agent to hand-map (which drifts)."""
    def _load(p):
        if not p:
            return []
        try:
            with open(p, encoding="utf-8") as f:
                return _as_list(json.load(f))
        except Exception:
            return []
    emails = [normalize_email(it, it) for it in _load(gmail_raw_path)]
    # Sent-lane parity for MCP hosts: dump the `in:sent newer_than:1d` tool result too and it merges
    # in with the SAME dedup + isSent guarantee the CLI path applies. Without --from-mcp-sent an MCP
    # host simply has no sent lane (its inbox dump may still carry SENT-labelled rows) — the one gap.
    sent = [mark_sent(normalize_email(it, it)) for it in _load(sent_raw_path)]
    events = [normalize_event(e) for e in _load(cal_raw_path)]
    return merge_sent(emails, sent), events


def history_page(since: int, until: int, page_token: str = '', limit: int = 100, service=None, now=None):
    """Frozen-window Gmail history through the existing credential builder; resume real API pages.

    Both directions are searched together. No attachments or writes. A failed get fails the page,
    so the caller never advances its checkpoint over missing messages.
    """
    from source_context import allowed
    if not allowed('gmail'):
        raise RuntimeError('Gmail history is not enabled')
    epoch = int((now or datetime.datetime.now(datetime.timezone.utc)).timestamp())
    if (not all(isinstance(v, int) and not isinstance(v, bool) for v in (since, until, limit))
            or not 1 <= limit <= 100 or since > until or since < epoch - 366*86400 or until > epoch + 60):
        raise ValueError('invalid Gmail history window')
    own_service = service is None
    service = service or _gmail_service()
    def plain(part):
        if part.get('filename'):
            return ''
        if part.get('mimeType') == 'text/plain':
            raw = part.get('body', {}).get('data', '')
            return base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)).decode('utf-8', errors='replace') if raw else ''
        return '\n'.join(filter(None, (plain(p) for p in part.get('parts', []))))
    try:
        params = {'userId': 'me', 'q': f'after:{since} before:{until + 1}', 'maxResults': limit}
        if page_token:
            params['pageToken'] = page_token
        page = service.users().messages().list(**params).execute()
        messages = []
        for item in page.get('messages', []):
            full = service.users().messages().get(userId='me', id=item['id'], format='full').execute()
            payload = full.get('payload', {})
            headers = {h['name'].lower(): h.get('value', '') for h in payload.get('headers', [])}
            at = datetime.datetime.fromtimestamp(int(full['internalDate']) / 1000, datetime.timezone.utc)
            if not since <= at.timestamp() < until + 1:
                continue
            full.update({'from': headers.get('from', ''), 'to': headers.get('to', ''),
                         'subject': headers.get('subject', ''), 'body': plain(payload)[:8000],
                         'date': at.isoformat()})
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
    # Host-agnostic MCP fallback: pass RAW dumps of the host's Gmail/Calendar MCP tool results and we
    # normalize them to the canonical shape (no CLI needed). Use these when `--check` says the CLI is
    # unavailable but the host can reach Google another way.
    ap.add_argument("--from-mcp-gmail", help="raw Gmail MCP tool-result JSON to normalize (no CLI)")
    ap.add_argument("--from-mcp-calendar", help="raw Calendar MCP tool-result JSON to normalize (no CLI)")
    ap.add_argument("--from-mcp-sent",
                    help="raw Gmail MCP tool-result JSON for `in:sent newer_than:1d` (merged, marked isSent)")
    ap.add_argument("--ensure-deps", action="store_true",
                    help="ONLY run the googleapiclient self-heal (setup-time), no gather")
    ap.add_argument("--attendee-comms", dest="attendee_comms",
                    help="attendee-list JSON ([{name,email}] or calendar-derived) — ONLY gather "
                         "per-attendee Gmail threads (30d, both directions) to --comms-out")
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
            err = ("google_api.py not found — the google-workspace CLI isn't this host's Google "
                   "path; attendee threads unavailable")
        else:
            _ensure_google_deps()
            try:
                comms = gather_attendee_comms(api, a.attendee_comms)
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

    if a.from_mcp_gmail or a.from_mcp_calendar or a.from_mcp_sent:
        emails, events = normalize_mcp(a.from_mcp_gmail, a.from_mcp_calendar, a.from_mcp_sent)
        # An arbitrary MCP dump has no verified pagination/window contract. It remains useful
        # context but never proves an absent event was cancelled.
        for source, supplied in (('gmail', a.from_mcp_gmail or a.from_mcp_sent), ('calendar', a.from_mcp_calendar)):
            source_results[source] = source_result('partial' if supplied else 'skipped')
            if not allowed(source):
                source_results[source] = source_result('disabled')
        if not allowed('gmail'):
            emails = []
        if not allowed('calendar'):
            events = []
        write_source_results()
        with open(a.gmail_out, "w", encoding="utf-8") as f:
            json.dump(emails, f)
        with open(a.cal_out, "w", encoding="utf-8") as f:
            json.dump(events, f)
        n_sent = sum(1 for e in emails if e.get("isSent"))
        msg = (f"[gather_google] normalized from MCP: {len(emails)} emails ({n_sent} sent), "
               f"{len(events)} events → {a.gmail_out}, {a.cal_out}")
        print(msg)
        _diag(msg)
        return

    api = _find_google_api()
    emails, sent, events, stale, err = [], [], [], [], None
    if not api:
        err = ("google_api.py not found — the google-workspace CLI isn't this host's Google path. "
               "FALLBACK: fetch Gmail (newer_than:1d), sent mail (in:sent newer_than:1d) + Calendar "
               "(next 3d) with the host's Gmail/Calendar MCP tools, dump them to files, and re-run "
               "with --from-mcp-gmail/--from-mcp-sent/--from-mcp-calendar.")
    else:
        _diag(f"[gather_google] using {api}")
        _ensure_google_deps()   # guarantee googleapiclient in THIS interpreter before any fetch
        if not a.skip_gmail and allowed('gmail'):
            try:
                emails = gather_gmail(api, a.max, a.bodies, days=a.window_days)
                capped = a.max > 0 and len(emails) >= a.max
                source_results['gmail'] = source_result('partial' if capped else 'ok', complete=not capped,
                    since=(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=a.window_days)).isoformat(),
                    until=observed_at)
            except Exception as e:  # noqa: BLE001
                err = f"gmail: {e}"
                source_results["gmail"] = source_result("unavailable", error=type(e).__name__)
            # Sent lane is exhaust, not brief content — its own try so a failure here can never
            # cost the brief its inbox.
            if not a.skip_sent and a.sent_max > 0:
                try:
                    sent = gather_sent(api, a.sent_max, min(SENT_BODIES, a.sent_max), days=a.window_days)
                except Exception as e:  # noqa: BLE001
                    err = (err + f"; sent: {e}") if err else f"sent: {e}"
            # The stale-sent lane: its own try, and only on the daily window (a backfill has no
            # "today" to be stale against).
            if not a.skip_stale and a.window_days <= 1:
                try:
                    stale = gather_stale_sent(api)
                except Exception as e:  # noqa: BLE001
                    err = (err + f"; stale: {e}") if err else f"stale: {e}"
        if not a.skip_calendar and allowed('calendar'):
            try:
                observation = {}
                events = gather_calendar(api, back_days=max(0, a.window_days - 1), observation=observation)
                complete = observation.get('complete') is True
                source_results['calendar'] = source_result('ok' if complete else 'partial', complete=complete,
                    **observation.get('coverage', {}))
            except Exception as e:  # noqa: BLE001
                err = (err + f"; calendar: {e}") if err else f"calendar: {e}"
                source_results["calendar"] = source_result("unavailable", error=type(e).__name__)

    # Recheck grants after I/O; a concurrent revocation must also discard staged results.
    for source in ('gmail', 'calendar'):
        if not allowed(source):
            source_results[source] = source_result('disabled')
            if source == 'gmail':
                emails, sent, stale = [], [], []
            else:
                events = []
    write_source_results()

    # Email-window honesty: the search returning EXACTLY the cap means the 24h window almost
    # certainly held more — never silently truncate. Wrap the array in a metadata envelope
    # ({"emails": [...], "truncated_at": N}) that compose_brief and triage_queue both accept, so the
    # brief's coverage/source-availability line can say "(inbox window truncated at N — more arrived)".
    # An un-truncated gather keeps the plain-array format (full back-compat).
    # NOTE: measured on the INBOX lane only, BEFORE the sent merge — the sent rows would otherwise
    # push the count past --max and either mask or fake a truncated window.
    truncated_at = a.max if (not a.skip_gmail and a.max > 0 and len(emails) == a.max) else None
    emails = merge_sent(emails, sent)
    n_sent = sum(1 for e in emails if e.get("isSent"))
    if truncated_at or stale:
        gmail_payload = {"emails": emails}
        if truncated_at:
            gmail_payload["truncated_at"] = truncated_at
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
        msg += f"  (inbox window truncated at {truncated_at} — more arrived)"
    if err:
        msg += f"  (WARNING: {err})"
    print(msg)
    _diag(msg)   # also persist to /debug/brief-log so a 0-email gather's REASON is visible on the box


if __name__ == "__main__":
    main()
