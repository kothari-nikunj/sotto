"""Exact original keep transformation through admitted durable receipts."""
from datetime import datetime
from pathlib import Path

import pytest
from test_runtime_owner_loop_tool import context, call, request, rows, ke
from test_runtime_source_loop_tool import source, actual_writer_module

__all__ = ['context', 'actual_writer_module']


def keep(context, *, waiting=False, parked=False):
    from delivery_effects import loop_version, loop_identity
    import ledger_io
    binding = source(context)
    row = rows()[0]
    row['status'] = 'parked' if parked else 'open'
    if waiting:
        row['action_type'] = 'waiting_on'
        for field in ledger_io.CHASE_STATE_FIELDS:
            row[field] = 3 if field == 'nudge_count' else '2026-10-05'
    ke._load_continuity()._persist(row)
    binding.update(loop_version=loop_version(row), loop_identity=loop_identity(row),
                   operation='keep', correction='I still care about this loop')
    return request(binding, event='e'*64, text=binding['correction'])


@pytest.mark.parametrize('waiting,parked', [(False,False),(False,True),(True,False),(True,True)])
def test_original_direction_keep_provenance_and_replay(context,waiting,parked):
    import ledger_io
    body=keep(context,waiting=waiting,parked=parked)
    before=rows()[0]
    result=call(body,context)
    assert result['status']=='committed' and result['operation']=='keep'
    post=rows()[0]
    assert post['created_at']==before['created_at']
    assert post['status']==('parked' if waiting and parked else 'open')
    if waiting:
        assert all(field not in post for field in ledger_io.CHASE_STATE_FIELDS)
    else:
        assert post['reopened_at']=='2026-10-05'
        assert ledger_io.active_entries([post], now=datetime(2026,10,5))
    from ledger_provenance import source_loop_sources
    assert source_loop_sources(post)==frozenset({'gmail'})
    context[1]['_configured_tz_name']=lambda:'Pacific/Kiritimati'
    context[1]['_source_authority']=lambda:{'complete':False}
    body['operation']='inspect'
    assert call(body,context)==result


def test_keep_lost_after_original_write_inspect_never_reapplies(context,monkeypatch):
    body=keep(context,parked=True)
    cr=ke._load_continuity(); original=cr._persist
    def lost(row,**kwargs):
        original(row,**kwargs)
        raise OSError('synthetic lost response')
    monkeypatch.setattr(cr,'_persist',lost)
    assert call(body,context)=={'version':1,'status':'unknown'}
    path=next(Path(cr._dir()).glob('*.md')); before=path.read_bytes()
    monkeypatch.setattr(cr,'_persist',lambda *a,**k:pytest.fail('no repeated keep'))
    body['operation']='inspect'
    assert call(body,context)=={'version':1,'status':'unknown'}
    assert path.read_bytes()==before


@pytest.mark.parametrize('case',['stale','terminal','revoked','mixed','span'])
def test_keep_refuses_before_effect(context,case):
    body=keep(context,parked=True)
    row=rows()[0]
    if case=='stale':
        body['args']['loop_version']='b'*64
    elif case=='terminal':
        row['status']='resolved'; ke._load_continuity()._persist(row)
    elif case=='revoked':
        context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':[]}
    elif case=='mixed':
        row['resolution_source_refs']=[{'source':'imessage','message_id':'x'}]
        ke._load_continuity()._persist(row)
    else:
        body['text']='Not that request'; body['text_hash']=__import__('hashlib').sha256(body['text'].encode()).hexdigest()
    from runtime_owner_loop_tool import _identity
    body['action_id']=_identity(body['event_id'],body['args'])[0]
    path=next(Path(ke._load_continuity()._dir()).glob('*.md')); before=path.read_bytes()
    assert call(body,context)['status']=='rejected'
    assert path.read_bytes()==before


def test_owner_keep_receipt_preferences_preserved_and_same_row_correctable(context):
    from delivery_effects import loop_version, loop_identity
    from test_runtime_source_loop_tool import body as correction
    import preferences
    added=call(request({'operation':'add','text':'send the deck'},text='send the deck'),context)
    row=next(r for r in rows() if r['anchor_key']==added['anchor_key'])
    args={'operation':'keep','anchor':row['anchor_key'],'loop_version':loop_version(row),
          'loop_identity':loop_identity(row),'source_revision':'a'*64,'correction':'I still care'}
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':[]}
    body=request(args,event='e'*64,text=args['correction'])
    receipt=call(body,context)
    assert receipt['status']=='committed'
    row=next(r for r in rows() if r['anchor_key']==added['anchor_key'])
    assert ke.classify_ledger_sources(row)==('owner',frozenset())
    preferences.add('tone_notes','Brief')
    body['operation']='inspect'
    assert call(body,context)==receipt
    binding={**args,'loop_version':loop_version(row),'loop_identity':loop_identity(row),
             'correction':'Dana will send the deck'}
    binding.pop('operation')
    assert call(correction(binding,operation='correct',event='f'*64),context)['loop_status']=='resolved'
    assert call(body,context)==receipt
