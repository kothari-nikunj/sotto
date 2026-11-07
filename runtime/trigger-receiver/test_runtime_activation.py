"""Credential-free activation pull, validation and write-before-ACK recovery."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import managed
import receiver as rec
import runtime_activation as activation


OWNER = '+15555550123'
PROJECT = 'fixture-project'
GUID = 'iMessage;-;+15555550123'


def evidence(photon, **changes):
    value = {
        'version': 1, 'event_id': 'a' * 64, 'provider_event_id': 'provider-event',
        'tenant_id': '', 'project_fingerprint': photon.project_fingerprint(PROJECT),
        'owner_id': OWNER, 'account_id': 'fixture-account', 'line_key': 'fixture-line',
        'provider_thread_id': f'imessage:{GUID}~+15555550999', 'admitted_at': 1_780_000_000_000,
    }
    value.update(changes)
    return value


def configure(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'photon')
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', OWNER)
    monkeypatch.setenv('PHOTON_ALLOWED_USERS', OWNER)
    monkeypatch.setenv('PHOTON_PROJECT_ID', PROJECT)
    monkeypatch.setenv('PHOTON_PROJECT_SECRET', 'fixture-secret')
    monkeypatch.setenv('PHOTON_ALLOW_ALL_USERS', 'false')
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    monkeypatch.delenv('SOTTO_TENANT_ID', raising=False)
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    return rec._sotto_adapter('photon_owner')


def test_helper_decodes_pinned_sdk_guid_then_writes_before_ack(tmp_path, monkeypatch):
    photon = configure(tmp_path, monkeypatch)
    calls = []
    def writer(source):
        calls.append(('write', source.chat_type, source.user_id, source.chat_id))
        return True
    def ack(event_id):
        calls.append(('ack', event_id))
        return True
    assert activation.consume(evidence(photon), tenant_id='', owner_id=OWNER,
                              project_fingerprint=photon.project_fingerprint(PROJECT),
                              writer=writer, ack=ack)
    assert calls == [('write', 'dm', OWNER, GUID), ('ack', 'a' * 64)]


@pytest.mark.parametrize('change', [
    {'version': True}, {'admitted_at': -1}, {'event_id': 'bad'},
    {'owner_id': '+15555550999'}, {'tenant_id': 'foreign'},
    {'project_fingerprint': 'b' * 64}, {'account_id': ''},
    {'provider_thread_id': 'telegram:other'}, {'provider_thread_id': 'imessage:'},
    {'provider_thread_id': 'imessage:guid~'}, {'provider_thread_id': 'imessage:guid~one~two'},
    {'unexpected': 'extra'},
])
def test_invalid_evidence_never_writes_or_acks(tmp_path, monkeypatch, change):
    photon = configure(tmp_path, monkeypatch)
    calls = []
    with pytest.raises(ValueError):
        activation.consume(evidence(photon, **change), tenant_id='', owner_id=OWNER,
                           project_fingerprint=photon.project_fingerprint(PROJECT),
                           writer=lambda _: calls.append('write'), ack=lambda _: calls.append('ack'))
    assert calls == []


def test_failed_writer_or_ack_never_turns_an_unproven_result_into_success(tmp_path, monkeypatch):
    photon = configure(tmp_path, monkeypatch)
    calls = []
    kwargs = {'tenant_id': '', 'owner_id': OWNER,
              'project_fingerprint': photon.project_fingerprint(PROJECT)}
    assert activation.consume(evidence(photon), **kwargs,
                              writer=lambda _: False,
                              ack=lambda _: calls.append('ack')) is False
    assert calls == []
    with pytest.raises(TimeoutError):
        activation.consume(evidence(photon), **kwargs,
                           writer=lambda _: True,
                           ack=lambda _: (_ for _ in ()).throw(TimeoutError()))


def test_receiver_retries_lost_ack_without_rewriting_receipt(tmp_path, monkeypatch):
    photon = configure(tmp_path, monkeypatch)
    pending = evidence(photon)
    class Runtime:
        attempts = 0
        def activation_evidence(self, timeout):
            assert timeout == 3
            return pending
        def activation_ack(self, event_id, timeout):
            assert event_id == pending['event_id'] and timeout == 3
            self.attempts += 1
            if self.attempts == 1:
                raise TimeoutError('lost acknowledgement')
            return True
    runtime = Runtime()
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: runtime)
    rec._activation_tick()
    receipt = Path(tmp_path) / 'config/photon-activation.json'
    first = receipt.read_bytes()
    assert json.loads(first)['chat_id'] == GUID
    assert rec._photon_status() == 'linked'
    rec._activation_tick()
    assert receipt.read_bytes() == first
    assert runtime.attempts == 2


def test_current_photon_route_uses_private_modern_receipt_and_current_policy(tmp_path, monkeypatch):
    photon = configure(tmp_path, monkeypatch)
    assert photon.record_owner_activation(
        SimpleNamespace(chat_type='dm', user_id=OWNER, chat_id=GUID))
    config = tmp_path / 'config'
    assert config.stat().st_mode & 0o022 == 0  # existing 0755 is allowed
    route = activation.verified_current_photon_route(tmp_path, os.environ)
    assert route == {'platform': 'imessage', 'chat_id': GUID, 'thread_id': None}
    with pytest.raises(ValueError, match='absolute'):
        activation.verified_current_photon_route('relative-root', os.environ)

    receipt = config / 'photon-activation.json'
    original = json.loads(receipt.read_text())
    for change in ({'owner': '+15555550999'}, {'tenant_id': 'other'},
                   {'project_fingerprint': '0' * 64},
                   {'project_fingerprint': 'é' * 64}, {'activated': 1},
                   {'extra': 'unknown'}):
        receipt.write_text(json.dumps({**original, **change}))
        with pytest.raises(ValueError):
            activation.verified_current_photon_route(tmp_path, os.environ)
    receipt.write_text(json.dumps(original))
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'rotated-project')
    with pytest.raises(ValueError):
        activation.verified_current_photon_route(tmp_path, os.environ)
    monkeypatch.setenv('PHOTON_PROJECT_ID', PROJECT)
    monkeypatch.setenv('PHOTON_ALLOWED_USERS', '+15555550999')
    with pytest.raises(ValueError):
        activation.verified_current_photon_route(tmp_path, os.environ)


def test_current_photon_route_holds_legacy_corrupt_or_redirected_receipt(tmp_path, monkeypatch):
    photon = configure(tmp_path, monkeypatch)
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'tenant-a')
    assert photon.record_owner_activation(
        SimpleNamespace(chat_type='dm', user_id=OWNER, chat_id=GUID))
    receipt = tmp_path / 'config' / 'photon-activation.json'
    modern = json.loads(receipt.read_text())
    receipt.write_text(json.dumps({key: value for key, value in modern.items()
                                   if key != 'project_fingerprint'}))
    with pytest.raises(ValueError):
        activation.verified_current_photon_route(tmp_path, os.environ)
    receipt.write_text('{partial')
    with pytest.raises(ValueError):
        activation.verified_current_photon_route(tmp_path, os.environ)
    receipt.write_text(json.dumps(modern))
    receipt.chmod(0o644)
    with pytest.raises(ValueError):
        activation.verified_current_photon_route(tmp_path, os.environ)
    receipt.unlink()
    receipt.symlink_to(tmp_path / 'outside')
    with pytest.raises(ValueError):
        activation.verified_current_photon_route(tmp_path, os.environ)


def test_current_photon_route_detects_atomic_receipt_replacement(tmp_path, monkeypatch):
    photon = configure(tmp_path, monkeypatch)
    assert photon.record_owner_activation(
        SimpleNamespace(chat_type='dm', user_id=OWNER, chat_id=GUID))
    receipt = tmp_path / 'config/photon-activation.json'
    read = activation.os.read

    def replace_after_read(fd, size):
        content = read(fd, size)
        replacement = receipt.with_suffix('.next')
        replacement.write_bytes(content)
        replacement.chmod(0o600)
        replacement.replace(receipt)
        return content

    monkeypatch.setattr(activation.os, 'read', replace_after_read)
    with pytest.raises(ValueError, match='changed'):
        activation.verified_current_photon_route(tmp_path, os.environ)


def test_non_photon_or_unconfigured_photon_never_polls(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: (_ for _ in ()).throw(AssertionError('polled')))
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'telegram')
    rec._activation_tick()
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'photon')
    monkeypatch.setenv('PHOTON_ALLOW_ALL_USERS', 'true')
    rec._activation_tick()
    monkeypatch.setenv('PHOTON_ALLOW_ALL_USERS', 'false')
    monkeypatch.delenv('PHOTON_PROJECT_SECRET')
    rec._activation_tick()
    assert not (Path(tmp_path) / 'config/photon-activation.json').exists()


def test_receiver_rejects_foreign_project_before_writer_or_ack(tmp_path, monkeypatch):
    photon = configure(tmp_path, monkeypatch)
    class Runtime:
        def activation_evidence(self, timeout):
            return evidence(photon, project_fingerprint='b' * 64)
        def activation_ack(self, event_id, timeout):
            raise AssertionError('invalid evidence was acknowledged')
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: Runtime())
    rec._activation_tick()
    assert not (Path(tmp_path) / 'config/photon-activation.json').exists()


def test_managed_first_brief_still_requires_source_after_activation(tmp_path, monkeypatch):
    photon = configure(tmp_path, monkeypatch)
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'tenant-a')
    pending = evidence(photon, tenant_id='tenant-a')
    assert managed.brief_hold(tmp_path) == 'Text Sotto once to activate iMessage delivery'
    assert activation.consume(pending, tenant_id='tenant-a', owner_id=OWNER,
                              project_fingerprint=photon.project_fingerprint(PROJECT),
                              writer=photon.record_owner_activation, ack=lambda _: True)
    assert managed.brief_hold(tmp_path) == 'Connect a context source to receive scheduled briefs'
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    assert managed.brief_hold(tmp_path) is None


def test_existing_receiver_tick_pulls_before_background_gate(monkeypatch):
    order = []
    monkeypatch.setattr(rec, '_activation_tick', lambda: order.append('activation'))
    monkeypatch.setattr(rec, '_background_context_tick', lambda: order.append('first-brief'))
    monkeypatch.setattr(rec, '_prepare_briefs', lambda: None)
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda: [])
    monkeypatch.setattr(managed, 'notice_pending', lambda _: False)
    rec._cron_tick()
    assert order == ['activation', 'first-brief']
