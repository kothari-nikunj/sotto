"""Frozen prep sources preserve real readers; no model or external connection is made."""
from datetime import datetime, timezone, timedelta
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
AGENT = HERE.parents[2]


def test_real_calendar_cli_reads_the_frozen_later_occurrence(tmp_path):
    spec = importlib.util.spec_from_file_location('prep_tokens', AGENT/'sotto-chief-of-staff/evals/run_evals.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scenario = json.loads((HERE/'scenarios/meeting-prep.json').read_text())
    now = datetime.now(timezone.utc)
    world = module.resolve_tokens(scenario['world'], now)
    path = tmp_path/'world.json';path.write_text(json.dumps(world))
    since = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    until = (now+timedelta(hours=72)).strftime('%Y-%m-%dT%H:%M:%SZ')
    result = subprocess.run([sys.executable,str(AGENT/'adapters/sotto/google_api.py'),'calendar','list','--start',since,'--end',until],
        env={**os.environ,'PYTHONPATH':str(HERE/'provider'),'SOTTO_EVAL_WORLD':str(path),'SOTTO_DATA':str(tmp_path)},
        text=True,capture_output=True,timeout=10)
    assert result.returncode == 0,result.stderr
    rows = json.loads(result.stdout)
    assert [row['id'] for row in rows] == ['routinecalendar01','fixturecalendar01']
    target = rows[1]
    assert 'west entrance' in target['description'] and target['calendar_id']=='owner@inside.test'
    traces = [json.loads(line) for line in (tmp_path/'source-reads.jsonl').read_text().splitlines()]
    assert {row['source'] for row in traces} == {'calendar_fixture'}
    wrong = subprocess.run([sys.executable,str(AGENT/'adapters/sotto/google_api.py'),'calendar','list',
        '--start',(now-timedelta(days=3)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        '--end',(now-timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%SZ')],
        env={**os.environ,'PYTHONPATH':str(HERE/'provider'),'SOTTO_EVAL_WORLD':str(path),'SOTTO_DATA':str(tmp_path)},
        text=True,capture_output=True,timeout=10)
    # The real calendar reader catches a per-calendar refusal into explicit partial coverage.
    # Its out-of-band denied trace still fails the complete conversation evaluator.
    assert json.loads(wrong.stdout)['complete'] is False
    assert json.loads(wrong.stdout)['events'] == []
    assert json.loads((tmp_path/'source-reads.jsonl').read_text().splitlines()[-1])['source'] == 'denied'


def test_prep_fixture_cannot_supply_final_prose(tmp_path):
    world = tmp_path/'world.json';world.write_text('{"meetingPrep":true,"calendarEvents":[]}')
    code = """from prep_sources import research_response
+def deny(reason): raise PermissionError(reason)
+try: research_response({'contents':[]},lambda row:None,deny)
+except PermissionError: pass
+else: raise AssertionError('fixture supplied final prose')
+""".replace('\n+','\n')
    result = subprocess.run([sys.executable,'-c',code],env={**os.environ,'PYTHONPATH':str(HERE/'provider'),
        'SOTTO_EVAL_WORLD':str(world)},text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_profile_fixture_requires_the_actual_sole_ada_selector(tmp_path):
    world = tmp_path/'world.json';world.write_text('{"meetingPrep":true,"calendarEvents":[]}')
    code = """from prep_sources import research_response
import json
def deny(reason): raise PermissionError(reason)
schema={'attendees':{'items':{'properties':{'summary':{}}}}}
def request(address):return {'tools':[{'google_search':{}}], 'contents':[{'parts':[{'text':
 '## Attendees to Research (1 people)\\n- Ada <'+address+'>\\n\\n## Task'}]}],
 'generationConfig':{'responseSchema':{'properties':schema}}}
result=research_response(request('ada@example.invalid'),lambda row:None,deny)
assert result['attendees'][0]['email']=='ada@example.invalid'
try:research_response(request('other@example.invalid'),lambda row:None,deny)
except PermissionError:pass
else:raise AssertionError('wrong person received Ada profile')
"""
    result = subprocess.run([sys.executable,'-c',code],env={**os.environ,'PYTHONPATH':str(HERE/'provider'),
        'SOTTO_EVAL_WORLD':str(world)},text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_worker_gather_requires_connection_then_reaches_native_boundary(tmp_path):
    """The actual worker gather must pass --check before any Calendar/profile read."""
    spec = importlib.util.spec_from_file_location('prep_tokens_full', AGENT/'sotto-chief-of-staff/evals/run_evals.py')
    tokens = importlib.util.module_from_spec(spec);spec.loader.exec_module(tokens)
    scenario = json.loads((HERE/'scenarios/meeting-prep.json').read_text())
    world = tokens.resolve_tokens(scenario['world'], datetime.now(timezone.utc))
    path = tmp_path/'world.json';path.write_text(json.dumps(world))
    data = tmp_path/'data';data.mkdir()
    for relative, content in world['files'].items():
        target=data/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(content)
    home = tmp_path/'home';home.mkdir()
    env = {**os.environ,'PYTHONPATH':str(HERE/'provider'),'SOTTO_EVAL_WORLD':str(path),
           'SOTTO_DATA':str(data),'SOTTO_HOME':str(home),'SOTTO_DEPLOYMENT_MODE':'managed',
           'SOTTO_TENANT_ID':'eval-tenant','SOTTO_USER_EMAIL':'owner@inside.test',
           'SOTTO_GEMINI_MODEL':'gemini-3.8-flash','SOTTO_FALLBACK_MODEL':'',
           'SOTTO_MODEL_PROXY_URL':'https://eval-source.invalid','SOTTO_MODEL_PROXY_TOKEN':'fixture',
           'GOOGLE_AI_API_KEY':'fixture','SOTTO_DELIVERY_RUN_ID':'a'*32,
           'SOTTO_EVAL_NATIVE_PREP_RELAY':'{}'}
    event=world['calendarEvents'][1]
    request={'kind':'shared-meeting-prep','pack':str(AGENT/'sotto-chief-of-staff'),
             'selection':{'mode':'focus','focus':event['summary'],
                          'meeting':{'event_id':event['id'],'start':event['start']['dateTime']}}}
    # Call the real procedure entrypoint, capture its stderr without any model endpoint.
    code='import json,sys;sys.path.insert(0,sys.argv[1]);import procedure_runner;procedure_runner.run(json.loads(sys.argv[2]))'
    argv=[sys.executable,'-c',code,str(AGENT/'runtime/trigger-receiver'),json.dumps(request)]
    missing=subprocess.run(argv,env=env,text=True,capture_output=True,timeout=20)
    assert missing.returncode!=0 and 'Calendar prep source unavailable' in missing.stderr
    assert not (tmp_path/'source-reads.jsonl').exists()
    setup=subprocess.run([sys.executable,'-c','from prep_sources import seed_calendar_connection;import sys;seed_calendar_connection(sys.argv[1])',str(home)],
                         env=env,text=True,capture_output=True,timeout=10)
    assert setup.returncode==0,setup.stderr
    now=datetime.now(timezone.utc)
    bounds={'start':now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'end':(now+timedelta(hours=72)).strftime('%Y-%m-%dT%H:%M:%SZ')}
    typed=subprocess.run([sys.executable,'-c',
        'import json,sys;from sitecustomize import read_owner;print(json.dumps(read_owner("calendar_list",json.loads(sys.argv[1]))))',
        json.dumps(bounds)],env=env,text=True,capture_output=True,timeout=10)
    assert typed.returncode==0,typed.stderr
    selected=next(row for row in json.loads(typed.stdout)['items'] if row['id']==event['id'])
    assert selected['calendar_id']=='owner@inside.test' and selected['id']=='fixturecalendar01'
    request['selection']['meeting']={'event_id':selected['id'],'start':selected['start']['dateTime'],
                                      'calendar_id':selected['calendar_id']}
    argv[-1]=json.dumps(request)
    resumed=subprocess.run(argv,env=env,text=True,capture_output=True,timeout=30)
    assert resumed.returncode!=0  # No native relay is configured: paid composition is impossible.
    records=[json.loads(line) for line in (tmp_path/'source-reads.jsonl').read_text().splitlines()]
    assert any(row['source']=='calendar_fixture' and row.get('ids')==['routinecalendar01','fixturecalendar01']
               and row.get('calendar_id')==selected['calendar_id'] for row in records),resumed.stderr
    assert any(row['source']=='prep_public_profile' for row in records),resumed.stderr
    assert records[-1]=={'source':'denied','reason':'Native prep composition lacks its exact loopback relay'},resumed.stderr


def test_malformed_profile_and_calendar_requests_leave_sticky_refusals(tmp_path):
    world=tmp_path/'world.json';world.write_text('{"meetingPrep":true,"calendarEvents":[]}')
    code="""import json,sys
from prep_sources import CALENDAR_ID,research_response,install_calendar
from sitecustomize import trace,denied,read_owner
for operation in [
 lambda:research_response({'tools':[{'google_search':{}}],'contents':None},trace,denied),
 lambda:research_response({'tools':[{'google_search':{}}],'contents':[{'parts':[{'text':'## Attendees to Research (1 people)\\n- Ada <ada@example.invalid>\\n\\n## Task'}]}],'generationConfig':{'responseSchema':None}},trace,denied),
 lambda:read_owner('calendar_list',{'start':'2026-10-27T00:00:00Z','end':'2026-10-30T00:00:00Z'})]:
 try:operation()
 except PermissionError:pass
 except Exception:pass  # The real reader may catch/refuse; the independent audit must remain.
import gmail_read
for args in [{}, {'timeMin':None,'timeMax':None}]:
 try:gmail_read.google_service('calendar','v3').events().list(calendarId=CALENDAR_ID,**args)
 except PermissionError:pass
install_calendar({'calendarEvents':[{'start':{'dateTime':'2026-10-28T00:00:00Z'}}]},trace,denied)
try:gmail_read.google_service('calendar','v3').events().list(calendarId=CALENDAR_ID,timeMin='2026-10-27T00:00:00Z',timeMax='2026-10-30T00:00:00Z')
except PermissionError:pass
"""
    result=subprocess.run([sys.executable,'-c',code],env={**os.environ,'PYTHONPATH':str(HERE/'provider'),
        'SOTTO_EVAL_WORLD':str(world),'SOTTO_DATA':str(tmp_path)},text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    records=[json.loads(line) for line in (tmp_path/'source-reads.jsonl').read_text().splitlines()]
    assert len([row for row in records if row['source']=='denied'])==6


def test_native_relay_refuses_deceptive_or_malformed_local_urls(tmp_path):
    world=tmp_path/'world.json';world.write_text('{"meetingPrep":true,"calendarEvents":[]}')
    code="""import json,os,urllib.request
for url in ['http://127.0.0.1:9@unexpected.invalid/compose','http://127.0.0.1:bad/compose']:
 os.environ['SOTTO_EVAL_NATIVE_PREP_RELAY']=json.dumps({'url':url,'bearer':'synthetic'})
 try:urllib.request.urlopen(urllib.request.Request('https://eval-source.invalid/native/v1beta/models/gemini-3.8-flash:generateContent',data=b'{"contents":[]}'))
 except PermissionError:pass
 else:raise AssertionError('untrusted relay admitted')
"""
    result=subprocess.run([sys.executable,'-c',code],env={**os.environ,'PYTHONPATH':str(HERE/'provider'),
        'SOTTO_EVAL_WORLD':str(world)},text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    records=[json.loads(line) for line in (tmp_path/'source-reads.jsonl').read_text().splitlines()]
    assert len(records)==2 and all(row['source']=='denied' for row in records)


def test_typed_calendar_get_default_primary_matches_only_the_frozen_primary_calendar(tmp_path):
    world=tmp_path/'world.json'
    world.write_text(json.dumps({'meetingPrep':True,'calendarEvents':[{
        'id':'frozenprimary01','summary':'Frozen primary review',
        'start':{'dateTime':'2026-10-29T17:30:00Z'},'end':{'dateTime':'2026-10-29T18:00:00Z'},
        'organizer':{'email':'owner@inside.test','self':True},
        'attendees':[{'email':'owner@inside.test','self':True},{'email':'ada@example.invalid'}]}]}))
    code="""from sitecustomize import read_owner
from prep_sources import CALENDAR_ID
a=read_owner('calendar_get',{'event_id':'frozenprimary01'})['item']
b=read_owner('calendar_get',{'calendar_id':CALENDAR_ID,'event_id':'frozenprimary01'})['item']
assert 'calendar_id' not in a and b['calendar_id']==CALENDAR_ID
assert {k:v for k,v in a.items() if k!='calendar_id'}=={k:v for k,v in b.items() if k!='calendar_id'}
for args in [{'calendar_id':'unknown@inside.test','event_id':'frozenprimary01'},
             {'event_id':'unknownprimary01'}]:
 try:read_owner('calendar_get',args)
 except PermissionError:pass
 else:raise AssertionError('unknown source scope or occurrence was admitted')
"""
    result=subprocess.run([sys.executable,'-c',code],env={**os.environ,'PYTHONPATH':str(HERE/'provider'),
        'SOTTO_EVAL_WORLD':str(world),'SOTTO_DATA':str(tmp_path),'SOTTO_USER_EMAIL':'owner@inside.test'},
        text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    denied=[json.loads(line) for line in (tmp_path/'source-reads.jsonl').read_text().splitlines()
            if json.loads(line)['source']=='denied']
    assert len(denied)==2
    assert all('Calendar' in row['reason'] for row in denied)
