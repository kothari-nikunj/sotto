"""Exercise an installed prep procedure in a fresh process with fictional source reads."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

PACK = Path('/app/sotto-skills')
sys.path[:0] = ['/app/trigger-receiver', str(PACK / '_shared/lib')]
import procedure_runner  # noqa: E402
import source_context  # noqa: E402

route = sys.argv[1]
assert route in ('offer', 'direct')
start = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
meeting = {'event_id': 'fictional-event', 'start': start}
event = {'id': meeting['event_id'], 'start': start, 'summary': 'Dana product review',
         'attendees': [{'email': 'dana@outside.example', 'displayName': 'Dana Example'}]}
point = 'Dana Example wants to review the simulator launch results.'
real_run = subprocess.run


def fixture_read(argv, **kwargs):
    args = list(map(str, argv))
    name = Path(args[1]).name
    if name == 'gather_google.py':
        for flag, value in [('--gmail-out', []), ('--cal-out', [event]),
                            ('--source-results-out', {'calendar': {'status': 'ok'}})]:
            Path(args[args.index(flag) + 1]).write_text(json.dumps(value))
        return subprocess.CompletedProcess(argv, 0, '', '')
    if name == 'research_attendees.py':
        return subprocess.CompletedProcess(argv, 0, '{"attendees":[]}', '')
    return real_run(argv, **kwargs)


with tempfile.TemporaryDirectory(prefix='sotto-prep-smoke-') as directory:
    root = Path(directory)
    model = root / 'writer.json'
    model.write_text(json.dumps({'prep_markdown': point,
        'meetings': [{**meeting, 'talking_points': [point]}]}))
    os.environ.update(SOTTO_DATA=str(root), HOME=str(root), SOTTO_USER_EMAIL='owner@fund.example',
                      SOTTO_LLM_STUB=str(model), SOTTO_DELIVERY_RUN_ID='a' * 32)
    if route == 'offer':
        request = {'kind': 'focused-meeting-prep', 'offer': {'kind': 'meeting_prep',
            'person': 'Dana Example', 'meeting': meeting, 'offer_id': 'b' * 64}}
    else:
        request = {'kind': 'shared-meeting-prep',
                   'selection': {'mode': 'focus', 'focus': 'Dana Example'}}
    with (patch.object(source_context, 'allowed', lambda source: source == 'calendar'),
          patch.object(source_context, 'read_local', lambda _hours: {}),
          patch.object(subprocess, 'run', fixture_read)):
        assert procedure_runner.run(dict(request, pack=str(PACK))) == point

print(f'packaged {route} returned the requested meeting prep', flush=True)
