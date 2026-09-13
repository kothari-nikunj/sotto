#!/usr/bin/env python3
"""Clean-state, frozen-time native-model replay. Invented sources; no delivery or account writes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
for part in ('scripts', 'knowledge', 'lib'):
    sys.path.insert(0, str(ROOT / '_shared' / part))
import compose_brief  # noqa: E402
import context_learning  # noqa: E402
import dreamer  # noqa: E402
import knowledge  # noqa: E402
import prewarm_graph  # noqa: E402
import style_extract  # noqa: E402

NOW = datetime(2026, 9, 7, 22, 30, tzinfo=timezone.utc)


def run():
    cases = json.loads((ROOT / 'evals/fixtures/relevance_cases.json').read_text())[:7]
    # A completed invitation on another day is not a conflict with tonight's pending one.
    # The shared relevance corpus separately covers conflicting/ambiguous evidence.
    answered = next(c for c in cases if c['id'] == 'answered_invitation')
    answered['messages'][0]['text'] = 'Join us for dinner Wednesday at 7? Please confirm by 5.'
    answered['messages'][1]['text'] = 'Yes, see you Wednesday at 7!'
    local = {'contacts': [], 'imessage': [], 'window_hours': 168}
    for i, case in enumerate(cases):
        phone = f'+120255501{i:02d}'
        local['contacts'].append({'name': case['sender'], 'phones': [phone], 'emails': []})
        for j, msg in enumerate(case['messages']):
            local['imessage'].append({'handle': phone, 'source_id': i*10+j+1,
                'timestamp': f'2026-09-07T21:{10+j:02d}:00Z', 'is_group_chat': False,
                'is_from_me': bool(msg.get('is_from_me')),
                'text': ('Background: ' + case['context'] + '\n' if j == 0 else '') + msg['text']})
    # Actual authored messages seed writing style; no imported profile or old Telegram memory.
    for i, text in enumerate([
        'thanks maya - dinner works. happy to meet at the cafe first, just send me the address.',
        'yep, that works for me. let me check the calendar and get back to you later this afternoon.',
        'thanks for sending this over. i can take a look tonight and send you thoughts tomorrow morning.',
    ]):
        local['imessage'].append({'handle': '+12025550103', 'source_id': 100+i,
            'timestamp': f'2026-09-01T18:0{i}:00Z', 'is_from_me': True, 'is_group_chat': False,
            'text': text})
    seed = style_extract.extract(local, now=NOW)
    prewarm_graph.prewarm(local, research=False)
    inputs = {'type': 'welcome', 'first_run': True, 'now': NOW.isoformat(),
              'userTimezone': 'America/Los_Angeles', 'google': {'emails': [], 'events': []}, 'local': local}
    brief = compose_brief.compose(inputs, critic=True)
    text = brief['brief_markdown']
    attention = '\n'.join(re.findall(
        r'(?ms)^## (?:Needs Attention Now|Should Handle Today)[ \t]*\n(.*?)(?=^## |\Z)', text))
    checks = [{'case': 'observed_voice_reaches_composer',
               'pass': 'thanks maya - dinner works.' in compose_brief._welcome_voice()}]
    for i, case in enumerate(cases):
        included = case['sender'] in attention or f'id:+120255501{i:02d}|' in attention
        checks.append({'case': case['id'], 'pass': included == case['brief_action'], 'observed': included})
    checks += [{'case': 'no_routine_appendices', 'pass': not any(h in text for h in ('## Still open', '## What moved today'))},
               {'case': 'no_generic_intro', 'pass': "I'm Sotto" not in text and 'I’m Sotto' not in text},
               {'case': 'draft_is_for_review', 'pass': not re.search(r'Say draft[^\n]*to send', text, re.I)},
               {'case': 'no_connection_checklist', 'pass': not re.search(r'Link .*full picture|Right now I can see', text)},
               {'case': 'no_unsupported_shared_event', 'pass': not re.search(r'told sam|sam.*hold your seat|sam.*your seat.*maya', text, re.I)}]
    observed = context_learning.observations('gmail', [
        {'id': 'one', 'from': 'Jordan <jordan@example.com>', 'date': '2026-09-05T10:00:00Z',
         'body': 'I am the founder of Lantern, building scheduling software for clinics. Can you review Tuesday?'},
        {'id': 'two', 'from': 'Casey <casey@example.com>', 'to': 'Jordan <jordan@example.com>', 'isSent': True,
         'date': '2026-09-06T10:00:00Z', 'body': 'Reviewed it. The scheduling changes look good; no follow-up needed.'}], NOW)
    learned = context_learning.learn(observed, now=NOW)
    curated = dreamer.run(now=NOW)
    root = Path(os.environ['SOTTO_DATA'])
    checks += [{'case': 'source_bound_memory', 'pass': learned['facts'] > 0},
               {'case': 'dreamer_consumed_new_memory', 'pass': curated['reviewed'] > 0},
               {'case': 'no_historical_nudges', 'pass': not (root / 'events/queue.jsonl').exists()},
               {'case': 'no_historical_open_loops', 'pass': not list((root / 'knowledge/continuity').glob('*.md'))}]
    return {'live': True, 'private_data': False, 'messages_sent': 0, 'clean_state': True,
            'frozen_at': NOW.isoformat(), 'seed': seed, 'brief_text': brief['brief_text'],
            'memory': learned, 'dreamer': curated,
            'people': len(list(Path(knowledge.people_dir()).glob('*.md'))),
            'passed': sum(c['pass'] for c in checks), 'total': len(checks), 'checks': checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({'live': False, 'note': 'Use --live for paid model evaluation.'}))
        return 0
    previous = {k: os.environ.get(k) for k in ('SOTTO_DATA', 'SOTTO_TIMEZONE')}
    try:
        with tempfile.TemporaryDirectory(prefix='sotto-first-day-eval-') as tmp:
            os.environ.update(SOTTO_DATA=tmp, SOTTO_TIMEZONE='America/Los_Angeles')
            # Fixture consent is isolated too; no reads of the real tenant's context or permissions.
            if os.environ.get('SOTTO_DEPLOYMENT_MODE') == 'managed':
                config = Path(tmp) / 'config'; config.mkdir()
                (config / 'managed-capabilities.json').write_text(json.dumps({
                    'tenant_id': os.environ.get('SOTTO_TENANT_ID'),
                    'sources': {s: {'consented': True, 'connected': True} for s in ('imessage', 'gmail', 'contacts')}}))
            report = run()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print(json.dumps(report, indent=2))
    return int(report['passed'] != report['total'])


if __name__ == '__main__':
    raise SystemExit(main())
