"""Account authorization and DM possession are independent proofs."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_broker import broker  # noqa: F401 — shared isolated account-service fixture
from linking import Linking, LINK_TTL, PROOF_ATTEMPTS


@pytest.fixture
def links(broker):  # noqa: F811 — pytest resolves the imported fixture by name
    clock = [1000.0]
    broker.registry.register('second', 'https://second.example.com', 'second-token', 'second@example.com')
    owners = []
    with broker.db() as db:
        db.execute('BEGIN IMMEDIATE')
        for sub, email in (('owner-sub', 'owner@example.com'), ('second-sub', 'second@example.com')):
            owners.append(broker.registry.claim(db, {'iss': 'https://accounts.google.com', 'sub': sub, 'email': email, 'email_verified': True})[0])
    return Linking(broker.registry, lambda: clock[0]), owners, clock


def dm(service, message, handle='+15555550100', **overrides):
    return service.observe(**{'line': 'sotto-line', 'kind': 'phone', 'handle': handle,
        'conversation': 'dm:' + handle, 'text': message, 'is_group': False, **overrides})


def test_both_proofs_required_and_route_survives_restart(links):
    service, (owner, other), clock = links
    intent = service.start(owner, line='sotto-line')
    assert service.start(owner, line='sotto-line') == intent
    assert service.resolve(line='sotto-line', kind='phone', handle='+15555550100') is None
    with pytest.raises(PermissionError):
        service.confirm(owner, intent['intent_id'], 'made-up-proof')
    assert dm(service, intent['message'])
    observed = service.start(owner, line='sotto-line')
    # Forwarding the challenge or learning its id never supplies account approval.
    with pytest.raises(PermissionError):
        service.confirm(other, observed['intent_id'], observed['proof_revision'])
    assert service.resolve(line='sotto-line', kind='phone', handle='+15555550100') is None
    binding = service.confirm(owner, observed['intent_id'], observed['proof_revision'])
    assert service.confirm(owner, observed['intent_id'], observed['proof_revision']) == binding
    restarted = Linking(service.registry, lambda: clock[0])
    route = restarted.resolve(line='sotto-line', kind='phone', handle='+15555550100')
    assert route['account_id'] == owner and route['tenant_id'] == 'tenant-fixture'
    assert route['version'] == 1
    assert restarted.resolve(line='other-line', kind='phone', handle='+15555550100') is None
    assert restarted.resolve(line='sotto-line', kind='email', handle='owner@example.com') is None


def test_changed_sender_cannot_replace_confirmation_target(links):
    service, (owner, _), _ = links
    intent = service.start(owner, line='sotto-line')
    assert not dm(service, intent['message'], is_group=True)
    assert dm(service, intent['message'])
    first = service.start(owner, line='sotto-line')
    assert not dm(service, intent['message'], '+15555550200')
    assert service.start(owner, line='sotto-line') == first
    with pytest.raises(PermissionError):
        service.confirm(owner, first['intent_id'], 'stale-or-forged-revision')


def test_challenges_expire_and_bruteforce_is_bounded(links):
    service, (owner, _), clock = links
    intent = service.start(owner, line='sotto-line')
    for _ in range(PROOF_ATTEMPTS):
        assert not dm(service, 'Connect Sotto AAAAAAAAAAAA')
    assert not dm(service, intent['message'])
    clock[0] += LINK_TTL
    assert not dm(service, intent['message'])
    current = service.start(owner, line='sotto-line')
    assert current['intent_id'] != intent['intent_id'] and dm(service, current['message'])


def test_two_accounts_cannot_claim_one_sender_even_concurrently(links):
    service, owners, _ = links
    confirmations = []
    for owner in owners:
        intent = service.start(owner, line='sotto-line')
        assert dm(service, intent['message'])
        observed = service.start(owner, line='sotto-line')
        confirmations.append((owner, observed['intent_id'], observed['proof_revision']))
    def confirm(args):
        try:
            return service.confirm(*args)
        except PermissionError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, confirmations))
    assert sum(bool(r) for r in results) == 1


def test_revocation_invalidates_old_confirmation_and_increments_route_version(links):
    service, (owner, _), _ = links
    intent = service.start(owner, line='sotto-line')
    dm(service, intent['message'])
    observed = service.start(owner, line='sotto-line')
    before = service.confirm(owner, observed['intent_id'], observed['proof_revision'])
    service.revoke(owner)
    assert service.resolve(line='sotto-line', kind='phone', handle='+15555550100') is None
    with pytest.raises(PermissionError):
        service.confirm(owner, observed['intent_id'], observed['proof_revision'])
    new = service.start(owner, line='sotto-line')
    dm(service, new['message'])
    current = service.start(owner, line='sotto-line')
    after = service.confirm(owner, current['intent_id'], current['proof_revision'])
    assert after['id'] == before['id'] and after['version'] > before['version']


def test_suspension_disables_routing_without_reassigning_sender(links):
    service, (owner, _), _ = links
    intent = service.start(owner, line='sotto-line')
    dm(service, intent['message'])
    observed = service.start(owner, line='sotto-line')
    service.confirm(owner, observed['intent_id'], observed['proof_revision'])
    with service.db() as db:
        db.execute("UPDATE tenants SET status='suspended' WHERE account_id=?", (owner,))
    assert service.resolve(line='sotto-line', kind='phone', handle='+15555550100') is None
    with pytest.raises(PermissionError):
        service.start(owner, line='sotto-line')


def test_rejected_candidate_can_be_replaced_only_with_a_new_proof(links):
    service, (owner, other), _ = links
    intent = service.start(owner, line='sotto-line')
    assert dm(service, intent['message'])
    observed = service.start(owner, line='sotto-line')
    with pytest.raises(PermissionError):
        service.cancel(other, intent['intent_id'])
    service.cancel(owner, intent['intent_id'])
    with pytest.raises(PermissionError):
        service.confirm(owner, observed['intent_id'], observed['proof_revision'])
    current = service.start(owner, line='sotto-line')
    assert current['message'] != intent['message']
    assert not dm(service, intent['message'])
    assert dm(service, current['message'], '+15555550200')
