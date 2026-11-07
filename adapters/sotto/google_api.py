#!/usr/bin/env python3
"""Narrow Sotto Google CLI compatibility for the chief-of-staff consumers.

This reads the existing authorized-user file through the shared Google client builder.
It does not create OAuth grants or write credentials. Only google_action.py may invoke
the write verbs in product flows; no general agent/script manifest exposes this CLI.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timezone
from email.mime.text import MIMEText
from email.utils import getaddresses
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import quote

WRITE_VERBS = {('gmail', 'send'), ('gmail', 'reply'),
               ('calendar', 'create'), ('calendar', 'patch'), ('calendar', 'delete')}
MAX_GMAIL_RESULTS = 1000
GMAIL_METADATA_WORKERS = 5
MAX_CALENDAR_PAGES = 20


class _MetadataPacer:
    """Space request starts across all Gmail searches in this process."""

    def __init__(self, clock=time.monotonic, wait=time.sleep):
        self._clock, self._wait = clock, wait
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait_turn(self, stop=None):
        with self._lock:
            while True:
                if stop is not None and stop.is_set():
                    return False
                now = self._clock()
                delay = self._next_start - now
                if delay <= 0:
                    self._next_start = now + 0.25  # four metadata reads/second, no burst credit
                    return True
                self._wait(delay)


_GMAIL_METADATA_PACER = _MetadataPacer()


def _shared_library():
    """Resolve only the immutable packaged skill lib (or its source-checkout twin)."""
    candidates = [Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff' / '_shared' / 'lib',
                  Path('/app/sotto-skills/_shared/lib')]
    for candidate in candidates:
        if (candidate / 'gmail_read.py').is_file():
            sys.path.insert(0, str(candidate))
            import gmail_read  # noqa: PLC0415
            return gmail_read
    raise RuntimeError('packaged Sotto Google reader is unavailable')


def _service(api, version):
    return _shared_library().google_service(api, version)


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError) as exc:
        raise ValueError('time must be ISO-8601 with a timezone') from exc
    if parsed.tzinfo is None:
        raise ValueError('time must include a timezone')
    return parsed


def _mail_headers(message):
    payload = message.get('payload') or {}
    return {str(header.get('name') or '').lower(): str(header.get('value') or '')
            for header in payload.get('headers') or [] if isinstance(header, dict)}


def _gmail_metadata(service, item, stop=None):
    if not _GMAIL_METADATA_PACER.wait_turn(stop):
        return None
    raw = service.users().messages().get(userId='me', id=str(item['id']), format='metadata',
                                        metadataHeaders=['From', 'To', 'Subject', 'Date']).execute()
    headers = _mail_headers(raw)
    return {'id': raw.get('id') or item['id'],
            'threadId': raw.get('threadId') or item.get('threadId'),
            'from': headers.get('from', ''), 'to': headers.get('to', ''),
            'subject': headers.get('subject', ''),
            'date': headers.get('date', '') or raw.get('internalDate', ''),
            'snippet': raw.get('snippet', ''),
            'labels': raw.get('labelIds', [])}


def _gmail_search(service, query, limit, service_factory, *, ids_only=False):
    if not 1 <= limit <= MAX_GMAIL_RESULTS:
        raise ValueError('gmail search max is out of bounds')
    users = service.users()
    messages, page, seen_pages = [], None, set()
    while len(messages) < limit:
        params = {'userId': 'me', 'q': query, 'maxResults': min(500, limit - len(messages))}
        if page:
            params['pageToken'] = page
        result = users.messages().list(**params).execute()
        for item in result.get('messages') or []:
            if isinstance(item, dict) and item.get('id'):
                messages.append(item)
                if len(messages) >= limit:
                    break
        page = result.get('nextPageToken')
        if not page:
            break
        if page in seen_pages or len(seen_pages) >= 20:
            raise RuntimeError('Gmail pagination did not complete')
        seen_pages.add(page)
    if ids_only:
        # The stale-thread reader needs candidates, then reads full thread metadata.
        # Preserve this same paginated cohort without fetching each message twice.
        return [{'id': item['id'], 'threadId': item['threadId']} if item.get('threadId')
                else _gmail_metadata(service, item) for item in messages]
    if len(messages) <= 1:
        return [_gmail_metadata(service, item) for item in messages]
    # Each worker owns its credentials/client/transport: googleapiclient's httplib2
    # transport cannot be shared across threads. Reuse that client within the worker,
    # keeping both connection setup and concurrent provider reads bounded.
    workers = min(GMAIL_METADATA_WORKERS, len(messages))
    out, stop = [None] * len(messages), threading.Event()

    def read_cohort(offset):
        client = None
        try:
            if stop.is_set():
                return
            client = service_factory('gmail', 'v1')
            for index in range(offset, len(messages), workers):
                if stop.is_set():
                    break
                out[index] = _gmail_metadata(client, messages[index], stop)
        except Exception:  # noqa: BLE001 -- a failed metadata read still fails the entire search
            stop.set()
            raise
        finally:
            close = getattr(client, 'close', None)
            if callable(close):
                close()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        # Consume every worker before returning; exceptions never publish a partial
        # search as complete. The result slots preserve Gmail's original list order.
        list(executor.map(read_cohort, range(workers)))
    return out


def _raw_email(to, subject, body, reply_id='', references=''):
    addresses = getaddresses([to])
    if len(addresses) != 1 or not _valid_email(addresses[0][1]) or '\n' in to or '\r' in to:
        raise ValueError('one valid recipient is required')
    message = MIMEText(body, 'plain', 'utf-8')
    message['To'] = addresses[0][1]
    if subject:
        message['Subject'] = subject
    if reply_id:
        message['In-Reply-To'] = reply_id
        message['References'] = (references + ' ' + reply_id).strip()
    return base64.urlsafe_b64encode(message.as_bytes()).decode('ascii').rstrip('=')


def _valid_email(value):
    if not isinstance(value, str) or value.count('@') != 1:
        return False
    local, domain = value.split('@')
    return bool(local and domain) and not any(char.isspace() or char in '<>\r\n' for char in value)


def _gmail_send(service, *, to=None, subject='', body='', reply_message_id=None,
                reviewed=None, sender=None):
    users = service.users()
    thread_id = None
    reply_id = references = ''
    if reply_message_id:
        original = users.messages().get(userId='me', id=reply_message_id, format='full').execute()
        if reviewed is not None:
            reviewed(original, users.getProfile(userId='me').execute())
        headers = _mail_headers(original)
        to = headers.get('reply-to') or headers.get('from') or ''
        subject = headers.get('subject') or ''
        if subject and not subject.lower().startswith('re:'):
            subject = 'Re: ' + subject
        reply_id = headers.get('message-id', '')
        references = headers.get('references', '')
        thread_id = original.get('threadId')
        if not thread_id:
            raise ValueError('message has no Gmail thread ID')
    payload = {'raw': _raw_email(to, subject, body, reply_id, references)}
    if thread_id:
        payload['threadId'] = thread_id
    result = (sender(payload) if sender is not None else
              users.messages().send(userId='me', body=payload).execute())
    if not isinstance(result, dict) or not result.get('id'):
        raise RuntimeError('Gmail did not confirm a sent message')
    return {'status': 'sent', 'id': result['id'], 'threadId': result.get('threadId') or thread_id or ''}


def _calendar_list(service, start, end):
    _timestamp(start)
    if _timestamp(end) <= _timestamp(start):
        raise ValueError('calendar end must follow start')
    _shared_library()
    from google_calendar_read import read_service  # noqa: PLC0415 - packaged shared library resolved above
    return read_service(service, start, end)


def _calendar_create(service, args):
    _timestamp(args.start)
    if _timestamp(args.end) <= _timestamp(args.start):
        raise ValueError('calendar end must follow start')
    body = {'summary': args.summary, 'start': {'dateTime': args.start},
            'end': {'dateTime': args.end}}
    event_id = getattr(args, 'event_id', '')
    if event_id:
        if not isinstance(event_id, str) or not re.fullmatch(r'[0-9a-f]{64}', event_id):
            raise ValueError('Calendar event ID is invalid')
        body['id'] = event_id
    if args.attendees:
        emails = [part.strip() for part in args.attendees.split(',') if part.strip()]
        if len(emails) > 50 or any(len(getaddresses([email])) != 1 or
                                   not _valid_email(getaddresses([email])[0][1]) for email in emails):
            raise ValueError('calendar attendees are invalid')
        body['attendees'] = [{'email': email} for email in emails]
    if args.location:
        body['location'] = args.location
    if args.description:
        body['description'] = args.description
    result = service.events().insert(calendarId='primary', body=body, sendUpdates='all').execute()
    if not isinstance(result, dict) or not result.get('id'):
        raise RuntimeError('Calendar did not confirm event creation')
    return result


def _calendar_patch(service, args):
    body = {}
    if args.attendees_json is not None:
        attendees = json.loads(args.attendees_json)
        if not isinstance(attendees, list) or len(attendees) > 100 or any(not isinstance(item, dict) for item in attendees):
            raise ValueError('calendar attendees JSON is invalid')
        body['attendees'] = attendees
    if args.location is not None:
        body['location'] = args.location
    if args.description is not None:
        body['description'] = args.description
    if not body:
        raise ValueError('calendar patch has no fields')
    result = service.events().patch(calendarId=args.calendar, eventId=args.event_id,
                                    body=body, sendUpdates=args.send_updates).execute()
    if not isinstance(result, dict) or not result.get('id'):
        raise RuntimeError('Calendar did not confirm event update')
    return result


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    domains = parser.add_subparsers(dest='domain', required=True)
    gmail = domains.add_parser('gmail').add_subparsers(dest='verb', required=True)
    search = gmail.add_parser('search'); search.add_argument('query'); search.add_argument('--max', type=int, default=40)
    search.add_argument('--ids-only', action='store_true', help='Return candidate IDs for a subsequent thread read')
    get = gmail.add_parser('get'); get.add_argument('message_id')
    send = gmail.add_parser('send'); send.add_argument('--to', required=True); send.add_argument('--subject', default=''); send.add_argument('--body', required=True)
    reply = gmail.add_parser('reply'); reply.add_argument('message_id'); reply.add_argument('--body', required=True)
    calendar = domains.add_parser('calendar').add_subparsers(dest='verb', required=True)
    listing = calendar.add_parser('list'); listing.add_argument('--start', required=True); listing.add_argument('--end', required=True)
    get_event = calendar.add_parser('get'); get_event.add_argument('event_id'); get_event.add_argument('--calendar', default='primary')
    create = calendar.add_parser('create')
    create.add_argument('--summary', required=True); create.add_argument('--start', required=True); create.add_argument('--end', required=True)
    create.add_argument('--event-id', default='')
    create.add_argument('--attendees', default=''); create.add_argument('--location', default=''); create.add_argument('--description', default='')
    patch = calendar.add_parser('patch'); patch.add_argument('event_id'); patch.add_argument('--calendar', default='primary')
    patch.add_argument('--attendees-json'); patch.add_argument('--send-updates', choices=['all', 'none'], default='all')
    patch.add_argument('--location'); patch.add_argument('--description')
    delete = calendar.add_parser('delete'); delete.add_argument('event_id'); delete.add_argument('--calendar', default='primary')
    return parser


def execute(args, service_factory=_service):
    if (args.domain, args.verb) in WRITE_VERBS and os.environ.get('SOTTO_UNATTENDED'):
        raise PermissionError('Google account writes are unavailable in unattended runs')
    if args.domain == 'gmail':
        service = service_factory('gmail', 'v1')
        try:
            if args.verb == 'search':
                return _gmail_search(service, args.query, args.max, service_factory, ids_only=args.ids_only)
            if args.verb == 'get':
                return _shared_library().fetch_message(service, args.message_id)
            if args.verb == 'send':
                return _gmail_send(service, to=args.to, subject=args.subject, body=args.body)
            if args.verb == 'reply':
                return _gmail_send(service, body=args.body, reply_message_id=args.message_id)
        finally:
            close = getattr(service, 'close', None)
            if callable(close):
                close()
    else:
        service = service_factory('calendar', 'v3')
        try:
            if args.verb == 'list':
                rows = _calendar_list(service, args.start, args.end)
                return list(rows) if rows.complete else {'events': list(rows), 'complete': False, 'calendars': rows.calendars}
            if args.verb == 'get':
                return service.events().get(calendarId=args.calendar, eventId=args.event_id).execute()
            if args.verb == 'create':
                return _calendar_create(service, args)
            if args.verb == 'patch':
                return _calendar_patch(service, args)
            if args.verb == 'delete':
                service.events().delete(calendarId=args.calendar, eventId=args.event_id, sendUpdates='all').execute()
                return {'status': 'deleted', 'id': args.event_id}
        finally:
            close = getattr(service, 'close', None)
            if callable(close):
                close()
    raise ValueError('unsupported Google command')


_OWNER_MESSAGE_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_OWNER_EVENT_ID = re.compile(r'[A-Za-z0-9_@.-]{1,256}\Z')
_OWNER_RESPONSE_BYTES = 64 * 1024
_OWNER_GMAIL_WIRE_BYTES = 256 * 1024
_OWNER_READ_DEADLINE_SECONDS = 15


def _owner_unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Google result has duplicate fields')
        result[key] = value
    return result


def _owner_invalid_number(_value):
    raise ValueError('Google result has invalid number')


def _owner_text(value, limit):
    if not isinstance(value, str) or len(value.encode('utf-8')) > limit:
        raise ValueError('Google result exceeds its bound')
    return value


def _owner_projection(kind, row):
    if not isinstance(row, dict):
        raise ValueError('Google result is invalid')
    if kind == 'gmail':
        fields = {'id': 128, 'threadId': 128, 'from': 1024, 'to': 1024, 'cc': 1024,
                  'subject': 2048, 'date': 128, 'snippet': 4096, 'body': 32 * 1024}
        result = {name: _owner_text(row.get(name) or '', bound) for name, bound in fields.items()}
        labels = row.get('labels') or row.get('labelIds') or []
        if not isinstance(labels, list) or len(labels) > 40:
            raise ValueError('Google result exceeds its bound')
        result['labels'] = [_owner_text(label, 128) for label in labels]
        return result
    start = row.get('start') or {}
    end = row.get('end') or {}
    attendees = row.get('attendees') or []
    if (not isinstance(start, dict) or not isinstance(end, dict)
            or not isinstance(attendees, list) or len(attendees) > 100
            or any(not isinstance(item, dict) for item in attendees)):
        raise ValueError('Google result exceeds its bound')
    _shared_library()
    from calendar_context import _self_describes_owner, user_participates  # noqa: PLC0415
    from timeutil import configured_user_email  # noqa: PLC0415
    owner_email = configured_user_email().strip().lower()
    return {'owner_participates': user_participates(row, owner_email),
            **{key: _owner_text(row[key], 128) for key in ('transparency', 'eventType') if key in row},
            **({'calendar_id': _owner_text(row['calendar_id'], 1024)} if row.get('calendar_id') else {}),
            **({'originalStartTime': {key: _owner_text(value, 128) for key, value in row['originalStartTime'].items() if key in ('dateTime', 'date', 'timeZone')}} if isinstance(row.get('originalStartTime'), dict) else {}),
            **({'recurringEventId': _owner_text(row['recurringEventId'], 256)} if row.get('recurringEventId') else {}),
            'id': _owner_text(row.get('id') or '', 256),
            'summary': _owner_text(row.get('summary') or '', 2048),
            'description': _owner_text(row.get('description') or '', 16 * 1024),
            'location': _owner_text(row.get('location') or '', 2048),
            'status': _owner_text(row.get('status') or '', 128),
            'start': {key: _owner_text(start[key], 128) for key in ('dateTime', 'date', 'timeZone') if key in start},
            'end': {key: _owner_text(end[key], 128) for key in ('dateTime', 'date', 'timeZone') if key in end},
            'attendees': [{'email': _owner_text(item.get('email') or '', 320),
                           'responseStatus': _owner_text(item.get('responseStatus') or '', 128),
                           'owner': bool((item.get('self') and _self_describes_owner(row)) or
                                         (owner_email and str(item.get('email') or '').strip().lower() == owner_email))}
                          for item in attendees]}


def _owner_thread_projection(raw, thread_id):
    """Select a bounded newest suffix; never trust provider array order or mixed identities."""
    messages = raw.get('messages')
    if (raw.get('id') != thread_id or not isinstance(messages, list) or not messages):
        raise ValueError('Google thread identity is invalid')
    seen = set()
    for message in messages:
        if (not isinstance(message, dict) or not isinstance(message.get('id'), str)
                or _OWNER_MESSAGE_ID.fullmatch(message['id']) is None
                or message['id'] in seen or message.get('threadId') != thread_id
                or not isinstance(message.get('internalDate'), str)
                or re.fullmatch(r'[0-9]{1,16}', message['internalDate']) is None):
            raise ValueError('Google thread message identity is invalid')
        labels = message.get('labelIds', [])
        if (not isinstance(labels, list) or len(labels) > 40
                or any(not isinstance(label, str) or len(label.encode('utf-8')) > 128 for label in labels)):
            raise ValueError('Google thread labels are invalid')
        seen.add(message['id'])
    # Drafts are unsent working copies, never correspondence or completion proof.
    correspondence = [message for message in messages if 'DRAFT' not in message.get('labelIds', [])]
    selected = sorted(correspondence, key=lambda row: (int(row['internalDate']), row['id']))[-20:]
    items = []
    for message in selected:
        fields = _shared_library().message_fields(message)
        body = fields.get('body') or ''
        if not isinstance(body, str):
            raise ValueError('Google thread body is invalid')
        encoded = body.encode('utf-8')
        truncated = len(encoded) > 16 * 1024
        fields['body'] = encoded[:16 * 1024].decode('utf-8', errors='ignore')
        item = _owner_projection('gmail', fields)
        item.update(body_truncated=truncated, internal_date=message['internalDate'])
        items.append(item)
    # Leave space for the observation envelope. Remove oldest whole messages, not
    # an arbitrary provider-order prefix that could hide the latest reply.
    while len(json.dumps(items).encode('utf-8')) > 48 * 1024:
        items.pop(0)
    if not items:
        raise ValueError('Google thread has no message within its response bound')
    omitted = len(correspondence) - len(items)
    return {'thread_id': thread_id, 'items': items, 'omitted_messages': omitted}, (
        omitted == 0 and not any(item['body_truncated'] for item in items))


def _owner_gmail_session():
    from google.oauth2.credentials import Credentials  # noqa: PLC0415
    from google.auth.transport.requests import AuthorizedSession, Request  # noqa: PLC0415
    from requests import Session  # noqa: PLC0415
    from requests.adapters import HTTPAdapter  # noqa: PLC0415
    path = _shared_library().token_path()
    if not path:
        raise RuntimeError('Google credential root is unavailable')
    auth_transport = Session()
    auth_transport.mount('https://', HTTPAdapter(max_retries=0))
    authorized = AuthorizedSession(Credentials.from_authorized_user_file(path),
        auth_request=Request(auth_transport), max_refresh_attempts=0, refresh_timeout=5)
    authorized.mount('https://', HTTPAdapter(max_retries=0))
    class OwnerSession:
        def post(self, *args, **kwargs):
            return authorized.post(*args, **kwargs)
        def get(self, *args, **kwargs):
            return authorized.get(*args, **kwargs)
        def close(self):
            authorized.close()
            auth_transport.close()
    return OwnerSession()


def _owner_gmail_service():
    """Fixed GET facade and POST share one credential/session for reviewed sends."""
    from types import SimpleNamespace  # noqa: PLC0415
    session = _owner_gmail_session()
    class Service:
        def users(self):
            return self
        def messages(self):
            return self
        def getProfile(self, **kwargs):  # noqa: N802 -- original Google resource interface

            if kwargs != {'userId': 'me'}:
                raise ValueError('Gmail profile target invalid')
            return SimpleNamespace(execute=lambda: _owner_fetch(session,
                'https://gmail.googleapis.com/gmail/v1/users/me/profile', {}, time.monotonic() + 10))
        def get(self, **kwargs):
            if (set(kwargs) != {'userId', 'id', 'format'} or kwargs['userId'] != 'me'
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', kwargs['id'])
                    or kwargs['format'] not in ('full', 'raw')):
                raise ValueError('Gmail message target invalid')
            return SimpleNamespace(execute=lambda: _owner_fetch(session,
                'https://gmail.googleapis.com/gmail/v1/users/me/messages/' + kwargs['id'],
                {'format': kwargs['format']}, time.monotonic() + 10))
        def send_reviewed(self, payload):
            return _owner_send(payload, session)
        def close(self):
            session.close()
    return Service()


def _owner_send(payload, session=None):
    """One non-idempotent POST: no redirects, auth/resource retries or fallback."""
    owned = session is None
    session, response = session or _owner_gmail_session(), None
    try:
        response = session.post('https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
            json=payload, stream=True, timeout=10, max_allowed_time=15, allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError('Gmail send unconfirmed')
        chunks, total = [], 0
        deadline = time.monotonic() + 5
        for chunk in response.iter_content(chunk_size=4096):
            total += len(chunk)
            if total > 16384 or time.monotonic() >= deadline:
                raise ValueError('Gmail send receipt unavailable')
            chunks.append(chunk)
        result = json.loads(b''.join(chunks).decode('utf-8'),
            object_pairs_hook=_owner_unique_json, parse_constant=_owner_invalid_number)
        if not isinstance(result, dict):
            raise ValueError('Gmail send receipt invalid')
        return result
    finally:
        if response is not None:
            response.close()
        if owned:
            session.close()


def _owner_fetch(session, url, params, deadline, *, phase=None):
    """Bound every typed Google GET before JSON parsing or MIME projection."""
    response = None
    stage = 'provider_transport'
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stage = 'provider_deadline'
            raise TimeoutError('Google read deadline expired')
        response = session.get(url, params=params, stream=True, timeout=min(5, remaining),
                               max_allowed_time=remaining,
                               allow_redirects=False)
        # Gmail's field-projected messages list can return 204 when no messages
        # or next-page token exist. This is an empty search, not a failed read.
        # Other operations still require a representation (especially exact gets).
        if response.status_code == 204 and phase == 'search_list':
            return {}
        if response.status_code != 200:
            status = response.status_code
            stage = ({400: 'provider_bad_request', 401: 'provider_unauthorized',
                      403: 'provider_forbidden', 429: 'provider_rate_limited'}.get(status, 'provider_http_other')
                     if type(status) is int else 'provider_http_other')
            if type(status) is int and 500 <= status <= 599:
                stage = 'provider_server'
            raise RuntimeError('Google message read unavailable')
        stage = 'provider_bounds'
        content_length = response.headers.get('Content-Length')
        if content_length is not None and int(content_length) > _OWNER_GMAIL_WIRE_BYTES:
            raise ValueError('Google message exceeds its wire bound')
        stage = 'provider_body'
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=8192):
            if time.monotonic() >= deadline:
                stage = 'provider_deadline'
                raise TimeoutError('Google read deadline expired')
            total += len(chunk)
            if total > _OWNER_GMAIL_WIRE_BYTES:
                stage = 'provider_bounds'
                raise ValueError('Google message exceeds its wire bound')
            chunks.append(chunk)
        stage = 'provider_decode'
        raw = json.loads(b''.join(chunks).decode('utf-8'),
                         object_pairs_hook=_owner_unique_json,
                         parse_constant=_owner_invalid_number)
        stage = 'provider_shape'
        if not isinstance(raw, dict):
            raise ValueError('Google result is invalid')
        return raw
    except Exception as error:
        error.sotto_read_stage = stage
        if phase in ('search_list', 'search_metadata', 'message_get'):
            error.sotto_read_phase = phase
        status = getattr(response, 'status_code', None)
        if type(status) is int and 100 <= status <= 599:
            error.sotto_read_http_status = status
        raise
    finally:
        if response is not None:
            response.close()


def read_owner(operation, arguments, session_factory=_owner_gmail_session):
    """Fixed read-only operations for the authenticated receiver owner route.

    This function neither checks consent nor issues a grant. The receiver must
    fence its current source authority before and after this provider call.
    """
    if not isinstance(arguments, dict):
        raise ValueError('Google request is invalid')
    if operation not in ('gmail_search', 'gmail_get', 'gmail_thread_get', 'calendar_list', 'calendar_get',
                         'contacts_search', 'contacts_get'):
        raise ValueError('Google request is invalid')
    session = None
    stage = 'credential_setup'
    try:
        session = session_factory()
        stage = 'projection'
        return _read_owner_with_session(operation, arguments, session)
    except Exception as error:
        # A closed enum only; never attach provider messages, arguments or source content.
        if not getattr(error, 'sotto_read_stage', None):
            error.sotto_read_stage = ('input_validation' if stage == 'projection'
                and isinstance(error, ValueError) and str(error) == 'Google request is invalid' else stage)
        raise
    finally:
        if session is not None:
            session.close()


def _read_owner_with_session(operation, arguments, session):
    source = ('gmail' if operation.startswith('gmail_') else
              'google_contacts' if operation.startswith('contacts_') else 'calendar')
    deadline = time.monotonic() + _OWNER_READ_DEADLINE_SECONDS
    if operation in ('contacts_search', 'contacts_get'):
        # Share the original writer's contact projection; never expose unrelated
        # People profile data or treat a bounded prefix search as a full address book.
        _shared_library()
        from contact_snapshot import CONTACT_MASK, RESOURCE, snapshot  # noqa: PLC0415
        if operation == 'contacts_search' and set(arguments) == {'query', 'max'}:
            query, limit = arguments['query'], arguments['max']
            if (not isinstance(query, str) or not 1 <= len(query) <= 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in query)
                    or type(limit) is not int or not 1 <= limit <= 30):
                raise ValueError('Google request is invalid')
            url = 'https://people.googleapis.com/v1/people:searchContacts'
            params = {'readMask': CONTACT_MASK, 'pageSize': limit,
                      'sources': ['READ_SOURCE_TYPE_CONTACT']}
            _owner_fetch(session, url, {**params, 'query': ''}, deadline)
            page = _owner_fetch(session, url, {**params, 'query': query}, deadline)
            rows = page.get('results', [])
            if (not isinstance(rows, list) or len(rows) > limit
                    or any(not isinstance(row, dict) or not isinstance(row.get('person'), dict)
                           for row in rows)):
                raise ValueError('Google result exceeds its bound')
            result = {'items': [snapshot(row['person']) for row in rows]}
            complete = len(rows) < limit
        elif operation == 'contacts_get' and set(arguments) == {'resource_name'}:
            resource = arguments['resource_name']
            if not isinstance(resource, str) or len(resource) > 256 or RESOURCE.fullmatch(resource) is None:
                raise ValueError('Google request is invalid')
            row = _owner_fetch(session, 'https://people.googleapis.com/v1/' + resource,
                {'personFields': CONTACT_MASK, 'sources': ['READ_SOURCE_TYPE_CONTACT']}, deadline)
            item = snapshot(row)
            if item['resource_name'] != resource:
                raise ValueError('Google returned a different contact')
            result = {'item': item}
            complete = True
        else:
            raise ValueError('Google request is invalid')
        coverage = {'since': None, 'until': None}
    elif operation == 'gmail_search' and set(arguments) == {'query', 'max'}:
        query, limit = arguments['query'], arguments['max']
        if (not isinstance(query, str) or not 1 <= len(query) <= 256
                or any(ord(char) < 32 or ord(char) == 127 for char in query)
                or type(limit) is not int or not 1 <= limit <= 20):
            raise ValueError('Google request is invalid')
        page = _owner_fetch(session, 'https://gmail.googleapis.com/gmail/v1/users/me/messages',
            {'q': query, 'maxResults': limit, 'fields': 'messages(id,threadId),nextPageToken'}, deadline, phase='search_list')
        items = page.get('messages') or []
        if (not isinstance(items, list) or len(items) > limit
                or any(not isinstance(item, dict) or not isinstance(item.get('id'), str)
                       or _OWNER_MESSAGE_ID.fullmatch(item['id']) is None for item in items)):
            raise ValueError('Google result exceeds its bound')
        rows = []
        for item in items:
            raw = _owner_fetch(session, 'https://gmail.googleapis.com/gmail/v1/users/me/messages/' + item['id'],
                {'format': 'metadata', 'metadataHeaders': ['From', 'To', 'Cc', 'Subject', 'Date'],
                 'fields': 'id,threadId,labelIds,snippet,internalDate,payload(headers)'}, deadline, phase='search_metadata')
            headers = _mail_headers(raw)
            rows.append({'id': raw.get('id') or item['id'], 'threadId': raw.get('threadId') or item.get('threadId'),
                'from': headers.get('from', ''), 'to': headers.get('to', ''), 'cc': headers.get('cc', ''),
                'subject': headers.get('subject', ''), 'date': headers.get('date') or raw.get('internalDate') or '',
                'snippet': raw.get('snippet') or '', 'labels': raw.get('labelIds') or []})
        result = {'items': [_owner_projection('gmail', row) for row in rows]}
        complete = not bool(page.get('nextPageToken'))
        coverage = {'since': None, 'until': None}
    elif operation == 'gmail_thread_get' and set(arguments) == {'thread_id'}:
        thread_id = arguments['thread_id']
        if not isinstance(thread_id, str) or _OWNER_MESSAGE_ID.fullmatch(thread_id) is None:
            raise ValueError('Google request is invalid')
        raw = _owner_fetch(session, 'https://gmail.googleapis.com/gmail/v1/users/me/threads/' + thread_id,
            {'format': 'full', 'fields': 'id,messages(id,threadId,labelIds,snippet,internalDate,payload)'}, deadline)
        result, complete = _owner_thread_projection(raw, thread_id)
        coverage = {'since': None, 'until': None}
    elif operation == 'gmail_get' and set(arguments) == {'message_id'}:
        message_id = arguments['message_id']
        if not isinstance(message_id, str) or _OWNER_MESSAGE_ID.fullmatch(message_id) is None:
            raise ValueError('Google request is invalid')
        raw = _owner_fetch(session, 'https://gmail.googleapis.com/gmail/v1/users/me/messages/' + message_id,
            {'format': 'full', 'fields': 'id,threadId,labelIds,snippet,internalDate,payload'}, deadline, phase='message_get')
        row = _shared_library().message_fields(raw)
        result = {'item': _owner_projection('gmail', row)}
        complete = True
        coverage = {'since': None, 'until': None}
    elif operation == 'calendar_list' and set(arguments) == {'start', 'end'}:
        start, end = arguments['start'], arguments['end']
        if (not isinstance(start, str) or not isinstance(end, str)
                or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', start)
                or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', end)):
            raise ValueError('Google request is invalid')
        from datetime import timedelta  # noqa: PLC0415
        if not datetime.fromisoformat(start.replace('Z', '+00:00')) < datetime.fromisoformat(end.replace('Z', '+00:00')) <= datetime.fromisoformat(start.replace('Z', '+00:00')) + timedelta(days=31):
            raise ValueError('Google request is invalid')
        _shared_library()
        from google_calendar_read import read  # noqa: PLC0415 - packaged shared library resolved above
        def inventory(page):
            return _owner_fetch(session, 'https://www.googleapis.com/calendar/v3/users/me/calendarList',
                {'maxResults': 250, 'minAccessRole': 'reader', 'showHidden': 'false',
                 'fields': 'items(id,accessRole,deleted,hidden,primary),nextPageToken',
                 **({'pageToken': page} if page else {})}, deadline)
        def events(calendar_id, page):
            return _owner_fetch(session, 'https://www.googleapis.com/calendar/v3/calendars/' + quote(calendar_id, safe='') + '/events',
                {'timeMin': start, 'timeMax': end, 'singleEvents': 'true', 'orderBy': 'startTime', 'maxResults': 100,
                 'fields': 'items(id,iCalUID,summary,description,location,status,transparency,eventType,start,end,recurringEventId,originalStartTime,organizer(email,self),attendees(email,responseStatus,self,resource)),nextPageToken',
                 **({'pageToken': page} if page else {})}, deadline)
        rows = read(inventory, events, max_items=100)
        from calendar_context import meeting_events  # noqa: PLC0415
        from timeutil import configured_user_email  # noqa: PLC0415
        order = {(row['calendar_id'], row['id']): index for index, row in enumerate(rows)}
        distinct = sorted(meeting_events(rows, configured_user_email()),
                          key=lambda row: order[(row['calendar_id'], row['id'])])
        result = {'items': [_owner_projection('calendar', row) for row in distinct[:100]], 'calendars': rows.calendars}
        complete = rows.complete and len(rows) <= 100
        coverage = {'since': start, 'until': end}
    elif operation == 'calendar_get' and set(arguments) in ({'event_id'}, {'event_id', 'calendar_id'}):
        event_id = arguments['event_id']
        if not isinstance(event_id, str) or _OWNER_EVENT_ID.fullmatch(event_id) is None:
            raise ValueError('Google request is invalid')
        calendar_id = arguments.get('calendar_id', 'primary')
        if (not isinstance(calendar_id, str) or not calendar_id or calendar_id != calendar_id.strip()
                or len(calendar_id)>1024 or any(c.isspace() or ord(c)<32 or 127<=ord(c)<=159
                    or 0xD800<=ord(c)<=0xDFFF for c in calendar_id)
                or len(calendar_id.encode('utf-16-le'))//2>2048):
            raise ValueError('Google request is invalid')
        _shared_library()
        from google_calendar_read import selection  # noqa: PLC0415 - packaged library resolved above
        chosen = selection()
        if chosen['mode'] == 'selected' and calendar_id not in chosen['calendar_ids']:
            raise ValueError('Calendar is outside the current read selection')
        primary = calendar_id == 'primary'
        if calendar_id != 'primary':
            try:
                visible = _owner_fetch(session, 'https://www.googleapis.com/calendar/v3/users/me/calendarList/' + quote(calendar_id, safe=''),
                    {'fields': 'id,hidden,deleted,accessRole,primary'}, deadline)
            except RuntimeError:
                if chosen['mode'] != 'selected':
                    raise
                # Explicit selections may have event-only grants; discovery failure cannot
                # prove primary ownership, but does not revoke an authorized event read.
                visible = None
            if visible is not None and (visible.get('id') != calendar_id or
                    (chosen['mode'] == 'all' and (visible.get('hidden') or visible.get('deleted') or
                     visible.get('accessRole') not in ('reader', 'writer', 'writerWithoutPrivateAccess', 'owner')))):
                raise ValueError('Calendar is outside the current read selection')
            primary = visible is not None and visible.get('primary') is True
        row = _owner_fetch(session, 'https://www.googleapis.com/calendar/v3/calendars/' +
            quote(calendar_id, safe='') + '/events/' + quote(event_id, safe=''),
            {'fields': 'id,summary,description,location,status,transparency,eventType,start,end,recurringEventId,originalStartTime,organizer(email,self),attendees(email,responseStatus,self,resource)'}, deadline)
        if 'calendar_id' in arguments and (not isinstance(row, dict) or row.get('id') != event_id):
            raise ValueError('Google event identity is invalid')
        result = {'item': _owner_projection('calendar', {**row, 'calendar_id': calendar_id, '_calendar_primary': primary})}
        if 'calendar_id' not in arguments:
            result['item'].pop('calendar_id', None)
        complete = True
        coverage = {'since': None, 'until': None}
    else:
        raise ValueError('Google request is invalid')
    result.update(version=1, source=source, operation=operation,
                  observation={'status': 'ok' if complete else 'partial',
                               'observed_at': datetime.now(timezone.utc).isoformat(),
                               'complete': complete, 'coverage': coverage})
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > _OWNER_RESPONSE_BYTES:
        raise ValueError('Google result exceeds its bound')
    return result


_PROVIDER_REASON_BYTES = 16 * 1024
_PROVIDER_REASONS = frozenset({
    'rateLimitExceeded', 'userRateLimitExceeded', 'quotaExceeded',
    'insufficientPermissions', 'forbidden', 'authError',
    'accessNotConfigured', 'backendError',
})


def _provider_reason(error):
    """Read only Google's bounded structured reason; never copy provider text."""
    if type(error).__name__ != 'HttpError':
        return None
    try:
        content = getattr(error, 'content', None)
        if type(content) not in (bytes, str) or len(content) > _PROVIDER_REASON_BYTES:
            return None
        if type(content) is str:
            content = content.encode('utf-8')
            if len(content) > _PROVIDER_REASON_BYTES:
                return None
        payload = json.loads(content)
        detail = payload.get('error') if isinstance(payload, dict) else None
        errors = detail.get('errors') if isinstance(detail, dict) else None
        if isinstance(errors, list):
            for item in errors[:16]:
                reason = item.get('reason') if isinstance(item, dict) else None
                if type(reason) is str and reason in _PROVIDER_REASONS:
                    return reason
    except Exception:  # noqa: BLE001 — malformed diagnostics must not replace the refusal
        pass
    return None


def _failure_diagnostic(error):
    """Emit only fixed categories/reasons and a bounded numeric provider status."""
    categories = {
        'HttpError': 'provider_http', 'HTTPError': 'provider_http',
        'RefreshError': 'credential_refresh', 'TransportError': 'transport',
        'Timeout': 'timeout', 'TimeoutError': 'timeout',
        'ConnectionError': 'transport', 'PermissionError': 'permission',
        'ValueError': 'validation', 'FileNotFoundError': 'credential_file',
    }
    category = categories.get(type(error).__name__, 'unavailable')
    status = None
    try:
        for attribute, field in (('resp', 'status'), ('response', 'status_code')):
            candidate = getattr(getattr(error, attribute, None), field, None)
            if type(candidate) is int and 100 <= candidate <= 599:
                status = candidate
                break
    except Exception:  # noqa: BLE001 — diagnostics must never replace the refusal
        pass
    reason = _provider_reason(error)
    return (category + (f' http_status={status}' if status is not None else '')
            + (f' provider_reason={reason}' if reason is not None else ''))


def main(argv=None, service_factory=_service):
    args = build_parser().parse_args(argv)
    try:
        result = execute(args, service_factory)
    except Exception as error:  # noqa: BLE001 — provider errors may contain source bodies or credentials
        print('Google operation unavailable or refused. diagnostic=' + _failure_diagnostic(error), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
