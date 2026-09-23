#!/usr/bin/env python3
"""Capture commitments from new or changed Granola meeting content during durable Learn.

The existing ``*.learned.json`` receipts are the checkpoint: only revisions recorded by a
successful prior capture are skipped. Granola's normal 14-day overlapping gather therefore picks
up notes that arrive late or are edited later without paying to re-extract unchanged meetings.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
PACK = HERE.parents[1]
sys.path.insert(0, str(PACK / '_shared/lib'))
sys.path.insert(0, str(HERE))

import apply_commitments  # noqa: E402
import compose_followup  # noqa: E402
import gemini  # noqa: E402
import jsonstore  # noqa: E402
import model_work  # noqa: E402
import source_context  # noqa: E402
from timeutil import configured_user_email  # noqa: E402

CAPTURE_WINDOW_HOURS = 14 * 24
MAX_MEETINGS_PER_RUN = 3
REVISION_FIELDS = ('meeting_id', 'id', 'title', 'start', 'end', 'date', 'time',
                   'attendee_emails', 'attendees', 'your_notes', 'ai_summary')


def meeting_revision(meeting: dict, *, include_transcript: bool = True) -> str:
    body = {key: meeting.get(key) for key in REVISION_FIELDS if meeting.get(key) not in (None, '')}
    if include_transcript and meeting.get('transcript'):
        body['transcript'] = meeting['transcript']
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]


def extraction_revision(meeting: dict, user_email: str = '', *, stable: bool = False) -> str:
    """Checkpoint/cache identity for source plus every contract that can change its meaning."""
    email = configured_user_email() or user_email
    provider, model = gemini.gemini_transport.effective_compose_model()
    return model_work.revision([
        meeting_revision(meeting, include_transcript=not stable), email.lower(),
        os.environ.get('SOTTO_USER_NAME', '').strip().lower(),
        provider, model, (HERE / '../references/followup-prompt.md').resolve().read_text(),
        Path(compose_followup.__file__).read_text(), Path(apply_commitments.__file__).read_text()])


def revision_history(data_root: str) -> tuple[set[str], dict[str, str]]:
    revisions, failed = set(), {}
    for filename in glob.glob(os.path.join(data_root, 'briefs', '*.learned.json')):
        try:
            receipt = json.loads(Path(filename).read_text())
            step = (receipt.get('steps') or {}).get('commitments') or {}
            proof = step.get('proof') or {}
            coverage = proof.get('coverage')
            if ((step.get('status') == 'ok' and coverage == 'successful_meeting_revisions')
                    or (step.get('status') == 'failed'
                        and coverage == 'partial_successful_meeting_revisions')):
                revisions.update(r for r in proof.get('meeting_revisions', []) if isinstance(r, str))
                revisions.update(r for r in proof.get('stable_meeting_revisions', [])
                                 if isinstance(r, str))
                attempted_at = str(receipt.get('ts') or '')
                for revision in proof.get('failed_meeting_revisions', []):
                    if isinstance(revision, str) and attempted_at >= failed.get(revision, ''):
                        failed[revision] = attempted_at
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return revisions, failed


def successful_revisions(data_root: str) -> set[str]:
    return revision_history(data_root)[0]


def extract_apply_meeting(meeting: dict, user_email: str = '', *, llm=None,
                          data_root: str | None = None) -> dict:
    """One cached, grounded extraction and canonical apply for every automatic caller."""
    root = data_root or os.environ.get('SOTTO_DATA', '/data')
    if not source_context.allowed('granola'):
        raise PermissionError('Granola source access is disabled')
    email = configured_user_email() or user_email
    source_revision = meeting_revision(meeting)
    revision = extraction_revision(meeting, email)
    stable_revision = extraction_revision(meeting, email, stable=True)
    directory = Path(root) / 'events/notification-artifacts'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'followup-{revision}.json'
    stable_path = directory / f'followup-{stable_revision}.json'
    with jsonstore.lock(model_work.artifact_lock_path(directory, str(stable_path))):
        cached = jsonstore.read(str(path), default=None, strict=True)
        if not isinstance(cached, dict):
            cached = jsonstore.read(str(stable_path), default=None, strict=True)
        if (isinstance(cached, dict)
                and revision in (cached.get('revision_aliases') or [cached.get('meeting_revision')])):
            if not source_context.allowed('granola'):
                raise PermissionError('Granola source access was revoked')
            return cached
        inputs = {'granola': [meeting], 'user_email': email, '_followup': True,
                  'google': {'events': [], 'userEmail': email}}
        _, ended = compose_followup.build_context(inputs, CAPTURE_WINDOW_HOURS)
        if not ended:
            return {'meeting_revision': '', 'followup': {}, 'ledger': {}}
        out = compose_followup.compose(inputs, since_hours=CAPTURE_WINDOW_HOURS, llm=llm)
        # Consent can change while model work is in flight. Recheck before either the canonical
        # write or its success artifact so revoked notes cannot create a durable obligation.
        if not source_context.allowed('granola'):
            raise PermissionError('Granola source access was revoked')
        ledger = apply_commitments.apply(out, email, source_meetings=ended)
        result = {'meeting_revision': revision, 'stable_meeting_revision': stable_revision,
                  'revision_aliases': list(dict.fromkeys([revision, stable_revision])),
                  'source_revision': source_revision,
                  'followup': out, 'ledger': ledger}
        # Cache only after canonical persistence. A cached result is therefore a truthful receipt.
        jsonstore.write_atomic(str(path), result)
        if stable_path != path:
            jsonstore.write_atomic(str(stable_path), result)
        return result


def capture(granola: dict, user_email: str = '', *, llm=None, data_root: str | None = None) -> dict:
    meetings = granola.get('meetings', []) if isinstance(granola, dict) else []
    meetings = [m for m in meetings if isinstance(m, dict)]
    root = data_root or os.environ.get('SOTTO_DATA', '/data')
    user_email = configured_user_email() or user_email
    done, failed_attempts = revision_history(root)
    selected = [(extraction_revision(m, user_email), m) for m in meetings]
    selected = [(revision, meeting) for revision, meeting in selected
                if revision not in done and not (
                    not meeting.get('transcript')
                    and extraction_revision(meeting, user_email, stable=True) in done)]
    # Filter before the cap: future/empty meetings are not extractable and must not occupy all
    # bounded slots. Previously failed revisions remain retryable, but fresh data goes first so a
    # permanently poisoned old revision cannot starve newly arrived notes forever.
    selected = [(revision, meeting) for revision, meeting in selected
                if compose_followup.build_context(
                    {'granola': [meeting], 'google': {'events': []}}, CAPTURE_WINDOW_HOURS)[1]]
    selected.sort(key=lambda pair: (
        pair[0] in failed_attempts,
        failed_attempts.get(pair[0], ''),
        str(pair[1].get('end') or pair[1].get('start') or pair[1].get('date') or '')))
    selected = selected[:MAX_MEETINGS_PER_RUN]
    if not selected:
        return {'meeting_revisions': [], 'examined': 0, 'written': 0, 'deduped': 0,
                'skipped_terminal': 0, 'anchor_keys': []}

    totals = {'written': 0, 'deduped': 0, 'skipped_terminal': 0}
    anchors, revisions, stable_revisions, failures = [], [], [], []
    for _revision, meeting in selected:
        try:
            result = extract_apply_meeting(meeting, user_email, llm=llm, data_root=root)
        except Exception as error:  # one poisoned revision must not starve later meetings
            failures.append({'meeting_revision': _revision, 'error': type(error).__name__})
            continue
        if not result.get('meeting_revision'):
            continue
        ledger = result.get('ledger') or {}
        revisions.append(result['meeting_revision'])
        stable_revisions.append(result.get('stable_meeting_revision') or result['meeting_revision'])
        for key in totals:
            totals[key] += int(ledger.get(key) or 0)
        anchors.extend(ledger.get('anchor_keys') or [])
    return {'meeting_revisions': revisions, 'stable_meeting_revisions': stable_revisions,
            'failed_meeting_revisions': [row['meeting_revision'] for row in failures],
            'examined': len(revisions), **totals, 'anchor_keys': anchors, 'failures': failures}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--granola', required=True)
    parser.add_argument('--user-email', default='')
    args = parser.parse_args(argv)
    try:
        value = json.loads(Path(args.granola).read_text())
        if not isinstance(value, dict):
            raise ValueError('Granola input must be an object')
    except (OSError, ValueError) as error:
        print(f'[capture_commitments] unreadable Granola input: {type(error).__name__}', file=sys.stderr)
        return 1
    result = capture(value, args.user_email)
    print(json.dumps(result))
    return 1 if result.get('failures') else 0


if __name__ == '__main__':
    raise SystemExit(main())
