"""Attended exact-reviewed Gmail draft saves and separately approved reply sends."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timezone
from email.utils import getaddresses
from contextlib import nullcontext
from runtime_owner_meeting_prep_request import selected_message_id, YES as OFFER_YES

from owner_tool_admission import validate_tool_admission, UnavailableError
from installed_writers import preferences_for
from fresh_routines import YES, digest
import owner_google_effect
from owner_google_effect import HEX, RefusalError, google as _google, readback_allowed as _readback_allowed
_preferences = preferences_for(__file__)

__all__ = ['perform', 'UnavailableError']
DOMAIN = 'sotto.owner-gmail-draft-tool.v1'
KIND = 'owner_gmail_draft_tool'
ID = re.compile(r'[A-Za-z0-9_-]{1,256}\Z')
REVIEW_FIELDS = {'message_id', 'thread_id', 'to', 'subject', 'body', 'reply_message_id', 'source_revision', 'review_hash'}
SEND_REVIEW_FIELDS = REVIEW_FIELDS | {'kind', 'sender_email', 'references'}
NEW_SEND_REVIEW_FIELDS = {'kind', 'sender_email', 'to', 'subject', 'body', 'source_revision', 'review_hash'}
NEW_REVIEW_FIELDS = {'kind', 'to', 'subject', 'body', 'source_revision', 'review_hash'}
SEND_YES = re.compile(r'(?:yes,?\s+)?send (?:it|this reply)[.!]?\Z', re.IGNORECASE)
OFFER_ID = re.compile(r'(?:[a-f0-9]{32}|[a-f0-9]{64})\Z')


class _PrecontactRefusalError(RefusalError):
    """The final guard refused before a reviewed draft reached Gmail."""


def _body(value):
    if (type(value) is not str or not value.strip() or len(value) > 8000
            or len(value.encode('utf-16-le', errors='surrogatepass')) // 2 > 16000
            or any((ord(c) < 32 and c not in '\t\r\n') or ord(c) == 127
                   or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('draft body invalid')


def _header(value, limit=512):
    if (type(value) is not str or not value or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise RefusalError('invalid_target')
    return value


def _review_hash(review):
    return digest({key: value for key, value in review.items() if key != 'review_hash'})


def _validate_review(review):
    new_send = type(review) is dict and review.get('kind') == 'new_send'
    sending = type(review) is dict and review.get('kind') in ('reply_send', 'new_send')
    new_email = type(review) is dict and review.get('kind') == 'new_email'
    fields = NEW_SEND_REVIEW_FIELDS if new_send else SEND_REVIEW_FIELDS if sending else NEW_REVIEW_FIELDS if new_email else REVIEW_FIELDS
    if type(review) is not dict or set(review) not in (fields, fields | {'draft_origin'}):
        raise ValueError('draft review invalid')
    if 'draft_origin' in review:
        _validate_draft_origin(review['draft_origin'])
        if new_email or new_send:
            raise ValueError('typed new email unsupported')
    _body(review['body'])
    for key in (() if new_email or new_send else ('message_id', 'thread_id')):
        if type(review[key]) is not str or not ID.fullmatch(review[key]):
            raise ValueError('draft provider identity invalid')
    for key in (('to',) if new_email or new_send else ('to', 'reply_message_id') if sending else ('to', 'subject', 'reply_message_id')):
        _header(review[key])
    if sending:
        _recipient(review['sender_email'])
        _recipient(review['to'])
        for key in (('subject',) if new_send else ('subject', 'references')):
            if type(review[key]) is not str or (review[key] and not _header(review[key], 2048 if key == 'references' else 512)):
                raise ValueError('send header invalid')
    if new_send:
        _new_subject(review['subject'])
        _new_subject(review['to'])
    if new_email:
        if type(review['subject']) is not str or (review['subject'] and not _header(review['subject'])):
            raise ValueError('draft subject invalid')
        _recipient(review['to'])
    if (type(review['source_revision']) is not str or type(review['review_hash']) is not str
            or not HEX.fullmatch(review['source_revision']) or not HEX.fullmatch(review['review_hash'])
            or _review_hash(review) != review['review_hash']):
        raise ValueError('draft review hash invalid')


def _recipient(value):
    _header(value, 254)
    if not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+', value):
        raise RefusalError('invalid_target')
    return value


def _authority(policy, google, revision=None, *, sending=False, read_only=False):
    grants = ('gmail_read',) if read_only else ('gmail_read', 'gmail_send' if sending else 'gmail_draft')
    return owner_google_effect.authority(policy, google, revision, source='gmail', grants=grants,
        refusal='send_permission_unavailable' if sending else 'draft_permission_unavailable')


def _resolve(service, message_id, body, revision, *, review_capacity=4000):
    if type(message_id) is not str or not ID.fullmatch(message_id):
        raise ValueError('draft message identity invalid')
    message = service.users().messages().get(userId='me', id=message_id, format='full').execute()
    if (not isinstance(message, dict) or message.get('id') != message_id
            or type(message.get('threadId')) is not str or not ID.fullmatch(message['threadId'])):
        raise RefusalError('invalid_target')
    headers = {}
    for header in (message.get('payload') or {}).get('headers', []):
        name = str(header.get('name', '')).lower()
        if name in ('reply-to', 'from', 'subject', 'message-id'):
            if name in headers:
                raise RefusalError('invalid_target')
            headers[name] = _header(header.get('value'))
    raw = headers.get('reply-to', headers.get('from', ''))
    addresses = getaddresses([raw])
    if len(addresses) != 1 or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+', addresses[0][1]):
        raise RefusalError('invalid_target')
    recipient = _header(addresses[0][1], 254)
    profile = service.users().getProfile(userId='me').execute()
    if not isinstance(profile, dict) or type(profile.get('emailAddress')) is not str:
        raise RefusalError('invalid_target')
    if recipient.lower() == profile['emailAddress'].lower():
        raise RefusalError('invalid_target')
    subject = _header(headers.get('subject'))
    reply = _header(headers.get('message-id'))
    if not re.fullmatch(r'<[^<>\s]+@[^<>\s]+>', reply):
        raise RefusalError('invalid_target')
    review = {'message_id': message_id, 'thread_id': message['threadId'], 'to': recipient,
              'subject': subject if subject.lower().startswith('re:') else 'Re: ' + subject,
              'body': body, 'reply_message_id': reply, 'source_revision': revision}
    rendered = f"To: {review['to']}\nSubject: {review['subject']}\n\n{review['body']}\n\nSave as a Gmail draft? Nothing will be sent."
    if len(rendered.encode('utf-16-le')) // 2 > review_capacity:
        raise RefusalError('review_unavailable')
    review['review_hash'] = _review_hash(review)
    _validate_review(review)
    return review


def _send_review(message, profile, message_id, body, revision):
    if (type(message) is not dict or message.get('id') != message_id
            or type(message.get('threadId')) is not str or not ID.fullmatch(message['threadId'])):
        raise RefusalError('invalid_target')
    headers = {}
    for header in (message.get('payload') or {}).get('headers', []):
        if type(header) is not dict:
            raise RefusalError('invalid_target')
        name = str(header.get('name', '')).lower()
        if name in ('reply-to', 'from', 'subject', 'message-id', 'references'):
            if name in headers:
                raise RefusalError('invalid_target')
            value = header.get('value')
            if type(value) is not str or value and not _header(value, 2048 if name == 'references' else 512):
                raise RefusalError('invalid_target')
            headers[name] = value
    if not headers.get('from'):
        raise RefusalError('invalid_target')
    addresses = getaddresses([headers.get('reply-to') or headers.get('from') or ''])
    if len(addresses) != 1:
        raise RefusalError('invalid_target')
    recipient = _recipient(addresses[0][1])
    if type(profile) is not dict:
        raise RefusalError('invalid_target')
    sender = _recipient(profile.get('emailAddress'))
    if recipient.lower() == sender.lower():
        raise RefusalError('invalid_target')
    reply = _header(headers.get('message-id'))
    if not re.fullmatch(r'<[^<>\s]+@[^<>\s]+>', reply):
        raise RefusalError('invalid_target')
    references = headers.get('references', '')
    if references and not re.fullmatch(r'<[^<>\s]+@[^<>\s]+>(?:[ \t]+<[^<>\s]+@[^<>\s]+>)*', references):
        raise RefusalError('invalid_target')
    subject = headers.get('subject', '')
    if subject and not subject.lower().startswith('re:'):
        subject = 'Re: ' + subject
    review = {'kind': 'reply_send', 'message_id': message_id, 'thread_id': message['threadId'],
        'sender_email': sender, 'to': recipient, 'subject': subject, 'body': body,
        'reply_message_id': reply, 'references': references, 'source_revision': revision}
    rendered = f"From: {sender}\nTo: {recipient}\nSubject: {subject}\n\n{body}\n\nSend this email reply now?"
    if len(rendered.encode('utf-16-le')) // 2 > 4000:
        raise RefusalError('review_unavailable')
    review['review_hash'] = _review_hash(review)
    _validate_review(review)
    return review



def _new_subject(value):
    if (type(value) is not str or len(value) > 512
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or ord(c) in (0x2028, 0x2029)
                   or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise RefusalError('invalid_target')
    return value


def _fresh_send_review(service, to, subject, body, revision):
    _recipient(to)
    _new_subject(to)
    _new_subject(subject)
    _body(body)
    profile = service.users().getProfile(userId='me').execute()
    if type(profile) is not dict:
        raise RefusalError('invalid_target')
    sender = _recipient(profile.get('emailAddress'))
    review = {'kind': 'new_send', 'sender_email': sender, 'to': to, 'subject': subject,
              'body': body, 'source_revision': revision}
    rendered = f"From: {sender}\nTo: {to}\nSubject: {subject}\n\n{body}\n\nSend this email now?"
    if len(rendered.encode('utf-16-le')) // 2 > 4000:
        raise RefusalError('review_unavailable')
    review['review_hash'] = _review_hash(review)
    _validate_review(review)
    return review

def _resolve_send(service, message_id, body, revision):
    if type(message_id) is not str or not ID.fullmatch(message_id):
        raise ValueError('send message identity invalid')
    message = service.users().messages().get(userId='me', id=message_id, format='full').execute()
    profile = service.users().getProfile(userId='me').execute()
    return _send_review(message, profile, message_id, body, revision)


def _send_accepted(result, review):
    """Gmail's own acceptance of the exact reviewed payload: a sent message id in the reviewed thread."""
    new_send = review.get('kind') == 'new_send'
    thread = result.get('threadId') if type(result) is dict and new_send else review.get('thread_id')
    return not (type(thread) is not str or not ID.fullmatch(thread)
                or type(result) is not dict or result.get('status') != 'sent'
                or type(result.get('id')) is not str or not ID.fullmatch(result['id'])
                or result.get('threadId') != thread)


def _sent_matches(service, result, review):
    import base64
    from email import policy as email_policy
    from email.parser import BytesParser
    new_send = review.get('kind') == 'new_send'
    if not _send_accepted(result, review):
        return False
    thread = result['threadId']
    sent = service.users().messages().get(userId='me', id=result['id'], format='raw').execute()
    if (type(sent) is not dict or sent.get('id') != result['id']
            or sent.get('threadId') != thread or 'SENT' not in sent.get('labelIds', [])
            or type(sent.get('raw')) is not str or len(sent['raw']) > 256 * 1024):
        return False
    raw = base64.urlsafe_b64decode(sent['raw'] + '=' * (-len(sent['raw']) % 4))
    mail = BytesParser(policy=email_policy.default).parsebytes(raw)
    expected = {'From': review['sender_email'], 'To': review['to'], 'Subject': review['subject'],
        **({} if new_send else {'In-Reply-To': review['reply_message_id'],
        'References': (review['references'] + ' ' + review['reply_message_id']).strip()})}
    if new_send and any(mail.get_all(name) for name in ('In-Reply-To', 'References')):
        return False
    for name, value in expected.items():
        values = mail.get_all(name, [])
        if not values and not value:
            continue
        if len(values) != 1:
            return False
        actual = str(values[0])
        if name in ('From', 'To'):
            addresses = getaddresses([actual])
            if len(addresses) != 1 or addresses[0][1].lower() != value.lower():
                return False
        elif actual != value:
            return False
    if any(mail.get_all(name) for name in ('Cc', 'Bcc', 'Resent-To', 'Resent-Cc', 'Resent-Bcc')):
        return False
    if mail.is_multipart() or mail.get_content_type() != 'text/plain':
        return False
    return mail.get_content().replace('\r\n', '\n') == review['body'].replace('\r\n', '\n')


def _path(root):
    parent = Path(root) / 'events'
    parent.mkdir(mode=0o700, exist_ok=True)
    meta = parent.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o022:
        raise ValueError('draft ledger directory invalid')
    path = parent / 'owner-gmail-drafts.json'
    if path.exists() or path.is_symlink():
        meta = path.lstat()
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid()
                or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_nlink != 1):
            raise ValueError('draft ledger invalid')
    return path


# Reviewed words are kept as long as the offered-draft ledger keeps them (retention.DRAFT_LEDGER_DAYS).
BODY_DAYS = 30
COMPACT_PROOF_KEYS = ('domain', 'event_id', 'action_id', 'payload_hash', 'text_hash', 'identity_digest',
                      'args_hash', 'admitted_at_ms', 'provider_thread_id')


def _same_proof(saved, proof):
    """A compacted receipt keeps every binding hash, so a replay still matches without the words."""
    if saved.get('compacted') is True:
        return {key: value for key, value in saved.items() if key != 'compacted'} == {
            key: proof.get(key) for key in COMPACT_PROOF_KEYS}
    return saved == proof


def _compacted(saved):
    """The content-free receipt an aged record becomes: binding hashes and provider ids, no words."""
    proof = {key: saved['proof'].get(key) for key in COMPACT_PROOF_KEYS} | {'compacted': True}
    if saved['status'] == 'committed':
        return {'proof': proof, 'status': 'committed', 'outcome': saved['outcome'],
                **({'learning': saved['learning']} if 'learning' in saved else {})}
    if saved['status'] == 'contacting':
        return {'proof': proof, 'status': 'contacting'}
    return {'proof': proof, 'status': 'rejected',
            'code': saved['code'] if saved['status'] == 'rejected' else 'review_unavailable'}


def compact(path, *, now=None, days=BODY_DAYS):
    """Retention's daily pass: records older than `days` keep only their content-free receipt."""
    import time
    path = Path(path)
    cutoff = ((time.time() if now is None else now) - days * 86400) * 1000
    # Unfinished learning may keep its words for one more retention window, never longer: if
    # consent was revoked, inspection can no longer repair it, and the words must still age out.
    learning_cutoff = ((time.time() if now is None else now) - 2 * days * 86400) * 1000
    writer = _preferences()
    with writer.jsonstore.lock(str(path)):
        path = _path(path.parent.parent)
        if not path.exists():
            return 0
        doc = _load(path)
        aged = [event for event, saved in doc['actions'].items()
                if saved['proof'].get('compacted') is not True
                # A committed typed result still needs its row and learning record
                # to finish local bookkeeping. Keep that bounded record until exact
                # inspection records the learning, then compact on a later sweep.
                and type(saved['proof'].get('admitted_at_ms')) is int
                and saved['proof']['admitted_at_ms'] < (
                    learning_cutoff if saved['status'] == 'committed' and saved.get('learning') == 'pending'
                    else cutoff)]
        for event in aged:
            doc['actions'][event] = _compacted(doc['actions'][event])
        if aged:
            writer._durable_preferences_replace(str(path), doc)
        return len(aged)


def _load(path):
    if not path.exists():
        return {'version': 1, 'actions': {}, 'consumed': {}}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding='utf-8') as stream:
        doc = json.load(stream)
    if type(doc) is not dict or set(doc) != {'version', 'actions', 'consumed'} or doc['version'] != 1:
        raise ValueError('draft ledger invalid')
    if type(doc['actions']) is not dict or type(doc['consumed']) is not dict:
        raise ValueError('draft ledger invalid')
    for event, saved in doc['actions'].items():
        if (type(saved) is not dict or saved.get('status') not in ('preview', 'contacting', 'committed', 'rejected')
                or type(saved.get('proof')) is not dict or saved['proof'].get('domain') != DOMAIN
                or saved['proof'].get('event_id') != event):
            raise ValueError('draft receipt invalid')
    return doc


def _response(proof, saved, review=None):
    if saved['status'] == 'preview':
        if saved['review'].get('kind') == 'reply_pair':
            _check_pair(saved)
        else:
            _check_preview(saved)
        return {'version': 1, 'status': 'preview', 'event_id': proof['event_id'],
                'payload_hash': proof['payload_hash'], 'review': saved['review'],
                **({'offer_receipt': saved['offer_receipt']} if 'offer_receipt' in saved else {})}
    if saved['status'] == 'rejected':
        return {'version': 1, 'status': 'rejected', 'code': saved['code']}
    if saved['status'] == 'contacting':
        return {'version': 1, 'status': 'unknown'}
    if 'learning' in saved and saved['learning'] not in ('pending', 'recorded'):
        raise ValueError('draft learning status invalid')
    if review is not None and review.get('kind') in ('reply_send', 'new_send'):
        outcome = saved.get('outcome')
        if (type(outcome) is not dict or set(outcome) != {'operation', 'message_id', 'thread_id', 'review_hash', 'status'}
                or outcome.get('operation') != 'send' or outcome.get('status') != 'sent'
                or type(outcome.get('message_id')) is not str or not ID.fullmatch(outcome['message_id'])
                or (type(outcome.get('thread_id')) is not str or not ID.fullmatch(outcome['thread_id']))
                or (review['kind'] == 'reply_send' and outcome['thread_id'] != review['thread_id'])
                or outcome.get('review_hash') != review['review_hash']):
            raise ValueError('sent receipt invalid')
    return {'version': 1, 'status': 'committed', **{key: proof[key] for key in (
        'action_id', 'event_id', 'payload_hash', 'args_hash')}, 'outcome': saved['outcome'],
        **({'learning': saved['learning']} if 'learning' in saved else {})}


def _validate_draft_origin(origin):
    if (type(origin) is not dict or set(origin) != {'action_type', 'baseline_action_id', 'baseline_key', 'baseline_body'}
            or origin['action_type'] not in ('reply', 'decline')
            or type(origin['baseline_action_id']) is not str or not HEX.fullmatch(origin['baseline_action_id'])
            or type(origin['baseline_key']) is not str or not re.fullmatch(r'[a-f0-9]{16}', origin['baseline_key'])):
        raise ValueError('typed draft origin invalid')
    _body(origin['baseline_body'])


def _typed_review(review, row, action_id):
    from keys import draft_key
    review = dict(review)
    review['draft_origin'] = {'action_type': row['action_type'], 'baseline_action_id': action_id,
                             'baseline_key': draft_key(row), 'baseline_body': row['text']}
    prefix = 'Draft type: ' + ('Decline' if row['action_type'] == 'decline' else 'Reply') + '\n'
    if review['body'] != row['text']:
        prefix += 'Edited draft\n'
    rendered = (f"From: {review['sender_email']}\n" if review.get('kind') == 'reply_send' else '')
    rendered += f"To: {review['to']}\nSubject: {review['subject']}\n\n{review['body']}\n\n"
    rendered += ('Send this email reply now?' if review.get('kind') == 'reply_send'
                 else 'Save as a Gmail draft? Nothing will be sent.')
    if len((prefix + rendered).encode('utf-16-le')) // 2 > 4000:
        raise RefusalError('review_unavailable')
    review['review_hash'] = _review_hash(review)
    _validate_review(review)
    return review



def _pair_text(review):
    first = review['first']
    order = (('Accept', 'accept_body'), ('Decline', 'decline_body'))
    if first == 'decline':
        order = order[::-1]
    return (f"To: {review['to']}\nSubject: {review['subject']}\n\n"
            + '\n\n'.join(label + ':\n' + review[key] for label, key in order)
            + '\n\nWhich would you like to use? Nothing has been saved or sent.')


def _pair_review(base, args, rows):
    from keys import draft_key
    review = {key: base[key] for key in ('message_id', 'thread_id', 'to', 'subject', 'reply_message_id', 'source_revision')}
    review.update(kind='reply_pair', accept_body=args['accept_body'], decline_body=args['decline_body'],
                  first=args.get('first', 'reply'), reply_key=draft_key(rows['reply']), decline_key=draft_key(rows['decline']))
    if len(_pair_text(review).encode('utf-16-le')) // 2 > 16000:
        raise RefusalError('review_unavailable')
    review['review_hash'] = _review_hash(review)
    return review


def _check_pair(saved):
    import action_links
    args, proof, review, rows = saved['args'], saved['proof'], saved['review'], saved['rows']
    if (type(args) is not dict or set(args) not in ({'operation','message_id','accept_body','decline_body'},
                                                  {'operation','message_id','accept_body','decline_body','first'})
            or args.get('operation') != 'propose_pair' or args.get('first','reply') not in ('reply','decline')
            or type(rows) is not dict or set(rows) != {'reply','decline'}
            or type(review) is not dict or set(review) != {'kind','message_id','thread_id','to','subject','reply_message_id',
                'source_revision','accept_body','decline_body','first','reply_key','decline_key','review_hash'}
            or review.get('kind') != 'reply_pair' or args['message_id'] != review['message_id']):
        raise ValueError('pair preview invalid')
    base = {key:review[key] for key in REVIEW_FIELDS - {'body','review_hash'}}
    base['body'] = args['accept_body']
    base['review_hash'] = _review_hash(base)
    _validate_review(base)
    for kind, key in (('reply','accept_body'),('decline','decline_body')):
        _body(args[key])
        row = rows[kind]
        if (type(row) is not dict or set(row) != {'ts','channel','identifier','text','action_type'}
                or row['channel'] != 'email' or row['identifier'] != action_links.normalized_identifier('email',review['to'])
                or row['text'] != args[key] or row['action_type'] != kind or type(row['ts']) is not str):
            raise ValueError('pair row invalid')
        datetime.fromisoformat(row['ts'].replace('Z','+00:00'))
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',',':'))
    action = hashlib.sha256(json.dumps([proof['event_id'],KIND,canonical],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    if proof['args_hash'] != hashlib.sha256(canonical.encode()).hexdigest() or proof['action_id'] != action or _pair_review(base,args,rows) != review:
        raise ValueError('pair proof invalid')
    flags = saved.get('recorded',{})
    if type(flags) is not dict or set(flags) - {'reply','decline'} or any(type(v) is not bool for v in flags.values()):
        raise ValueError('pair recording invalid')
    if 'accepted_delivery' in saved:
        _pair_acceptance(saved['accepted_delivery'],review)
    if 'offer_receipt' in saved:
        if flags != {'reply':True,'decline':True} or 'accepted_delivery' not in saved or saved['offer_receipt'] != _pair_receipt(saved):
            raise ValueError('pair receipt invalid')


def _pair_acceptance(delivery, review):
    if (type(delivery) is not dict or set(delivery) != {'accepted_dispatch_id','provider_message_id','source_revision','review_hash','payload_hash'}
            or any(type(delivery[k]) is not str or not HEX.fullmatch(delivery[k]) for k in ('source_revision','review_hash','payload_hash'))
            or delivery['source_revision'] != review['source_revision'] or delivery['review_hash'] != review['review_hash']
            or delivery['payload_hash'] != hashlib.sha256(_pair_text(review).encode()).hexdigest()):
        raise RefusalError('review_unavailable')
    for key, bound in (('accepted_dispatch_id',160),('provider_message_id',512)):
        value = delivery[key]
        if (type(value) is not str or not value or any(ord(c)<32 or 127<=ord(c)<=159 or c in '\u2028\u2029'
                or 0xD800<=ord(c)<=0xDFFF for c in value) or len(value.encode('utf-16-le'))//2 > bound):
            raise RefusalError('review_unavailable')


def _pair_receipt(saved):
    return {'version':1, **{k:saved['proof'][k] for k in ('action_id','event_id','payload_hash','args_hash')},
            'review_hash':saved['review']['review_hash'], 'accepted_dispatch_id':saved['accepted_delivery']['accepted_dispatch_id'],
            'reply_key':saved['review']['reply_key'], 'decline_key':saved['review']['decline_key']}


def _record_pair(saved, doc, *, writer, path, guard):
    if 'offer_receipt' in saved or 'accepted_delivery' not in saved:
        return
    import action_links
    from keys import draft_key
    for kind in ('reply','decline'):
        if saved['recorded'].get(kind) is True:
            continue
        guard()
        row = saved['rows'][kind]
        result = action_links.record_draft(row['channel'],row['identifier'],row['text'],row['action_type'],stable_row=row)
        if result != {'recorded':True,'key':draft_key(row)}:
            raise ValueError('pair append unconfirmed')
        writer._sync_existing_preferences(action_links.drafts_path())
        saved['recorded'][kind] = True
        writer._durable_preferences_replace(str(path),doc)
    guard()
    saved['offer_receipt'] = _pair_receipt(saved)
    writer._durable_preferences_replace(str(path),doc)


def _pair_selection(saved, action_type):
    _check_pair(saved)
    if action_type not in ('reply','decline') or 'offer_receipt' not in saved:
        raise RefusalError('review_unavailable')
    row = saved['rows'][action_type]
    from keys import draft_key
    review = {k:saved['review'][k] for k in ('message_id','thread_id','to','subject','reply_message_id','source_revision')}
    review.update(body=row['text'],draft_origin={'action_type':action_type,'baseline_action_id':saved['proof']['action_id'],
                  'baseline_key':draft_key(row),'baseline_body':row['text']})
    return {**saved,'row':row,'review':review}

def _check_preview(saved):
    import action_links
    from keys import draft_key
    review, args, proof, row = saved['review'], saved['args'], saved['proof'], saved['row']
    _validate_review(review)
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    action = hashlib.sha256(json.dumps([proof['event_id'], KIND, canonical], ensure_ascii=False,
                                      separators=(',', ':')).encode()).hexdigest()
    origin = review['draft_origin']
    if (proof['args_hash'] != hashlib.sha256(canonical.encode()).hexdigest() or proof['action_id'] != action
            or args.get('operation') not in ('propose_typed', 'propose_send_typed')
            or args.get('message_id') != review['message_id'] or args.get('body') != review['body']
            or args.get('action_type') != origin['action_type']
            or (args['operation'] == 'propose_send_typed') != (review.get('kind') == 'reply_send')
            or type(row) is not dict or set(row) != {'ts', 'channel', 'identifier', 'text', 'action_type'}
            or row['channel'] != 'email' or row['identifier'] != action_links.normalized_identifier('email', review['to'])
            or row['text'] != origin['baseline_body'] or row['action_type'] != origin['action_type']
            or draft_key(row) != origin['baseline_key']):
        raise ValueError('typed preview record invalid')
    datetime.fromisoformat(row['ts'].replace('Z', '+00:00'))


def _proposal_preview(doc, proposal, review=None, *, action_type=None):
    if (type(proposal) is not dict or set(proposal) != {'event_id', 'action_id', 'args_hash', 'accepted_dispatch_id', 'source_revision'}
            or any(type(proposal.get(k)) is not str or not HEX.fullmatch(proposal[k])
                   for k in ('event_id', 'action_id', 'args_hash', 'source_revision'))
            or type(proposal['accepted_dispatch_id']) is not str or not 0 < len(proposal['accepted_dispatch_id']) <= 160):
        raise RefusalError('review_unavailable')
    saved = doc['actions'].get(proposal['event_id'])
    if saved and saved.get('review',{}).get('kind') == 'reply_pair':
        selected = _pair_selection(saved,action_type)
        if (review is not None or any(saved['proof'].get(k) != proposal[k] for k in ('event_id','action_id','args_hash'))
                or saved['review']['source_revision'] != proposal['source_revision']
                or saved['offer_receipt']['accepted_dispatch_id'] != proposal['accepted_dispatch_id']):
            raise RefusalError('review_unavailable')
        return selected, saved
    if (not saved or saved.get('status') != 'preview' or 'row' not in saved
            or any(saved['proof'].get(k) != proposal[k] for k in ('event_id', 'action_id', 'args_hash'))
            or saved['review']['source_revision'] != proposal['source_revision']
            or review is not None and saved['review'] != review):
        raise RefusalError('review_unavailable')
    _check_preview(saved)
    from keys import draft_key
    row = saved['row']
    origin = saved['review']['draft_origin']
    if (row.get('text') != origin['baseline_body'] or row.get('action_type') != origin['action_type']
            or draft_key(row) != origin['baseline_key']):
        raise RefusalError('review_unavailable')
    root = next((record for record in doc['actions'].values()
                 if record['proof'].get('action_id') == origin['baseline_action_id']), None)
    if root and root.get('review',{}).get('kind') == 'reply_pair':
        selected = _pair_selection(root,origin['action_type'])
        if (selected['row'] != row or selected['review']['draft_origin'] != origin
                or root['proof']['identity_digest'] != saved['proof']['identity_digest']
                or any(root['review'][key] != saved['review'][key] for key in ('message_id','thread_id','to'))):
            raise RefusalError('review_unavailable')
        return saved, root
    if root and root.get('status') == 'preview':
        _check_preview(root)
    if (not root or root.get('status') != 'preview' or root.get('row') != row
            or root['proof']['action_id'] != origin['baseline_action_id'] or 'baseline' in root['args']
            or root['proof']['identity_digest'] != saved['proof']['identity_digest']
            or any(root['review'][key] != saved['review'][key] for key in ('message_id', 'thread_id', 'to'))
            or root['review']['draft_origin'] != origin or root['review']['body'] != row['text']):
        raise RefusalError('review_unavailable')
    return saved, root


def _offered(writer, root):
    if root.get('review',{}).get('kind') == 'reply_pair':
        _check_pair(root)
        if 'offer_receipt' not in root:
            raise RefusalError('review_unavailable')
        return
    if root.get('offered') is True:
        return
    import action_links
    from keys import draft_key
    row = root['row']
    result = action_links.record_draft(row['channel'], row['identifier'], row['text'], row['action_type'], stable_row=row)
    if result != {'recorded': True, 'key': draft_key(row)}:
        raise ValueError('offered draft record unavailable')
    writer._sync_existing_preferences(action_links.drafts_path())
    root['offered'] = True


def _record_learning(writer, rec):
    import log_outcome
    path = log_outcome._path()
    with writer.jsonstore.lock(path):
        rows = []
        if os.path.exists(path):
            with open(path, encoding='utf-8') as handle:
                rows = [json.loads(line) for line in handle if line.strip()]
        same_event = [row for row in rows if row.get('event_id') == rec['event_id']]
        if same_event and same_event != [rec]:
            raise ValueError('draft outcome event conflict')
        reuse = bool(same_event)
        if rec['outcome'] in ('executed', 'edited_and_sent'):
            claimed = [row for row in rows if row.get('source_event_id') == rec['source_event_id']
                       and row.get('outcome') in ('executed', 'edited_and_sent')]
            if claimed:
                fields = ('action_id', 'channel', 'contact', 'action_type', 'outcome')
                if any(any(row.get(key) != rec[key] for key in fields)
                       or rec['action_type'] == 'decline' and row.get('tier') != 'review' for row in claimed):
                    raise ValueError('sent event belongs to another draft')
                reuse = True
        else:
            prior = [row for row in rows if row.get('action_id') == rec['action_id']]
            if prior and prior[-1].get('outcome') in ('executed', 'edited_and_sent'):
                reuse = True  # delayed save bookkeeping must not regress confirmed send evidence
        if not reuse:
            result = log_outcome.log(rec)
            if result not in ({'logged': True, 'outcome': rec['outcome']},
                              {'logged': True, 'outcome': rec['outcome'], 'duplicate': True}):
                raise ValueError('draft outcome unavailable')
            with open(path, encoding='utf-8') as handle:
                matches = [json.loads(line) for line in handle if line.strip()]
            if not any(row == rec for row in matches):
                raise ValueError('draft outcome mismatch')
        writer._sync_existing_preferences(path)


def _learn(proof, review, *, writer, path, policy, google):
    """A committed provider receipt remains primary if local learning cannot finish."""
    try:
        _authority(policy, google, review['source_revision'], sending=review.get('kind') == 'reply_send')
        import draft_outcomes
        with writer.jsonstore.lock(str(path)):
            doc = _load(path)
            saved = doc['actions'][proof['event_id']]
            if saved.get('learning') != 'pending':
                return
            rec = saved['learning_record']
            from keys import draft_key
            import action_links
            origin = review['draft_origin']
            if (saved['row']['channel'] != 'email' or saved['row']['identifier'] != action_links.normalized_identifier('email', review['to'])
                    or saved['row']['action_type'] != origin['action_type']
                    or draft_key(saved['row']) != origin['baseline_key']):
                raise ValueError('draft learning lineage invalid')
            expected = draft_outcomes.bound_outcome(saved['row'], review['body'],
                sent_id=saved['outcome']['message_id'] if review.get('kind') == 'reply_send' else None)
            expected.update(event_id='attended-draft:' + proof['action_id'], ts=rec.get('ts'))
            datetime.fromisoformat(str(rec.get('ts')).replace('Z', '+00:00'))
            if rec != expected or saved['row']['text'] != review['draft_origin']['baseline_body']:
                raise ValueError('draft learning record invalid')
            _record_learning(writer, rec)
            _authority(policy, google, review['source_revision'], sending=review.get('kind') == 'reply_send')
            if rec['outcome'] == 'executed':
                def permitted(source):
                    _authority(policy, google, review['source_revision'], sending=True)
                    return source == 'gmail'
                draft_outcomes._confirm_style_sample(review['body'], rec['action_id'], permitted=permitted)
            saved['learning'] = 'recorded'
            writer._durable_preferences_replace(str(path), doc)
    except Exception:  # local bookkeeping failure never changes the successful send/save
        pass


def _offers():
    from runtime_owner_loop import _load_writer
    _load_writer()  # existing fixed-root loader establishes shared script/library imports
    import pending_offer
    return pending_offer


def _offer(selection, *, policy, data_root, activation, expected=None):
    message_id = selected_message_id(selection)
    route = activation.verified_current_photon_route(data_root, policy)
    if message_id is None or route is None:
        return None
    offers = _offers()
    offer = offers.get_offer(message_id=message_id, latest=False, target='photon', chat_id=route['chat_id'])
    if (not offer or offer.get('ambiguous') or offer.get('kind') not in ('commitment', 'chase')
            or not offers._accepted_at(offer)):
        return None
    if expected is not None and offer != expected:
        raise RefusalError('target_changed')
    from runtime_owner_loop import _load_writer
    knowledge_edit = _load_writer()
    from ledger_provenance import source_loop_sources
    try:
        row = knowledge_edit.source_offer_row(offer.get('loop_snapshot'))
    except knowledge_edit.OwnerLoopToolRefusalError as error:
        raise RefusalError('authority_changed' if error.code == 'source_unavailable' else 'target_changed') from error
    # A source-message id is only Gmail authority on an unambiguous Gmail-origin row.
    if source_loop_sources(row) != {'gmail'} and row.get('source') != 'gmail':
        raise RefusalError('invalid_target')
    message = row.get('source_message_id')
    if type(message) is not str or not ID.fullmatch(message):
        raise RefusalError('invalid_target')
    try:
        detail = json.loads(offer['detail'])
    except (TypeError, ValueError, KeyError):
        raise RefusalError('invalid_target') from None
    if (type(detail) is not dict or set(detail) != {'to', 'body', 'thread_id'}
            or type(detail['thread_id']) is not str or not ID.fullmatch(detail['thread_id'])
            or detail['thread_id'] != row.get('source_thread_id')):
        raise RefusalError('invalid_target')
    _body(detail['body'])
    _header(detail['to'], 254)
    return offer, message, detail


def _reconcile_offer(saved):
    if saved.get('status') == 'committed' and saved.get('offer') is not None:
        # The immutable provider receipt is primary. Cleanup can be retried by exact inspection.
        try:
            _offers().clear_offer(expected=saved['offer'])
        except Exception:
            pass


def _followup_detail(offer, policy, revision):
    raw = offer.get('detail')
    if (type(raw) is not str or offer.get('payload_sha256') != hashlib.sha256(raw.encode()).hexdigest()):
        raise RefusalError('invalid_target')
    try:
        detail = json.loads(raw)
    except ValueError:
        raise RefusalError('invalid_target') from None
    if (type(detail) is not dict or set(detail) != {'version', 'to_name', 'to_email', 'channel',
            'subject', 'body', 'sources', 'source_revision'} or type(detail['version']) is not int
            or detail['version'] != 1 or detail['source_revision'] != revision
            or type(detail['sources']) is not list or not detail['sources']
            or any(type(source) is not str for source in detail['sources'])
            or len(set(detail['sources'])) != len(detail['sources']) or 'granola' not in detail['sources']):
        raise RefusalError('authority_changed')
    current = policy['_source_authority']()
    allowed = set(current.get('bridge_sources', []) + current.get('google_sources', [])
                  + current.get('connector_sources', []))
    if (current.get('complete') is not True or current.get('revision') != revision
            or not set(detail['sources']) <= allowed):
        raise RefusalError('authority_changed')
    if type(detail['to_name']) is not str or (detail['to_name'] and not _header(detail['to_name'])):
        raise RefusalError('invalid_target')
    if detail['to_email'] is not None:
        _recipient(detail['to_email'])
    if detail['channel'] not in ('email', 'imessage', 'whatsapp', None):
        raise RefusalError('invalid_target')
    if detail['subject'] is not None and (type(detail['subject']) is not str
            or (detail['subject'] and not _header(detail['subject']))):
        raise RefusalError('invalid_target')
    _body(detail['body'])
    return detail


def _followup_offers(*, policy, data_root, activation, revision, offer_id=None, expected=None):
    route = activation.verified_current_photon_route(data_root, policy)
    if route is None:
        raise RefusalError('authority_changed')
    found = _offers().get_offer(offer_id=offer_id or '', latest=False, target='photon', chat_id=route['chat_id'])
    candidates = found.get('offers', []) if found.get('ambiguous') else [found] if found else []
    matches = []
    for offer in candidates:
        if offer.get('kind') != 'followup_draft' or not _offers()._accepted_at(offer):
            continue
        if type(offer.get('offer_id')) is not str or not OFFER_ID.fullmatch(offer['offer_id']):
            raise RefusalError('invalid_target')
        if expected is not None and offer != expected:
            raise RefusalError('target_changed')
        try:
            detail = _followup_detail(offer, policy, revision)
        except RefusalError:
            if offer_id is not None:
                raise
            continue  # Withheld offers never contribute titles, recipients or counts.
        matches.append((offer, detail))
    if offer_id is not None and len(matches) > 1:
        raise RefusalError('invalid_target')
    return matches


def _owner_recipient(value, text=None):
    _recipient(value)
    # Match one complete owner-supplied address, never a substring of another address.
    if text is not None:
        tokens = {token.strip('()[]{}\"').rstrip('.,!?') for token in re.findall(r'[^\s<>@,;]+@[^\s<>@,;]+', text)}
        if value not in tokens:
            raise RefusalError('invalid_target')
    return value


def _new_review(detail, revision, recipient=None):
    if recipient is not None and (detail['to_email'] is not None or detail['channel'] not in ('email', None)):
        raise RefusalError('invalid_target')
    if (recipient is None and (detail['channel'] != 'email' or detail['to_email'] is None)):
        raise RefusalError('invalid_target')
    review = {'kind': 'new_email', 'to': recipient or detail['to_email'], 'subject': detail['subject'] or '',
              'body': detail['body'], 'source_revision': revision}
    rendered = f"To: {review['to']}\nSubject: {review['subject']}\n\n{review['body']}\n\nSave as a Gmail draft? Nothing will be sent."
    if len(rendered.encode('utf-16-le')) // 2 > 4000:
        raise RefusalError('review_unavailable')
    review['review_hash'] = _review_hash(review)
    _validate_review(review)
    return review


def perform(body, *, bearer, root, policy, data_root, capability, activation,
            telegram_verifier=None, chat_client=None):
    admission_body = body
    if type(body) is dict and body.get('operation') == 'record_offer':
        if 'accepted_delivery' not in body or type(body.get('args')) is not dict or body['args'].get('operation') != 'propose_pair':
            raise ValueError('pair acceptance invalid')
        admission_body = {k:v for k,v in body.items() if k != 'accepted_delivery'}
    proof = validate_tool_admission(admission_body, operations=('preview', 'apply', 'inspect', 'record_offer'), kind=KIND,
        domain=DOMAIN, bearer=bearer, root=root, capability=capability, policy=policy,
        data_root=data_root, activation=activation, telegram_verifier=telegram_verifier, chat_client=chat_client)
    args = body['args']
    google = _google()
    writer = path = None
    send_service = None
    try:
        if type(args) is dict and args.get('operation') == 'propose_pair':
            if (set(args) not in ({'operation','message_id','accept_body','decline_body'},
                                 {'operation','message_id','accept_body','decline_body','first'})
                    or args.get('first','reply') not in ('reply','decline')):
                raise ValueError('pair proposal invalid')
            _body(args['accept_body'])
            _body(args['decline_body'])
            writer,path = _preferences(),_path(data_root)
            with writer.jsonstore.lock(str(path)):
                doc = _load(path)
                saved = doc['actions'].get(proof['event_id'])
                if saved is not None and not _same_proof(saved['proof'], proof):
                    raise RefusalError('event_conflict')
                if saved is None and body['operation'] == 'inspect':
                    return {'version':1,'status':'not_found'}
                if saved is None and body['operation'] != 'preview':
                    raise RefusalError('review_unavailable')
                revision = _authority(policy,google,read_only=True)
                if saved is None:
                    base = _resolve(google._gmail_service(),args['message_id'],args['accept_body'],revision,review_capacity=16000)
                    import action_links
                    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                    rows = {kind:{'ts':ts,'channel':'email','identifier':action_links.normalized_identifier('email',base['to']),
                            'text':args[key],'action_type':kind} for kind,key in (('reply','accept_body'),('decline','decline_body'))}
                    review = _pair_review(base,args,rows)
                    _authority(policy,google,revision,read_only=True)
                    saved = {'proof':proof,'status':'preview','args':args,'review':review,'rows':rows,'recorded':{}}
                    doc['actions'][proof['event_id']] = saved
                    writer._durable_preferences_replace(str(path),doc)
                _check_pair(saved)
                _authority(policy,google,saved['review']['source_revision'],read_only=True)
                if body['operation'] == 'record_offer':
                    delivery = body['accepted_delivery']
                    _pair_acceptance(delivery,saved['review'])
                    if 'accepted_delivery' in saved and saved['accepted_delivery'] != delivery:
                        raise RefusalError('event_conflict')
                    if 'accepted_delivery' not in saved:
                        saved['accepted_delivery'] = delivery
                        writer._durable_preferences_replace(str(path),doc)
                elif body['operation'] not in ('preview','inspect'):
                    raise ValueError('pair operation invalid')
                if body['operation'] in ('record_offer','inspect'):
                    try:
                        _record_pair(saved,doc,writer=writer,path=path,
                            guard=lambda:_authority(policy,google,saved['review']['source_revision'],read_only=True))
                    except RefusalError:
                        raise
                    except Exception:
                        return {'version':1,'status':'unknown'}
                writer._sync_existing_preferences(str(path))
                _authority(policy,google,saved['review']['source_revision'],read_only=True)
                return _response(proof,saved)
        typed_preview = type(args) is dict and args.get('operation') in ('propose_typed', 'propose_send_typed')
        if typed_preview:
            if (set(args) not in ({'operation', 'message_id', 'body', 'action_type'},
                                 {'operation', 'message_id', 'body', 'action_type', 'baseline'})
                    or args['action_type'] not in ('reply', 'decline')):
                raise ValueError('typed draft proposal invalid')
            _body(args['body'])
            writer, path = _preferences(), _path(data_root)
            with writer.jsonstore.lock(str(path)):
                doc = _load(path)
                saved = doc['actions'].get(proof['event_id'])
                if saved is not None:
                    if not _same_proof(saved['proof'], proof):
                        raise RefusalError('event_conflict')
                    writer._sync_existing_preferences(str(path))
                    if saved.get('status') == 'preview':
                        _authority(policy, google, saved['review']['source_revision'],
                                   sending=saved['review'].get('kind') == 'reply_send')
                    return _response(proof, saved)
                if body['operation'] == 'inspect':
                    return {'version': 1, 'status': 'not_found'}
            if body['operation'] != 'preview':
                raise ValueError('typed proposal operation invalid')
            sending = args['operation'] == 'propose_send_typed'
            revision = _authority(policy, google, sending=sending)
            service = google._gmail_send_service() if sending else google._gmail_service()
            if sending:
                send_service = service
            review = (_resolve_send if sending else _resolve)(service, args['message_id'], args['body'], revision)
            with writer.jsonstore.lock(str(path)):
                doc = _load(path)
                saved = doc['actions'].get(proof['event_id'])
                if saved is not None:
                    if not _same_proof(saved['proof'], proof):
                        raise RefusalError('event_conflict')
                    return _response(proof, saved)
                _authority(policy, google, revision, sending=sending)
                if 'baseline' in args:
                    prior, baseline_root = _proposal_preview(doc, args['baseline'], action_type=args['action_type'])
                    if (args['baseline']['event_id'] == proof['event_id']
                            or prior['proof']['identity_digest'] != proof['identity_digest']
                            or prior['review']['message_id'] != args['message_id']
                            or args['baseline']['source_revision'] != revision
                            or prior['review']['to'] != review['to']
                            or prior['review']['thread_id'] != review['thread_id']
                            or prior['row']['action_type'] != args['action_type']):
                        raise RefusalError('review_unavailable')
                    _offered(writer, baseline_root)
                    row = prior['row']
                    baseline_action = prior['review']['draft_origin']['baseline_action_id']
                else:
                    import action_links
                    row = {'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                           'channel': 'email', 'identifier': action_links.normalized_identifier('email', review['to']),
                           'text': args['body'], 'action_type': args['action_type']}
                    if not row['identifier']:
                        raise RefusalError('invalid_target')
                    baseline_action = proof['action_id']
                review = _typed_review(review, row, baseline_action)
                _authority(policy, google, revision, sending=sending)
                doc['actions'][proof['event_id']] = {'proof': proof, 'status': 'preview',
                                                    'args': args, 'row': row, 'review': review}
                writer._durable_preferences_replace(str(path), doc)
            return _response(proof, doc['actions'][proof['event_id']])
        if body['operation'] == 'preview':
            if type(args) is not dict:
                raise ValueError('draft proposal invalid')
            if args.get('operation') in ('list_followup', 'propose_followup'):
                listing = args['operation'] == 'list_followup'
                if ((set(args) != {'operation'} if listing else
                     set(args) not in ({'operation', 'offer_id'}, {'operation', 'offer_id', 'to'}))
                        or body['identity']['channel'] != 'photon'
                        or not listing and (type(args['offer_id']) is not str or not OFFER_ID.fullmatch(args['offer_id']))):
                    raise ValueError('followup draft proposal invalid')
                revision = _authority(policy, google)
                recipient = _owner_recipient(args['to'], body['text']) if 'to' in args else None
                matches = _followup_offers(policy=policy, data_root=data_root, activation=activation,
                    revision=revision, offer_id=None if listing else args['offer_id'])
                _authority(policy, google, revision)
                if not matches:
                    return {'version': 1, 'status': 'no_match'}
                if listing:
                    summaries = [{'offer_id': offer['offer_id'], **{key: detail[key]
                        for key in ('to_name', 'to_email', 'channel')}, 'subject': detail['subject'] or ''}
                        for offer, detail in matches]
                    if len(json.dumps(summaries, ensure_ascii=False).encode()) > 48 * 1024:
                        raise RefusalError('review_unavailable')
                    return {'version': 1, 'status': 'offers', 'offers': summaries}
                offer, detail = matches[0]
                return {'version': 1, 'status': 'preview', 'event_id': proof['event_id'],
                        'payload_hash': proof['payload_hash'], 'review': _new_review(detail, revision, recipient),
                        'offer_id': offer['offer_id']}
            if args.get('operation') == 'propose_new_send':
                if set(args) != {'operation', 'to', 'subject', 'body'}:
                    raise ValueError('fresh send proposal invalid')
                revision = _authority(policy, google, sending=True)
                send_service = google._gmail_send_service()
                review = _fresh_send_review(send_service, args['to'], args['subject'], args['body'], revision)
                _authority(policy, google, revision, sending=True)
                return {'version': 1, 'status': 'preview', 'event_id': proof['event_id'],
                        'payload_hash': proof['payload_hash'], 'review': review}
            origin = args.get('operation') == 'propose_offer'
            if origin:
                if set(args) != {'operation', 'selection'} or body['identity']['channel'] != 'photon':
                    raise ValueError('draft offer proposal invalid')
                selected_message_id(args['selection'])
                if not OFFER_YES.fullmatch(body['text'].strip()):
                    raise ValueError('draft offer affirmation invalid')
                resolved = _offer(args['selection'], policy=policy, data_root=data_root, activation=activation)
                if resolved is None:
                    return {'version': 1, 'status': 'no_match'}
                offer, message_id, detail = resolved
                draft_body = detail['body']
            else:
                if set(args) != {'operation', 'message_id', 'body'} or args['operation'] not in ('propose', 'propose_send'):
                    raise ValueError('draft proposal invalid')
                message_id, draft_body = args['message_id'], args['body']
            _body(draft_body)
            sending = args['operation'] == 'propose_send'
            revision = _authority(policy, google, sending=sending)
            if sending:
                send_service = google._gmail_send_service()
            review = (_resolve_send if sending else _resolve)(send_service if sending else google._gmail_service(), message_id, draft_body, revision)
            _authority(policy, google, revision, sending=sending)
            if origin:
                if _offer(args['selection'], policy=policy, data_root=data_root, activation=activation, expected=offer) is None:
                    raise RefusalError('target_changed')
                if review['to'].lower() != detail['to'].lower() or review['thread_id'] != detail['thread_id']:
                    raise RefusalError('target_changed')
            return {'version': 1, 'status': 'preview', 'event_id': proof['event_id'],
                    'payload_hash': proof['payload_hash'], 'review': review,
                    **({'offer_id': offer['offer_id']} if origin else {})}
        if type(args) is not dict or set(args) != {'operation', 'review', 'proposal'} or args['operation'] not in ('save', 'send'):
            raise ValueError('draft save invalid')
        review, proposal = args['review'], args['proposal']
        _validate_review(review)
        sending = args['operation'] == 'send'
        if sending != (review.get('kind') in ('reply_send', 'new_send')) or sending and type(proposal) is dict and 'origin' in proposal:
            raise ValueError('send review binding invalid')
        if (type(proposal) is not dict or set(proposal) not in ({'event_id', 'action_id', 'args_hash', 'accepted_dispatch_id', 'source_revision'},
                                     {'event_id', 'action_id', 'args_hash', 'accepted_dispatch_id', 'source_revision', 'origin'})
                or any(type(proposal[k]) is not str or not HEX.fullmatch(proposal[k])
                       for k in ('event_id', 'action_id', 'args_hash', 'source_revision'))
                or type(proposal['accepted_dispatch_id']) is not str or not 0 < len(proposal['accepted_dispatch_id']) <= 160):
            raise ValueError('draft approval binding invalid')
        new_send = review.get('kind') == 'new_send'
        origin = proposal.get('origin')
        followup = type(origin) is dict and origin.get('kind') == 'followup'
        if 'origin' in proposal:
            if (type(origin) is not dict or (set(origin) not in ({'kind', 'offer_id'}, {'kind', 'offer_id', 'to'})
                                           if followup else set(origin) != {'selection', 'offer_id'})
                    or type(origin['offer_id']) is not str or not OFFER_ID.fullmatch(origin['offer_id'])):
                raise ValueError('draft offer origin invalid')
            if not followup:
                selected_message_id(origin['selection'])
            elif 'to' in origin:
                _owner_recipient(origin['to'])
        if (review.get('kind') == 'new_email') != followup:
            raise ValueError('draft review origin invalid')
        writer, path = _preferences(), _path(data_root)
        with writer.jsonstore.lock(str(path)):
            doc = _load(path)
            saved = doc['actions'].get(proof['event_id'])
            if saved is not None:
                if not _same_proof(saved['proof'], proof):
                    raise RefusalError('event_conflict')
                writer._sync_existing_preferences(str(path))
                original_response = _response(proof, saved, review)
                # Do not nest offer locking inside the draft ledger lock.
                original_saved = saved
            else:
                original_saved = None
            if original_saved is not None:
                pass
            elif body['operation'] == 'inspect':
                return {'version': 1, 'status': 'not_found'}
        if original_saved is not None:
            _reconcile_offer(original_saved)
            if original_saved.get('learning') == 'pending':
                _learn(proof, review, writer=writer, path=path, policy=policy, google=google)
                try:
                    with writer.jsonstore.lock(str(path)):
                        return _response(proof, _load(path)['actions'][proof['event_id']], review)
                except Exception:
                    return original_response
            return original_response
        with writer.jsonstore.lock(str(path)):
            doc = _load(path)
            if body['operation'] == 'inspect':
                return {'version': 1, 'status': 'not_found'}
            if (not (YES.fullmatch(body['text'].strip()) or sending and SEND_YES.fullmatch(body['text'].strip()))
                    or proposal['event_id'] == proof['event_id']
                    or proposal['source_revision'] != review['source_revision']):
                raise RefusalError('review_unavailable')
            original = ({'operation': 'propose_followup', 'offer_id': origin['offer_id'],
                         **({'to': origin['to']} if 'to' in origin else {})} if followup else
                        {'operation': 'propose_offer', 'selection': origin['selection']} if origin is not None
                        else {'operation': 'propose_new_send', 'to': review['to'], 'subject': review['subject'], 'body': review['body']} if new_send
                        else {'operation': 'propose_send' if sending else 'propose', 'message_id': review['message_id'], 'body': review['body']})
            typed = 'draft_origin' in review
            if typed:
                typed_saved, typed_root = _proposal_preview(doc, proposal, review)
                if typed_saved['proof']['identity_digest'] != proof['identity_digest']:
                    raise RefusalError('review_unavailable')
                original = typed_saved['args']
            canonical = json.dumps(original, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
            expected_action = hashlib.sha256(json.dumps([proposal['event_id'], KIND, canonical], ensure_ascii=False,
                                                       separators=(',', ':')).encode()).hexdigest()
            if proposal['args_hash'] != hashlib.sha256(canonical.encode()).hexdigest() or proposal['action_id'] != expected_action:
                raise RefusalError('review_unavailable')
            if proposal['action_id'] in doc['consumed']:
                raise RefusalError('review_unavailable')
        offer = None
        if followup:
            revision = _authority(policy, google, review['source_revision'])
            resolved = _followup_offers(policy=policy, data_root=data_root, activation=activation,
                revision=revision, offer_id=origin['offer_id'])
            if not resolved:
                raise RefusalError('review_unavailable')
            offer, detail = resolved[0]
            if _new_review(detail, revision, origin.get('to')) != review:
                raise RefusalError('target_changed')
        elif origin is not None:
            resolved = _offer(origin['selection'], policy=policy, data_root=data_root, activation=activation)
            if resolved is None or resolved[0]['offer_id'] != origin['offer_id']:
                raise RefusalError('review_unavailable')
            offer, message_id, detail = resolved
            if (review['message_id'] != message_id or review['body'] != detail['body']
                    or review['to'].lower() != detail['to'].lower() or review['thread_id'] != detail['thread_id']):
                raise RefusalError('target_changed')
        revision = _authority(policy, google, review['source_revision'], sending=sending)
        service = google._gmail_send_service() if sending else google._gmail_service()
        if sending:
            send_service = service
        if not followup:
            current_review = (_fresh_send_review(service, review['to'], review['subject'], review['body'], revision) if new_send else
                              (_resolve_send if sending else _resolve)(service, review['message_id'], review['body'], revision))
            if typed:
                current_review = _typed_review(current_review, typed_saved['row'], review['draft_origin']['baseline_action_id'])
            if current_review != review:
                raise RefusalError('target_changed')
        _authority(policy, google, revision, sending=sending)
        # Recheck the local fence and durably consume it before provider contact.
        offers = _offers() if offer is not None else None
        with (writer.jsonstore.lock(offers._path()) if offers else nullcontext()):
            if offer is not None:
                resolved = (_followup_offers(policy=policy, data_root=data_root, activation=activation,
                    revision=revision, offer_id=origin['offer_id'], expected=offer) if followup else
                    _offer(origin['selection'], policy=policy, data_root=data_root, activation=activation, expected=offer))
                if not resolved:
                    raise RefusalError('review_unavailable')
            with writer.jsonstore.lock(str(path)):
                doc = _load(path)
                if proof['event_id'] in doc['actions']:
                    saved = doc['actions'][proof['event_id']]
                    if not _same_proof(saved['proof'], proof):
                        raise RefusalError('event_conflict')
                    return _response(proof, saved, review)
                if proposal['action_id'] in doc['consumed']:
                    raise RefusalError('review_unavailable')
                if offer is not None and 'offer:' + offer['offer_id'] in doc['consumed']:
                    raise RefusalError('review_unavailable')
                if typed:
                    _, typed_root = _proposal_preview(doc, proposal, review)
                    _offered(writer, typed_root)
                doc['actions'][proof['event_id']] = {'proof': proof, 'status': 'contacting',
                    **({'offer': offer} if offer is not None else {})}
                if offer is not None:
                    doc['consumed']['offer:' + offer['offer_id']] = proof['event_id']
                doc['consumed'][proposal['action_id']] = proof['event_id']
                writer._durable_preferences_replace(str(path), doc)
        def save_reviewed():
            try:
                if new_send:
                    def validate_profile(profile):
                        if type(profile) is not dict or profile.get('emailAddress') != review['sender_email']:
                            raise RefusalError('target_changed')
                    return google._gmail_reviewed_new_send(service, review, validate_profile,
                        lambda: _authority(policy, google, revision, sending=True))
                if sending:
                    def validate(message, profile):
                        current = _send_review(message, profile, review['message_id'], review['body'], revision)
                        if typed:
                            current = _typed_review(current, typed_saved['row'], review['draft_origin']['baseline_action_id'])
                        if current != review:
                            raise RefusalError('target_changed')
                    return google._gmail_reviewed_reply(service, review, validate,
                        lambda: _authority(policy, google, revision, sending=True))
                def before_create():
                    try:
                        _authority(policy, google, revision)
                    except RefusalError as error:
                        raise _PrecontactRefusalError(error.code) from error
                    except Exception as error:
                        raise _PrecontactRefusalError('authority_changed') from error
                return (google._gmail_reviewed_new_draft(service, review, before_create) if followup else
                        google._gmail_reviewed_draft(service, review, before_create))
            except _PrecontactRefusalError as error:
                return {'status': 'error', 'precontact_refusal': error.code}
            except Exception:  # noqa: BLE001 -- contact remains uncertain; audit only sanitized metadata
                return {'status': 'error'}

        try:
            result, refused = google._gated('gmail-send' if new_send else 'gmail-reply' if sending else 'gmail-draft',
                {'to': review['to']} if new_send else {'message_id': review['message_id']} if sending else {'to': review['to']},
                google._canonical({key: review[key] for key in ('to', 'subject', 'body')}) if new_send else
                google._canonical({'message_id': review['message_id'], 'body': review['body']}) if sending else review['body'],
                save_reviewed, False)
            if not refused and isinstance(result, dict) and result.get('precontact_refusal'):
                with writer.jsonstore.lock(str(path)):
                    doc = _load(path)
                    saved = doc['actions'].get(proof['event_id'])
                    if (not saved or not _same_proof(saved['proof'], proof)
                            or saved['status'] != 'contacting'):
                        return {'version': 1, 'status': 'unknown'}
                    doc['actions'][proof['event_id']] = {
                        'proof': proof, 'status': 'rejected', 'code': result['precontact_refusal']}
                    writer._durable_preferences_replace(str(path), doc)
                    return _response(proof, doc['actions'][proof['event_id']], review)
            if sending:
                if refused or not _send_accepted(result, review):
                    return {'version': 1, 'status': 'unknown'}
                # Gmail accepted the send. A later authority change never makes it unknown: it only
                # skips the private read-back that otherwise confirms the exact reviewed message.
                if (_readback_allowed(lambda: _authority(policy, google, revision, sending=True))
                        and not _sent_matches(service, result, review)):
                    return {'version': 1, 'status': 'unknown'}
                outcome = {'operation': 'send', 'message_id': result['id'], 'thread_id': result['threadId'] if new_send else review['thread_id'],
                    'review_hash': review['review_hash'], 'status': 'sent'}
            elif (refused or result.get('status') != 'drafted' or not ID.fullmatch(result['draft_id'])
                    or not ID.fullmatch(result.get('thread_id', ''))):
                return {'version': 1, 'status': 'unknown'}
            if not sending:
                outcome = {'operation': 'save', 'draft_id': result['draft_id'], 'thread_id': result['thread_id'],
                       'review_hash': review['review_hash'], 'status': 'draft_saved'}
            learning = {}
            if typed:
                import draft_outcomes
                rec = draft_outcomes.bound_outcome(typed_saved['row'], review['body'],
                    sent_id=result['id'] if sending else None)
                rec.update(event_id='attended-draft:' + proof['action_id'],
                           ts=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
                learning = {'learning': 'pending', 'learning_record': rec, 'row': typed_saved['row']}
            with writer.jsonstore.lock(str(path)):
                doc = _load(path)
                doc['actions'][proof['event_id']] = {'proof': proof, 'status': 'committed', 'outcome': outcome, **learning,
                    **({'offer': offer} if offer is not None else {})}
                writer._durable_preferences_replace(str(path), doc)
            _reconcile_offer(doc['actions'][proof['event_id']])
            known_response = _response(proof, doc['actions'][proof['event_id']], review)
            if typed:
                _learn(proof, review, writer=writer, path=path, policy=policy, google=google)
                try:
                    with writer.jsonstore.lock(str(path)):
                        return _response(proof, _load(path)['actions'][proof['event_id']], review)
                except Exception:
                    return known_response
            return known_response
        except Exception:  # provider contact or receipt persistence may have succeeded
            return {'version': 1, 'status': 'unknown'}
    except RefusalError as error:
        if body['operation'] == 'apply' and writer is not None and path is not None:
            with writer.jsonstore.lock(str(path)):
                doc = _load(path)
                saved = doc['actions'].get(proof['event_id'])
                if saved is not None:
                    if _same_proof(saved['proof'], proof):
                        return _response(proof, saved, review)
                    return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
                if saved is None:
                    doc['actions'][proof['event_id']] = {'proof': proof, 'status': 'rejected', 'code': error.code}
                    writer._durable_preferences_replace(str(path), doc)
        return {'version': 1, 'status': 'rejected', 'code': error.code}

    finally:
        if send_service is not None:
            send_service.close()
