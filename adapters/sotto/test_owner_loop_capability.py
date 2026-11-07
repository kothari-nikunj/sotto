"""Channel-specific owner-command bearer derivation remains exact and isolated."""
import copy
import hashlib
import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff' / '_shared' / 'lib' / 'owner_loop_capability.py'
SPEC = importlib.util.spec_from_file_location('owner_loop_capability', SOURCE)
owner_loop_capability = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner_loop_capability)


ROOT = 'root-mcp-fixture-token-0123456789'
PHOTON = {
    'tenant_id': 'tenant-a', 'channel': 'photon', 'account_id': 'account-a',
    'owner_id': '+15551234567', 'line_key': 'line-a', 'target': 'photon',
    'route_mode': 'first_contact', 'route_id': '+15557654321',
    'project_fingerprint': 'a' * 64,
}
TELEGRAM = {
    'tenant_id': 'tenant-a', 'channel': 'telegram', 'account_id': 'account-a',
    'owner_id': '8675309', 'line_key': 'line-a', 'target': 'telegram',
    'route_mode': 'fixed', 'route_id': 'telegram:8675309',
    'bot_fingerprint': hashlib.sha256(b'sotto-telegram-bot-v1:fixture-token').hexdigest(),
}


def test_existing_photon_domain_stays_byte_for_byte_stable():
    assert owner_loop_capability.derive(ROOT, PHOTON) == (
        'd8dbf14f9a758b009b4853b6cf09fd66a9194e0d658c882c3690c96e78066e6c')
    assert owner_loop_capability.derive(ROOT, TELEGRAM) != owner_loop_capability.derive(ROOT, PHOTON)


@pytest.mark.parametrize(('field', 'value'), [
    ('owner_id', '08675309'), ('owner_id', '9007199254740992'),
    ('route_id', 'telegram:other'), ('route_mode', 'first_contact'),
    ('target', 'photon'), ('bot_fingerprint', '0' * 63),
])
def test_telegram_canonical_identity_refuses_mismatch(field, value):
    changed = copy.deepcopy(TELEGRAM)
    changed[field] = value
    with pytest.raises(ValueError):
        owner_loop_capability.derive(ROOT, changed)


def test_cross_channel_fields_and_empty_telegram_tenant_are_refused():
    for identity in ({**TELEGRAM, 'project_fingerprint': 'a' * 64},
                     {**PHOTON, 'bot_fingerprint': 'a' * 64},
                     {**TELEGRAM, 'tenant_id': ''}):
        with pytest.raises(ValueError):
            owner_loop_capability.derive(ROOT, identity)
