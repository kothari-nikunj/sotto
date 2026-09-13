#!/usr/bin/env python3
"""
draft_outcomes.py — match offered drafts against what the user actually sent (Step 3's middle).

In one sentence: a draft matches the first message you sent to the same person within 24 hours of
the offer — ≥0.95 similarity is sent-verbatim, ≥0.60 is edited-and-sent, and a 24-hour-old draft
with no match is recorded as dismissed (legacy vocabulary, not explicit feedback); every terminal outcome lands in outcomes.jsonl through log_outcome
and a verbatim send confirms its style
sample so the voice register finally gets graded.

The two halves it joins:
  left  — $SOTTO_DATA/events/drafts.jsonl, written by action_links.record_draft (every tap link
          built with a message). Matching is fuzzy BY ARCHITECTURE: a message you send from your
          phone can't carry an id, so counterpart + time window + text similarity is the whole
          contract.
  right — $SOTTO_DATA/events/queue.jsonl "signal" rows: your own outbound messages, which the
          Bridge watcher pushes through the funnel (is_from_me → queued silently, never nudged).

We only grade what we can see, and "can see" is checked per draft: a draft is dismissed only when
its channel's outbound lane showed life during the window (at least one sent-message signal from
that lane) — a Mac that slept all day, or a disabled Gmail poll, must never turn every offered
draft into a false "dismissed" that misstates draft usage. Email is
observable since the poll's `in:sent` lane (poll_gmail.SENT_QUERY) started queuing the user's own
outbound mail as signals; email signal text is compared with the quoted reply tail stripped, since
a Gmail reply body carries the whole thread below the new words.

Automatic mute suggestions do not consume these dismissal signals. Cross-channel attribution and
a distinct non-use outcome belong to the coordinated tracking redesign.

Runs directly in learn_step.py; also a CLI (prints the run summary as JSON) for on-demand runs and tests. Idempotent:
outcomes.jsonl rows carry the draft's key as action_id, and a draft with a recorded outcome is
never graded twice.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                                        # log_outcome, action_links
sys.path.insert(0, os.path.join(_HERE, "..", "lib"))             # keys

import log_outcome  # noqa: E402
from keys import sample_hash  # noqa: E402

MATCH_WINDOW_HOURS = 24      # a draft older than this with no match was dismissed
VERBATIM_SIM = 0.95          # ≥ this similarity = sent verbatim → outcome "executed" + style confirm
EDITED_SIM = 0.60            # ≥ this = the draft was the base → outcome "edited_and_sent"
# Draft channel → the event source its outbound lane arrives on (iMessage carries SMS rows too).
LANE_SOURCE = {"imessage": "imessage", "sms": "imessage", "whatsapp": "whatsapp",
               "email": "email", "gmail": "email"}
OBSERVABLE_CHANNELS = frozenset(LANE_SOURCE)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+[\w.-]*\.[A-Za-z]{2,}")
_QUOTE_RE = re.compile(r"^\s*(>|On .{0,120} wrote:|-{2,}\s*Forwarded message)", re.M)


def _root() -> str:
    return os.environ.get("SOTTO_DATA", "/data")


def _events(name: str) -> str:
    return os.path.join(_root(), "events", name)


def _parse_ts(s: str):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _rows(path: str) -> list:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # A torn write or a stray echo can leave a non-object line; it is not a row, and
                # since Learn runs this grader directly one such line must not fail the receipt.
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        pass
    return out


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _similarity(a: str, b: str) -> float:
    a, b = _norm_text(a), _norm_text(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _digits_tail(s: str) -> str:
    """Last 10 digits — the pragmatic phone identity ("+1 (415) 555-1234" == "4155551234")."""
    d = re.sub(r"\D", "", s or "")
    return d[-10:] if len(d) >= 7 else d


def _counterpart_key(channel: str, identifier: str) -> str:
    if channel in ("imessage", "sms", "whatsapp"):
        return _digits_tail(identifier)
    return (identifier or "").strip().lower()


def _strip_quoted(text: str) -> str:
    """A Gmail reply body carries the whole thread below the new words — cut at the first quote
    marker so similarity compares what the user WROTE, not what they replied to."""
    m = _QUOTE_RE.search(text or "")
    return text[:m.start()] if m else (text or "")


def _signal_counterparts(event: dict) -> set:
    """Everyone an outbound message went TO, as match keys. Bridge rows put the counterpart in
    `handle` (iMessage/SMS) or `contact_jid` ("1555…@s.whatsapp.net"); a sent email names its
    people in To/Cc — any of them matching the draft's recipient is a match."""
    if (event.get("source") or "").lower() == "email":
        addrs = _EMAIL_RE.findall(f"{event.get('to') or ''} {event.get('cc') or ''}")
        return {a.lower() for a in addrs}
    for field in ("handle", "phone"):
        v = event.get(field)
        if isinstance(v, str) and v.strip():
            return {_digits_tail(v)}
    jid = event.get("contact_jid")
    if isinstance(jid, str) and "@" in jid:
        return {_digits_tail(jid.split("@", 1)[0])}
    return set()


def _draft_key(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _graded_keys() -> set:
    return {str(r.get("action_id")) for r in _rows(log_outcome._path()) if r.get("action_id")}


def _confirm_style_sample(sent_text: str) -> bool:
    """A draft sent verbatim is the user's voice validated by pressing send — find the observed
    style sample carrying that text and move it into style.json's `confirmed` bucket through the
    bucket's ONE writer (style_extract.confirm_sample). Best-effort: the sample may not be
    extracted yet (the Learn step's extract order), in which case tomorrow's run catches it."""
    try:
        import style_extract  # noqa: PLC0415
        with open(os.path.join(_root(), "style.json"), encoding="utf-8") as f:
            style = json.load(f) or {}
        pool = []
        canonical = style.get("canonical")
        if isinstance(canonical, dict):
            for bucket in canonical.values():
                pool += [s for s in (bucket or []) if isinstance(s, dict)]
        pool += [s for s in (style.get("recent") or []) if isinstance(s, dict)]
        want = _norm_text(sent_text)
        hit = next((s for s in pool if _norm_text(s.get("text", "")) == want), None)
        if hit is None:
            return False
        return bool(style_extract.confirm_sample(sample_hash(hit)).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def run(now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    graded = _graded_keys()
    signals = []
    for row in _rows(_events("queue.jsonl")):
        if row.get("verdict_class") != "signal":
            continue
        ev = row.get("event") or {}
        source = (ev.get("source") or "").lower()
        text = ev.get("text") or ev.get("body") or ""
        if source == "email":
            text = _strip_quoted(text)
        ts = _parse_ts(row.get("ts", ""))
        if not ev.get("is_from_me") or not text.strip() or ts is None:
            continue
        signals.append({"ts": ts, "text": text, "source": source,
                        "who": _signal_counterparts(ev)})

    summary = {"graded": 0, "executed": 0, "edited_and_sent": 0, "dismissed": 0,
               "pending": 0, "skipped_unobservable": 0, "style_confirmed": 0}
    window = timedelta(hours=MATCH_WINDOW_HOURS)
    for draft in _rows(action_links_drafts_path()):
        key = _draft_key(draft)
        if key in graded:
            continue
        ts = _parse_ts(draft.get("ts", ""))
        channel = (draft.get("channel") or "").lower()
        if ts is None:
            continue
        if channel not in OBSERVABLE_CHANNELS:
            summary["skipped_unobservable"] += 1
            continue
        lane = LANE_SOURCE[channel]
        in_window = [s for s in signals if s["source"] == lane and ts <= s["ts"] <= ts + window]
        who = _counterpart_key(channel, draft.get("identifier", ""))
        best, best_sim = None, 0.0
        for s in in_window:
            if who not in s["who"]:
                continue
            sim = _similarity(draft.get("text", ""), s["text"])
            if sim > best_sim:
                best, best_sim = s, sim
        rec = {"action_id": key, "channel": channel,
               "contact": draft.get("identifier", ""),
               "action_type": draft.get("action_type") or "reply"}
        if best is not None and best_sim >= EDITED_SIM:
            outcome = "executed" if best_sim >= VERBATIM_SIM else "edited_and_sent"
            rec.update({"outcome": outcome, "tier": "one_tap",
                        "edits": f"similarity {best_sim:.2f}"})
            log_outcome.log(rec)
            summary[outcome] += 1
            summary["graded"] += 1
            if best_sim >= VERBATIM_SIM and _confirm_style_sample(best["text"]):
                summary["style_confirmed"] += 1
        elif now - ts > window and in_window:
            # Dismissed ONLY when the lane was demonstrably alive during the window: the user sent
            # other messages there and still didn't use this draft. A silent lane (Mac asleep,
            # Gmail poll off) leaves the draft ungraded — no verdict beats a false one.
            rec["outcome"] = "dismissed"
            log_outcome.log(rec)
            summary["dismissed"] += 1
            summary["graded"] += 1
        else:
            summary["pending"] += 1
    return summary


def action_links_drafts_path() -> str:
    """One path, one owner: action_links.drafts_path() writes it, we read it. Imported lazily so the grader can still find historical drafts if the link builder is unavailable."""
    try:
        import action_links  # noqa: PLC0415
        return action_links.drafts_path()
    except Exception:  # noqa: BLE001
        return _events("drafts.jsonl")


def main():
    print(json.dumps(run()))


if __name__ == "__main__":
    main()
