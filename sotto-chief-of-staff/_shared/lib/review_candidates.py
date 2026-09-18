"""Conservative Friday review candidates backed by accepted-delivery receipts.

Selection is read-only and offers at most one explicit command. It never changes a preference;
chat or the dashboard still performs the user's confirmed command through the existing writers.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for directory in (HERE, HERE.parent / "scripts", HERE.parent / "knowledge"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import jsonstore  # noqa: E402
import knowledge as kg  # noqa: E402
import master_file  # noqa: E402
import preferences  # noqa: E402
from relationship_importance import attention_tiebreak, classify  # noqa: E402

COOLDOWN_DAYS = 30
EFFECT_KIND = "review_candidate_offer"
STATE_REL = ("knowledge", "relationship_state.json")
OFFER_KEY = "review_candidate_offers"


def _root():
    return os.environ.get("SOTTO_DATA", "/data")


def _state_path():
    return os.path.join(_root(), *STATE_REL)


def _instant(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _prefs(value=None):
    value = preferences.load_explicit() if value is None else value
    if isinstance(value, dict) and isinstance(value.get("explicit"), dict):
        value = value["explicit"]
    return value if isinstance(value, dict) else {}


def _muted(record, person, prefs):
    names = {str(record.get("name") or "").strip().casefold()}
    names.update(str(value).strip().casefold() for value in record.get("aliases", []) or [])
    identifiers = {str(record.get("canonical_id") or "").strip().casefold()}
    if person:
        names.add(str(person.name or "").strip().casefold())
        identifiers.update(str(value).strip() for value in person.identifiers or [])
        identifiers.update("@" + str(value.get("handle") or "").strip().lstrip("@")
                           for value in person.x_handles or [] if isinstance(value, dict))
        identifiers.add(str(person.canonical_id or "").strip())
    muted_people = {str(item).strip().casefold() for item in prefs.get("mute_people", [])}
    muted_senders = prefs.get("mute_senders", []) or []
    muted_sender_exact = {str(item).strip().casefold() for item in muted_senders}
    return bool(({value for value in names if value} & muted_people)
                or ({value.casefold() for value in identifiers if value} & muted_sender_exact)
                or any(preferences.sender_is_muted(value, muted_senders)
                       for value in identifiers if value))


def _identity_values(record, person):
    names = {str(record.get("name") or "").strip().casefold()}
    names.update(str(value).strip().casefold() for value in record.get("aliases", []) or [])
    identifiers = {str(record.get("canonical_id") or "").strip().casefold()}
    if person:
        names.add(str(person.name or "").strip().casefold())
        identifiers.add(str(person.canonical_id or "").strip().casefold())
        identifiers.update(str(value).strip().casefold() for value in person.identifiers or [])
        identifiers.update("@" + str(value.get("handle") or "").strip().casefold().lstrip("@")
                           for value in person.x_handles or [] if isinstance(value, dict))
    return {value for value in names if value}, {value for value in identifiers if value}


def _explicit_vip(record, person, prefs):
    names, identifiers = _identity_values(record, person)
    stated = {str(item).strip().casefold() for item in prefs.get("vip_people", [])}
    return bool((names | identifiers) & stated)


def _confirmed_user_family(person):
    """True only for the user's confirmed edge to this person, never a relation between others."""
    if not person or not kg.valid_canonical_id(str(person.canonical_id or "")):
        return False
    return any(relation.type == "family_of" and relation.source == "user_edit"
               and (relation.slug == "c_user"
                    or relation.name.strip().casefold() in {"you", "me", "the user"})
               for relation in person.relations)


def _people(history):
    """Resolve every history person from one graph index, parsing each file at most once."""
    try:
        index = kg.build_people_index()
    except Exception:  # noqa: BLE001 — graph context is optional candidate evidence
        return {}
    result = {}
    for canonical_id in history:
        path = index.get("by_cid", {}).get(str(canonical_id))
        if not path:
            continue
        try:
            result[canonical_id] = kg.parse_person_file(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
    return result


def _payload_hash(candidate_type, canonical_id, command):
    raw = json.dumps({"candidate_type": candidate_type, "canonical_id": canonical_id,
                      "command": command}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _candidate(candidate_type, canonical_id, name, command, explanation, score):
    digest = _payload_hash(candidate_type, canonical_id, command)
    return {"candidate_type": candidate_type, "canonical_id": canonical_id, "name": name,
            "command": command, "explanation": explanation,
            "effect": {"kind": EFFECT_KIND, "canonical_id": canonical_id,
                       "candidate_type": candidate_type, "payload_hash": digest},
            "_score": score}


def _recent_offers(state, now):
    cutoff = now - timedelta(days=COOLDOWN_DAYS)
    return [row for row in state.get(OFFER_KEY, []) if isinstance(row, dict)
            and (_instant(row.get("accepted_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]


def _eligible(now, prefs, state):
    history = state.get("history", {}) if isinstance(state, dict) else {}
    history = history if isinstance(history, dict) else {}
    recent = _recent_offers(state, now)
    if recent:  # global maximum: one accepted offer in any rolling 30-day window
        return []
    priority_state = master_file.priorities()["priorities"]
    priority_slots = master_file.PRIORITY_MAX - len(priority_state)
    people = _people(history)
    rows = []
    for key, record in history.items():
        if not isinstance(record, dict) or not kg.valid_canonical_id(str(key)):
            continue
        name = str(record.get("name") or "").strip()
        person = people.get(key)
        identity = {**record, "canonical_id": key}
        identity_names, _identifiers = _identity_values(identity, person)
        if not name or _muted(identity, person, prefs):
            continue
        evidence = record.get("importance_evidence") or {}
        if priority_slots > 0:
            engagement = record.get("engagement") or {}
            score = attention_tiebreak(engagement, now)
            already_named = any(alias in str(item.get("text") or "").casefold()
                                for item in priority_state for alias in identity_names)
            # Your reply speed alone is a tie-break, never a proposal: a vendor you answered twice
            # within the hour is not someone to "stay in touch with". The offer needs the
            # relationship to have gone both ways on distinct days as well.
            if score > 0 and _reciprocal(evidence) and not already_named:
                command = f"prioritize staying in touch with {name}"
                rows.append(_candidate(
                    "priority", key, name, command,
                    "This adds a stated priority used only to rank work inside the same real urgency band.",
                    (1, score, name.casefold())))
        confirmed_family = _confirmed_user_family(person)
        inferred_tier = classify(evidence, now).get("tier")
        sustained_relationship = inferred_tier in {"vip", "vvip"}
        if not _explicit_vip(identity, person, prefs) and (sustained_relationship or confirmed_family):
            command = f"make {name} VIP"
            # Sustained reciprocity first, then a relation the user confirmed, then a priority
            # from attention: the user's own words about a person outrank an inference.
            rows.append(_candidate(
                "vip", key, name, command,
                "This lets this person's missed calls clear the quiet-hours bar; it does not send for you.",
                ((3 if sustained_relationship else 2),
                 attention_tiebreak(record.get("engagement") or {}, now), name.casefold())))
    return rows


def _reciprocal(evidence):
    """Contact on at least two distinct days in EACH direction — the same evidence bar the pulse
    uses for "meaningful"; one-way outreach or a single chat burst is not a relationship."""
    evidence = evidence if isinstance(evidence, dict) else {}
    return (len({str(day)[:10] for day in evidence.get("sent_days") or []}) >= 2
            and len({str(day)[:10] for day in evidence.get("received_days") or []}) >= 2)


def choose(now, prefs):
    """Return one candidate or None: sustained reciprocity, then confirmed family, then an
    attention priority — the user's own words about a person outrank an inference."""
    now = _instant(now) or datetime.now(timezone.utc)
    state = jsonstore.read(_state_path(), default={})
    candidates = _eligible(now, _prefs(prefs), state if isinstance(state, dict) else {})
    if not candidates:
        return None
    selected = max(candidates, key=lambda row: row["_score"])
    return {key: value for key, value in selected.items() if key != "_score"}


def candidate_valid(effect, now=None):
    """Re-check current mutes, explicit choices, capacity, evidence and cooldown before delivery."""
    if not isinstance(effect, dict) or effect.get("kind") != EFFECT_KIND:
        return False
    now = _instant(now) or datetime.now(timezone.utc)
    state = jsonstore.read(_state_path(), default={})
    descriptor = {key: value for key, value in effect.items() if key != "delivery_id"}
    return any(row["effect"] == descriptor for row in _eligible(
        now, _prefs(), state if isinstance(state, dict) else {}))


def mark_offered(effect, receipt):
    """Stamp an accepted offer once; never applies the proposed preference."""
    if not isinstance(receipt, dict):
        return False
    accepted = _instant(receipt.get("accepted_at"))
    if accepted is None or not isinstance(effect, dict) or effect.get("kind") != EFFECT_KIND:
        return False
    delivery_id = str(effect.get("delivery_id") or "").strip()
    message_id = str(receipt.get("message_id") or "").strip()
    if not delivery_id and not message_id:
        return False
    if (effect.get("candidate_type") not in {"priority", "vip"}
            or not kg.valid_canonical_id(str(effect.get("canonical_id") or ""))
            or len(str(effect.get("payload_hash") or "")) != 64):
        return False
    path = _state_path()
    with jsonstore.transaction(path, default={}) as state:
        offers = state.get(OFFER_KEY, [])
        offers = offers if isinstance(offers, list) else []
        if any(isinstance(row, dict)
               and ((delivery_id and row.get("delivery_id") == delivery_id)
                    or (message_id and row.get("message_id") == message_id)) for row in offers):
            return True
        # This records an accepted question, not current eligibility or authority. Later
        # preference or cooldown changes cannot erase delivery or strand an outbox retry.
        offers.append({**effect, "accepted_at": accepted.astimezone(timezone.utc).isoformat(),
                       **({"delivery_id": delivery_id} if delivery_id else {}),
                       **({"message_id": message_id} if message_id else {})})
        state[OFFER_KEY] = offers[-64:]
    return True
