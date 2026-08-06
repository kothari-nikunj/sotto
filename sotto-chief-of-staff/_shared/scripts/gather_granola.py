#!/usr/bin/env python3
"""gather_granola.py — deterministically fetch Granola meetings (notes + AI summaries, and full
TRANSCRIPTS for recently-ended meetings) and write /tmp/sotto_granola.json in the exact
`granola_meetings` shape the pipeline already consumes (render_local._format_granola_meetings,
compose_brief's top-level `granola` fold-in, compose_followup's transcript window).

WHY: the skills used to say "via the Granola MCP: list recent meetings…" — agent-driven fetching,
the #1 historical failure mode (steps got skipped → briefs without meeting context, followups
without transcripts). This is the deterministic replacement: ONE command, like gather_google.py.

Modes:
  default — the MCP lane: token from $SOTTO_DATA/connectors/granola.json (written by the
            receiver's /setup OAuth tile), Streamable-HTTP MCP client, tool names discovered via
            tools/list and matched tolerantly (list_meetings / get_meetings / …,
            get_meeting_transcript / get_transcript — the Mac's granola-mcp.ts names).
  --rest  — break-glass fallback (Business/Enterprise plans): GRANOLA_API_KEY against the official
            REST API https://public-api.granola.ai/v1 (GET /notes?created_after=…, per-note
            ?include=transcript), throttled to 5 req/s.
  No connector AND no key → writes {"meetings": []} + a diag line and exits 0 (fail-empty, like
  gather_google — the brief still runs, honestly Granola-less).

Usage: python3 gather_granola.py [--out P] [--days N] [--transcripts-since-hours H] [--rest]
Output: {"meetings": [{title, date, time, attendee_emails, your_notes, ai_summary[, transcript]}]}
Prints `[gather_granola] N meetings (T with transcripts, K with notes) → OUT` — the followup
skill's gate reads those counts. Exits 0 even on failure (empty file + WARNING line).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib")
sys.path.insert(0, _LIB)
from connector_tokens import ConnectorAuthError, ConnectorMissing, get_access_token  # noqa: E402
from mcp_client import McpClient  # noqa: E402

SERVICE = "granola"
DEFAULT_MCP_URL = "https://mcp.granola.ai/mcp"       # the Mac client's URL; connectors file may override
REST_BASE = "https://public-api.granola.ai/v1"       # official REST API (Feb 2026, Business plans)
REST_MIN_INTERVAL = 0.2                              # 5 req/s cap
LIST_LIMIT = 50                                      # mirrors granola-mcp.ts fetchGranolaMeetings

# Tool-name tolerance (exact names first, in preference order; then substring fallback). The Mac
# client (api/src/services/granola-mcp.ts) uses list_meetings + get_meeting_transcript; other
# builds/servers of the same idea vary.
LIST_TOOL_EXACT = ("list_meetings", "get_meetings", "list_notes", "list_recent_meetings",
                   "query_granola_meetings", "list")
TRANSCRIPT_TOOL_EXACT = ("get_meeting_transcript", "get_transcript", "fetch_transcript",
                         "meeting_transcript", "transcript")


def _diag(msg: str) -> None:
    """Persist to $SOTTO_DATA/logs/compose_brief.log (served at /debug/brief-log) so a 0-meeting
    gather is DIAGNOSABLE — same convention as gather_google.py. Best-effort."""
    try:
        from sotto_log import diag
        diag(msg)
    except Exception:
        print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Field mapping — a faithful port of granola-mcp.ts mapToGranolaMeeting()
# ---------------------------------------------------------------------------

def _parse_dt(v):
    if not v or not isinstance(v, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except ValueError:
        return None


def _meeting_dt(m: dict):
    """The meeting's timestamp, tolerant of field-name variants (MCP vs REST vs cache shapes)."""
    for k in ("start_time", "date", "start", "created_at", "createdAt", "startTime"):
        dt = _parse_dt(m.get(k))
        if dt is not None:
            return dt
    return None


def map_meeting(m: dict) -> dict:
    """Map a raw Granola meeting/note (MCP tool result OR REST /notes item) into the EXISTING
    granola_meetings shape: {title, date, time, attendee_emails, your_notes, ai_summary}."""
    date, tstr = "", ""
    dt = _parse_dt(m.get("start_time") or m.get("created_at") or m.get("createdAt") or m.get("start"))
    if dt is not None:
        date = dt.strftime("%Y-%m-%d")
        tstr = dt.strftime("%H:%M")
    elif m.get("date"):
        date = str(m.get("date"))
        tstr = str(m.get("time") or "")

    emails = []
    attendees = m.get("attendees") or m.get("participants")
    if isinstance(attendees, list):
        for a in attendees:
            if isinstance(a, str) and a:
                emails.append(a)
            elif isinstance(a, dict) and a.get("email"):
                emails.append(a["email"])
    elif isinstance(m.get("attendee_emails"), list):
        emails.extend(e for e in m["attendee_emails"] if e)

    return {
        "title": m.get("title") or m.get("name") or "Untitled Meeting",
        "date": date,
        "time": tstr,
        "attendee_emails": emails,
        "your_notes": m.get("notes") or m.get("your_notes") or m.get("notes_markdown") or None,
        "ai_summary": m.get("summary") or m.get("ai_summary") or m.get("summary_markdown") or None,
    }


def _in_window(m: dict, days: int, now) -> bool:
    dt = _meeting_dt(m)
    if dt is None:
        return True   # include if no date to filter on (granola-mcp.ts behavior)
    return dt >= now - datetime.timedelta(days=days)


def _wants_transcript(m: dict, since_hours: int, now) -> bool:
    """Transcripts only for meetings that (recently) ended — the followup/prep window."""
    dt = _meeting_dt(m)
    if dt is None:
        return False
    hours_ago = (now - dt).total_seconds() / 3600.0
    return -1 <= hours_ago <= since_hours


def _extract_transcript(result) -> str | None:
    if isinstance(result, str) and result.strip():
        return result
    if isinstance(result, dict):
        t = result.get("transcript") or result.get("text") or result.get("content")
        if isinstance(t, str) and t.strip():
            return t
    return None


# ---------------------------------------------------------------------------
# MCP mode (the default lane)
# ---------------------------------------------------------------------------

def _pick_tool(tools: list, exact: tuple, must_contain: str, exclude: tuple = ()) -> str | None:
    """Tolerant tool-name discovery: exact candidates in preference order, else the first tool
    whose name contains `must_contain` (and none of `exclude`)."""
    names = [t.get("name") for t in tools if isinstance(t, dict) and t.get("name")]
    by_lower = {n.lower(): n for n in names}
    for cand in exact:
        if cand in by_lower:
            return by_lower[cand]
    for n in names:
        low = n.lower()
        if must_contain in low and not any(x in low for x in exclude):
            return n
    return None


def _make_client(force_refresh: bool = False) -> McpClient:
    token, mcp_url = get_access_token(SERVICE, force_refresh=force_refresh)
    return McpClient(mcp_url or DEFAULT_MCP_URL, token)


def gather_mcp(days: int, since_hours: int, client: McpClient | None = None,
               client_factory=None, now=None) -> tuple[list, list]:
    """List meetings via the Granola MCP, fetch transcripts for the recent window, map to the
    canonical shape. `client`/`client_factory` injectable for tests. Returns (meetings, warnings).
    A 401 mid-session triggers ONE token refresh + retry (fresh client), then gives up."""
    factory = client_factory or _make_client
    warnings: list[str] = []
    if client is None:
        try:
            client = factory()
            client.initialize()
        except ConnectorAuthError:
            client = factory(force_refresh=True)   # stale token on connect → refresh once
            client.initialize()

    tools = client.list_tools()
    list_tool = _pick_tool(tools, LIST_TOOL_EXACT, "meeting", exclude=("transcript", "folder", "account"))
    if not list_tool:
        list_tool = _pick_tool(tools, (), "note", exclude=("transcript", "folder", "account"))
    if not list_tool:
        raise RuntimeError(f"no meeting-list tool found on the Granola MCP (tools: "
                           f"{[t.get('name') for t in tools]})")
    transcript_tool = _pick_tool(tools, TRANSCRIPT_TOOL_EXACT, "transcript")

    raw = client.call_tool(list_tool, {"limit": LIST_LIMIT}, timeout=60)
    if isinstance(raw, dict):
        for k in ("meetings", "notes", "items", "results", "data"):
            if isinstance(raw.get(k), list):
                raw = raw[k]
                break
    if not isinstance(raw, list):
        warnings.append(f"{list_tool} returned non-list ({type(raw).__name__}); treating as empty")
        raw = []

    now = now or datetime.datetime.now(datetime.timezone.utc)
    windowed = [m for m in raw if isinstance(m, dict) and _in_window(m, days, now)]

    meetings = []
    for m in windowed:
        mapped = map_meeting(m)
        if transcript_tool and _wants_transcript(m, since_hours, now):
            mid = m.get("id") or m.get("meeting_id") or m.get("note_id") or m.get("uuid")
            if mid:
                try:
                    t = _extract_transcript(client.call_tool(transcript_tool, {"meeting_id": mid}, timeout=60))
                    if t:
                        mapped["transcript"] = t
                except ConnectorAuthError:
                    raise                                     # bubble for the refresh-once retry
                except Exception as e:  # noqa: BLE001 — one bad transcript never kills the gather
                    warnings.append(f"transcript fetch failed for '{mapped['title']}': {e}")
        meetings.append(mapped)
    if not transcript_tool:
        warnings.append("no transcript tool on the Granola MCP — meetings carry notes/summaries only")
    return meetings, warnings


def gather_mcp_with_retry(days: int, since_hours: int, client_factory=None, now=None) -> tuple[list, list]:
    factory = client_factory or _make_client
    try:
        return gather_mcp(days, since_hours, client_factory=factory, now=now)
    except ConnectorAuthError:
        # Token went stale mid-session → refresh ONCE, fresh client, one retry.
        client = factory(force_refresh=True)
        client.initialize()
        return gather_mcp(days, since_hours, client=client, now=now)


# ---------------------------------------------------------------------------
# REST mode (break-glass: GRANOLA_API_KEY, Business/Enterprise plans)
# ---------------------------------------------------------------------------

def _default_http_get(url: str, headers: dict, timeout: int = 30):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class _Throttle:
    """5 req/s: sleep so consecutive requests are ≥ REST_MIN_INTERVAL apart. Injectable clock."""
    def __init__(self, sleep=time.sleep, monotonic=time.monotonic):
        self._sleep, self._mono, self._last = sleep, monotonic, None

    def wait(self):
        now = self._mono()
        if self._last is not None:
            delta = REST_MIN_INTERVAL - (now - self._last)
            if delta > 0:
                self._sleep(delta)
        self._last = self._mono()


def gather_rest(api_key: str, days: int, since_hours: int, http_get=None, base: str = REST_BASE,
                throttle: _Throttle | None = None, now=None) -> tuple[list, list]:
    """Official REST API: GET /notes?created_after=… (cursor pagination) + per-note
    ?include=transcript for the recent window. Same output shape as the MCP lane."""
    http_get = http_get or _default_http_get
    throttle = throttle or _Throttle()
    warnings: list[str] = []
    now = now or datetime.datetime.now(datetime.timezone.utc)
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    created_after = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    raw, cursor = [], None
    for _page in range(10):                      # pagination hard-cap: 10 pages is plenty for a window
        params = {"created_after": created_after}
        if cursor:
            params["cursor"] = cursor
        throttle.wait()
        status, body = http_get(f"{base}/notes?{urllib.parse.urlencode(params)}", headers)
        if status == 401:
            raise ConnectorAuthError("GRANOLA_API_KEY rejected (HTTP 401)")
        if status != 200:
            warnings.append(f"GET /notes HTTP {status}")
            break
        page = json.loads(body)
        items = page if isinstance(page, list) else next(
            (page[k] for k in ("notes", "items", "data", "results") if isinstance(page.get(k), list)), [])
        raw.extend(i for i in items if isinstance(i, dict))
        cursor = page.get("next_cursor") or page.get("nextCursor") if isinstance(page, dict) else None
        if not cursor or not items:
            break

    meetings = []
    for m in raw:
        mapped = map_meeting(m)
        mid = m.get("id") or m.get("note_id")
        if mid and _wants_transcript(m, since_hours, now):
            throttle.wait()
            status, body = http_get(f"{base}/notes/{mid}?include=transcript", headers)
            if status == 200:
                try:
                    detail = json.loads(body)
                    detail = detail.get("note") if isinstance(detail.get("note"), dict) else detail
                    t = _extract_transcript(detail) or _extract_transcript(detail.get("transcript"))
                    if t:
                        mapped["transcript"] = t
                    # The detail may also carry fuller notes/summary than the list item did.
                    for src, dst in (("notes", "your_notes"), ("summary", "ai_summary")):
                        if not mapped.get(dst) and isinstance(detail.get(src), str):
                            mapped[dst] = detail[src]
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"note detail parse failed for '{mapped['title']}': {e}")
            else:
                warnings.append(f"GET /notes/{mid} HTTP {status}")
        meetings.append(mapped)
    return meetings, warnings


# ---------------------------------------------------------------------------

def _write(out_path: str, meetings: list) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"meetings": meetings}, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/sotto_granola.json")
    ap.add_argument("--days", type=int, default=14, help="meeting list window (days back)")
    ap.add_argument("--transcripts-since-hours", dest="since_hours", type=int, default=36,
                    help="fetch full transcripts for meetings that ended within this window")
    ap.add_argument("--rest", action="store_true",
                    help="force the GRANOLA_API_KEY REST lane (break-glass; Business plans)")
    a = ap.parse_args()

    meetings, warnings, lane = [], [], None
    api_key = os.environ.get("GRANOLA_API_KEY", "").strip()
    try:
        if a.rest:
            if not api_key:
                raise ConnectorMissing("--rest requires GRANOLA_API_KEY")
            lane = "rest"
            meetings, warnings = gather_rest(api_key, a.days, a.since_hours)
        else:
            try:
                lane = "mcp"
                meetings, warnings = gather_mcp_with_retry(a.days, a.since_hours)
            except ConnectorMissing:
                if api_key:                      # no connector, but a key exists → break-glass lane
                    lane = "rest"
                    meetings, warnings = gather_rest(api_key, a.days, a.since_hours)
                else:
                    lane = None
                    warnings.append("Granola not connected (no connector token, no GRANOLA_API_KEY) "
                                    "— connect Granola on /setup")
    except ConnectorAuthError as e:
        warnings.append(f"Granola auth failed — reconnect on /setup: {e}")
        meetings = []
    except Exception as e:  # noqa: BLE001 — fail-empty, never kill the brief
        warnings.append(f"{lane or 'gather'} lane failed: {e}")
        meetings = []

    _write(a.out, meetings)
    with_t = sum(1 for m in meetings if m.get("transcript"))
    with_n = sum(1 for m in meetings if m.get("ai_summary") or m.get("your_notes"))
    msg = (f"[gather_granola] {len(meetings)} meetings ({with_t} with transcripts, "
           f"{with_n} with notes) → {a.out}")
    if lane:
        msg += f"  [lane: {lane}]"
    for w in warnings:
        msg += f"  (WARNING: {w})"
    print(msg)
    _diag(msg)   # persist the REASON for an empty gather to /debug/brief-log


if __name__ == "__main__":
    main()
