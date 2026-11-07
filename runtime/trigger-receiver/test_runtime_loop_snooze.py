from datetime import datetime, timedelta

import pytest

from test_runtime_owner_loop_tool import context, call, request, rows, ke
from test_runtime_source_loop_tool import source, actual_writer_module

__all__ = ['context', 'actual_writer_module']


def snooze(context, days=3, event='e'*64):
    args = {**source(context),'operation':'snooze','days':days}
    return request(args,event=event,text=args['correction'])


def test_original_snooze_visibility_receipt_replay_and_repeat_snooze(context):
    from ledger_io import active_entries
    from delivery_effects import loop_version, loop_identity
    body = snooze(context)
    result = call(body,context)
    assert result['status']=='committed' and result['operation']=='snooze'
    until = result['snoozed_until']
    row = rows()[0]
    assert not active_entries([row], now=datetime.fromisoformat(until)-timedelta(days=1))
    assert active_entries([row], now=datetime.fromisoformat(until))
    # The original local-day reader surfaces it once the snooze day is reached.
    body['operation']='inspect'
    before = next(__import__('pathlib').Path(ke._load_continuity()._dir()).glob('*.md'))
    from pathlib import Path
    content = Path(before).read_bytes()
    context[1]['_source_authority'] = lambda:{'complete':False}
    assert call(body,context)==result and Path(before).read_bytes()==content
    context[1]['_source_authority'] = lambda:{'complete':True,'revision':'a'*64,'google_sources':['gmail']}
    args={**body['args'],'loop_version':loop_version(row),'loop_identity':loop_identity(row),'days':5}
    next_body=request(args,event='f'*64,text=args['correction'])
    assert call(next_body,context)['status']=='committed'
    assert call(body,context)==result


def test_snooze_lost_after_write_held_without_date_extension(context,monkeypatch):
    body=snooze(context)
    cr=ke._load_continuity()
    original=cr._persist
    def lost(row,**kwargs):
        original(row,**kwargs)
        raise OSError('synthetic response loss')
    monkeypatch.setattr(cr,'_persist',lost)
    assert call(body,context)=={'version':1,'status':'unknown'}
    from pathlib import Path
    before=next(Path(cr._dir()).glob('*.md')).read_bytes()
    monkeypatch.setattr(cr,'_persist',lambda *a,**k:pytest.fail('never repeat uncertain write'))
    body['operation']='inspect'
    assert call(body,context)=={'version':1,'status':'unknown'}
    assert next(Path(cr._dir()).glob('*.md')).read_bytes()==before


@pytest.mark.parametrize('case',['stale','terminal','revoked','overflow'])
def test_refusals_before_snooze(context,case):
    body=snooze(context)
    if case=='stale':
        body['args']['loop_version']='b'*64
    elif case=='terminal':
        row=rows()[0];row['status']='resolved';ke._load_continuity()._persist(row)
    elif case=='revoked':
        context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':[]}
    else:
        body['args']['days']=3652058
    from runtime_owner_loop_tool import _identity
    body['action_id']=_identity(body['event_id'],body['args'])[0]
    result=call(body,context)
    assert result['status']=='rejected'
    assert not rows()[0].get('snoozed_until')


def test_original_local_date_survives_timezone_change(context):
    body=snooze(context)
    result=call(body,context)
    context[1]['_configured_tz_name']=lambda:'Pacific/Kiritimati'
    body['operation']='inspect'
    assert call(body,context)==result


def test_owner_provenance_survives_snooze_and_later_correction(context):
    from delivery_effects import loop_version, loop_identity
    from test_runtime_source_loop_tool import body as correction
    args={'operation':'add','text':'send the deck'}
    added=call(request(args,text=args['text']),context)
    row=next(r for r in rows() if r['anchor_key']==added['anchor_key'])
    binding={'anchor':row['anchor_key'],'loop_version':loop_version(row),'loop_identity':loop_identity(row),
             'source_revision':'a'*64,'correction':'snooze this loop'}
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':[]}
    first=request({**binding,'operation':'snooze','days':400},event='e'*64,text=binding['correction'])
    receipt=call(first,context)
    assert receipt['status']=='committed'
    row=rows()[0]
    assert ke.classify_ledger_sources(row)==('owner',frozenset())
    binding.update(loop_version=loop_version(row),loop_identity=loop_identity(row),correction='Dana will send the deck')
    assert call(correction(binding,operation='correct',event='f'*64),context)['loop_status']=='resolved'
    first['operation']='inspect'
    assert call(first,context)==receipt


def test_tagged_snooze_receipt_tamper_refused(context):
    import jsonstore
    import preferences
    body=snooze(context)
    call(body,context)
    with jsonstore.transaction(preferences._path()) as data:
        data['owner_preference_receipts'][body['event_id']]['receipt']['snoozed_until']='2099-01-01'
    body['operation']='inspect'
    with pytest.raises(ke.EditError,match='receipt unavailable'):
        call(body,context)


def test_stamped_mixed_source_grants_and_preferences_preserve_receipt(context):
    from delivery_effects import loop_version, loop_identity
    from ledger_provenance import digest_frontmatter
    import preferences
    import jsonstore
    binding=source(context)
    row=rows()[0]
    row['source_refs']=[{'source_type':'gmail','source_id':'m1'}, {'source_type':'imessage','source_id':'chat1'}]
    row['source_thread_id']=''
    ke._load_continuity()._persist(row)
    row=rows()[0]
    row['content_provenance']={'version':1,'kind':'sources','sources':['gmail','imessage'],
                               'digest':digest_frontmatter(row)}
    ke._load_continuity()._persist(row)
    from ledger_provenance import source_loop_sources
    assert source_loop_sources(rows()[0]) == frozenset({'gmail','imessage'})
    binding.update(loop_version=loop_version(row),loop_identity=loop_identity(row))
    body=request({**binding,'operation':'snooze','days':3},text=binding['correction'])
    assert call(body,context)['code']=='source_unavailable'
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,
                                           'google_sources':['gmail'],'bridge_sources':['imessage']}
    receipt=call(body,context)
    assert receipt['status']=='committed'
    assert ke.classify_ledger_sources(rows()[0])==('sources',frozenset({'gmail','imessage'}))
    before=jsonstore.read(preferences._path())['owner_preference_receipts']
    preferences.add('vip_people','Dana')
    assert jsonstore.read(preferences._path())['owner_preference_receipts']==before
    body['operation']='inspect'
    assert call(body,context)==receipt
