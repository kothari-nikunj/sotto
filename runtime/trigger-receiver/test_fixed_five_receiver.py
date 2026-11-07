"""The selected limited profile does not spend scheduler slots before ACK."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import time

import pytest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('receiver_fixed_five_test', HERE / 'receiver.py')
rec = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rec)


def test_unacknowledged_photon_channel_preserves_all_five_slots(monkeypatch):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'managed-photon-fixed-five-v1')
    events = []
    monkeypatch.setattr(rec, '_activation_tick', lambda: events.append('activation'))
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: None)
    monkeypatch.setattr(rec, '_background_context_tick', lambda: pytest.fail('background ran'))
    monkeypatch.setattr(rec, '_prepare_briefs', lambda: pytest.fail('brief prepared'))
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda: pytest.fail('cron slot probed'))
    before = dict(rec._CRON_FIRED)
    rec._cron_tick()
    rec._cron_tick()
    assert events == ['activation', 'activation']
    assert rec._CRON_FIRED == before


def test_unacknowledged_selfhost_photon_channel_preserves_all_five_slots(monkeypatch):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host-photon-fixed-five-v1')
    events = []
    monkeypatch.setattr(rec, '_activation_tick', lambda: events.append('activation'))
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: None)
    monkeypatch.setattr(rec, '_background_context_tick', lambda: pytest.fail('background ran'))
    monkeypatch.setattr(rec, '_prepare_briefs', lambda: pytest.fail('brief prepared'))
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda: pytest.fail('cron slot probed'))
    before = dict(rec._CRON_FIRED)
    rec._cron_tick()
    assert events == ['activation']
    assert rec._CRON_FIRED == before


def test_selfhost_photon_profile_cannot_use_managed_mode_route(monkeypatch):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host-photon-fixed-five-v1')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'photon')
    monkeypatch.setattr(rec, '_configured_tz_name', lambda: 'UTC')
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: pytest.fail('Node contacted'))
    assert rec._candidate_retained_route_digest() is None


def test_unproved_telegram_link_preserves_all_five_slots(monkeypatch):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host-telegram-fixed-five-v1')
    events = []
    monkeypatch.setattr(rec, '_activation_tick', lambda: events.append('activation'))
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: None)
    monkeypatch.setattr(rec, '_background_context_tick', lambda: pytest.fail('background ran'))
    monkeypatch.setattr(rec, '_prepare_briefs', lambda: pytest.fail('brief prepared'))
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda: pytest.fail('cron slot probed'))
    before = dict(rec._CRON_FIRED)
    rec._cron_tick()
    rec._cron_tick()
    assert events == ['activation', 'activation']
    assert rec._CRON_FIRED == before


def test_fixed_schedule_authority_is_profile_scoped(monkeypatch):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host-telegram-fixed-five-v1')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'selfhost-tenant')
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: 'a' * 64)
    monkeypatch.setattr(rec, '_configured_tz_name', lambda: 'UTC')
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda: [
        (f'job-{index}', f'{index} * * * *', f'prompt-{index}', f'skill-{index}')
        for index in range(5)])
    telegram = rec._fixed_schedule_authority()
    assert len(telegram) == 64
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'managed-photon-fixed-five-v1')
    assert rec._fixed_schedule_authority() != telegram
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host-photon-fixed-five-v1')
    assert rec._fixed_schedule_authority() not in (telegram,)


@contextmanager
def _transaction(path, default):
    target = Path(path)
    document = json.loads(target.read_text()) if target.exists() else default
    yield document
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document))


def test_queued_telegram_send_rechecks_link_before_contact_then_retries_safely(
        monkeypatch, tmp_path):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host-telegram-fixed-five-v1')
    route = {'current': False}
    contacts = []

    class Runtime:
        def send_receipt(self, body, target, timeout, dispatch_id, attempt):
            contacts.append((body, target, dispatch_id, attempt))
            return True, '', {'acceptance': 'accepted', 'message_id': 'telegram-provider-id'}

    monkeypatch.setattr(rec, '_runtime_adapter', lambda: Runtime())
    monkeypatch.setattr(rec, '_candidate_retained_route_digest',
                        lambda: 'a' * 64 if route['current'] else None)
    box = rec.OUTBOX
    monkeypatch.setitem(box.HOOKS, 'data_root', lambda: str(tmp_path))
    monkeypatch.setitem(box.HOOKS, 'json_transaction', _transaction)
    monkeypatch.setitem(box.HOOKS, 'local_today', lambda: '2026-10-09')
    monkeypatch.setitem(box.HOOKS, 'valid', lambda _payload: True)
    monkeypatch.setitem(box.HOOKS, 'record', lambda *_args, **_kwargs: None)
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda _payload: None)
    monkeypatch.setitem(box.HOOKS, 'send', rec._send_with_identity)
    box._enqueue('stable-telegram', box.KIND_NUDGE,
                 {'label': 'event', 'body': 'owner brief', 'target': 'telegram'},
                 time.time(), '2026-10-09')
    assert box._attempt('stable-telegram') is False
    path = Path(box.path())
    state = json.loads(path.read_text())
    assert contacts == []
    assert state['rows'][0]['acceptance'] == 'not_attempted'
    assert state['rows'][0]['acceptance_uncertain'] is False
    state['rows'][0]['next_at'] = 0
    path.write_text(json.dumps(state))
    route['current'] = True
    assert box._attempt('stable-telegram') is True
    assert contacts == [('owner brief', 'telegram', 'stable-telegram', 2)]
    assert box._attempt('stable-telegram') is False
    assert len(contacts) == 1


def test_telegram_gallery_precheck_is_before_provider_contact(monkeypatch):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host-telegram-fixed-five-v1')
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: None)
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: pytest.fail('provider contacted'))
    assert rec._send_gallery_with_current_route({}, 'telegram', 'stable-gallery')[2] == {
        'acceptance': 'not_attempted'}


def test_selfhost_photon_requires_current_ack_before_queued_send_contact(monkeypatch):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host-photon-fixed-five-v1')
    route = {'current': False}
    contacts = []

    class Runtime:
        def send_receipt(self, body, target, timeout, dispatch_id, attempt):
            contacts.append((body, target, dispatch_id, attempt))
            return True, '', {'acceptance': 'accepted', 'message_id': 'photon-provider-id'}

    monkeypatch.setattr(rec, '_runtime_adapter', lambda: Runtime())
    monkeypatch.setattr(rec, '_candidate_retained_route_digest',
                        lambda: 'b' * 64 if route['current'] else None)
    assert rec._send_with_identity('owner brief', 'photon', 'stable-photon', 1)[2] == {
        'acceptance': 'not_attempted'}
    assert contacts == []
    route['current'] = True
    assert rec._send_with_identity('owner brief', 'photon', 'stable-photon', 1)[2] == {
        'acceptance': 'accepted', 'message_id': 'photon-provider-id'}
    assert contacts == [('owner brief', 'photon', 'stable-photon', 1)]


def test_selfhost_photon_gallery_precheck_is_before_provider_contact(monkeypatch):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host-photon-fixed-five-v1')
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: None)
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: pytest.fail('provider contacted'))
    assert rec._send_gallery_with_current_route({}, 'photon', 'stable-gallery')[2] == {
        'acceptance': 'not_attempted'}
