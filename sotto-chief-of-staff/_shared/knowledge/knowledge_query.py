#!/usr/bin/env python3
"""
knowledge_query.py — pack person/company knowledge for the LLM, or answer "what do I know about X".

PORT SOURCE: knowledge_files.rs::get_person_knowledge_for_llm / pack_person_compact (line 900-1075)
Used by morning-brief (load prior knowledge) and the ask/people skills.

Usage:
    knowledge_query.py --person "Sarah Chen"        # one person (name, email, or phone), expanded
    knowledge_query.py --person "Sarah Chen" --topic "clinic procurement"  # topical facts first
    knowledge_query.py --topic "clinic procurement"  # bounded recorded obligation candidates
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
import hashlib
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta

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
INTERACTIVE_BYTES = 48 * 1024
NARROWER_QUERY = 'Ask about one person or a more specific topic to retrieve omitted context.'
FACT_EVIDENCE_BYTES = 12 * 1024
FACT_EVIDENCE_REFS = 8
FACT_EVIDENCE_SCOPE = ('Selected active graph facts only; observation dates are not original source '
                       'timestamps. References may be unavailable or withheld by current source grants. '
                       'First and latest retained references are shown when capped. Source archives '
                       'were not searched; this is not a complete fact change history.')


def _fits_interactive(value: dict) -> bool:
    # Check UTF-8 transport as well as the receiver's default ASCII JSON reserialization.
    return (len(json.dumps(value, ensure_ascii=False).encode('utf-8')) + 1 <= INTERACTIVE_BYTES
            and len(json.dumps(value).encode('utf-8')) + 1 <= INTERACTIVE_BYTES)


def _observation_date(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return None


def attach_fact_evidence(result: dict, records: list, now: datetime, *, interactive: bool,
                         retained_units: dict | None = None) -> dict:
    """Explain exactly the selected graph assertions, using existing refs and captured grants.

    Observation dates are graph evidence clocks, never reconstructed message timestamps.
    Unknown or revoked origins cannot release opaque references or their counts.
    """
    from ledger_provenance import ALIASES
    from render_local import message_version_reference
    permitted = _history_sources(interactive=interactive)
    evidence = {'items': [], 'omitted': len(records), 'coverage': FACT_EVIDENCE_SCOPE}
    result['fact_evidence'] = evidence
    result['suggestion'] = NARROWER_QUERY
    if interactive and not _fits_interactive(result):
        # Normal callers reserve this envelope before packing whole assertion groups. Keep
        # this helper safe for an already projected result too, shedding whole people only.
        retained_units = {}
        result = interactive_projection(result, retained_units=retained_units)
        evidence = result['fact_evidence']
    for record in records:
        cid, fid, fact = record[:3]
        unit = record[3] if len(record) > 3 else fact.text
        container = record[4] if len(record) > 4 else cid
        packed = result.get('person_knowledge', {}).get(container, '')
        # Interactive transport may have shed a complete assertion after pack_person selected it.
        if (interactive and (len(record) < 6 or record[5] not in (retained_units or {}).get(container, set()))
                or not isinstance(packed, str) or '\n' + unit + '\n' not in '\n' + packed + '\n'):
            continue
        raw_refs = ([fact.source_ref] if fact.source_ref not in (None, "") else [])
        valid_container = isinstance(fact.evidence_refs, list)
        if valid_container:
            raw_refs += fact.evidence_refs
        # Validate the whole provenance set before projection: dropping a malformed or
        # oversized reference must not make a mixed-source assertion appear authorized.
        refs = (list(dict.fromkeys(raw_refs)) if valid_container and all(
            isinstance(ref, str) and 0 < len(ref) <= 500 for ref in raw_refs) else [])
        owner = fact.source == 'user_edit'
        public = fact.source == 'web_research'
        origin = ALIASES.get('contacts' if fact.source == 'apple_contacts' else fact.source)
        def accessible(ref):
            # observed_message binds each reference independently; a label or saved health
            # cannot supply authority for an untyped or mixed-source opaque identifier.
            typed = ALIASES.get(ref.partition(':')[0]) if ':' in ref else None
            if ref.startswith('msg:'):
                reference, _, version = ref.rpartition(':v:')
                typed = next((channel for channel in ('imessage', 'whatsapp')
                              if message_version_reference(
                                  {'source_ref': reference, 'content_version': version}, channel) == ref), None)
                if typed is None:
                    return False
            source = typed or origin
            return (permitted is not None and source in permitted if source else
                    public and ref.startswith(('https://', 'http://')))
        visible_refs = refs if (refs and (origin is None or permitted is not None and origin in permitted)
                                and all(accessible(ref) for ref in refs)) else []
        known = owner or bool(visible_refs)
        item = {'canonical_id': cid, 'fact_id': fid, 'text': fact.text,
                'status': fact.status, 'provenance_state': kg.provenance_state(fact) if known else 'reference_unavailable',
                'source_timestamp': None,
                'uncertainty': 'Source timestamp unavailable; graph observation dates do not establish when it was said.'}
        if known:
            item.update(source=fact.source,
                        first_observed_date=_observation_date(fact.first),
                        last_observed_date=_observation_date(fact.last),
                        effective_confidence=kg.effective_confidence(fact, now))
            if not math.isfinite(item['effective_confidence']):
                item['effective_confidence'] = None
            if visible_refs:
                item['source_ref'] = fact.source_ref if fact.source_ref in visible_refs else ''
                item['evidence_refs'] = visible_refs if len(visible_refs) <= FACT_EVIDENCE_REFS else [
                    visible_refs[0], *visible_refs[-(FACT_EVIDENCE_REFS - 1):]]
                item['references_omitted'] = max(0, len(visible_refs) - FACT_EVIDENCE_REFS)
        evidence['items'].append(item)
        evidence['omitted'] -= 1
        if (len(json.dumps(evidence).encode('utf-8')) > FACT_EVIDENCE_BYTES
                or (interactive and not _fits_interactive(result))):
            evidence['items'].pop()
            evidence['omitted'] += 1
    history = result.get('obligation_history', {})
    if (not evidence['omitted'] and not any(item.get('references_omitted') for item in evidence['items'])
            and not any(result.get('omitted', {}).get(key) for key in ('people', 'assertions', 'contacts'))
            and not history.get('omitted')
            and not any(row.get('truncated_fields') for row in history.get('items', []))):
        result.pop('suggestion', None)
    return result


def interactive_projection(value: dict, *, selected_index: list | None = None,
                           packed_groups: dict | None = None,
                           packed_weights: dict | None = None,
                           packed_omitted: dict | None = None,
                           retained_units: dict | None = None,
                           unselected_people: int = 0) -> dict:
    """Fit complete packed lines and index entries in one aggregate transport budget."""
    if 'company_knowledge' in value and 'person_knowledge' not in value:
        if _fits_interactive(value):
            return value
        # Keep the closed company result contract if malformed legacy data defeats its small cap.
        return {'status': 'unavailable', 'company_knowledge': {}, 'truncated': False}

    def weight(key, index):
        weights = (packed_weights or {}).get(key, [])
        return weights[index] if index < len(weights) else int(index > 0)

    if 'person_knowledge' not in value and 'company_knowledge' not in value:
        # Exact person and editable reads retain their established top-level keyed shape.
        result = {}
        retained_weights = {}
        omitted = {'people': 0, 'assertions': 0, 'contacts': 0, 'reason': None}
        for key, packed in value.items():
            if isinstance(packed, dict) and isinstance(packed.get('facts'), list):
                item = {**packed, 'facts': []}
                result[key] = item
                if not _fits_interactive({**result, 'omitted': omitted, 'suggestion': NARROWER_QUERY}):
                    item = {'canonical_id': str(packed.get('canonical_id', ''))[:200], 'facts': []}
                    result[key] = item
                retained_weights[key] = 0
                for fact in packed['facts']:
                    item['facts'].append(fact)
                    if not _fits_interactive({**result, 'omitted': omitted, 'suggestion': NARROWER_QUERY}):
                        item['facts'].pop()
                        omitted['assertions'] += 1
                    else:
                        retained_weights[key] += 1
                omitted['assertions'] += (packed_omitted or {}).get(key, 0)
            elif isinstance(packed, str):
                groups = (packed_groups or {}).get(key, [packed])
                result[key] = groups[0] if groups else ''
                retained_weights[key] = weight(key, 0)
                for index, group in enumerate(groups[1:], 1):
                    previous = result[key]
                    result[key] = previous + '\n' + group
                    if not _fits_interactive({**result, 'omitted': omitted, 'suggestion': NARROWER_QUERY}):
                        result[key] = previous
                        omitted['assertions'] += weight(key, index)
                    else:
                        retained_weights[key] += weight(key, index)
                omitted['assertions'] += (packed_omitted or {}).get(key, 0)
            else:
                result[key] = packed
        if any(omitted[k] for k in ('people', 'assertions', 'contacts')):
            omitted['reason'] = 'interactive_budget'
            result.update(omitted=omitted, suggestion=NARROWER_QUERY)
        while not _fits_interactive(result):
            keys = [key for key in result if key not in ('omitted', 'suggestion')]
            if not keys:
                break
            result.pop(keys[-1])
            omitted['people'] += 1
            omitted['assertions'] += retained_weights.pop(keys[-1], 0)
            result.update(omitted=omitted, suggestion=NARROWER_QUERY)
            omitted['reason'] = 'interactive_budget'
        return result

    omitted = {'people': unselected_people, 'assertions': 0, 'contacts': 0, 'reason': None}
    result = {'person_knowledge': {}, 'contact_index': [], 'memory_participants': [],
              'omitted': omitted, 'suggestion': NARROWER_QUERY}
    if 'coverage' in value:
        result['coverage'] = value['coverage']
    if 'obligation_history' in value:
        result['obligation_history'] = value['obligation_history']
    if 'fact_evidence' in value:
        result['fact_evidence'] = value['fact_evidence']
    source_index = selected_index if selected_index is not None else value.get('contact_index', [])
    selected = value.get('person_knowledge', {})
    retained_weights = {}
    omitted['contacts'] = max(0, len(value.get('contact_index', [])) - len(source_index))
    for key, packed in selected.items():
        if isinstance(packed, dict) and isinstance(packed.get('facts'), list):
            item = {**packed, 'facts': []}
            result['person_knowledge'][key] = item
            if not _fits_interactive(result):
                result['person_knowledge'][key] = {
                    'canonical_id': str(packed.get('canonical_id', ''))[:200], 'facts': []}
            if not _fits_interactive(result):
                result['person_knowledge'].pop(key)
                omitted['people'] += 1
                omitted['assertions'] += len(packed['facts'])
                omitted['assertions'] += (packed_omitted or {}).get(key, 0)
                continue
            item = result['person_knowledge'][key]
            retained_weights[key] = 0
            for fact in packed['facts']:
                item['facts'].append(fact)
                if not _fits_interactive(result):
                    item['facts'].pop()
                    omitted['assertions'] += 1
                else:
                    retained_weights[key] += 1
            omitted['assertions'] += (packed_omitted or {}).get(key, 0)
            continue
        groups = (packed_groups or {}).get(key, [packed])
        result['person_knowledge'][key] = groups[0] if groups else ''
        if not _fits_interactive(result):
            result['person_knowledge'].pop(key)
            omitted['people'] += 1
            omitted['assertions'] += sum(weight(key, index) for index in range(len(groups)))
            omitted['assertions'] += (packed_omitted or {}).get(key, 0)
            continue
        retained_weights[key] = weight(key, 0)
        if retained_units is not None:
            retained_units[key] = {0}
        for index, line in enumerate(groups[1:], 1):
            previous = result['person_knowledge'][key]
            result['person_knowledge'][key] = previous + '\n' + line
            if not _fits_interactive(result):
                result['person_knowledge'][key] = previous
                omitted['assertions'] += weight(key, index)
            else:
                retained_weights[key] += weight(key, index)
                if retained_units is not None:
                    retained_units[key].add(index)
        omitted['assertions'] += (packed_omitted or {}).get(key, 0)
    retained = set(result['person_knowledge'])
    for entry in source_index:
        if entry.get('canonical_id') not in retained:
            omitted['contacts'] += 1
            continue
        result['contact_index'].append(entry)
        if not _fits_interactive(result):
            result['contact_index'].pop()
            omitted['contacts'] += 1
    for identifier in value.get('memory_participants', []):
        # The selected index supplies exact aliases without returning the entire graph's index.
        if not any(identifier == row['canonical_id'] or identifier in row['identifiers']
                   for row in result['contact_index']):
            continue
        result['memory_participants'].append(identifier)
        if not _fits_interactive(result):
            result['memory_participants'].pop()
    for key, packed in value.get('company_knowledge', {}).items():
        result.setdefault('company_knowledge', {})[key] = packed
        if not _fits_interactive(result):
            result['company_knowledge'].pop(key)
            omitted['assertions'] += 1
    if any(omitted[k] for k in ('people', 'assertions', 'contacts')):
        omitted['reason'] = 'interactive_budget_or_selector_cap'
    # Final metadata digits can grow at the boundary; shed whole fields until both encodings fit.
    while not _fits_interactive(result):
        if result['memory_participants']:
            result['memory_participants'].pop()
        elif result['contact_index']:
            result['contact_index'].pop()
            omitted['contacts'] += 1
        elif result.get('company_knowledge'):
            result['company_knowledge'].popitem()
            omitted['assertions'] += 1
        elif result['person_knowledge']:
            key, _ = result['person_knowledge'].popitem()
            if retained_units is not None:
                retained_units.pop(key, None)
            omitted['people'] += 1
            omitted['assertions'] += retained_weights.pop(key, 0)
        elif 'coverage' in result and result['coverage'] != {'graph': value.get('coverage', {}).get('graph', 'unknown')}:
            result['coverage'] = {'graph': value.get('coverage', {}).get('graph', 'unknown')}
            omitted['assertions'] += 1
        else:
            break
        omitted['reason'] = 'interactive_budget_or_selector_cap'
    history = result.get('obligation_history', {})
    if (not any(omitted[k] for k in ('people', 'assertions', 'contacts'))
            and not history.get('omitted')
            and not result.get('fact_evidence', {}).get('omitted')
            and not any(row.get('truncated_fields') for row in history.get('items', []))):
        # A bounded read is not a complete archive, but do not invent omitted result content.
        result.pop('suggestion', None)
    return result


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
    text = f.text
    if kg.effective_confidence(f, now) < LOW_CONFIDENCE and not f.text.startswith("Per web search"):
        text += " (unverified)"
    if kg.provenance_state(f) == "reference_unavailable":
        text += " (source reference unavailable)"
    return text


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
    return kg.live_conflict_pairs(p, pairs[:kg.CONFLICT_PAIRS])


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


def pack_person(p: kg.PersonFile, expanded: bool, now: datetime, topic: str = '',
                units: list[str] | None = None, stats: dict | None = None,
                selected_records: list | None = None,
                unit_weights: list[int] | None = None,
                selected_evidence: list | None = None) -> str:
    p = kg.projected_person(p)
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
    if units is not None:
        units.append(lines[0])
    if unit_weights is not None:
        unit_weights.append(0)
    remaining = cap - len(lines[0]) - len(OMITTED_CONTEXT) - 2
    # Two kinds of leaving out. `omitted`: the fact allowance or a legacy field's limit cut
    # something — expected in a compact read (it is a summary by design, and the brief's model
    # cannot "narrow the topic"), worth saying in an expanded one. `cut`: the character ceiling
    # refused a whole assertion — always said, in both reads.
    omitted = cut = False
    if stats is not None:
        stats['assertions'] = 0

    def append(text, assertion_count=0):
        nonlocal remaining, cut
        if len(text) + 1 > remaining:
            cut = True
            return False
        lines.append(text)
        if units is not None:
            units.append(text)
        if unit_weights is not None:
            unit_weights.append(assertion_count)
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
        if stats is not None:
            stats['assertions'] += sum(len(set(pair)) for pair in pairs[1:])
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
            if stats is not None:
                stats['assertions'] += len(keys)
            continue
        if relation == 'conflict':
            text = 'Unresolved memory conflict (do not choose silently): ' + ' / '.join(
                _fact_text(f, now) for _, f in facts)
        elif relation:
            text = f'Related context about {owner.name} ({owner.canonical_id}), {relation}: ' + _fact_text(facts[0][1], now)
        else:
            text = '= ' + _fact_text(facts[0][1], now)
        if append(text, len(keys)):
            included.update(keys)
            count += len(keys)
            if selected_records is not None:
                selected_records.extend((owner.canonical_id, fid, f) for fid, f in facts)
            if selected_evidence is not None:
                selected_evidence.extend((owner.canonical_id, fid, f, text,
                                         p.canonical_id or kg.slugify(p.name), len(lines) - 1)
                                        for fid, f in facts)
        elif stats is not None:
            stats['assertions'] += len(keys)
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
        if units is not None:
            units.append(OMITTED_CONTEXT)
        if unit_weights is not None:
            unit_weights.append(0)
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
        person = kg.parse_person_file(f.read())
    return person



def _unique_person(selector):
    """A targeted read must not silently choose the latest file for a shared name/alias."""
    people = []
    for path in sorted(glob.glob(os.path.join(kg.people_dir(), '*.md'))):
        try:
            person = _load(path)
            if not isinstance(person.name, str):
                raise ValueError('saved person name is unavailable')
            people.append(person)
        except Exception:  # an unreadable person may own the same name or alias
            return None, [], 'unavailable'
    normalized = kg.normalize_identifier(selector)
    canonical = [p for p in people if p.canonical_id == selector]
    aliases = [p for p in people if normalized and any(
        kg.normalize_identifier(str(alias)) == normalized for alias in p.identifiers)]
    names = [p for p in people if ' '.join(p.name.split()).casefold() == ' '.join(selector.split()).casefold()]
    matches = canonical or aliases or names
    if len(matches) != 1:
        return None, [], 'ambiguous' if matches else 'no_match'
    person = matches[0]
    # Shared aliases cannot expand the selected person's historical scope, even on an exact CID.
    identifiers = [alias for alias in person.identifiers if sum(
        any(kg.normalize_identifier(str(other)) == kg.normalize_identifier(str(alias))
            for other in candidate.identifiers) for candidate in people) == 1]
    return person, identifiers, 'unique'


def _person_obligation_history(person, identifiers, selector, topic, identity_status, *, interactive):
    if identity_status in ('ambiguous', 'unavailable'):
        return {'status': 'identity_unresolved' if identity_status == 'ambiguous' else 'unavailable',
                'items': [], 'omitted': 0, 'absence_is_authoritative': False,
                'coverage': ('Multiple saved people match. Resolve the identity before reading obligations.'
                             if identity_status == 'ambiguous' else
                             'Saved identity could not be read completely; obligations were not selected.')}
    exact_identifier = bool(EMAIL_RE.fullmatch(selector.strip()) or
                            re.fullmatch(r'\+?[0-9][0-9 ()-]{5,}', selector.strip()))
    return _read_obligation_history(interactive=interactive, topic=topic,
        canonical_id=person.canonical_id if person else '',
        identifiers=identifiers if person else [selector] if exact_identifier else [])


def _history_sources(*, interactive=False):
    """One authority projection for obligation evidence and source-wide learning metadata."""
    from source_context import CONTEXT_SOURCES, allowed
    from ledger_provenance import CANONICAL_SOURCES
    # A protected runtime read uses exactly the captured authority, never saved source health.
    captured = os.environ.get('SOTTO_CAPTURED_SOURCES')
    if captured is not None:
        try:
            permitted = json.loads(captured)
            if type(permitted) is not list or any(type(source) is not str for source in permitted):
                raise ValueError('invalid captured authority')
            # Capability grants such as google_contacts are not canonical ledger sources.
            # Ignore extra grant names; never translate them into a different source permission.
            permitted = [source for source in permitted if source in CANONICAL_SOURCES]
        except (ValueError, TypeError):
            return None
    elif interactive and os.environ.get('SOTTO_READ_ONLY') == '1':
        return None
    else:
        permitted = [source for source in CONTEXT_SOURCES if allowed(source)]
    return permitted


def _read_obligation_history(*, interactive, topic, canonical_id='', identifiers=(), topic_only=False):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
    from loops_query import person_history, topic_history
    from protected_ledger_io import ProtectedLedgerUnavailableError
    permitted = _history_sources(interactive=interactive)
    if permitted is None:
        return {'status': 'unavailable', 'items': [], 'omitted': 0, 'absence_is_authoritative': False,
                'coverage': 'Current source authority is unavailable.'}
    try:
        if topic_only:
            return topic_history(os.environ.get('SOTTO_DATA', '/data'), permitted, topic=topic)
        return person_history(os.environ.get('SOTTO_DATA', '/data'), permitted,
                              canonical_id=canonical_id, identifiers=identifiers, topic=topic)
    except (OSError, ValueError, TypeError, ProtectedLedgerUnavailableError):
        return {'status': 'unavailable', 'items': [], 'omitted': 0, 'absence_is_authoritative': False,
                'coverage': 'Recorded obligations could not be read; source archives were not searched.'}


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


def editable_person(p: kg.PersonFile, now: datetime, topic: str = '',
                    stats: dict | None = None) -> dict:
    """Bounded active fact inventory used to bind an owner correction to one existing fact."""
    p = kg.projected_person(p)
    ranked = _rank_facts(kg.sorted_active_facts(p.facts, now), topic)
    if stats is not None:
        stats['assertions'] = max(0, len(ranked) - kg.MAX_FACTS_FOR_LLM)
    facts = ranked[:kg.MAX_FACTS_FOR_LLM]
    return {'canonical_id': p.canonical_id, 'name': p.name,
            'facts': [{'id': fid, 'text': fact.text,
                       'fact_version': kg.person_fact_version(p.canonical_id, fid, fact)}
                      for fid, fact in facts]}


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


def person_lookup_coverage(found: bool, person: str, identifiers=(), *, interactive=False) -> dict:
    """Read-only guidance for an Ask Sotto person lookup, never proof of complete history.

    Source health describes recent reads, not the reach of a historical search. In particular,
    an empty recent snapshot can still have older conversations to inspect.
    """
    from source_context import source_health, allowed
    from history_coverage import learning_coverage

    exact_identifier = bool(EMAIL_RE.fullmatch(person.strip()) or
                            re.fullmatch(r'\+?[0-9][0-9 ()-]{5,}', person.strip()))
    if found and not exact_identifier:
        exact_identifier = any(EMAIL_RE.fullmatch(str(value).strip()) or
                               re.fullmatch(r'\+?[0-9][0-9 ()-]{5,}', str(value).strip())
                               for value in identifiers)
    coverage = {'graph': 'found' if found else 'no_match', 'historical_sources': []}
    # A saved biography can answer a background question, but cannot establish
    # what happened after a promise. Keep retrieval guidance even on a graph hit.
    health = source_health()
    local = {row['source']: row['status'] for row in health.get('sources', [])
             if isinstance(row, dict) and row.get('source') in ('imessage', 'whatsapp', 'contacts')}
    if not found:
        contacts_status = local.get('contacts', 'unknown')
        coverage['identity_resolution'] = {
            'status': 'exact_identifier' if exact_identifier else 'name_only',
            'contacts_status': contacts_status,
            'next_action': ('use_identifier' if exact_identifier else
                            'search_contacts' if contacts_status in ('ok', 'empty', 'stale', 'partial', 'degraded')
                            else 'check_connection' if contacts_status == 'unverified'
                            else 'ask_owner_for_identifier'),
        }
        if not exact_identifier:
            coverage['identity_resolution']['saved_identity_discovery'] = {
                'reader': 'knowledge-query', 'argv': [],
                'scope': ('Read the bounded current/recent saved identity contact_index once before '
                          'Contacts or asking the owner. Candidates are identity discovery, not a '
                          'partial-name match or permission to select obligations. Use an exact '
                          'returned identity only when the subject is unambiguous. Omitted or '
                          'missing candidates do not establish absence of saved history. The cohort '
                          'covers current participants or recently updated people, not all saved '
                          'identities. It does not establish source permission for history. For '
                          'older/outside-cohort people, continue with existing consented '
                          'Contacts/history guidance.'),
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
    learning = learning_coverage(os.environ.get('SOTTO_DATA', '/data'),
                                 _history_sources(interactive=interactive) or [])
    for row in coverage['historical_sources']:
        if row['source'] in learning['sources']:
            row['learning'] = learning['sources'][row['source']]
    coverage['learning_scope'] = learning['scope']
    return coverage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--person")
    ap.add_argument("--company", help="saved company About and recent news; no research")
    ap.add_argument("--company-page", help="first or returned cursor for bounded saved-company details")
    ap.add_argument("--person-coverage", action="store_true",
                    help="with --person, wrap the result with consent-aware historical source guidance")
    ap.add_argument("--facts-only", action="store_true",
                    help="with --person --person-coverage, read saved facts and coverage without obligations")
    ap.add_argument("--editable-person", action="store_true",
                    help="with --person, return active fact ids/text for an explicit correction")
    ap.add_argument("--topic", default="", help="subject keywords: standalone reads obligations; with person/cohort inputs ranks facts")
    ap.add_argument("--relevant-days", type=int, default=None,
                    help="explicit people/cohort read window (default cohort window: 7 days)")
    ap.add_argument("--calendar", help="gathered calendar JSON; today's attendees pack, and their "
                                       "email domains pull in company context")
    ap.add_argument("--gmail", help="gathered gmail JSON; everyone in today's From/To packs. "
                                    "Supplying it REPLACES the --relevant-days mtime window")
    ap.add_argument("--local", help="consented Bridge local JSON; include current phone/chat participants")
    ap.add_argument("--loops", help="active commitment JSON; defaults to the canonical active ledger")
    ap.add_argument("--interactive", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.facts_only and (not args.person or not args.person_coverage or args.editable_person
            or args.relevant_days is not None or args.company is not None):
        ap.error("--facts-only requires --person --person-coverage without edit, company or cohort flags")
    if args.company is not None:
        value = args.company
        if (not value or value.strip() != value or len(value) > 200
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 or ord(c) in (0x2028, 0x2029)
                       or 0xD800 <= ord(c) <= 0xDFFF for c in value)
                or len(sys.argv[1:]) != (3 if args.interactive else 2) + (2 if args.company_page is not None else 0)
                or sys.argv[1] != '--company'
                or args.company_page is not None and not re.fullmatch(r'first|[a-f0-9]{64}:[0-9]{1,9}', args.company_page)):
            ap.error('--company must be the sole valid selector')
    if args.company_page is not None and args.company is None:
        ap.error("--company-page requires --company")
    now = datetime.now()

    if os.environ.get('SOTTO_READ_ONLY') == '1':
        import jsonstore
        import knowledge_update as ku
        with jsonstore.lock(ku.apply_lock_target()):
            # Never expose a partially applied graph or turn a protected read into repair.
            if os.path.lexists(kg.journal_path()):
                raise RuntimeError('knowledge update pending')
            return query(args, now)

    try:  # legacy name-slug files → canonical_id keying (idempotent; reads work pre-first-update)
        # Under THE graph lock: this is a read path, but migrate re-keys and merges FILES — the one
        # writer that used to run outside the lock, able to race a locked writer or overwrite its
        # in-flight journal. graph_lock also replays an interrupted batch before we read anything.
        import knowledge_update as ku  # noqa: PLC0415 — lazy: a query must never break on import
        with ku.graph_lock():
            kg.migrate_people_dir(now)
    except Exception:  # noqa: BLE001
        pass

    return query(args, now)


def query(args, now):
    if getattr(args, 'company', None) is not None:
        import knowledge_update as ku
        out = {'status': 'missing', 'company_knowledge': {}, 'truncated': False}
        try:
            known = ku.company_knowledge(company_name=args.company, unique=True)
        except ku.CompanyLookupError as exc:
            out['status'] = exc.status
        else:
            if known:
                entry = _company_entry(known)
                full_lines = [(known.get('name') or known['slug']).strip()]
                about = " ".join(known.get('about', '').split())
                if about:
                    full_lines[0] += ' — ' + about
                full_lines += ['- ' + " ".join(str(n).split()) for n in known.get('news', []) if str(n).strip()]
                domain = known.get('domain', '').strip().lower().lstrip('@')
                if len(domain) > 253 or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', domain):
                    domain = ''
                researched = known.get('last_researched', '')
                if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', researched):
                    researched = ''
                out.update(status='saved', company_knowledge={known['slug']: entry},
                           company_identity={'slug': known['slug'], 'name': known.get('name', '')[:200],
                                             'domain': domain, 'last_researched': researched},
                           truncated=bool(known.get('context')) or entry != '\n'.join(full_lines))
                cursor = getattr(args, 'company_page', None)
                if cursor is not None:
                    # Pages reuse the canonical company reader. No new store, no file path input,
                    # and no inference of source freshness or completion from saved prose.
                    content = '\n'.join(full_lines)
                    if known.get('context'):
                        content += '\n\nSaved context\n' + str(known['context'])
                    version = hashlib.sha256(content.encode()).hexdigest()
                    match = re.fullmatch(r'([a-f0-9]{64}):([0-9]{1,9})', cursor)
                    if cursor != 'first' and (not match or match[1] != version or int(match[2]) >= len(content)):
                        out = {'status': 'changed', 'company_knowledge': {}, 'truncated': False,
                               'guidance': 'Saved company changed or cursor is invalid; restart with cursor=first.'}
                    else:
                        offset = 0 if cursor == 'first' else int(match[2])
                        end = offset + 3000  # fits even worst-case escaped Unicode transport
                        out['company_knowledge'] = {known['slug']: content[offset:end]}
                        out['truncated'] = end < len(content)
                        out['next_cursor'] = f'{version}:{end}' if out['truncated'] else None
                        out['coverage'] = 'Saved company record only; original sources were not searched.'
        print(json.dumps(interactive_projection(out) if args.interactive else out))
        return
    if args.person:
        # Accept a name, an email, or a phone — resolved through the same identity map as writes.
        p, identifiers, identity_status = _unique_person(args.person)
        out = {}
        groups = {}
        weights = {}
        omitted_by_person = {}
        selected_records = []
        if p is not None:
            key = p.canonical_id or kg.slugify(p.name)
            stats = {}
            out[key] = editable_person(p, now, args.topic,
                                       stats=stats if args.interactive else None) if args.editable_person else pack_person(
                p, True, now, args.topic, units=groups.setdefault(key, []) if args.interactive else None,
                stats=stats if args.interactive else None,
                selected_evidence=selected_records if args.person_coverage else None,
                unit_weights=weights.setdefault(key, []) if args.interactive else None)
            omitted_by_person[key] = stats.get('assertions', 0)
        result = ({'person_knowledge': out, 'coverage': person_lookup_coverage(bool(out), args.person, identifiers,
                                                                            interactive=args.interactive)}
                  if args.person_coverage else out)
        if args.person_coverage:
            if identity_status in ('ambiguous', 'unavailable'):
                result['coverage']['identity_resolution'] = {
                    'status': identity_status,
                    'next_action': 'ask_owner_for_identifier' if identity_status == 'ambiguous' else 'retry_read'}
            if not args.facts_only:
                result['obligation_history'] = _person_obligation_history(
                    p, identifiers, args.person, args.topic, identity_status, interactive=args.interactive)
        retained_units = {}
        if args.interactive:
            if args.person_coverage and not args.editable_person:
                result['fact_evidence'] = {'items': [], 'omitted': len(selected_records),
                                          'coverage': FACT_EVIDENCE_SCOPE}
            result = interactive_projection(result, packed_groups=groups, packed_weights=weights,
                                            packed_omitted=omitted_by_person, retained_units=retained_units)
        if args.person_coverage and not args.editable_person:
            result = attach_fact_evidence(result, selected_records, now, interactive=args.interactive,
                                          retained_units=retained_units)
        print(json.dumps(result))
        return

    relevant_days = getattr(args, 'relevant_days', None)
    if args.topic and relevant_days is None and not (args.calendar or args.gmail or args.local or args.loops):
        # A topic-only outcome read needs no graph identity/cohort expansion. Keep the same
        # permitted ledger evidence and aggregate interactive projection as the person path.
        result = {'person_knowledge': {}, 'obligation_history': _read_obligation_history(
            interactive=args.interactive, topic=args.topic, topic_only=True)}
        print(json.dumps(interactive_projection(result) if args.interactive else result))
        return

    cutoff = now - timedelta(days=relevant_days if relevant_days is not None else 7)
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
    candidates = []
    groups = {}
    weights = {}
    omitted_by_person = {}
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
        topic = topic_for_identifiers(topics, [*identifiers, p.canonical_id])
        if args.interactive:
            words = _topic_words(topic or args.topic)
            relevance = max((len(words & _topic_words(f.text)) for _, f in
                             kg.sorted_active_facts(p.facts, now)), default=0)
            candidates.append((p, topic, relevance))
        elif len(person_knowledge) < MAX_PEOPLE_PACKED:
            person_knowledge[p.canonical_id or kg.slugify(p.name)] = pack_person(p, False, now, topic)
            if p.company:
                companies.append(p.company)

    if args.interactive:
        # Rank the already eligible cohort before applying the person cap. File order is only a tie break.
        candidates.sort(key=lambda row: -row[2])
        for p, topic, _ in candidates[:MAX_PEOPLE_PACKED]:
            key = p.canonical_id or kg.slugify(p.name)
            stats = {}
            person_knowledge[key] = pack_person(p, False, now, topic or args.topic,
                                               units=groups.setdefault(key, []), stats=stats,
                                               unit_weights=weights.setdefault(key, []))
            omitted_by_person[key] = stats['assertions']
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
    if args.interactive:
        selected_index = [entry for entry in contact_index if entry['canonical_id'] in person_knowledge]
        out = interactive_projection(out, selected_index=selected_index, packed_groups=groups,
                                     packed_weights=weights,
                                     packed_omitted=omitted_by_person,
                                     unselected_people=max(0, len(candidates) - len(person_knowledge)))
    print(json.dumps(out))


if __name__ == "__main__":
    main()
