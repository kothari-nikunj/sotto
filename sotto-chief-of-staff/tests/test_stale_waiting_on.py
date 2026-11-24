"""Stale Gmail hand-offs and the evidence shared by the critic and Learn."""

import pytest

import compose_brief as cb
import continuity_resolve as cr
import test_capture_structure as capture

admitted_stale_sent = capture.admitted_stale_sent
sandbox = capture.sandbox


def test_stale_proof_uses_current_contacts_permission(admitted_stale_sent, monkeypatch):
    import source_context
    from stale_sent import stale_waiting_on_evidence

    google, local, _admitted, _asked, _now = admitted_stale_sent
    assert stale_waiting_on_evidence(google, local)
    allowed = source_context.allowed
    monkeypatch.setattr(source_context, 'allowed', lambda source: source != 'contacts' and allowed(source))
    assert stale_waiting_on_evidence(google, local) == []


def test_stale_email_reply_owed_by_them_is_captured_as_waiting_on(admitted_stale_sent):
    _google, _local, admitted_local, asked, now = admitted_stale_sent

    def resolve(owed_by, marked):
        candidate = {**capture.action('reply', asked, 'stale-1', contextAsk='Send the signed term sheet'),
                     'channel': 'email', 'contactIdentifier': 'dan@example.com', 'owedBy': owed_by,
                     'newObligation': marked}
        candidate['evidence'][0]['sourceType'] = 'email'
        return cr.resolve({'today': now.strftime('%Y-%m-%d'), 'local': admitted_local,
                           'new_actions': [candidate]}, now)

    assert resolve('owner', True)['active'] == []
    unmarked = resolve('them', False)
    assert unmarked['active'] == [] and 'direction_corrected' not in unmarked['capture_outcomes']
    captured = resolve('them', True)
    row, = captured['active']
    assert row['action_type'] == 'waiting_on' and captured['capture_outcomes']['direction_corrected'] == 1


def test_stale_email_is_critic_evidence_for_a_reply_owed_by_them(admitted_stale_sent):
    google, local, _admitted, asked, _now = admitted_stale_sent
    sources = cb._review_message_sources({'local': local, 'google': google})
    candidate = capture.action('reply', asked, 'stale-1', contactIdentifier='dan@example.com', owedBy='them')
    candidate['evidence'][0]['sourceType'] = 'email'
    observed = cb._critic_action_evidence(candidate, sources)['evidence'][0]['observed_source']
    assert observed['direction'] == 'sent_by_user' and observed['quote_matches_source']
    for other in ({**candidate, 'owedBy': 'owner'}, {**candidate, 'newObligation': False}):
        assert cb._critic_action_evidence(other, sources)['evidence'][0]['observed_source']['status'] == 'unavailable'


@pytest.mark.parametrize('aliases, expected', [
    ({'owedBy': 'them', 'owed_by': 'owner'}, 'unavailable'),
    ({'owedBy': 'owner', 'owed_by': 'them'}, 'matched'),
    ({'newObligation': False, 'new_obligation': True}, 'matched'),
    ({'contactIdentifier': 'dan@example.com', 'contact_identifier': capture.EMAIL}, 'unavailable'),
    ({'type': 'reply', 'action_type': 'follow_up'}, 'unavailable'),
])
def test_stale_reply_eligibility_agrees_in_critic_and_resolver(admitted_stale_sent, aliases, expected):
    google, local, admitted_local, asked, now = admitted_stale_sent
    candidate = capture.action('reply', asked, 'stale-1', channel='email',
                       contactIdentifier='dan@example.com', contextAsk='Send the signed term sheet', owedBy='them')
    candidate.update(aliases)
    candidate['evidence'][0]['sourceType'] = 'email'
    sources = cb._review_message_sources({'local': local, 'google': google})
    observed = cb._critic_action_evidence(candidate, sources)['evidence'][0]['observed_source']
    captured = cr.resolve({'today': now.strftime('%Y-%m-%d'), 'local': admitted_local,
                           'new_actions': [candidate]}, now, resolve_existing=False)
    assert bool(captured['active']) == (expected == 'matched')
    assert observed['status'] == expected


@pytest.mark.parametrize('thread, hours_ago, labels, answered', [
    ('t1', 1, ['INBOX'], True),
    ('t1', 24 * 5, ['INBOX'], False),
    ('different-thread', 1, ['INBOX'], False),
    ('t1', 1, ['DRAFT'], False),
    ('t1', 1, ['SENT'], False),
])
def test_newer_inbound_email_fences_stale_request_at_every_stage(admitted_stale_sent, thread, hours_ago,
                                                                 labels, answered):
    from datetime import timedelta
    from stale_sent import stale_waiting_on_evidence
    google, local, admitted_local, asked, now = admitted_stale_sent
    reply = {'id': 'answer-1', 'threadId': thread, 'date': (now - timedelta(hours=hours_ago)).isoformat(),
             'from': 'dan@example.com', 'to': 'owner@example.com', 'labelIds': labels,
             'isSent': 'SENT' in labels, 'body': 'Attached is the signed term sheet.'}
    google = {'staleThreads': google['stale_threads'], 'emails': [reply]}
    inputs = {'local': local, 'google': google}
    candidate = capture.action('reply', asked, 'stale-1', channel='email', contactName='Dan Example',
                       contactIdentifier='dan@example.com', contextAsk='Send the signed term sheet', owedBy='them')
    candidate['evidence'][0]['sourceType'] = 'email'
    captured = cr.resolve({'today': now.strftime('%Y-%m-%d'),
                           'local': admitted_local, 'emails': [reply],
                           'new_actions': [candidate]}, now, resolve_existing=False)
    assert bool(captured['active']) is not answered
    assert bool(stale_waiting_on_evidence(google, local)) is not answered
    shown_threads = cb._normalize_local(inputs)['stale_threads']
    assert ('t1' in {row['threadId'] for row in shown_threads}) is not answered
    sources = cb._review_message_sources(inputs)
    observed = cb._critic_action_evidence(candidate, sources)['evidence'][0]['observed_source']
    assert observed['status'] == ('unavailable' if answered else 'matched')
