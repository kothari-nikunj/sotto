#!/usr/bin/env python3
"""
knowledge_query.py — pack person/company knowledge for the LLM, or answer "what do I know about X".

PORT SOURCE: knowledge_files.rs::get_person_knowledge_for_llm / pack_person_compact (line 900-1075)
Used by morning-brief (load prior knowledge) and the ask/people skills.

Usage:
    knowledge_query.py --person "Sarah Chen"        # one person (name, email, or phone), expanded
    knowledge_query.py --person "Sarah Chen" --topic "clinic procurement"  # topical facts first
    knowledge_query.py --person "Sarah Chen" --editable-person  # active ids for a correction
    knowledge_query.py --calendar /tmp/sotto_cal.json --gmail /tmp/sotto_gmail.json   # today's cast
    knowledge_query.py --relevant-days 7            # fallback: everyone updated in last N days
--person prints JSON { "<canonical_id>": "<packed string>" }.
The default mode prints the NAMED form compose_brief._normalize_local consumes directly:
    { "person_knowledge":  { "<canonical_id>": "<packed>" },
      "company_knowledge": { "<company_slug>": "<packed>" },     # omitted when nothing is on file
      "contact_index":     [ {canonical_id, display_name, identifiers, confidence} ] }
contact_index covers EVERY person file (not just recently-updated ones) — it is the identity map
that lets the brief resolve a phone and an email to the SAME person (the phone↔email bridge).

WHO PACKS: current Gmail, Calendar and consented Bridge participants (--local), plus canonical
active-loop participants, select the bounded cohort. Durable graph facts travel with that
cohort; a display name or nearby timestamp cannot join two conversations. The --relevant-days
file-mtime window is the fallback only when no participant identifiers can be obtained. mtime
says when a file was last REWRITTEN, which is not the same question as "does this person matter
today" (persist_prep.py documents the same lesson) — so the person who emailed you this morning
used to pack nothing and the model re-derived what the graph already knew.

company_knowledge answers the other half — the company files the Learn step has been writing since
the Rust port and nothing ever read back. Companies come from today's attendees' email domains and
from the packed people's `company` field, resolved through knowledge_update's one company reader.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # knowledge.py, its sibling
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import knowledge as kg  # noqa: E402
from personal_context import (  # noqa: E402
    PARTICIPANT_LIMIT, TOPIC_CHARS, participant_identifiers, participant_topics, topic_for_identifiers,
)

MAX_PEOPLE_PACKED = PARTICIPANT_LIMIT
MAX_COMPACT_CHARS = 3200
MAX_EXPANDED_CHARS = 8000
MAX_RELATED_PEOPLE = 2
MAX_RELATED_FACTS = 2
OMITTED_CONTEXT = 'Additional stored context omitted; narrow the topic for details.'


LOW_CONFIDENCE = 0.6   # below this a fact must be visibly labeled in the packed context

# Company context is a garnish on the brief, not a second brief: a handful of the companies in
# today's cast, each a couple of sentences. Same spirit as the per-person compact caps above.
MAX_COMPANIES_PACKED = 5
MAX_COMPANY_ENTRY_CHARS = 600
MAX_COMPANY_NEWS_LINES = 3   # the file stores news newest-first (knowledge_update inserts at 0)

EMAIL_RE = re.compile(r"[\w.+\-']+@[\w\-]+\.[\w.\-]+")
TOPIC_WORD_RE = re.compile(r"[a-z0-9]{3,}")
TOPIC_STOP = frozenset({
    'about', 'after', 'before', 'from', 'have', 'meeting', 'sync', 'that', 'their', 'them',
    'this', 'with', 'your', 'you', 'our', 'can', 'please', 'thanks', 'thank', 'tell',
    'know', 'what', 'who', 'how', 'when', 'where', 'there',
}) | kg.STOP_WORDS


def _fact_text(f: kg.FactMeta, now: datetime) -> str:
    """Research facts (prewarm_graph / persist_prep, conf 0.55) bake a "Per web search:" prefix into
    the text, so they arrive pre-labeled. Any OTHER fact that is low-confidence — an LLM extraction
    scored 0.5-0.69, or one whose confidence decayed — would otherwise pack as a bare assertion;
    label it so the brief never presents an unverified fact as ground truth."""
    if kg.effective_confidence(f, now) < LOW_CONFIDENCE and not f.text.startswith("Per web search"):
        return f.text + " (unverified)"
    return f.text


def _topic_words(value: str) -> set[str]:
    return {word for word in TOPIC_WORD_RE.findall((value or '').casefold())
            if word not in TOPIC_STOP}


def _rank_facts(facts: list, topic: str) -> list:
    """Keep authority first, then prefer facts sharing concrete words with the current work."""
    words = _topic_words(topic)
    indexed = list(enumerate(facts))
    indexed.sort(key=lambda item: (
        item[1][1].source != 'user_edit',
        -len(words & _topic_words(item[1][1].text)),
        item[0],
    ))
    return [fact for _, fact in indexed]


def _conflicts(p, state):
    """Only live fact IDs participate; an edit/archive immediately removes stale conflict prose."""
    people = state.get('people')
    record = people.get(p.canonical_id) if isinstance(people, dict) else None
    pairs = record.get('pairs') if isinstance(record, dict) else None
    if not isinstance(pairs, list):
        return []
    return [pair for pair in pairs[:3] if isinstance(pair, list) and len(pair) == 2
            and all(isinstance(fid, str) and fid in p.facts and p.facts[fid].status == 'active' for fid in pair)]


def _related_facts(p, topic, now, state):
    """One exact graph edge, never a name match, recursion, or a second person's full profile."""
    words = _topic_words(topic)
    if not words:
        return []
    found, seen = [], {p.canonical_id}
    for relation in p.relations[:kg.MAX_RELATIONS_FOR_LLM]:
        cid = relation.slug
        if relation.type not in kg.RELATION_INVERSE or cid in seen or not kg.valid_canonical_id(cid):
            continue
        seen.add(cid)
        try:
            other = _load(kg.safe_path(kg.people_dir(), cid))
        except Exception:  # noqa: BLE001 - optional related context cannot break the primary read
            continue
        if other.canonical_id != cid:
            continue
        # An indirect excerpt must not silently choose one side of a known disagreement.
        disputed = {fid for pair in _conflicts(other, state) for fid in pair}
        facts = [(fid, f) for fid, f in _rank_facts(kg.sorted_active_facts(other.facts, now), topic)
                 if fid not in disputed and words & _topic_words(f.text)][:MAX_RELATED_FACTS]
        if facts:
            found.append((other, relation, facts))
    # Choose by relevance, not edge insertion order; bounded inspection is the displayed edges.
    found.sort(key=lambda row: -max(len(words & _topic_words(f.text)) for _, f in row[2]))
    return found[:MAX_RELATED_PEOPLE]


def pack_person(p: kg.PersonFile, expanded: bool, now: datetime, topic: str = '') -> str:
    topic = topic[:TOPIC_CHARS]
    cap = MAX_EXPANDED_CHARS if expanded else MAX_COMPACT_CHARS
    limit = kg.MAX_FACTS_FOR_LLM if expanded else kg.MAX_FACTS_COMPACT
    identity = f"{p.name} ({p.canonical_id})"
    if p.title and p.company:
        identity += f" | {p.title} @ {p.company}"
    elif p.title:
        identity += f" | {p.title}"
    elif p.company:
        identity += f" | @ {p.company}"
    email = next((i for i in p.identifiers if "@" in i), None)
    if email:
        identity += f" | {email}"
    if p.x_handles:
        handle = kg.normalize_x_handle(p.x_handles[-1].get("handle")) \
            if isinstance(p.x_handles[-1], dict) else ""
        if handle:
            identity += f" | X @{handle}"
    # Profile strings are not facts. Bound malformed/legacy headers without cutting an assertion.
    lines = [identity if len(identity) <= 600 else f"{p.name[:200]} ({p.canonical_id[:200]})"]
    remaining = cap - len(lines[0]) - len(OMITTED_CONTEXT) - 2
    # Two kinds of leaving out. `omitted`: the fact allowance or a legacy field's limit cut
    # something — expected in a compact read (it is a summary by design, and the brief's model
    # cannot "narrow the topic"), worth saying in an expanded one. `cut`: the character ceiling
    # refused a whole assertion — always said, in both reads.
    omitted = cut = False

    def append(text):
        nonlocal remaining, cut
        if len(text) + 1 > remaining:
            cut = True
            return False
        lines.append(text)
        remaining -= len(text) + 1
        return True

    try:
        import jsonstore
        state = jsonstore.read(os.path.join(kg.data_root(), 'knowledge/conflicts.json'), default={})
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError, TypeError):
        state = {}
    sentences = [s for s in (r.sentence() for r in p.relations) if s][:kg.MAX_RELATIONS_FOR_LLM]
    if sentences:
        append("& " + "; ".join(sentences))

    # Summary refs influence selection but never emit another copy. Recent observations compete
    # inside the same cap. Owner corrections outrank every automatic selection, even without a topic.
    active = _rank_facts(kg.sorted_active_facts(p.facts, now), topic)
    pairs = _conflicts(p, state)
    disputed = {fid for pair in pairs for fid in pair}
    candidates = [(p, [(fid, f)], '') for fid, f in active if fid not in disputed]
    if pairs:
        candidates.append((p, [(fid, p.facts[fid]) for fid in dict.fromkeys(pairs[0])], 'conflict'))
        omitted |= len(pairs) > 1
    for other, relation, facts in _related_facts(p, topic, now, state):
        candidates.extend((other, [(fid, f)], relation.type) for fid, f in facts)
    words = _topic_words(topic)
    candidates.sort(key=lambda row: (
        not any(f.source == 'user_edit' for _, f in row[1]),
        -max(len(words & _topic_words(f.text)) for _, f in row[1]),
        row[0].canonical_id != p.canonical_id,
        not any(fid in p.summary_refs for fid, _ in row[1]) if row[0] is p else True,
    ))
    included, count = set(), 0
    for owner, facts, relation in candidates:
        keys = {(owner.canonical_id, ' '.join(f.text.split()).casefold()) for _, f in facts}
        if keys <= included:
            continue
        if count + len(keys) > limit:
            omitted = True
            continue
        if relation == 'conflict':
            text = 'Unresolved memory conflict (do not choose silently): ' + ' / '.join(
                _fact_text(f, now) for _, f in facts)
        elif relation:
            text = f'Related context about {owner.name} ({owner.canonical_id}), {relation}: ' + _fact_text(facts[0][1], now)
        else:
            text = '= ' + _fact_text(facts[0][1], now)
        if append(text):
            included.update(keys)
            count += len(keys)
    # Read compatibility for old files only. New writers do not persist situational advice.
    for prefix, values, maximum in (('> ', p.talking_points, kg.MAX_TALKING_POINTS_FOR_LLM),
                                    ('~ ', p.recent_activity, kg.MAX_RECENT_ACTIVITY_FOR_LLM)):
        values = [v for v in values if (p.canonical_id, ' '.join(v.split()).casefold()) not in included]
        for value in values[:maximum]:
            append(prefix + value)
        omitted |= len(values) > maximum
    if expanded and p.notes:
        excerpt = _notes_excerpt(p.notes)
        if excerpt:
            append('# ' + excerpt)
        omitted |= excerpt != p.notes
    if cut or (expanded and omitted):
        lines.append(OMITTED_CONTEXT)
    return "\n".join(lines)


def _notes_excerpt(notes: str) -> str:
    """The note whole when it fits, else its leading COMPLETE sentences inside NOTES_EXCERPT_CHARS
    with an ellipsis. A sentence is the unit that carries its own qualifier, so a note made of
    several keeps what fits; one sentence too long to fit is left out whole, never cut before
    its "only if"."""
    if len(notes) <= kg.NOTES_EXCERPT_CHARS:
        return notes
    suffix = ' …'
    head = notes[:kg.NOTES_EXCERPT_CHARS]
    # A wrapped line can continue an assertion's qualifier. Only punctuation ends the
    # excerpt, and the omission marker shares the cap rather than overflowing it.
    boundary = max((match.end() for match in re.finditer(r'[.!?](?=\s)', head)
                    if match.end() <= kg.NOTES_EXCERPT_CHARS - len(suffix)), default=0)
    return head[:boundary] + suffix if boundary else ''


def _load(path):
    with open(path, encoding="utf-8") as f:
        return kg.parse_person_file(f.read())


def _calendar_attendee_emails(path: str | None) -> set:
    """Lowercased attendee emails from a gathered calendar file (gather_google.py's
    /tmp/sotto_cal.json — a list of events, or {events:[…]}; attendees are dicts or bare strings).
    Missing/unreadable file → empty set (the exemption just doesn't apply)."""
    if not path or not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return set()
    events = data.get("events") if isinstance(data, dict) else data
    emails = set()
    for e in events if isinstance(events, list) else []:
        if not isinstance(e, dict):
            continue
        for a in e.get("attendees") or []:
            em = a.get("email") if isinstance(a, dict) else a
            if isinstance(em, str) and "@" in em:
                emails.add(em.strip().lower())
    return emails


def editable_person(p: kg.PersonFile, now: datetime, topic: str = '') -> dict:
    """Bounded active fact inventory used to bind an owner correction to one existing fact."""
    facts = _rank_facts(kg.sorted_active_facts(p.facts, now), topic)[:kg.MAX_FACTS_FOR_LLM]
    return {'canonical_id': p.canonical_id, 'name': p.name,
            'facts': [{'id': fid, 'text': fact.text} for fid, fact in facts]}


def _gmail_addresses(path: str | None) -> set:
    """Lowercased From/To addresses from a gathered gmail file (gather_google.py's
    /tmp/sotto_gmail.json — a list of emails, or {emails:[…]}). The fields are display strings
    ("Sarah Chen <sarah@acme.com>", or a comma-joined To), so the addresses are pulled out with a
    regex rather than parsed. Missing/unreadable file → empty set (the gate falls back to mtime)."""
    if not path or not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return set()
    rows = data.get("emails") if isinstance(data, dict) else data
    out = set()
    for e in rows if isinstance(rows, list) else []:
        if not isinstance(e, dict):
            continue
        for field in ("from", "to", "cc"):
            v = e.get(field)
            if isinstance(v, str):
                out.update(m.group(0).lower() for m in EMAIL_RE.finditer(v))
    return out


def _input(path, key):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value.get(key, value) if isinstance(value, dict) else value
    except (OSError, ValueError, TypeError):
        return {} if key == 'local' else []


def active_loop_participants():
    """Use the ledger's read contract; retrieving a commitment never creates or changes it."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
    try:
        import ledger_io
        return ledger_io.load_active()
    except (OSError, ValueError, TypeError, ImportError):
        return []


def _company_entry(known: dict) -> str:
    """One company file → one packed block: what it is, then its newest news. Capped."""
    lines = []
    name = (known.get("name") or known.get("slug") or "").strip()
    about = " ".join((known.get("about") or "").split())
    lines.append(f"{name} — {about}" if about else name)
    for n in (known.get("news") or [])[:MAX_COMPANY_NEWS_LINES]:
        text = " ".join(str(n).split())
        if text:
            lines.append("- " + text)
    return "\n".join(lines)[:MAX_COMPANY_ENTRY_CHARS]


def pack_companies(domains, companies) -> dict:
    """{slug: packed block} for today's companies — the read side of the company files the Learn
    step writes. Sources, in order: today's attendees' email domains, then the packed people's
    `company` field. Resolution and dedup are knowledge_update's (`company_knowledge` runs the same
    slug → alias → domain ladder the writer does), so a domain and a name that mean one company
    pack once. Best-effort: no companies dir, or an unreadable one, packs nothing."""
    try:
        import knowledge_update as ku  # noqa: PLC0415 — lazy: reading companies must never break a query
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for name in list(domains) + list(companies):
        if len(out) >= MAX_COMPANIES_PACKED:
            break
        if not str(name).strip():
            continue
        try:
            known = ku.company_knowledge(company_name=str(name))
        except Exception:  # noqa: BLE001
            continue
        slug = known.get("slug") if known else ""
        if not slug or slug in out:
            continue
        entry = _company_entry(known)
        if entry.strip():
            out[slug] = entry
    return out


def person_lookup_coverage(found: bool, person: str) -> dict:
    """Read-only guidance for an Ask Sotto person lookup, never proof of complete history.

    Source health describes recent reads, not the reach of a historical search. In particular,
    an empty recent snapshot can still have older conversations to inspect.
    """
    from source_context import source_health, allowed

    exact_identifier = bool(EMAIL_RE.fullmatch(person.strip()) or
                            re.fullmatch(r'\+?[0-9][0-9 ()-]{5,}', person.strip()))
    coverage = {'graph': 'found' if found else 'no_match', 'historical_sources': []}
    if found:
        return coverage
    health = source_health()
    local = {row['source']: row['status'] for row in health.get('sources', [])
             if isinstance(row, dict) and row.get('source') in ('imessage', 'whatsapp', 'contacts')}
    contacts_status = local.get('contacts', 'unknown')
    coverage['identity_resolution'] = {
        'status': 'exact_identifier' if exact_identifier else 'name_only',
        'contacts_status': contacts_status,
        'next_action': ('use_identifier' if exact_identifier else
                        'get_contacts' if contacts_status in ('ok', 'empty', 'stale', 'partial', 'degraded')
                        else 'check_connection' if contacts_status == 'unverified'
                        else 'ask_owner_for_identifier'),
    }
    for source in ('imessage', 'whatsapp'):
        status = local.get(source, 'unknown')
        action = ('check_history' if status in ('ok', 'empty', 'stale', 'partial', 'degraded')
                  else 'check_connection' if status == 'unverified' else 'explain_limit')
        if action == 'check_history' and not exact_identifier:
            action = 'resolve_identifier'
        coverage['historical_sources'].append({'source': source, 'status': status,
            'next_action': action, 'reader': 'read_history',
            'scope': 'Use an explicit bounded since/until window, limit <= 100, and at most 3 '
                     'pages; paginate with next_cursor as before, preserving the window. '
                     'Disclose unsearched pages.'})
    # Self-host Google/Granola grants are verified by their own connected tools. The generic
    # source permission defaults to true there, so it cannot certify a connection.
    for source in ('gmail', 'granola'):
        if os.environ.get('SOTTO_DEPLOYMENT_MODE') == 'managed':
            permitted = allowed(source)
            status, action = ('connected', 'check_history') if permitted else ('disabled', 'explain_limit')
        else:
            status, action = 'unknown', 'check_connection'
        coverage['historical_sources'].append({'source': source, 'status': status,
            'next_action': action, 'reader': 'gmail_search' if source == 'gmail' else 'gather_granola',
            'scope': ('Search a bounded date range; disclose the range and incomplete results.'
                      if source == 'gmail' else
                      'Default gathering covers 14 days of notes and 36 hours of transcripts; '
                      'request a bounded wider window if needed and disclose the window.')})
    return coverage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--person")
    ap.add_argument("--person-coverage", action="store_true",
                    help="with --person, wrap the result with consent-aware historical source guidance")
    ap.add_argument("--editable-person", action="store_true",
                    help="with --person, return active fact ids/text for an explicit correction")
    ap.add_argument("--topic", default="", help="prefer facts relevant to this subject")
    ap.add_argument("--relevant-days", type=int, default=7)
    ap.add_argument("--calendar", help="gathered calendar JSON; today's attendees pack, and their "
                                       "email domains pull in company context")
    ap.add_argument("--gmail", help="gathered gmail JSON; everyone in today's From/To packs. "
                                    "Supplying it REPLACES the --relevant-days mtime window")
    ap.add_argument("--local", help="consented Bridge local JSON; include current phone/chat participants")
    ap.add_argument("--loops", help="active commitment JSON; defaults to the canonical active ledger")
    args = ap.parse_args()
    now = datetime.now()

    try:  # legacy name-slug files → canonical_id keying (idempotent; reads work pre-first-update)
        # Under THE graph lock: this is a read path, but migrate re-keys and merges FILES — the one
        # writer that used to run outside the lock, able to race a locked writer or overwrite its
        # in-flight journal. graph_lock also replays an interrupted batch before we read anything.
        import knowledge_update as ku  # noqa: PLC0415 — lazy: a query must never break on import
        with ku.graph_lock():
            kg.migrate_people_dir(now)
    except Exception:  # noqa: BLE001
        pass

    if args.person:
        # Accept a name, an email, or a phone — resolved through the same identity map as writes.
        path = kg.find_person_file(name=args.person, identifier=args.person)
        out = {}
        if path and os.path.exists(path):
            p = _load(path)
            key = p.canonical_id or kg.slugify(p.name)
            out[key] = editable_person(p, now, args.topic) if args.editable_person else pack_person(
                p, True, now, args.topic)
        print(json.dumps({'person_knowledge': out, 'coverage': person_lookup_coverage(bool(out), args.person)}
                         if args.person_coverage else out))
        return

    cutoff = now - timedelta(days=args.relevant_days)
    from source_context import allowed
    cal_emails = _calendar_attendee_emails(args.calendar) if allowed('calendar') else set()
    local = _input(args.local, 'local') if args.local else {}
    loops = _input(args.loops, 'items') if args.loops else active_loop_participants()
    inputs = dict(local=local if isinstance(local, dict) else {},
        gmail=_input(args.gmail, 'emails') if args.gmail else [],
        calendar=_input(args.calendar, 'events') if args.calendar else [], loops=loops)
    participants = participant_identifiers(**inputs)
    topics = participant_topics(cohort=participants, **inputs)
    # THE GATE, in one sentence: a person packs when they appear in today's inputs or on today's
    # calendar; the mtime window is the fallback cohort, used only when no inputs were supplied.
    # mtime only says when a file was last REWRITTEN, so under it someone who emailed you this
    # morning packs zero context and the model re-derives what the graph already knows. An empty or
    # unreadable --gmail is "no inputs", not "nobody" — a broken gather degrades to the old cohort.
    inputs_gate = bool(participants)
    person_knowledge, contact_index, companies = {}, [], []
    for path in sorted(glob.glob(os.path.join(kg.people_dir(), "*.md"))):
        try:
            p = _load(path)
        except Exception:  # noqa: BLE001 — one unreadable file must not kill the brief's knowledge
            continue
        identifiers = [str(i) for i in p.identifiers if str(i).strip()]
        if p.canonical_id and identifiers:
            # confidence "medium": graph identifiers unify identity (canonical_id attach, known-person
            # rescue) but never override a name Apple Contacts resolved (those seed as "high").
            entry = {"canonical_id": p.canonical_id, "display_name": p.name,
                     "identifiers": identifiers, "confidence": "medium"}
            if p.x_user_id:
                entry["x_user_id"] = p.x_user_id
            handles = [kg.normalize_x_handle(h.get("handle")) for h in p.x_handles
                       if isinstance(h, dict) and kg.normalize_x_handle(h.get("handle"))]
            if handles:
                entry["x_handles"] = handles
            contact_index.append(entry)
        if not ({kg.normalize_identifier(i) for i in identifiers} | {p.canonical_id}).intersection(participants):
            if inputs_gate:
                continue
            try:
                if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                    continue
            except OSError:
                continue
        if len(person_knowledge) >= MAX_PEOPLE_PACKED:
            continue
        topic = topic_for_identifiers(topics, [*identifiers, p.canonical_id])
        person_knowledge[p.canonical_id or kg.slugify(p.name)] = pack_person(p, False, now, topic)
        if p.company:
            companies.append(p.company)

    expanded = set(participants)
    for person in contact_index:
        ids = {kg.normalize_identifier(i) for i in person['identifiers']} | {person['canonical_id']}
        if ids.intersection(participants):
            expanded.update(ids)
    out = {"person_knowledge": person_knowledge, "contact_index": contact_index,
           "memory_participants": sorted(expanded)}
    domains = sorted({e.split("@", 1)[1] for e in cal_emails if "@" in e})
    company_knowledge = pack_companies(domains, companies)
    if company_knowledge:   # absent, not empty, when nothing is on file — the brief renders neither
        out["company_knowledge"] = company_knowledge
    print(json.dumps(out))


if __name__ == "__main__":
    main()
