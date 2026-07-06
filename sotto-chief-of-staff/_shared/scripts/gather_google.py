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
    ap.add_argument("--max", type=int, default=25)
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
    a = ap.parse_args()

    if a.ensure_deps:
        # Setup-time heal: pay the (up to 240s) pip install during onboarding, so the first brief's
        # in-line self-heal (the backstop) finds everything already importable and costs ~nothing.
        ok = _ensure_google_deps()
        msg = f"[gather_google] --ensure-deps: googleapiclient {'OK' if ok else 'STILL MISSING (see log)'}"
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

    with open(a.gmail_out, "w", encoding="utf-8") as f:
        json.dump(emails, f)
    with open(a.cal_out, "w", encoding="utf-8") as f:
        json.dump(events, f)
    msg = f"[gather_google] {len(emails)} emails, {len(events)} events → {a.gmail_out}, {a.cal_out}"
    if err:
        msg += f"  (WARNING: {err})"
    print(msg)
    _diag(msg)   # also persist to /debug/brief-log so a 0-email gather's REASON is visible on the box


if __name__ == "__main__":
    main()
