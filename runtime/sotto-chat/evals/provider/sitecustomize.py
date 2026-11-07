"""External-source fixture and egress fence for eval worker subprocesses.

External website/research and optional Gmail reads use frozen source fixtures. Final
composition uses the real local runtime /v1/run and configured model. Unknown remote reads
fail, never silently fall back. Gmail search returns a bounded fixture inventory, not a query engine.
"""
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import sys
import urllib.request
from urllib.parse import parse_qs, quote, urlparse

def trace(record):
    with Path(os.environ['SOTTO_EVAL_WORLD']).with_name('source-reads.jsonl').open('a') as log:
        log.write(json.dumps(record) + '\n')


def denied(reason):
    trace({'source': 'denied', 'reason': reason})
    raise PermissionError(reason)


original = getattr(urllib.request, '_sotto_eval_original_urlopen', urllib.request.urlopen)

def source_open(request, *args, **kwargs):
    url = getattr(request, 'full_url', str(request))
    if url.startswith('https://eval-source.invalid/native/'):
        body = json.loads(request.data)
        world = json.loads(Path(os.environ['SOTTO_EVAL_WORLD']).read_text())
        if world.get('meetingPrep') and body.get('tools') == [{'google_search': {}}]:
            from prep_sources import research_response  # noqa: PLC0415 - synthetic prep world only
            result = research_response(body, trace, denied)
            return io.BytesIO(json.dumps({'candidates': [{'content': {'parts': [{'text': json.dumps(result)}]},
                'finishReason': 'STOP', **({'groundingMetadata': world['prepGrounding']} if 'prepGrounding' in world else {})}], 'usageMetadata': {'promptTokenCount': 0, 'candidatesTokenCount': 0}}).encode())
        actual_commitment = world.get('commitmentJourney', {}).get('actualModel') is True
        if (world.get('meetingPrep') or actual_commitment) and 'tools' not in body:
            relay = json.loads(os.environ.get('SOTTO_EVAL_NATIVE_PREP_RELAY', '{}'))
            try:
                local_relay = urlparse(str(relay.get('url', '')))
                relay_port = local_relay.port
            except ValueError:
                denied('Malformed native prep loopback relay URL')
            if (url != 'https://eval-source.invalid/native/v1beta/models/gemini-3.8-flash:generateContent'
                    or local_relay.scheme != 'http' or local_relay.hostname != '127.0.0.1'
                    or local_relay.username is not None or local_relay.password is not None
                    or local_relay.path != '/compose' or local_relay.query or local_relay.fragment
                    or relay_port is None or not relay.get('bearer')):
                denied('Native prep composition lacks its exact loopback relay')
            headers = {name: value for name, value in request.header_items() if name.lower().startswith('x-sotto-')}
            headers.update({'Authorization': 'Bearer ' + relay['bearer'], 'Content-Type': 'application/json'})
            trace({'source': 'native_commitment_composition' if actual_commitment else 'native_prep_composition',
                   'boundary': 'approved Node relay'})
            return original(urllib.request.Request(relay['url'], data=request.data, headers=headers), timeout=185)
        prompt = '\n'.join(part.get('text', '') for content in body.get('contents', []) for part in content.get('parts', []))
        if body.get('tools') == [{'google_search': {}}]:
            lines = prompt.splitlines()
            if len(lines) < 2 or lines[0] != '## Company requested by the owner':
                denied('Unexpected research kind')
            selector = json.loads(lines[1])
            result = world.get('companyResearch', {}).get(selector)
            if result is None:
                denied('No fixture for selected company research')
            trace({'source': 'company_research', 'selector': selector})
            return io.BytesIO(json.dumps({'candidates': [{'content': {'parts': [{'text': json.dumps(result)}]},
                'finishReason': 'STOP'}], 'usageMetadata': {'promptTokenCount': 0, 'candidatesTokenCount': 0}}).encode())
        if body.get('tools') != [{'url_context': {}}]:
            denied('No fixture for this external research request')
        websites = {**world.get('websites', {}), **{u: None for u in world.get('unavailableWebsites', [])}}
        matches = re.findall(r'https?://[^\s]+', prompt)
        if len(matches) != 1 or matches[0] not in websites:
            denied('Website fixture missing or ambiguous')
        selected = matches[0]
        if websites[selected] is None:
            trace({'source': 'unavailable', 'url': selected})
            raise OSError('Declared unavailable website fixture')
        trace({'source': 'website', 'url': selected})
        return io.BytesIO(json.dumps({'candidates': [{'content': {'parts': [{'text': websites[selected]}]},
            'finishReason': 'STOP', 'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': selected,
            'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]}}],
            'usageMetadata': {'promptTokenCount': 0, 'candidatesTokenCount': 0}}).encode())
    if not url.startswith(('http://127.0.0.1:', 'http://[::1]:')):
        denied('No eval fixture for external HTTP endpoint')
    return original(request, *args, **kwargs)

if not hasattr(urllib.request, '_sotto_eval_original_urlopen'):
    urllib.request._sotto_eval_original_urlopen = original
    urllib.request.urlopen = source_open

def network_guard(event, args):
    if event == 'socket.connect':
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
            denied('Conversation eval blocks external worker connections')

if not getattr(urllib.request, '_sotto_eval_audit_installed', False):
    sys.addaudithook(network_guard)
    urllib.request._sotto_eval_audit_installed = True


def read_owner(operation, arguments):
    """Frozen Gmail transport through the real reader and receiver authority checks."""
    world = json.loads(Path(os.environ['SOTTO_EVAL_WORLD']).read_text())
    messages = world.get('gmailMessages', {})
    calendar = world.get('calendarEvents')
    if (operation not in ('gmail_get', 'gmail_search', 'gmail_thread_get', *(['calendar_list', 'calendar_get'] if isinstance(calendar, list) else []))
            or not isinstance(messages, dict)):
        denied('No Gmail fixture for this operation')
    trace({'source': 'gmail_fixture', 'operation': operation, 'arguments': arguments})
    api_path = Path(__file__).resolve().parents[4] / 'adapters/sotto/google_api.py'
    spec = importlib.util.spec_from_file_location('eval_real_google_reader', api_path)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    class Response:
        status_code = 200
        headers = {}
        def __init__(self, value): self.value = value
        def json(self): return self.value
        def raise_for_status(self): pass
        def iter_content(self, chunk_size): yield json.dumps(self.value).encode()
        def close(self): pass
    class Session:
        def get(self, url, params=None, **kwargs):
            if isinstance(calendar, list):
                try:
                    from prep_sources import CALENDAR_ID, calendar_page  # noqa: PLC0415 - declared Calendar fixture
                    if url == 'https://www.googleapis.com/calendar/v3/users/me/calendarList/' + quote(CALENDAR_ID, safe=''):
                        return Response({'id': CALENDAR_ID, 'primary': True, 'accessRole': 'owner'})
                    if url == 'https://www.googleapis.com/calendar/v3/users/me/calendarList':
                        return Response({'items': [{'id': CALENDAR_ID, 'primary': True, 'accessRole': 'owner'}]})
                    prefix = 'https://www.googleapis.com/calendar/v3/calendars/' + quote(CALENDAR_ID, safe='') + '/events'
                    if url == prefix:
                        return Response({'items': calendar_page(world, params['timeMin'], params['timeMax'])})
                    primary_prefix = 'https://www.googleapis.com/calendar/v3/calendars/primary/events'
                    occurrence_prefix = next((value for value in (prefix, primary_prefix) if url.startswith(value + '/')), None)
                    if occurrence_prefix is not None:
                        found = [row for row in calendar if row['id'] == url[len(occurrence_prefix)+1:]]
                        if len(found) == 1:
                            return Response(found[0])
                        denied('Unknown frozen prep Calendar occurrence')
                    if url.startswith('https://www.googleapis.com/calendar/v3/'):
                        denied('Unknown frozen prep Calendar scope or occurrence')
                except PermissionError:
                    raise
                except Exception:  # noqa: BLE001 - malformed source fixtures must remain sticky
                    denied('Malformed prep Calendar request or fixture')
            prefix = 'https://gmail.googleapis.com/gmail/v1/users/me/messages'
            thread_prefix = 'https://gmail.googleapis.com/gmail/v1/users/me/threads/'
            if url.startswith(thread_prefix):
                thread_id = url[len(thread_prefix):]
                rows = [row for row in messages.values()
                        if isinstance(row, dict) and row.get('threadId') == thread_id]
                if not rows:
                    denied('Unknown frozen Gmail thread')
                return Response({'id': thread_id, 'messages': rows})
            if url == prefix:
                # Deliberately bounded fixture inventory, never a live mailbox search.
                limit = (params or {}).get('maxResults', 20)
                return Response({'messages': [{'id': key, 'threadId': row.get('threadId')}
                    for key, row in list(messages.items())[:limit]],
                    **({'nextPageToken': 'fixture-omitted'} if len(messages) > limit else {})})
            if url.startswith(prefix + '/') and url[len(prefix) + 1:] in messages:
                return Response(messages[url[len(prefix) + 1:]])
            denied('Unknown frozen Gmail message')
        def close(self): pass
    return api.read_owner(operation, arguments, session_factory=Session)


def install_triage(receiver):
    """Keep the real isolated triage subprocess; freeze only its Google HTTP boundary."""
    home = Path(os.environ['SOTTO_HOME'])
    home.mkdir(parents=True, exist_ok=True)
    token = home / 'google_token.json'
    token.write_text(json.dumps({'token': 'eval-only-token', 'refresh_token': 'eval-only-refresh',
        'client_id': 'eval-only-client', 'client_secret': 'eval-only-secret',
        'token_uri': 'https://oauth.invalid/token', 'expiry': '2099-01-01T00:00:00Z',
        'scopes': ['https://www.googleapis.com/auth/gmail.readonly'],
        'sotto_source_generation': 'a' * 64}))
    token.chmod(0o600)
    original_popen = receiver.RUNTIME_KNOWLEDGE_READ.subprocess.Popen
    fixture = str(Path(__file__).resolve())
    world_file = os.environ['SOTTO_EVAL_WORLD']
    def start(command, *args, **kwargs):
        if any(str(item).endswith('/triage_read.py') for item in command):
            script = next(str(item) for item in command if str(item).endswith('/triage_read.py'))
            bootstrap = ("import importlib.util,runpy,sys; "
                f"s=importlib.util.spec_from_file_location('eval_triage_sources',{fixture!r}); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "sys.argv=[sys.argv[1]]; runpy.run_path(sys.argv[0],run_name='__main__')")
            command = [command[0], '-I', '-B', '-c', bootstrap, script]
            kwargs['env'] = {**kwargs['env'], 'SOTTO_EVAL_WORLD': world_file,
                             'PYTHONPATH': str(Path(fixture).parent)}
        return original_popen(command, *args, **kwargs)
    receiver.RUNTIME_KNOWLEDGE_READ.subprocess.Popen = start


def install_triage_http(world):
    import httplib2  # noqa: PLC0415 - optional triage source world only
    messages = world['gmailMessages']
    def request(_self, uri, method='GET', body=None, headers=None, **kwargs):
        parsed = urlparse(uri)
        if method != 'GET' or parsed.scheme != 'https' or parsed.hostname not in ('gmail.googleapis.com', 'www.googleapis.com'):
            denied('Unexpected triage Google HTTP request')
        path = parsed.path.removeprefix('/gmail/v1/users/me/')
        query = parse_qs(parsed.query)
        trace({'source': 'gmail_triage_fixture', 'path': path, 'query': query})
        if path == 'messages':
            selector = query.get('q', [''])[0]
            if selector not in ('newer_than:1d', 'in:sent newer_than:1d') or 'pageToken' in query:
                denied('Unknown triage Gmail search')
            rows = list(messages.values())
            if selector.startswith('in:sent'):
                rows = [row for row in rows if 'SENT' in row.get('labelIds', [])]
            limit = int(query.get('maxResults', ['20'])[0])
            result = {'messages': [{'id': row['id'], 'threadId': row['threadId']} for row in rows[:limit]],
                      **({'nextPageToken': 'fixture-omitted'} if len(rows) > limit else {})}
        elif path.startswith('messages/') and path[len('messages/'):] in messages:
            result = messages[path[len('messages/'):]]
        elif path.startswith('threads/'):
            thread_id = path[len('threads/'):]
            rows = [row for row in messages.values() if row['threadId'] == thread_id]
            if not rows:
                denied('Unknown triage Gmail thread')
            result = {'id': thread_id, 'messages': rows}
        elif path == 'profile':
            result = {'emailAddress': 'owner@example.test'}
        else:
            denied('Unknown triage Gmail HTTP operation')
        return httplib2.Response({'status': '200', 'content-type': 'application/json'}), json.dumps(result).encode()
    httplib2.Http.request = request


if os.environ.get('SOTTO_EVAL_WORLD'):
    _world = json.loads(Path(os.environ['SOTTO_EVAL_WORLD']).read_text())
    if _world.get('gmailTriage') is True:
        install_triage_http(_world)
    if _world.get('meetingPrep'):
        from prep_sources import install_calendar
        install_calendar(_world, trace, denied)

if __name__ == '__main__':
    denied('Frozen Gmail fixture supports read_owner only; Google CLI operations are unavailable')
