"""Diagnostic projection reveals finite metadata and preserves phase/tool boundaries."""
import importlib.util
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import google_read_contract as contract


@pytest.mark.parametrize('status', [None, True, 99, 600, '404', [], {}])
def test_receiver_drops_invalid_status_and_private_details(status):
    error = SimpleNamespace(sotto_read_stage='provider_forbidden', sotto_read_http_status=status,
                            sotto_read_phase='message_get', private='private query')
    assert contract.receiver_failure(error, 'reader_call', 'gmail_search') == {
        'error': 'Google read unavailable', 'read_stage': 'provider_forbidden'}


@pytest.mark.parametrize('operation,phase', [('gmail_search', 'search_list'),
    ('gmail_search', 'search_metadata'), ('gmail_get', 'message_get')])
def test_receiver_preserves_supported_phase_and_status(operation, phase):
    error = SimpleNamespace(sotto_read_http_status=404, sotto_read_phase=phase)
    assert contract.receiver_failure(error, 'reader_call', operation) == {
        'error': 'Google read unavailable', 'read_stage': 'reader_call',
        'read_http_status': 404, 'read_phase': phase}
    assert contract.receiver_failure(error, 'reader_call', 'gmail_thread_get') == {
        'error': 'Google read unavailable', 'read_stage': 'reader_call', 'read_http_status': 404}


@pytest.mark.parametrize('stage', ['receiver_transport', 'result_schema', 'target_contract',
                                  'execution_context', 'private source text', None, [], {}])
def test_receiver_narrower_vocabulary_withholds_transport_and_untrusted_categories(stage):
    assert contract.receiver_failure(SimpleNamespace(sotto_read_stage=stage), 'reader_load', 'gmail_get') == {
        'error': 'Google read unavailable', 'read_stage': 'unknown'}


@pytest.mark.parametrize('layout,subtree', [('source', 'src'), ('standalone', 'src'), ('image', 'dist')])
def test_contract_loads_only_immutable_code_in_each_layout(tmp_path, monkeypatch, layout, subtree):
    root = tmp_path / layout
    loader = root / 'adapters/sotto/google_read_contract.py'
    loader.parent.mkdir(parents=True)
    shutil.copyfile(contract.__file__, loader)
    target = root / f'runtime/sotto-chat/{subtree}/tools/google-read-contract.json'
    target.parent.mkdir(parents=True)
    source = Path(contract.__file__).resolve().parents[2] / 'runtime/sotto-chat/src/tools/google-read-contract.json'
    shutil.copyfile(source, target)
    private = tmp_path / 'private-runtime'
    private.mkdir()
    (private / 'google-read-contract.json').write_text(source.read_text())
    monkeypatch.setenv('SOTTO_DATA', str(private))
    spec = importlib.util.spec_from_file_location('isolated_google_contract', loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.receiver_failure(SimpleNamespace(sotto_read_stage='provider_forbidden'), 'reader_call', 'gmail_get') == {
        'error': 'Google read unavailable', 'read_stage': 'provider_forbidden'}
    target.unlink()
    with pytest.raises(StopIteration):
        spec.loader.exec_module(module)
