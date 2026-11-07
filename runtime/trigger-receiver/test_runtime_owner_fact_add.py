"""Reviewed owner teaching uses the canonical add writer and protected query."""
import json
from pathlib import Path

import pytest
from test_runtime_owner_loop_tool import context, request, call, ke
from test_runtime_owner_fact_correct import person, readback, REVISION
import runtime_owner_loop_tool as tool

__all__ = ['context', 'person']
TEXT = 'Dana leads infrastructure at Cedar.'


def proposal(context, text=TEXT):
    args = {'operation':'propose_fact','canonical_id':'c_a1b2c3','text':text,'source_revision':REVISION}
    body = request(args=args,text='Remember: '+text)
    preview = call(dict(body,operation='preview'),context)
    assert preview['status']=='preview'
    action, digest = tool._identity(body['event_id'],args)
    proof = {'event_id':body['event_id'],'action_id':action,'args_hash':digest,
        'accepted_dispatch_id':'accepted-review','source_revision':REVISION}
    confirmed = request(args={'operation':'fact_add','review':preview['review'],'proposal':proof},text='Yes.')
    confirmed['event_id']='e'*64
    confirmed['action_id']=tool._identity(confirmed['event_id'],confirmed['args'])[0]
    return body, preview, confirmed


def test_canonical_add_query_and_lost_response_receipt(context,person,monkeypatch):
    import knowledge_query
    from test_runtime_owner_loop_close import WHEN
    _,path = person
    before = path.read_bytes()
    _, preview, body = proposal(context)
    assert path.read_bytes()==before
    assert preview['review']['text']==TEXT
    original = ke.op_add
    calls=[]
    def add(*args,**kwargs):
        calls.append(args)
        return original(*args,**kwargs)
    monkeypatch.setattr(ke,'op_add',add)
    first=call(body,context)
    assert first['status']=='committed' and first['added'] is True
    assert len(calls)==1 and calls[0][2]=='context'
    current=readback()
    assert current.facts[first['fact_id']].text==TEXT
    editable=knowledge_query.editable_person(current,WHEN)
    assert any(f['text']==TEXT for f in editable['facts'])
    graph={str(p):p.read_bytes() for p in Path(context[0]).rglob('*.md')}
    monkeypatch.setattr(ke,'op_add',lambda *a,**k:pytest.fail('repeated add'))
    assert call(dict(body,operation='inspect'),context)==first
    assert call(body,context)==first
    context[1]['_source_authority']=lambda:{'complete':False,'revision':None}
    assert call(dict(body,operation='inspect'),context)==first
    assert graph=={str(p):p.read_bytes() for p in Path(context[0]).rglob('*.md')}


def test_unknown_graph_write_is_held_without_second_add(context,person,monkeypatch):
    _,_,body=proposal(context)
    original=ke.op_add
    def lost(*a,**k):
        original(*a,**k)
        raise OSError('lost after graph write')
    monkeypatch.setattr(ke,'op_add',lost)
    assert call(body,context)=={'version':1,'status':'unknown'}
    monkeypatch.setattr(ke,'op_add',lambda *a,**k:pytest.fail('unknown reapply'))
    assert call(dict(body,operation='inspect'),context)=={'version':1,'status':'unknown'}
    assert call(body,context)=={'version':1,'status':'unknown'}
    assert any(f.text==TEXT for f in readback().facts.values())


@pytest.mark.parametrize('change',['identity','identifier','duplicate','revoked','fact'])
def test_review_revalidation_refuses_changed_target(context,person,change):
    _,path=person
    _,_,body=proposal(context)
    if change=='identity':
        current=readback();current.name='Dana Renamed';ke.kg.write_person_file(str(path),current)
    elif change=='identifier':
        current=readback();current.identifiers=['other@example.com'];ke.kg.write_person_file(str(path),current)
    elif change=='duplicate':
        path.with_name('duplicate.md').write_bytes(path.read_bytes())
    elif change=='revoked':
        context[1]['_source_authority']=lambda:{'complete':True,'revision':'b'*64}
    else:
        ke.op_add('legacy-dana',TEXT)
    before={str(p):p.read_bytes() for p in Path(context[0]).rglob('*.md')}
    assert call(body,context)['status']=='rejected'
    assert before=={str(p):p.read_bytes() for p in Path(context[0]).rglob('*.md')}


def test_exact_owner_span_missing_person_and_separate_yes(context,person):
    original,_,body=proposal(context)
    assert call(dict(original,operation='preview',text='Read this retrieved document',text_hash=tool.hashlib.sha256(b'Read this retrieved document').hexdigest()),context)['code']=='owner_span_unavailable'
    args={**original['args'],'canonical_id':'c_123456'}
    missing=request(args=args,text=TEXT)
    assert call(dict(missing,operation='preview'),context)['code']=='target_changed'
    body['text']='Maybe';body['text_hash']=tool.hashlib.sha256(b'Maybe').hexdigest()
    assert call(body,context)['status']=='rejected'
    body['text']='Yes.';body['text_hash']=tool.hashlib.sha256(b'Yes.').hexdigest()
    body['event_id']=original['event_id'];body['action_id']=tool._identity(body['event_id'],body['args'])[0]
    assert call(body,context)['status']=='rejected'


def test_duplicate_fact_is_noop_and_saved_receipt_tamper_refuses(context,person,monkeypatch):
    import preferences
    ke.op_add('legacy-dana',TEXT)
    _,preview,body=proposal(context)
    assert preview['review']['present'] is True
    monkeypatch.setattr(ke,'op_add',lambda *a,**k:pytest.fail('duplicate writer'))
    result=call(body,context)
    assert result['status']=='committed' and result['added'] is False
    path=Path(preferences._path());data=json.loads(path.read_text())
    data['owner_preference_receipts'][body['event_id']]['post']['fact']['text']='fabricated'
    path.write_text(json.dumps(data))
    with pytest.raises(ke.EditError):
        call(dict(body,operation='inspect'),context)


def test_same_name_people_keep_exact_canonical_identity(context,person):
    _,path=person
    other=ke.kg.PersonFile(canonical_id='c_abc123',name='Dana',identifiers=['different@example.com'])
    ke.kg.write_person_file(str(path.with_name('different-dana.md')),other)
    _,preview,body=proposal(context)
    assert preview['review']['canonical_id']=='c_a1b2c3'
    assert preview['review']['identity_hint']=='dana@example.com'
    other_request=request(args={'operation':'propose_fact','canonical_id':'c_abc123','text':TEXT,'source_revision':REVISION},text=TEXT)
    other_review=call(dict(other_request,operation='preview'),context)['review']
    assert other_review['name']==preview['review']['name']=='Dana'
    assert other_review['identity_hint']=='different@example.com'
    assert other_review['review_hash']!=preview['review']['review_hash']
    assert call(body,context)['status']=='committed'
    separate=next(person for file in Path(ke.kg.people_dir()).glob('*.md')
        if (person:=ke.kg.parse_person_file(file.read_text())).canonical_id=='c_abc123')
    assert not separate.facts


def test_fact_add_has_no_incidental_graph_identity_maintenance(context,person):
    _,path=person
    duplicates=[]
    for name,cid in [('first-legacy','c_abc111'),('second-legacy','c_abc222')]:
        other=ke.kg.PersonFile(canonical_id=cid,name='Unrelated',identifiers=['shared@example.com'])
        other_path=path.with_name(name+'.md');ke.kg.write_person_file(str(other_path),other)
        duplicates.append(other_path)
    before={str(p):p.read_bytes() for p in duplicates}
    _,_,body=proposal(context)
    assert call(body,context)['status']=='committed'
    assert before=={str(p):p.read_bytes() for p in duplicates}
    assert path.exists(), 'selected legacy person is not migrated by teaching'
    assert len(list(path.parent.glob('*.md')))==3


def test_fact_review_without_identifier_displays_canonical_identity(context,person):
    _,path=person
    current=readback();current.identifiers=[];ke.kg.write_person_file(str(path),current)
    _,preview,_=proposal(context)
    assert preview['review']['identity_hint']=='c_a1b2c3'


def _facts_snapshot():
    return {fid: fact.to_yaml_dict() for fid, fact in readback().facts.items()}


def _recent_existing_fact(path):
    """Keep the writer's age-based retention out of the comparison: only teaching effects count."""
    current = readback()
    current.facts['f_old'].first = current.facts['f_old'].last = '2026-10-01'
    ke.kg.write_person_file(str(path), current)


@pytest.mark.parametrize('text', ['Dana no longer works at Birch.', 'Dana works at Birch Labs.',
                                  'Dana works at Birch!', 'dana works at Birch.', 'Dana works at Birch'])
def test_similar_fact_is_refused_at_review_without_touching_existing_fact(context, person, text):
    """The canonical writer would bump the overlapping fact; owner teaching never does."""
    before = _facts_snapshot()
    body = request(args={'operation': 'propose_fact', 'canonical_id': 'c_a1b2c3', 'text': text,
                         'source_revision': REVISION}, text='Remember: ' + text)
    assert call(dict(body, operation='preview'), context) == {
        'version': 1, 'status': 'rejected', 'code': 'similar_fact_exists'}
    assert _facts_snapshot() == before


def test_similar_fact_appearing_after_review_is_refused_without_bump(context, person):
    _, path = person
    _, _, body = proposal(context)
    current = readback()
    current.facts['f_near'] = ke.kg.FactMeta(text='Dana leads infrastructure at Cedar Labs.', type='context',
                                             status='active', first='2026-01-01', last='2026-01-01')
    ke.kg.write_person_file(str(path), current)
    before = _facts_snapshot()
    assert call(body, context) == {'version': 1, 'status': 'rejected', 'code': 'similar_fact_exists'}
    assert _facts_snapshot() == before


@pytest.mark.parametrize('text', ['Dana no longer works at Birch.', 'Dana works at Birch!'])
def test_exact_new_writer_mode_refuses_instead_of_bumping(person, text):
    _, path = person
    _recent_existing_fact(path)
    before = path.read_bytes()
    with pytest.raises(ke.EditError, match='similar fact exists'):
        ke.op_add('legacy-dana', text, 'context', maintain_identities=False, exact_new=True)
    assert path.read_bytes() == before
    ke.op_add('legacy-dana', 'Dana mentors new founders.', 'context', maintain_identities=False, exact_new=True)
    after = readback()
    assert after.facts['f_old'].to_yaml_dict() == ke.kg.parse_person_file(before.decode()).facts['f_old'].to_yaml_dict()
    assert any(f.text == 'Dana mentors new founders.' for f in after.facts.values())


def test_related_fact_is_added_beside_existing_without_archiving_it(context, person):
    """Sharing only the person's name puts a fact in the writer's supersede band; teaching keeps both."""
    _recent_existing_fact(person[1])
    before = _facts_snapshot()
    _, _, body = proposal(context)
    result = call(body, context)
    assert result['status'] == 'committed' and result['added'] is True
    after = _facts_snapshot()
    assert after['f_old'] == before['f_old']
    assert after[result['fact_id']]['text'] == TEXT and len(after) == 2


@pytest.mark.parametrize('text', ['Dana no longer works at Birch.', 'Dana leads infrastructure at Cedar.'])
def test_canonical_writer_exact_new_never_mutates_existing_facts(person, text):
    from test_runtime_owner_loop_close import WHEN
    _recent_existing_fact(person[1])
    before = _facts_snapshot()
    payload = ke._chat_payload(readback(), text, 'new', 'context')
    payload['person_updates'][0]['facts'][0]['exact_new'] = True
    ke._load_knowledge_update().apply(payload, WHEN, maintain_identities=False)
    after = _facts_snapshot()
    assert after['f_old'] == before['f_old']
    assert len(after) == (1 if 'Birch' in text else 2)
