"""Actual canonical knowledge CLI through a bounded, non-mutating receiver operation."""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import runtime_knowledge_read as read
from test_runtime_owner_loop_close import context

__all__ = ['context']

PACK = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'
SCRIPT = PACK / '_shared/knowledge/knowledge_query.py'
REVISION = 'a'*64


@pytest.fixture
def graph(tmp_path,monkeypatch):
    for part in ('_shared/knowledge','_shared/lib'):
        monkeypatch.syspath_prepend(str(PACK/part))
    import knowledge as kg
    monkeypatch.setenv('SOTTO_DATA',str(tmp_path))
    person=kg.PersonFile(canonical_id='dana',name='Dana',company='Cedar',identifiers=['dana@cedar.com'],
        facts={'old-clinic':kg.FactMeta(text='Discussed clinic procurement in the older meeting.',
            source='gmail',source_ref='old-thread',conf=0.95,first='2020-01-01',last='2020-01-01')})
    people=tmp_path/'knowledge/people';people.mkdir(parents=True)
    path=people/'legacy-dana.md';path.write_text(kg.serialize_person_file(person))
    company=tmp_path/'knowledge/companies';company.mkdir()
    (company/'cedar.md').write_text('---\nname: Cedar\n---\n\n## About\nBuilds clinic tools.\n')
    state=tmp_path/'config/source-state.json';state.parent.mkdir()
    state.write_text(json.dumps({'sources':{'imessage':{'status':'disabled'},'contacts':{'status':'disabled'}}}))
    return tmp_path,path


def body(argv=None):
    return {'operation':'knowledge_query','arguments':{'argv':argv or []},'source_revision':REVISION}


def call(root,argv=None,authority=None):
    return read.perform(body(argv),script=str(SCRIPT),executable=sys.executable,
                        env={**os.environ,'SOTTO_DATA':str(root)},
                        authority=authority or (lambda:{'complete':True,'revision':REVISION}))


def tree(root):
    return {str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file() and not path.name.endswith('.lock')}


def test_person_history_uses_captured_grants_not_inherited_environment(tmp_path):
    script = tmp_path / 'captured.py'
    script.write_text("import json,os; print(json.dumps({'sources':json.loads(os.environ['SOTTO_CAPTURED_SOURCES'])}))")
    current = {'complete': True, 'revision': REVISION,
               'bridge_sources': ['imessage'], 'google_sources': ['gmail']}
    def invoke(authority):
        return read.perform(body(['--person', 'Dana', '--person-coverage']),
                            script=str(script), executable=sys.executable,
                            env={**os.environ, 'SOTTO_CAPTURED_SOURCES': '["contacts","granola"]'},
                            authority=authority)
    assert invoke(lambda: current) == {'sources': ['gmail', 'imessage']}
    assert invoke(lambda: {'complete': True, 'revision': REVISION}) == {'sources': []}
    snapshots = iter([current, {**current, 'revision': 'b' * 64, 'google_sources': []}])
    with pytest.raises(read.UnavailableError):
        invoke(lambda: next(snapshots))


@pytest.mark.parametrize('google_sources, bridge_sources, expected', [
    (['gmail', 'google_contacts'], ['imessage'], {'pricing', 'checklist'}),
    (['gmail', 'google_contacts'], ['imessage', 'contacts'], {'pricing', 'checklist', 'bridge-contacts'}),
    (['google_contacts'], ['imessage'], set()),
    (['gmail', 'google_contacts'], [], {'checklist'}),
])
def test_person_obligation_history_projects_real_google_grants_without_contacts_alias(graph, google_sources, bridge_sources, expected):
    import importlib.util
    root, _ = graph
    spec = importlib.util.spec_from_file_location('history_grants_resolver', PACK/'morning-brief/scripts/continuity_resolve.py')
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    for anchor in ('pricing', 'checklist', 'bridge-contacts'):
        row = {'anchor_key':anchor, 'status':'open', 'action_type':'follow_up',
               'contact_name':'Dana', 'contact_identifier':'dana@cedar.com', 'canonical_id':'dana',
               'summary':anchor, 'ask':anchor, 'created_at':'2026-10-18',
               'source':'gmail', 'channel':'gmail', 'source_thread_id':'promise-thread',
               'source_refs':[{'sourceType':'gmail', 'sourceId':'promise-mail'}]}
        if anchor == 'pricing':
            row.update(status='resolved', resolution='replied', resolution_evidence='Sent the pricing document by text.',
                       resolution_source_refs=[{'sourceType':'imessage', 'sourceId':'pricing-text'}])
        elif anchor == 'bridge-contacts':
            row.update(source='contacts', channel='contacts', source_thread_id=None,
                       source_refs=[{'sourceType':'contacts', 'sourceId':'contacts-row'}])
        resolver._persist(row)
    before = tree(root)
    result = call(root, ['--person', 'Dana', '--person-coverage'],
                  lambda:{'complete':True, 'revision':REVISION,
                          'google_sources':google_sources, 'bridge_sources':bridge_sources})
    history = result['obligation_history']
    assert history['status'] == ('matched' if expected else 'no_match')
    assert {item['anchor_key'] for item in history['items']} == expected
    assert tree(root) == before


def test_actual_person_old_topic_editable_recent_company_read_preserves_canonical_files(graph):
    root,path=graph
    before=tree(root)
    packed=call(root,['--person','Dana','--topic','clinic procurement'])
    assert 'clinic procurement' in packed['dana']
    editable=call(root,['--person','Dana','--editable-person'])
    assert 'old-clinic' in str(editable)
    recent=call(root,['--relevant-days','7'])
    assert 'dana' in recent['person_knowledge'] and 'Builds clinic tools.' in str(recent['company_knowledge'])
    assert tree(root)==before and path.exists()
    assert not (root/'events').exists()


def test_interactive_read_bounds_thousands_of_contacts_and_keeps_selected_index(graph):
    import knowledge as kg
    root, _ = graph
    people = root/'knowledge/people'
    for n in range(1200):
        person = kg.PersonFile(canonical_id=f'other-{n:04d}', name=f'Other {n}',
                               identifiers=[f'other{n}@example.test'])
        (people/f'other-{n:04d}.md').write_text(kg.serialize_person_file(person))
    result = call(root)
    assert 'dana' in result['person_knowledge']
    assert {entry['canonical_id'] for entry in result['contact_index']} <= set(result['person_knowledge'])
    assert result['omitted']['contacts'] > 0
    assert len(json.dumps(result).encode()) <= 48 * 1024


def test_interactive_read_bounds_unicode_and_editable_assertions(graph):
    import knowledge as kg
    root, _ = graph
    people = root/'knowledge/people'
    for n in range(12):
        person = kg.PersonFile(canonical_id=f'unicode-{n:02d}', name=f'Unicode {n}',
            identifiers=[f'unicode{n}@example.test'],
            facts={'f': kg.FactMeta(text='条' * 2200 + ' only if approved.', source='gmail',
                                    source_ref=f'ref-{n}', conf=.95)})
        (people/f'unicode-{n:02d}.md').write_text(kg.serialize_person_file(person))
    result = call(root)
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 48 * 1024
    assert len(json.dumps(result).encode()) <= 48 * 1024
    assert result['omitted']['assertions'] > 0

    dana = kg.PersonFile(canonical_id='dana', name='Dana', identifiers=['dana@cedar.com'],
        facts={f'f-{n}': kg.FactMeta(text='条' * 4000 + ' only if approved.', source='user_edit')
               for n in range(12)})
    (people/'legacy-dana.md').write_text(kg.serialize_person_file(dana))
    editable = call(root, ['--person', 'Dana', '--editable-person'])
    assert len(json.dumps(editable).encode()) <= 48 * 1024
    assert editable['omitted']['assertions'] > 0
    assert all(fact['text'].endswith('only if approved.') for fact in editable['dana']['facts'])


@pytest.mark.parametrize('days', ['7', '30'])
def test_interactive_relevance_precedes_person_cap(graph, days):
    import knowledge as kg
    root, _ = graph
    people = root/'knowledge/people'
    for n in range(80):
        person = kg.PersonFile(canonical_id=f'a-{n:02d}', name=f'Other {n}',
            identifiers=[f'other{n}@example.test'],
            facts={'f': kg.FactMeta(text='Discussed budgets.', source='gmail', source_ref=f'ref-{n}')})
        (people/f'a-{n:02d}.md').write_text(kg.serialize_person_file(person))
    relevant = kg.PersonFile(canonical_id='z-relevant', name='Relevant',
        identifiers=['relevant@example.test'],
        facts={'f': kg.FactMeta(text='Pediatric clinic procurement needs review.',
                                source='gmail', source_ref='relevant-ref')})
    (people/'z-relevant.md').write_text(kg.serialize_person_file(relevant))
    result = call(root, ['--topic', 'clinic procurement', '--relevant-days', days])
    assert 'z-relevant' in result['person_knowledge']
    assert 'procurement needs review' in result['person_knowledge']['z-relevant']
    assert result['omitted']['people'] >= 1


def test_interactive_coverage_and_editable_share_budget(graph):
    import knowledge as kg
    root, _ = graph
    person = kg.PersonFile(canonical_id='dana', name='Dana', identifiers=['dana@cedar.com'],
        facts={f'f-{n}': kg.FactMeta(text='条' * 4000 + ' only if approved.', source='user_edit')
               for n in range(12)})
    (root/'knowledge/people/legacy-dana.md').write_text(kg.serialize_person_file(person))
    result = call(root, ['--person', 'Dana', '--person-coverage', '--editable-person'])
    assert result['coverage']['graph'] == 'found'
    assert result['person_knowledge']['dana']['canonical_id'] == 'dana'
    assert result['omitted']['assertions'] > 0
    assert len(json.dumps(result).encode()) <= 48 * 1024


def test_interactive_editable_reports_selector_omissions_in_both_shapes(graph):
    import knowledge as kg
    root, _ = graph
    person = kg.PersonFile(canonical_id='dana', name='Dana', identifiers=['dana@cedar.com'],
        facts={f'f-{n:02d}': kg.FactMeta(text=f'Short owner correction {n}.', source='user_edit')
               for n in range(20)})
    (root/'knowledge/people/legacy-dana.md').write_text(kg.serialize_person_file(person))
    for extra in ([], ['--person-coverage']):
        result = call(root, ['--person', 'Dana', '--editable-person', *extra])
        editable = result['person_knowledge']['dana'] if extra else result['dana']
        assert len(editable['facts']) == kg.MAX_FACTS_FOR_LLM
        assert result['omitted']['assertions'] == 20 - kg.MAX_FACTS_FOR_LLM
        assert result['suggestion']
        assert len(json.dumps(result).encode()) <= 48 * 1024


def test_actual_no_match_coverage_uses_existing_permission_receipts_without_live_reads(graph):
    root,_=graph
    before=tree(root)
    result=call(root,['--person','Unknown','--person-coverage'])
    assert result['person_knowledge']=={} and result['coverage']['graph']=='no_match'
    assert result['coverage']['identity_resolution']['next_action']=='ask_owner_for_identifier'
    sources={row['source']:row for row in result['coverage']['historical_sources']}
    assert sources['imessage']['status']=='disabled'
    assert tree(root)==before


def test_pending_graph_journal_refuses_without_replay_or_migration(graph):
    root,_=graph
    journal=root/'knowledge/.journal.json'
    journal.write_text(json.dumps({'ops':[{'op':'unsafe-pending-write'}]}))
    before=tree(root)
    with pytest.raises(read.UnavailableError):
        call(root,['--person','Dana'])
    assert tree(root)==before and journal.exists()


def test_normal_cli_still_runs_original_maintenance(graph):
    import subprocess
    root,path=graph
    result=subprocess.run([sys.executable,str(SCRIPT),'--person','Dana'],
        env={key:value for key,value in {**os.environ,'SOTTO_DATA':str(root)}.items() if key!='SOTTO_READ_ONLY'},
        capture_output=True,text=True,timeout=10)
    assert result.returncode==0
    assert not path.exists() and len(list((root/'knowledge/people').glob('*.md'))) == 1


@pytest.mark.parametrize('argv',[
    ['--person','Dana','--person','Sam'],['--calendar','/tmp/cal.json'],['--gmail','/tmp/x'],
    ['--loops','/tmp/x'],['--local','/tmp/x'],['--person-coverage'],['--editable-person'],
    ['--relevant-days','0'],['--relevant-days','366'],['--relevant-days','01'],
    ['--person','Dana\n'],['--topic','x'*501],['--person','x'*201],['--person'],
])
def test_closed_flags_refuse_before_any_subprocess(monkeypatch,argv):
    monkeypatch.setattr(read.subprocess,'run',lambda *a,**k:pytest.fail('invalid flags cannot execute'))
    with pytest.raises(ValueError):
        read.perform(body(argv),script=str(SCRIPT),executable=sys.executable,env={},authority=lambda:{})


def test_current_authority_required_before_contact_and_after_actual_read(graph,monkeypatch):
    root,_=graph
    counter=[0]
    def authority():
        counter[0]+=1
        return {'complete':True,'revision':REVISION if counter[0]==1 else 'b'*64}
    with pytest.raises(read.UnavailableError):
        call(root,['--person','Dana'],authority)
    assert counter[0]==2
    monkeypatch.setattr(read.subprocess,'run',lambda *a,**k:pytest.fail('no contact without authority'))
    with pytest.raises(read.UnavailableError):
        call(root,['--person','Dana'],lambda:{'complete':False,'revision':REVISION})


@pytest.mark.parametrize('stdout',[b'[]',b'{"value":NaN}',b'x'*65537,b'not JSON'])
def test_output_shape_and_size_refuse(monkeypatch,stdout):
    monkeypatch.setattr(read.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=stdout))
    with pytest.raises(read.UnavailableError):
        read.perform(body(),script=str(SCRIPT),executable=sys.executable,env={},
                     authority=lambda:{'complete':True,'revision':REVISION})


def master_call(root, revision=None, authority=None):
    return read.perform({'operation':'master_file','arguments':{'argv':['get']},'source_revision':revision},
        script=str(PACK/'_shared/knowledge/master_file.py'), executable=sys.executable,
        env={**os.environ,'SOTTO_DATA':str(root)}, authority=authority or (lambda:{'complete':False}))


def test_original_master_reader_missing_and_confirmed_rule_no_read_writes(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    before = tree(tmp_path)
    result = master_call(tmp_path)
    assert {k:v for k,v in result.items() if k != 'prompt'} == {'text':'','chars':0,'cap':8000}
    assert result['prompt'].startswith('Current local date and time: ')
    assert tree(tmp_path) == before
    monkeypatch.syspath_prepend(str(PACK/'_shared/knowledge'))
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    import master_file
    master_file.append_section('Procedures', 'Never book Fridays.\nUse mornings instead.')
    before = tree(tmp_path)
    result = master_call(tmp_path, authority=lambda:pytest.fail('owner-only read probed source authority'))
    assert {k:v for k,v in result.items() if k != 'prompt'} == {'text':master_file.read(),'chars':len(master_file.read()),'cap':8000}
    assert result['prompt'].startswith('Current local date and time: ')
    assert result['prompt'].endswith(master_file.render_for_prompt())
    assert 'Never book Fridays.' in result['text']
    assert tree(tmp_path) == before
    assert set(result) == {'text','chars','cap','prompt'}


@pytest.mark.parametrize('argv', [[], ['sections'], ['append'], ['get','--section','Procedures'], ['get','/tmp/x']])
def test_master_closed_get_only_no_paths_or_write_flags(monkeypatch, argv):
    monkeypatch.setattr(read.subprocess, 'run', lambda *a, **k: pytest.fail('invalid master call cannot execute'))
    with pytest.raises(ValueError):
        read.perform({'operation':'master_file','arguments':{'argv':argv},'source_revision':None},
                     script='unused',executable=sys.executable,env={},authority=lambda:{})


def test_master_owner_only_read_never_probes_source_authority(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    def unavailable():
        pytest.fail('owner-authored master read must not depend on connector authority')
    result = master_call(tmp_path, authority=unavailable)
    assert result['text'] == '' and result['chars'] == 0 and result['cap'] == 8000
    assert result['prompt'].startswith('Current local date and time: ')
    assert not list(tmp_path.iterdir())


def test_master_get_requires_owner_only_body_without_source_revision():
    with pytest.raises(ValueError):
        read.validate({'operation':'master_file','arguments':{'argv':['get']},'source_revision':REVISION})


@pytest.mark.parametrize('value',[{'text':'x','chars':0,'cap':8000}, {'text':'','chars':0,'cap':7},
                                  {'text':'','chars':0,'cap':8000,'path':'private'}])
def test_master_original_output_contract_refuses_invalid_shape(monkeypatch,value):
    monkeypatch.setattr(read.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps(value).encode()))
    with pytest.raises(read.UnavailableError):
        master_call(Path('/unused'))


def test_master_reads_original_confirmed_offer_effect(context, monkeypatch):
    from test_runtime_owner_preference_tool import procedure_offer, call as preference_call
    submitted = procedure_offer(context, monkeypatch, 'Never book Fridays.\nUse mornings instead.')
    receipt = preference_call(submitted, context)
    assert receipt['status'] == 'committed'
    before = tree(context[0])
    result = master_call(context[0])
    assert receipt['outcome']['rule'] in result['text']
    assert receipt['outcome']['rule'] in result['prompt']
    assert 'Procedures section is standing instructions' in result['prompt']
    assert tree(context[0]) == before


def loops_call(root, argv=None, authority=None):
    return read.perform({'operation':'loops_query','arguments':{'argv':argv or []},'source_revision':REVISION},
        script=str(PACK/'_shared/scripts/loops_query.py'),executable=sys.executable,
        env={**os.environ,'SOTTO_DATA':str(root),'SOTTO_TIMEZONE':'+00:00'},
        authority=authority or (lambda:{'complete':True,'revision':REVISION,'bridge_sources':[], 'google_sources':['gmail']}))


def test_actual_source_loops_full_union_projection_and_no_read_writes(tmp_path, monkeypatch):
    import importlib.util
    monkeypatch.setenv('SOTTO_DATA',str(tmp_path))
    spec = importlib.util.spec_from_file_location('loops_reader_real_resolver',PACK/'morning-brief/scripts/continuity_resolve.py')
    cr = importlib.util.module_from_spec(spec); spec.loader.exec_module(cr)
    def row(anchor, **values):
        item = {'anchor_key':anchor,'status':'open','summary':anchor,'contact_name':anchor,
                'source':'gmail','channel':'email','source_thread_id':'thread-'+anchor,
                'source_message_id':'message-'+anchor,'source_refs':[{'sourceType':'gmail','sourceId':'message-'+anchor}],
                'action_type':'reply','created_at':'2020-01-01', **values}
        cr._persist(item)
        return item
    row('overdue',deadline='2020-02-01')
    row('waiting',action_type='waiting_on',chased_count=1,last_chased_at='2020-02-02')
    row('snoozed',snoozed_until='2099-01-01')
    row('parked',status='parked')
    row('resolved',status='resolved')
    row('granola-withheld',source='followup_commitment',source_thread_id='commitment:meeting',source_message_id=None,
        source_refs=[{'sourceType':'granola','sourceId':'meeting'}])
    row('mixed-denied',resolution_source_refs=[{'sourceType':'imessage','sourceId':'reply'}])
    row('ambiguous-mail',source='apple_mail')
    row('stale-proof',content_provenance={'version':1,'kind':'sources','sources':['gmail'],'digest':'0'*64})
    before = tree(tmp_path)
    result = loops_call(tmp_path,['--parked'])
    assert result['coverage'] == 'currently_permitted_canonical_loops'
    assert [item['what'] for item in result['you_owe']] == ['overdue']
    assert [item['what'] for item in result['waiting_on_them']] == ['waiting']
    assert result['waiting_on_them'][0]['chased_count'] == 1
    assert [item['what'] for item in result['parked']] == ['parked']
    assert result['counts'] == {'you_owe':1,'waiting_on_them':1,'parked':1}
    assert tree(tmp_path) == before
    allowed = loops_call(tmp_path,authority=lambda:{'complete':True,'revision':REVISION,
        'bridge_sources':['imessage'],'google_sources':['gmail']})
    assert {item['what'] for item in allowed['you_owe']} == {'overdue','mixed-denied'}
    denied = loops_call(tmp_path,authority=lambda:{'complete':True,'revision':REVISION,
        'bridge_sources':[],'google_sources':[]})
    assert denied['counts'] == {'you_owe':0,'waiting_on_them':0}
    assert tree(tmp_path) == before
    connector = loops_call(tmp_path,authority=lambda:{'complete':True,'revision':REVISION,
        'bridge_sources':[],'google_sources':['gmail'],'connector_sources':['granola']})
    assert {item['what'] for item in connector['you_owe']} == {'overdue','granola-withheld'}
    assert tree(tmp_path) == before


def test_loops_empty_read_does_not_create_files(tmp_path):
    assert loops_call(tmp_path)['counts'] == {'you_owe':0,'waiting_on_them':0}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('argv',[['--person','Dana'],['--parked','--parked'],['--input','/tmp/x'],['/tmp/x']])
def test_loops_closed_arguments_refuse_before_contact(monkeypatch,argv):
    monkeypatch.setattr(read.subprocess,'run',lambda *a,**k:pytest.fail('invalid loops argv cannot run'))
    with pytest.raises(ValueError):
        loops_call(Path('/unused'),argv)


def test_loops_incomplete_or_changed_authority_refuses(tmp_path,monkeypatch):
    revisions = iter([{'complete':True,'revision':REVISION,'google_sources':['gmail']},
                      {'complete':True,'revision':'b'*64,'google_sources':[]}])
    with pytest.raises(read.UnavailableError):
        loops_call(tmp_path,authority=lambda:next(revisions))
    monkeypatch.setattr(read.subprocess,'run',lambda *a,**k:pytest.fail('incomplete authority cannot read'))
    with pytest.raises(read.UnavailableError):
        loops_call(tmp_path,authority=lambda:{'complete':False,'revision':REVISION})


def test_loops_owner_projection_remains_separate(context,monkeypatch):
    from datetime import datetime, timezone
    from runtime_owner_loop import _load_writer
    writer = _load_writer()
    writer.op_loop_add_admitted_owner_text('Send my deck',owner_text_span='Send my deck',
        owner_event_id='owner-loop-fixture',now=datetime.now(timezone.utc))
    import owner_continuity
    original = owner_continuity.read_owner_continuity(str(context[0]),now=datetime.now(timezone.utc))
    assert original['coverage'] == 'owner_authored_only'
    before = tree(context[0])
    result = loops_call(context[0],authority=lambda:{'complete':True,'revision':REVISION,
        'bridge_sources':[],'google_sources':[]})
    assert [item['what'] for item in result['you_owe']] == ['Send my deck']
    assert tree(context[0]) == before
    assert owner_continuity.read_owner_continuity(str(context[0]),now=datetime.now(timezone.utc)) == original


def test_granola_mixed_obligation_requires_every_captured_grant(tmp_path):
    import yaml
    folder=tmp_path/'knowledge/continuity';folder.mkdir(parents=True)
    row={'anchor_key':'mixed-granola','status':'open','summary':'Follow up on meeting',
        'source':'followup_commitment','source_thread_id':'commitment:meeting',
        'source_refs':[{'sourceType':'granola','sourceId':'meeting'},{'sourceType':'gmail','sourceId':'message'}],
        'created_at':'2020-01-01','action_type':'follow_up'}
    (folder/'meeting.md').write_text('---\n'+yaml.safe_dump(row)+'---\n')
    before=tree(tmp_path)
    for google,connectors,expected in [([],['granola'],0),(['gmail'],[],0),(['gmail'],['granola'],1)]:
        result=loops_call(tmp_path,authority=lambda:{'complete':True,'revision':REVISION,
            'bridge_sources':[],'google_sources':google,'connector_sources':connectors})
        assert result['counts']['you_owe']==expected
    assert tree(tmp_path)==before


def importance_call(root, argv=None, authority=None):
    return read.perform({'operation': 'relationship_importance', 'arguments': {'argv': argv or []},
                         'source_revision': REVISION},
        script=str(PACK / '_shared/lib/relationship_importance.py'), executable=sys.executable,
        env={**os.environ, 'SOTTO_DATA': str(root)},
        authority=authority or (lambda: {'complete': True, 'revision': REVISION}))


def test_original_relationship_importance_vip_decay_ambiguity_and_no_writes(tmp_path):
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=n)).isoformat() for n in (1, 8, 15, 22, 29, 36)]
    evidence = {'active_days': days, 'sent_days': days, 'received_days': days}
    old = {'active_days': ['2020-01-01'], 'sent_days': ['2020-01-01'], 'received_days': ['2020-01-01']}
    history = {'dana': {'name': 'Dana', 'importance_evidence': evidence},
               'old': {'name': 'Old', 'importance_evidence': old},
               'sam1': {'name': 'Sam', 'importance_evidence': evidence},
               'sam2': {'name': 'Sam', 'importance_evidence': old}}
    (tmp_path / 'knowledge').mkdir()
    (tmp_path / 'knowledge/relationship_state.json').write_text(json.dumps({'history': history}))
    (tmp_path / 'preferences.json').write_text(json.dumps({'explicit': {'vip_people': ['Chosen']}}))
    before = tree(tmp_path)
    ranked = {person['name']: person for person in importance_call(tmp_path)['people']}
    assert set(ranked) == {'Dana', 'Chosen'}
    assert ranked['Dana']['active_days'] == 6 and ranked['Dana']['tier'] == 'vip'
    assert ranked['Chosen']['reason'] == 'you marked this person VIP'
    assert importance_call(tmp_path, ['--person', 'Old'])['people'][0]['active_days'] == 0
    ambiguous = importance_call(tmp_path, ['--person', 'Sam'])['people'][0]
    assert ambiguous['tier'] == 'regular' and ambiguous['active_days'] == 0
    assert tree(tmp_path) == before


@pytest.mark.parametrize('argv', [['--person'], ['--person', ''], ['--person', ' Dana'],
    ['--person', 'Dana\n'], ['--person', 'x'*201], ['--person', '\ud800'],
    ['--person', 'Dana', '--person', 'Sam'], ['--file', '/tmp/private'], ['--parked']])
def test_relationship_importance_closed_grammar(monkeypatch, tmp_path, argv):
    monkeypatch.setattr(read.subprocess, 'run', lambda *a, **k: pytest.fail('invalid request executes'))
    with pytest.raises(ValueError):
        importance_call(tmp_path, argv)


def test_relationship_importance_authority_bookends(tmp_path, monkeypatch):
    snapshots = iter([{'complete': True, 'revision': REVISION},
                      {'complete': True, 'revision': 'b'*64}])
    with pytest.raises(read.UnavailableError):
        importance_call(tmp_path, authority=lambda: next(snapshots))
    assert tree(tmp_path) == {}
    monkeypatch.setattr(read.subprocess, 'run', lambda *a, **k: pytest.fail('incomplete authority executes'))
    with pytest.raises(read.UnavailableError):
        importance_call(tmp_path, authority=lambda: {'complete': False, 'revision': REVISION})


def triage_body(argv=None):
    return {'operation': 'triage_queue', 'arguments': {'argv': [] if argv is None else argv},
            'source_revision': REVISION}


def test_triage_closed_catalog_capture_and_authority_bookends(tmp_path, monkeypatch):
    script = tmp_path / 'fixture.py'
    script.write_text("import json,os; print(json.dumps({'captured':json.loads(os.environ['SOTTO_CAPTURED_SOURCES'])}))")
    root = tmp_path / 'data'
    root.mkdir()
    def invoke(authority):
        return read.perform(triage_body(), script=str(script), executable=sys.executable,
            env={**os.environ, 'SOTTO_DATA': str(root)}, authority=authority)
    authority = {'complete': True, 'revision': REVISION,
                 'bridge_sources': ['contacts', 'imessage'], 'google_sources': ['gmail']}
    assert invoke(lambda: authority) == {'captured': ['contacts', 'imessage', 'gmail']}
    assert tree(root) == {}
    snapshots = iter([authority, {**authority, 'revision': 'b'*64}])
    with pytest.raises(read.UnavailableError):
        invoke(lambda: next(snapshots))
    monkeypatch.setattr(read.subprocess, 'Popen', lambda *a, **k: pytest.fail('invalid authority executes'))
    with pytest.raises(read.UnavailableError):
        invoke(lambda: {**authority, 'complete': False})
    for argv in (['--local', '/private'], ['--max', '10'], ['--days', '3']):
        with pytest.raises(ValueError):
            read.validate(triage_body(argv))


def test_triage_overflow_withholds_whole_result(tmp_path):
    script = tmp_path / 'large.py'
    script.write_text("import json; print(json.dumps({'email':['x'*70000]}))")
    with pytest.raises(read.UnavailableError):
        read.perform(triage_body(), script=str(script), executable=sys.executable,
            env=os.environ, authority=lambda: {'complete': True, 'revision': REVISION})


def test_triage_disconnect_kills_and_waits_child_process_group(tmp_path, monkeypatch):
    script = tmp_path / 'wait.py'
    script.write_text('import time; time.sleep(60)')
    launched = []
    original = read.subprocess.Popen
    def launch(*args, **kwargs):
        assert kwargs['start_new_session'] is True
        child = original(*args, **kwargs)
        launched.append(child)
        return child
    monkeypatch.setattr(read.subprocess, 'Popen', launch)
    with pytest.raises(read.UnavailableError):
        read.perform(triage_body(), script=str(script), executable=sys.executable,
            env=os.environ, authority=lambda: {'complete': True, 'revision': REVISION},
            disconnected=lambda: bool(launched))
    assert len(launched) == 1 and launched[0].poll() is not None
    with pytest.raises(read.UnavailableError):
        read.perform(triage_body(), script=str(script), executable=sys.executable,
            env=os.environ, authority=lambda: {'complete': True, 'revision': REVISION},
            disconnected=lambda: True)
    assert len(launched) == 1


def attention_call(root, argv=None, authority=None):
    return read.perform({'operation':'relationship_attention','arguments':{'argv':argv or []},
        'source_revision':REVISION},script=str(PACK/'_shared/lib/relationship_importance.py'),
        executable=sys.executable,env={**os.environ,'SOTTO_DATA':str(root)},
        authority=authority or (lambda:{'complete':True,'revision':REVISION}))


def test_saved_attention_original_order_projection_and_read_only(tmp_path):
    (tmp_path/'knowledge').mkdir()
    queue=[{'display_name':'Dana','queue_type':'waiting_on_you','reason':'They asked last','graph_context':'private'},
           {'display_name':'Maya','queue_type':'losing_touch','reason':'Spoke less often'},
           {'display_name':'Lee','queue_type':'lapsed','reason':'Old saved reason'}]
    (tmp_path/'knowledge/relationship_state.json').write_text(json.dumps({'attention_queue':queue,'history':{'private':'omitted'}}))
    before=tree(tmp_path)
    assert attention_call(tmp_path)=={'status':'available','freshness':'unknown','attention_queue':[
        {k:r[k] for k in ('display_name','queue_type','reason')} for r in queue],'truncated':False}
    assert tree(tmp_path)==before


@pytest.mark.parametrize('state',[None,{}, {'attention_queue':[]}])
def test_attention_missing_is_distinct_from_saved_empty(tmp_path,state):
    if state is not None:
        (tmp_path/'knowledge').mkdir()
        (tmp_path/'knowledge/relationship_state.json').write_text(json.dumps(state))
    before=tree(tmp_path)
    assert attention_call(tmp_path)=={'status':'available' if state=={'attention_queue':[]} else 'missing',
        'freshness':'unknown','attention_queue':[],'truncated':False}
    assert tree(tmp_path)==before


@pytest.mark.parametrize('content',['{', '[]', '{"attention_queue":null}', 'x'*(4*1024*1024+1)],ids=['json','root','queue','oversize'])
def test_attention_malformed_or_oversized_is_unavailable(tmp_path,content):
    (tmp_path/'knowledge').mkdir()
    (tmp_path/'knowledge/relationship_state.json').write_text(content)
    before=tree(tmp_path)
    with pytest.raises(read.UnavailableError):
        attention_call(tmp_path)
    assert tree(tmp_path)==before


def test_attention_omits_invalid_rows_and_bounds_without_reordering(tmp_path):
    (tmp_path/'knowledge').mkdir()
    queue=[{'display_name':str(n),'queue_type':'waiting_on_you','reason':'saved'} for n in range(14)]
    queue.insert(0,{'display_name':'oversize','queue_type':'lapsed','reason':'x'*1001})
    queue.insert(1,{'display_name':'private','queue_type':'unknown','reason':'not accepted'})
    (tmp_path/'knowledge/relationship_state.json').write_text(json.dumps({'attention_queue':queue}))
    result=attention_call(tmp_path)
    assert result['truncated'] is True
    assert [r['display_name'] for r in result['attention_queue']]==[str(n) for n in range(12)]


def test_attention_closed_grammar_and_current_authority(tmp_path,monkeypatch):
    snapshots=iter([{'complete':True,'revision':REVISION},{'complete':True,'revision':'b'*64}])
    with pytest.raises(read.UnavailableError):
        attention_call(tmp_path,authority=lambda:next(snapshots))
    monkeypatch.setattr(read.subprocess,'run',lambda *a,**k:pytest.fail('invalid read executes'))
    with pytest.raises(read.UnavailableError):
        attention_call(tmp_path,authority=lambda:{'complete':False,'revision':REVISION})
    for argv in (['--person','Dana'],['--attention'],['--file','/tmp/private']):
        with pytest.raises(ValueError):
            attention_call(tmp_path,argv)


def test_saved_company_sole_selector_and_read_only_preservation(graph):
    root, _ = graph
    company = root / 'knowledge/companies/cedar.md'
    company.write_text('---\nnormalized: Cedar Labs\naliases: [Cedar, Cedar Labs]\ndomain: cedar.example\n---\n\n## About\nBuilds clinic tools.\n\n## News\n- New clinic launch.\n')
    before = tree(root)
    for selector in ('Cedar', 'Cedar Labs', 'cedar.example'):
        assert call(root, ['--company', selector]) == {
            'status': 'saved', 'company_knowledge': {'cedar': 'Cedar — Builds clinic tools.\n- New clinic launch.'},
            'company_identity': {'slug': 'cedar', 'name': 'Cedar', 'domain': 'cedar.example', 'last_researched': ''},
            'truncated': False}
    assert call(root, ['--company', 'Unknown']) == {'status': 'missing', 'company_knowledge': {}, 'truncated': False}
    assert tree(root) == before


def test_company_collision_unreadable_and_packing_disclosure(graph):
    root, _ = graph
    companies = root / 'knowledge/companies'
    (companies / 'cedar.md').write_text('---\naliases: [Cedar]\ndomain: cedar.example\n---\n## About\n' + 'x' * 700 + '\n## Context\nSaved context\n')
    result = call(root, ['--company', 'Cedar'])
    assert result['status'] == 'saved' and result['truncated'] is True
    assert len(result['company_knowledge']['cedar']) == 600
    (companies / 'other.md').write_text('---\naliases: [Cedar]\n---\n## About\nDifferent entity\n')
    assert call(root, ['--company', 'Cedar']) == {'status': 'ambiguous', 'company_knowledge': {}, 'truncated': False}
    (companies / 'other.md').write_text('---\n[invalid yaml\n---\n')
    assert call(root, ['--company', 'Cedar']) == {'status': 'unavailable', 'company_knowledge': {}, 'truncated': False}


@pytest.mark.parametrize('argv', [
    ['--company', 'Cedar', '--person', 'Dana'], ['--company', 'Cedar', '--topic', 'clinic'],
    ['--company', 'Cedar', '--relevant-days', '7'], ['--company', 'Cedar', '--editable-person'],
    ['--company', ''], ['--company', 'Cedar\u2028'], ['--company', 'x' * 201], ['--company'],
])
def test_company_invalid_or_combined_selector_refused(argv):
    with pytest.raises(ValueError):
        read.validate(body(argv))


def test_company_authority_and_pending_journal_are_not_bypassed(graph):
    root, _ = graph
    count = []
    def changed():
        count.append(1)
        return {'complete': True, 'revision': REVISION if len(count) == 1 else 'b' * 64}
    with pytest.raises(read.UnavailableError):
        call(root, ['--company', 'Cedar'], changed)
    with pytest.raises(read.UnavailableError):
        call(root, ['--company', 'Cedar'], lambda: {'complete': False, 'revision': REVISION})
    journal = root / 'knowledge/.journal.json'
    journal.write_text('{"ops":[]}')
    before = tree(root)
    with pytest.raises(read.UnavailableError):
        call(root, ['--company', 'Cedar'])
    assert tree(root) == before


def test_company_unreadable_utf8_and_original_default_resolution(graph):
    root, _ = graph
    companies = root / 'knowledge/companies'
    (companies / 'other.md').write_bytes(b'\xff')
    before = tree(root)
    assert call(root, ['--company', 'Cedar']) == {
        'status': 'unavailable', 'company_knowledge': {}, 'truncated': False}
    assert tree(root) == before
    import knowledge_update as ku
    assert ku.company_knowledge(company_name='Cedar')['slug'] == 'cedar'


def test_company_context_alone_discloses_omission(graph):
    root, _ = graph
    (root / 'knowledge/companies/cedar.md').write_text(
        '---\naliases: [Cedar]\n---\n## About\nClinic tools.\n## Context\nPrivate saved context.\n')
    result = call(root, ['--company', 'Cedar'])
    assert result == {'status': 'saved', 'company_knowledge': {'cedar': 'Cedar — Clinic tools.'},
                      'company_identity': {'slug': 'cedar', 'name': 'Cedar', 'domain': '', 'last_researched': ''}, 'truncated': True}


def test_retune_original_protected_reader_and_revision_bookends(graph):
    root, _ = graph
    import yaml
    from ledger_provenance import digest_frontmatter
    directory = root / 'knowledge/continuity'
    directory.mkdir()
    row = {'anchor_key': 'gmail-stale', 'status': 'open', 'action_type': 'waiting_on',
           'contact_name': 'Maya', 'summary': 'Await their signed deck', 'created_at': '2020-01-01', 'chased_count': 2,
           'source': 'gmail', 'channel': 'email', 'source_thread_id': 'gmail-thread'}
    row['content_provenance'] = {'version': 1, 'kind': 'sources', 'sources': ['gmail'], 'digest': digest_frontmatter(row)}
    (directory / 'gmail.md').write_text('---\n' + yaml.safe_dump(row) + '---\n')
    script = PACK / '_shared/scripts/retune_scan.py'
    request_body = {'operation': 'retune_scan', 'arguments': {'argv': []}, 'source_revision': REVISION}
    def run(authority):
        return read.perform(request_body, script=str(script), executable=sys.executable,
                            env={**os.environ, 'SOTTO_DATA': str(root)}, authority=authority)
    before = tree(root)
    granted = lambda: {'complete': True, 'revision': REVISION, 'google_sources': ['gmail']}
    result = run(granted)
    assert result['coverage'] == 'currently_permitted_canonical_loops'
    assert result['stale_loops'][0]['chased_out'] is True
    assert result['current']['mute_people'] == []
    assert run(lambda: {'complete': True, 'revision': REVISION})['stale_loops'] == []
    with pytest.raises(read.UnavailableError):
        run(lambda: {'complete': False, 'revision': REVISION})
    calls = []
    def change():
        calls.append(1)
        return {**granted(), 'revision': REVISION if len(calls) == 1 else 'b' * 64}
    with pytest.raises(read.UnavailableError):
        run(change)
    assert tree(root) == before
    with pytest.raises(ValueError):
        read.validate({**request_body, 'arguments': {'argv': ['--parked']}})


def test_company_identity_exposes_saved_domain_but_never_a_payload_url(graph):
    root, _ = graph
    company = root / 'knowledge/companies/cedar.md'
    for domain, expected in [('Cedar.Example', 'cedar.example'), ('https://cedar.example/?secret=data', '')]:
        company.write_text('---\naliases: [Cedar]\ndomain: "' + domain + '"\nlast_researched: "2026-10-17"\n---\n## About\nClinic tools.\n')
        before = company.read_bytes()
        result = call(root, ['--company', 'Cedar'])
        assert result['company_identity'] == {'slug': 'cedar', 'name': 'Cedar', 'domain': expected, 'last_researched': '2026-10-17'}
        assert company.read_bytes() == before


def test_unquoted_company_research_date_survives_saved_identity_projection(graph):
    root, _ = graph
    (root / 'knowledge/companies/cedar.md').write_text(
        '---\naliases: [Cedar]\ndomain: cedar.example\nlast_researched: 2026-10-17\n---\n## About\nClinic tools.\n')
    assert call(root, ['--company', 'Cedar'])['company_identity']['last_researched'] == '2026-10-17'


def test_person_history_permission_projection_does_not_disclose_withheld_existence(graph):
    import importlib.util
    root,_=graph
    authority=lambda:{'complete':True,'revision':REVISION,'google_sources':['google_contacts'],'bridge_sources':['imessage']}
    read=lambda:call(root,['--person','Dana','--person-coverage'],authority)['obligation_history']
    empty=read()
    assert empty['status']=='no_match' and empty['absence_is_authoritative'] is False
    spec=importlib.util.spec_from_file_location('history_absence_resolver',PACK/'morning-brief/scripts/continuity_resolve.py')
    resolver=importlib.util.module_from_spec(spec);spec.loader.exec_module(resolver)
    for index in range(3):
        resolver._persist({'anchor_key':f'private-{index}','status':'open','action_type':'follow_up',
            'canonical_id':'dana','contact_identifier':'dana@cedar.com','ask':'PRIVATE-WITHHELD-TASK',
            'source':'gmail','channel':'gmail','source_thread_id':'private-thread',
            'source_refs':[{'sourceType':'gmail','sourceId':f'private-message-{index}'}],'created_at':'2026-10-18'})
        assert read()==empty
    before=tree(root)
    assert read()==empty and tree(root)==before


def test_explicit_relevant_days_selects_cohort_including_default_seven(graph):
    import time
    import knowledge as kg
    root, _ = graph
    person = kg.PersonFile(canonical_id='old-clinic', name='Older Clinic Contact',
        identifiers=['older@example.test'], facts={'f': kg.FactMeta(
            text='Clinic procurement background.', source='user_edit')})
    path = root / 'knowledge/people/old-clinic.md'
    path.write_text(kg.serialize_person_file(person))
    old = time.time() - 30 * 86400
    os.utime(path, (old, old))
    before = tree(root)
    topic_only = call(root, ['--topic', 'clinic'])
    assert topic_only['person_knowledge'] == {}
    assert topic_only['obligation_history']['status'] == 'no_match'
    assert topic_only['obligation_history']['absence_is_authoritative'] is False
    default_cohort = call(root)
    assert 'dana' in default_cohort['person_knowledge']
    assert 'old-clinic' not in default_cohort['person_knowledge']
    for argv in (['--topic', 'clinic', '--relevant-days', '7'],
                 ['--relevant-days', '7', '--topic', 'clinic']):
        recent = call(root, argv)
        assert 'dana' in recent['person_knowledge']
        assert 'old-clinic' not in recent['person_knowledge']
        assert 'obligation_history' not in recent
    wide = call(root, ['--topic', 'clinic', '--relevant-days', '365'])
    assert 'old-clinic' in wide['person_knowledge']
    assert 'obligation_history' not in wide
    assert tree(root) == before


def test_topic_outcome_carries_measured_gaps_only_under_captured_source_grants(graph):
    root, _ = graph
    directory = root / 'knowledge/continuity'
    directory.mkdir()
    row = {'anchor_key': 'clinic-approval', 'canonical_id': 'dana', 'contact_name': 'Dana',
        'contact_identifier': 'dana@cedar.com', 'status': 'resolved', 'action_type': 'follow_up',
        'source': 'gmail', 'channel': 'gmail', 'source_thread_id': 'original-thread',
        'ask': 'Clinic approval follow-up', 'created_at': '2026-10-18',
        'source_refs': [{'sourceType': 'gmail', 'sourceId': 'original'}],
        'resolution': 'replied', 'resolution_evidence': 'Approved and sent.',
        'resolution_source_refs': [{'sourceType': 'gmail', 'sourceId': 'sent'}]}
    (directory / 'clinic.md').write_text('---\n' + json.dumps(row) + '\n---\n')
    window = {'since': 1791936000, 'until': 1792022400, 'complete': True,
        'initial_complete': True, 'pages': 3, 'window_page_base': 2,
        'cursor': 'PRIVATE-CURSOR', 'error': 'PRIVATE-ERROR',
        'observation_totals': dict(pages=1, rows=20, eligible=20, selected=10,
            skipped=0, omitted=10, truncated=2, rejected_people=1)}
    checkpoint = root / 'knowledge/history-state.json'
    def save(sources):
        checkpoint.write_text(json.dumps({'sources': sources}))
    save({'gmail': window})
    authority = lambda: {'complete': True, 'revision': REVISION,
                          'google_sources': ['gmail'], 'bridge_sources': []}
    before = tree(root)
    result = call(root, ['--topic', 'clinic'], authority)
    assert tree(root) == before
    history = result['obligation_history']
    assert history['items'][0]['completion_basis'] == 'source_evidence'
    assert history['items'][0]['resolution_source_refs'][0]['sourceId'] == 'sent'
    gmail = history['learning']['sources']['gmail']
    assert gmail['review_scope'] == 'partial'
    assert gmail['observation_counts']['omitted'] == 10
    assert gmail['observation_counts']['truncated'] == 2
    assert gmail['observation_counts']['rejected_people'] == 1
    assert gmail['outside_window_review'] == 'unknown'
    assert history['learning']['sources']['whatsapp'] == {'checkpoint': 'withheld'}
    assert history['absence_is_authoritative'] is False
    assert 'PRIVATE' not in json.dumps(result)
    assert len(json.dumps({key: value for key, value in history.items() if key != 'learning'}).encode()) <= 18 * 1024
    assert len(json.dumps(history).encode()) <= 23 * 1024
    assert len(json.dumps(result).encode()) <= 48 * 1024
    # A withheld source checkpoint's existence and numbers cannot change the projection.
    save({'gmail': window, 'whatsapp': {**window, 'excluded_page_count': 999}})
    assert call(root, ['--topic', 'clinic'], authority) == result
    # Malformed permitted coverage remains explicit unknown, without losing exact closure evidence.
    checkpoint.write_text('{broken')
    unknown = call(root, ['--topic', 'clinic'], authority)['obligation_history']
    assert unknown['learning']['sources']['gmail'] == {'checkpoint': 'unknown'}
    assert unknown['items'] == history['items']
    no_sources = lambda: {'complete': True, 'revision': REVISION,
                           'google_sources': [], 'bridge_sources': []}
    withheld = call(root, ['--topic', 'clinic'], no_sources)['obligation_history']
    assert withheld['items'] == [] and withheld['omitted'] == 0
    assert all(value == {'checkpoint': 'withheld'} for value in withheld['learning']['sources'].values())
    checkpoint.unlink()
    assert call(root, ['--topic', 'clinic'], no_sources)['obligation_history'] == withheld


def test_person_learning_uses_captured_google_grants_and_withholds_checkpoint_existence(graph, monkeypatch):
    root, _ = graph
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-hosted')
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '["gmail"]')  # inherited values are not authority
    checkpoint = root / 'knowledge/history-state.json'
    no_sources = lambda: {'complete': True, 'revision': REVISION,
                         'google_sources': [], 'bridge_sources': []}
    args = ['--person', 'dana@cedar.com', '--person-coverage']
    empty = call(root, args, no_sources)
    checkpoint.write_text(json.dumps({'sources': {'gmail': {'complete': True, 'pages': 123}}}))
    before = tree(root)
    present = call(root, args, no_sources)
    assert tree(root) == before
    assert present == empty
    sources = {row['source']: row for row in present['coverage']['historical_sources']}
    assert sources['gmail']['learning'] == {'checkpoint': 'withheld'}
    consented = lambda: {'complete': True, 'revision': REVISION,
                         'google_sources': ['gmail'], 'bridge_sources': []}
    permitted = call(root, args, consented)
    gmail = next(row for row in permitted['coverage']['historical_sources'] if row['source'] == 'gmail')
    assert gmail['learning']['checkpoint'] == 'present'


def test_topic_receiver_keeps_large_closure_evidence_with_separate_coverage_budget(graph):
    root, _ = graph
    directory = root / 'knowledge/continuity'
    directory.mkdir()
    refs = lambda source: [{'sourceType': source, 'sourceId': str(i), 'snippet': 'Clinic ' + 'x' * 1193}
                           for i in range(4)]
    row = {'anchor_key': 'large-clinic-approval', 'canonical_id': 'dana', 'contact_name': 'Dana',
        'contact_identifier': 'dana@cedar.com', 'status': 'resolved', 'action_type': 'follow_up',
        'source': 'gmail', 'channel': 'gmail', 'source_thread_id': 'original-thread',
        'ask': 'Clinic ' + 'a' * 1593, 'created_at': '2026-10-18', 'source_refs': refs('gmail'),
        'source_snippet': 's' * 1200, 'resolution': 'replied',
        'resolution_evidence': 'e' * 1600, 'resolution_reason': 'r' * 1600,
        'resolution_source_refs': refs('imessage')}
    (directory / 'clinic.md').write_text('---\n' + json.dumps(row) + '\n---\n')
    window = {'since': 1791936000, 'until': 1792022400, 'initial_since': 1791936000,
        'initial_until': 1792022400, 'complete': True, 'initial_complete': True,
        'pages': 1, 'window_page_base': 0, 'observation_totals': dict(pages=1, rows=1,
        eligible=1, selected=1, skipped=0, omitted=0, truncated=0, rejected_people=0)}
    (root / 'knowledge/history-state.json').write_text(json.dumps({'sources': {
        source: window for source in ('gmail', 'imessage', 'whatsapp')}}))
    authority = lambda: {'complete': True, 'revision': REVISION, 'google_sources': ['gmail'],
                         'bridge_sources': ['imessage', 'whatsapp']}
    before = tree(root)
    result = call(root, ['--topic', 'clinic'], authority)
    history = result['obligation_history']
    assert history['omitted'] == 0 and len(history['items']) == 1
    assert history['items'][0]['source_refs'] == row['source_refs']
    assert history['items'][0]['resolution_source_refs'] == row['resolution_source_refs']
    assert len(json.dumps({key: value for key, value in history.items() if key != 'learning'}).encode()) <= 18 * 1024
    assert 18 * 1024 < len(json.dumps(history).encode()) <= 23 * 1024
    assert len(json.dumps(result).encode()) <= 48 * 1024
    assert tree(root) == before


def test_facts_only_receiver_scope_and_real_read(graph):
    root, _ = graph
    argv = ['--person', 'dana@cedar.com', '--person-coverage', '--facts-only']
    assert read.validate(body(argv)) == argv
    result = call(root, argv)
    assert 'obligation_history' not in result
    assert 'coverage' in result and 'fact_evidence' in result
    assert 'obligation_history' in call(root, argv[:-1])
    for args in [[], ['--person', 'Dana'], ['--person-coverage'],
                 ['--person', 'Dana', '--person-coverage', '--editable-person'],
                 ['--person', 'Dana', '--person-coverage', '--relevant-days', '7'],
                 ['--company', 'Cedar']]:
        with pytest.raises(ValueError):
            read.validate(body([*args, '--facts-only']))


def test_company_pages_retain_details_and_refuse_changed_snapshot(graph):
    root, _ = graph
    company = root / 'knowledge/companies/cedar.md'
    context = 'Original evidence https://cedar.example/report\n' + '詳😀' * 3000
    company.write_text('---\naliases: [Cedar]\n---\n## About\nClinic tools.\n## Context\n' + context)
    before = tree(root)
    assert call(root, ['--company', 'Cedar'])['truncated'] is True
    first = call(root, ['--company', 'Cedar', '--company-page', 'first'])
    pages = [first['company_knowledge']['cedar']]
    page = first
    while page['next_cursor']:
        page = call(root, ['--company', 'Cedar', '--company-page', page['next_cursor']])
        assert len(json.dumps(page).encode()) < 48 * 1024
        pages.append(page['company_knowledge']['cedar'])
    assert ''.join(pages) == 'Cedar — Clinic tools.\n\nSaved context\n' + context
    assert page['truncated'] is False
    assert tree(root) == before
    company.write_text(company.read_text() + '\nNew evidence')
    changed = call(root, ['--company', 'Cedar', '--company-page', first['next_cursor']])
    assert changed['status'] == 'changed' and changed['company_knowledge'] == {}
    for cursor in ('../private', 'a' * 65, 'first --person Dana'):
        with pytest.raises(ValueError):
            call(root, ['--company', 'Cedar', '--company-page', cursor])
