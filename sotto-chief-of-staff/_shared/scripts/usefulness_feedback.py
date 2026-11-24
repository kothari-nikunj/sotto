"""Bind usefulness feedback to retained, consented outputs; one outcomes writer."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import log_outcome  # noqa: E402
from personal_context import FEEDBACK_EXCERPT_CHARS, accepted_outputs, item_locator  # noqa: E402


def items(*, permitted=None, versions=False):
    outputs = accepted_outputs(permitted=permitted)
    return [{'reference': reference, **(outputs[reference] if versions else {'text': outputs[reference]['text']})}
            for reference in sorted(outputs, reverse=True)[:8]]


def record(reference, rating, excerpt='', reason='', *, permitted=None, archive_version=None):
    if os.environ.get('SOTTO_UNATTENDED'):
        raise ValueError('usefulness feedback requires the owner in chat')
    if rating not in ('useful', 'not_useful'):
        raise ValueError('invalid rating')
    item = next((item for item in items(permitted=permitted, versions=True) if item['reference'] == reference), None)
    if item is None:
        raise ValueError('no eligible delivered brief with that reference')
    if archive_version is not None and item['archive_version'] != archive_version:
        raise ValueError('referenced archive changed')
    version = item['archive_version']
    text = item['text']
    excerpt = excerpt.strip()
    if len(excerpt) > FEEDBACK_EXCERPT_CHARS or len(reason) > 900:
        raise ValueError('feedback must be short')
    # Keep the decision and content-free identity so no replacement can inherit the rating.
    locator = item_locator(text, excerpt)
    key = hashlib.sha256(f'{reference}|{version}|{locator}'.encode()).hexdigest()[:16]
    return log_outcome.log({'action_id': 'feedback:' + key, 'outcome': rating,
                            'source': 'user_feedback', 'reference': reference,
                            'archive_version': version, 'item_locator': locator, 'decision': rating})


def _record_notification(reference, rating, receipt, event_id):
    """The attended receiver supplies the exact accepted, still-permitted outbox proof."""
    if os.environ.get('SOTTO_UNATTENDED'):
        raise ValueError('usefulness feedback requires the owner in chat')
    event_key = hashlib.sha256(f'notification-feedback:{event_id}'.encode()).hexdigest()
    return log_outcome.log({'action_id': 'feedback:' + reference, 'event_id': event_key,
                            'outcome': rating, 'source': 'notification_feedback', 'reference': reference,
                            'decision_id': receipt['decision_id'], 'source_ref': receipt['source_ref'],
                            'archive_version': receipt['archive_version']},
                           dedupe_latest=True)


def locator_matches(args, locator):
    excerpt = args.get('excerpt', '').strip()
    if not excerpt:
        return locator == 'output'
    match = re.fullmatch(r'excerpt:(\d+):(\d+):([a-f0-9]{20})', str(locator))
    return bool(match) and int(match[2]) == len(excerpt) and match[3] == hashlib.sha256(excerpt.encode()).hexdigest()[:20]


def outcome(args, locator):
    reference = 'notification:' + args['dispatch_id'] if args['operation'] == 'notification-usefulness' else args['reference']
    return {'operation': args['operation'], 'reference': reference, 'item_locator': locator,
            'rating': args['rating'], 'status': 'recorded'}


def record_admitted(proof, selection, select, permitted):
    args = proof['args']
    if args['operation'] == 'notification-usefulness':
        if select() != selection:
            raise ValueError('accepted notification changed')
        return _record_notification(outcome(args, 'output')['reference'], args['rating'], selection, proof['event_id'])
    return record(args['reference'], args['rating'], args['excerpt'], permitted=permitted,
                  archive_version=args['archive_version'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('rating', choices=('list', 'useful', 'not_useful'))
    parser.add_argument('--reference', help='a reference printed by `list`')
    parser.add_argument('--archive-version', help='exact archive_version printed by list')
    parser.add_argument('--excerpt', default='', help='optional unique verbatim excerpt, at most '
                        f'{FEEDBACK_EXCERPT_CHARS} characters; stores a hashed locator, not text')
    parser.add_argument('--reason', default='', help='optional; length-checked and discarded, '
                        'never stored')
    args = parser.parse_args()
    try:
        if args.rating != 'list' and not args.archive_version:
            raise ValueError('archive version from list is required')
        result = items(versions=True) if args.rating == 'list' else record(
            args.reference, args.rating, args.excerpt, args.reason, archive_version=args.archive_version)
        print(json.dumps(result))
    except ValueError as error:
        print(json.dumps({'error': str(error)}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
