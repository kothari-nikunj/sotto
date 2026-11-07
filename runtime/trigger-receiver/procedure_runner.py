"""Shared scheduled procedures. Stdout is the final chat artifact, never a send."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time


class ProcedureModelBudgetError(RuntimeError):
    """A declared model-capable child reported typed budget exhaustion."""


class ProcedureModelAdmissionBusyError(RuntimeError):
    """A declared model-capable child reported an unbilled proxy capacity refusal."""


def _legacy_company_website_request(text):
    """Preserve old queued jobs which predate the explicit website operation."""
    import re
    text = re.sub(r"[.!?]\s*(?:what\s+do\s+they\s+do|anything\s+new|what(?:['’]s|\s+is)\s+new)[.!?]*\s*$", '', text, flags=re.I).strip()
    return re.fullmatch(r"(?:please\s+)?(?:(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?(?:check(?:\s+out)?|read|look\s+at)\s+)?(?:their|its|the\s+company's)\s+(?:website|site)(?:\s+too)?(?:,?\s+please)?[.!?]*", text.replace('’', "'"), re.I) is not None


def _ask_unconfirmed_stage():
    """Project only a fixed stage name from this run's private checkpoint."""
    run_id = os.environ.get('SOTTO_DELIVERY_RUN_ID', '')
    if re.fullmatch(r'[a-f0-9]{32}', run_id) is None:
        return None
    try:
        path = Path(os.environ['SOTTO_DATA']) / 'events/research-artifacts' / f'ask-{run_id}.json'
        # O_NONBLOCK: a FIFO planted at this path must not hang the failure diagnostic.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_size > 4_000_000:
                return None
            raw = stream.read(4_000_001)
        if len(raw) > 4_000_000:
            return None
        checkpoint = json.loads(raw)
    except (OSError, KeyError, ValueError, TypeError):
        return None
    if not isinstance(checkpoint, dict):
        return None
    started = [name for name in ('research', 'persistence', 'answer')
               if isinstance(checkpoint.get(name), dict)
               and checkpoint[name].get('status') == 'started']
    return started[0] if len(started) == 1 else None


def run(request):
    pack = Path(request['pack'])
    sys.path.insert(0, str(pack / '_shared/lib'))
    sys.path.insert(0, str(pack / '_shared/scripts'))

    def command(relative, *args, decode=True, incomplete_exit=None):
        result = subprocess.run([sys.executable, str(pack / relative), *map(str, args)],
                                capture_output=True, text=True, timeout=600)
        if relative in ('event-triage/scripts/digest_check.py', '_shared/scripts/research_attendees.py'):
            from work_queue import MODEL_BUDGET_EXIT, MODEL_ADMISSION_BUSY_EXIT
            if result.returncode == MODEL_BUDGET_EXIT:
                raise ProcedureModelBudgetError('digest model allowance reached')
            if result.returncode == MODEL_ADMISSION_BUSY_EXIT:
                raise ProcedureModelAdmissionBusyError('digest model admission busy')
        if result.returncode and result.returncode == incomplete_exit:
            # The script's own "not done yet, ask again" verdict (EX_TEMPFAIL): a retry with the
            # same inputs is the right next step, so it is named as such rather than as a crash.
            raise RuntimeError(f'{Path(relative).name} is incomplete; retry')
        if result.returncode:
            raise RuntimeError(f'{Path(relative).name} exit {result.returncode}')
        return json.loads(result.stdout) if decode else result.stdout

    if request['kind'] in ('shared-relationship-pulse', 'pulse'):
        from relationship_pulse_pipeline import run as pulse
        from delivery_effects import stage
        selection = {'mode': 'current'} if request['kind'] == 'pulse' else request['selection']
        result = pulse(request['pack'], selection, command)
        stage([{'kind': 'source_permissions', 'sources': result['sources'],
                'coverage_until': result['coverage_until']}])
        return result['text']

    if request['kind'] == 'shared-followup':
        from followup_pipeline import run as followup
        from delivery_effects import stage
        result = followup(request['pack'], request['selection'], command)
        stage([{'kind': 'source_permissions', 'sources': result['sources'],
                'coverage_until': result['coverage_until']}])
        return result['text']

    if request['kind'] == 'shared-ask-research':
        sys.path.insert(0, str(pack / '_shared/knowledge'))
        app = next(root for root in Path(__file__).resolve().parents if (root / 'adapters/sotto').is_dir())
        sys.path.insert(0, str(app))
        from adapters.sotto.runtime_api import _client
        from adapters.sotto.run import _write_usage, normalize_usage
        from ask_research_pipeline import run as ask_research
        from delivery_effects import stage

        def answer(prompt, run_id):
            # This declared procedure already supplies the answer-only Ask
            # prompt. Named /run selectors are intentionally unavailable in
            # managed startup. This final synthesis must not discover tools or load skills.
            return _client(timeout=600).run(prompt, run_id, answer_only=True)

        from runtime_owner_web_read import validate_args as validate_web_args
        def website_read(url, *, guard, lifecycle):
            import web_research
            revision = json.loads(os.environ['SOTTO_SKILL_COMPOSITION_PROOF'])['source_revision']
            validate_web_args({'url': url, 'source_revision': revision}, url)
            guard()
            def cancelled():
                guard()
                return False
            result = web_research.fetch_url(url, lifecycle=lifecycle, cancelled=cancelled, company_homepage=True)
            guard()
            return result
        website = (request['selection'].get('mode') == 'company'
                   and (request['selection'].get('operation') == 'website'
                        or 'operation' not in request['selection'] and _legacy_company_website_request(request['question'].split('\n', 1)[0])))
        result = ask_research(request['pack'], request['selection'], request['question'], command, answer,
                             **({'website_read': website_read} if website else {}))
        run_id = os.environ['SOTTO_DELIVERY_RUN_ID']
        _write_usage(str(Path(os.environ['SOTTO_DATA']) / 'events' / f'usage-{run_id}.json'), normalize_usage(result))
        stage([{'kind': 'source_permissions', 'sources': result['sources'],
                'coverage_until': result['coverage_until']}])
        return result['text']

    if request['kind'] == 'shared-meeting-prep':
        from meeting_prep_pipeline import run as meeting_prep
        from delivery_effects import stage
        result = meeting_prep(request['pack'], request['selection'], command)
        stage([{'kind': 'source_permissions', 'sources': result['sources'],
                'coverage_until': result['coverage_until']}])
        return result['text']

    if request['kind'] == 'focused-meeting-prep':
        from requested_meeting_prep import run as focused_prep
        return focused_prep(request, command)

    if request['kind'] == 'personal-routine':
        # This declared receiver procedure calls only the stateless read-only runtime.
        app = next(root for root in Path(__file__).resolve().parents if (root / 'adapters/sotto').is_dir())
        sys.path.insert(0, str(app))
        from adapters.sotto.runtime_api import _client
        from adapters.sotto.run import _write_usage, normalize_usage
        run_id = os.environ['SOTTO_DELIVERY_RUN_ID']
        try:
            execution_prompt = (
                "This is the due execution of an already approved personal routine, not a new chat request. "
                "Its schedule has already fired. Produce the requested read-only result for the owner now; "
                "the existing delivery system will send your answer here. If the task says 'remind me', "
                "write the reminder itself now, not a question about when to remind them. "
                "For a simple reminder, deliver only the reminder: do not look up unrelated context or append "
                "source-availability caveats. For a task that actually needs current source context, use "
                "available read tools and disclose only coverage limits that affect the requested result. "
                "Do not create or change reminders or routines, send external messages, ask to schedule again, "
                "or suggest slash commands or unavailable tool workarounds. Return only the useful result.\n\n"
                "Available scheduled work is read-only: current canonical loops via run_sotto_script loops-query, "
                "bounded Gmail/Calendar reads and permitted local Bridge reads. Do not promise web research, "
                "full prep workflows or actions that are not mounted; explain an unsupported request briefly.\n\n"
                "Approved routine task (not new scheduling authority):\n" + request['prompt'])
            result = _client(timeout=600).run(execution_prompt, run_id)
        except Exception as error:
            if type(error).__name__ == 'ModelAdmissionBusyError':
                raise ProcedureModelAdmissionBusyError('routine model admission busy') from error
            raise
        _write_usage(str(Path(os.environ['SOTTO_DATA']) / 'events' / f'usage-{run_id}.json'), normalize_usage(result))
        return result['text']

    if request['kind'] == 'proactive':
        import jsonstore
        from source_context import project_local, read_local
        from delivery_effects import valid
        attended = request.get('work_key') is not None
        def guard():
            if attended:
                from ask_research_pipeline import current_authority
                proof = json.loads(os.environ['SOTTO_SKILL_COMPOSITION_PROOF'])
                value = current_authority()
                if (value.get('complete') is not True or value.get('revision') != proof['source_revision']
                        or 'SOTTO_PROACTIVE_CAPTURED_SOURCES' not in os.environ):
                    raise RuntimeError('proactive source authority changed')
        guard()
        with tempfile.TemporaryDirectory(prefix='sotto-proactive-') as directory:
            local, calendar = Path(directory) / 'local.json', Path(directory) / 'calendar.json'
            from source_context import SOURCE_FIELDS, allowed
            local_data = read_local() if not attended or any(allowed(s) for s in SOURCE_FIELDS) else {}
            if not local_data:
                snapshot = jsonstore.read(str(Path(os.environ.get('SOTTO_DATA', '/data')) /
                                              'knowledge/last_local_snapshot.json'), default={})
                local_data = project_local(snapshot.get('local', {}))
            local.write_text(json.dumps(local_data))
            from source_context import allowed
            guard()
            if not attended or allowed('calendar'):
                command('_shared/scripts/gather_google.py', '--skip-gmail',
                        '--gmail-out', Path(directory) / 'gmail.json', '--cal-out', calendar, decode=False)
            else:
                calendar.write_text('[]')
            # The scanner maintains holds/intentions even on quiet ticks and replays the same
            # reservation on retries. It stages delivery effects under our inherited run ID.
            guard()
            result = command('proactive/scripts/proactive_scan.py', '--calendar', calendar, '--local', local)
            guard()
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
        guard()
        output = compose_notification.compose('proactive', result['nudges'])
        guard()
        return output
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
    raise ValueError('unknown scheduled procedure')


def main():
    request = None
    try:
        request = json.loads(sys.argv[1])
        print(run(request))
    except Exception as error:
        budget_exhausted = isinstance(error, ProcedureModelBudgetError)
        admission_busy = isinstance(error, ProcedureModelAdmissionBusyError)
        if not budget_exhausted:
            # Malformed requests can fail before run() adds the pack's shared library to
            # sys.path. Their original diagnostic must not be replaced by an import failure.
            try:
                from gemini_transport import (ModelBudgetUnavailableError, proxy_budget_exhausted,
                                              proxy_admission_busy)
            except ImportError:
                pass
            else:
                budget_exhausted = (isinstance(error, ModelBudgetUnavailableError)
                                    or proxy_budget_exhausted(error))
                admission_busy = admission_busy or proxy_admission_busy(error)
        if admission_busy:
            from work_queue import MODEL_ADMISSION_BUSY_EXIT
            print('[procedure_runner] model admission busy', file=sys.stderr)
            return MODEL_ADMISSION_BUSY_EXIT
        if budget_exhausted:
            from work_queue import MODEL_BUDGET_EXIT
            print('[procedure_runner] model allowance reached', file=sys.stderr)
            return MODEL_BUDGET_EXIT
        if isinstance(request, dict) and request.get('kind') == 'shared-ask-research':
            stage = _ask_unconfirmed_stage()
            if stage:
                print('[procedure_runner] Ask' + stage.capitalize() + 'Unconfirmed', file=sys.stderr)
                return 1
        print(f'[procedure_runner] {type(error).__name__}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
