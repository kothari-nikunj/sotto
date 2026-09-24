#!/usr/bin/env python3
"""Read one meeting's notes and canonical obligations. No model, extraction, or ledger writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path[:0] = [str(Path(__file__).resolve().parent),
               str(Path(__file__).resolve().parents[1] / 'lib')]
import ledger_io  # noqa: E402
from source_context import allowed  # noqa: E402

MAX_MEETING_ITEMS = 20
NOTES_CHARS = 8000


def obligations(meeting_id, rows=None):
    """Join by source occurrence, never a shared name or merely another meeting with a person."""
    if not meeting_id or not allowed('granola'):
        return []
    rows = ledger_io.load_entries() if rows is None else rows
    result = []
    for row in rows:
        thread = row.get('source_thread_id') or ''
        refs = row.get('source_refs') or []
        meeting_ref = next((ref for ref in refs if isinstance(ref, dict)
                            and ref.get('source') == 'granola'
                            and ref.get('source_id') == meeting_id), None)
        direct = isinstance(thread, str) and thread.startswith(f'granola:{meeting_id}:')
        if not direct and meeting_ref is None:
            continue
        status = row.get('status', 'open')
        # A closed ledger row is a state, not necessarily proof that the promised work happened.
        resolution = str(row.get('resolution') or '')
        refs = row.get('resolution_source_refs') or []
        evidence_visible = bool(refs) and all(
            isinstance(ref, dict) and ref.get('sourceType') in ('email', 'imessage', 'whatsapp')
            and allowed({'email': 'gmail'}.get(ref['sourceType'], ref['sourceType'])) for ref in refs)
        evidence = row.get('resolution_evidence') if evidence_visible else None
        basis = ('user_confirmed' if resolution == 'user_resolved' and status == 'resolved' else
                 'source_evidence' if evidence and status == 'resolved' else 'ledger_state')
        result.append({'anchor_key': row.get('anchor_key'), 'status': status,
                       'direction': 'they_owe_you' if ledger_io.is_waiting_on(row.get('action_type')) else 'you_owe',
                       'what': row.get('ask') or row.get('summary') or '',
                       'person': row.get('contact_name') or row.get('contact_identifier') or '',
                       'deadline': row.get('deadline'),
                       'source_snippet': (meeting_ref.get('snippet') if meeting_ref is not None
                                          else row.get('source_snippet')),
                       'resolution': resolution, 'completion_basis': basis,
                       'resolved_at': row.get('resolved_at'),
                       'resolution_evidence': evidence})
    return sorted(result, key=lambda r: (r['status'] not in ledger_io.ACTIVE,
                                         str(r['deadline'] or '9999'), str(r['anchor_key'])))


def query(meeting_id, granola):
    if not allowed('granola'):
        return {'status': 'withheld', 'reason': 'Granola access is disabled.'}
    meetings = granola.get('meetings', []) if isinstance(granola, dict) else granola
    matches = [m for m in meetings if isinstance(m, dict)
               and (m.get('meeting_id') or m.get('id')) == meeting_id]
    if len(matches) != 1:
        return {'status': 'not_found' if not matches else 'ambiguous',
                'coverage': 'Only the supplied Granola window was searched.'}
    meeting = matches[0]
    notes = {k: str(meeting.get(k) or '') for k in ('your_notes', 'ai_summary')}
    rows = obligations(meeting_id)
    return {'status': 'found', 'meeting_id': meeting_id, 'title': meeting.get('title'),
            'start': meeting.get('start') or meeting.get('date'),
            'notes': {k: v[:NOTES_CHARS] for k, v in notes.items()},
            'notes_truncated': any(len(v) > NOTES_CHARS for v in notes.values()),
            'obligations': rows[:MAX_MEETING_ITEMS], 'omitted_obligations': max(0, len(rows) - MAX_MEETING_ITEMS),
            'coverage': 'Notes describe decisions; ledger rows describe recorded obligations. An empty list does not establish that nothing was promised.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--meeting-id', required=True)
    parser.add_argument('--granola', required=True, help='fresh consented gather_granola output')
    args = parser.parse_args()
    print(json.dumps(query(args.meeting_id, json.loads(Path(args.granola).read_text()))))


if __name__ == '__main__':
    main()
