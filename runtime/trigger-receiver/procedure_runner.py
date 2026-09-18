"""Shared scheduled procedures. Stdout is the final chat artifact, never a send."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


def run(request):
    pack = Path(request['pack'])
    sys.path.insert(0, str(pack / '_shared/lib'))
    sys.path.insert(0, str(pack / '_shared/scripts'))

    def command(relative, *args, decode=True, incomplete_exit=None):
        result = subprocess.run([sys.executable, str(pack / relative), *map(str, args)],
                                capture_output=True, text=True, timeout=600)
        if result.returncode and result.returncode == incomplete_exit:
            # The script's own "not done yet, ask again" verdict (EX_TEMPFAIL): a retry with the
            # same inputs is the right next step, so it is named as such rather than as a crash.
            raise RuntimeError(f'{Path(relative).name} is incomplete; retry')
        if result.returncode:
            raise RuntimeError(f'{Path(relative).name} exit {result.returncode}')
        return json.loads(result.stdout) if decode else result.stdout

    if request['kind'] == 'proactive':
        import jsonstore
        from source_context import project_local, read_local
        from delivery_effects import valid
        with tempfile.TemporaryDirectory(prefix='sotto-proactive-') as directory:
            local, calendar = Path(directory) / 'local.json', Path(directory) / 'calendar.json'
            local_data = read_local()
            if not local_data:
                snapshot = jsonstore.read(str(Path(os.environ.get('SOTTO_DATA', '/data')) /
                                              'knowledge/last_local_snapshot.json'), default={})
                local_data = project_local(snapshot.get('local', {}))
            local.write_text(json.dumps(local_data))
            command('_shared/scripts/gather_google.py', '--skip-gmail',
                    '--gmail-out', Path(directory) / 'gmail.json', '--cal-out', calendar, decode=False)
            # The scanner maintains holds/intentions even on quiet ticks and replays the same
            # reservation on retries. It stages delivery effects under our inherited run ID.
            result = command('proactive/scripts/proactive_scan.py', '--calendar', calendar, '--local', local)
        if not result.get('nudges') or result.get('quiet'):
            return 'NO_NUDGES'
        effects = result.get('_eligibility')
        # The shared scanner emits both per-item eligibility and the global nudge-off guard.
        # Keep one global guard and one proof per nudge for valid() and the outbox.
        if (not isinstance(effects, list)
                or any(not isinstance(effect, dict) or effect.get('kind') not in
                       ('eligibility', 'unsolicited_nudge') for effect in effects)
                or sum(effect['kind'] == 'unsolicited_nudge' for effect in effects) != 1
                or sum(effect['kind'] == 'eligibility' for effect in effects) != len(result['nudges'])):
            raise RuntimeError('proactive scanner omitted eligibility')
        if not valid(effects, time.time()):
            return 'NO_NUDGES'
        import compose_notification
        return compose_notification.compose('proactive', result['nudges'])
    if request['kind'] == 'digest':
        from delivery_effects import stage
        # digest_check exits 75 (and prints its verdict) when the review could not complete —
        # e.g. the reviewing model call failed — so the incomplete case never reaches the JSON.
        result = command('event-triage/scripts/digest_check.py', incomplete_exit=75)
        if not result.get('deliver'):
            return 'NO_NUDGES'
        item_ids = result.get('item_ids')
        if not isinstance(item_ids, list) or not item_ids:
            raise RuntimeError('digest item identities are missing')
        effects = [{'kind': 'digest_accept', 'item_ids': item_ids}, *(result.get('effects') or [])]
        if result.get('valid_until') is not None:
            effects.append({'kind': 'eligibility', 'valid_until': result['valid_until']})
        stage(effects)
        lines = [f"{item['sender']}: {item['why']}" for item in result.get('items') or []]
        return 'Midday catch-up\n\n' + '\n\n'.join(lines) if lines else 'NO_NUDGES'
    if request['kind'] == 'pulse':
        from source_context import allowed, read_local, used_sources
        from delivery_effects import stage
        cutoff = datetime.now(timezone.utc).isoformat()
        with tempfile.TemporaryDirectory(prefix='sotto-pulse-') as directory:
            local, gmail = Path(directory) / 'local.json', Path(directory) / 'gmail.json'
            local_data = read_local(1008)
            local.write_text(json.dumps(local_data))
            command('_shared/scripts/gather_google.py', '--window-days', '42', '--bodies', '0',
                    '--max', '300', '--sent-max', '300', '--skip-stale', '--skip-calendar',
                    '--gmail-out', gmail, '--cal-out', Path(directory) / 'calendar.json', decode=False)
            sources = used_sources(local_data, json.loads(gmail.read_text()))
            result = command('relationship-pulse/scripts/relationship_pulse.py', local, '--gmail', gmail)
            if any(not allowed(source) for source in sources):
                raise RuntimeError('source permission changed while preparing pulse')
            stage([{'kind': 'source_permissions', 'sources': sources, 'coverage_until': cutoff}])
            return result.get('pulse_markdown') or 'NO_NUDGES'
    raise ValueError('unknown scheduled procedure')


if __name__ == '__main__':
    try:
        print(run(json.loads(sys.argv[1])))
    except Exception as error:
        print(f'[procedure_runner] {type(error).__name__}', file=sys.stderr)
        sys.exit(1)
