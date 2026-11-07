"""Private close wire through the real canonical owner writer, without HTTP or provider calls."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

HERE = Path(__file__).resolve().parent
PACK = HERE.parents[1] / 'sotto-chief-of-staff'
sys.path.insert(0, str(PACK / '_shared' / 'lib'))
sys.path.insert(0, str(PACK / '_shared' / 'scripts'))
sys.path.insert(0, str(PACK / '_shared' / 'knowledge'))
import knowledge_edit as ke  # noqa: E402
import owner_loop_capability as capability  # noqa: E402

spec = importlib.util.spec_from_file_location('owner_close_receiver', HERE / 'runtime_owner_loop.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)

WHEN = datetime(2026, 10, 5, 17, 35, tzinfo=timezone.utc)
THREAD = 'imessage:iMessage;-;owner~+15555550123'
ROOT = 'synthetic-owner-loop-root-' + 'x' * 40
PROJECT = 'synthetic-project'
FINGERPRINT = hashlib.sha256(('sotto-photon-project-v1:' + PROJECT).encode()).hexdigest()
IDENTITY = {'tenant_id': 'tenant', 'channel': 'photon', 'account_id': 'account',
            'owner_id': '+15555550123', 'line_key': 'primary', 'target': 'photon',
            'route_mode': 'fixed', 'route_id': THREAD, 'project_fingerprint': FINGERPRINT}
EVENT = 'a' * 64


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    policy = {'SOTTO_DEPLOYMENT_MODE': 'managed',
              'SOTTO_CRON_DELIVER': 'photon', 'SOTTO_TENANT_ID': 'tenant',
              'PHOTON_HOME_CHANNEL': IDENTITY['owner_id'],
              'PHOTON_ALLOWED_USERS': IDENTITY['owner_id'],
              'PHOTON_PROJECT_ID': PROJECT, 'SOTTO_IMESSAGE_NUMBER': '+15555550123',
              '_configured_tz_name': lambda: 'UTC', '_tz_resolve': lambda name: ZoneInfo(name)}
    activation = SimpleNamespace(verified_current_photon_route=lambda _root, _policy:
                                 {'chat_id': 'iMessage;-;owner'})
    return tmp_path, policy, activation


def request(anchor, operation, *, zone=None, digest=None):
    text = '/loop close ' + anchor
    return {'version': 1, 'operation': operation, 'identity': IDENTITY,
            'action_id': hashlib.sha256(json.dumps(['sotto.owner-loop-close-command.v1', EVENT],
                                                    separators=(',', ':')).encode()).hexdigest(),
            'event_id': EVENT, 'payload_hash': 'b' * 64,
            'text_hash': hashlib.sha256(text.encode()).hexdigest(), 'text': text,
            'admitted_at_ms': int(WHEN.timestamp() * 1000), 'zone': zone,
            'provider_thread_id': THREAD, 'anchor_key': anchor, 'expected_row_digest': digest}


def call(body, context):
    root, policy, activation = context
    return runtime.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT,
                           policy=policy, data_root=root, capability=capability,
                           activation=activation)


def test_close_prepare_write_inspect_and_exact_wire(context):
    anchor = ke.op_loop_add_admitted_owner_text(
        'Send the signed deck', owner_text_span='Send the signed deck',
        owner_event_id='add-event', now=WHEN)['anchor_key']
    prepared = call(request(anchor, 'close_prepare'), context)
    assert prepared['status'] == 'prepared' and prepared['zone'] == 'UTC'
    pre = prepared['expected_row_digest']
    committed = call(request(anchor, 'close_write', zone='UTC', digest=pre), context)
    assert committed['status'] == 'committed' and committed['anchor_key'] == anchor
    assert call(request(anchor, 'close_inspect', zone='UTC', digest=pre), context) == committed
    assert call(request(anchor, 'close_write', zone='UTC', digest=pre), context) == committed
    for malformed in (dict(request(anchor, 'close_prepare'), extra=True),
                      dict(request(anchor, 'prepare')),
                      dict(request(anchor, 'close_write', zone='UTC', digest='c' * 64), text='wrong'),
                      request(' ' + anchor, 'close_prepare'), request(anchor + ' ', 'close_prepare')):
        with pytest.raises(ValueError):
            call(malformed, context)


def test_close_route_zone_and_target_refusal(context):
    anchor = ke.op_loop_add_admitted_owner_text(
        'Send the signed deck', owner_text_span='Send the signed deck',
        owner_event_id='add-event', now=WHEN)['anchor_key']
    prepared = call(request(anchor, 'close_prepare'), context)
    with pytest.raises(runtime.UnavailableError, match='timezone changed'):
        call(request(anchor, 'close_write', zone='America/Los_Angeles',
                     digest=prepared['expected_row_digest']), context)
    changed = dict(request(anchor, 'close_prepare'), provider_thread_id='imessage:iMessage;-;other~+15555550123')
    with pytest.raises(runtime.UnavailableError, match='route changed'):
        call(changed, context)
    rejected = call(request('missing', 'close_prepare'), context)
    assert rejected == {'version': 1, 'status': 'rejected', 'code': 'owner_close_target_unavailable'}


def test_selfhost_photon_close_uses_canonical_writer_and_current_route(context):
    root, policy, activation = context
    policy.update(SOTTO_DEPLOYMENT_MODE='self_host',
                  SOTTO_RUNTIME_PROFILE='self-host-photon-fixed-five-v1',
                  GOOGLE_AI_API_KEY='test-key')
    anchor = ke.op_loop_add_admitted_owner_text(
        'Send the signed deck', owner_text_span='Send the signed deck',
        owner_event_id='add-event', now=WHEN)['anchor_key']
    prepared = call(request(anchor, 'close_prepare'), context)
    assert prepared['status'] == 'prepared'
    body = request(anchor, 'close_write', zone='UTC', digest=prepared['expected_row_digest'])
    committed = call(body, context)
    assert committed['status'] == 'committed'
    assert call(body, context) == committed
    policy['SOTTO_RUNTIME_PROFILE'] = 'self-host-telegram-fixed-five-v1'
    with pytest.raises(runtime.UnavailableError):
        call(request(anchor, 'close_inspect', zone='UTC', digest=prepared['expected_row_digest']), context)
    policy['SOTTO_RUNTIME_PROFILE'] = 'self-host-photon-fixed-five-v1'
    policy['SOTTO_MODEL_PROXY_URL'] = 'https://proxy.example'
    with pytest.raises(runtime.UnavailableError):
        call(request(anchor, 'close_inspect', zone='UTC', digest=prepared['expected_row_digest']), context)
