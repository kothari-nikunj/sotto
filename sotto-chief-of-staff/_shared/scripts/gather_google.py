#!/usr/bin/env python3
"""gather_google.py — deterministically fetch Gmail (last 24h) + Calendar (next 3 days) via Hermes'
google-workspace `google_api.py`, normalized to the shapes compose_brief expects, and write
/tmp/sotto_gmail.json + /tmp/sotto_cal.json.

WHY: the brief skill used to tell the agent to "use the google-workspace tools" and write the files by
hand. The agent kept skipping it → local-only briefs (0 emails), the #1 quality failure. The real
google-workspace interface is a CLI:
    google_api.py gmail search "newer_than:1d" --max N   -> [{id,threadId,from,subject,date,snippet,labels}]
    google_api.py gmail get MESSAGE_ID                    -> full message (body, headers, labels)
    google_api.py calendar list --start ISO --end ISO     -> [{id,summary,start,end,location,description,htmlLink}]
Running it deterministically (one command the skill invokes) makes Google ALWAYS get married with local.

Usage: python3 gather_google.py [--gmail-out P] [--cal-out P] [--max N] [--bodies N]
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
import datetime
import glob
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

BODY_FETCH_WORKERS = 5   # concurrent full-body fetches (each its own google_api.py subprocess)

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
    return json.loads(r.stdout or "null")


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
    }


def _fetch_body(api, mid):
    """One full-message fetch. A failure just means that email stays snippet-only."""
    try:
        return mid, _run(api, ["gmail", "get", str(mid)], timeout=30)
    except Exception:
        return mid, None


def gather_gmail(api, max_n: int, bodies: int):
    items = _as_list(_run(api, ["gmail", "search", "newer_than:1d", "--max", str(max_n)]))
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
    return [normalize_email(it, full.get(it.get("id"), {})) for it in items]


def gather_calendar(api):
    now = datetime.datetime.now(datetime.timezone.utc)
    end = now + datetime.timedelta(days=3)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    items = _as_list(_run(api, ["calendar", "list", "--start", now.strftime(fmt), "--end", end.strftime(fmt)]))
    return [normalize_event(e) for e in items]


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


def normalize_mcp(gmail_raw_path, cal_raw_path):
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
    events = [normalize_event(e) for e in _load(cal_raw_path)]
    return emails, events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gmail-out", default="/tmp/sotto_gmail.json")
    ap.add_argument("--cal-out", default="/tmp/sotto_cal.json")
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--bodies", type=int, default=12)
    ap.add_argument("--skip-gmail", action="store_true", help="calendar only (e.g. meeting-prep)")
    ap.add_argument("--skip-calendar", action="store_true", help="gmail only")
    # Host-agnostic MCP fallback: pass RAW dumps of the host's Gmail/Calendar MCP tool results and we
    # normalize them to the canonical shape (no CLI needed). Use these when `--check` says the CLI is
    # unavailable but the host can reach Google another way.
    ap.add_argument("--from-mcp-gmail", help="raw Gmail MCP tool-result JSON to normalize (no CLI)")
    ap.add_argument("--from-mcp-calendar", help="raw Calendar MCP tool-result JSON to normalize (no CLI)")
    ap.add_argument("--ensure-deps", action="store_true",
                    help="ONLY run the googleapiclient self-heal (setup-time), no gather")
    ap.add_argument("--attendee-comms", dest="attendee_comms",
                    help="attendee-list JSON ([{name,email}] or calendar-derived) — ONLY gather "
                         "per-attendee Gmail threads (30d, both directions) to --comms-out")
    ap.add_argument("--comms-out", dest="comms_out", default="/tmp/sotto_attendee_comms.json",
                    help="output file for --attendee-comms (default /tmp/sotto_attendee_comms.json)")
    a = ap.parse_args()

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

    if a.from_mcp_gmail or a.from_mcp_calendar:
        emails, events = normalize_mcp(a.from_mcp_gmail, a.from_mcp_calendar)
        with open(a.gmail_out, "w", encoding="utf-8") as f:
            json.dump(emails, f)
        with open(a.cal_out, "w", encoding="utf-8") as f:
            json.dump(events, f)
        msg = (f"[gather_google] normalized from MCP: {len(emails)} emails, {len(events)} events "
               f"→ {a.gmail_out}, {a.cal_out}")
        print(msg)
        _diag(msg)
        return

    api = _find_google_api()
    emails, events, err = [], [], None
    if not api:
        err = ("google_api.py not found — the google-workspace CLI isn't this host's Google path. "
               "FALLBACK: fetch Gmail (newer_than:1d) + Calendar (next 3d) with the host's Gmail/"
               "Calendar MCP tools, dump them to files, and re-run with --from-mcp-gmail/--from-mcp-calendar.")
    else:
        _diag(f"[gather_google] using {api}")
        _ensure_google_deps()   # guarantee googleapiclient in THIS interpreter before any fetch
        if not a.skip_gmail:
            try:
                emails = gather_gmail(api, a.max, a.bodies)
            except Exception as e:  # noqa: BLE001
                err = f"gmail: {e}"
        if not a.skip_calendar:
            try:
                events = gather_calendar(api)
            except Exception as e:  # noqa: BLE001
                err = (err + f"; calendar: {e}") if err else f"calendar: {e}"

    # Email-window honesty: the search returning EXACTLY the cap means the 24h window almost
    # certainly held more — never silently truncate. Wrap the array in a metadata envelope
    # ({"emails": [...], "truncated_at": N}) that compose_brief and triage_queue both accept, so the
    # brief's coverage/source-availability line can say "(inbox window truncated at N — more arrived)".
    # An un-truncated gather keeps the plain-array format (full back-compat).
    truncated_at = a.max if (not a.skip_gmail and a.max > 0 and len(emails) == a.max) else None
    gmail_payload = ({"emails": emails, "truncated_at": truncated_at,
                      "truncation_note": f"(inbox window truncated at {truncated_at} — more arrived)"}
                     if truncated_at else emails)
    with open(a.gmail_out, "w", encoding="utf-8") as f:
        json.dump(gmail_payload, f)
    with open(a.cal_out, "w", encoding="utf-8") as f:
        json.dump(events, f)
    msg = f"[gather_google] {len(emails)} emails, {len(events)} events → {a.gmail_out}, {a.cal_out}"
    if truncated_at:
        msg += f"  (inbox window truncated at {truncated_at} — more arrived)"
    if err:
        msg += f"  (WARNING: {err})"
    print(msg)
    _diag(msg)   # also persist to /debug/brief-log so a 0-email gather's REASON is visible on the box


if __name__ == "__main__":
    main()
