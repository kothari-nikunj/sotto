#!/usr/bin/env python3
"""
loops_query.py — the open-loops / action-ledger view for the `sotto-loops` skill.

Reads the continuity ledger ($SOTTO_DATA/knowledge/continuity/*.md — the same files the brief's
continuity_resolve.py maintains) and splits the ACTIVE loops into two directions a chief of staff cares
about, interleaving due work with undated work by age (the split itself is
`ledger_io.WAITING_ON_TYPES` — one predicate,
shared with the resolver and retune_scan):
  - you_owe        — things YOU need to do (reply / follow up / follow up on a stale thread / call back)
  - waiting_on_them — things you've handed off and are AWAITING the other side on

This is read-only (resolution happens in the brief's Learn step); it just surfaces what's open so the
user can ask "what am I waiting on?" / "what are my open loops?" without running a whole brief.

Usage: loops_query.py            → {you_owe:[...], waiting_on_them:[...], counts:{...}}
Env: SOTTO_DATA (ledger root), SOTTO_TIMEZONE (for "today"/age).
"""
from __future__ import annotations

import json
import os
import re
import sys
import argparse
import unicodedata
from datetime import datetime

# Shared tz helpers so "today"/age match the brief; shared ledger loader so all the
# read views agree on what's open (ledger_io).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from textutil import _s  # noqa: E402
from timeutil import _now_local, _calendar_day, configured_tz  # noqa: E402
import ledger_io  # noqa: E402
from delivery_effects import loop_version, loop_identity  # noqa: E402
from ledger_provenance import project_entries  # noqa: E402

# Direction is defined ONCE, in ledger_io: `waiting_on` is what THEY owe you; everything else
# active — `follow_up_stale` included, since it resolves on your outgoing message and expires on
# your clock — is what you owe. A second copy here is how a chased item got filed under you_owe.


def _entry(it: dict, today: datetime, today_str: str, *, include_request_context=False, include_ask=False) -> dict:
    name = ledger_io.contact_label(it)
    label = ledger_io.task_label(it)
    if include_ask:
        label = _s(it.get("summary")).strip() or label
    deadline = _calendar_day(it.get("deadline"))
    courtesy = ledger_io.is_optional_birthday_courtesy(it)
    if courtesy:
        label, deadline = 'Optional birthday greeting', ''
    age = ledger_io.age_days(it.get("created_at"), today)
    entry = {
        "loop_version": loop_version(it), "loop_identity": loop_identity(it),
        "name": name,
        "what": label[:200],
        "channel": _s(it.get("channel")),
        "identifier": _s(it.get("contact_identifier")),
        # The Gmail thread this debt came from (ledger frontmatter). Surfaced so an email draft
        # offered off this loop lands IN the thread (`google_action.py gmail-draft --thread-id`)
        # instead of starting a new one; "" for every loop with no email thread behind it.
        "thread_id": _s(it.get("source_thread_id")),
        "group_id": _s(it.get("group_id")),
        "action_type": _s(it.get("action_type")),
        "age_days": age,
        "deadline": deadline or None,
        "overdue": bool(deadline and deadline < today_str),
        # Chase state (written ONLY by continuity_resolve — see its `_stamp_chase`). Surfaced so
        # "what am I waiting on" can name it ("chased once, Tuesday") and so proactive_scan can
        # read today's chase-due item without a second ledger reader.
        # `chase_pending` is the PROPOSED chase for that day: proactive_scan delivers it and calls
        # `continuity_resolve.py --finalize-chase <anchor_key>`, which is what makes it count.
        "anchor_key": _s(it.get("anchor_key")),
        "chased_count": int(it.get("chased_count") or 0),
        "chase_pending": _calendar_day(it.get("chase_pending")),
        "last_chased_at": _calendar_day(it.get("last_chased_at")),
        "chase_after": _calendar_day(it.get("chase_after")),
        # Chased its full quota: the automatic chase has stopped and the decision is the user's.
        # A hand-off is something the system OWES them, so lanes may surface it without waiting
        # for the tidy-up threshold.
        "chased_out": int(it.get("chased_count") or 0) >= ledger_io.CHASE_MAX,
        # …and the day that question was actually DELIVERED (continuity_resolve's
        # `--finalize-handoff`, the hand-off's half of the chase's two-phase rule). Once it is set
        # the user has been asked, so the question is not repeated and the brief stops giving the
        # loop a named line — it waits here, on this list, for their answer.
        "handoff_asked_at": _calendar_day(it.get("handoff_asked_at")),
    }
    if include_ask:
        entry['ask'] = _s(it.get('ask'))[:200]
        entry['canonical_id'] = _s(it.get('canonical_id'))
    if include_request_context:
        entry.update(created_at=_s(it.get('created_at')), birthday_courtesy=courtesy,
                     canonical_id=_s(it.get('canonical_id')), ask=_s(it.get('ask'))[:200])
    return entry


def rank_entries(entries: list[dict], today: str) -> list[dict]:
    """Keep urgent dates first while reserving every third slot for undated work.

    Apply after narrowing to a person too, so the three-line notification and the
    forty-row follow-up share the same fairness rule. Future dates never bury undated work.
    """
    key = lambda e: (e.get('deadline') or '', -(e.get('age_days') or 0),
                     e.get('anchor_key', ''), e.get('name', ''), e.get('what', ''))
    urgent = sorted((e for e in entries if e.get('deadline') and e['deadline'] <= today), key=key)
    undated = sorted((e for e in entries if not e.get('deadline')), key=key)
    future = sorted((e for e in entries if e.get('deadline') and e['deadline'] > today), key=key)
    result = []
    for index in range(max((len(urgent) + 1) // 2, len(undated))):
        result.extend(urgent[index * 2:index * 2 + 2])
        result.extend(undated[index:index + 1])
    return result + future


def _query_rows(active_rows: list[dict], parked_rows: list[dict] | None,
                *, log_diagnostic: bool, now: datetime | None = None, include_request_context=False, include_ask=False) -> dict:
    today = now or _now_local(configured_tz() or "+00:00")
    today_str = today.strftime("%Y-%m-%d")
    you_owe, waiting = [], []
    # No second birthday visibility rule here: active_entries/load_active already applied
    # ledger_io.hidden_from_obligation_views, the same predicate the brief reads.
    for it in active_rows:   # ACTIVE and not user-snoozed — ledger_io owns both rules
        # Meeting prep/info are calendar shadows, not communication debts — the docket and the
        # brief's schedule section own them. (Legacy entries; new ones are no longer created.)
        if ledger_io.normalize_action_type(it.get("action_type")) in ("meeting_prep", "meeting_info"):
            continue
        e = _entry(it, today, today_str, include_request_context=include_request_context, include_ask=include_ask)
        (waiting if ledger_io.is_waiting_on(it.get("action_type")) else you_owe).append(e)
    you_owe = rank_entries(you_owe, today_str)
    waiting = rank_entries(waiting, today_str)
    out = {"you_owe": you_owe, "waiting_on_them": waiting,
           "counts": {"you_owe": len(you_owe), "waiting_on_them": len(waiting)}}
    if parked_rows is not None:
        parked = []
        for it in parked_rows:
            if it.get("status") not in ledger_io.PARKED:
                continue
            if ledger_io.normalize_action_type(it.get("action_type")) in ledger_io.MEETING_TYPES:
                continue
            parked.append(_entry(it, today, today_str, include_request_context=include_request_context, include_ask=include_ask))
        parked = rank_entries(parked, today_str)
        out["parked"] = parked
        out["counts"]["parked"] = len(parked)
    if log_diagnostic and os.environ.get('SOTTO_READ_ONLY') != '1':
        try:
            from sotto_log import diag
            diag(f"[loops_query] {out['counts']['you_owe']} you-owe, {out['counts']['waiting_on_them']} waiting-on")
        except Exception:
            pass
    return out


def query(include_parked: bool = False) -> dict:
    """Legacy reader behavior remains unchanged until an explicit protected caller is wired."""
    if 'SOTTO_PROACTIVE_CAPTURED_SOURCES' in os.environ:
        from source_context import CONTEXT_SOURCES, allowed
        return query_permitted_rows(os.environ.get('SOTTO_DATA','/data'),
            [s for s in CONTEXT_SOURCES if allowed(s)], include_parked=include_parked)
    return _query_rows(ledger_io.load_active(), ledger_io.load_entries() if include_parked else None,
                       log_diagnostic=True)



def query_permitted_rows(data_root: str, permitted: list[str], *, include_parked=False,
                         now: datetime | None = None, include_request_context=False, include_ask=False) -> dict:
    """Protected canonical read using the caller's captured current source grants."""
    if type(permitted) is not list or any(type(source) is not str for source in permitted) or type(include_parked) is not bool or type(include_request_context) is not bool or type(include_ask) is not bool:
        raise ValueError('permitted loop read invalid')
    clock = now or _now_local(configured_tz() or '+00:00')
    if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError('permitted loop clock invalid')
    visible = permitted_rows(data_root, permitted)
    result = _query_rows(ledger_io.active_entries(visible, clock), visible if include_parked else None,
                         log_diagnostic=False, now=clock, include_request_context=include_request_context, include_ask=include_ask)
    result['coverage'] = 'currently_permitted_canonical_loops'
    return result


def permitted_rows(data_root, permitted):
    """Reuse the protected full source-union projection before rendering any fields."""
    from protected_ledger_io import load_protected_entries
    from ledger_provenance import classify_ledger_sources, source_loop_sources, UnknownProvenanceError
    visible = []
    for row in load_protected_entries(data_root):
        try:
            if row.get('content_provenance') is not None:
                kind, _sources = classify_ledger_sources(row)
                if kind == 'owner':
                    visible.append(row)
                    continue
            sources = source_loop_sources(row)
        except (UnknownProvenanceError, TypeError, ValueError, KeyError):
            continue
        if all(source in permitted for source in sources):
            visible.append(row)
    return visible



HISTORY_LIMIT = 12
HISTORY_BYTES = 18 * 1024


def person_history(data_root: str, permitted: list[str], *, canonical_id='', identifiers=(),
                   topic='', now: datetime | None = None) -> dict:
    """Bounded recorded obligations for an exact person, including terminal states."""
    return _obligation_history(data_root, permitted, canonical_id=canonical_id,
                               identifiers=identifiers, topic=topic, now=now)


def topic_history(data_root: str, permitted: list[str], *, topic: str,
                  now: datetime | None = None) -> dict:
    """Read independent obligation candidates containing every literal topic token.

    This selects records, not people: matching names never merge identities or tasks.
    Source projection precedes matching, ambiguity, ranking and counts.
    """
    return _obligation_history(data_root, permitted, topic=topic, now=now, topic_only=True)


def _topic_tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize('NFKC', text).casefold()
    words = ''.join(char if char.isalnum() or unicodedata.category(char).startswith('M') else ' '
                    for char in normalized).split()
    return {word for word in words if any(char.isalnum() for char in word)}


def _obligation_history(data_root: str, permitted: list[str], *, canonical_id='', identifiers=(),
                        topic='', now: datetime | None = None, topic_only=False) -> dict:
    """One protected source projection and evidence renderer; never reads source archives or writes."""
    from textutil import _normalize_identifier
    if type(permitted) is not list or any(type(source) is not str for source in permitted):
        raise ValueError('permitted history read invalid')
    clock = now or _now_local(configured_tz() or '+00:00')
    if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError('permitted history clock invalid')
    aliases = {_normalize_identifier(value) for value in identifiers if isinstance(value, str)} - {''}
    result = {'status': 'no_match', 'items': [], 'omitted': 0, 'absence_is_authoritative': False,
              'coverage': 'Only matching retained obligations permitted by current source grants are returned. '
                          'No match means no matching permitted rows were returned; it does not establish whether '
                          'other canonical records exist or whether any obligation is outstanding. '
                          'Active rows are not recorded as fulfilled; this is not proof that the work has not been done or sent. '
                          'Later fulfillment may be unread. Records outside this permission-filtered view are not included or counted. '
                          'Older outcomes may have aged out; a reopened obligation may replace its prior outcome. '
                          'created_at is the resolution cutoff, not necessarily the original promise time. '
                          'Source timestamps are reported only when retained; meeting timestamps describe the '
                          'meeting boundary, not an exact utterance. Reference lists retain their first and latest '
                          'stored entries when capped; storage order is not independently verified chronology. '
                          'Source archives were not searched.'}
    topic_words = _topic_tokens(topic) if topic_only else set()
    if topic_only and not topic_words:
        result['status'] = 'needs_topic'
        return result
    if not topic_only and not canonical_id and not aliases:
        result['status'] = 'identity_unresolved'
        return result
    visible = permitted_rows(data_root, permitted)
    selected = []
    for row in visible:
        # Retired rows are bookkeeping aliases, not distinct dismissed obligations.
        if row.get('status') in ledger_io.TERMINAL and row.get('resolution') in ('merged_duplicate', 'user_retargeted'):
            continue
        cid = _s(row.get('canonical_id'))
        identifier = _normalize_identifier(_s(row.get('contact_identifier')))
        if topic_only:
            parts = [_s(row.get(key)) for key in
                     ('ask', 'summary', 'source_snippet', 'resolution_evidence', 'resolution_reason')]
            for key in ('source_refs', 'resolution_source_refs'):
                refs = row.get(key) or []
                refs = [refs] if isinstance(refs, dict) else refs
                parts.extend(_s(ref.get('snippet')) for ref in refs if isinstance(ref, dict))
            if not topic_words <= _topic_tokens(' '.join(parts)):
                continue
        else:
            if cid and canonical_id and cid != canonical_id:
                continue
            if not (canonical_id and cid == canonical_id or identifier and identifier in aliases):
                continue
        if ledger_io.normalize_action_type(row.get('action_type')) in ledger_io.MEETING_TYPES:
            continue
        if ledger_io.hidden_from_obligation_views(row, clock):
            continue
        selected.append(row)
    words = set(re.findall(r'[a-z0-9]{3,}', topic.lower()))
    def order(row):
        text = ' '.join(_s(row.get(key)) for key in ('ask', 'summary')).lower()
        return (-len(words & set(re.findall(r'[a-z0-9]{3,}', text))),
                _s(row.get('anchor_key')), _s(row.get('created_at')))
    for row in sorted(selected, key=order):
        if len(result['items']) >= HISTORY_LIMIT:
            result['omitted'] += 1
            continue
        truncated = []
        def excerpt(key, value, limit=1600):
            text = _s(value)
            if len(text) > limit:
                truncated.append(key)
            return text[:limit]
        def refs(key):
            values = row.get(key) or []
            values = [values] if isinstance(values, dict) else values
            out = []
            selected_refs = list(enumerate(values))
            if len(selected_refs) > 4:
                selected_refs = selected_refs[:1] + selected_refs[-3:]
            for index, ref in selected_refs:
                # Provenance has already validated the source union. Return only evidence fields.
                # Preserve original field names, which existing readers and evidence IDs use.
                item = {field: excerpt(f'{key}.{index}.{field}', ref[field], 1200 if field == 'snippet' else 512)
                        for field in ('sourceType', 'source_type', 'source', 'sourceId', 'source_id', 'id',
                                      'snippet', 'timestamp', 'date') if field in ref}
                out.append(item)
            if len(values) > 4:
                truncated.append(key)
            return out
        status = _s(row.get('status')) or 'open'
        resolution = _s(row.get('resolution'))
        item = {key: excerpt(key, row.get(key), 512) for key in
                ('anchor_key', 'canonical_id', 'contact_identifier', 'contact_name', 'channel',
                 'action_type', 'created_at', 'closed_at', 'resolved_at', 'source_timestamp',
                 'source_timestamp_kind', 'source_thread_id', 'source_message_id', 'deadline', 'snoozed_until',
                 'reopened_at', 'parked_at', 'last_chased_at', 'handoff_asked_at')}
        item['source_timestamp_state'] = 'recorded' if item['source_timestamp'] else 'unavailable'
        chased = _s(row.get('chased_count'))
        item['chased_count'] = int(chased) if re.fullmatch(r'[0-9]{1,9}', chased) else None
        item.update(status=status, direction='they_owe_you' if ledger_io.is_waiting_on(row.get('action_type')) else 'you_owe',
                    ask=excerpt('ask', row.get('ask') or row.get('summary')),
                    summary=excerpt('summary', row.get('summary')),
                    source_snippet=excerpt('source_snippet', row.get('source_snippet'), 1200),
                    source_refs=refs('source_refs'), resolution_source_refs=refs('resolution_source_refs'),
                    resolution=resolution, resolution_evidence=excerpt('resolution_evidence', row.get('resolution_evidence')),
                    resolution_reason=excerpt('resolution_reason', row.get('resolution_reason')))
        item['completion_basis'] = ledger_io.completion_basis(status, resolution,
            evidence_visible=bool(item['resolution_evidence'] and item['resolution_source_refs']))
        item['truncated_fields'] = sorted(set(truncated))
        result['items'].append(item)
        if len(json.dumps(result).encode('utf-8')) > HISTORY_BYTES:
            result['items'].pop()
            result['omitted'] += 1
    if selected:
        result['status'] = 'ambiguous' if topic_only and len(selected) > 1 else 'matched'
    if topic_only:
        from history_coverage import learning_coverage
        # Evidence keeps its original 18 KiB budget; this fixed-source metadata has a separate cap.
        result['learning'] = learning_coverage(data_root, permitted)
    return result


def query_owner_rows(active_rows: list[dict], *, now: datetime,
                     parked_rows: list[dict] | None = None) -> dict:
    """Owner-only path over caller-bounded parsed rows.

    Filter raw frontmatter before labels, sorting and counts. The caller must
    enforce regular non-symlink files, count/byte limits and canonical status
    selection; ledger_io.load_entries itself does not bound disk reads. This
    function is not exposed to the CLI or agent by this slice.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("protected query requires timezone-aware now")
    deny_source = lambda _source: False
    active = project_entries(active_rows, deny_source)
    parked = project_entries(parked_rows, deny_source) if parked_rows is not None else None
    out = _query_rows(active, parked, log_diagnostic=False, now=now)
    out["coverage"] = "owner_authored_only"
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read Sotto's open-loop ledger")
    parser.add_argument("--parked", action="store_true",
                        help="also return parked obligations for an explicit restore request")
    args = parser.parse_args()
    print(json.dumps(query(include_parked=args.parked)))
