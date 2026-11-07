"""The eval must detect a provider refusal even when production catches the exception."""
import base64
import json
import ast
import hashlib
import os
from pathlib import Path
import subprocess
import sys


def test_caught_external_read_still_has_failure_evidence(tmp_path):
    here = Path(__file__).resolve().parent
    world = tmp_path / 'world.json'
    world.write_text('{}')
    scripts = here.parents[2] / 'sotto-chief-of-staff/_shared/scripts'
    result = subprocess.run([sys.executable, '-c',
        'import sys,json;sys.path.insert(0,sys.argv[1]);import web_research;'
        'print(json.dumps(web_research.research("Dana Reed Cedar Labs")))', str(scripts)],
        env={**os.environ, 'PYTHONPATH': str(here / 'provider'), 'SOTTO_EVAL_WORLD': str(world),
             'SOTTO_DATA': str(tmp_path), 'SOTTO_MODEL_PROXY_URL': 'https://eval-source.invalid',
             'SOTTO_MODEL_PROXY_TOKEN': 'fixture', 'GOOGLE_AI_API_KEY': 'fixture',
             'EXA_API_KEY': '', 'PARALLEL_API_KEY': ''},
        text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['error']  # Production deliberately catches the provider error.
    records = [json.loads(line) for line in (tmp_path / 'source-reads.jsonl').read_text().splitlines()]
    assert records and all(record['source'] == 'denied' for record in records)


def test_receiver_refuses_existing_data_before_importing_runtime(tmp_path):
    here = Path(__file__).resolve().parent
    world = tmp_path / 'world.json'
    world.write_text('{}')
    data = tmp_path / 'data'
    data.mkdir()
    sentinel = data / 'existing.txt'
    sentinel.write_text('preserve')
    result = subprocess.run([sys.executable, str(here / 'receiver.py'), str(world), str(data)],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert 'empty, explicit data directory' in result.stderr
    assert sentinel.read_text() == 'preserve'


def test_commitment_replay_fixture_uses_exact_verified_rows_and_real_google_grants(tmp_path):
    here = Path(__file__).resolve().parent
    pack = here.parents[2] / 'sotto-chief-of-staff'
    sys.path.insert(0, str(here.parents[1] / 'trigger-receiver'))
    import runtime_knowledge_read  # noqa: PLC0415 - explicit local receiver path selected above
    manifest = json.loads((here.parent/'test/fixtures/commitment-history-replay-provenance.json').read_text())
    # Evaluate the harness's actual fixture-grant adapter without starting providers or a model.
    source = ast.parse((here/'receiver.py').read_text())
    start = next(i for i,node in enumerate(source.body) if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == 'saved_grants' for target in node.targets))
    finish = next(i for i,node in enumerate(source.body[start:], start)
                  if isinstance(node, ast.FunctionDef) and node.name == 'authority')
    grants_code = compile(ast.Module(body=source.body[start:finish+1], type_ignores=[]), str(here/'receiver.py'), 'exec')
    for name in ('commitment-history', 'commitment-history-revoked'):
        scenario = json.loads((here/'scenarios'/f'{name}.json').read_text())
        world = scenario['world']
        namespace = {'world':world}
        exec(grants_code, namespace)
        authority = namespace['authority']
        assert 'google_contacts' in authority()['google_sources']
        assert authority()['bridge_sources'] == ['imessage']
        directory = tmp_path/name
        for relative, content in world['files'].items():
            path = directory/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        for relative, digest in manifest['files_sha256'].items():
            assert hashlib.sha256(world['files'][relative].encode()).hexdigest() == digest
        original = {path:path.read_bytes() for path in directory.rglob('*.md')}
        result = runtime_knowledge_read.perform(
            {'operation':'knowledge_query', 'arguments':{'argv':['--person','Maya Journey','--person-coverage']},
             'source_revision':'a'*64}, script=str(pack/'_shared/knowledge/knowledge_query.py'),
            executable=sys.executable, env={**os.environ, 'SOTTO_DATA':str(directory)}, authority=authority)
        history = result['obligation_history']
        if name.endswith('-revoked'):
            assert 'gmail' not in authority()['google_sources']
            assert history['status'] == 'no_match' and history['items'] == []
        else:
            assert history['status'] == 'matched' and len(history['items']) == 2
            closed = next(row for row in history['items'] if row['status'] == 'resolved')
            pending = next(row for row in history['items'] if row['status'] == 'open')
            assert closed['ask'] == 'send the pricing document tomorrow'
            assert closed['completion_basis'] == 'source_evidence'
            assert pending['ask'] == 'send the launch checklist tomorrow'
            assert not pending['resolution_evidence'] and pending['resolution_source_refs'] == []
            assert closed['deadline'] == pending['deadline'] == '2026-10-19'
            assert scenario['turns'][1]['contains'] == ['checklist','19']
            assert 'not delivered' in scenario['turns'][1]['containsAny'][0]
        assert {path:path.read_bytes() for path in directory.rglob('*.md')} == original


def test_website_fixture_never_accepts_a_url_prefix(tmp_path):
    here = Path(__file__).resolve().parent
    world = tmp_path / 'world.json'
    world.write_text(json.dumps({'websites': {'https://cedar.example/': 'Public facts'}}))
    script = """import urllib.request,json
request=urllib.request.Request('https://eval-source.invalid/native/model',data=json.dumps({
 'contents':[{'parts':[{'text':'Read https://cedar.example/leak?private=data'}]}],
 'tools':[{'url_context':{}}]}).encode())
try: urllib.request.urlopen(request)
except PermissionError: pass
else: raise AssertionError('A prefix matched the wrong source')
"""
    result = subprocess.run([sys.executable, '-c', script],
        env={**os.environ, 'PYTHONPATH': str(here / 'provider'), 'SOTTO_EVAL_WORLD': str(world)},
        text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in (tmp_path / 'source-reads.jsonl').read_text().splitlines()]
    assert records == [{'source': 'denied', 'reason': 'Website fixture missing or ambiguous'}]


def test_frozen_gmail_fixture_uses_real_projection_and_refuses_unknown_ids(tmp_path):
    here = Path(__file__).resolve().parent
    world = tmp_path / 'world.json'
    world.write_text(json.dumps({'gmailMessages': {'mail1': {
        'id': 'mail1', 'threadId': 'thread1', 'internalDate': '1792207260000', 'labelIds': ['SENT'],
        'payload': {'mimeType': 'text/plain', 'headers': [{'name': 'Date', 'value': 'Fri, 16 Oct 2026 20:21:00 -0700'}],
                    'body': {'data': base64.urlsafe_b64encode(b'Approved.').decode()}}}}}))
    code = '''import sitecustomize,json,sys,importlib.util,pathlib
# Import only the shared projection module, then poison its credential builders.
lib=pathlib.Path(sitecustomize.__file__).resolve().parents[4]/'sotto-chief-of-staff/_shared/lib'
sys.path.insert(0,str(lib))
import gmail_read
def forbidden(*args,**kwargs): raise AssertionError('live credential builder reached')
gmail_read.token_path=forbidden
gmail_read.google_service=forbidden
# The receiver dynamically imports the fixture per read; patches must stay singular.
wrapper=sitecustomize.urllib.request.urlopen
hooks=[]
original_add=sys.addaudithook
sys.addaudithook=lambda hook: hooks.append(hook)
for index in range(2):
 spec=importlib.util.spec_from_file_location('fixture_repeat'+str(index),sitecustomize.__file__)
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
assert sitecustomize.urllib.request.urlopen is wrapper and hooks==[]
sys.addaudithook=original_add
for operation in ['calendar_list','contacts_get','gmail_send','draft']:
 try: module.read_owner(operation,{})
 except PermissionError: pass
 else: raise AssertionError('unsupported operation accepted')
result=sitecustomize.read_owner('gmail_get', {'message_id':'mail1'})
thread=sitecustomize.read_owner('gmail_thread_get', {'thread_id':'thread1'})
assert thread['items'][0]['id']=='mail1' and thread['items'][0]['body']=='Approved.'
try: sitecustomize.read_owner('gmail_thread_get', {'thread_id':'unknown'})
except PermissionError: pass
else: raise AssertionError('unknown thread accepted')
print(json.dumps(result))
try: sitecustomize.read_owner('gmail_get', {'message_id':'unknown'})
except PermissionError: pass
else: raise AssertionError('unknown source accepted')
'''
    result = subprocess.run([sys.executable, '-c', code],
        env={**os.environ, 'PYTHONPATH': str(here / 'provider'), 'SOTTO_EVAL_WORLD': str(world)},
        text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    item = json.loads(result.stdout)['item']
    assert item['id'] == 'mail1' and item['threadId'] == 'thread1'
    assert item['body'] == 'Approved.'
    assert '20:21' in item['date']
    records = [json.loads(line) for line in (tmp_path / 'source-reads.jsonl').read_text().splitlines()]
    assert [row['operation'] for row in records if row['source'] == 'gmail_fixture'] == ['gmail_get', 'gmail_thread_get', 'gmail_thread_get', 'gmail_get']
    assert records[-1]['source'] == 'denied'
    assert [row['arguments']['message_id'] for row in records if row['source'] == 'gmail_fixture' and row['operation']=='gmail_get'] == ['mail1', 'unknown']
    cli = subprocess.run([sys.executable, str(here / 'provider/sitecustomize.py'), 'gmail', 'send'],
        env={**os.environ, 'SOTTO_EVAL_WORLD': str(world)}, text=True, capture_output=True, timeout=10)
    assert cli.returncode != 0
    assert 'supports read_owner only' in cli.stderr


def test_frozen_gmail_search_marks_bounded_inventory_partial(tmp_path):
    here = Path(__file__).resolve().parent
    world = tmp_path / 'world.json'
    world.write_text(json.dumps({'gmailMessages': {
        key: {'id': key, 'threadId': 'thread1', 'payload': {'headers': []}}
        for key in ('mail1', 'mail2')}}))
    result = subprocess.run([sys.executable, '-c',
        "import sitecustomize,json;print(json.dumps(sitecustomize.read_owner('gmail_search',{'query':'synthetic','max':1})))"],
        env={**os.environ, 'PYTHONPATH': str(here / 'provider'), 'SOTTO_EVAL_WORLD': str(world)},
        text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert len(value['items']) == 1 and value['observation']['status'] == 'partial'
    assert value['observation']['complete'] is False


def test_triage_world_runs_original_gather_and_queue_without_live_sources(tmp_path):
    here = Path(__file__).resolve().parent
    scenario = json.loads((here / 'scenarios/current-action-selection.json').read_text())
    world = tmp_path / 'world.json'
    world.write_text(json.dumps(scenario['world']))
    code = '''import importlib.util,json,os,pathlib,sys,types,subprocess
fixture=pathlib.Path(sys.argv[1])
s=importlib.util.spec_from_file_location('eval_triage',fixture);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
receiver=types.SimpleNamespace(RUNTIME_KNOWLEDGE_READ=types.SimpleNamespace(subprocess=subprocess))
m.install_triage(receiver)
script=fixture.parents[4]/'sotto-chief-of-staff/_shared/scripts/triage_read.py'
result=subprocess.run([sys.executable,'-I','-B',str(script)],env=dict(os.environ),capture_output=True,text=True,timeout=40)
assert result.returncode==0,result.stderr
value=json.loads(result.stdout)
assert [row['threadId'] for row in value['email']]==['ellis','mira','dana'],value
assert value['email'][0]['message_id']=='ellis-ack'
assert value['coverage']['sources'][0]['status']=='ok',value
print(json.dumps(value))
# A refused HTTP call remains recorded even when the caller catches it.
import httplib2
for url in ('https://gmail.googleapis.com/gmail/v1/users/me/messages/unknown',
            'https://gmail.googleapis.com/gmail/v1/users/me/messages?q=unrecognized',
            'https://gmail.googleapis.com/gmail/v1/users/me/messages/dana-ask/attachments/unknown'):
 try: httplib2.Http().request(url)
 except PermissionError: pass
 else: raise AssertionError('unknown Google request accepted')
'''
    result = subprocess.run([sys.executable, '-c', code, str(here / 'provider/sitecustomize.py')],
        env={**os.environ, 'SOTTO_EVAL_WORLD': str(world), 'SOTTO_DATA': str(tmp_path / 'data'),
             'SOTTO_HOME': str(tmp_path / 'home'), 'SOTTO_CAPTURED_SOURCES': '["gmail"]'},
        text=True, capture_output=True, timeout=50)
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in (tmp_path / 'source-reads.jsonl').read_text().splitlines()]
    reads = [row for row in records if row['source'] == 'gmail_triage_fixture']
    assert any(row['query'].get('q') == ['newer_than:1d'] for row in reads)
    assert any(row['query'].get('q') == ['in:sent newer_than:1d'] for row in reads)
    assert len([row for row in records if row['source'] == 'denied']) == 3
