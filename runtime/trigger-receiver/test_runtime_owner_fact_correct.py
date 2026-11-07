"""Original graph correction under exact admitted target and non-repeating receipt."""
import json
from pathlib import Path

import pytest
from test_runtime_owner_loop_tool import context, request, call, ke
import runtime_owner_loop_tool as tool

__all__ = ['context']
REVISION = 'a'*64
TEXT = 'Dana now works at Cedar.'


@pytest.fixture
def person(context,monkeypatch):
    monkeypatch.setattr(tool.loops, '_load_writer', lambda: ke)
    root, policy, _ = context
    policy['_source_authority'] = lambda: {'complete': True, 'revision': REVISION}
    kg = ke.kg
    path = Path(kg.people_dir()) / 'legacy-dana.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    original = kg.PersonFile(canonical_id='c_a1b2c3', name='Dana', identifiers=['dana@example.com'],
        facts={'f_old': kg.FactMeta(text='Dana works at Birch.', type='context', status='active',
            first='2026-01-01', last='2026-01-01')})
    kg.write_person_file(str(path), original)
    args = {'operation':'fact_correct','canonical_id':original.canonical_id,'fact_id':'f_old',
        'fact_version':kg.person_fact_version(original.canonical_id,'f_old',original.facts['f_old']),
        'text':TEXT,'source_revision':REVISION}
    return args, path


def readback():
    paths = list(Path(ke.kg.people_dir()).glob('*.md'))
    assert len(paths) == 1
    return ke.kg.parse_person_file(paths[0].read_text())


def test_original_correction_receipt_and_reopen_never_reapplies(context,person,monkeypatch):
    args,_ = person
    body = request(args=args,text='Please correct this: '+TEXT)
    first = call(body,context)
    assert first['status'] == 'committed'
    actual = readback()
    assert actual.facts['f_old'].status == 'archived'
    assert actual.facts['f_old'].archived_reason == 'user_corrected'
    assert actual.facts[first['replacement_fact_id']].text == TEXT
    before = {str(p):p.read_bytes() for p in Path(context[0]).rglob('*.md')}
    monkeypatch.setattr(ke,'op_correct',lambda *a,**k:pytest.fail('repeated correction'))
    assert call(dict(body,operation='inspect'),context) == first
    assert call(body,context) == first
    context[1]['_source_authority'] = lambda: {'complete':False,'revision':None}
    assert call(dict(body,operation='inspect'),context) == first
    assert before == {str(p):p.read_bytes() for p in Path(context[0]).rglob('*.md')}
    import preferences
    preferences.add('tone_notes','Be concise')
    assert call(dict(body,operation='inspect'),context) == first
    assert 'person_fact_correct' not in str(preferences.read_owner_preferences())


def test_unknown_original_two_write_effect_never_reapplied(context,person,monkeypatch):
    args,_ = person
    body = request(args=args,text=TEXT)
    original = ke.op_correct
    def lost(*a,**k):
        original(*a,**k)
        raise OSError('lost after graph write')
    monkeypatch.setattr(ke,'op_correct',lost)
    assert call(body,context) == {'version':1,'status':'unknown'}
    monkeypatch.setattr(ke,'op_correct',lambda *a,**k:pytest.fail('unknown recontact'))
    assert call(dict(body,operation='inspect'),context) == {'version':1,'status':'unknown'}
    assert call(body,context) == {'version':1,'status':'unknown'}
    assert readback().facts['f_old'].status == 'archived'


@pytest.mark.parametrize('change',[{'fact_version':'0'*64},{'canonical_id':'c_123456'},{'fact_id':'absent'}])
def test_stale_foreign_missing_targets_have_no_graph_effect(context,person,change):
    args,path = person
    before = path.read_bytes()
    result = call(request(args={**args,**change},text=TEXT),context)
    assert result['status']=='rejected' and result['code']=='target_changed'
    assert path.read_bytes()==before


def test_duplicate_canonical_and_revoked_authority_refuse(context,person):
    args,path = person
    other=path.with_name('duplicate.md');other.write_bytes(path.read_bytes())
    assert call(request(args=args,text=TEXT),context)['code']=='target_changed'
    other.unlink()
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'b'*64}
    assert call(request(args=args,text=TEXT),context)['code']=='source_unavailable'


def test_exact_owner_words_and_malformed_saved_receipt_refuse(context,person):
    args,_=person
    assert call(request(args=args,text='Please correct Dana'),context)['code']=='owner_span_unavailable'
    body=request(args=args,text=TEXT)
    assert call(body,context)['status']=='committed'
    import preferences
    path=Path(preferences._path());data=json.loads(path.read_text())
    data['owner_preference_receipts'][body['event_id']]['post']['replacement_fact']['text']='fabricated'
    path.write_text(json.dumps(data))
    with pytest.raises(ke.EditError):
        call(dict(body,operation='inspect'),context)


def test_failure_between_original_apply_and_target_archive_stays_held(context,person,monkeypatch):
    args,_=person
    args={**args,'text':'Dana moved to a new role.'}
    body=request(args=args,text=args['text'])
    monkeypatch.setattr(ke,'_resolve_after_apply',lambda *_: (_ for _ in ()).throw(OSError('interrupted second step')))
    assert call(body,context)=={'version':1,'status':'unknown'}
    before={str(p):p.read_bytes() for p in Path(context[0]).rglob('*.md')}
    monkeypatch.setattr(ke,'op_correct',lambda *a,**k:pytest.fail('partial correction replay'))
    assert call(dict(body,operation='inspect'),context)=={'version':1,'status':'unknown'}
    assert before=={str(p):p.read_bytes() for p in Path(context[0]).rglob('*.md')}


def test_editable_inventory_fact_version_is_exact_stored_target(context,person):
    import knowledge_query
    from test_runtime_owner_loop_close import WHEN
    args,path=person
    current=ke.kg.parse_person_file(path.read_text())
    editable=knowledge_query.editable_person(current,WHEN)
    assert editable['facts'][0]['fact_version']==args['fact_version']
    current.facts['f_old'].seen+=1
    assert knowledge_query.editable_person(current,WHEN)['facts'][0]['fact_version']!=args['fact_version']


def test_graph_sync_precedes_receipt_and_sync_failure_stays_unknown(context,person,monkeypatch):
    import preferences
    args,_=person
    body=request(args=args,text=TEXT)
    original_sync=preferences._sync_existing_preferences
    original_replace=preferences._durable_preferences_replace
    order=[]
    def sync(path):
        if str(path).endswith('.md'):
            order.append('graph_sync')
            raise OSError('directory fsync unavailable')
        return original_sync(path)
    def replace(path,data):
        record=data['owner_preference_receipts'][body['event_id']]
        order.append(record['status'])
        return original_replace(path,data)
    monkeypatch.setattr(preferences,'_sync_existing_preferences',sync)
    monkeypatch.setattr(preferences,'_durable_preferences_replace',replace)
    assert call(body,context)=={'version':1,'status':'unknown'}
    assert order==['contacting','graph_sync']
    saved=json.loads(Path(preferences._path()).read_text())['owner_preference_receipts'][body['event_id']]
    assert saved['status']=='contacting'
    monkeypatch.setattr(ke,'op_correct',lambda *a,**k:pytest.fail('sync uncertainty reapply'))
    assert call(dict(body,operation='inspect'),context)=={'version':1,'status':'unknown'}


def test_success_syncs_exact_graph_file_before_committed_receipt(context,person,monkeypatch):
    import preferences
    args,_=person
    body=request(args=args,text=TEXT)
    original_sync=preferences._sync_existing_preferences
    original_replace=preferences._durable_preferences_replace
    order=[]
    def sync(path):
        if str(path).endswith('.md'):
            assert Path(path).parent==Path(ke.kg.people_dir())
            assert ke.kg.parse_person_file(Path(path).read_text()).facts['f_old'].status=='archived'
            order.append('graph_sync')
        return original_sync(path)
    def replace(path,data):
        order.append(data['owner_preference_receipts'][body['event_id']]['status'])
        return original_replace(path,data)
    monkeypatch.setattr(preferences,'_sync_existing_preferences',sync)
    monkeypatch.setattr(preferences,'_durable_preferences_replace',replace)
    assert call(body,context)['status']=='committed'
    assert order==['contacting','graph_sync','committed']


@pytest.mark.parametrize('fields', [{'title':'COO'}, {'company':'Cedar'}, {'title':'COO','company':'Cedar'}])
def test_explicit_identity_correction_is_protected_and_replays(context,person,monkeypatch,fields):
    args,path=person
    original=readback()
    original.title=None
    original.company=None
    original.identity_support={field:{'source':'user_edit','fact_id':'previous'} for field in ('title','company')}
    ke.kg.write_person_file(str(path),original)
    body=request(args={**args,**fields},text=TEXT+' Title: COO. Company: Cedar.')
    receipt=call(body,context)
    assert receipt['status']=='committed'
    actual=readback()
    for field in ('title','company'):
        assert getattr(actual,field)==fields.get(field)
        if field in fields:
            assert actual.identity_support[field]=={'source':'user_edit','fact_id':receipt['replacement_fact_id']}
    ku=ke._load_knowledge_update()
    ku.apply({'person_updates':[{'name':actual.name,'identifiers':actual.identifiers,
        'patch':{'title':'Wrong title','company':'Wrong company'},'updated_by':'web_research'}]})
    actual=readback()
    for field in fields:
        assert getattr(actual,field)==fields[field]
    monkeypatch.setattr(ke,'op_correct',lambda *a,**k:pytest.fail('repeated typed correction'))
    assert call(dict(body,operation='inspect'),context)==receipt
    assert call(body,context)==receipt
    changed=request(args={**args,**fields,'title':'CEO'},text=TEXT+' COO Cedar CEO')
    assert call(changed,context)['code']=='event_conflict'


@pytest.mark.parametrize('field', ['title','company'])
def test_identity_correction_requires_exact_owner_span(context,person,field):
    args,path=person
    before=path.read_bytes()
    result=call(request(args={**args,field:'Invented'},text=TEXT),context)
    assert result['code']=='owner_span_unavailable'
    assert path.read_bytes()==before


@pytest.mark.parametrize('field', ['title','company'])
@pytest.mark.parametrize('value', ['', ' padded ', 'bad\nvalue', 'bad\u0085value', 'bad\u2028value', 'x'*201, None, 12])
def test_identity_correction_malformed_values_never_write(context,person,field,value):
    args,path=person
    before=path.read_bytes()
    with pytest.raises(ValueError):
        call(request(args={**args,field:value},text=TEXT),context)
    assert path.read_bytes()==before


def test_explicit_identity_post_is_bound_and_missing_fields_still_withdraw(context,person):
    args,path=person
    original=readback()
    original.title='Old title'
    original.company='Birch'
    original.identity_support={field:{'source':'web_research','fact_id':'f_old'} for field in ('title','company')}
    ke.kg.write_person_file(str(path),original)
    body=request(args={**args,'title':'COO'},text=TEXT+' COO')
    receipt=call(body,context)
    actual=readback()
    assert actual.title=='COO' and actual.company is None
    assert actual.identity_support['company']=={'source':'user_edit','fact_id':'f_old'}
    import preferences
    path=Path(preferences._path())
    data=json.loads(path.read_text())
    post=data['owner_preference_receipts'][body['event_id']]['post']
    assert post['identity']=={'title':{'value':'COO','support':{'source':'user_edit','fact_id':receipt['replacement_fact_id']}}}
    post['identity']['title']['value']='CEO'
    path.write_text(json.dumps(data))
    with pytest.raises(ke.EditError,match='identity unavailable'):
        call(dict(body,operation='inspect'),context)


def test_explicit_identity_uncertain_write_never_reapplies(context,person,monkeypatch):
    args,_=person
    body=request(args={**args,'title':'COO'},text=TEXT+' COO')
    original=ke.op_correct
    def lost(*a,**k):
        original(*a,**k)
        raise OSError('lost after graph write')
    monkeypatch.setattr(ke,'op_correct',lost)
    assert call(body,context)=={'version':1,'status':'unknown'}
    assert readback().title=='COO'
    monkeypatch.setattr(ke,'op_correct',lambda *a,**k:pytest.fail('uncertain identity replay'))
    assert call(dict(body,operation='inspect'),context)=={'version':1,'status':'unknown'}
    assert call(body,context)=={'version':1,'status':'unknown'}


def test_withdrawn_profile_can_be_restored_by_a_later_explicit_correction(context,person):
    args,path=person
    original=readback()
    original.title='CEO'
    original.company='Birch'
    original.identity_support={field:{'source':'web_research','fact_id':'f_old'} for field in ('title','company')}
    ke.kg.write_person_file(str(path),original)
    withdrawn=call(request(args=args,text=TEXT),context)
    current=readback()
    assert current.title is None and current.company is None
    replacement=withdrawn['replacement_fact_id']
    text='Dana is COO at Cedar.'
    restore={**args,'fact_id':replacement,
        'fact_version':ke.kg.person_fact_version(current.canonical_id,replacement,current.facts[replacement]),
        'text':text,'title':'COO','company':'Cedar'}
    body=request(args=restore,event='e'*64,text=text)
    restored=call(body,context)
    assert restored['status']=='committed'
    current=readback()
    assert current.title=='COO' and current.company=='Cedar'
    assert current.canonical_id==args['canonical_id']
    import knowledge_query
    from test_runtime_owner_loop_close import WHEN
    packed=knowledge_query.pack_person(current,True,WHEN)
    assert 'COO @ Cedar' in packed and 'CEO @ Birch' not in packed
    assert call(dict(body,operation='inspect'),context)==restored


@pytest.mark.parametrize('change',[{'fact_version':'0'*64},{'canonical_id':'c_123456'},{'source_revision':'b'*64}])
def test_explicit_identity_stale_authority_and_target_have_no_effect(context,person,change):
    args,path=person
    before=path.read_bytes()
    result=call(request(args={**args,'title':'COO',**change},text=TEXT+' COO'),context)
    assert result['status']=='rejected'
    assert path.read_bytes()==before
