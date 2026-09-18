#!/usr/bin/env python3
"""
digest_check.py — the adaptive midday catch-up gate (Phase 2 §3).

Reads the queue since the last digest/acknowledged brief. The existing activity gate (at least
SOTTO_DIGEST_MIN, default 8, known-sender ambient/deferred events) avoids reviewing empty activity;
one already-actionable item also triggers review. Neither gate decides what deserves delivery. One bounded native-model review applies
_shared/references/relevance.md to conversations, including later outbound replies. Unknown/group
rows may qualify on their substance; names and queue classes cannot make noise relevant.

Review precedes the six-item delivery cap and uses at most 100 conversations, 20 recent messages
per conversation, each message truncated by the shared conversation renderer
(personal_context.CONVERSATION_TEXT_CHARS). Existing deferred-actionable bands take priority
when bounding the review; the brief remains the backstop beyond it. Relevant action/decision items
lead, meaningful developments follow. No relevant items means completed silence; malformed judgments or provider failure
means retryable silence with an unchanged coverage window. No sender/category blacklist and no per-item model calls.
The user's own mutes are the one exception, and they are applied to ENTRIES before any context is
built (preferences.proactively_muted, the same predicate the funnel's ingress gate and the valve
use): a muted person's words never reach the review model, nor a delivered item's `messages`, even
when someone else in their group thread qualifies.

  digest_check.py              → the decision JSON (the skill consumes it verbatim);
                                 successful silent reviews advance the window; delivered reviews
                                 carry coverage_until for the receiver to acknowledge after sending
  digest_check.py --stamp      → records now to last_digest.txt (kept for the skill's post-deliver
                                 stamp; use --now coverage_until after acceptance)
                                 (the BRIEF also advances this window — brief_marker.claim() calls
                                 advance_stamp() when it WINS the deliver-once claim, so the 12:30
                                 digest can't re-surface what the delivered brief just covered;
                                 Sprint 0 §2c)
  digest_check.py --now ISO    → clock override (tests)

The marker style (--silent-check/--stamp) keeps the decision pure and testable; the skill only
composes and delivers what this prints.

Env: SOTTO_DATA (state dir), SOTTO_DIGEST_MIN (signal threshold, default 8).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED_LIB = os.path.realpath(os.path.join(_HERE, "..", "..", "_shared", "lib"))
_SHARED_SCRIPTS = os.path.realpath(os.path.join(_HERE, "..", "..", "_shared", "scripts"))
for _path in (_SHARED_LIB, _SHARED_SCRIPTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)
from textutil import _looks_like_phone_number, _s  # noqa: E402
from timeutil import _parse_ts  # noqa: E402
import relevance  # noqa: E402
import preferences  # noqa: E402
from personal_context import conversation_key, conversation_message  # noqa: E402

REVIEW_CONVERSATION_CAP = 100
REVIEW_MESSAGES_CAP = 20
# No per-message text cap here: conversation_message() (personal_context) owns that truncation.

ITEM_CAP = 6                                     # matches the skill's 6-line rule (Sprint 0 §2d)
# The proactive lane's synthetic event source — the contract with proactive_scan._proactive_event
# (and triage_event.PROACTIVE_SOURCE). A nudge Sotto queued is not a signal from a person.
PROACTIVE_SOURCE = "proactive"
# Classes that count toward "heavy day" (when the sender is known): Tier-1 ambient plus the
# deterministically-deferred agent verdicts (quiet hours / cooldown / stale-catchup / daily
# interrupt budget / user snooze / in-meeting hold).
COUNT_CLASSES = frozenset({"ambient", "quiet", "cooldown", "stale", "budget", "snoozed",
                           "meeting_hold", "actionable", "scheduling_ask"})
# Of those, the deferred-agent ones sort first in items — they were judged interrupt-worthy once.
ACTIONABLE_CLASSES = frozenset({"quiet", "cooldown", "stale", "budget", "snoozed", "meeting_hold", "actionable", "scheduling_ask"})


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _events_dir() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "events")


def queue_path() -> str:
    return os.path.join(_events_dir(), "queue.jsonl")


def stamp_path() -> str:
    return os.path.join(_events_dir(), "last_digest.txt")


def accepted_path() -> str:
    return os.path.join(_events_dir(), "digest_accepted.json")


def _accepted() -> set[str]:
    try:
        with open(accepted_path(), encoding="utf-8") as f:
            value = json.load(f)
        return {str(v) for v in value} if isinstance(value, list) else set()
    except Exception:  # noqa: BLE001
        return set()


def accept_items(ids: list[str]) -> bool:
    """Record provider-accepted digest items atomically; concurrent accepts merge."""
    import jsonstore
    try:
        with jsonstore.transaction(accepted_path(), default=[], strict=True) as current:
            if not isinstance(current, list):
                raise ValueError("invalid digest acceptance receipt")
            current[:] = list(dict.fromkeys([*map(str, current), *(str(v) for v in ids if v)]))
        return True
    except Exception:  # noqa: BLE001
        return False


def _parse_iso(raw):
    """ISO-8601 → aware UTC datetime, or None (naive treated as UTC — the marker's own
    semantics)."""
    if not raw:
        return None
    dt = _parse_ts(str(raw).strip())
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_stamp():
    """Last digest time as an aware UTC datetime, or None if never stamped / unreadable."""
    try:
        with open(stamp_path(), encoding="utf-8") as f:
            return _parse_iso(f.read())
    except OSError:
        return None


def write_stamp(now: datetime) -> None:
    """Persist `now` as the last digest (atomic). Best-effort — a missed stamp just widens the next
    window, never loses an item."""
    try:
        os.makedirs(_events_dir(), exist_ok=True)
        tmp = stamp_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        os.replace(tmp, stamp_path())
    except OSError:
        pass


def advance_stamp(now: datetime) -> None:
    """Move the digest window FORWARD to `now` — never backward (Sprint 0 §2c).

    The brief calls this (brief_marker.claim, on the claim that wins delivery) so the 12:30 digest
    can't re-surface what the 6:30a brief just covered: the window starts after the DELIVERED
    brief, not after yesterday's digest — a compose that loses the claim never stamps.
    Forward-only because this write has callers outside the digest's own clock — a re-run, an eval,
    or a backfilled compose must never rewind the window and replay items the user already saw.
    Best-effort, like write_stamp."""
    try:
        now = now.astimezone(timezone.utc)
        current = read_stamp()
        if current is not None and now <= current:
            return
        write_stamp(now)
    except (OSError, ValueError, AttributeError):
        pass


def entries_since(stamp) -> list:
    """queue.jsonl entries with ts AFTER `stamp` (all entries when never stamped). One bad line
    never poisons the read."""
    out = []
    try:
        with open(queue_path(), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if stamp is not None:
            ts = _parse_iso(entry.get("ts"))
            if ts is None or ts <= stamp:
                continue
        out.append(entry)
    return out


def _item(entry: dict) -> dict:
    ev = entry.get("event") if isinstance(entry.get("event"), dict) else {}
    sender = (_s(entry.get("sender")) or _s(ev.get("from")) or _s(ev.get("handle"))
              or _s(ev.get("contact_jid")) or _s(ev.get("phone")) or "unknown")
    preview = _s(ev.get("subject")) or _s(ev.get("text")) or _s(ev.get("body"))
    return {"sender": sender, "class": _s(entry.get("verdict_class")),
            "preview": preview[:200], "ts": _s(entry.get("ts"))}


def _sender_known(sender) -> bool:
    """The queue entry's `sender` (the resolved display name triage_event wrote) names a real
    person: non-empty, not the resolver's "Unknown" sentinel, not phone/shortcode-shaped."""
    n = _s(sender).strip()
    return bool(n) and n != "Unknown" and not _looks_like_phone_number(n)


def _from_a_person(entry: dict) -> bool:
    """The gate's actual question: is this a signal from a PERSON you know? A queued proactive nudge
    is Sotto's own voice with its title in the `sender` field — counting it as a known sender let a
    birthday and a tidy-up offer inflate the heavy-day threshold the digest exists to measure."""
    ev = entry.get("event") if isinstance(entry.get("event"), dict) else {}
    return _s(ev.get("source")) != PROACTIVE_SOURCE and _sender_known(entry.get("sender"))


def _counts_toward_threshold(entry: dict) -> bool:
    return (_s(entry.get("verdict_class")) in COUNT_CLASSES
            and _from_a_person(entry))


def _band(entry: dict) -> int:
    """Item priority band: 0 = known deferred-actionable, 1 = known ambient, 2 = the rest
    (unknown/group/error/Sotto's own held nudges) — below the fold."""
    if _from_a_person(entry):
        cls = _s(entry.get("verdict_class"))
        if cls in ACTIONABLE_CLASSES:
            return 0
        if cls == "ambient":
            return 1
    return 2


def review_conversations(conversations: list) -> list:
    return relevance.judge(json.dumps(conversations, ensure_ascii=False), batch=True,
                           label=" [digest relevance]")


def _conversation_key(entry: dict) -> str:
    return conversation_key(entry.get("event") or {})


def _item_id(key: str, entries: list[dict]) -> str:
    """A conversation version: stable on retry, new when another observed message arrives."""
    from work_queue import event_item_key
    identities = [event_item_key(e.get("event") or {}) for e in entries[-REVIEW_MESSAGES_CAP:]]
    return hashlib.sha256((key + "\0" + "\0".join(identities)).encode()).hexdigest()[:32]


def _message(entry: dict) -> dict:
    ev = entry.get("event") if isinstance(entry.get("event"), dict) else {}
    return conversation_message(ev, timestamp=_s(entry.get("ts")),
                                prior_class=_s(entry.get("verdict_class")))


def check(entries: list, min_n: int | None = None, *, track_review: bool = False) -> dict:
    """Activity gate → bounded conversation review → relevance ranking → delivery cap.

    All context is read-only. The injectable review_conversations seam tests selection without
    paid calls; live judgment evaluation uses that same seam and the actual shared policy.
    """
    from source_context import allowed
    import delivery_effects
    entries = [e for e in entries if (not delivery_effects.source_for_event(e.get('event') or {})
                or allowed(delivery_effects.source_for_event(e.get('event') or {})))]
    min_n = _int_env("SOTTO_DIGEST_MIN", 8) if min_n is None else min_n
    prefs = preferences.load_explicit()
    # A muted person leaves the review BEFORE anything is built from it — not just the candidate
    # list. The message context below is assembled per ENTRY, so filtering only candidates still
    # sent a muted person's words to the review model (and into the delivered item's `messages`)
    # whenever someone else in their group thread qualified. Per entry sender, once, here.
    entries = [e for e in entries
               if not preferences.proactively_muted(_s(e.get("sender")), e.get("event") or {}, prefs)]
    candidates = [e for e in entries if _s(e.get("verdict_class")) != "signal"
                  and e.get("verdict") != "drop"
                  and not relevance.is_automated_assistant(e.get("event") or {})]
    actionable = any((_s(e.get('verdict_class')) in ACTIONABLE_CLASSES
                      or _s(e.get('held_class')) in ('urgent', 'actionable', 'scheduling_ask'))
                     and _s((e.get('event') or {}).get('source')) != PROACTIVE_SOURCE
                     for e in candidates)
    if not actionable and sum(_counts_toward_threshold(e) for e in candidates) < min_n:
        return {"deliver": False}
    # Keep every message as context: newest-per-person alone can hide an ask or its later answer.
    by_sender: dict = {}
    entries_by_key: dict[str, list[dict]] = {}
    for entry in entries:
        entries_by_key.setdefault(_conversation_key(entry), []).append(entry)
    for entry in reversed(candidates):
        key = _conversation_key(entry)
        if key not in by_sender:
            by_sender[key] = entry
        elif _band(entry) < _band(by_sender[key]):
            by_sender[key] = entry
    accepted = _accepted()
    ranked_all = [pair for pair in sorted(by_sender.items(), key=lambda pair: _band(pair[1]))
                  if _item_id(pair[0], entries_by_key[pair[0]]) not in accepted]
    ranked = ranked_all[:REVIEW_CONVERSATION_CAP]
    messages = {key: [_message(entry) for entry in entries_by_key[key]] for key, _ in ranked}
    conversations = [{"id": i, "sender": _item(entry)["sender"],
                      "messages": sorted(messages[key], key=lambda m: delivery_effects.instant(m['ts']) or 0)[-REVIEW_MESSAGES_CAP:]}
                     for i, (key, entry) in enumerate(ranked)]
    try:
        judgments = review_conversations(conversations)
        # Fail closed on missing/duplicate/invented IDs, not partial model output silently accepted.
        ids = [j.get("id") for j in judgments]
        if (any(type(i) is not int for i in ids) or sorted(ids) != list(range(len(conversations)))
                or any(j.get("class") not in relevance.CLASSES or not _s(j.get("why"))
                       for j in judgments)):
            raise ValueError("incomplete digest relevance review")
        selected, reviewed_silent = [], []
        for j in judgments:
            if j["class"] == "ignore" or j.get("sender_role") == "assistant":
                i = j["id"]
                reviewed_silent.append(_item_id(ranked[i][0], entries_by_key[ranked[i][0]]))
                continue
            i = j["id"]
            entry = ranked[i][1]
            item = _item(entry)
            item.update(relevance=j["class"], why=j["why"], messages=conversations[i]["messages"])
            for field in ("deadline", "priority_id", "priority_revision"):
                if j.get(field) is not None:
                    item[field] = j[field]
            item["item_id"] = _item_id(ranked[i][0], entries_by_key[ranked[i][0]])
            band = ({"urgent": 0, "actionable": 1, "scheduling_ask": 1}.get(j["class"],
                    2 + int(_band(entry) == 2)))
            selected.append((band, relevance.tie_break_key(j), i, item))
        selected.sort(key=lambda row: (row[0], row[1], row[2]))
        chosen = selected[:ITEM_CAP]
        items = [item for _, _, _, item in chosen]
        if not items:
            result = ({'deliver': False} if len(ranked_all) <= REVIEW_CONVERSATION_CAP
                      else {'deliver': False, '_review_incomplete': True})
            if track_review:
                result['_reviewed_silent'] = reviewed_silent
            return result
        eligibility, deadlines = [], []
        for _, _, i, item in chosen:
            descriptor = delivery_effects.for_bundle({'events': [ranked[i][1]]})
            for effect in descriptor['effects']:
                # Replies already considered by this review are context. Only a newer observed
                # reply supersedes the held digest; otherwise a partial reply blocks it forever.
                effect['observed_until'] = max((m['ts'] for m in item['messages']),
                    key=lambda ts: delivery_effects.instant(ts) or 0, default=item['ts'])
            eligibility.extend(descriptor['effects'])
            if descriptor['valid_until'] is not None:
                deadlines.append(descriptor['valid_until'])
        result = {'deliver': True, 'items': items, 'item_ids': [i['item_id'] for i in items],
                  'effects': eligibility, 'valid_until': min(deadlines) if deadlines else None}
        if track_review:
            result['_reviewed_silent'] = reviewed_silent
        return result
    except Exception as err:  # noqa: BLE001 — a failed relevance review must never become a digest
        print(f"[digest] relevance review failed ({type(err).__name__}); staying silent", file=sys.stderr)
        return {"deliver": False, "status": "retry", "retryable": True, "error": type(err).__name__}


def run_check(now: datetime) -> dict:
    """A failed review preserves coverage; a delivered review closes it only on acceptance."""
    entries = [e for e in entries_since(read_stamp())
               if (_parse_iso(e.get('ts')) or now) <= now]
    result = check(entries, track_review=True)
    if result.get('retryable'):
        return result
    reviewed_silent = result.pop('_reviewed_silent', [])
    if reviewed_silent and not accept_items(reviewed_silent):
        return {"deliver": False, "status": "retry", "retryable": True,
                "error": "DigestReceiptError"}
    if result.get('deliver'):
        result['coverage_until'] = now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    elif not result.pop('_review_incomplete', False):
        advance_stamp(now)  # successfully reviewed silence is a completed window
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", action="store_true",
                    help="record this digest's time (run AFTER delivering)")
    ap.add_argument("--now", help="ISO timestamp override (tests)")
    a = ap.parse_args()
    now = _parse_iso(a.now) or datetime.now(timezone.utc)
    if a.stamp:
        advance_stamp(now)
        print("stamped")
        return
    result = run_check(now)
    print(json.dumps(result))
    if result.get("retryable"):
        sys.exit(75)


if __name__ == "__main__":
    main()
