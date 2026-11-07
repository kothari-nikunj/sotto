"""Real reviewed MIME helper and durable receiver receipts against a fake Gmail provider."""
import base64
from email import message_from_bytes
import hashlib
import json
from types import SimpleNamespace

import pytest

import runtime_owner_gmail_draft_tool as tool
from test_runtime_owner_loop_close import context, IDENTITY, ROOT, WHEN, THREAD, capability

__all__ = ['context']


class Gmail:
    def __init__(self):
        self.created = []
        self.message_id = 'message-123'
        self.after_accept = False
        self.thread = 'thread-123'
        self.recipient = 'Alice <alice@example.com>'
        self.bad_receipt = False

    def users(self):
        return self

    def messages(self):
        return self

    def drafts(self):
        return self

    def get(self, **kwargs):
        assert kwargs == {'userId': 'me', 'id': self.message_id, 'format': 'full'}
        return SimpleNamespace(execute=lambda: {'id': self.message_id, 'threadId': self.thread,
            'payload': {'headers': [{'name': 'From', 'value': self.recipient},
                                   {'name': 'Subject', 'value': 'Cedar review'},
                                   {'name': 'Message-ID', 'value': '<cedar@example.com>'}]}})

    def getProfile(self, **kwargs):
        assert kwargs == {'userId': 'me'}
        return SimpleNamespace(execute=lambda: {'emailAddress': 'owner@example.com'})

    def create(self, **kwargs):
        def execute():
            self.created.append(kwargs)
            if self.after_accept:
                raise TimeoutError('provider accepted before response was lost')
            return {'id': 'draft-123', 'message': {'threadId': 'other' if self.bad_receipt else self.thread}}
        return SimpleNamespace(execute=execute)

    def send(self, **kwargs):
        raise AssertionError('no Gmail send API may be called')


def request(args, operation='apply', event='a' * 64, text='yes'):
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    action = hashlib.sha256(json.dumps([event, tool.KIND, canonical], ensure_ascii=False,
                                      separators=(',', ':')).encode()).hexdigest()
    return {'version': 1, 'operation': operation, 'identity': IDENTITY, 'action_id': action,
            'event_id': event, 'payload_hash': 'b' * 64, 'text_hash': hashlib.sha256(text.encode()).hexdigest(),
            'text': text, 'admitted_at_ms': int(WHEN.timestamp() * 1000), 'provider_thread_id': THREAD, 'args': args}


@pytest.fixture
def setup(context, monkeypatch):
    data, policy, activation = context
    policy.update(SOTTO_DATA=str(data), SOTTO_HOME=str(data / 'sotto-home'),
                  SOTTO_RUNTIME_PROFILE='managed-photon-fixed-five-v1',
                  _source_authority=lambda: {'complete': True, 'revision': 'c' * 64, 'google_sources': ['gmail']})
    monkeypatch.delenv('SOTTO_UNATTENDED', raising=False)
    google = tool._google()
    gmail = Gmail()
    grants = {'gmail_read': True, 'gmail_draft': True}
    monkeypatch.setattr(google, 'capabilities', lambda: grants)
    monkeypatch.setattr(google, '_gmail_service', lambda: gmail)
    monkeypatch.setattr(tool, '_google', lambda: google)
    def call(body):
        return tool.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT, policy=policy,
                            data_root=data, capability=capability, activation=activation)
    return data, policy, gmail, grants, call


def reviewed(setup):
    call = setup[-1]
    proposed = request({'operation': 'propose', 'message_id': 'message-123', 'body': 'I reviewed the cedar packet.\nThank you.'},
                       'preview', event='d' * 64, text='Draft a reply to Alice.')
    preview = call(proposed)
    assert preview['status'] == 'preview'
    args = {'operation': 'save', 'review': preview['review'], 'proposal': {
        'event_id': proposed['event_id'], 'action_id': proposed['action_id'],
        'args_hash': tool.digest(proposed['args']), 'accepted_dispatch_id': 'accepted-review',
        'source_revision': 'c' * 64}}
    return request(args)


def test_exact_reviewed_mime_commit_lost_response_inspection_restart_and_revocation(setup, monkeypatch):
    data, policy, gmail, grants, call = setup
    audit_path = data / 'events/sends.jsonl'
    monkeypatch.setattr(tool._google(), '_sends_path', lambda: str(audit_path))
    body = reviewed(setup)
    saved = call(body)  # consumer loses this response; receiver already retained its receipt
    assert saved['outcome'] == {'operation': 'save', 'draft_id': 'draft-123', 'thread_id': 'thread-123',
                               'review_hash': body['args']['review']['review_hash'], 'status': 'draft_saved'}
    assert len(gmail.created) == 1
    audit_raw = audit_path.read_text()
    audit = json.loads(audit_raw)
    assert audit['verb'] == 'gmail-draft' and audit['result'] == 'drafted'
    assert audit['to'] == body['args']['review']['to'] and 'message_id' not in audit
    assert audit['payload_sha256'] == hashlib.sha256(body['args']['review']['body'].encode()).hexdigest()
    assert body['args']['review']['subject'] not in audit_raw
    assert body['args']['review']['body'] not in audit_raw
    assert not {'body', 'subject', 'review'} & audit.keys()
    payload = gmail.created[0]
    assert payload['userId'] == 'me' and payload['body']['message']['threadId'] == 'thread-123'
    mime = message_from_bytes(base64.urlsafe_b64decode(payload['body']['message']['raw']))
    assert mime['To'] == 'alice@example.com' and mime['Subject'] == 'Re: Cedar review'
    assert mime['In-Reply-To'] == mime['References'] == '<cedar@example.com>'
    assert mime.get_payload(decode=True).decode(mime.get_content_charset() or 'ascii') == body['args']['review']['body']
    grants['gmail_draft'] = False
    policy['_source_authority'] = lambda: {'complete': False}
    # Reopen the persisted ledger as on process restart; no cached response or provider query.
    assert call(dict(body, operation='inspect')) == saved
    assert call(body) == saved and len(gmail.created) == 1
    doc = json.loads((data / 'events/owner-gmail-drafts.json').read_text())
    proof = doc['actions'][body['event_id']]['proof']
    assert proof['text_hash'] == hashlib.sha256(b'yes').hexdigest() and 'text' not in proof
    assert proof['domain'] == tool.DOMAIN


def test_provider_accepted_before_timeout_stays_unknown_and_never_recreates(setup, monkeypatch):
    data, _, gmail, _, call = setup
    audit_path = data / 'events/sends.jsonl'
    monkeypatch.setattr(tool._google(), '_sends_path', lambda: str(audit_path))
    body = reviewed(setup)
    gmail.after_accept = True
    assert call(body) == {'version': 1, 'status': 'unknown'}
    audit_raw = audit_path.read_text()
    audit = json.loads(audit_raw)
    assert audit['result'] == 'error' and audit['to'] == body['args']['review']['to']
    assert audit['payload_sha256'] == hashlib.sha256(body['args']['review']['body'].encode()).hexdigest()
    assert body['args']['review']['subject'] not in audit_raw
    assert body['args']['review']['body'] not in audit_raw
    assert not {'body', 'subject', 'review', 'error'} & audit.keys()
    assert call(dict(body, operation='inspect')) == {'version': 1, 'status': 'unknown'}
    assert call(body) == {'version': 1, 'status': 'unknown'} and len(gmail.created) == 1
    assert call(request(body['args'], event='f' * 64))['code'] == 'review_unavailable'
    assert audit_path.read_text() == audit_raw


@pytest.mark.parametrize('complete, revision', [(1, 'c' * 64), (True, 'not-a-revision')])
def test_malformed_current_authority_refuses_before_gmail_contact(setup, complete, revision):
    _, policy, gmail, _, call = setup
    policy['_source_authority'] = lambda: {'complete': complete, 'revision': revision,
                                          'google_sources': ['gmail']}
    result = call(request({'operation': 'propose', 'message_id': 'message-123',
                           'body': 'A reply'}, 'preview'))
    assert result == {'version': 1, 'status': 'rejected', 'code': 'authority_changed'}
    assert not gmail.created


@pytest.mark.parametrize('kind', ['threaded', 'new_email'])
def test_revocation_after_durable_reservation_is_stable_no_contact_refusal(setup, monkeypatch, kind):
    data, policy, gmail, _, call = setup
    if kind == 'threaded':
        body = reviewed(setup)
    else:
        offer = followup_offer(setup, monkeypatch)
        _, body = followup_review(setup, offer)
    writer = tool._preferences()
    monkeypatch.setattr(tool, '_preferences', lambda: writer)
    replace = writer._durable_preferences_replace
    def revoke_after_reservation(path, doc):
        result = replace(path, doc)
        if doc['actions'].get(body['event_id'], {}).get('status') == 'contacting':
            policy['_source_authority'] = lambda: {'complete': False}
        return result
    monkeypatch.setattr(writer, '_durable_preferences_replace', revoke_after_reservation)
    refused = call(body)
    assert refused == {'version': 1, 'status': 'rejected', 'code': 'authority_changed'}
    assert not gmail.created
    assert call(dict(body, operation='inspect')) == refused
    assert call(body) == refused
    record = json.loads((data / 'events/owner-gmail-drafts.json').read_text())
    assert record['actions'][body['event_id']]['status'] == 'rejected'
    assert body['args']['proposal']['action_id'] in record['consumed']
    assert not gmail.created


def test_revocation_after_draft_acceptance_keeps_committed_receipt(setup, monkeypatch):
    _, policy, gmail, _, call = setup
    body = reviewed(setup)
    create = gmail.create
    def accepted_then_revoked(**kwargs):
        operation = create(**kwargs)
        def execute():
            result = operation.execute()
            policy['_source_authority'] = lambda: {'complete': False}
            return result
        return SimpleNamespace(execute=execute)
    monkeypatch.setattr(gmail, 'create', accepted_then_revoked)
    saved = call(body)
    assert saved['status'] == 'committed'
    assert saved['outcome']['status'] == 'draft_saved'
    assert call(dict(body, operation='inspect')) == saved
    assert call(body) == saved
    assert len(gmail.created) == 1


def test_missing_draft_grant_is_original_immutable_refusal(setup):
    _, _, gmail, grants, call = setup
    body = reviewed(setup)
    grants['gmail_draft'] = False
    refused = call(body)
    assert refused['code'] == 'draft_permission_unavailable'
    grants['gmail_draft'] = True
    assert call(dict(body, operation='inspect')) == refused
    assert call(body) == refused and not gmail.created


def test_changed_target_unclear_yes_and_unattended_refuse(setup, monkeypatch):
    _, _, gmail, _, call = setup
    body = reviewed(setup)
    gmail.recipient = 'Other <other@example.com>'
    assert call(body)['code'] == 'target_changed'
    gmail.recipient = 'Alice <alice@example.com>'
    assert call(request(body['args'], event='1' * 64, text='Maybe later'))['code'] == 'review_unavailable'
    monkeypatch.setenv('SOTTO_UNATTENDED', '1')
    assert call(request(body['args'], event='2' * 64))['code'] == 'authority_changed' and not gmail.created


def test_invalid_recipient_headers_and_full_review_limit_refuse(setup):
    _, _, gmail, _, call = setup
    args = {'operation': 'propose', 'message_id': 'message-123', 'body': 'A reply'}
    for recipient in ('owner@example.com', 'Alice <alice@example.com>, Bob <bob@example.com>', 'bad-address'):
        gmail.recipient = recipient
        assert call(request(args, 'preview'))['code'] == 'invalid_target'
    gmail.recipient = 'Alice <alice@example.com>'
    assert call(request(dict(args, body='x' * 4000), 'preview'))['code'] == 'review_unavailable'
    assert not gmail.created


def test_changed_args_conflict_and_malformed_provider_receipt_hold(setup):
    _, _, gmail, _, call = setup
    body = reviewed(setup)
    gmail.bad_receipt = True
    assert call(body)['status'] == 'unknown'
    changed = json.loads(json.dumps(body['args']))
    changed['review']['body'] = 'Different reply'
    changed['review']['review_hash'] = tool._review_hash(changed['review'])
    assert call(request(changed))['code'] == 'event_conflict'
    assert len(gmail.created) == 1


def test_revoked_source_and_unknown_inspection_never_contact_provider(setup):
    _, policy, gmail, _, call = setup
    body = reviewed(setup)
    assert call(dict(body, operation='inspect')) == {'version': 1, 'status': 'not_found'}
    assert not gmail.created
    policy['_source_authority'] = lambda: {'complete': True, 'revision': 'e' * 64, 'google_sources': ['gmail']}
    refusal = call(body)
    assert refusal['code'] == 'authority_changed'
    policy['_source_authority'] = lambda: {'complete': True, 'revision': 'c' * 64, 'google_sources': ['gmail']}
    assert call(body) == refusal and not gmail.created


def followup_offer(setup, monkeypatch, *, recipient='alice@example.com', subject='Cedar follow-up', suffix=''):
    import pending_offer
    data, policy, _gmail, _grants, _call = setup
    monkeypatch.setattr(pending_offer, '_now', lambda: WHEN)
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', '9'*32)
    policy['_source_authority'] = lambda: {'complete': True, 'revision': 'c'*64,
        'google_sources': ['gmail'], 'bridge_sources': [], 'connector_sources': ['granola']}
    detail = {'version': 1, 'to_name': 'Alice' + suffix, 'to_email': recipient,
        'channel': 'email' if recipient else None, 'subject': subject, 'body': 'I will send the Cedar packet.' + suffix,
        'sources': ['granola'], 'source_revision': 'c'*64}
    raw = json.dumps(detail, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    offer = pending_offer.set_offer('followup_draft', 'Review this follow-up?', detail=raw,
        payload_sha256=hashlib.sha256(raw.encode()).hexdigest())
    pending_offer.activate_offer(offer, {'message_id': 'accepted-followup', 'target': 'photon',
        'chat_id': 'iMessage;-;owner', 'accepted_at': WHEN.timestamp()-1})
    return pending_offer.get_offer(offer_id=offer['offer_id'], target='photon', chat_id='iMessage;-;owner')


def followup_review(setup, offer, *, to=None):
    args = {'operation': 'propose_followup', 'offer_id': offer['offer_id'], **({'to': to} if to is not None else {})}
    proposed = request(args, 'preview', event='d'*64, text='Review the follow-up' + (' for '+to+'.' if to else ''))
    preview = setup[-1](proposed)
    assert preview['status'] == 'preview'
    save = request({'operation': 'save', 'review': preview['review'], 'proposal': {
        'event_id': proposed['event_id'], 'action_id': proposed['action_id'], 'args_hash': tool.digest(args),
        'accepted_dispatch_id': 'accepted-new-review', 'source_revision': 'c'*64,
        'origin': {'kind': 'followup', 'offer_id': offer['offer_id'], **({'to': to} if to else {})}}})
    return proposed, save


def test_followup_list_exact_new_email_mime_once_and_original_inspect(setup, monkeypatch):
    import pending_offer
    offer = followup_offer(setup, monkeypatch)
    call, gmail = setup[-1], setup[2]
    listing = call(request({'operation': 'list_followup'}, 'preview', text='Which follow-ups are ready?'))
    assert listing['status'] == 'offers' and listing['offers'][0]['offer_id'] == offer['offer_id']
    assert 'body' not in listing['offers'][0]
    proposed, save = followup_review(setup, offer)
    assert not gmail.created and not (setup[0]/'events/owner-gmail-drafts.json').exists()
    gmail.get = lambda **kwargs: pytest.fail('new email must not fetch a Gmail source message')
    result = call(save)
    assert result['status'] == 'committed' and len(gmail.created) == 1
    message = gmail.created[0]['body']['message']
    assert set(message) == {'raw'}
    mime = message_from_bytes(base64.urlsafe_b64decode(message['raw']))
    assert mime['To'] == 'alice@example.com' and mime['Subject'] == 'Cedar follow-up'
    assert mime['In-Reply-To'] is None and mime['References'] is None
    assert mime.get_payload(decode=True).decode() == 'I will send the Cedar packet.'
    assert not pending_offer.get_offer(offer_id=offer['offer_id'])
    setup[1]['_source_authority'] = lambda: {'complete': False}
    assert call(dict(save, operation='inspect')) == result and call(save) == result
    assert len(gmail.created) == 1


def test_followup_unknown_consumes_offer_and_never_repeats_after_restart(setup, monkeypatch):
    offer = followup_offer(setup, monkeypatch)
    _, save = followup_review(setup, offer)
    setup[2].after_accept = True
    call = setup[-1]
    assert call(save)['status'] == 'unknown'
    assert call(dict(save, operation='inspect'))['status'] == 'unknown'
    assert call(save)['status'] == 'unknown'
    assert call(request(save['args'], event='f'*64))['code'] == 'review_unavailable'
    assert len(setup[2].created) == 1


def test_followup_unknown_recipient_requires_exact_owner_address_and_empty_subject(setup, monkeypatch):
    offer = followup_offer(setup, monkeypatch, recipient=None, subject=None)
    call = setup[-1]
    args = {'operation': 'propose_followup', 'offer_id': offer['offer_id']}
    assert call(request(args, 'preview'))['code'] == 'invalid_target'
    assert call(request({**args, 'to': 'alice@example.com'}, 'preview', text='Use otheralice@example.com'))['code'] == 'invalid_target'
    _, save = followup_review(setup, offer, to='alice@example.com')
    assert save['args']['review']['subject'] == ''
    assert call(save)['status'] == 'committed'
    mime = message_from_bytes(base64.urlsafe_b64decode(setup[2].created[0]['body']['message']['raw']))
    assert mime['To'] == 'alice@example.com' and mime['Subject'] is None


def test_followup_known_recipient_immutable_and_changed_detail_or_source_refused(setup, monkeypatch):
    import pending_offer
    offer = followup_offer(setup, monkeypatch)
    args = {'operation': 'propose_followup', 'offer_id': offer['offer_id'], 'to': 'other@example.com'}
    assert setup[-1](request(args, 'preview', text='other@example.com'))['code'] == 'invalid_target'
    _, save = followup_review(setup, offer)
    path = pending_offer._path()
    state = pending_offer.jsonstore.read(path)
    state['offers'][0]['detail'] = state['offers'][0]['detail'].replace('Cedar packet', 'Changed packet')
    pending_offer.jsonstore.write_atomic(path, state)
    assert setup[-1](save)['code'] == 'invalid_target' and not setup[2].created
    # A different event cannot revive the unchanged old review after source revocation.
    setup[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'c'*64, 'google_sources': ['gmail'], 'connector_sources': []}
    assert setup[-1](request({'operation': 'list_followup'}, 'preview'))['status'] == 'no_match'


def test_followup_multiple_offers_nominate_one_preserve_others_and_cleanup_after_receipt(setup, monkeypatch):
    import pending_offer
    first = followup_offer(setup, monkeypatch)
    second = followup_offer(setup, monkeypatch, recipient='bob@example.com', suffix=' second')
    call = setup[-1]
    listed = call(request({'operation': 'list_followup'}, 'preview'))
    assert {row['offer_id'] for row in listed['offers']} == {first['offer_id'], second['offer_id']}
    _, save = followup_review(setup, first)
    clear = pending_offer.clear_offer
    monkeypatch.setattr(pending_offer, 'clear_offer', lambda **kwargs: (_ for _ in ()).throw(OSError('synthetic cleanup interruption')))
    result = call(save)
    assert result['status'] == 'committed'
    assert pending_offer.get_offer(offer_id=first['offer_id'])
    monkeypatch.setattr(pending_offer, 'clear_offer', clear)
    setup[1]['_source_authority'] = lambda: {'complete': False}
    assert call(dict(save, operation='inspect')) == result
    assert not pending_offer.get_offer(offer_id=first['offer_id'])
    assert pending_offer.get_offer(offer_id=second['offer_id']) == second
    assert len(setup[2].created) == 1


def test_followup_unknown_provider_receipt_and_offer_withdrawal_prevent_recontact(setup, monkeypatch):
    import pending_offer
    offer = followup_offer(setup, monkeypatch)
    _, save = followup_review(setup, offer)
    google = tool._google()
    original_service = google._gmail_service
    def withdraw():
        pending_offer.clear_offer(expected=offer)
        return original_service()
    monkeypatch.setattr(google, '_gmail_service', withdraw)
    assert setup[-1](save)['code'] == 'review_unavailable'
    assert not setup[2].created
    # A malformed acceptance cannot be converted into a confirmed save.
    monkeypatch.setattr(google, '_gmail_service', original_service)
    other = followup_offer(setup, monkeypatch, suffix='new')
    _, second_save = followup_review(setup, other)
    second_save = request(second_save['args'], event='f'*64)
    def create(**kwargs):
        def execute():
            setup[2].created.append(kwargs)
            return {'id': 'draft-123', 'message': {'threadId': 'invalid thread id'}}
        return SimpleNamespace(execute=execute)
    monkeypatch.setattr(setup[2], 'create', create)
    assert setup[-1](second_save)['status'] == 'unknown'
    assert setup[-1](dict(second_save, operation='inspect'))['status'] == 'unknown'
    assert len(setup[2].created) == 1


def offered_review(context, setup, monkeypatch, contextual_source=None):
    from test_runtime_owner_offer_reply import source_offer
    path, row, offer = source_offer(context, monkeypatch, contextual_source=contextual_source)
    gmail = setup[2]
    gmail.message_id = row['source_message_id']
    gmail.thread = row['source_thread_id']
    gmail.recipient = 'Dana <dana@outside.com>'
    selection = {'reply_to_message_id': 'accepted-source-offer', 'provider_sent_at_ms': None,
                 'provider_message_id': 'owner-offer-yes'}
    proposed = request({'operation': 'propose_offer', 'selection': selection}, 'preview', event='d' * 64)
    preview = setup[-1](proposed)
    assert preview['status'] == 'preview' and preview['offer_id'] == offer['offer_id']
    args = {'operation': 'save', 'review': preview['review'], 'proposal': {
        'event_id': proposed['event_id'], 'action_id': proposed['action_id'],
        'args_hash': tool.digest(proposed['args']), 'accepted_dispatch_id': 'accepted-full-review',
        'source_revision': 'c' * 64, 'origin': {'selection': selection, 'offer_id': offer['offer_id']}}}
    return path, proposed, request(args)


def test_producer_offer_review_separate_save_original_inspect_leaves_loop_open(context, setup, monkeypatch):
    path, proposed, save = offered_review(context, setup, monkeypatch)
    original = path.read_bytes()
    assert not setup[2].created
    result = setup[-1](save)
    assert result['status'] == 'committed' and len(setup[2].created) == 1
    assert path.read_bytes() == original
    assert setup[-1](dict(save, operation='inspect')) == result
    assert setup[-1](save) == result and len(setup[2].created) == 1
    assert setup[-1](proposed) == {'version': 1, 'status': 'no_match'}


def test_producer_offer_provider_unknown_remains_held_after_restart(context, setup, monkeypatch):
    path, _, save = offered_review(context, setup, monkeypatch)
    original = path.read_bytes()
    setup[2].after_accept = True
    assert setup[-1](save) == {'version': 1, 'status': 'unknown'}
    assert setup[-1](dict(save, operation='inspect')) == {'version': 1, 'status': 'unknown'}
    assert setup[-1](save) == {'version': 1, 'status': 'unknown'}
    assert len(setup[2].created) == 1 and path.read_bytes() == original


def test_offer_cleared_during_header_read_cannot_mint_review(context, setup, monkeypatch):
    _, proposed, _ = offered_review(context, setup, monkeypatch)
    original = setup[2].getProfile
    def withdraw(**kwargs):
        tool._offers().clear_offer()
        return original(**kwargs)
    monkeypatch.setattr(setup[2], 'getProfile', withdraw)
    assert setup[-1](proposed)['code'] == 'target_changed'
    assert not setup[2].created


def test_explicit_gmail_origin_context_union_and_revocation(context, setup, monkeypatch):
    import source_context
    _, _, save = offered_review(context, setup, monkeypatch, contextual_source='imessage')
    monkeypatch.setattr(source_context, 'allowed', lambda source: source != 'imessage')
    assert setup[-1](save)['code'] == 'authority_changed'
    assert not setup[2].created


def test_explicit_gmail_origin_with_permitted_context_can_save(context, setup, monkeypatch):
    _, _, save = offered_review(context, setup, monkeypatch, contextual_source='imessage')
    assert setup[-1](save)['status'] == 'committed'
    assert len(setup[2].created) == 1


def test_once_per_offer_across_distinct_reviews_and_interrupted_exact_clear(context, setup, monkeypatch):
    import pending_offer
    _, proposed, save = offered_review(context, setup, monkeypatch)
    second_proposed = request(proposed['args'], 'preview', event='e' * 64)
    second_preview = setup[-1](second_proposed)
    second_args = dict(save['args'], proposal={**save['args']['proposal'],
        'event_id': second_proposed['event_id'], 'action_id': second_proposed['action_id'],
        'args_hash': tool.digest(second_proposed['args'])}, review=second_preview['review'])
    real_clear = pending_offer.clear_offer
    monkeypatch.setattr(pending_offer, 'clear_offer', lambda **kwargs: (_ for _ in ()).throw(OSError('interrupted cleanup')))
    result = setup[-1](save)
    assert result['status'] == 'committed'
    assert setup[-1](request(second_args, event='f' * 64))['code'] == 'review_unavailable'
    assert len(setup[2].created) == 1
    monkeypatch.setattr(pending_offer, 'clear_offer', real_clear)
    # A newer accepted question must survive original receipt inspection/cleanup.
    newer = pending_offer.set_offer('meeting_prep', 'Pull another prep?', 'Dana')
    pending_offer.activate_offer(newer, {'message_id': 'newer-offer', 'target': 'photon',
        'chat_id': 'iMessage;-;owner', 'accepted_at': WHEN.timestamp()})
    setup[3]['gmail_read'] = False
    setup[1]['_source_authority'] = lambda: {'complete': False}
    assert setup[-1](dict(save, operation='inspect')) == result
    assert pending_offer.get_offer(message_id='newer-offer')['offer_id'] == newer['offer_id']
    assert len(setup[2].created) == 1


def test_offer_validator_loads_in_fresh_interpreter_without_knowledge_path():
    import os
    from pathlib import Path
    import subprocess
    import sys
    receiver = Path(tool.__file__).parent
    code = f"import sys; sys.path.insert(0, {str(receiver)!r}); import runtime_owner_gmail_draft_tool as t; t._offers(); from runtime_owner_loop import _load_writer; assert callable(_load_writer().source_offer_row)"
    env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    completed = subprocess.run([sys.executable, '-I', '-B', '-c', code], env=env,
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr


def _retention(data, monkeypatch):
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location('retention_drafts', os.path.join(os.path.dirname(__file__), 'retention.py'))
    ret = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ret)
    monkeypatch.setitem(ret.HOOKS, 'data_root', lambda: str(data))
    return ret


def test_reviewed_draft_words_age_out_with_the_draft_ledger_but_receipts_stay_idempotent(setup, monkeypatch):
    data, policy, gmail, grants, call = setup
    ret = _retention(data, monkeypatch)
    body = reviewed(setup)
    saved = call(body)
    assert saved['status'] == 'committed'
    path = data / 'events/owner-gmail-drafts.json'
    words = json.dumps(body['args']['review']['body'])[1:-1]
    assert isinstance(ret.accounts_for('events/owner-gmail-drafts.json'), ret.Rule)
    assert ret.accounts_for('events/owner-gmail-drafts.json').amount == ret.DRAFT_LEDGER_DAYS == tool.BODY_DAYS
    young = ret.sweep(now=WHEN.timestamp() + (ret.DRAFT_LEDGER_DAYS - 1) * 86400)
    assert young['errors'] == [] and words in path.read_text()
    aged = ret.sweep(now=WHEN.timestamp() + 400 * 86400)
    assert aged['errors'] == [] and {'path': 'events/owner-gmail-drafts.json', 'policy': ret.COMPACT_OLDER,
                                     'amount': 1} in aged['swept']
    text = path.read_text()
    assert words not in text and 'Cedar review' not in text and 'alice@example.com' not in text
    # The content-free receipt still answers the same exact replay, without contacting Gmail.
    grants['gmail_draft'] = False
    policy['_source_authority'] = lambda: {'complete': False}
    assert call(dict(body, operation='inspect')) == saved
    assert call(body) == saved and len(gmail.created) == 1
    other = {**body['args']['review'], 'body': 'Other words.'}
    other['review_hash'] = tool._review_hash(other)
    changed = request({**body['args'], 'review': other})  # same event, different reviewed words
    assert call(changed) == {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
    again = ret.sweep(now=WHEN.timestamp() + 800 * 86400)  # compaction is idempotent
    assert again['errors'] == [] and not [row for row in again['swept'] if row['policy'] == ret.COMPACT_OLDER]


def test_aged_preview_cannot_be_approved_after_its_words_are_gone(setup, monkeypatch):
    data, _, gmail, _, call = setup
    ret = _retention(data, monkeypatch)
    proposed = request({'operation': 'propose_typed', 'message_id': 'message-123',
                        'body': 'Typed words.', 'action_type': 'reply'}, 'preview', event='d' * 64,
                       text='Draft a reply to Alice.')
    preview = call(proposed)
    assert preview['status'] == 'preview'
    ret.sweep(now=WHEN.timestamp() + 400 * 86400)
    assert 'Typed words.' not in (data / 'events/owner-gmail-drafts.json').read_text()
    assert call(proposed) == {'version': 1, 'status': 'rejected', 'code': 'review_unavailable'}
    approval = request({'operation': 'save', 'review': preview['review'], 'proposal': {
        'event_id': proposed['event_id'], 'action_id': proposed['action_id'],
        'args_hash': tool.digest(proposed['args']), 'accepted_dispatch_id': 'accepted-review',
        'source_revision': 'c' * 64}})
    assert call(approval) == {'version': 1, 'status': 'rejected', 'code': 'review_unavailable'}
    assert not gmail.created
