"""One ready draft from an exact cached meeting commitment in a delivered brief."""
from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
import re
from subprocess import SubprocessError
import sys

import delivery_effects
import pending_offer
from ledger_provenance import source_refs, ref_fields, source_loop_sources
from message_match import _email_direction_known, _email_recipients, _is_sent_email
from protected_ledger_io import load_protected_scope
from source_authority import current_authority
from source_context import allowed
from timeutil import parse_observed_time

ACTION = 'contextual_draft'
QUESTION = 'Reply “Review draft” to see it here, or “Hide” to dismiss the offer.'
LOOKAHEAD_DAYS = 3
EMAIL = re.compile(r'[a-z0-9][a-z0-9_.+%-]*@[a-z0-9.-]+\.[a-z]{2,}', re.I)
REPLY_SUBJECT = re.compile(r'^(?:(?:re|fw|fwd):\s*)+', re.I)
_CAP = 12
_SENT_LOOKUP_SECONDS = 8
_MAX_DRAFT_CHARS = 1500
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029\[\]`<>]")
_REVISION = re.compile(r'[a-f0-9]{64}')
_OFFER_ID = re.compile(r'(?:[a-f0-9]{32}|[a-f0-9]{64})')
_REVIEW = re.compile(r'review draft[.!]?', re.I)
_PROVIDER_MESSAGE_ID = re.compile(r'[^\s\x00-\x1f\x7f\ud800-\udfff]{1,512}')


def cached_followup(meeting, email):
    path = str(Path(__file__).resolve().parents[2] / 'followup/scripts')
    if path not in sys.path:
        sys.path.insert(0, path)
    from capture_commitments import cached_followup as read
    return read(meeting, email)


def _single_line(value, limit):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit and not _CONTROL.search(value)


def _subject(value):
    return REPLY_SUBJECT.sub('', value.strip()).casefold()


def _sent_matches(emails, recipient, subject, after):
    for email in emails:
        if (not isinstance(email, dict) or not _email_direction_known(email) or not _is_sent_email(email)
                or recipient not in _email_recipients(email) or not isinstance(email.get('subject'), str)
                or _subject(email['subject']) != _subject(subject)):
            continue
        stamp = parse_observed_time(email.get('date') or email.get('timestamp'))
        if stamp and stamp > after:
            return True
    return False


def _open_row(anchor):
    rows, complete = load_protected_scope(os.environ.get('SOTTO_DATA', '/data'), [anchor])
    if complete and len(rows) == 1 and rows[0].get('status') == 'open' and rows[0].get('action_type') == 'follow_up':
        return rows[0]
    return None


def _candidate(meeting, email, now, authority):  # noqa: PLR0911 -- independent evidence gates
    meeting_id = meeting.get('meeting_id') or meeting.get('id')
    ended = parse_observed_time(meeting.get('end'))
    title = meeting.get('title')
    if not (meeting_id and _single_line(title, 120) and ended and now - timedelta(days=4) <= ended <= now):
        return None
    cache = cached_followup(meeting, email) or {}
    anchors = (cache.get('ledger') or {}).get('recorded_anchor_keys') or []
    # The prepared draft must map to a single canonical ask from this exact occurrence.
    if len(anchors) != 1:
        return None
    row = _open_row(anchors[0])
    if not row or not _single_line(row.get('ask'), 160):
        return None
    recipient = row.get('contact_identifier') or ''
    attendees = meeting.get('attendee_emails') or []
    if (not EMAIL.fullmatch(recipient) or recipient.casefold() == email.casefold()
            or recipient.casefold() not in {address.casefold() for address in attendees if isinstance(address, str)}):
        return None
    try:
        due = date.fromisoformat(row.get('deadline') or '')
    except ValueError:
        return None
    if not 0 <= (due - now.date()).days <= LOOKAHEAD_DAYS:
        return None
    sources = sorted(set(source_loop_sources(row)) | {'gmail', 'granola'})
    available = {source for key in ('bridge_sources', 'google_sources', 'connector_sources')
                 for source in authority.get(key, [])}
    if not set(sources) <= available:
        return None
    quotes = {snippet for source, ident, snippet in map(ref_fields, source_refs(row))
              if source == 'granola' and ident == meeting_id and snippet}
    drafts = [draft for draft in (cache.get('followup') or {}).get('drafts', []) if isinstance(draft, dict)
              and draft.get('channel') == 'email' and draft.get('to_email') == recipient
              and draft.get('commitment_quote') in quotes and _single_line(draft.get('to_name'), 100)
              and _single_line(draft.get('subject'), 140) and isinstance(draft.get('body'), str)
              and 0 < len(draft['body'].strip()) <= _MAX_DRAFT_CHARS and '\x00' not in draft['body']]
    if len(drafts) != 1:
        return None
    detail = {key: drafts[0][key] for key in ('to_name', 'to_email', 'channel', 'subject', 'body')}
    return due, ended, row, {**detail, 'version': 1, 'sources': sources, 'source_revision': authority['revision']}


def append_offer(artifact, meetings, emails, owner_email, now):
    """Append after the brief's own model work. Reuses cache and starts no extraction."""
    if (not delivery_effects.run_id() or os.environ.get('SOTTO_CRON_DELIVER') != 'photon'
            or not owner_email or not all(allowed(source) for source in ('gmail', 'granola'))):
        return
    try:
        emails = emails.get('emails', []) if isinstance(emails, dict) else emails
        authority = current_authority()
        if authority.get('complete') is not True or not _REVISION.fullmatch(authority.get('revision') or ''):
            return
        offers = []
        for meeting in meetings:
            result = _candidate(meeting, owner_email, now, authority)
            if not result:
                continue
            due, ended, row, detail = result
            identity = pending_offer.payload_hash(f"{ACTION}:{row['anchor_key']}".encode())
            if not pending_offer.was_accepted(identity) and not _sent_matches(
                    emails, detail['to_email'].casefold(), detail['subject'], ended):
                offers.append((due, ended, identity, row, detail, meeting['title']))
        if offers:
            due, ended, identity, row, detail, title = min(offers, key=lambda item: (item[0], -item[1].timestamp()))
            raw = json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            record = pending_offer.set_offer('followup_draft', QUESTION, person=detail['to_name'], detail=raw,
                payload_sha256=pending_offer.payload_hash(raw.encode()), anchor_key=row['anchor_key'],
                action=ACTION, prepare_only=True)
            record.update(offer_id=identity, observed_end=ended.isoformat(), loop_version=delivery_effects.loop_version(row),
                          expires_at=datetime.combine(due + timedelta(days=1), time.min, tzinfo=now.tzinfo).isoformat())
            ask = row['ask'].strip().rstrip(' .?!')
            source_day = ended.astimezone(now.tzinfo).strftime('%a, %b %d').replace(' 0', ' ')
            due_text = 'Due today' if due == now.date() else 'Due ' + due.strftime('%A, %b %d').replace(' 0', ' ')
            copy = f"{ask[:1].upper() + ask[1:]}? For {detail['to_name']}. {due_text}. From your {title} notes · {source_day}.\n{QUESTION}"
            artifact['brief_markdown'] = artifact.get('brief_markdown', '').rstrip() + '\n\n**I can help**\n' + copy
            artifact['brief_text'] = artifact['brief_text'].rstrip() + '\n\nI can help\n' + copy
            artifact['_contextual_offer'] = record
            artifact['_source_permissions'] = sorted(set(artifact.get('_source_permissions', [])) | set(detail['sources']))
    except Exception as error:  # optional assistance must leave a useful brief deliverable
        print(f'[brief_runner] follow-up offer skipped: {type(error).__name__}', file=sys.stderr)


def stage_offer(artifact):
    offer = artifact.get('_contextual_offer')
    if not isinstance(offer, dict):
        return
    try:
        if (delivery_effects.run_id() and not pending_offer.was_accepted(offer['offer_id'])
                and all(artifact.get(key, '').count(QUESTION) == 1 for key in ('brief_text', 'brief_markdown'))
                and is_current(offer, check_sent=True)):
            delivery_effects.stage([{'kind': 'pending_offer', 'offer': offer}])
            return
    except Exception as error:  # a missing optional offer must not hold the brief
        print(f'[brief_runner] follow-up offer withheld: {type(error).__name__}', file=sys.stderr)
    for key, heading in (('brief_text', 'I can help'), ('brief_markdown', '**I can help**')):
        artifact[key] = artifact.get(key, '').rsplit('\n\n' + heading + '\n', 1)[0]
    artifact.pop('_contextual_offer', None)


def prune_queued_offer(payload):
    """Recheck just before contact. A stale offer must leave neither text nor an acceptance effect."""
    effects = payload['effects']
    matching = [effect for effect in effects if effect.get('kind') == 'pending_offer'
                and effect.get('offer', {}).get('action') == ACTION]
    if len(matching) != 1:
        return None
    effect = matching[0]
    offer = effect['offer']
    try:
        if (pending_offer._fresh_offer(offer) and is_current(offer, check_sent=True)
                and is_current(offer) and not pending_offer.was_accepted(offer['offer_id'])):
            return payload
    except (OSError, ValueError, RuntimeError) as error:
        print(f'[brief_runner] queued follow-up offer withheld: {type(error).__name__}', file=sys.stderr)
    body, _, suffix = payload['body'].rpartition('\n\nI can help\n')
    if not body or '\n\n' in suffix or not suffix.endswith('\n' + QUESTION) or payload.get('presentation'):
        return None
    cleaned = {**payload, 'body': body, 'effects': [item for item in effects if item is not effect]}
    cleaned.pop('retained_text_artifact', None)
    return cleaned


def review_selection(args, body, listing):
    contextual = _REVIEW.fullmatch(body['text'].strip())
    keys = {'operation'} if listing else {'operation', 'offer_id'}
    optional = set() if listing else {'to'}
    if contextual:
        keys.add('message_id')
        optional = set()
    if (body['identity']['channel'] != 'photon' or not keys <= set(args) <= keys | optional
            or not listing and not (isinstance(args['offer_id'], str) and _OFFER_ID.fullmatch(args['offer_id']))):
        raise ValueError('followup draft selection invalid')
    message_id = args.get('message_id', '')
    if contextual and not (isinstance(message_id, str) and _PROVIDER_MESSAGE_ID.fullmatch(message_id)):
        raise ValueError('brief message proof invalid')
    return message_id


def is_current(offer, *, check_sent=False):
    if offer.get('action') != ACTION:
        return True
    row = _open_row(offer['anchor_key'])
    detail = json.loads(offer['detail'])
    if (not row or row.get('contact_identifier') != detail['to_email']
            or delivery_effects.loop_version(row) != offer.get('loop_version')):
        return False
    if not check_sent:
        return True
    from gather_google import _find_google_api, _search_gmail, mark_sent
    api, ended = _find_google_api(), parse_observed_time(offer.get('observed_end'))
    recipient = detail['to_email']
    if not api or not ended or not EMAIL.fullmatch(recipient):
        return False
    try:
        emails = _search_gmail(api, f'in:sent to:{recipient} after:{int(ended.timestamp())}', _CAP, 0,
                               _SENT_LOOKUP_SECONDS)
    except SubprocessError:
        return False
    return len(emails) < _CAP and not _sent_matches(
        (mark_sent(email) for email in emails), recipient.casefold(), detail['subject'], ended)
