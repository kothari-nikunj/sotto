#!/usr/bin/env python3
"""
poll_gmail.py — cloud-side email events for the Phase 2 funnel (no Pub/Sub setup burden).

The receiver's Gmail poll thread runs this every SOTTO_EMAIL_POLL_SECS. One run:
  1. locate the google-workspace `google_api.py` CLI (same discovery as gather_google.py),
  2. page through fixed, at-most-24-hour inbox slices (70 messages/pass), plus a
     smaller sent lane (30/pass):
     the user's OWN outbound mail, marked `is_from_me: true`, which Tier 0 queues as a silent
     "signal" (never a nudge, never Tier-1) — the email half of what the Bridge's is_from_me rows
     already provide for texts. It exists so the draft→outcome matcher can grade email drafts and
     deterministic loop resolution can see email replies; a sent-lane failure never costs the
     inbox lane,
  3. persist each page and dedupe against $SOTTO_DATA/events/gmail_seen.json (one state, both lanes),
  4. fetch full bodies for the NEW ids only (gather_google's per-message `gmail get` pattern),
  5. print the events JSON the triage funnel expects:
       [{"source":"email","rowid":"<gmail id>","from":…,"to":…,"cc":…,"subject":…,"body":…,…}]

The poll command may persist its current page, but never advances beyond it. The receiver
acknowledges returned message ids with `--ack` only after its event pipeline returns 200; only a
fully acknowledged page advances the cursor or page token. A failed full read stays on that page.

Env: SOTTO_DATA (state dir), HERMES_HOME (optional install root override).
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED_LIB = os.path.join(_HERE, "..", "..", "_shared", "lib")
if _SHARED_LIB not in sys.path:
    sys.path.insert(0, _SHARED_LIB)
from gmail_read import fetch_message, gmail_service  # noqa: E402

GMAIL_SEEN_MAX = 1000   # ring size — 20 msgs/poll × ~1h windows leaves plenty of overlap margin
SEARCH_QUERY = "in:inbox"
SEARCH_MAX = 70
SENT_QUERY = "in:sent"
SENT_MAX = 30
RECOVERY_LOOKBACK_SECONDS = 24 * 3600
GAP_SLICE_SECONDS = 24 * 3600
FRESH_GAP_SECONDS = 3600


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
    from google_cli import decode_output
    return decode_output(r.stdout, args)


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


def _load_state() -> dict:
    try:
        with open(_seen_path(), encoding="utf-8") as f:
            v = json.load(f)
    except FileNotFoundError:
        return {"seen": [], "lanes": {}}
    if isinstance(v, list):  # safe migration from the original seen-only ring
        return {"seen": [str(x) for x in v], "lanes": {}}
    if not isinstance(v, dict):
        raise ValueError("gmail state must be an object or legacy seen-id list")
    seen = v.get("seen", [])
    lanes = v.get("lanes", {})
    if not isinstance(seen, list) or not isinstance(lanes, dict):
        raise ValueError("gmail state has invalid seen or lanes shape")
    if any(not isinstance(lane, dict) for lane in lanes.values()):
        raise ValueError("gmail state lane must be an object")
    for lane in lanes.values():
        if "page_ids" in lane and not isinstance(lane["page_ids"], list):
            raise ValueError("gmail state page_ids must be a list")
    return {"seen": [str(x) for x in seen], "lanes": lanes}


def _load_seen() -> list:
    return _load_state()["seen"]


def _save_state(state: dict) -> None:
    """Capped ring, atomic write — mirrors the receiver's seen.json handling."""
    path = _seen_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        state["seen"] = state.get("seen", [])[-GMAIL_SEEN_MAX:]
        json.dump(state, f, sort_keys=True)
    os.replace(tmp, path)


def _complete_page(lane: dict) -> None:
    lane["page_ids"] = []
    if lane.get("next_page_token"):
        lane["page_token"] = lane.pop("next_page_token")
    else:
        lane["cursor"] = lane.get("query_end", lane.get("cursor", 0))
        for key in ("query_end", "query_catchup", "page_token", "next_page_token"):
            lane.pop(key, None)


def acknowledge(ids: list[str]) -> int:
    """Commit ids only after the receiver durably accepted their events.

    The Gmail poll and receiver are separate processes, so this tiny CLI handshake is the cursor
    transaction boundary: fetch is read-only; `--ack` is the commit. Repeated acknowledgements are
    harmless and the ring remains capped.
    """
    clean = [str(v).strip() for v in ids if str(v).strip()]
    if not clean:
        return 0
    state = _load_state()
    seen = state["seen"]
    known = set(seen)
    fresh = []
    for v in clean:
        if v not in known:
            known.add(v)
            fresh.append(v)
    if fresh:
        state["seen"] = seen + fresh
    # A page advances only when every id it exposed was durably accepted. A failed full read or
    # receiver failure therefore pins the page across restarts instead of moving beyond a hole.
    known.update(fresh)
    for lane in state["lanes"].values():
        page_ids = lane.get("page_ids") or []
        if page_ids and all(str(v) in known for v in page_ids):
            _complete_page(lane)
    _save_state(state)
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


def _page(service, lane: dict, query: str, maximum: int, now: int, api=None) -> list[dict]:
    if lane.get("page_ids"):
        return [{"id": str(v)} for v in lane["page_ids"]]
    cursor = int(lane.get("cursor") or (now - RECOVERY_LOOKBACK_SECONDS))
    lane["cursor"] = cursor
    end = int(lane.get("query_end") or min(cursor + GAP_SLICE_SECONDS, now))
    lane["query_end"] = end
    # Recovery is a property of the fixed query, not of each message's wall-clock age. Persist it
    # with the bounds so every page in a downtime gap receives the same treatment even when the
    # clock advances between polls.
    lane.setdefault("query_catchup", now - cursor > FRESH_GAP_SECONDS)
    params = {"userId": "me", "q": f"{query} after:{cursor} before:{end + 1}",
              "maxResults": maximum}
    if lane.get("page_token"):
        params["pageToken"] = lane["page_token"]
    if not hasattr(service, "users"):
        # The CLI search verb cannot express after/before or page tokens. Using it while advancing
        # this cursor would turn an unbounded read into a false coverage claim and skip older mail.
        raise RuntimeError("Gmail client lacks bounded paginated list support")
    result = service.users().messages().list(**params).execute()
    items = result.get("messages") or []
    lane["page_ids"] = [str(v.get("id")) for v in items if isinstance(v, dict) and v.get("id")]
    lane["next_page_token"] = result.get("nextPageToken") or ""
    if not lane["page_ids"]:  # an empty page is complete without an acknowledgement
        if lane["next_page_token"]:
            lane["page_token"] = lane.pop("next_page_token")
        else:
            lane["cursor"] = end
            for key in ("query_end", "query_catchup", "page_token", "next_page_token"):
                lane.pop(key, None)
    return items


def poll() -> list:
    api = _find_google_api()
    if not api:
        raise RuntimeError("google_api.py not found — google-workspace skill missing")
    state = _load_state()
    now = int(time.time())
    service = gmail_service()
    lanes = state["lanes"]
    inbox = lanes.setdefault("inbox", {})
    sent = lanes.setdefault("sent", {})
    items = _page(service, inbox, SEARCH_QUERY, SEARCH_MAX, now, api)
    sent_items = []
    try:
        sent_items = _page(service, sent, SENT_QUERY, SENT_MAX, now, api)
    except Exception:  # noqa: BLE001 — sent outcomes never cost inbox intake
        pass
    sent_ids = {str(v.get("id")) for v in sent_items if isinstance(v, dict) and v.get("id")}
    catchup_ids = {
        str(v.get("id"))
        for lane, lane_items in ((inbox, items), (sent, sent_items))
        if lane.get("query_catchup")
        for v in lane_items
        if isinstance(v, dict) and v.get("id")
    }
    items.extend(sent_items)
    known = set(state["seen"])
    # A page can consist entirely of overlap already accepted on an earlier page/run. There is
    # nothing for the receiver to acknowledge, so close it here or the cursor pins forever.
    for lane in (inbox, sent):
        if lane.get("page_ids") and all(str(v) in known for v in lane["page_ids"]):
            _complete_page(lane)
    _save_state(state)  # persist the page claim before any full-message read
    seen = state["seen"]
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
    if not new:
        return []
    try:
        events = []
        deferred = 0
        for it in new:
            mid = str(_pick(it, "id", "message_id", "messageId"))
            try:
                full = fetch_message(service, mid)
            except Exception:  # noqa: BLE001
                # Successfully read neighbors still flow. This id is absent from the output, so
                # the receiver cannot ack it and the seen ring retries it next poll.
                deferred += 1
                continue
            ev = _to_event(it, full)
            if mid in catchup_ids:
                ev["_sotto_catchup"] = True
            if mid in sent_ids:
                ev["is_from_me"] = True
            events.append(ev)
        if deferred:
            _diag(f"[poll_gmail] deferred {deferred} message(s) after full-read failure")
        return events
    finally:
        close = getattr(service, "close", None)
        if callable(close):
            close()


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
