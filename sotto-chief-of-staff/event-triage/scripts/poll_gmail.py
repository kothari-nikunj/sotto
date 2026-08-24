#!/usr/bin/env python3
"""
poll_gmail.py — cloud-side email events for the Phase 2 funnel (no Pub/Sub setup burden).

The receiver's Gmail poll thread runs this every SOTTO_EMAIL_POLL_SECS. One run:
  1. locate the google-workspace `google_api.py` CLI (same discovery as gather_google.py),
  2. `gmail search "newer_than:1h in:inbox" --max 20` — plus a smaller `in:sent` lane (SENT_QUERY):
     the user's OWN outbound mail, marked `is_from_me: true`, which Tier 0 queues as a silent
     "signal" (never a nudge, never Tier-1) — the email half of what the Bridge's is_from_me rows
     already provide for texts. It exists so the draft→outcome matcher can grade email drafts and
     deterministic loop resolution can see email replies; a sent-lane failure never costs the
     inbox lane,
  3. dedupe against the capped ring $SOTTO_DATA/events/gmail_seen.json (one ring, both lanes),
  4. fetch full bodies for the NEW ids only (gather_google's per-message `gmail get` pattern),
  5. print the events JSON the triage funnel expects:
       [{"source":"email","rowid":"<gmail id>","from":…,"to":…,"cc":…,"subject":…,"body":…,…}]

The poll command is CLAIM-FREE: it does not advance gmail_seen.json. The receiver acknowledges the
returned message ids with `--ack` only after its event pipeline returns 200. A fetch/parse failure
exits non-zero so the receiver can distinguish a broken lane from a genuinely quiet inbox.

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
SENT_QUERY = "newer_than:1h in:sent"
SENT_MAX = 15


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


def acknowledge(ids: list[str]) -> int:
    """Commit ids only after the receiver durably accepted their events.

    The Gmail poll and receiver are separate processes, so this tiny CLI handshake is the cursor
    transaction boundary: fetch is read-only; `--ack` is the commit. Repeated acknowledgements are
    harmless and the ring remains capped.
    """
    clean = [str(v).strip() for v in ids if str(v).strip()]
    if not clean:
        return 0
    seen = _load_seen()
    known = set(seen)
    fresh = []
    for v in clean:
        if v not in known:
            known.add(v)
            fresh.append(v)
    if fresh:
        _save_seen(seen + fresh)
    return len(fresh)


def _to_event(item: dict, full: dict) -> dict:
    mid = _pick(item, "id", "message_id", "messageId")
    return {
        "source": "email",
        "rowid": str(mid),
        "from": _addr_str(_pick(full, "from", "sender") or _pick(item, "from", "sender") or ""),
        # To/Cc feed exactly one triage signal: "the user is Cc'd, not To'd" (triage_event.
        # _addressed_line) — a reply aimed at someone else (an intro handoff) must not read as an
        # ask of the user. Absent fields simply omit the signal; nothing downstream requires them.
        "to": _addr_str(_pick(full, "to", "to_recipients", "toRecipients")
                        or _pick(item, "to") or ""),
        "cc": _addr_str(_pick(full, "cc", "cc_recipients", "ccRecipients")
                        or _pick(item, "cc") or ""),
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
    sent_ids = set()
    try:
        # The sent lane is additive and fail-silent ON ITS OWN: a broken in:sent search must never
        # cost the inbox lane (the funnel's whole email intake).
        for it in _as_list(_run(api, ["gmail", "search", SENT_QUERY, "--max", str(SENT_MAX)])):
            if isinstance(it, dict) and _pick(it, "id", "message_id", "messageId"):
                sent_ids.add(str(_pick(it, "id", "message_id", "messageId")))
                items.append(it)
    except Exception:  # noqa: BLE001
        pass
    seen = _load_seen()
    seen_set = set(seen)
    new, new_ids = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        mid = _pick(it, "id", "message_id", "messageId")
        if not mid or str(mid) in seen_set or str(mid) in new_ids:
            continue   # one ring, both lanes — a self-addressed mail is one event, not two
        new.append(it)
        new_ids.add(str(mid))
    events = []
    for it in new:
        mid = str(_pick(it, "id", "message_id", "messageId"))
        full = {}
        try:
            full = _run(api, ["gmail", "get", mid], timeout=30) or {}
        except Exception:  # noqa: BLE001
            pass   # snippet-only event is still an event
        ev = _to_event(it, full)
        if mid in sent_ids:
            ev["is_from_me"] = True   # Tier 0 queues these as silent signals, never a nudge
        events.append(ev)
    return events


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--ack":
        print(json.dumps({"acknowledged": acknowledge(sys.argv[2:])}))
        return
    try:
        events = poll()
        if events:
            outbound = sum(1 for e in events if e.get("is_from_me"))
            _diag(f"[poll_gmail] {len(events) - outbound} new inbox message(s), {outbound} sent")
        print(json.dumps(events))
    except Exception as e:  # noqa: BLE001
        _diag(f"[poll_gmail] poll failed: {e}")
        print(json.dumps({"error": str(e)[:300]}))
        sys.exit(1)


if __name__ == "__main__":
    main()
