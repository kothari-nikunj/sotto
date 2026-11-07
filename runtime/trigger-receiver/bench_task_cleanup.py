"""Local synthetic task-batch cost probe; no providers, credentials or live data."""
import argparse
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

from _pytest.monkeypatch import MonkeyPatch
from test_runtime_owner_loop_tool import context, call, request, ke
from test_runtime_source_loop_tool import actual_writer_module


def measure(row_count, selected_count):
    with TemporaryDirectory(prefix='sotto-task-bench-') as folder, MonkeyPatch.context() as monkeypatch:
        fixture=context.__wrapped__(Path(folder),monkeypatch)
        actual_writer_module.__wrapped__(monkeypatch)
        cr=ke._load_continuity()
        authority_calls=0
        def authority():
            nonlocal authority_calls
            authority_calls+=1
            return {'complete':True,'revision':'a'*64,'google_sources':['gmail']}
        fixture[1]['_source_authority']=authority
        for index in range(row_count):
            cr._persist({'anchor_key':f'bench-{index:04d}','action_type':'follow_up','channel':'email',
                'status':'open','created_at':'2026-09-01','summary':f'Bench task {index}: '+'request '*20,
                'ask':'A synthetic source request with enough detail to resemble a retained obligation. '*6,
                'contact_name':f'Person {index}','contact_identifier':f'p{index}@example.com','source':'gmail',
                'source_message_id':f'm{index}','source_thread_id':f't{index}'})
        proposal=request({'operation':'propose_cleanup','scope':'open','source_revision':'a'*64},
            event='a'*64,operation='preview',text='Show my open tasks')
        preview=call(proposal,fixture)
        assert preview['status']=='preview',preview
        items=[{'index':index,'action':'resolve','correction':str(index)} for index in range(1,selected_count+1)]
        import runtime_owner_loop_tool as tool
        args={'operation':'cleanup','list':preview['review'],'proposal':{'event_id':proposal['event_id'],
            'action_id':proposal['action_id'],'args_hash':tool._identity(proposal['event_id'],proposal['args'])[1],
            'accepted_dispatch_id':'synthetic-accepted-list','source_revision':'a'*64},'items':items}
        body=request(args,event='e'*64,text=', '.join(str(i['index']) for i in items)+' are done')
        authority_calls=0
        reads=0
        original=ke._owner_close_rows
        load_seconds=0.0
        durable_seconds=0.0
        writes=0
        import preferences
        durable=preferences._durable_preferences_replace
        def save(*args,**kwargs):
            nonlocal durable_seconds,writes
            began=perf_counter();writes+=1
            try:
                return durable(*args,**kwargs)
            finally:
                durable_seconds+=perf_counter()-began
        def load(cr):
            nonlocal reads,load_seconds
            reads+=1;began=perf_counter()
            try:
                return original(cr)
            finally:
                load_seconds+=perf_counter()-began
        start=perf_counter()
        with patch.object(ke,'_owner_close_rows',load),patch.object(preferences,'_durable_preferences_replace',save):
            receipt=call(body,fixture)
        elapsed=perf_counter()-start
        assert receipt['outcome']=='complete',receipt
        actual_preferences=Path(__import__('preferences')._path())
        return {'rows':row_count,'selected':selected_count,'seconds':round(elapsed,3),
            'authority_calls':authority_calls,'strict_ledger_reads':reads,'strict_read_seconds':round(load_seconds,3),
            'durable_rewrites':writes,'durable_rewrite_seconds':round(durable_seconds,3),
            'preferences_bytes':actual_preferences.stat().st_size,
            'ledger_bytes':sum(p.stat().st_size for p in Path(cr._dir()).glob('*.md')),
            'receipt_bytes':len(json.dumps(receipt).encode()),'synthetic_authority':'instant; no Bridge latency simulated'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows',type=int,default=300)
    parser.add_argument('--selected',type=int,default=12)
    parser.add_argument('--source-ref',help='Compare against trusted local Git source, without changing the checkout')
    options=parser.parse_args()
    if not 1<=options.selected<=min(options.rows,50) or options.rows>1024:
        parser.error('require 1..1024 rows and 1..min(rows,50) selected items')
    if options.source_ref:
        source=subprocess.run(['git','show',options.source_ref+':sotto-agent/sotto-chief-of-staff/_shared/knowledge/knowledge_edit.py'],check=True,capture_output=True,text=True).stdout
        exec(compile(source,ke.__file__,'exec'),ke.__dict__)
    print(json.dumps({'source':options.source_ref or 'working-tree',**measure(options.rows,options.selected)},sort_keys=True))
