"""Contact names and compact Contacts cards for client MCP reads.

A phone or handle gets a name only from a permitted Mac Contacts card that alone owns it
(render_local.build_contact_lookup); a shared or unknown number stays bare. Cards lose Facebook
sync tags and long notes, and a query narrows the address book to matching cards.
"""
import re
import secrets
import time

import client_mcp_audit

CACHE_SECONDS = 600
NOTE_CHARS = 200
QUERY_MATCHES = 25
PHONE_DIGITS = 7  # fewer digits is a name or fragment, not a phone number
BOOK_HOURS = 168  # above 48 hours the Bridge returns the whole address book
# Identity fields only: a title or other free text that happens to hold a number is never named.
IDENTITY_FIELDS = ('handle', 'phone', 'contact_jid', 'sender_jid', 'jid', 'author', 'email', 'sender')
_SYNC_TAG = re.compile(r'<sn>.*?</sn>', re.S)


def card(contact):
    """One contact without sync tags; notes collapse and stop at NOTE_CHARS."""
    out = {key: value for key, value in contact.items() if key != 'notes'}
    notes = ' '.join(_SYNC_TAG.sub(' ', str(contact.get('notes') or '')).split())
    if notes:
        out['notes'] = notes if len(notes) <= NOTE_CHARS else notes[:NOTE_CHARS - 1] + '…'
    return out


def matches(contact, query, normalize):
    """Case-insensitive name/email text, or the same phone number in any format."""
    text = ' '.join(query.split()).casefold()
    name = ' '.join(str(contact.get('name') or '').split()).casefold()
    if text and (text in name or any(text in str(email).casefold() for email in contact.get('emails') or [])):
        return True
    digits = re.sub(r'\D', '', query)
    key = normalize(query) if len(digits) >= PHONE_DIGITS else ''
    return bool(key) and any(normalize(str(phone)) == key for phone in contact.get('phones') or [])


def shape(result, query, normalize):
    """Compact every card; a query keeps only matching cards and says how many matched."""
    contacts = result.get('contacts')
    if not isinstance(contacts, list):
        return result
    cards = [card(row) for row in contacts if isinstance(row, dict)]
    if query:
        cards = [row for row in cards if matches(row, query, normalize)]
        result = {**result, 'contacts_query': {'query': query, 'matched': len(cards),
                                               'returned': min(len(cards), QUERY_MATCHES)}}
        cards = sorted(cards, key=lambda row: str(row.get('name') or ''))[:QUERY_MATCHES]
    return {**result, 'contacts': cards}


def _complete_cards(payload):
    cards = payload.get('contacts')
    if not isinstance(cards, list) or payload.get('contacts_total', len(cards)) != len(cards):
        return []
    if 'source_coverage' in payload:
        coverage = payload['source_coverage']
        scope = coverage.get('contacts') if isinstance(coverage, dict) else None
        if (not isinstance(scope, dict) or scope.get('status', 'ok') != 'ok' or scope.get('omitted_rows')
                or scope.get('content_partial') or scope.get('limit_reached')):
            return []
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get('name'), str) or not card['name'].strip():
            return []
        for field in ('phones', 'emails'):
            values = card.get(field, [])
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                return []
    return cards


def _lookup(receiver, revision):
    owner = (getattr(receiver, 'DATA', None), revision)
    cache = getattr(receiver.RELAY, '_client_contact_names', None)
    if cache and cache['owner'] == owner and time.monotonic() - cache['at'] < CACHE_SECONDS:
        return cache['lookup']
    request = {'jsonrpc': '2.0', 'id': 'client-contacts-' + secrets.token_hex(12), 'method': 'tools/call',
               'params': {'name': 'read_local', 'arguments': {'since_hours': BOOK_HOURS, 'sources': ['contacts']}}}
    response = receiver.RELAY.mcp_call(request, timeout=20)
    if not isinstance(response, dict) or type(response.get('result')) is not dict or response['result'].get('isError'):
        return {}
    payload = receiver._source_context().unwrap_tool_result(response['result'])
    statuses = payload.get('source_status') if isinstance(payload, dict) else None
    status = statuses.get('contacts') if isinstance(statuses, dict) else None
    if status not in ('ok', 'partial'):
        return {}
    cards = _complete_cards(payload) if status == 'ok' else []
    lookup = receiver._load_shared_lib('render_local').build_contact_lookup(cards) if cards else {}
    receiver.RELAY._client_contact_names = {'owner': owner, 'at': time.monotonic(), 'lookup': lookup}
    return lookup


def _rows(result):
    if not isinstance(result, dict):
        return
    for key, value in result.items():
        if key != 'contacts' and isinstance(value, list):
            yield from (row for row in value if isinstance(row, dict))


def name_rows(receiver, before, result):
    """Add `<field>_name` beside each identifier a permitted Contacts card uniquely owns."""
    if 'contacts' not in before.get('bridge_sources', []):
        return result
    rows = list(_rows(result))
    if not any(isinstance(row.get(field), str) for row in rows for field in IDENTITY_FIELDS):
        return result
    client_mcp_audit.touch('contacts')
    lookup = _lookup(receiver, before['revision'])
    normalize = receiver._load_shared_lib('textutil')._normalize_identifier
    for row in rows:
        for field in IDENTITY_FIELDS:
            value = row.get(field)
            name = lookup.get(normalize(value)) if isinstance(value, str) and value != 'me' else None
            if name and name != value and field + '_name' not in row:
                row[field + '_name'] = name
    return result
