"""Bind usefulness feedback to a retained, consented brief; one outcomes writer."""
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
from personal_context import FEEDBACK_EXCERPT_CHARS, archived_output, item_locator  # noqa: E402


def items():
    root = Path(os.environ.get('SOTTO_DATA', '/data'))
    result = []
    for path in sorted((root / 'briefs').glob('*.json'), reverse=True):
        match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})_(morning|evening|welcome)', path.stem)
        if not match or not (root / 'briefs' / f'{match[1]}.{match[2]}.delivered').exists():
            continue
        text = archived_output(path.stem)
        if text:
            result.append({'reference': path.stem, 'text': text})
        if len(result) >= 8:
            break
    return result


def record(reference, rating, excerpt='', reason=''):
    if os.environ.get('SOTTO_UNATTENDED'):
        raise ValueError('usefulness feedback requires the owner in chat')
    if rating not in ('useful', 'not_useful'):
        raise ValueError('invalid rating')
    item = next((item for item in items() if item['reference'] == reference), None)
    if item is None:
        raise ValueError('no eligible delivered brief with that reference')
    text = item['text']
    excerpt = excerpt.strip()
    if len(excerpt) > FEEDBACK_EXCERPT_CHARS or len(reason) > 900:
        raise ValueError('feedback must be short')
    # Keep only the user's narrow decision and the existing output reference. The referenced
    # archive remains the source of truth; copying brief/draft prose into the outcomes stream made
    # private historical text appear in every future model prompt.
    locator = item_locator(text, excerpt)
    key = hashlib.sha256(f'{reference}|{locator}'.encode()).hexdigest()[:16]
    return log_outcome.log({'action_id': 'feedback:' + key, 'outcome': rating,
                            'source': 'user_feedback', 'reference': reference,
                            'item_locator': locator, 'decision': rating})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('rating', choices=('list', 'useful', 'not_useful'))
    parser.add_argument('--reference', help='a reference printed by `list`')
    parser.add_argument('--excerpt', default='', help='optional unique verbatim excerpt, at most '
                        f'{FEEDBACK_EXCERPT_CHARS} characters; stores a hashed locator, not text')
    parser.add_argument('--reason', default='', help='optional; length-checked and discarded, '
                        'never stored')
    args = parser.parse_args()
    try:
        result = items() if args.rating == 'list' else record(
            args.reference, args.rating, args.excerpt, args.reason)
        print(json.dumps(result))
    except ValueError as error:
        print(json.dumps({'error': str(error)}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
