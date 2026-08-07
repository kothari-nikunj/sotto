#!/usr/bin/env python3
"""
poll_gmail.py — cloud-side email events for the Phase 2 funnel (no Pub/Sub setup burden).

The receiver's Gmail poll thread runs this every SOTTO_EMAIL_POLL_SECS. One run:
  1. locate the google-workspace `google_api.py` CLI (same discovery as gather_google.py),
  2. `gmail search "newer_than:1h in:inbox" --max 20`,
  3. dedupe against the capped ring $SOTTO_DATA/events/gmail_seen.json,
  4. fetch full bodies for the NEW ids only (gather_google's per-message `gmail get` pattern),
  5. print the events JSON the triage funnel expects:
       [{"source":"email","rowid":"<gmail id>","from":…,"subject":…,"body":…,"threadId":…,"date":…}]

Fail-silent by contract: ANY failure prints `[]`, logs one diag line (sotto_log → /debug/brief-log),
and exits 0 — the poll thread must never see a crash, and a missed poll is retried in ~90s anyway.

Env: SOTTO_DATA (state dir), HERMES_HOME (optional install root override).
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED_LIB = os.path.join(_HERE, "..", "..", "_shared", "lib")
if _SHARED_LIB not in sys.path:
    sys.path.insert(0, _SHARED_LIB)

GMAIL_SEEN_MAX = 1000   # ring size — 20 msgs/poll × ~1h windows leaves plenty of overlap margin
SEARCH_QUERY = "newer_than:1h in:inbox"
SEARCH_MAX = 20


def _diag(msg: str) -> None:
    try:
        from sotto_log import diag  # noqa: PLC0415
        diag(msg)
    except Exception:  # noqa: BLE001
        print(msg, file=sys.stderr)


def _find_google_api():
    """Locate the google-workspace skill's google_api.py (same discovery as gather_google.py)."""
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


def _as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        for k in ("messages", "emails", "items", "results"):
            if isinstance(v.get(k), list):
                return v[k]
    return []


def _pick(d, *keys):
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if v not in (None, "", [], {}):
            return v
    return None


def _addr_str(v):
    """Coerce a from field to a string (google_api CLI / MCP variants: str, {name,email}, list)."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        name, email = v.get("name") or "", v.get("email") or v.get("address") or ""
        return f"{name} <{email}>".strip() if email else (name or "")
    if isinstance(v, list):
        return ", ".join(_addr_str(x) for x in v if x)
    return ""


def _seen_path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "events", "gmail_seen.json")


def _load_seen() -> list:
    try:
        with open(_seen_path(), encoding="utf-8") as f:
            v = json.load(f)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save_seen(ids: list) -> None:
    """Capped ring, atomic write — mirrors the receiver's seen.json handling."""
    try:
        path = _seen_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ids[-GMAIL_SEEN_MAX:], f)
        os.replace(tmp, path)
    except OSError:
        pass


def _to_event(item: dict, full: dict) -> dict:
    mid = _pick(item, "id", "message_id", "messageId")
    return {
        "source": "email",
        "rowid": str(mid),
        "from": _addr_str(_pick(full, "from", "sender") or _pick(item, "from", "sender") or ""),
        "subject": _pick(full, "subject", "title") or _pick(item, "subject", "title") or "",
        "body": _pick(full, "body", "text", "content", "plain_text")
                or _pick(item, "snippet", "preview") or "",
        "threadId": _pick(item, "threadId", "thread_id") or _pick(full, "threadId", "thread_id") or "",
        "date": _pick(full, "date", "internalDate") or _pick(item, "date", "internalDate") or "",
    }


def poll() -> list:
    api = _find_google_api()
    if not api:
        raise RuntimeError("google_api.py not found — google-workspace skill missing")
    items = _as_list(_run(api, ["gmail", "search", SEARCH_QUERY, "--max", str(SEARCH_MAX)]))
    seen = _load_seen()
    seen_set = set(seen)
    new = [it for it in items
           if isinstance(it, dict) and _pick(it, "id", "message_id", "messageId")
           and str(_pick(it, "id", "message_id", "messageId")) not in seen_set]
    events = []
    for it in new:
        mid = str(_pick(it, "id", "message_id", "messageId"))
        full = {}
        try:
            full = _run(api, ["gmail", "get", mid], timeout=30) or {}
        except Exception:  # noqa: BLE001
            pass   # snippet-only event is still an event
        events.append(_to_event(it, full))
    if new:
        _save_seen(seen + [str(_pick(it, "id", "message_id", "messageId")) for it in new])
    return events


def main():
    try:
        events = poll()
        if events:
            _diag(f"[poll_gmail] {len(events)} new inbox message(s)")
        print(json.dumps(events))
    except Exception as e:  # noqa: BLE001
        _diag(f"[poll_gmail] poll failed (silent): {e}")
        print("[]")
    sys.exit(0)


if __name__ == "__main__":
    main()
