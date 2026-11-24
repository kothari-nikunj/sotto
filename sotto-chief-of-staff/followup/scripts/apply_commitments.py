#!/usr/bin/env python3
"""
apply_commitments.py — write a followup's extracted commitments STRAIGHT into the continuity ledger.

Before this, compose_followup's `commitments[]` only reached the ledger if the NEXT brief happened to
carry them through its continuity payload — a day of lag and an easy drop. This is the deterministic
write: each commitment becomes a ledger item under $SOTTO_DATA/knowledge/continuity/ in the SAME
markdown+YAML-frontmatter shape continuity_resolve.py maintains (we reuse its loader/persister and
anchor_key machinery, so the brief's resolution sweep and sotto-loops read them natively).

Anchoring / dedupe:
  - the exact Granola occurrence keys on meeting_id + direction + normalized verbatim source
    snippet (falling back to commitment text for legacy payloads). The prose may vary between LLM
    runs without forking a loop; the same words in next week's meeting remain a new occurrence.
  - the extractor may point at an EXISTING open-loop anchor when the current source is clearly the
    same obligation. That bounded LLM judgment is validated against the live ledger and direction;
    an absent, invented or direction-inverted anchor is ignored and a new occurrence is written.
  - an existing ACTIVE item is reused without counting a surfacing; only an accepted delivery can
    increment that provenance. A TERMINAL (resolved/dismissed) item is left alone — we never
    resurrect something the user already closed.

Direction: the user's own commitments become `follow_up` (you owe); another attendee's become
`waiting_on` (they owe you) — exactly the split loops_query.py surfaces. Writes files only —
NEVER sends anything.

Automatic callers pass the meetings that produced the extraction. Before any write, a small
deterministic gate requires the meeting id and verbatim evidence to exist in that material, requires
the quoted evidence to name the claimed owner, and requires the deliverable to overlap the quote.
The model still interprets language; it cannot manufacture the evidence or reverse its direction.

Usage:
    apply_commitments.py /tmp/sotto_followup.json --user-email you@example.com
    (the file is compose_followup.py's full output, or any {"commitments":[...]} / bare array)
Prints {"written": N, "deduped": M, "skipped_terminal": K, "anchor_keys": [...]}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "morning-brief", "scripts"))
import continuity_resolve as cr  # noqa: E402
from ledger_provenance import source_refs  # noqa: E402
import capture_shape  # noqa: E402  (on the path continuity_resolve set up)

_USER_ALIASES = {"user", "the user", "me", "you", "i", "myself", "self"}

# Grammatical words that do not identify an obligation. Every remaining action/object token from
# `what` must occur in the verbatim evidence. This deliberately fails toward silence: "send the
# deck" cannot be supported by "I'll review the deck" merely because both mention the deck.
_GROUNDING_FILLER = {
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "i", "in", "it", "me", "my",
    "of", "on", "or", "our", "the", "their", "them", "they", "to", "us", "we", "will",
    "with", "you", "your",
}
# Timing inside the deliverable can distinguish debts (Monday report vs Tuesday report).
# Only grammatical words may disappear; the separately extracted due date is not identity.
_DELIVERABLE_IDENTITY_FILLER = _GROUNDING_FILLER | {'him', 'her'}
_PROMISE_PREFIX = re.compile(
    r"^([^!?\n:]+?)(?::\s*(?:i'll|i will|i plan to)|"
    r"\s+(?:will|plans? to|committed to))\s+", re.I)
_WHEN = (r"(?:\s+(?:today|tomorrow|later|(?:next|this) (?:week|month)|"
         r"(?:(?:on|next|this) )?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)))?")
_CLOSE_ASK = {
    "intro": re.compile(r"(?:intro(?:duce)? (.+?) to|connect (.+?) with) (.+)", re.I),
    "schedule": re.compile(
        r"(?:find (?:a )?time|pick a time|(?:book|schedule|set up) (?:a |the )?"
        r"(?:meeting|call|chat|catch-up))\s+with\s+(.+?)" + _WHEN, re.I),
    "callback": re.compile(r"(?:call(?:\s+back)?|phone|ring)\s+(.+?)(?:\s+back)?" + _WHEN, re.I),
}

_s = cr._s   # the shared string coercion (also ISO-stringifies YAML dates), not a private copy


def _owner_is_user(owner: str, user_email: str, user_name: str = "", meeting: dict | None = None) -> bool:
    o = _s(owner).strip().lower()
    if not o:
        return True   # unowned → treat as the user's (they're the one following up)
    if o in _USER_ALIASES:
        return True
    ue = _s(user_email).strip().lower()
    if ue and o == ue:
        return True
    un = _s(user_name).strip().lower()
    if un and o == un:
        return True
    # Transcript speakers often use first names while account identity stores the full name. Accept
    # that alias only when this meeting contains no second plausible person with the same first
    # name. Account email local-parts remain insufficient evidence for a different display name.
    if un and len(un.split()) > 1 and o == un.split()[0] and meeting is not None:
        first = un.split()[0]
        candidates = set()
        for source in _meeting_sources(meeting):
            for label in re.findall(r"(?:^|\n)\s*([^:\n]{1,80})\s*:", source):
                normalized = " ".join(_normalized_what(label).split())
                if normalized.split()[:1] == [first]:
                    candidates.add(normalized)
        return (not (_attendee_matches(first, meeting) - {ue})
                and all(candidate in {first, un} for candidate in candidates))
    return False


def _meeting_user_name(meeting: dict, user_email: str, configured_name: str) -> str:
    """Configured name, or the display name on the exact owner account attendee."""
    if _s(configured_name).strip():
        return _s(configured_name).strip()
    wanted = _s(user_email).strip().lower()
    for attendee in meeting.get('attendees', []) or []:
        if (isinstance(attendee, dict) and _s(attendee.get('email')).strip().lower() == wanted
                and _s(attendee.get('name') or attendee.get('display_name')).strip()):
            return _s(attendee.get('name') or attendee.get('display_name')).strip()
    return ""


def _normalized_what(value: str) -> str:
    # Granola attendees can have an email with a null/missing display name. Absence is not
    # identity evidence, and must not crash the grounding of every other commitment.
    return " ".join("".join(ch.lower() if ch.isalnum() else " "
                           for ch in (value if isinstance(value, str) else "")).split())


def _attendee_matches(marker: str, meeting: dict) -> set[str]:
    def matches(name):
        return name == marker or (" " not in marker and name.split()[:1] == [marker])

    matched = set()
    emails = {_s(email).strip().lower() for email in meeting.get("attendee_emails", []) or []}
    for attendee in meeting.get("attendees", []) or []:
        if isinstance(attendee, dict) and (email := _s(attendee.get("email")).strip().lower()):
            emails.add(email)
            if matches(_normalized_what(attendee.get("name") or attendee.get("display_name"))):
                matched.add(email)
    for email in emails - {""}:
        if matches(_normalized_what(re.split(r"[@+]", email, maxsplit=1)[0])):
            matched.add(email)
    return matched


def _deliverable_key(value: str) -> str:
    normalized = _normalized_what(value)
    verb_aliases = {'share': 'send', 'email': 'send', 'forward': 'send', 'provide': 'send',
                    'give': 'send', 'deliver': 'send'}
    words = [verb_aliases.get(word, word) for word in normalized.split()
             if word not in _DELIVERABLE_IDENTITY_FILLER]
    return ' '.join(words) or normalized


def _fold_excerpt(value: str) -> str:
    """Whitespace-tolerant exact-copy check; punctuation and words must still match."""
    return " ".join(_s(value).split())


def _meeting_sources(meeting: dict) -> list[str]:
    return [_s(meeting.get(k)) for k in ("your_notes", "ai_summary", "transcript")
            if _s(meeting.get(k)).strip()]


def _snippet_names_owner(snippet: str, owner: str, is_user: bool,
                         user_email: str, user_name: str, meeting: dict | None = None) -> bool:
    text = _s(snippet).lower().replace("’", "'")
    if is_user:
        markers = {"you", "the user"}
        local = _s(user_email).strip().lower().split("@", 1)[0]
        if local:
            markers.add(local)
        name = _s(user_name).strip().lower()
        if name:
            markers.update({name, name.split()[0]})
    else:
        name = _s(owner).strip().lower()
        markers = {name, name.split()[0]} if name else set()

    # Transcript form: "Dana: I'll send…". A speaker label only proves ownership when the quoted
    # speech itself is first-person; "Dana: Casey will send…" must not become Dana's obligation.
    first_person = r"(?:i(?:'ll|\s+will(?!\s+not)|\s+can|\s+am\s+going\s+to|\s+plan\s+to|\s+need\s+to)|we(?:'ll|\s+will(?!\s+not)|\s+can))"
    # Notes/summary form: "Dana will/to/needs to send…". Keeping the attribution adjacent avoids
    # accepting "Dana said Casey will send…" merely because both names occur in the excerpt.
    third_person = r"(?:will(?!\s+not)|is\s+going\s+to|plans?\s+to|needs?\s+to|committed\s+to|to)"
    for marker in markers:
        marker = marker.strip()
        if not marker:
            continue
        if meeting is not None:
            key = _normalized_what(marker)
            identities = _attendee_matches(key, meeting)
            owner_email = _s(user_email).strip().lower()
            owner_name = _normalized_what(user_name)
            if owner_email and (key == owner_name or owner_name.split()[:1] == [key]):
                identities.add(owner_email)
            if len(identities) > 1 or (is_user and identities - {owner_email}):
                continue
            if is_user and name and marker == name.split()[0] and not _owner_is_user(marker, user_email, name, meeting):
                continue
        escaped = re.escape(marker)
        if re.search(rf"(?:^|\n)\s*{escaped}\s*:\s*{first_person}\b", text):
            return True
        if re.search(rf"\b{escaped}\b\s+{third_person}\b", text):
            return True
    return False


def _snippet_supports_what(snippet: str, what: str) -> bool:
    evidence = set(_normalized_what(snippet).split())
    objects = {w for w in _normalized_what(what).split()
               if len(w) > 1 and w not in _GROUNDING_FILLER}
    return bool(objects and objects <= evidence)


def _closing_fields(capture: dict, commitment: dict, meeting: dict,
                    counterpart: str, external: list[str], owner_email: str, owner_name: str) -> dict:
    """A proposed signal must match the verb and recipient of this owner's quoted promise."""
    kind, target = capture["fulfillment"], capture["deliver_to"]
    if kind in (None, "none") or commitment["owner_is_user"] is not True or capture["owed_by"] == "them":
        return {"fulfillment": "none"} if kind == "none" else {}
    quote = _fold_excerpt(commitment["source_snippet"]).replace("’", "'")
    prefix = _PROMISE_PREFIX.match(quote)
    ask = _fold_excerpt(commitment["what"])
    if (not prefix or not _owner_is_user(prefix[1], owner_email, owner_name, meeting)
            or not re.match(re.escape(ask) + r"(?=$|[.!?](?:\s|$))", quote[prefix.end():], re.I)):
        return {}
    matched = _CLOSE_ASK[kind].fullmatch(ask)
    if not matched:
        return {}
    def identifies(recipient: str, email: str) -> bool:
        recipient = recipient.strip().lower()
        return recipient == email or _attendee_matches(_normalized_what(recipient), meeting) == {email}

    recipient = matched[1]
    if kind == "intro":
        recipient = recipient or matched[2]
        if not target or target == counterpart or not identifies(matched[3], target):
            return {}
    elif target not in (None, counterpart):
        return {}
    proven = external == [counterpart] if recipient.strip().lower() == "you" else identifies(recipient, counterpart)
    return {"fulfillment": kind, **({"deliver_to": target} if kind == "intro" else {})} if proven else {}


def ground_commitments(payload, source_meetings: list, user_email: str = "",
                       user_name: str = "") -> tuple[dict, dict]:
    """Keep only commitments whose occurrence, evidence, owner, and object are source-backed.

    Meeting material is untrusted input and the model output is an untrusted interpretation of it.
    This is intentionally not a second semantic system: it checks four mechanical claims the model
    is already required to make. Ambiguous cases are omitted for the user to recover from Granola,
    rather than becoming durable obligations and proactive chases.
    """
    out = dict(payload) if isinstance(payload, dict) else {"commitments": payload or []}
    meetings = {}
    for m in source_meetings or []:
        if not isinstance(m, dict):
            continue
        meeting_id = _s(m.get("meeting_id") or m.get("id")).strip()
        if meeting_id:
            meetings[meeting_id] = m

    accepted = []
    reasons = {"meeting": 0, "snippet": 0, "owner": 0, "deliverable": 0, "counterpart": 0}
    for c in out.get("commitments", []) or []:
        if not isinstance(c, dict) or not _s(c.get("what")).strip():
            continue
        meeting_id = _s(c.get("meeting_id")).strip()
        meeting = meetings.get(meeting_id)
        if meeting is None:
            reasons["meeting"] += 1
            continue

        snippet = _s(c.get("source_snippet")).strip()
        folded = _fold_excerpt(snippet)
        if not folded or not any(folded in _fold_excerpt(src) for src in _meeting_sources(meeting)):
            reasons["snippet"] += 1
            continue

        owner = _s(c.get("owner")).strip()
        claimed = c.get("owner_is_user")
        meeting_user_name = _meeting_user_name(meeting, user_email, user_name)
        if not isinstance(claimed, bool) or claimed != _owner_is_user(
                owner, user_email, meeting_user_name, meeting) or not _snippet_names_owner(
                    snippet, owner, claimed, user_email, meeting_user_name, meeting):
            reasons["owner"] += 1
            continue
        if not _snippet_supports_what(snippet, _s(c.get("what"))):
            reasons["deliverable"] += 1
            continue

        clean = dict(c)
        attendee_emails = {_s(e).strip().lower() for e in meeting.get("attendee_emails", [])
                           if _s(e).strip()}
        attendee_emails.update(_s(attendee.get('email')).strip().lower()
                               for attendee in meeting.get('attendees', []) or []
                               if isinstance(attendee, dict) and _s(attendee.get('email')).strip())
        owner_email = _s(user_email).strip().lower()
        if (_s(clean.get("to_email")).strip().lower() not in attendee_emails
                or _s(clean.get('to_email')).strip().lower() == owner_email):
            clean["to_email"] = None
        external = sorted(email for email in attendee_emails if email and email != owner_email)
        if claimed is True and not clean.get('to_email') and len(external) == 1:
            clean['to_email'] = external[0]
        elif claimed is False:
            matches = _attendee_matches(_normalized_what(owner), meeting) - {owner_email}
            clean['to_email'] = next(iter(matches)) if len(matches) == 1 else None
        if not clean.get('to_email'):
            reasons['counterpart'] += 1
            continue
        capture = capture_shape.fields(clean.get)
        clean.pop("deliverTo", None)
        clean.pop("deliver_to", None)
        clean.pop("fulfillment", None)
        clean.update(_closing_fields(capture, c, meeting, clean["to_email"].strip().lower(), external,
                                     owner_email, meeting_user_name))
        accepted.append(clean)

    out["commitments"] = accepted
    stats = {"accepted": len(accepted), "rejected": sum(reasons.values()), "reasons": reasons}
    return out, stats


def _action_for(c: dict, user_email: str, created_at: str, user_name: str = "", *, grounded=False):
    """Map one source-backed commitment into the continuity ledger's action shape."""
    what = _s(c.get("what")).strip()
    if not what:
        return None
    meeting = _s(c.get("meeting")).strip()
    owner = _s(c.get("owner")).strip()
    explicit_owner = c.get("owner_is_user")
    is_user = explicit_owner if isinstance(explicit_owner, bool) else _owner_is_user(
        owner, user_email, user_name)
    to_email = _s(c.get("to_email")).strip().lower()
    due = _s(c.get("due")).strip()
    meeting_id = _s(c.get("meeting_id")).strip()
    snippet = _s(c.get("source_snippet")).strip()[:500]
    # Only an ISO-ish date becomes a hard deadline (continuity's expiry compares date strings);
    # a fuzzy "Friday" stays in the summary instead of mis-expiring the loop.
    deadline = due if (due.startswith("20") and len(due) >= 10) else None

    summary = f"You committed to: {what}" if is_user else f"{owner or 'They'} owes: {what}"
    if meeting:
        summary += f' (from "{meeting}")'
    if due and not deadline:
        summary += f" — due {due}"

    capture = capture_shape.fields(c.get)
    normalized = _deliverable_key(what)
    action = {
        "action_type": "follow_up" if is_user else "waiting_on",
        "channel": "email" if to_email else "followup",
        "contact_identifier": to_email or None,
        "contact_name": (owner if not is_user else "") or (to_email.split("@")[0] if to_email else meeting),
        "summary": summary,
        "ask": what,
        "deadline": deadline,
        "created_at": created_at,
        "source_id": meeting_id or None,
        "source_snippet": snippet or None,
        "existing_anchor_key": _s(c.get("existing_anchor_key")).strip() or None,
        "deliverable": capture["deliverable"] if _deliverable_key(capture["deliverable"]) == normalized else None,
        "owed_by": capture["owed_by"],
        "fulfillment": capture["fulfillment"] if grounded else None,
        "deliver_to": capture["deliver_to"] if grounded else None,
    }
    role = "user" if is_user else "other"
    # Identity is meeting + verified counterpart + direction + a conservative normalized
    # deliverable. Only equivalent delivery verbs fold; action, qualifiers and numbers remain so
    # "review Q2" cannot collapse into "send Q3". The quote is evidence, not row identity.
    h = hashlib.sha256(f"{role}|{to_email}|{normalized}".encode()).hexdigest()[:12]
    if meeting_id:
        action["source_thread_id"] = f"granola:{meeting_id}:{h}"
    else:
        # Backward-compatible fallback for old staged payloads that predate meeting_id preservation.
        legacy = hashlib.sha256(f"{meeting}|{owner}|{what}".encode()).hexdigest()[:12]
        action["source_thread_id"] = f"commitment:{legacy}"
    return action


def _source_ref(raw: dict) -> dict | None:
    source_id = _s(raw.get("source_id")).strip()
    snippet = _s(raw.get("source_snippet")).strip()
    if not (source_id or snippet):
        return None
    ref = {"source": "granola", "source_id": source_id or None, "snippet": snippet or None}
    if raw.get('source_timestamp'):
        ref['timestamp'] = raw['source_timestamp']
    return ref


def _attach_source(it: dict, ref: dict | None) -> None:
    if not ref:
        return
    refs = [r for r in source_refs(it) if isinstance(r, dict)]
    matched = ref in refs
    if not matched and ref.get('source_id'):
        for previous in refs:
            if cr.ref_fields(previous) != cr.ref_fields(ref):
                continue
            if previous.get('timestamp') and ref.get('timestamp') and previous['timestamp'] != ref['timestamp']:
                continue  # genuinely conflicting source timestamps remain independent evidence
            if ref.get('timestamp') and not previous.get('timestamp'):
                previous['timestamp'] = ref['timestamp']
            matched = True
            break
    if not matched:
        refs.append(ref)
    it["source_refs"] = refs[-20:]


def apply(payload, user_email: str = "", now: datetime | None = None, user_name: str = "",
          source_meetings: list | None = None) -> dict:
    # User-zone "now" (same helper continuity_resolve uses): server datetime.now() is UTC on
    # Railway, so the 17:30 followup for a US-west user would stamp TOMORROW's created_at,
    # skewing age_days/expiry against every other writer's user-local dates.
    now = now or cr._now_local(cr.configured_tz() or "+00:00")
    today = now.strftime("%Y-%m-%d")
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")
    user_name = user_name or os.environ.get("SOTTO_USER_NAME", "")
    grounding = None
    if source_meetings is not None:
        payload, grounding = ground_commitments(payload, source_meetings, user_email, user_name)
    meetings = {_s(m.get('meeting_id') or m.get('id')): m for m in source_meetings or []
                if isinstance(m, dict)}
    commitments = payload.get("commitments", []) if isinstance(payload, dict) else (payload or [])
    if not commitments:   # nothing to write — don't even load the ledger
        result = {"written": 0, "deduped": 0, "skipped_terminal": 0, "anchor_keys": [],
                  "recorded_anchor_keys": [], "closed_anchor_keys": []}
        if grounding is not None:
            result["grounding"] = grounding
        return result

    written, deduped, skipped_terminal, anchors = 0, 0, 0, []
    recorded_anchors, closed_anchors = [], []
    with cr._ledger_lock():
        items = cr._load_items()  # reload under the same lock every other mutation path uses
        for c in commitments:
            if not isinstance(c, dict):
                continue
            meeting = meetings.get(_s(c.get('meeting_id'))) or {}
            observed = cr._parse_dt(meeting.get('end') or meeting.get('start') or meeting.get('date'))
            captured = created_at
            if observed is not None and cr._to_user_zone(observed) <= cr._to_user_zone(now):
                # Date-only meeting records retain a conservative end-of-day cutoff.
                stamp = _s(meeting.get('end') or meeting.get('start') or meeting.get('date'))
                captured = (cr._inbound_cutoff(stamp) if len(stamp) <= 10
                            else cr._to_user_zone(observed).isoformat())
            verified_end = bool(len(_s(meeting.get("end"))) > 10 and observed and
                                cr._to_user_zone(observed) <= cr._to_user_zone(now))
            raw = _action_for(c, user_email, captured, user_name, grounded=verified_end)
            if raw is None:
                continue
            # Preserve the source's own precision separately from the conservative resolution
            # cutoff above. A date-only meeting is not a promise made at 23:59:59, and capture
            # time cannot fill an unknown source date. This is a meeting boundary, not an
            # exact utterance timestamp. Never take this field from model output.
            if observed is not None and cr._to_user_zone(observed) <= cr._to_user_zone(now):
                raw['source_timestamp'] = stamp
                raw['source_timestamp_kind'] = ('meeting_date' if len(stamp) <= 10 else
                    'meeting_end' if meeting.get('end') else 'meeting_start' if meeting.get('start') else 'meeting_date')
            a = cr._normalize_action(raw)
            occurrence_key = cr.compute_anchor_key(a)
            suggested = _s(raw.get("existing_anchor_key")).strip()
            candidate = items.get(suggested) if suggested else None
            same_direction = (candidate is not None and
                              cr.is_waiting_on(candidate.get("action_type")) ==
                              cr.is_waiting_on(a.get("action_type")))
            same_counterpart = (candidate is not None
                                and _s(candidate.get('contact_identifier')).strip().lower()
                                == _s(a.get('contact_identifier')).strip().lower())
            same_deliverable = (candidate is not None and _s(candidate.get('ask')).strip()
                                and _deliverable_key(candidate.get('ask'))
                                == _deliverable_key(raw.get('ask')))
            ak = suggested if (same_direction and same_counterpart and same_deliverable
                               and _s(candidate.get("status", "open")) not in cr.TERMINAL) \
                else occurrence_key
            anchors.append(ak)
            existing = items.get(ak)
            if existing is not None:
                if _s(existing.get("status", "open")) in cr.TERMINAL:
                    skipped_terminal += 1    # the user already closed this — never resurrect it
                    closed_anchors.append(ak)
                    continue
                _attach_source(existing, _source_ref(raw))
                if not _s(existing.get('ask')).strip():
                    existing['ask'] = raw.get('ask')
                # Explicit is a user/manual lock and remains authoritative. A normal loop gains the
                # source-grounded close policy without weakening a pre-existing lock.
                if existing.get("resolution_mode") != "explicit":
                    existing["resolution_mode"] = "source_grounded"
                cr._persist(existing)
                deduped += 1
                recorded_anchors.append(ak)
                continue
            it = {
                "anchor_key": ak, "action_type": a.get("action_type"), "channel": a.get("channel"),
                "contact_name": a.get("contact_name"), "contact_identifier": a.get("contact_identifier"),
                "canonical_id": a.get("canonical_id"), "status": "open",
                "created_at": a.get("created_at") or today,
                "summary": a.get("summary", ""), "ask": a.get("ask"),
                "meeting_time": a.get("meeting_time"), "deadline": a.get("deadline"),
                "source_thread_id": a.get("source_thread_id"),
                "source": "followup_commitment",
                "resolution_mode": "source_grounded",
                **capture_shape.stored(a),
                **({key: raw[key] for key in ('source_timestamp', 'source_timestamp_kind')}
                   if raw.get('source_timestamp') else {}),
            }
            _attach_source(it, _source_ref(raw))
            items[ak] = it
            cr._persist(it)
            written += 1
            recorded_anchors.append(ak)

    result = {"written": written, "deduped": deduped,
              "skipped_terminal": skipped_terminal, "anchor_keys": anchors,
              "recorded_anchor_keys": recorded_anchors, "closed_anchor_keys": closed_anchors}
    if grounding is not None:
        result["grounding"] = grounding
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("payload", nargs="?", help="compose_followup output JSON (default: stdin)")
    ap.add_argument("--user-email", dest="user_email", default="")
    a = ap.parse_args()
    try:
        raw = open(a.payload, encoding="utf-8").read() if a.payload else sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    print(json.dumps(apply(payload, a.user_email)))


if __name__ == "__main__":
    main()
