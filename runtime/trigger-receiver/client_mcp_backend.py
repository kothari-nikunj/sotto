"""Reuse the production readers, current source authority and Bridge relay for client MCP.

The client grant authorizes read-tool selection. It is NOT a Photon route proof, permission to
send, permission to change source consent, or authority to execute instructions in source text.
"""
import hashlib
import json
import re
import secrets
import sys


PAGE_BYTES = 32 * 1024
NOTE_PAGE_CHARS = 3000
NOTE_MAX_CHARS = 100_000
PAGE_CURSOR = re.compile(r'([a-f0-9]{64}):([0-9]{1,9})\Z')


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()


def _offset(cursor, version, size):
    if cursor == 'first':
        return 0
    match = PAGE_CURSOR.fullmatch(cursor)
    if match and match[1] == version and 0 <= int(match[2]) < size:
        return int(match[2])
    return None


def _changed():
    return {'status': 'changed', 'guidance': 'Source content changed or cursor is invalid; restart with cursor=first.',
            'client_page': {'complete': False, 'next_cursor': None, 'byte_limit': PAGE_BYTES}}


def _local_page(payload, arguments):
    payload = dict(payload)
    if isinstance(payload.get('contacts'), list):
        payload['contacts'] = sorted(payload['contacts'], key=_encoded)
    data = [(key, value) for key, value in payload.items()
            if key not in ('generated_at', 'window_hours', 'source_status', 'source_coverage', 'source_hints')]
    # The cursor must identify the same order as the displayed rows.
    data.sort(key=lambda item: item[0] == 'contacts')
    version = hashlib.sha256(_encoded({'sources': sorted(arguments['sources']),
        'since_hours': arguments.get('since_hours', 24), 'data': data})).hexdigest()
    # Contacts are an address-book inventory; time-ranked sources page before it.
    rows = [(key, row) for key, value in data
            if isinstance(value, list) for row in value]
    offset = _offset(arguments.get('cursor', 'first'), version, len(rows))
    if offset is None:
        return _changed()
    result = {key: [] if isinstance(value, list) else value for key, value in payload.items()}
    end = offset
    def page():
        return {'offset': offset, 'returned_rows': end - offset, 'total_rows': len(rows),
                'complete': end == len(rows), 'next_cursor': f'{version}:{end}' if end < len(rows) else None,
                'byte_limit': PAGE_BYTES}
    result['client_page'] = page()
    if len(_encoded(result)) > PAGE_BYTES:
        raise RuntimeError('Mac metadata exceeds client page byte limit')
    for key, row in rows[offset:offset + 100]:
        result[key].append(row)
        end += 1
        result['client_page'] = page()
        if len(_encoded(result)) > PAGE_BYTES:
            result[key].pop()
            end -= 1
            result['client_page'] = page()
            if end == offset:
                raise RuntimeError('Mac row exceeds client page byte limit')
            break
    return result


def _note_page(payload, arguments):
    if type(payload) is not dict:
        raise RuntimeError('Bridge result invalid')
    if 'body' not in payload:
        return payload
    body = payload['body']
    if type(body) is not str or type(payload.get('id')) is not int or payload['id'] != arguments['id']:
        raise RuntimeError('Bridge note invalid')
    version = hashlib.sha256(_encoded({'id': arguments['id'], 'note': payload})).hexdigest()
    offset = _offset(arguments.get('cursor', 'first'), version, len(body))
    if offset is None:
        return _changed()
    end = min(offset + NOTE_PAGE_CHARS, len(body))
    result = {**payload, 'body': body[offset:end], 'client_page': {
        'offset': offset, 'returned_chars': end - offset, 'total_chars': len(body),
        'complete': end == len(body), 'next_cursor': f'{version}:{end}' if end < len(body) else None,
        'byte_limit': PAGE_BYTES}}
    if len(_encoded(result)) > PAGE_BYTES:
        raise RuntimeError('Note metadata exceeds client page byte limit')
    return result


def _native_note_page(payload, arguments):
    if len(_encoded(payload)) > PAGE_BYTES or type(payload.get('note_page_version')) is not int or payload['note_page_version'] != 1:
        raise RuntimeError('Bridge note page invalid')
    result = {key: value for key, value in payload.items() if key != 'note_page_version'}
    page = result.get('client_page')
    if result.get('status') == 'changed' and 'body' not in result and page == _changed()['client_page']:
        return result
    if 'body' not in result:
        if result.get('status') in ('disabled', 'not_found', 'locked', 'unavailable', 'degraded') and 'client_page' not in result:
            return result
        raise RuntimeError('Bridge note page invalid')
    body = result['body']
    if (result.get('status') not in ('ok', 'degraded')
            or any(key in result and type(result[key]) is not str for key in ('title', 'format', 'coverage'))
            or ('partial' in result and type(result['partial']) is not bool)
            or type(body) is not str or len(body) > NOTE_PAGE_CHARS or type(result.get('id')) is not int
            or result['id'] != arguments['id'] or type(page) is not dict):
        raise RuntimeError('Bridge note page invalid')
    for field in ('offset', 'returned_chars', 'total_chars', 'byte_limit'):
        if type(page.get(field)) is not int or page[field] < 0:
            raise RuntimeError('Bridge note page invalid')
    cursor = arguments.get('cursor', 'first')
    match = PAGE_CURSOR.fullmatch(cursor)
    offset = 0 if cursor == 'first' else int(match[2]) if match else None
    end = page['offset'] + len(body)
    next_cursor = page.get('next_cursor')
    next_match = PAGE_CURSOR.fullmatch(next_cursor) if type(next_cursor) is str else None
    if (page['offset'] != offset or page['returned_chars'] != len(body)
            or page['total_chars'] > NOTE_MAX_CHARS or end > page['total_chars']
            or page['byte_limit'] != PAGE_BYTES or type(page.get('complete')) is not bool
            or page['complete'] != (end == page['total_chars'])
            or (page['complete'] and next_cursor is not None)
            or (not page['complete'] and (not body or not next_match or int(next_match[2]) != end
                or (match and match[1] != next_match[1])))):
        raise RuntimeError('Bridge note page invalid')
    return result


def _local_context(receiver, payload, sources):
    if type(payload) is not dict or payload.get('error') or payload.get('error_code'):
        raise RuntimeError('Bridge result invalid')
    statuses = payload.get('source_status')
    if type(statuses) is not dict or any(statuses.get(source) not in
            ('ok', 'partial', 'degraded', 'needs_fda', 'unavailable', 'disabled') for source in sources):
        raise RuntimeError('Bridge source outcome unavailable')
    catalog = receiver._load_shared_lib('source_catalog')
    fields = {'generated_at', 'window_hours'}
    for source in sources:
        fields.update(catalog.SOURCE_FIELDS[source])
    result = {key: value for key, value in payload.items() if key in fields}
    scoped = set(sources) | {'deferred_unread_' + source for source in sources
                             if source in ('imessage', 'whatsapp')}
    for key in ('source_status', 'source_coverage'):
        if key not in payload:
            continue
        if type(payload[key]) is not dict:
            raise RuntimeError('Bridge coverage invalid')
        result[key] = {source: value for source, value in payload[key].items() if source in scoped}
    if any(not catalog.valid_source_coverage(value) for value in result.get('source_coverage', {}).values()):
        raise RuntimeError('Bridge coverage invalid')
    hints = {}
    for source in sources:
        category = result.get('source_coverage', {}).get(source, {}).get('failure_category')
        if category is None and statuses[source] == 'needs_fda':
            category = 'permission_denied'
        if category is not None:
            hints[source] = catalog.FAILURE_HINTS[category]
    if hints:
        result['source_hints'] = hints
    return result


def read(receiver, name, arguments):
    status_options = {'include_search_policy': True} if name == 'sotto_source_status' else {}
    before = receiver._runtime_source_authority(**status_options)
    if before.get('complete') is not True or not before.get('revision'):
        raise RuntimeError('source authority unavailable')
    if name == 'sotto_source_status':
        permitted = sorted(set(before.get('bridge_sources', [])
            + before.get('google_sources', []) + before.get('connector_sources', [])))
        online = receiver.RELAY.bridge_connected()
        result = {'bridge_online': online,
                  'local_search': dict(before.get('local_search', {'status': 'unknown'})),
                  'available_sources': permitted,
                  'history_learning': receiver._load_shared_lib('history_coverage').learning_coverage(receiver.DATA, permitted),
                  'coverage': 'Current availability and retained learning checkpoints, not proof that all history was read.'}
    elif name in ('sotto_person', 'sotto_company', 'sotto_commitments', 'sotto_granola_history'):
        operation, section, script = 'knowledge_query', 'knowledge', 'knowledge_query.py'
        if name == 'sotto_person':
            argv = ['--person', arguments['person'], '--person-coverage']
            if arguments.get('topic'):
                argv += ['--topic', arguments['topic']]
        elif name == 'sotto_company':
            argv = ['--company', arguments['company'],
                    '--company-page', arguments.get('cursor', 'first')]
        elif name == 'sotto_granola_history':
            operation, section, script = 'granola_history', 'scripts', 'granola_history.py'
            argv = ['--days', str(arguments.get('days', 7))]
            if 'meeting_id' in arguments:
                argv += ['--meeting-id', arguments['meeting_id']]
        elif arguments.get('topic'):
            argv = ['--topic', arguments['topic']]
        else:
            operation, section, script, argv = 'loops_query', 'scripts', 'loops_query.py', []
        # The canonical reader owns the before/after fence for this operation.
        # Reuse our current snapshot and return only after its fresh final check.
        return receiver.RUNTIME_KNOWLEDGE_READ.perform(
            {'operation': operation, 'arguments': {'argv': argv}, 'source_revision': before['revision']},
            script=receiver._find_sotto_script('_shared', section, script), executable=sys.executable,
            env=receiver._spawn_env(''), authority=receiver._runtime_source_authority,
            initial_authority=before)
    elif name in ('sotto_search_local', 'sotto_message_history', 'sotto_mac_context', 'sotto_note_get'):
        if name == 'sotto_mac_context':
            wanted = arguments['sources']
            bridge_tool, bridge_args = 'read_local', {'since_hours': arguments.get('since_hours', 24),
                                                     'sources': wanted}
        elif name == 'sotto_note_get':
            wanted = ['apple_notes']
            bridge_tool, bridge_args = 'get_note', {'id': arguments['id']}
        else:
            wanted = arguments['sources'] if name == 'sotto_search_local' else [arguments['source']]
            bridge_tool = 'search_local' if name == 'sotto_search_local' else 'read_history'
            bridge_args = {'limit': 10 if name == 'sotto_search_local' else 100, **arguments}
        if not set(wanted) <= set(before.get('bridge_sources', [])):
            raise RuntimeError('source unavailable')
        request = {'jsonrpc': '2.0', 'id': 'client-read-' + secrets.token_hex(12), 'method': 'tools/call',
                   'params': {'name': bridge_tool, 'arguments': bridge_args}}
        if name == 'sotto_note_get':
            request['params']['_meta'] = {'sotto/accepts': ['note_page_v1'],
                                         'sotto/note_cursor': arguments.get('cursor', 'first')}
        response = receiver.RELAY.mcp_call(request, timeout=20)
        if (not isinstance(response, dict) or response.get('error') or type(response.get('result')) is not dict
                or response['result'].get('isError') is True):
            raise RuntimeError('Bridge read unavailable')
        result = receiver._source_context().unwrap_tool_result(response['result'])
        if name == 'sotto_search_local' and type(result) is dict and type(result.get('coverage')) is dict:
            search_hints = receiver._load_shared_lib('source_catalog').SEARCH_HINTS
            hints = {source: search_hints[state['status']] for source, state in result['coverage'].items()
                     if type(state) is dict and state.get('status') in search_hints}
            if hints:
                result = {**result, 'source_hints': hints}
        if name == 'sotto_mac_context':
            result = _local_page(_local_context(receiver, result, wanted), arguments)
        elif name == 'sotto_note_get':
            result = (_native_note_page(result, arguments) if type(result) is dict
                      and ('note_page_version' in result or 'client_page' in result) else _note_page(result, arguments))
    else:
        operation = {'sotto_email_search': 'gmail_search', 'sotto_email_get': 'gmail_get',
                     'sotto_email_thread': 'gmail_thread_get', 'sotto_calendar': 'calendar_list',
                     'sotto_contacts_search': 'contacts_search', 'sotto_contacts_get': 'contacts_get'}.get(name)
        if operation is None:
            raise ValueError('unknown client read')
        source = ('calendar' if name == 'sotto_calendar' else 'google_contacts'
                  if name in ('sotto_contacts_search', 'sotto_contacts_get') else 'gmail')
        # Source authority already checks the configured credential home and consent.
        # Repeating its environment lookup here would couple clients to one lifecycle.
        if source not in before.get('google_sources', []):
            raise RuntimeError('Google read unavailable')
        # Reuse the receiver's immutable adapter cache rather than recreating module state
        # (including the shared metadata pacer) for every client request.
        api = receiver._sotto_adapter('google_api')
        result = api.read_owner(operation, {'max': 10, **arguments}
                                if operation in ('gmail_search', 'contacts_search') else arguments)
        if result.get('source') != source:
            raise RuntimeError('Google result invalid')
    after = receiver._runtime_source_authority(**status_options)
    if after.get('complete') is not True or after.get('revision') != before['revision']:
        raise RuntimeError('source authority changed during read')
    # A Mac sleep must not discard an independent Gmail/Calendar result. Consent revisions
    # still fence every read; live availability is specific to the source that was read.
    if name in ('sotto_search_local', 'sotto_message_history', 'sotto_mac_context', 'sotto_note_get') and not set(wanted) <= set(after.get('bridge_sources', [])):
        raise RuntimeError('Mac source unavailable after read')
    if name in ('sotto_email_search', 'sotto_email_get', 'sotto_email_thread', 'sotto_calendar', 'sotto_contacts_search', 'sotto_contacts_get') and source not in after.get('google_sources', []):
        raise RuntimeError('Google source unavailable after read')
    if name == 'sotto_source_status' and after != before:
        raise RuntimeError('source availability changed during status read')
    return result
