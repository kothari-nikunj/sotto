"""Fresh send uses original MIME and exact reviewed session; recovery never sends again."""
import base64
from email import message_from_bytes

import pytest

# Imported fixtures are intentionally injected by pytest.
# ruff: noqa: F811

import runtime_owner_gmail_draft_tool as tool
from test_runtime_owner_gmail_draft_tool import request
from test_runtime_owner_gmail_send_tool import sending, setup, context  # noqa: F401


def proposed_send(sending, **changes):
    args = {'operation': 'propose_new_send', 'to': 'alice@example.com',
            'subject': 'A fresh subject', 'body': 'Hello Alice.\nThank you.'}
    args.update(changes)
    proposal = request(args, 'preview', event='d' * 64, text='Email Alice.')
    result = sending[-1](proposal)
    assert result['status'] == 'preview'
    assert not sending[2].posts
    return request({'operation': 'send', 'review': result['review'], 'proposal': {
        'event_id': proposal['event_id'], 'action_id': proposal['action_id'],
        'args_hash': tool.digest(args), 'accepted_dispatch_id': 'accepted-review',
        'source_revision': 'c' * 64}})


def test_original_fresh_mime_and_provider_thread_recovery(sending):
    body = proposed_send(sending)
    sending[2].thread = 'existing-grouped-thread'
    result = sending[-1](body)
    assert result['status'] == 'committed'
    assert result['outcome']['thread_id'] == 'existing-grouped-thread'
    payload, = sending[2].posts
    assert set(payload) == {'raw'}
    mail = message_from_bytes(base64.urlsafe_b64decode(payload['raw'] + '=='))
    assert mail['To'] == 'alice@example.com'
    assert mail['Subject'] == 'A fresh subject'
    assert not mail['In-Reply-To'] and not mail['References']
    assert mail.get_payload(decode=True).decode() == body['args']['review']['body']
    sending[1]['_source_authority'] = lambda: {'complete': False}
    assert sending[-1]({**body, 'operation': 'inspect'}) == result
    assert sending[-1](body) == result
    assert len(sending[2].posts) == 1


@pytest.mark.parametrize('fault', ['loss', 'bad_readback'])
def test_uncertainty_never_sends_again(sending, fault):
    body = proposed_send(sending)
    setattr(sending[2], fault, True)
    assert sending[-1](body)['status'] == 'unknown'
    setattr(sending[2], fault, False)
    assert sending[-1]({**body, 'operation': 'inspect'})['status'] == 'unknown'
    assert sending[-1](body)['status'] == 'unknown'
    assert len(sending[2].posts) == 1


@pytest.mark.parametrize('header', ['In-Reply-To', 'References', 'Cc', 'Bcc', 'Resent-To'])
def test_extra_headers_hold_exact_send(sending, monkeypatch, header):
    body = proposed_send(sending)
    original = sending[2].send_reviewed
    def augmented(payload):
        result = original(payload)
        mail = message_from_bytes(base64.urlsafe_b64decode(sending[2].raw + '=='))
        mail[header] = 'other@example.com'
        sending[2].raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        return result
    monkeypatch.setattr(sending[2], 'send_reviewed', augmented)
    assert sending[-1](body)['status'] == 'unknown'
    assert sending[-1]({**body, 'operation': 'inspect'})['status'] == 'unknown'
    assert len(sending[2].posts) == 1


@pytest.mark.parametrize('change', ['sender', 'grant', 'revision'])
def test_changed_authority_or_sender_zero_post(sending, monkeypatch, change):
    body = proposed_send(sending)
    if change == 'sender':
        from types import SimpleNamespace
        monkeypatch.setattr(sending[2], 'getProfile', lambda **kwargs:
                            SimpleNamespace(execute=lambda: {'emailAddress': 'changed@example.com'}))
    elif change == 'grant':
        sending[3]['gmail_send'] = False
    else:
        sending[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'f' * 64, 'sources': ['gmail']}
    assert sending[-1](body)['status'] == 'rejected'
    assert not sending[2].posts


@pytest.mark.parametrize('changes', [{'to': 'a@example.com;b@example.com'}, {'subject': 'bad\u0085subject'}, {'body': 'x' * 4000}])
def test_invalid_or_unreviewable_zero_send(sending, changes):
    args = {'operation': 'propose_new_send', 'to': 'alice@example.com', 'subject': '', 'body': 'Hello'}
    args.update(changes)
    assert sending[-1](request(args, 'preview'))['status'] == 'rejected'
    assert not sending[2].posts


def test_final_same_session_profile_pin_before_post(sending, monkeypatch):
    from types import SimpleNamespace
    body = proposed_send(sending)
    reads = []
    def profile(**kwargs):
        reads.append(1)
        address = 'owner@example.com' if len(reads) == 1 else 'switched@example.com'
        return SimpleNamespace(execute=lambda: {'emailAddress': address})
    monkeypatch.setattr(sending[2], 'getProfile', profile)
    assert sending[-1](body)['status'] == 'unknown'
    assert len(reads) == 2 and not sending[2].posts
    assert sending[-1]({**body, 'operation': 'inspect'})['status'] == 'unknown'
    assert len(reads) == 2


@pytest.mark.parametrize('fault', ['body', 'to', 'from', 'thread'])
def test_readback_mismatch_held(sending, monkeypatch, fault):
    body = proposed_send(sending)
    original = sending[2].send_reviewed
    def mismatched(payload):
        result = original(payload)
        if fault == 'thread':
            return {**result, 'threadId': 'different-thread'}
        mail = message_from_bytes(base64.urlsafe_b64decode(sending[2].raw + '=='))
        if fault == 'body':
            mail.set_payload('Different body')
        else:
            name = 'To' if fault == 'to' else 'From'
            mail.replace_header(name, 'other@example.com')
        sending[2].raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        return result
    monkeypatch.setattr(sending[2], 'send_reviewed', mismatched)
    assert sending[-1](body)['status'] == 'unknown'
    assert sending[-1]({**body, 'operation': 'inspect'})['status'] == 'unknown'
    assert len(sending[2].posts) == 1
