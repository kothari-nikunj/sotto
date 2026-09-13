"""Shared scheduled procedures. Stdout is the final chat artifact, never a send."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


def run(request):
    pack = Path(request['pack'])
    sys.path.insert(0, str(pack / '_shared/lib'))

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

    if request['kind'] == 'digest':
        from delivery_effects import stage
        # digest_check exits 75 (and prints its verdict) when the review could not complete —
        # e.g. the reviewing model call failed — so the incomplete case never reaches the JSON.
        result = command('event-triage/scripts/digest_check.py', incomplete_exit=75)
        if not result.get('deliver'):
            return 'NO_NUDGES'
        coverage = result.get('coverage_until')
        if not coverage:
            raise RuntimeError('digest coverage is missing')
        effects = [{'kind': 'digest_stamp', 'coverage_until': coverage}, *(result.get('effects') or [])]
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
