#!/usr/bin/env python3
"""
draft_outcomes.py — match offered drafts against what the user actually sent (Step 3's middle).

In one sentence: the best-supported draft matches one observed message sent to the same canonical
counterpart within 24 hours of the offer — ≥0.95 similarity is sent-verbatim and ≥0.60 is
edited-and-sent; silence stays unknown, and every observed outcome lands in outcomes.jsonl through
log_outcome with the source event receipt that prevents another draft claiming the same send.

The two halves it joins:
  left  — $SOTTO_DATA/events/drafts.jsonl, written by action_links.record_draft (every tap link
          built with a message). Matching is fuzzy BY ARCHITECTURE: a message you send from your
          phone can't carry an id, so counterpart + time window + text similarity is the whole
          contract.
  right — $SOTTO_DATA/events/queue.jsonl "signal" rows: your own outbound messages, which the
          Bridge watcher pushes through the funnel (is_from_me → queued silently, never nudged).

Queue time is only ingestion time. Matching uses the event's original timestamp, works across
channels when the counterpart identity is the same, and stores a stable event identity in the
existing outcome log. Email signal text is compared with the quoted reply tail stripped, since a
Gmail reply body carries the whole thread below the new words.

Runs directly in learn_step.py; also a CLI (prints the run summary as JSON) for on-demand runs and tests. Idempotent:
outcomes.jsonl rows carry the draft's key as action_id, and a draft with a recorded outcome is
never graded twice.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                                        # log_outcome, action_links
sys.path.insert(0, os.path.join(_HERE, "..", "lib"))             # keys

import log_outcome  # noqa: E402
from keys import draft_key, sample_hash  # noqa: E402

MATCH_WINDOW_HOURS = 24      # observed sends match only within this window; silence stays unknown
VERBATIM_SIM = 0.95          # ≥ this similarity = sent verbatim → outcome "executed" + style confirm
EDITED_SIM = 0.60            # ≥ this = the draft was the base → outcome "edited_and_sent"
OBSERVABLE_CHANNELS = frozenset(("imessage", "sms", "whatsapp", "email", "gmail"))

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+[\w.-]*\.[A-Za-z]{2,}")
_QUOTE_RE = re.compile(r"^\s*(>|On .{0,120} wrote:|-{2,}\s*Forwarded message)", re.M)


def _root() -> str:
    return os.environ.get("SOTTO_DATA", "/data")


def _events(name: str) -> str:
    return os.path.join(_root(), "events", name)


def _parse_ts(s):
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        # Native adapters use seconds, milliseconds, or Apple's nanoseconds-since-2001.
        try:
            value = float(s)
            if value > 1.2 * 10**18:  # contemporary Unix nanoseconds
                return datetime.fromtimestamp(value / 1_000_000_000, timezone.utc)
            if value > 10**14:        # Apple nanoseconds since 2001
                return datetime.fromtimestamp(value / 1_000_000_000 + 978307200, timezone.utc)
            if value > 10**11:
                value /= 1000
            return datetime.fromtimestamp(value, timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        value = str(s or "").strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            return _parse_ts(float(value))
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
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


def _counterpart_key(channel: str, identifier: str) -> str:
    value = (identifier or "").strip().lower()
    jid = value.endswith(("@s.whatsapp.net", "@c.us"))
    if not jid and _EMAIL_RE.fullmatch(value):
        return "email:" + value
    if "@" in value and not jid:  # opaque WhatsApp @lid/@g.us identities are not phone numbers
        return ""
    phone = value.split("@", 1)[0] if jid else value
    if not re.fullmatch(r"\+?[\d\s().-]+", phone):
        return ""
    digits = re.sub(r"\D", "", phone)
    if not 7 <= len(digits) <= 15:
        return ""
    # A bare US local number may omit +1. Every explicit international form and every WhatsApp
    # JID keeps its complete country code, so two countries sharing the same final ten digits do
    # not become the same person.
    if len(digits) == 10 and not phone.startswith("+") and not jid:
        digits = "1" + digits
    return "phone:+" + digits


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
        return {_counterpart_key("email", a) for a in addrs}
    for field in ("handle", "phone"):
        v = event.get(field)
        if isinstance(v, str) and v.strip():
            return {_counterpart_key("imessage", v)}
    jid = event.get("contact_jid")
    if isinstance(jid, str) and "@" in jid:
        return {_counterpart_key("whatsapp", jid)}
    return set()


def _event_ts(row: dict, event: dict):
    """Native send time is evidence; the queue envelope only says when Sotto observed it."""
    for field in ("timestamp", "date", "sent_at", "internalDate", "date_sent"):
        parsed = _parse_ts(event.get(field))
        if parsed is not None:
            return parsed
    return None


def _event_id(event: dict, sent_at: datetime) -> str:
    source = (event.get("source") or "").lower()
    native = next((event.get(k) for k in ("rowid", "id", "message_id", "messageId")
                   if event.get(k) not in (None, "")), None)
    if native is not None:
        return f"{source}:{native}"
    material = {"source": source, "sent_at": sent_at.isoformat(),
                "counterparts": sorted(_signal_counterparts(event)),
                "text": _norm_text(event.get("text") or event.get("body") or "")}
    return source + ":sha256:" + __import__("hashlib").sha256(
        json.dumps(material, sort_keys=True).encode()).hexdigest()


# The ledger row id lives in keys.draft_key — style_extract prunes its confirmed-action markers by
# the same id and cannot import this module (this one imports it). Kept under the old private name
# for the callers that already reach for it.
_draft_key = draft_key


def _outcome_evidence() -> tuple[dict, set]:
    latest, claimed = {}, set()
    for row in _rows(log_outcome._path()):
        if row.get("action_id"):
            latest[str(row["action_id"])] = row
        if row.get("outcome") in ("executed", "edited_and_sent") and row.get("source_event_id"):
            claimed.add(str(row["source_event_id"]))
    return latest, claimed


def _legacy_inferred(row: dict) -> bool:
    """Rows emitted by the old automatic no-send grader may be repaired by later send evidence.
    A dismissal explicitly identified as user feedback is terminal and is never inferred here."""
    return (row.get("outcome") == "dismissed"
            and row.get("feedback_source") not in ("user", "explicit_user")
            and row.get("explicit") is not True
            and all(k in row for k in ("channel", "contact", "action_type")))


def _confirm_style_sample(sent_text: str, action_id: str) -> bool:
    """A draft sent verbatim is the user's voice validated by pressing send — find the observed
    style sample carrying that text and move it into style.json's `confirmed` bucket through the
    bucket's ONE writer (style_extract.confirm_sample). Best-effort: malformed or unavailable
    style state must not prevent the independently valid draft outcome from being recorded."""
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
        # Even when the capped prompt bucket has rotated this sample out, the action marker lets
        # confirm_sample report that the send was already handled without cycling the bucket.
        result = style_extract.confirm_sample(sample_hash(hit) if hit else "",
                                              action_id=action_id)
        return bool(result.get("ok") and not result.get("already"))
    except Exception:  # noqa: BLE001
        return False


def _run_locked(now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    outcomes, claimed = _outcome_evidence()
    signals = []
    for row in _rows(_events("queue.jsonl")):
        if row.get("verdict_class") != "signal":
            continue
        ev = row.get("event") or {}
        source = (ev.get("source") or "").lower()
        text = ev.get("text") or ev.get("body") or ""
        if source == "email":
            text = _strip_quoted(text)
        ts = _event_ts(row, ev)
        if not ev.get("is_from_me") or not text.strip() or ts is None:
            continue
        identity = _event_id(ev, ts)
        signals.append({"ts": ts, "text": text, "source": source, "id": identity,
                        "claimed": identity in claimed, "who": _signal_counterparts(ev)})

    summary = {"graded": 0, "executed": 0, "edited_and_sent": 0, "dismissed": 0,
               "pending": 0, "skipped_unobservable": 0, "style_confirmed": 0}
    window = timedelta(hours=MATCH_WINDOW_HOURS)
    drafts, candidates = [], []
    for draft in _rows(action_links_drafts_path()):
        key = _draft_key(draft)
        prior = outcomes.get(key)
        if prior and prior.get("outcome") == "executed" and prior.get("source_event_id"):
            observed = next((signal for signal in signals
                             if signal["id"] == prior["source_event_id"]), None)
            if (observed is not None
                    and _similarity(draft.get("text", ""), observed["text"]) >= VERBATIM_SIM
                    and _confirm_style_sample(observed["text"], key)):
                summary["style_confirmed"] += 1
        if prior and not _legacy_inferred(prior):
            continue
        ts = _parse_ts(draft.get("ts", ""))
        channel = (draft.get("channel") or "").lower()
        if ts is None:
            continue
        if channel not in OBSERVABLE_CHANNELS:
            summary["skipped_unobservable"] += 1
            continue
        who = _counterpart_key(channel, draft.get("identifier", ""))
        index = len(drafts)
        drafts.append((draft, key, ts, prior))
        for signal_index, signal in enumerate(signals):
            if (signal["claimed"] or not who or who not in signal["who"]
                    or not (ts <= signal["ts"] <= ts + window)):
                continue
            similarity = _similarity(draft.get("text", ""), signal["text"])
            if similarity >= EDITED_SIM:
                # Stronger text wins; at equal strength, the closest preceding offer is the best
                # supported attribution. Stable ids make the final tie deterministic.
                candidates.append((-similarity, (signal["ts"] - ts).total_seconds(), key,
                                   index, signal_index))

    matched_drafts, matched_signals = set(), set()
    for neg_sim, _distance, _key, draft_index, signal_index in sorted(candidates):
        if draft_index in matched_drafts or signal_index in matched_signals:
            continue
        matched_drafts.add(draft_index)
        matched_signals.add(signal_index)
        draft, key, _ts, _prior = drafts[draft_index]
        best, best_sim = signals[signal_index], -neg_sim
        channel = (draft.get("channel") or "").lower()
        rec = {"action_id": key, "channel": channel,
               "contact": draft.get("identifier", ""), "action_type": draft.get("action_type") or "reply",
               "source_event_id": best["id"], "source_event_ts": best["ts"].strftime("%Y-%m-%dT%H:%M:%SZ")}
        outcome = "executed" if best_sim >= VERBATIM_SIM else "edited_and_sent"
        rec.update({"outcome": outcome, "tier": "one_tap", "edits": f"similarity {best_sim:.2f}"})
        log_outcome.log(rec)
        summary[outcome] += 1
        summary["graded"] += 1
        if best_sim >= VERBATIM_SIM and _confirm_style_sample(best["text"], key):
            summary["style_confirmed"] += 1

    for index, (_draft, _key, _ts, _prior) in enumerate(drafts):
        if index not in matched_drafts:
            summary["pending"] += 1
    return summary


def run(now=None) -> dict:
    """Grade under the outcome log's lock so two Learn runs cannot claim one send."""
    from jsonstore import lock  # noqa: PLC0415
    with lock(log_outcome._path()):
        return _run_locked(now)


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
