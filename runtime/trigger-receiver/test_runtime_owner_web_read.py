"""Literal owner URL through original provider ladder and durable contact receipt, offline."""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
import receiver as rec
import runtime_owner_web_read as tool
from test_runtime_owner_loop_close import context, IDENTITY, ROOT, WHEN, THREAD, capability

__all__ = ['context']
URL = 'https://example.com/read'


def request(*, event='d' * 64, operation='execute', revision='a' * 64, text=None, url=URL):
    text = text or 'Read ' + url
    args = {'url': url, 'source_revision': revision}
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    action = hashlib.sha256(json.dumps([event, 'owner_web_read', canonical], ensure_ascii=False,
                                      separators=(',', ':')).encode()).hexdigest()
    return dict(version=1, operation=operation, identity=IDENTITY, action_id=action, event_id=event,
        payload_hash='b' * 64, text_hash=hashlib.sha256(text.encode()).hexdigest(), text=text,
        admitted_at_ms=int(WHEN.timestamp() * 1000), provider_thread_id=THREAD, args=args)


@pytest.fixture
def harness(context, monkeypatch):
    data, policy, activation = context
    reader = rec.RUNTIME_WEB_READER
    web = reader._web()
    policy.update(SOTTO_RUNTIME_PROFILE='managed-photon-fixed-five-v1', SOTTO_DATA=str(data))
    state = {'complete': True, 'revision': 'a' * 64}
    policy['_source_authority'] = lambda: dict(state)
    monkeypatch.setattr(reader.time, 'time', lambda: WHEN.timestamp())
    for key in ('EXA_API_KEY', 'PARALLEL_API_KEY', 'GOOGLE_AI_API_KEY', 'BROWSER_USE_API_KEY', 'SOTTO_LLM_STUB'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.synthetic.invalid')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'synthetic')
    monkeypatch.setattr(web, 'BROWSERUSE_POLL_SECS', 0)
    def call(body, **kwargs):
        return tool.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT, policy=policy,
            data_root=data, capability=capability, activation=activation, reader=reader, **kwargs)
    return data, reader, web, state, call


def test_actual_exa_to_gemini_receipt_parent_usage_and_restart_no_refetch(harness, monkeypatch):
    data, reader, web, state, call = harness
    monkeypatch.setenv('EXA_API_KEY', 'synthetic')
    calls = []
    def post(url, body, headers, timeout, method='POST'):
        calls.append((url, headers))
        if '/contents' in url:
            return {'results': []}
        assert headers['X-Sotto-Operation'] and headers['X-Sotto-Parent-Operation']
        return {'candidates': [{'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': URL, 'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]}, 'content': {'parts': [{'text': 'Page title\nActual page evidence'}]}}],
                'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 8}}
    monkeypatch.setattr(web, '_post', post)
    receipt = call(request())
    assert receipt['status'] == 'committed' and receipt['outcome']['result']['provider'] == 'gemini'
    assert receipt['outcome']['result']['text'] == 'Source-derived summary (not verbatim page text):\n' + 'Page title\nActual page evidence'
    assert len(calls) == 2
    checkpoint = json.loads(next((Path(data) / 'events/research-artifacts').glob('web-*.json')).read_text())
    with sqlite3.connect(str(Path(data) / 'events/model-work.sqlite3')) as db:
        rows = db.execute('SELECT metadata,status,usage FROM attempts').fetchall()
    assert len(rows) == 1 and rows[0][1] == 'succeeded'
    assert json.loads(rows[0][0])['parent'] == checkpoint['model_operation']
    assert json.loads(rows[0][2])['input_tokens'] == 12
    assert call(dict(request(), operation='inspect')) == receipt
    assert call(request()) == receipt and len(calls) == 2
    state['revision'] = 'c' * 64
    assert call(dict(request(), operation='inspect'))['code'] == 'source_unavailable'
    assert call(request(revision='c' * 64))['code'] == 'event_conflict' and len(calls) == 2


def test_browser_known_session_cleanup_unknown_inspect_only(harness, monkeypatch):
    data, reader, web, state, call = harness
    monkeypatch.setenv('BROWSER_USE_API_KEY', 'synthetic')
    # Gemini is configured by the harness but returns no text, then original browser runs.
    calls = []
    fail_stop = [True]
    def post(url, body, headers, timeout, method='POST'):
        calls.append((url, method))
        if '/generateContent' in url or ':generateContent' in url:
            return {'candidates': []}
        if url.endswith('/tasks'):
            return {'id': 'task', 'sessionId': 'session'}
        assert url.endswith('/sessions/session') and method == 'PATCH'
        if fail_stop[0]:
            raise TimeoutError('private transport message')
        return {'status': 'stopped'}
    monkeypatch.setattr(web, '_post', post)
    monkeypatch.setattr(web, '_get', lambda *args: {'status': 'finished', 'output': 'Browser actual page'})
    assert call(request()) == {'version': 1, 'status': 'unknown'}
    assert len([url for url, _ in calls if url.endswith('/tasks')]) == 1
    fail_stop[0] = False
    state['revision'] = 'c' * 64  # cleanup is permitted without redisclosing cached page content
    assert call(dict(request(), operation='inspect')) == {'version': 1, 'status': 'unknown'}
    assert call(request()) == {'version': 1, 'status': 'unknown'}
    assert len([url for url, _ in calls if url.endswith('/tasks')]) == 1
    checkpoint = json.loads(next((Path(data) / 'events/research-artifacts').glob('web-*.json')).read_text())
    assert checkpoint['session_status'] == 'stop_accepted' and checkpoint['status'] == 'contacting'


def test_unknown_create_never_retry_or_claim_empty(harness, monkeypatch):
    data, reader, web, state, call = harness
    monkeypatch.setenv('BROWSER_USE_API_KEY', 'synthetic')
    calls = []
    def post(url, *args, **kwargs):
        if url.endswith('/tasks'):
            calls.append(url)
            raise TimeoutError('provider may have accepted')
        return {'candidates': []}
    monkeypatch.setattr(web, '_post', post)
    assert call(request()) == {'version': 1, 'status': 'unknown'}
    assert call(dict(request(), operation='inspect')) == {'version': 1, 'status': 'unknown'}
    assert call(request()) == {'version': 1, 'status': 'unknown'}
    assert len(calls) == 1


def test_null_revision_offline_sources_and_old_admission(harness, monkeypatch):
    data, reader, web, state, call = harness
    state.update(complete=False, revision='b' * 64)
    monkeypatch.setattr(web, '_post', lambda *args, **kwargs: {'candidates': [
        {'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': URL, 'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]}, 'content': {'parts': [{'text': 'Actual public page'}]}}]})
    assert call(request(revision=None))['status'] == 'committed'
    monkeypatch.setattr(reader.time, 'time', lambda: WHEN.timestamp() + 2 * 86400)
    assert call(request(event='e' * 64, revision=None))['code'] == 'unavailable'


@pytest.mark.parametrize('url', ['https://127.1', 'http://0x7f000001', 'http://127.0.0.1',
    'http://[::1]', 'https://user@example.com', 'https://localhost', 'https://intranet.local',
    'https://docsend.com/view/a', 'https://x.docsend.com/a', 'file:///tmp/x', 'https://example.1'])
def test_local_identity_docsend_paths_refused(url):
    with pytest.raises(ValueError):
        tool.validate_args({'url': url, 'source_revision': None}, 'Read ' + url)


def test_exact_normalized_complete_token_not_prefix():
    assert tool.owner_urls('Read (HTTPS://example.com/a(b)).') == ['HTTPS://example.com/a(b)']
    assert tool.owner_urls('https://example.com/q?x=1,2') == ['https://example.com/q?x=1,2']
    with pytest.raises(ValueError):
        tool.validate_args({'url': 'https://example.com', 'source_revision': None}, 'Read https://example.com/private')


def test_owner_question_allows_only_empty_query_delimiter_omission():
    for text in ('Quick summary of ' + URL + '?', 'Quick summary of <' + URL + '?>'):
        tool.validate_args({'url': URL, 'source_revision': None}, text)
        tool.validate_args({'url': URL + '?', 'source_revision': None}, text)
    for suffix in ('?why?', '?q=why?', '??', '#section?', '?q=%3F', '%3F'):
        exact = URL + suffix
        tool.validate_args({'url': exact, 'source_revision': None}, 'Read ' + exact)
        for changed in {URL, exact[:-1]} if suffix.endswith('?') else {URL}:
            with pytest.raises(ValueError):
                tool.validate_args({'url': changed, 'source_revision': None}, 'Read ' + exact)
    with pytest.raises(ValueError):
        tool.validate_args({'url': URL + '?', 'source_revision': None}, 'Read ' + URL)
    with pytest.raises(ValueError):
        tool.validate_args({'url': URL, 'source_revision': None}, 'Read ' + URL + '/other?')
    reply_target = dict(accepted_dispatch_id='accepted', provider_message_id='message',
                        text='Read ' + URL + '?', source_revision='a' * 64)
    tool.validate_args(dict(url=URL, source_revision='a' * 64, reply_target=reply_target), 'Summarize it')
    with pytest.raises(ValueError):
        tool.validate_args(dict(url=URL, source_revision='a' * 64, reply_target=reply_target), 'Read https://elsewhere.com')


@pytest.mark.parametrize('suffix', ['', '?'])
def test_owner_question_receipt_retains_exact_args_and_lost_receipt_inspection(harness, monkeypatch, suffix):
    data, reader, web, state, call = harness
    selected = URL + suffix
    calls = []
    def post(_url, body, *args, **kwargs):
        calls.append(body)
        return {'candidates': [{'urlContextMetadata': {'urlMetadata': [
            {'retrievedUrl': selected, 'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]},
            'content': {'parts': [{'text': 'Actual synthetic page'}]}}]}
    monkeypatch.setattr(web, '_post', post)
    body = request(url=selected, text='Quick summary of ' + URL + '?')
    receipt = call(body)
    assert receipt['status'] == 'committed'
    assert receipt['outcome']['result']['url'] == selected
    assert receipt['action_id'] == body['action_id']
    checkpoint_path = next((Path(data) / 'events/research-artifacts').glob('web-*.json'))
    checkpoint = json.loads(checkpoint_path.read_text())
    assert checkpoint['proof']['args'] == body['args']
    assert checkpoint['proof']['text_hash'] == body['text_hash']
    assert call(dict(body, operation='inspect')) == receipt
    assert call(body) == receipt and len(calls) == 1
    alternate = URL if suffix else URL + '?'
    assert call(request(url=alternate, text=body['text']))['code'] == 'event_conflict'
    state['revision'] = 'c' * 64
    assert call(dict(body, operation='inspect'))['code'] == 'source_unavailable'
    assert len(calls) == 1


def test_browser_hook_failure_and_cancel_still_stop_known_session(harness, monkeypatch):
    data, reader, web, state, call = harness
    calls = []
    monkeypatch.setattr(web, '_post', lambda url, *a, **k: calls.append(url) or (
        {'id': 'task', 'sessionId': 'session'} if url.endswith('/tasks') else {}))
    monkeypatch.setattr(web, '_get', lambda *a, **k: pytest.fail('no poll after cancellation'))
    def failed_hook(event, session):
        if event == 'created':
            raise OSError('checkpoint write failed')
    with pytest.raises(OSError):
        web._browseruse_fetch(URL, 5, lifecycle=failed_hook)
    assert calls[-1].endswith('/sessions/session')
    with pytest.raises(web.WebReadHeldError):
        web._browseruse_fetch(URL, 5, lifecycle=lambda *a: None, cancelled=lambda: True)
    assert calls[-1].endswith('/sessions/session')


@pytest.mark.parametrize('response', [None, [], {'id': 'task'}, {'id': 7, 'sessionId': 'session'}])
def test_malformed_paid_create_is_held_with_known_session_cleanup(harness, monkeypatch, response):
    data, reader, web, state, call = harness
    monkeypatch.setenv('BROWSER_USE_API_KEY', 'synthetic')
    calls = []
    def post(url, *args, **kwargs):
        calls.append(url)
        return response if url.endswith('/tasks') else ({'candidates': []} if ':generateContent' in url else {})
    monkeypatch.setattr(web, '_post', post)
    assert call(request()) == {'version': 1, 'status': 'unknown'}
    assert call(dict(request(), operation='inspect')) == {'version': 1, 'status': 'unknown'}
    assert len([url for url in calls if url.endswith('/tasks')]) == 1
    if isinstance(response, dict) and response.get('sessionId') == 'session':
        assert len([url for url in calls if url.endswith('/sessions/session')]) == 1


def test_fresh_authority_change_withholds_result_and_corrupt_receipt_refuses(harness, monkeypatch):
    data, reader, web, state, call = harness
    def post(*args, **kwargs):
        state['revision'] = 'c' * 64
        return {'candidates': [{'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': URL, 'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]}, 'content': {'parts': [{'text': 'Actual result'}]}}]}
    monkeypatch.setattr(web, '_post', post)
    assert call(request())['code'] == 'source_unavailable'
    state['revision'] = 'a' * 64
    assert call(dict(request(), operation='inspect'))['outcome']['result']['text'] == 'Source-derived summary (not verbatim page text):\n' + 'Actual result'
    path = next((Path(data) / 'events/research-artifacts').glob('web-*.json'))
    saved = json.loads(path.read_text()); saved['receipt']['outcome']['result']['url'] = 'https://elsewhere.com'
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError):
        call(dict(request(), operation='inspect'))


def test_cancellation_before_new_rung_never_starts_browser(harness, monkeypatch):
    data, reader, web, state, call = harness
    monkeypatch.setenv('BROWSER_USE_API_KEY', 'synthetic')
    checks = iter([False, False, True])  # precontact, Gemini, then browser continuation
    calls = []
    def post(url, *args, **kwargs):
        calls.append(url)
        assert not url.endswith('/tasks')
        return {'candidates': []}
    monkeypatch.setattr(web, '_post', post)
    assert call(request(), cancelled=lambda: next(checks)) == {'version': 1, 'status': 'unknown'}
    assert len(calls) == 1


DECK_URL = 'https://docsend.com/view/deckA'


def deck_request(args=None, *, event='e'*64, operation='execute', text=None):
    args = args or {'operation':'docsend','url':DECK_URL,'source_revision':'a'*64}
    text = text or 'Read ' + DECK_URL
    body = request(event=event, operation=operation, text=text)
    encoded = json.dumps(args,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    body.update(args=args,action_id=hashlib.sha256(json.dumps([event,'owner_web_read',encoded],
                ensure_ascii=False,separators=(',',':')).encode()).hexdigest())
    return body


@pytest.fixture
def deck_harness(harness, monkeypatch):
    data, reader, web, state, call = harness
    deck = reader._deck()
    monkeypatch.setenv('SOTTO_USER_EMAIL','owner@example.com')
    monkeypatch.setenv('SOTTO_DATA',str(data))
    contacts = []
    class Http:
        def __init__(self,url,guard):
            self.guard=guard
        def remaining(self):
            self.guard()
            return 120
        def get(self,url,accept='text/html'):
            self.guard()
            contacts.append(('GET',url))
            if '/page_data/1' in url:
                return b'{"imageUrl":"https://images.example/page.jpg"}'
            if '/page_data/' in url:
                raise RuntimeError('end')
            if accept=='image/*':
                return b'not JPEG'
            return b'<title>Founder deck | DocSend</title>'
        def post_form(self,*args):
            self.guard()
            contacts.append(('POST',args[0]))
            return b'ok'
    monkeypatch.setattr(deck,'AttendedHttp',Http)
    monkeypatch.setattr(deck,'_gemini_read',lambda *args,**kwargs:'Page 1: Actual deck evidence')
    return data,reader,deck,state,call,contacts


@pytest.mark.parametrize('prefix', ['Do not open ', "Don't read ", "dont open ", "Read without a passcode: ", 'Skip '])
def test_declined_docsend_never_enters_reader_or_contacts_deck(deck_harness, monkeypatch, prefix):
    data, reader, deck, state, call, contacts = deck_harness
    with monkeypatch.context() as fence:
        fence.setattr(reader, 'perform', lambda *args, **kwargs: pytest.fail('declined read reached reader'))
        with pytest.raises(ValueError, match='web read was cancelled'):
            call(deck_request(text=prefix + DECK_URL))
    assert contacts == []
    receipt = call(deck_request(event='f' * 64, text='Read ' + DECK_URL))
    assert receipt['status'] == 'committed'
    assert receipt['outcome']['result']['status'] == 'ok'
    assert contacts[0] == ('GET', DECK_URL)


def test_docsend_original_read_cache_and_inspect_no_second_view(deck_harness):
    data,reader,deck,state,call,contacts=deck_harness
    receipt=call(deck_request())
    assert receipt['status']=='committed'
    result=receipt['outcome']['result']
    assert result==dict(status='ok',url=DECK_URL,title='Founder deck',pages=1,
                       text=result['text'],cached=False,pdf='')
    assert 'Full-deck completeness is unknown' in result['text']
    assert result['text'].endswith('Page 1: Actual deck evidence')
    count=len(contacts)
    assert call(dict(deck_request(),operation='inspect'))==receipt
    again=call(deck_request(event='f'*64))
    assert again['outcome']['result']['cached'] is True and len(contacts)==count
    assert again['outcome']['result']['text'] == result['text']
    state['revision']='c'*64
    assert call(dict(deck_request(),operation='inspect'))['code']=='source_unavailable'


def test_docsend_confirm_fresh_once_and_changed_email_refusal(deck_harness,monkeypatch):
    data,reader,deck,state,call,contacts=deck_harness
    original={'operation':'propose_docsend','url':DECK_URL,'fresh':True,'source_revision':'a'*64}
    body=deck_request(original,operation='preview')
    preview=call(body)
    assert preview['status']=='preview' and not contacts
    proposal={k:body[k] for k in ('event_id','action_id')}
    proposal.update(args_hash=hashlib.sha256(reader._canonical(original).encode()).hexdigest(),
                    accepted_dispatch_id='accepted',source_revision='a'*64)
    args={'operation':'confirm_docsend','review':preview['review'],'proposal':proposal}
    confirm=deck_request(args,event='f'*64,text='Yes')
    monkeypatch.setenv('SOTTO_USER_EMAIL','changed@example.com')
    assert call(confirm)['code']=='target_changed' and not contacts
    monkeypatch.setenv('SOTTO_USER_EMAIL','owner@example.com')
    assert call(confirm)['status']=='committed'
    assert call(deck_request(args,event='1'*64,text='Yes'))['code']=='proposal_consumed'


def test_docsend_contact_loss_held_and_page_budget_does_not_return_partial(deck_harness,monkeypatch):
    data,reader,deck,state,call,contacts=deck_harness
    class Lost:
        def __init__(self,url,guard): pass
        def get(self,*args,**kwargs):
            contacts.append(('GET',DECK_URL))
            raise deck.DocSendHeldError('private exception')
    monkeypatch.setattr(deck,'AttendedHttp',Lost)
    assert call(deck_request())['status']=='unknown'
    assert call(dict(deck_request(),operation='inspect'))['status']=='unknown'
    assert call(deck_request())['status']=='unknown' and len(contacts)==1
    class Partial:
        def get(self,url,accept='text/html'):
            if '/page_data/2' in url:
                raise deck.DocSendHeldError('budget')
            if '/page_data/' in url:
                return b'{"imageUrl":"https://images.example/page.jpg"}'
            return b'<title>Deck</title>' if accept=='text/html' else b'image'
    with pytest.raises(deck.DocSendHeldError):
        deck.fetch_deck(DECK_URL,'owner@example.com',http=Partial(),vision=lambda *a:'partial')


def test_docsend_same_id_other_identity_refuses_without_contact(deck_harness):
    data,reader,deck,state,call,contacts=deck_harness
    assert call(deck_request())['status']=='committed'
    count=len(contacts)
    url=DECK_URL+'?different=1'
    args={'operation':'docsend','url':url,'source_revision':'a'*64}
    assert call(deck_request(args,event='f'*64,text='Read '+url))['code']=='target_changed'
    assert len(contacts)==count


def test_docsend_transport_same_origin_gate_redirect_no_asset_credentials(deck_harness,monkeypatch):
    import http.client
    import socket
    import io
    data,reader,deck,state,call,contacts=deck_harness
    # Restore actual image-owned transport; only the socket/resource boundary is synthetic.
    spec=__import__('importlib.util',fromlist=['spec_from_file_location'])
    module_spec=spec.spec_from_file_location('transport_original',deck.__file__)
    actual=spec.module_from_spec(module_spec)
    module_spec.loader.exec_module(actual)
    responses=[(303,{'Location':'/view/deckA','Set-Cookie':'gate=token'},b''),
               (200,{},b'ok'),(200,{},b'image')]
    wire=[]
    class Response:
        def __init__(self,status,headers,body):
            self.status=status; self.headers=headers; self.body=io.BytesIO(body)
        def getheaders(self): return list(self.headers.items())
        def getheader(self,key): return self.headers.get(key)
        def read1(self,n): return self.body.read(n)
    class Connection:
        def __init__(self,host,timeout): self.host=host; self.sock=None
        def request(self,method,path,data,headers): wire.append((self.host,method,path,data,headers))
        def getresponse(self): return Response(*responses.pop(0))
        def close(self): pass
    monkeypatch.setattr(http.client,'HTTPSConnection',Connection)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])
    transport=actual.AttendedHttp(DECK_URL,lambda:None)
    assert transport.post_form(DECK_URL,{'email':'owner@example.com'},DECK_URL)==b'ok'
    assert wire[0][1]=='POST' and wire[1][1]=='GET' and wire[1][3] is None
    assert wire[1][4]['Cookie']=='gate=token'
    transport.get('https://images.example/page.jpg',accept='image/*')
    assert 'Cookie' not in wire[2][4] and 'Referer' not in wire[2][4]
    assert all(row[4]['User-Agent']=='OpenAI File Downloader, XaiImageApiFetch/1.0' for row in wire)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    with pytest.raises(actual.DocSendHeldError):
        transport.get(DECK_URL)
    assert len(wire)==3


def test_docsend_gate_passcode_and_verification_preserve_distinct_hints(deck_harness,monkeypatch):
    data,reader,deck,state,call,contacts=deck_harness
    class Gate:
        def __init__(self,url,guard): self.guard=guard
        def get(self,*args,**kw):
            self.guard()
            return b'<form><input name="link_auth_form[email]"><input name="authenticity_token" value="token"></form>'
        def post_form(self,url,fields,referer):
            contacts.append(fields)
            assert fields['link_auth_form[email]']=='owner@example.com'
    monkeypatch.setattr(deck,'AttendedHttp',Gate)
    result=call(deck_request())['outcome']['result']
    assert result['step']=='gate' and 'email verification' in result['error']
    assert 'passcode' not in result['error']


def test_docsend_completed_authority_bookend_and_passcode_substitution(deck_harness,monkeypatch):
    data,reader,deck,state,call,contacts=deck_harness
    receipt=call(deck_request())
    assert receipt['status']=='committed'
    # Existing callback supplied by harness is closed over state; change it between saved sync and return.
    original_sync=reader._sync
    def sync(path):
        original_sync(path)
        state['revision']='c'*64
    monkeypatch.setattr(reader,'_sync',sync)
    assert call(dict(deck_request(),operation='inspect'))['code']=='source_unavailable'
    with pytest.raises(ValueError):
        tool.validate_args({'operation':'docsend','url':DECK_URL,'source_revision':'a'*64,'passcode':'invented'},'Read '+DECK_URL)


def test_docsend_actual_jpeg_saved_pdf_authenticated_dashboard(deck_harness,monkeypatch):
    from test_dashboard import _server, _login, _get, _opener
    data,reader,deck,state,call,contacts=deck_harness
    jpeg=(b'\xff\xd8'+b'\xff\xc0\x00\x11\x08'+(100).to_bytes(2,'big')
          +(200).to_bytes(2,'big')+b'\x03'+b'\x01\x11\x00\x02\x11\x00\x03\x11\x00'+b'\xff\xd9')
    base_http=deck.AttendedHttp
    class Jpeg(base_http):
        def get(self,url,accept='text/html'):
            if accept=='image/*':
                self.guard()
                contacts.append(('GET',url))
                return jpeg
            return super().get(url,accept)
    monkeypatch.setattr(deck,'AttendedHttp',Jpeg)
    result=call(deck_request())['outcome']['result']
    assert result['pdf']=='/api/decks/deckA.pdf'
    saved=(Path(data)/'decks/deckA.pdf').read_bytes()
    assert saved.startswith(b'%PDF-') and jpeg in saved
    module,server,base=_server(data)
    try:
        assert _get(base,result['pdf'])[0]==401
        import urllib.request
        request=urllib.request.Request(base+result['pdf'],headers=dict(_login(base), **{'User-Agent':deck.UA}))
        with _opener.open(request,timeout=10) as response:
            assert response.status==200 and response.read()==saved
            assert response.headers['Content-Type']=='application/pdf'
    finally:
        server.shutdown()


def test_docsend_revocation_or_viewer_change_after_contact_stays_unknown(deck_harness,monkeypatch):
    data,reader,deck,state,call,contacts=deck_harness
    base_http=deck.AttendedHttp
    class Changed(base_http):
        def get(self,url,accept='text/html'):
            result=super().get(url,accept)
            monkeypatch.setenv('SOTTO_USER_EMAIL','different@example.com')
            return result
    monkeypatch.setattr(deck,'AttendedHttp',Changed)
    assert call(deck_request())['status']=='unknown'
    assert call(dict(deck_request(),operation='inspect'))['status']=='unknown'
    assert len(contacts)==1


def test_docsend_receipt_projection_rejects_forged_pdf(deck_harness):
    data,reader,deck,state,call,contacts=deck_harness
    receipt=call(deck_request())
    path=next((Path(data)/'events/research-artifacts').glob('web-*.json'))
    saved=json.loads(path.read_text())
    saved['receipt']['outcome']['result']['pdf']='/api/decks/another.pdf'
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError):
        call(dict(deck_request(),operation='inspect'))
    assert receipt['outcome']['result']['pdf']==''


@pytest.mark.parametrize('failure',['body','response','authority'])
def test_docsend_original_vision_bound_and_guard_hold(deck_harness,monkeypatch,failure):
    import urllib.request
    import io
    data,reader,deck,state,call,contacts=deck_harness
    # Restore original vision callable from the exact original module.
    import importlib.util
    spec=importlib.util.spec_from_file_location('vision_original',deck.__file__)
    actual=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(actual)
    calls=[]
    class Response:
        def __init__(self): self.buffer=io.BytesIO(b'x'*(512*1024+1))
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read1(self,n): return self.buffer.read(n)
    def urlopen(req,timeout):
        calls.append(timeout)
        return Response()
    monkeypatch.setattr(urllib.request,'urlopen',urlopen)
    checks=[]
    def guard():
        checks.append(True)
        if failure=='authority' and len(checks)>1:
            raise actual.DocSendHeldError('revoked')
    with pytest.raises(actual.DocSendHeldError):
        actual._gemini_read([b'image'],'Deck',timeout=30,max_body=1 if failure=='body' else 8*1024*1024,guard=guard)
    assert len(calls)==(0 if failure=='body' else 1)


def reply_request(**overrides):
    body = request(text='Read the link in this reply.')
    target = {'accepted_dispatch_id': 'accepted-reply', 'provider_message_id': 'provider-reply',
              'text': 'Saved reply: ' + URL + '\nAgain: ' + URL, 'source_revision': 'a' * 64}
    target.update(overrides)
    body['args']['reply_target'] = target
    encoded = json.dumps(body['args'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_web_read', encoded],
                                     ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return body


def test_attested_reply_target_original_reader_and_inspect_no_refetch(harness, monkeypatch):
    _, _, web, _, call = harness
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        return {'candidates': [{'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': URL, 'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]}, 'content': {'parts': [{'text': 'Original page evidence'}]}}]}
    monkeypatch.setattr(web, '_post', post)
    body = reply_request()
    result = call(body)
    assert result['status'] == 'committed'
    assert result['outcome']['result']['url'] == URL
    assert result['outcome']['result']['text'] == 'Source-derived summary (not verbatim page text):\n' + 'Original page evidence'
    assert len(calls) == 1
    assert call({**body, 'operation': 'inspect'}) == result
    assert call(body) == result and len(calls) == 1


@pytest.mark.parametrize('target_change', [
    {'text': 'No complete link'}, {'text': URL + ' https://other.example/read'},
    {'source_revision': None}, {'source_revision': 'c' * 64},
    {'accepted_dispatch_id': ''}, {'provider_message_id': 'bad\u2028id'},
    {'accepted_dispatch_id': '😀' * 257}, {'text': '😀' * 8000 + URL},
    {'text': 'bad\ud800' + URL}, {'extra': 'unsupported'},
])
def test_reply_target_invalid_zero_contact(harness, monkeypatch, target_change):
    _, _, web, _, call = harness
    monkeypatch.setattr(web, 'fetch_url', lambda *a, **k: pytest.fail('invalid evidence cannot contact'))
    with pytest.raises(ValueError):
        call(reply_request(**target_change))


def test_reply_target_current_url_precedence_and_nonnull_authority(harness, monkeypatch):
    _, _, web, state, call = harness
    body = reply_request()
    with pytest.raises(ValueError):
        tool.validate_args(body['args'], 'Read https://other.example/read')
    with pytest.raises(ValueError):
        tool.validate_args(body['args'], 'Read ' + URL)
    with pytest.raises(ValueError):
        tool.validate_args({**body['args'], 'source_revision': None}, body['text'])
    monkeypatch.setattr(web, 'fetch_url', lambda *a, **k: pytest.fail('incomplete source cannot contact'))
    state['complete'] = False
    assert call(body) == {'version': 1, 'status': 'rejected', 'code': 'source_unavailable'}


def test_reply_target_docsend_refused_and_changed_proof_conflicts(harness, monkeypatch):
    _, _, web, _, call = harness
    with pytest.raises(ValueError):
        tool.validate_args({'url': DECK_URL, 'source_revision': 'a' * 64,
            'reply_target': {**reply_request()['args']['reply_target'], 'text': DECK_URL}}, 'Read the deck.')
    calls = []
    monkeypatch.setattr(web, '_post', lambda *a, **k: calls.append(1) or {
        'candidates': [{'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': URL, 'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]}, 'content': {'parts': [{'text': 'Saved page'}]}}]})
    original = reply_request()
    result = call(original)
    assert result['status'] == 'committed'
    changed = reply_request(accepted_dispatch_id='different-accepted')
    assert call(changed)['code'] == 'event_conflict'
    assert len(calls) == 1
