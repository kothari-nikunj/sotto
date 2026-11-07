"""Accepted source corrections invalidate materialized context through existing authority."""
from types import SimpleNamespace

import pytest
import managed
import receiver
import source_corrections


@pytest.fixture(params=['managed', 'self-hosted'])
def authority_fixture(tmp_path, monkeypatch, request):
    mode = request.param
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', mode)
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_HOME', raising=False)
    monkeypatch.setattr(receiver, 'DATA', str(tmp_path))
    if mode == 'managed':
        managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    state = {'revision': '', 'during_probe': None}
    monkeypatch.setattr(source_corrections, 'revision', lambda data_dir=None: state['revision'])
    def probe(*args, **kwargs):
        if state['during_probe']:
            state['during_probe']()
        return {'result': {'structuredContent': {'connected': True, 'source_policy': {
            'version': 1, 'bridge_revision': 'bridge-unchanged',
            'cloud_revision': 'cloud-unchanged' if mode == 'managed' else None,
            'managed': mode == 'managed', 'allowed_sources': ['imessage']}}}}
    monkeypatch.setattr(receiver, 'RELAY', SimpleNamespace(bridge_connected=lambda: True, mcp_call=probe))
    return state


def test_accepted_correction_changes_runtime_authority_without_consent_change(authority_fixture):
    before = receiver._runtime_source_authority()
    assert before['complete']
    authority_fixture['revision'] = 'a' * 64
    after = receiver._runtime_source_authority()
    assert after['complete'] and after['revision'] != before['revision']
    assert after['bridge_sources'] == before['bridge_sources']


def test_correction_during_authority_probe_refuses_mixed_context(authority_fixture):
    authority_fixture['during_probe'] = lambda: authority_fixture.update(revision='b' * 64)
    assert receiver._runtime_source_authority()['complete'] is False


def test_unreadable_active_correction_receipt_refuses_authority(authority_fixture, monkeypatch):
    def fail(data_dir=None):
        raise ValueError('invalid correction state')
    monkeypatch.setattr(source_corrections, 'revision', fail)
    assert receiver._runtime_source_authority()['complete'] is False


def test_last_authority_check_withholds_fact_materialized_before_correction(authority_fixture, monkeypatch):
    import json
    import runtime_knowledge_read
    captured = receiver._runtime_source_authority()
    materialized = {'person': 'Alex', 'facts': ['The original fact was still present during extraction.']}
    def query(*args, **kwargs):
        authority_fixture['revision'] = 'c' * 64
        return SimpleNamespace(returncode=0, stdout=json.dumps(materialized).encode())
    monkeypatch.setattr(runtime_knowledge_read.subprocess, 'run', query)
    with pytest.raises(runtime_knowledge_read.UnavailableError, match='knowledge authority unavailable'):
        runtime_knowledge_read.perform(
            {'operation': 'knowledge_query', 'arguments': {'argv': []}, 'source_revision': captured['revision']},
            script='offline-synthetic-reader', executable='unused-python', env={},
            authority=receiver._runtime_source_authority)
