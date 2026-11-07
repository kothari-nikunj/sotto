"""Real shared selection/filter/composer/persistence, with synthetic external provider inputs."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

PACK = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    for part in ('_shared/lib', '_shared/scripts', 'meeting-prep/scripts'):
        monkeypatch.syspath_prepend(str(PACK/part))
    import meeting_prep_pipeline as pipeline
    import compose_meeting_prep as composer
    import source_context
    import compose_brief
    import persist_prep
    monkeypatch.setenv('SOTTO_DATA',str(tmp_path))
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID','a'*32)
    monkeypatch.setattr(composer.ledger_io,'load_entries',lambda:[])
    grants = set(source_context.CONTEXT_SOURCES)
    monkeypatch.setattr(source_context,'allowed',lambda source:source in grants)
    monkeypatch.setattr(source_context,'read_local',lambda _: {'contacts':[]})
    starts=[(datetime.now(timezone.utc)+timedelta(hours=hour)).isoformat() for hour in (2,3)]
    events=[{'id':'dana','summary':'Dana meeting','start':starts[0],
             'attendees':[{'email':'dana@outside.com','displayName':'Dana'}]},
            {'id':'sam','summary':'Sam meeting','start':starts[1],
             'attendees':[{'email':'sam@outside.com','displayName':'Sam'}]}]
    calls=[]; models=[]; revoke_at=[]
    def command(script,*args,decode=True):
        calls.append(script)
        if script.endswith('gather_google.py'):
            if '--attendee-comms' in args:
                attendees=json.loads(Path(args[1]).read_text())
                Path(args[3]).write_text(json.dumps({row['email']:[{'subject':'Real thread'}] for row in attendees}))
            else:
                Path(args[1]).write_text('[]'); Path(args[3]).write_text(json.dumps(events))
                Path(args[5]).write_text(json.dumps({'calendar':{'status':'ok'}}))
            return ''
        if script.endswith('gather_granola.py'):
            assert args[:4]==('--days',30,'--transcripts-since-hours',168)
            Path(args[-1]).write_text(json.dumps({'meetings':[{'meeting_id':'past','attendee_emails':['dana@outside.com'],
                'start':starts[0],'transcript':'Past synthetic transcript'}]})); return ''
        if script.endswith('knowledge_query.py'):
            result=subprocess.run([sys.executable,str(PACK/script),*map(str,args)],capture_output=True,text=True,check=True)
            return json.loads(result.stdout)
        if script.endswith('select_attendees.py'):
            return compose_brief.select_attendees_for_research(json.loads(Path(args[0]).read_text()))
        if script.endswith('persist_prep.py'):
            if '--filter-fresh' in args:
                file=Path(args[1]); selected=json.loads(file.read_text())
                kept,skipped=persist_prep.filter_fresh(selected,keep=args[-1] if '--keep' in args else '')
                file.write_text(json.dumps(kept))
                if 'filter' in revoke_at:
                    grants.discard('calendar')
                return {'kept':len(kept),'skipped_fresh':skipped}
            if 'persist_failure' in revoke_at:
                raise RuntimeError('synthetic local persistence refusal')
            return persist_prep.persist(json.loads(Path(args[1]).read_text()),json.loads(Path(args[3]).read_text()))
        if script.endswith('research_attendees.py'):
            selected=json.loads(Path(args[1]).read_text())
            assert '--focus' in args or len(selected)==2
            result={'attendees':[{'email':row['email'],'name':row['name'],'title':'Engineer',
                'summary':'Per research https://example.com/evidence'} for row in selected]}
            Path(args[args.index('--out')+1]).write_text(json.dumps(result))
            if 'research' in revoke_at:
                grants.discard('calendar')
            return result
        if script.endswith('x_connectivity.py'):
            Path(args[-1]).write_text(json.dumps({'attendees':[{'email':'dana@outside.com','handle':'dana',
                'posts':[{'text':'Synthetic source post'}], 'bookmarks':[{'text':'Synthetic bookmark'}]}]}));return ''
        pytest.fail('unexpected shared command '+script)
    def model(prompt,inputs):
        models.append(inputs)
        assert inputs['attendee_comms'] and inputs['attendee_research']['attendees']
        if 'granola' in grants:
            assert inputs['local']['granola_meetings']
        if 'x' in grants:
            assert inputs['x_context']['attendees'][0]['posts']
            assert inputs['x_context']['attendees'][0]['bookmarks']
        if 'composition' in revoke_at:
            grants.discard('calendar')
        return json.dumps({'prep_markdown':'Actual shared prep', 'meetings':[
            {'event_id':event['id'],'start':event['start'],'talking_points':['Discuss their actual product plans.']}
            for event in inputs['google']['events']]})
    monkeypatch.setattr(composer,'call_gemini',model)
    return pipeline,command,events,grants,calls,models,revoke_at,tmp_path


@pytest.mark.parametrize('selection,count',[({'mode':'sweep'},2),({'mode':'focus','focus':'Dana'},1)])
def test_full_skill_sweep_focus_and_actual_grounded_persistence(pipeline,selection,count):
    module,command,events,grants,calls,models,revoked,data=pipeline
    result=module.run(PACK,selection,command)
    assert result['text']=='Actual shared prep' and len(models[0]['google']['events'])==count
    assert {'calendar','gmail','granola','x','x_bookmarks'} <= set(result['sources'])
    assert calls.count('meeting-prep/scripts/persist_prep.py')==2
    assert '_shared/scripts/gather_granola.py' in calls and '_shared/scripts/x_connectivity.py' in calls
    assert result['persistence']['persisted']>=1
    assert list((data/'knowledge/people').glob('*.md'))


def test_unmatched_focus_returns_clarification_without_research_or_model(pipeline):
    module,command,events,grants,calls,models,revoked,data=pipeline
    result=module.run(PACK,{'mode':'focus','focus':'Unknown Person'},command)
    assert 'Which person or meeting should I prep?' in result['text']
    assert result['sources']==['calendar']
    assert calls==['_shared/scripts/gather_google.py']
    assert models==[]
    assert not list((data/'knowledge/people').glob('*.md'))


def test_optional_sources_absent_and_persistence_failure_reported(pipeline,capsys):
    module,command,events,grants,calls,models,revoked,data=pipeline
    grants.difference_update({'granola','x','x_bookmarks'})
    revoked.append('persist_failure')
    result=module.run(PACK,{'mode':'sweep'},command)
    assert result['text']=='Actual shared prep' and result['persistence']['status']=='error'
    assert 'research persistence failed' in capsys.readouterr().err
    assert '_shared/scripts/gather_granola.py' not in calls and '_shared/scripts/x_connectivity.py' not in calls
    assert 'granola' not in result['sources']


def test_partial_google_receipt_reaches_actual_prep_context(pipeline):
    module, command, events, grants, calls, models, revoked, data = pipeline
    import compose_meeting_prep

    def partial_read(script, *args, **kwargs):
        result = command(script, *args, **kwargs)
        if script.endswith('gather_google.py') and '--source-results-out' in args:
            Path(args[args.index('--source-results-out') + 1]).write_text(json.dumps({
                'calendar': {'status': 'ok'},
                'gmail': {'status': 'partial', 'error': 'rate_limited'},
            }))
        return result

    result = module.run(PACK, {'mode': 'sweep'}, partial_read)
    assert result['text'] == 'Actual shared prep'
    context, _ = compose_meeting_prep.build_context(models[0])
    assert 'Partial coverage: Gmail' in context
    assert "Google's query quota was reached" in context
    assert 'do not infer that missing results mean nothing happened' in context


@pytest.mark.parametrize('stage',['filter','research','composition'])
def test_current_source_revocation_stops_next_model_or_persistence(pipeline,stage):
    module,command,events,grants,calls,models,revoked,data=pipeline
    revoked.append(stage)
    with pytest.raises(RuntimeError,match='source permission changed'):
        module.run(PACK,{'mode':'focus','focus':'Dana'},command)
    if stage=='filter':
        assert '_shared/scripts/research_attendees.py' not in calls
    if stage in ('filter','research'):
        assert not models
    assert calls.count('meeting-prep/scripts/persist_prep.py')==1
    assert not list((data/'knowledge/people').glob('*.md'))


@pytest.mark.parametrize('focus', ['Dana Example', 'Dana'])
def test_named_prep_uses_existing_exact_email_context_without_an_offer(pipeline, monkeypatch, focus):
    module, command, events, grants, calls, models, revoked, data = pipeline
    import source_context
    events[0]['attendees'][0].pop('displayName')
    monkeypatch.setattr(source_context, 'read_local', lambda _: {
        'contacts': [{'name': 'Dana Example', 'emails': ['dana@outside.com']}],
        'source_status': {'contacts': 'ok'}})
    result = module.run(PACK, {'mode': 'focus', 'focus': focus}, command)
    assert result['text'] == 'Actual shared prep'
    assert [e['id'] for e in models[0]['google']['events']] == ['dana']
    assert models[0]['attendee_research']['attendees'][0]['name'] == 'Dana Example'
    assert set(models[0]['attendee_comms']) == {'dana@outside.com'}
    assert calls.count('_shared/scripts/gather_google.py') == 2  # existing calendar + selected comms
    assert calls.count('_shared/scripts/research_attendees.py') == 1


@pytest.mark.parametrize('case', ['wrong_email', 'source_off', 'ambiguous'])
def test_named_prep_does_not_guess_identity_from_unusable_name_context(pipeline, monkeypatch, case):
    module, command, events, grants, calls, models, revoked, data = pipeline
    import source_context
    events[0]['attendees'][0].pop('displayName')
    contacts = [{'name': 'Dana Example', 'emails': ['dana@outside.com']}]
    if case == 'wrong_email':
        contacts[0]['emails'] = ['another@outside.com']
    elif case == 'source_off':
        grants.discard('contacts')
    else:
        events[0]['attendees'][0]['displayName'] = 'Dana Example'
        events[1]['attendees'][0]['displayName'] = 'Dana Example'
        contacts.append({'name': 'Dana Example', 'emails': ['sam@outside.com']})
    monkeypatch.setattr(source_context, 'read_local', lambda _: {
        'contacts': contacts, 'source_status': {'contacts': 'ok'}})
    result = module.run(PACK, {'mode': 'focus', 'focus': 'Dana Example'}, command)
    assert 'Which person or meeting should I prep?' in result['text']
    assert calls == ['_shared/scripts/gather_google.py']
    assert models == []
    assert not list((data/'knowledge/people').glob('*.md'))


def test_named_prep_reads_cached_envelopes_once_and_selects_next_same_person_meeting(pipeline, monkeypatch):
    module, command, events, grants, calls, models, revoked, data = pipeline
    import personal_context
    events[0]['attendees'][0].pop('displayName')
    events.append({**events[0], 'id': 'later-dana',
                   'start': (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()})
    reads = []
    def snapshot():
        reads.append(True)
        return [({'source': 'gmail', 'from': 'Dana Example <dana@outside.com>'}, None, None)]
    monkeypatch.setattr(personal_context, 'conversation_snapshot', snapshot)
    result = module.run(PACK, {'mode': 'focus', 'focus': 'Dana Example'}, command)
    assert result['text'] == 'Actual shared prep'
    assert [e['id'] for e in models[0]['google']['events']] == ['dana']
    assert reads == [True]
    assert 'gmail' in result['sources']

# Exercise the durable receiver selection through the real shared preparation pipeline.
from test_runtime_owner_skill_request import context as context, rec as rec, setup as setup_admission, request as skill_request  # noqa: PLC0414 — explicit pytest fixture re-exports

__all__ = ['context', 'rec']  # Re-export fixtures for pytest's name-based injection.


def test_literal_short_prep_receiver_job_selects_actual_email_only_attendee(pipeline, context, rec, monkeypatch):
    module, command, events, grants, calls, models, revoked, data = pipeline
    events[0]['attendees'] = [{'email': 'tanya@example.invalid'}]
    call = setup_admission(context, rec, monkeypatch)
    receipt = call(skill_request({'mode': 'focus', 'focus': 'Tanya'}, text='full prep on Tanya'))
    assert receipt['status'] == 'committed'
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    request = json.loads(job['payload']['prompt'])
    result = module.run(PACK, request['selection'], command)
    assert result['text'] == 'Actual shared prep'
    assert [e['id'] for e in models[0]['google']['events']] == ['dana']


def test_initial_google_and_local_reads_overlap_without_losing_context(pipeline, monkeypatch):
    from threading import Barrier
    import source_context
    module, command, events, grants, calls, models, revoked, data = pipeline
    both_started = Barrier(2, timeout=3)
    local_reads = []
    def read_local(hours):
        local_reads.append(hours)
        both_started.wait()
        return {'contacts': [{'name': 'Dana Example', 'emails': ['dana@outside.com']}],
                'source_status': {'contacts': 'ok'}}
    def concurrent_command(script, *args, **kwargs):
        if script.endswith('gather_google.py') and '--gmail-out' in args:
            both_started.wait()
        return command(script, *args, **kwargs)
    monkeypatch.setattr(source_context, 'read_local', read_local)
    events[0]['attendees'][0].pop('displayName')
    result = module.run(PACK, {'mode': 'focus', 'focus': 'Dana Example'}, concurrent_command)
    assert result['text'] == 'Actual shared prep'
    assert local_reads == [168]
    assert models[0]['local']['contacts'][0]['name'] == 'Dana Example'
    assert models[0]['attendee_research']['attendees'][0]['name'] == 'Dana Example'
    assert set(models[0]['attendee_comms']) == {'dana@outside.com'}
    assert calls.count('_shared/scripts/gather_google.py') == 2


@pytest.mark.parametrize('failure', ['google', 'local', 'revoked_calendar'])
def test_parallel_source_reads_join_before_model_and_fail_closed(pipeline, monkeypatch, failure):
    from threading import Barrier
    import source_context
    module, command, events, grants, calls, models, revoked, data = pipeline
    both_started = Barrier(2, timeout=3)
    finished = []
    def read_local(hours):
        both_started.wait()
        finished.append('local')
        if failure == 'local':
            raise RuntimeError('synthetic local read failure')
        if failure == 'revoked_calendar':
            grants.discard('calendar')
        return {'contacts': []}
    def concurrent_command(script, *args, **kwargs):
        if script.endswith('gather_google.py') and '--gmail-out' in args:
            both_started.wait()
            finished.append('google')
            if failure == 'google':
                raise RuntimeError('synthetic Google read failure')
        return command(script, *args, **kwargs)
    monkeypatch.setattr(source_context, 'read_local', read_local)
    with pytest.raises(RuntimeError, match='synthetic|source permission changed'):
        module.run(PACK, {'mode': 'focus', 'focus': 'Dana'}, concurrent_command)
    assert sorted(finished) == ['google', 'local']
    assert not models
    assert '_shared/scripts/research_attendees.py' not in calls
    assert 'meeting-prep/scripts/persist_prep.py' not in calls


def test_prep_timings_use_existing_log_with_only_fixed_labels_and_elapsed_ms(pipeline):
    import re
    module, command, events, grants, calls, models, revoked, data = pipeline
    result = module.run(PACK, {'mode': 'focus', 'focus': 'Dana'}, command)
    assert result['text'] == 'Actual shared prep'
    lines = (data / 'logs/compose_brief.log').read_text().splitlines()
    timings = [line.split('[meeting-prep timing] ', 1)[1]
               for line in lines if '[meeting-prep timing] ' in line]
    stages = {'gather_google', 'gather_local', 'gather', 'identity', 'notes_knowledge',
              'comms', 'research', 'x', 'compose', 'persist'}
    assert len(timings) == len(stages)
    assert {line.split()[0] for line in timings} == stages
    assert all(re.fullmatch(r'[a-z_]+ elapsed_ms=\d+', line) for line in timings)
    assert not any('Dana' in line or 'outside.com' in line or str(data) in line for line in timings)


def test_failed_prep_stage_records_duration_but_not_private_error(pipeline, monkeypatch):
    module, command, events, grants, calls, models, revoked, data = pipeline
    ticks = iter([1, 1.042])
    monkeypatch.setattr(module, 'monotonic', lambda: next(ticks))
    with pytest.raises(ValueError, match='private source excerpt'):
        with module._timed('compose'):
            raise ValueError('private source excerpt')
    lines = (data / 'logs/compose_brief.log').read_text().splitlines()
    assert lines[-1].endswith('[meeting-prep timing] compose elapsed_ms=42')
    assert 'private source excerpt' not in '\n'.join(lines)


def test_prep_avoids_stale_scan(pipeline, monkeypatch):
    module, command, events, grants, calls, models, revoked, data = pipeline
    import gather_google
    reads = []
    inbox = {'id': 'received', 'from': 'Dana <dana@outside.com>', 'subject': 'Product discussion'}
    sent = {'id': 'sent', 'to': 'dana@outside.com', 'subject': 'Our reply', 'isSent': True}
    monkeypatch.setattr(gather_google, '_find_google_api', lambda: 'synthetic-google-api')
    monkeypatch.setattr(gather_google, '_ensure_google_deps', lambda: None)
    monkeypatch.setattr(gather_google, '_refresh_google_token', lambda: None)
    def get_inbox(*args, **kwargs):
        reads.append('inbox')
        return [inbox]
    def get_sent(*args, **kwargs):
        reads.append('sent')
        return [sent]
    def get_stale(*args, **kwargs):
        reads.append('unused-stale-scan')
        return [{'subject': 'Unrelated stale thread'}]
    def get_calendar(*args, **kwargs):
        reads.append('calendar')
        kwargs['observation'].update(complete=True)
        return events
    monkeypatch.setattr(gather_google, 'gather_gmail', get_inbox)
    monkeypatch.setattr(gather_google, 'gather_sent', get_sent)
    monkeypatch.setattr(gather_google, 'gather_stale_sent', get_stale)
    monkeypatch.setattr(gather_google, 'gather_calendar', get_calendar)
    def actual_initial_gather(script, *args, decode=True):
        if script.endswith('gather_google.py') and '--gmail-out' in args:
            calls.append(script)
            with monkeypatch.context() as patched:
                patched.setattr(sys, 'argv', [script, *map(str, args)])
                gather_google.main()
            return ''
        if script.endswith('knowledge_query.py'):
            payload = json.loads(Path(args[args.index('--gmail') + 1]).read_text())
            rows = payload.get('emails', []) if isinstance(payload, dict) else payload
            assert {row['id'] for row in rows} == {'received', 'sent'}
            assert json.loads(Path(args[args.index('--calendar') + 1]).read_text())[0]['id'] == 'dana'
        return command(script, *args, decode=decode)
    result = module.run(PACK, {'mode': 'focus', 'focus': 'Dana'}, actual_initial_gather)
    assert result['text'] == 'Actual shared prep'
    assert reads.count('calendar') == 1 and [r for r in reads if r != 'calendar'] == ['inbox', 'sent']
    assert {row['id'] for row in models[0]['google']['emails']} == {'received', 'sent'}
    assert set(models[0]['attendee_comms']) == {'dana@outside.com'}
    assert {'calendar', 'gmail', 'granola', 'x'} <= set(result['sources'])


@pytest.mark.parametrize('source', ['contacts', 'graph'])
def test_prep_name_indexes_build_once_per_run_without_merging_namesakes(pipeline, monkeypatch, source):
    module, command, events, grants, calls, models, revoked, data = pipeline
    import render_local
    import source_context
    import knowledge as kg
    import prep_selection
    projected_names = []
    original_hits = prep_selection.focus_hits
    def focus_hits(inputs, upcoming=None):
        projected_names.append([event['attendees'][0].get('displayName') for event in inputs['google']['events']])
        return original_hits(inputs, upcoming)
    monkeypatch.setattr(prep_selection, 'focus_hits', focus_hits)
    builds = {'contacts': 0, 'graph': 0}
    original_resolver, original_index = render_local.build_identity_resolver, kg.build_people_index
    def resolver(local):
        builds['contacts'] += 1
        return original_resolver(local)
    def index():
        builds['graph'] += 1
        return original_index()
    monkeypatch.setattr(render_local, 'build_identity_resolver', resolver)
    monkeypatch.setattr(kg, 'build_people_index', index)
    people = data / 'knowledge/people'
    people.mkdir(parents=True, exist_ok=True)
    for iteration, name in enumerate(('Lee Morgan', 'Lee Updated'), 1):
        contacts = []
        for n, event in enumerate(events):
            attendee = event['attendees'][0]
            attendee.pop('displayName', None)
            email = attendee['email']
            if source == 'contacts':
                contacts.append({'name': name, 'emails': [email]})
            else:
                (people / f'person-{n}.md').write_text(
                    f'---\ncanonical_id: c_person{n}\nname: {name}\nidentifiers: [{email}]\n---\n')
        monkeypatch.setattr(source_context, 'read_local', lambda _: {
            'contacts': contacts, 'source_status': {'contacts': 'ok'}})
        result = module.run(PACK, {'mode': 'focus', 'focus': 'Lee Morgan'}, command)
        assert 'Which person or meeting should I prep?' in result['text']
        # First run has two distinct people with the same name. Second run must observe
        # the changed names instead of retaining the preceding run's identity indexes.
        assert projected_names[-1] == [name, name]
        assert builds == {'contacts': iteration, 'graph': iteration if source == 'graph' else 0}
    assert models == []
    assert '_shared/scripts/research_attendees.py' not in calls


def test_reused_prep_identity_index_cannot_cross_a_source_revocation(pipeline, monkeypatch):
    module, command, events, grants, calls, models, revoked, data = pipeline
    import render_local
    import source_context
    for event in events:
        event['attendees'][0].pop('displayName')
    contacts = [{'name': 'Dana Example', 'emails': ['dana@outside.com']},
                {'name': 'Sam Example', 'emails': ['sam@outside.com']}]
    monkeypatch.setattr(source_context, 'read_local', lambda _: {
        'contacts': contacts, 'source_status': {'contacts': 'ok'}})
    original = render_local.build_identity_resolver
    def resolver(local):
        lookup = original(local)
        def revoke_after_first_lookup(email):
            grants.discard('contacts')
            return lookup(email)
        return revoke_after_first_lookup
    monkeypatch.setattr(render_local, 'build_identity_resolver', resolver)
    with pytest.raises(RuntimeError, match='source permission changed during prep attendee identity'):
        module.run(PACK, {'mode': 'focus', 'focus': 'Dana Example'}, command)
    assert models == []
    assert '_shared/scripts/research_attendees.py' not in calls
    assert 'meeting-prep/scripts/persist_prep.py' not in calls


@pytest.fixture
def timed_pipeline(pipeline, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 14, 16, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(__import__('prep_selection'), 'datetime', Clock)
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@fund.example')
    events = pipeline[2]
    for event, hour in zip(events, (17, 18)):
        event.update(start=f'2026-10-14T{hour}:00:00+00:00', end=f'2026-10-14T{hour+1}:00:00+00:00',
                     calendar_id='primary')
    return pipeline


@pytest.mark.parametrize('focus', ['my 10am', '10:00', 'today at 10am'])
def test_timed_prep_receiver_job_runs_exact_calendar_occurrence(timed_pipeline, context, rec, monkeypatch, focus):
    module, command, events, grants, calls, models, revoked, data = timed_pipeline
    call = setup_admission(context, rec, monkeypatch)
    receipt = call(skill_request({'mode': 'focus', 'focus': focus}, text=f'prep for {focus}?'))
    assert receipt['status'] == 'committed'
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    selection = json.loads(job['payload']['prompt'])['selection']
    assert selection['focus'] == focus  # durable owner selection remains literal
    result = module.run(PACK, selection, command)
    assert result['text'] == 'Actual shared prep'
    assert models[0]['exact_meeting'] == {'event_id': 'dana', 'start': events[0]['start']}
    assert [event['id'] for event in models[0]['google']['events']] == ['dana']
    assert set(models[0]['attendee_comms']) == {'dana@outside.com'}
    assert calls.count('_shared/scripts/research_attendees.py') == 1


@pytest.mark.parametrize('case', ['wrong_day', 'ambiguous', 'shared_nonparticipant', 'declined', 'invalid_zone', 'no_time', 'day_conflict', 'partial', 'cancelled', 'missing_id'])
def test_timed_prep_unavailable_never_broadens_or_researches(timed_pipeline, monkeypatch, case):
    module, command, events, grants, calls, models, revoked, data = timed_pipeline
    focus = 'my 10am'
    if case == 'wrong_day':
        focus = 'my 10am tomorrow'
    elif case == 'ambiguous':
        events[1]['start'] = events[0]['start']
    elif case == 'shared_nonparticipant':
        events[0]['calendar_id'] = 'colleague@other.example'
    elif case == 'declined':
        events[0]['my_response'] = 'declined'
    elif case == 'cancelled':
        events[0]['status'] = 'cancelled'
    elif case == 'missing_id':
        events[0].pop('id')
    elif case == 'invalid_zone':
        import timeutil
        monkeypatch.setattr(timeutil, 'configured_tz', lambda: 'Invalid/Zone')
    elif case == 'no_time':
        focus = 'my 9am'
    elif case == 'day_conflict':
        focus = 'today 10am tomorrow'
    def read(*args, **kwargs):
        result = command(*args, **kwargs)
        if case == 'partial' and '--source-results-out' in args:
            Path(args[args.index('--source-results-out') + 1]).write_text(json.dumps({'calendar': {'status': 'partial'}}))
        return result
    result = module.run(PACK, {'mode': 'focus', 'focus': focus}, read)
    assert 'Which day or meeting should I prep?' in result['text']
    assert calls == ['_shared/scripts/gather_google.py']
    assert models == []


def test_timed_prep_explicit_tomorrow_and_exact_shared_copy_dedupe(timed_pipeline):
    import copy
    module, command, events, grants, calls, models, revoked, data = timed_pipeline
    events[0].update(start='2026-10-15T17:00:00+00:00', end='2026-10-15T18:00:00+00:00', iCalUID='same-invite')
    duplicate = copy.deepcopy(events[0])
    duplicate.update(id='shared-copy', calendar_id='shared@example.invalid')
    duplicate['attendees'].append({'email': 'owner@fund.example', 'responseStatus': 'accepted'})
    events.append(duplicate)
    result = module.run(PACK, {'mode': 'focus', 'focus': 'tomorrow 10am'}, command)
    assert result['text'] == 'Actual shared prep'
    assert len(models[0]['google']['events']) == 1
    assert models[0]['exact_meeting']['start'] == '2026-10-15T17:00:00+00:00'


def test_timed_group_prep_keeps_all_actual_attendees(timed_pipeline):
    module, command, events, grants, calls, models, revoked, data = timed_pipeline
    events[0]['attendees'].append({'email': 'sam@outside.com', 'displayName': 'Sam'})
    result = module.run(PACK, {'mode': 'focus', 'focus': 'my 10am'}, command)
    assert result['text'] == 'Actual shared prep'
    assert {r['email'] for r in models[0]['attendee_research']['attendees']} == {'dana@outside.com', 'sam@outside.com'}
    assert models[0]['focus'] == ''
    assert models[0]['exact_meeting']['event_id'] == 'dana'


def test_time_focus_uses_owner_day_at_utc_midnight(timed_pipeline, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 15, 0, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(__import__('prep_selection'), 'datetime', Clock)
    module, command, events, grants, calls, models, revoked, data = timed_pipeline
    events[0].update(start='2026-10-15T01:00:00+00:00', end='2026-10-15T02:00:00+00:00')
    result = module.run(PACK, {'mode': 'focus', 'focus': 'my 6pm'}, command)
    assert result['text'] == 'Actual shared prep'
    assert models[0]['exact_meeting']['start'] == '2026-10-15T01:00:00+00:00'


def test_time_focus_conflicting_owner_response_across_copies_clarifies(timed_pipeline):
    import copy
    module, command, events, grants, calls, models, revoked, data = timed_pipeline
    events[0].update(iCalUID='same-invite', my_response='declined')
    accepted = copy.deepcopy(events[0])
    accepted.update(id='accepted-copy', calendar_id='shared@example.invalid', my_response='accepted')
    accepted['attendees'].append({'email': 'owner@fund.example', 'responseStatus': 'accepted'})
    events.append(accepted)
    result = module.run(PACK, {'mode': 'focus', 'focus': 'my 10am'}, command)
    assert 'Which day or meeting should I prep?' in result['text']
    assert models == []
    assert calls == ['_shared/scripts/gather_google.py']



def test_receiver_focused_prep_keeps_older_intro_and_latest_reply_from_exact_thread(pipeline, context, rec, monkeypatch):
    import base64
    import gather_google as gather
    import compose_meeting_prep as composer
    module, command, events, grants, calls, models, revoked, data = pipeline
    call = setup_admission(context, rec, monkeypatch)
    receipt = call(skill_request({'mode': 'focus', 'focus': 'Dana'}, text='full prep on Dana'))
    assert receipt['status'] == 'committed'
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    selection = json.loads(job['payload']['prompt'])['selection']
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(8):
        body = ('Happy to connect you both. Discuss actual infrastructure.' if i == 0 else
                'Correction: 10 on the 14th is easiest.' if i == 7 else f'Scheduling message {i}')
        sender = 'Introducer <intro@other.example>' if i == 0 else 'Dana <dana@outside.com>'
        stamp = now-timedelta(days=16-i)
        rows.append({'id': f'm{i}', 'threadId': 'actual-thread', 'internalDate': str(int(stamp.timestamp()*1000)),
            'labelIds': ['INBOX'], 'snippet': body[:15], 'payload': {'mimeType': 'text/plain',
            'headers': [{'name': 'From', 'value': sender}, {'name': 'To', 'value': 'owner@fund.example'},
                        {'name': 'Cc', 'value': 'dana@outside.com'}, {'name': 'Date', 'value': stamp.isoformat()}],
            'body': {'data': base64.urlsafe_b64encode(body.encode()).decode()}}})
    reads = []
    class Gmail:
        def users(self): return self
        def threads(self): return self
        def get(self, **kwargs): reads.append(kwargs); return self
        def execute(self): return {'id': 'actual-thread', 'messages': rows}
        def close(self): pass
    monkeypatch.setattr(gather, '_gmail_service', Gmail)
    def search(api, args, **kwargs):
        assert 'cc:dana@outside.com' in args[2]
        return [{'id': row['id'], 'threadId': row['threadId'], 'from': 'Dana <dana@outside.com>',
                 'snippet': row['snippet']} for row in reversed(rows[3:])]
    monkeypatch.setattr(gather, '_run', search)
    def connected(script, *args, **kwargs):
        if script.endswith('gather_google.py') and '--attendee-comms' in args:
            calls.append(script)
            assert '--expand-threads' in args
            comms = gather.gather_attendee_comms('fake', str(args[1]), expand_threads=True)
            Path(args[3]).write_text(json.dumps(comms))
            return ''
        return command(script, *args, **kwargs)
    original_model = composer.call_gemini
    def model(prompt, inputs):
        assert 'Happy to connect you both' in prompt
        assert 'Introducer <intro@other.example>' in prompt
        assert 'Correction: 10 on the 14th' in prompt
        assert 'past meetings:' in prompt  # thread block did not consume the notes lane
        return original_model(prompt, inputs)
    monkeypatch.setattr(composer, 'call_gemini', model)
    result = module.run(PACK, selection, connected)
    assert result['text'] == 'Actual shared prep'
    assert reads == [{'userId': 'me', 'id': 'actual-thread', 'format': 'full'}]
    assert 'gmail' in result['sources']
    assert calls.count('_shared/scripts/research_attendees.py') == 1


def test_focused_worker_supplies_direct_conversation_to_writer_but_not_research(pipeline, monkeypatch):
    module, command, events, grants, calls, models, revoked, data = pipeline
    import source_context
    import compose_meeting_prep as composer
    request = 'Context. ' * 50 + 'Please test the profile at https://example.test and tell me what broke.'
    monkeypatch.setattr(source_context, 'read_local', lambda _: {'contacts': [], 'contact_index': [
        {'display_name': 'Dana', 'identifiers': ['dana@outside.com', '+14155551234']}], 'imessage': [
        {'handle': '+14155551234', 'text': request, 'timestamp': datetime.now(timezone.utc).isoformat()}]})
    research_context = []
    def capture(script, *args, **kwargs):
        if script.endswith('research_attendees.py'):
            research_context.extend(json.loads(Path(args[1]).read_text()))
        return command(script, *args, **kwargs)
    module.run(PACK, {'mode': 'focus', 'focus': 'Dana'}, capture)
    # Research prompts leave for third-party providers; the private text stays with the writer.
    assert research_context and all('conversation_context' not in row and request not in json.dumps(row)
                                    for row in research_context)
    assert request in composer.build_context(models[0])[0]


def test_exact_calendar_title_uses_existing_occurrence_pipeline(pipeline):
    module, command, events, grants, calls, models, revoked, data = pipeline
    events[0]['summary'] = 'Railway Board Meeting'
    result = module.run(PACK, {'mode': 'focus', 'focus': 'railway  board meeting'}, command)
    assert result['text'] == 'Actual shared prep'
    assert models[0]['exact_meeting'] == {'event_id': 'dana', 'start': events[0]['start']}
    assert [event['id'] for event in models[0]['google']['events']] == ['dana']
    assert set(models[0]['attendee_comms']) == {'dana@outside.com'}


def test_typed_occurrence_reuses_exact_pipeline_even_with_earlier_same_person(pipeline):
    module, command, events, grants, calls, models, revoked, data = pipeline
    earlier = dict(events[0], id='earlier', summary=events[0]['summary'],
                   start=(datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat())
    events.append(earlier)
    meeting = {'event_id': events[0]['id'], 'start': events[0]['start']}
    result = module.run(PACK, {'mode': 'focus', 'focus': events[0]['summary'], 'meeting': meeting}, command)
    assert result['text'] == 'Actual shared prep'
    assert models[0]['exact_meeting'] == meeting
    assert [event['id'] for event in models[0]['google']['events']] == [meeting['event_id']]


@pytest.mark.parametrize('title', ['Project review', 'Dana'])
def test_typed_occurrence_preserves_named_person_focus_with_other_attendees(pipeline, title):
    module, command, events, grants, calls, models, revoked, data = pipeline
    events[0]['summary'] = title
    events[0]['attendees'].append({'email': 'other@outside.com', 'displayName': 'Other Person'})
    meeting = {'event_id': events[0]['id'], 'start': events[0]['start']}
    result = module.run(PACK, {'mode': 'focus', 'focus': 'Dana', 'meeting': meeting}, command)
    assert result['text'] == 'Actual shared prep'
    assert models[0]['exact_meeting'] == meeting
    assert models[0]['focus'] == 'Dana'


def test_typed_occurrence_rejects_a_person_who_does_not_attend_before_research(pipeline):
    module, command, events, grants, calls, models, revoked, data = pipeline
    meeting = {'event_id': events[0]['id'], 'start': events[0]['start']}
    with pytest.raises(ValueError, match='occurrence unavailable'):
        module.run(PACK, {'mode': 'focus', 'focus': 'Sam', 'meeting': meeting}, command)
    assert not models and not any(call.endswith('research_attendees.py') for call in calls)


def test_typed_exact_title_needs_occurrence_proof_not_unrelated_calendar_completeness(pipeline):
    module, command, events, grants, calls, models, revoked, data = pipeline
    def partial(script, *args, **kwargs):
        result = command(script, *args, **kwargs)
        if '--source-results-out' in args:
            Path(args[args.index('--source-results-out') + 1]).write_text(json.dumps({'calendar': {'status': 'partial'}}))
        return result
    meeting = {'event_id': events[0]['id'], 'start': events[0]['start']}
    result = module.run(PACK, {'mode': 'focus', 'focus': events[0]['summary'], 'meeting': meeting}, partial)
    assert result['text'] == 'Actual shared prep'
    assert models[0]['exact_meeting'] == meeting


def test_actual_calendar_gather_horizon_reaches_54_hour_exact_prep(pipeline, monkeypatch):
    import gather_google
    module, command, events, grants, calls, models, revoked, data = pipeline
    target = dict(events[0], start=(datetime.now(timezone.utc) + timedelta(hours=54)).isoformat())
    raw = {**target, 'start': {'dateTime': target['start']},
           'end': {'dateTime': (datetime.fromisoformat(target['start']) + timedelta(hours=1)).isoformat()}}
    observed = []
    monkeypatch.setattr(gather_google, '_token_path', lambda: None)
    def api(_api, args, **kwargs):
        assert args[:2] == ['calendar', 'list']
        since = datetime.fromisoformat(args[args.index('--start') + 1].replace('Z', '+00:00'))
        until = datetime.fromisoformat(args[args.index('--end') + 1].replace('Z', '+00:00'))
        assert until - since == timedelta(hours=72)
        assert since < datetime.fromisoformat(target['start']) < until
        observed.append(True)
        return [raw]
    monkeypatch.setattr(gather_google, '_run', api)
    def gathered(script, *args, **kwargs):
        if script.endswith('gather_google.py') and '--cal-out' in args:
            assert '--skip-stale' in args
            observation = {}
            rows = gather_google.gather_calendar('synthetic-api', observation=observation)
            Path(args[args.index('--gmail-out') + 1]).write_text('[]')
            Path(args[args.index('--cal-out') + 1]).write_text(json.dumps(rows))
            Path(args[args.index('--source-results-out') + 1]).write_text(json.dumps({
                'calendar': {'status': 'ok', 'complete': observation['complete'], **observation['coverage']}}))
            return ''
        return command(script, *args, **kwargs)
    meeting = {'event_id': target['id'], 'start': target['start']}
    result = module.run(PACK, {'mode': 'focus', 'focus': target['summary'], 'meeting': meeting}, gathered)
    assert observed == [True] and result['text'] == 'Actual shared prep'
    assert models[0]['exact_meeting'] == meeting


@pytest.mark.parametrize('change', ['missing', 'moved', 'duplicate', 'declined', 'cancelled', 'revoked'])
def test_typed_occurrence_cannot_fall_back_or_research_after_source_change(pipeline, change):
    module, command, events, grants, calls, models, revoked, data = pipeline
    meeting = {'event_id': events[0]['id'], 'start': events[0]['start']}
    focus = events[0]['summary']
    if change == 'missing':
        events.pop(0)
    elif change == 'moved':
        events[0]['start'] = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
    elif change == 'duplicate':
        events.append(dict(events[0]))
    elif change in ('declined', 'cancelled'):
        events[0]['my_response' if change == 'declined' else 'status'] = change
    else:
        grants.discard('calendar')
    with pytest.raises((RuntimeError, ValueError)):
        module.run(PACK, {'mode': 'focus', 'focus': focus, 'meeting': meeting}, command)
    assert not models
    assert not any(call.endswith('research_attendees.py') for call in calls)


def test_duplicate_calendar_titles_do_not_select_the_first(pipeline):
    module, command, events, grants, calls, models, revoked, data = pipeline
    for event in events:
        event['summary'] = 'Board Meeting'
    result = module.run(PACK, {'mode': 'focus', 'focus': 'Board Meeting'}, command)
    assert 'Which day and time' in result['text']
    assert not models
    assert not any(call.endswith('research_attendees.py') for call in calls)


@pytest.mark.parametrize('invalid', ['declined', 'not_participating', 'past'])
def test_calendar_title_does_not_bypass_owner_meeting_rules(pipeline, invalid):
    module, command, events, grants, calls, models, revoked, data = pipeline
    events[0]['summary'] = 'Board Meeting'
    if invalid == 'declined':
        events[0]['my_response'] = 'declined'
    elif invalid == 'not_participating':
        events[0]['calendar_id'] = 'colleague@other.example'
    else:
        events[0]['start'] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    result = module.run(PACK, {'mode': 'focus', 'focus': 'Board Meeting'}, command)
    assert 'Which person or meeting' in result['text']
    assert not models


def test_title_selection_requires_complete_calendar(pipeline):
    module, command, events, grants, calls, models, revoked, data = pipeline
    def partial(script, *args, **kwargs):
        result = command(script, *args, **kwargs)
        if '--source-results-out' in args:
            Path(args[args.index('--source-results-out') + 1]).write_text(json.dumps({'calendar': {'status': 'partial'}}))
        return result
    result = module.run(PACK, {'mode': 'focus', 'focus': 'Dana meeting'}, partial)
    assert 'calendar is incomplete' in result['text']
    assert not models


def test_group_title_keeps_all_attendee_context_in_one_meeting(pipeline):
    module, command, events, grants, calls, models, revoked, data = pipeline
    events[0]['summary'] = 'Board Meeting'
    events[0]['attendees'].extend(events[1]['attendees'])
    result = module.run(PACK, {'mode': 'focus', 'focus': 'Board Meeting'}, command)
    assert result['text'] == 'Actual shared prep'
    assert len(models[0]['google']['events']) == 1
    assert set(models[0]['attendee_comms']) == {'dana@outside.com', 'sam@outside.com'}
    assert models[0]['exact_meeting']['event_id'] == 'dana'


def test_typed_provider_occurrence_resolves_to_current_scoped_gather_identity(pipeline):
    module, command, events, grants, calls, models, revoked, data = pipeline
    from gather_google import normalize_event
    original = events[0]
    events[0] = normalize_event({**original, 'calendar_id': 'primary'})
    result = module.run(PACK, {'mode': 'focus', 'focus': 'Dana',
        'meeting': {'event_id': original['id'], 'start': original['start']}}, command)
    assert result['text'] == 'Actual shared prep'
    assert models[0]['exact_meeting'] == {'event_id': events[0]['id'], 'start': original['start']}
    assert models[0]['google']['events'] == [events[0]]
    assert events[0]['provider_event_id'] == original['id']
    assert events[0]['id'].startswith('gcal_')
    staged = json.loads((data/'events'/('delivery-effects-'+'a'*32+'.json')).read_text())
    occurrences = [row for row in staged['effects'] if row['kind'] == 'meeting_prep_delivered']
    assert occurrences == [{'kind': 'meeting_prep_delivered', 'mode': 'full',
                            'calendar_event_id': events[0]['id'], 'calendar_start': original['start']}]


@pytest.mark.parametrize('case', ['two_calendars', 'missing_calendar', 'wrong_start'])
def test_provider_occurrence_mapping_refuses_ambiguous_or_unbound_rows(pipeline, case):
    module, command, events, grants, calls, models, revoked, data = pipeline
    from gather_google import normalize_event
    original = events[0]
    events[0] = normalize_event({**original, 'calendar_id': 'primary'})
    if case == 'two_calendars':
        events[1] = normalize_event({**original, 'calendar_id': 'other-calendar'})
    elif case == 'missing_calendar':
        events[0].pop('calendar_id')
    start = events[1]['start'] if case == 'wrong_start' else original['start']
    with pytest.raises(RuntimeError, match='Offered calendar occurrence unavailable'):
        module.run(PACK, {'mode': 'focus', 'focus': 'Dana',
            'meeting': {'event_id': original['id'], 'start': start}}, command)
    assert calls == ['_shared/scripts/gather_google.py']
    assert models == []


@pytest.mark.parametrize('scoped', [False, True])
def test_partial_calendar_needs_already_scoped_occurrence_identity(pipeline, scoped):
    module, command, events, grants, calls, models, revoked, data = pipeline
    from gather_google import normalize_event
    original = events[0]
    events[0] = normalize_event({**original, 'calendar_id': 'primary'})
    def partial(script, *args, **kwargs):
        result = command(script, *args, **kwargs)
        if script.endswith('gather_google.py') and '--attendee-comms' not in args:
            Path(args[5]).write_text(json.dumps({'calendar': {'status': 'partial'}}))
        return result
    selection = {'mode': 'focus', 'focus': 'Dana', 'meeting': {
        'event_id': events[0]['id'] if scoped else original['id'], 'start': original['start']}}
    if scoped:
        assert module.run(PACK, selection, partial)['text'] == 'Actual shared prep'
        assert models[0]['exact_meeting']['event_id'] == events[0]['id']
    else:
        with pytest.raises(RuntimeError, match='Offered calendar occurrence unavailable'):
            module.run(PACK, selection, partial)
        assert models == []


@pytest.mark.parametrize('calendar_id', ['owner@inside.test', 'other-calendar', 'unknown-calendar', ' bad'])
def test_exact_provider_calendar_tuple_survives_partial_other_calendars(pipeline, monkeypatch, calendar_id):
    module, command, events, grants, calls, models, revoked, data = pipeline
    from gather_google import normalize_event
    original = events[0]
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@inside.test')
    original['attendees'].append({'email': 'owner@inside.test', 'responseStatus': 'accepted'})
    events[0] = normalize_event({**original, 'calendar_id': 'owner@inside.test'})
    events[1] = normalize_event({**original, 'calendar_id': 'other-calendar'})
    def partial(script, *args, **kwargs):
        result = command(script, *args, **kwargs)
        if script.endswith('gather_google.py') and '--attendee-comms' not in args:
            Path(args[5]).write_text(json.dumps({'calendar': {'status': 'partial'}}))
        return result
    selection = {'mode': 'focus', 'focus': 'Dana', 'meeting': {
        'event_id': original['id'], 'start': original['start'], 'calendar_id': calendar_id}}
    if calendar_id in ('owner@inside.test', 'other-calendar'):
        result = module.run(PACK, selection, partial)
        expected = next(e for e in events if e['calendar_id'] == calendar_id)
        assert result['text'] == 'Actual shared prep'
        assert models[0]['google']['events'] == [expected]
        assert models[0]['exact_meeting'] == {'event_id': expected['id'], 'start': original['start']}
    else:
        with pytest.raises((RuntimeError, ValueError)):
            module.run(PACK, selection, partial)
        assert models == []


def test_scoped_occurrence_with_contradictory_calendar_refuses(pipeline):
    module, command, events, grants, calls, models, revoked, data = pipeline
    from gather_google import normalize_event
    events[0] = normalize_event({**events[0], 'calendar_id': 'primary'})
    with pytest.raises(RuntimeError, match='Offered calendar occurrence unavailable'):
        module.run(PACK, {'mode': 'focus', 'focus': 'Dana', 'meeting': {
            'event_id': events[0]['id'], 'start': events[0]['start'], 'calendar_id': 'other-calendar'}}, command)
    assert models == []


def test_exact_composition_research_is_staged_with_provider_metadata(pipeline):
    module, command, events, grants, calls, models, revoked, data = pipeline
    import hashlib
    import research_attendees as ra
    record = {'provider': 'gemini', 'stage': 'focus', 'subjects': ['dana@outside.com'],
              'response_json': '{"company":"Different"}', 'response_sha256': 'c' * 64,
              'grounding_json': '{"groundingChunks":[{"web":{"uri":"https://different.test"}}]}',
              'truncated': False}
    def boundary(script, *args, **kwargs):
        result = command(script, *args, **kwargs)
        if script.endswith('research_attendees.py'):
            result['research_evidence'] = ra.prep_research_evidence(result, [record])
            Path(args[args.index('--out') + 1]).write_text(json.dumps(result))
        return result
    result = module.run(PACK, {'mode': 'focus', 'focus': 'Dana'}, boundary)
    actual = models[0]['attendee_research']
    used = {key: value for key, value in actual.items() if key != 'research_evidence'}
    assert result['research_evidence']['input_sha256'] == hashlib.sha256(ra._evidence_json(used).encode()).hexdigest()
    staged = json.loads((data / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert staged['research_evidence'] == result['research_evidence']
    assert staged['research_evidence']['records'] == [record]
