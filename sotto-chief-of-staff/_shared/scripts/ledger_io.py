#!/usr/bin/env python3
"""
ledger_io.py — shared READ helpers for the continuity ledger ($SOTTO_DATA/knowledge/continuity/*.md).

One place for the load/parse/age logic that used to be copy-pasted across retune_scan.py,
loops_query.py, and continuity_resolve.py (drift between the copies = loops silently disagreeing
about what's open). Read-only: writing/resolution stays in continuity_resolve.py (`_persist`).

Exports:
  ACTIVE / TERMINAL         — the status sets (continuity.rs:227/230)
  normalize_action_type(t)  — the ONE spelling normalizer (variants → canonical types)
  WAITING_ON_TYPES          — the ONE direction predicate: what the user is OWED
  MEETING_TYPES / SCHEDULING_TYPES — the rest of the CLOSED family vocabulary (continuity_resolve's
                              `action_family` maps everything else onto the owed-by-you family)
  CHASE_MAX                 — how many chases a waiting_on gets before it hands off to sotto-loops
  ledger_dir()              — the ledger directory under $SOTTO_DATA
  parse_frontmatter(content)— YAML frontmatter → dict; None when there is no frontmatter block OR
                              the block is malformed (load_entries tells the two apart and flags
                              malformed entries so writers never persist over them)
  load_entries(...)         — every ledger file's frontmatter, sorted by path (deterministic)
  load_active()             — the entries the read views surface: ACTIVE status, not snoozed,
                              excluding proven unsourced brief birthday courtesies
                              (a PARKED row is neither: hidden until something touches it)
  last_touch_day(fm) / days_untouched(fm, today) — the park rule's one clock
  CHASE_STATE_FIELDS        — everything the chase lane has spent on a loop, cleared as one
  age_days(created_at, today) — whole days old vs an aware "today" (naive created_at → UTC)
"""
from __future__ import annotations

import glob
import os
import re
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from textutil import _s  # noqa: E402
from timeutil import _calendar_day, _now_local, _parse_ts, configured_tz  # noqa: E402  (ages match the brief)
import funnel_policy as _funnel_policy  # noqa: E402
from ledger_provenance import classify_ledger_sources, UnknownProvenanceError  # noqa: E402

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

ACTIVE = {"open", "waiting", "failed", "blocked"}      # continuity.rs:227
TERMINAL = {"resolved", "dismissed", "expired"}        # continuity.rs:230
# ── Parked: kept, hidden, revived by a touch ─────────────────────────────────────────────────────
# One sentence: something you owe that nothing has touched for PARK_AFTER_DAYS — no new message,
# no brief re-capture, no `keep` — parks after an accepted warning: its history stays on disk, out of the brief
# and the open list, and comes back the moment anything touches it again. Neither ACTIVE (the read
# views skip it) nor TERMINAL (the resolver never prunes it and a re-capture reopens it without new
# request evidence). What you're OWED never parks: it is chased, then handed back to you by name.
PARKED = {"parked"}
PARK_AFTER_DAYS = 14


def task_label(row: dict) -> str:
    return (_s(row.get("ask")).strip() or _s(row.get("summary")).strip()
            or _s(row.get("action_type")).replace("_", " ").strip())


def contact_label(row: dict) -> str:
    name = _s(row.get("contact_name"))
    if name:
        return name
    try:
        kind, _sources = classify_ledger_sources(row)
    except UnknownProvenanceError:
        return "(unknown)"
    return "Your task" if kind == "owner" else "(unknown)"


def completion_basis(status: str, resolution: str, *, evidence_visible: bool = False) -> str:
    """Describe recorded fulfillment, never infer real-world non-completion from active state.

    Callers must apply source consent before indicating that supporting evidence is visible.
    """
    if status in ACTIVE:
        return 'not_recorded'
    if status != 'resolved':
        return 'ledger_state'
    if resolution == 'user_resolved':
        return 'user_confirmed'
    return 'source_evidence' if evidence_visible else 'ledger_state'


# ── Direction, defined ONCE ──────────────────────────────────────────────────────────────────────
# One sentence: `waiting_on` is the only type where the OTHER side owes the user, so it is the only
# one in waiting_on; everything else (reply / follow_up / follow_up_stale / call_back / …) is a
# debt the USER owes. `follow_up_stale` means "you owe them a nudge", so it lives in you_owe.
# Age and silence do not resolve ordinary obligations; source-backed completion is validated
# by the canonical resolver, not inferred by this direction map.
WAITING_ON_TYPES = {"waiting_on"}
CHASE_MAX = _funnel_policy.CHASE_MAX

# ── The CLOSED family vocabulary (the anchor's middle component) ─────────────────────────────────
# One sentence: a debt belongs to one of four families — owed-to-you, a calendar shadow, a
# scheduling ask, or owed-by-you — and anything the extractor invents falls into owed-by-you rather
# than minting a family of its own.
#
# The brief's `type` is a free string in the responseSchema, and a real volume showed ~19 distinct
# values (`action`, `task`, `review`, `read`, `info`, `reminder`, `document_mention`,
# `action_required`, `email_follow`, …). `action_family` used to return an unrecognized type
# VERBATIM, so every new word the model reached for forked a fresh anchor and the same debt opened
# a second row. These two sets plus WAITING_ON_TYPES are the whole vocabulary; the default closes it.
MEETING_TYPES = {"meeting_prep", "meeting_info", "meeting", "calendar"}
SCHEDULING_TYPES = {"schedule", "reschedule", "propose_times", "rsvp"}

# Everything the chase lane has SPENT on one loop — the two nudges, the clock between them, the
# undelivered proposal, and the hand-off question that ends the lane. Named once because they are
# cleared together or not at all: a re-opened anchor and the user's "keep waiting" both mean "start
# this loop's chase story over", and a list that is clipped in one caller and not the other leaves a
# loop that can never be chased again or one that is never asked about twice.
CHASE_STATE_FIELDS = ("chased_count", "chase_after", "last_chased_at", "chase_pending",
                      "handoff_asked_at", "chase_stalls", "last_heard_at")

# The OTHER row-level predicate lives one directory over, in `_shared/lib/brief_validate.is_urgent`
# (overdue / due within 24h / already chased) — it belongs to the BRIEF's contract about which loops
# earn a line, it is pure and stdlib-only there, and both the validator and the composer already
# import that module. Named here so nobody writes a second copy looking for it in the ledger's own
# helpers. One concept, one implementation.


def normalize_action_type(t) -> str:
    """Collapse model-emitted VARIANTS of the FLEX action-type vocabulary onto the canonical types.
    The brief's responseSchema leaves `type` a free string, so Gemini emits channel-suffixed reply
    forms (reply_message, reply_email — seen in real runs) alongside the documented "reply", plus
    the occasional hyphen/space spelling of follow_up / call_back. Anchor-keying, cross-channel
    resolution AND every read view key off this: a variant normalized in the writer but not in a
    reader is an item chased by one lane and filed under the wrong direction by the other."""
    t = (t if isinstance(t, str) else ("" if t is None else str(t)))
    t = t.lower().strip().replace("-", "_").replace(" ", "_")
    if t.startswith("reply"):
        return "reply"              # reply_message / reply_email / reply_imessage → reply
    if t in ("followup", "followup_stale"):
        return t.replace("followup", "follow_up")
    if t in ("callback", "call"):
        # A bare "call" is the same debt as "call_back" — and naming it so is what routes it
        # through the call-history resolver instead of leaving it to age out unanswered.
        return "call_back"
    if t in ("follow_up_stalled", "followup_stalled"):
        return "follow_up_stale"
    if t == "propose_time":
        return "propose_times"
    return t


def is_waiting_on(t) -> bool:
    """True when the other side owes the user; this classifies direction, not completion."""
    return normalize_action_type(t) in WAITING_ON_TYPES


def is_routine_birthday_greeting(row: dict) -> bool:
    """A suggested birthday wish, distinct from planning or a requested birthday task."""
    text = f"{_s(row.get('summary'))} {_s(row.get('ask'))}".casefold()
    if re.search(r'\b(?:plan\w*|party|dinner|lunch|gift|present|reserv\w*|book\w*|venue|invoice|pay\w*|organi[sz]\w*|'
                 r'celebrat\w*|cake|invite\w*)\b', text):
        return False                      # planning a birthday is a real task, not a courtesy wish
    return bool(re.search(
        r'\bhappy\s+b(?:irth)?day\b|\bb(?:irth)?day\s+(?:wish(?:es)?|greet(?:ing)?s?)\b|'
        r'\bb(?:irth)?day\b.*\bbelated\b|\bbelated\b.*\bb(?:irth)?day\b', text))


def deadline_context(row: dict, today: str) -> dict:
    """Calendar deadline facts, distinct from the request's recording date and age."""
    try:
        deadline = date.fromisoformat(_s(row.get('deadline'))[:10])
        current = date.fromisoformat(today[:10])
    except (ValueError, TypeError):
        return {'deadline': None, 'days_overdue': None}
    return {'deadline': deadline.isoformat(), 'days_overdue': max(0, (current - deadline).days)}


def dated_request_text(text: str, entries: list[dict], now=None) -> str:
    """Quote relative request wording under its recording date, never infer its event date."""
    if not re.search(r'\b(?:today|yesterday|tomorrow|tonight)\b', text, re.I):
        return text
    dates = {_s(e.get('created_at'))[:10] for e in entries}
    try:
        recorded = datetime.strptime(next(iter(dates)), '%Y-%m-%d').date() if len(dates) == 1 else None
    except ValueError:
        recorded = None
    current = (now or datetime.now(timezone.utc)).date()
    if recorded and recorded <= current:
        label = f"{recorded.strftime('%b')} {recorded.day}"
        if recorded.year != current.year:
            label += f", {recorded.year}"
        prefix = f'From {label}: '
        if text.startswith(prefix):
            text = text[len(prefix):]
            if text.startswith('“') and text.endswith('”'):
                return prefix + text
        return f'From {label}: “{text}”'
    return f'Earlier request (date unknown): “{text}”'


def birthday_courtesy_identity():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'morning-brief', 'scripts'))
    from continuity_resolve import person_identity
    return person_identity()


def birthday_courtesy_importance(row: dict, now, identity=None):
    """Require a proven person and current permitted reciprocal evidence, or an explicit VIP."""
    import jsonstore
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'knowledge'))
    import knowledge as kg
    from relationship_importance import for_contact
    from source_context import allowed, _read_source_state, _source_rows, _state_path
    if row.get('group_id') or not allowed('contacts'):
        return None
    if identity is None:
        # Reuse continuity's collision-aware exact identifier index, never a display-name match.
        identity = birthday_courtesy_identity()
    identifier = kg.normalize_identifier(_s(row.get('contact_identifier')))
    cid = (identity.get('unique_identifiers') or {}).get(identifier)
    if not cid or (row.get('canonical_id') and row['canonical_id'] != cid):
        return None
    try:
        people = list((identity.get('people_by_cid') or {}).values())
        person = next(p for p in people if p.canonical_id == cid)
        name = person.name.strip().casefold()
        unique_name = sum(p.name.strip().casefold() == name for p in people) == 1
        root = os.environ.get('SOTTO_DATA', '/data')
        state = jsonstore.read(os.path.join(root, 'knowledge/relationship_state.json'), default={}, strict=True)
        record = (state.get('history') or {}).get(cid, {})
        channels = set(record.get('channels') or {})
        sources = {('gmail' if ch == 'email' else ch) for ch in channels}
        # Retained activity from before a regrant cannot re-establish importance. Dated evidence
        # uses the same conservative common cutoff as its union of source dependencies.
        source_rows = _source_rows(_read_source_state(_state_path()))
        regranted = [source_rows.get(source, {}).get('consent_regrant_epoch', 0) for source in sources]
        cutoff = max((value for value in regranted if type(value) in (int, float)), default=0)
        if cutoff:
            since = datetime.fromtimestamp(cutoff, timezone.utc).date().isoformat()
            record = {**record, 'importance_evidence': {
                key: [day for day in days if isinstance(day, str) and day > since]
                for key, days in (record.get('importance_evidence') or {}).items()}}
        history = {cid: record} if sources and all(allowed(source) for source in sources) else {}
        prefs = jsonstore.read(os.path.join(root, 'preferences.json'), default={}, strict=True)
        vips = (prefs.get('explicit') or {}).get('vip_people', []) if unique_name else []
        importance = for_contact({'name': person.name, 'canonical_id': cid}, history, vips, now)
        return {**importance, 'canonical_id': cid,
                'sources': sorted(sources) if importance['reason'] != 'you marked this person VIP' else []}
    except (OSError, ValueError, TypeError, AttributeError, StopIteration, OverflowError, jsonstore.Unreadable):
        return None


def birthday_courtesy_context(row: dict, local: dict, now, identity: dict):
    """Bind an optional recent birthday greeting to a current Contacts card, never to silence."""
    import knowledge as kg
    from relationship_importance import WINDOW_DAYS
    importance = birthday_courtesy_importance(row, now, identity)
    if not importance or importance['tier'] not in {'vip', 'vvip'}:
        return None
    cid = importance['canonical_id']
    dates = set()
    for card in local.get('contacts') or []:
        ids = card.get('phones', []) + card.get('emails', [])
        normalized = {kg.normalize_identifier(_s(value)) for value in ids}
        owners = {(identity.get('unique_identifiers') or {}).get(value) for value in normalized}
        if (kg.normalize_identifier(_s(row.get('contact_identifier'))) not in normalized
                or owners != {cid}):
            continue
        try:
            month, day = map(int, _s(card.get('birthday'))[-5:].split('-'))
            occurred = date(now.year, month, day)
            if occurred > now.date():
                occurred = date(now.year - 1, month, day)
            if 0 < (now.date() - occurred).days <= WINDOW_DAYS:
                dates.add(occurred.isoformat())
        except (TypeError, ValueError):
            continue
    if len(dates) != 1:
        return None
    occurred = dates.pop()
    return {'source': 'contacts', 'source_refs': [
        {'sourceType': 'contacts', 'sourceId': f'{cid}:birthday:{occurred}'}],
        'birthday_importance_sources': importance['sources'], 'new_obligation': False,
        'summary': f'Optional birthday greeting (birthday {occurred})', 'ask': '',
        'created_at': occurred + ' 00:00:00', 'deadline': None, 'resolution_mode': 'birthday_greeting'}


def is_optional_birthday_courtesy(row: dict) -> bool:
    """THE optional-greeting classifier, shared by the brief, `sotto-loops` and the pulse.

    Only a captured Contacts-backed courtesy (`resolution_mode: birthday_greeting`) is optional.
    A row without that mode — a legacy source-backed request, an owner reminder, or any row whose
    provenance cannot be classified — stays an ordinary task, visible wherever tasks are.
    """
    if _s(row.get('resolution_mode')).strip() != 'birthday_greeting':
        return False
    # The same authority split as loops_query.permitted_rows: versioned content provenance
    # classifies owner rows; a provenance-free row must prove it is a source loop.
    from ledger_provenance import classify_ledger_sources, source_loop_sources, UnknownProvenanceError
    try:
        if row.get('content_provenance') is not None:
            return classify_ledger_sources(row)[0] != 'owner'
        return bool(source_loop_sources(row))
    except (UnknownProvenanceError, TypeError, ValueError, KeyError):
        return False


def birthday_greeting_retired(row: dict, today: str) -> bool:
    """An optional greeting outlives its offer once the birthday is beyond the capture window.

    Capture admits a greeting only within relationship_importance.WINDOW_DAYS of the actual
    Contacts birthday; past that same window the courtesy expires through the resolver's ordinary
    expiry. Owner rows and every non-greeting row are never retired by this rule.
    """
    if not is_optional_birthday_courtesy(row):
        return False
    from relationship_importance import WINDOW_DAYS
    dates = {_s(ref.get('sourceId'))[-10:] for ref in row.get('source_refs') or []
             if isinstance(ref, dict) and ref.get('sourceType') == 'contacts'}
    if len(dates) != 1:
        dates = {_s(row.get('created_at'))[:10]}
    try:
        birthday = date.fromisoformat(dates.pop())
        return (date.fromisoformat(_s(today)[:10]) - birthday).days > WINDOW_DAYS
    except ValueError:
        return False


def is_unsourced_brief_birthday_courtesy(row: dict) -> bool:
    """Legacy model-created courtesy: retain the record, omit it from active obligation views.

    A positive brief origin and the absence of owner/source evidence are both required. An old
    row with uncertain provenance remains visible; this policy never guesses that a real request
    or an owner-added birthday reminder was a courtesy.
    """
    if not _s(row.get('source_brief_at')).strip() or not is_routine_birthday_greeting(row):
        return False
    provenance = row.get('content_provenance')
    if isinstance(provenance, dict) and (
            provenance.get('kind') == 'owner' or provenance.get('sources')):
        return False
    return not any(row.get(key) for key in (
        'source', 'source_refs', 'source_thread_id', 'source_message_id', 'origin_key'))


def ledger_dir() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "knowledge", "continuity")


def parse_frontmatter(content: str):
    """The YAML frontmatter of one ledger file. Returns a dict only when the frontmatter parses to
    a mapping (a valid empty mapping `{}` included). Returns None both for a bare file (no
    frontmatter block at all) and for a MALFORMED one (unclosed fence, YAML error, non-mapping):
    malformed metadata must never masquerade as an empty-but-valid entry — continuity_resolve used
    to treat it as a status-less open item and rewrite the file as '---\\n{}\\n---', destroying the
    user's content. load_entries distinguishes the two shapes via the opening fence."""
    if not isinstance(content, str) or not content.startswith("---"):
        return None
    end = content.find("\n---", 3)
    if end == -1:
        return None        # opening fence but no closing one — malformed, not a bare file
    try:
        fm = yaml.safe_load(content[4:end])
    except Exception:
        return None
    return fm if isinstance(fm, dict) else None


def load_entries(with_path: bool = False, include_bare: bool = False) -> list:
    """Frontmatter dicts for every *.md in the ledger, sorted by path. Unreadable files are
    skipped. Files with no valid frontmatter are skipped unless include_bare=True
    (continuity_resolve historically keys bare files by filename; the read views ignore them).
    MALFORMED files (a fence that doesn't parse to a mapping) are likewise only surfaced with
    include_bare=True, and carry {"_malformed": True} so writers (continuity_resolve) can skip
    them instead of persisting over the broken file. with_path=True adds the source path under
    "_path" (continuity_resolve persists back to it)."""
    if yaml is None:  # pragma: no cover — the read views degrade to empty without PyYAML
        return []
    out = []
    for path in sorted(glob.glob(os.path.join(ledger_dir(), "*.md"))):
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        fm = parse_frontmatter(content)
        if fm is None:
            if not include_bare:
                continue
            # A fence that didn't parse to a mapping = malformed; no fence at all = bare.
            fm = {"_malformed": True} if content.startswith("---") else {}
        if with_path:
            fm["_path"] = path
        out.append(fm)
    return out


def load_active(now=None) -> list:
    """The entries the read views surface: ACTIVE status, not snoozed, and real obligations.
    Missing status
    counts as open (matching continuity_resolve's default). A parked row (PARKED) is not ACTIVE, so
    every view that reads through here — the brief's ledger, the count line, `sotto-loops`, the
    cleanup scan, the chase clock — skips it until the resolver revives it.

    The SNOOZE lives here, not in each caller. The brief, `sotto-loops` and the cleanup scan are
    three views of one ledger, and the brief's used to be the one that ignored `snoozed_until` — so
    a loop the user had explicitly parked was named as urgent in the brief and counted in the line
    that says how many are open, while the dashboard the line points at showed neither. Legacy
    model-only birthday courtesies are kept on disk but hidden from these obligation views; a
    sourced request or owner-created task still appears. One concept, one implementation: hidden
    is hidden in every view."""
    instant = now or _now_local(configured_tz() or "+00:00")
    return active_entries(load_entries(), instant)


def active_entries(rows: list[dict], now) -> list[dict]:
    """Apply the canonical active/snooze/visibility predicate to already loaded rows.

    Only an optional birthday greeting reads importance state; every other row needs no I/O."""
    today = now.strftime("%Y-%m-%d")
    cache: dict = {}
    return [fm for fm in rows
            if fm.get("status", "open") in ACTIVE
            and not (_s(fm.get("snoozed_until"))[:10] > today)
            and not hidden_from_obligation_views(fm, now, cache)]


def hidden_from_obligation_views(row: dict, now=None, cache: dict | None = None) -> bool:
    """THE birthday visibility rule for the brief, `sotto-loops` and the pulse — one predicate.

    Hidden: a provably unsourced brief-created legacy courtesy, or a captured optional greeting
    (`resolution_mode: birthday_greeting`) whose person no longer proves VIP/VVIP importance.
    Every other row — legacy source-backed requests, owner reminders, unclassifiable provenance —
    stays visible.
    """
    if is_unsourced_brief_birthday_courtesy(row):
        return True
    if not is_optional_birthday_courtesy(row):
        return False
    cache = {} if cache is None else cache
    instant = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if 'identity' not in cache:
        cache['identity'] = birthday_courtesy_identity()
    importance = birthday_courtesy_importance(row, instant, cache['identity'])
    return not importance or importance['tier'] not in {'vip', 'vvip'}


def last_touch_day(fm: dict) -> str:
    """The day something last touched this row, as YYYY-MM-DD: it was created, re-captured by a
    brief (`source_brief_at`), or came back to life (`reopened_at` — a terminal or parked row
    re-captured, or a `keep`). A snooze restarts this clock on its scheduled return day, so a long
    snooze cannot immediately park when it ends. These touches never rewrite the original request
    date, which remains the completion-evidence cutoff."""
    return max(_s(fm.get(k))[:10] for k in
               ("created_at", "source_brief_at", "reopened_at", "snoozed_until"))


def days_untouched(fm: dict, today: str):
    """Whole days between the last touch and `today` (YYYY-MM-DD). None when no touch parses."""
    touch = last_touch_day(fm)
    if len(touch) < 10 or len(_s(today)) < 10:
        return None
    try:
        return (date.fromisoformat(_s(today)[:10]) - date.fromisoformat(touch)).days
    except ValueError:
        return None


def park_candidate(fm: dict, today: str) -> bool:
    """One eligibility rule for both the resolver and the preceding evening's warning."""
    return (fm.get("status", "open") in ACTIVE
            and not is_waiting_on(fm.get("action_type"))
            and normalize_action_type(fm.get("action_type")) not in MEETING_TYPES
            and _s(fm.get("resolution_mode")).strip() not in {"explicit", "birthday_greeting"}
            and not (_s(fm.get("snoozed_until"))[:10] > today)
            and not ((_calendar_day(fm.get("deadline")) or "") >= today)
            and (days_untouched(fm, today) or 0) >= PARK_AFTER_DAYS)


def parking_notice_current(fm: dict) -> bool:
    """An accepted notice belongs to this unchanged obligation and its current parking clock."""
    from delivery_effects import loop_version
    try:
        date.fromisoformat(_s(fm.get("parking_notice_at")))
    except ValueError:
        return False
    return (fm.get("parking_notice_touch") == last_touch_day(fm)
            and fm.get("parking_notice_version") == loop_version(fm))


def age_days(created_at, today):
    """Whole days between created_at and `today` (an AWARE datetime, i.e. _now_local(...)).
    A naive created_at is treated as UTC. None when created_at doesn't parse."""
    d = _parse_ts(_s(created_at))
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return max(0, (today - d).days)
