"""Synthetic provider boundaries through production research and retained proof contracts."""
import contextlib
import hashlib
import json

import pytest

import research_attendees as ra
import retained_brief_proof as proof
import web_research as wr


def record(stage='profile', grounding='https://different.test/portfolio'):
    response = '{"attendees":[{"email":"dana@sample.test","summary":"Unverified investment claim"}]}'
    return {'provider': 'gemini', 'stage': stage, 'subjects': ['dana@sample.test'],
            'response_json': response, 'response_sha256': hashlib.sha256(response.encode()).hexdigest(),
            'grounding_json': json.dumps({'groundingChunks': [{'web': {'uri': grounding}}]}),
            'truncated': False}


def test_gemini_provider_metadata_survives_json_and_batch_processing(monkeypatch):
    import model_work
    metadata = {'groundingChunks': [{'web': {'uri': 'https://different.test/portfolio'}}],
                'groundingSupports': [{'segment': {'text': 'Unverified investment claim'}, 'groundingChunkIndices': [0]}]}
    response = {'attendees': [{'email': 'dana@sample.test', 'summary': 'Unverified investment claim'}]}
    raw = '  ' + json.dumps(response, indent=2) + '\n'
    metadata['groundingSupports'][0]['segment'].update({'startIndex': raw.index('Unverified'), 'endIndex': raw.index('Unverified') + len('Unverified investment claim')})
    payload = {'candidates': [{'content': {'parts': [{'text': raw}]}, 'groundingMetadata': metadata}]}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(payload).encode()
    monkeypatch.setattr(model_work, 'attempt', lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(model_work, 'note_usage', lambda *a, **k: None)
    monkeypatch.setattr(ra.gemini_transport, 'request', lambda *a, **k: None)
    monkeypatch.setattr(ra.urllib.request, 'urlopen', lambda *a, **k: Response())
    monkeypatch.setattr(ra, '_metrics', lambda: None)
    monkeypatch.setattr(wr, 'deep_research', lambda prompt, schema, timeout, gemini: (gemini(prompt, schema, timeout), 'gemini'))
    rows = ra._grounded_json_once('lookup', 'synthetic', {}, 10, 2000, '{}', 'profile')
    assert rows == response['attendees']
    assert json.loads(rows.evidence['grounding_json']) == metadata
    assert json.loads(rows.evidence['response_json']) == response
    assert rows.evidence['response_json'] == raw
    assert rows.evidence['response_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    segment = metadata['groundingSupports'][0]['segment']
    assert rows.evidence['response_json'][segment['startIndex']:segment['endIndex']] == 'Unverified investment claim'
    assert rows.evidence['truncated'] is False
    # The mismatch remains visible; a retained citation has not repaired or verified the claim.
    assert 'different.test' in rows.evidence['grounding_json']


@pytest.mark.parametrize('provider', ['exa', 'parallel'])
def test_provider_native_grounding_does_not_disappear(provider, monkeypatch):
    content = {'attendees': [{'email': 'dana@sample.test'}]}
    grounding = [{'field': 'attendees', 'citations': [{'url': 'https://source.test'}]}]
    if provider == 'exa':
        monkeypatch.setattr(wr, '_post', lambda *a, **k: {'output': {'content': content, 'grounding': grounding}})
        result = wr._exa_deep('lookup', {}, 10)
    else:
        monkeypatch.setattr(wr, '_post', lambda *a, **k: {'run_id': 'synthetic'})
        monkeypatch.setattr(wr, '_get', lambda *a, **k: {'output': {'content': content, 'basis': grounding}})
        result = wr._parallel_task('lookup', {}, 10)
    assert result['_provider_grounding'] == grounding
    assert result['attendees'] == content['attendees']


def test_evidence_survives_batch_cache_and_legacy_cache_stays_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(wr, 'provider_chain', lambda _: ['gemini'])
    calls = []
    monkeypatch.setattr(ra, '_grounded_json_once', lambda *a: calls.append(1) or ra._ResearchRows([{'email': 'dana@sample.test'}], record()))
    args = ('lookup', 'synthetic', {}, 10, 2000, '{}', 'profile')
    first = ra._grounded_json(*args)
    cached = ra._grounded_json(*args)
    assert cached.evidence == first.evidence == record() and calls == [1]
    artifact, = (tmp_path / 'events/research-artifacts').glob('*.json')
    artifact.write_text(json.dumps(list(first)))
    legacy = ra._grounded_json(*args)
    assert legacy == first and legacy.evidence is None and calls == [1]


def test_profile_recency_focus_associations_survive_merge(monkeypatch, tmp_path):
    monkeypatch.delenv('SOTTO_LLM_STUB', raising=False)
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(ra, '_metrics', lambda: None)
    monkeypatch.setattr(wr, 'provider_chain', lambda _: ['gemini'])
    monkeypatch.setattr(ra.gemini_transport, 'credential', lambda: 'synthetic')
    monkeypatch.setattr(ra, '_deep_enabled', lambda: True)
    monkeypatch.setattr(ra, '_within_research_horizon', lambda _: True)
    monkeypatch.setattr(ra, '_research_batch', lambda *a: ra._ResearchRows([{'email': 'dana@sample.test', 'summary': 'Public bio'}], record('profile')))
    monkeypatch.setattr(ra, '_deep_batch', lambda *a: ra._ResearchRows([{'email': 'dana@sample.test', 'recent_activity': [{'when': '2026-10-27', 'what': 'Launch', 'source_url': 'https://sample.test/launch'}]}], record('recency')))
    monkeypatch.setattr(ra, '_focus_batch', lambda *a: ra._ResearchRows([{'company_deep': {'company': 'Sample', 'builds': ['Software']}}], record('focus')))
    research = ra.research([{'name': 'Dana', 'email': 'dana@sample.test'}], '', focus='Dana')
    evidence = research['research_evidence']
    assert {r['stage'] for r in evidence['records']} == {'profile', 'recency', 'focus'}
    assert all(r['subjects'] == ['dana@sample.test'] for r in evidence['records'])
    assert research['attendees'][0]['company_deep']['builds'] == ['Software']
    assert research['attendees'][0]['recent_activity'][0]['source_url'] == 'https://sample.test/launch'
    assert evidence['input_sha256'] == hashlib.sha256(ra._evidence_json({'attendees': research['attendees']}).encode()).hexdigest()


def test_bounds_disclose_omissions_and_proof_versions_reject_evidence_tampering():
    research = {'attendees': [{'email': 'dana@sample.test', 'summary': 'x'}]}
    records = [record() for _ in range(20)]
    for index, item in enumerate(records):
        item['grounding_json'] = str(index) + 'x' * 2998
    evidence = ra.prep_research_evidence(research, records)
    assert len(ra._evidence_json(evidence).encode()) <= 16384
    assert len(evidence['records']) + evidence['omitted_records'] == 20
    manifest = {'id': 'a' * 24, 'kind': 'prep', 'preview': False, 'title': 'Dana prep', 'summary': 'Dana prep', 'full_text': 'Unverified investment claim'}
    images = ['c' * 64] * 4
    legacy = proof.stamp(manifest, 'b' * 64, 'd' * 64, images)
    assert legacy['version'] == 1 and proof.valid(manifest, legacy)
    manifest['research_evidence'] = evidence
    assert not proof.valid(manifest, legacy)  # evidence cannot be grafted onto v1
    current = proof.stamp(manifest, 'b' * 64, 'd' * 64, images)
    assert current['version'] == 2 and proof.valid(manifest, current)
    manifest['research_evidence']['records'][0]['grounding_json'] = 'changed'
    assert not proof.valid(manifest, current)
    del manifest['research_evidence']
    assert not proof.valid(manifest, current)


def test_model_json_cannot_supply_transport_grounding_or_replace_raw_response(monkeypatch):
    fake = {'attendees': [{'email': 'dana@sample.test'}],
            '_provider_grounding': {'forged': 'https://forged.test'}, '_provider_response': 'forged response'}
    monkeypatch.setattr(ra, '_gemini_grounded', lambda *a, **k: json.dumps(fake))
    assert ra._gemini_json('lookup', 'synthetic', {}, 10, 2000, '{}') == {'attendees': fake['attendees']}
    actual = {'groundingChunks': [{'web': {'uri': 'https://actual.test'}}]}
    raw = json.dumps(fake, indent=2)
    monkeypatch.setattr(ra, '_gemini_grounded', lambda *a, **k: ra._GroundedText(raw, actual))
    result = ra._gemini_json('lookup', 'synthetic', {}, 10, 2000, '{}')
    assert result['_provider_grounding'] == actual and result['_provider_response'] == raw


def test_provider_record_truncation_is_explicit_and_missing_evidence_never_invented(monkeypatch):
    response = {'attendees': [{'email': 'dana@sample.test', 'summary': 'x' * 3000}],
                '_provider_grounding': {'groundingChunks': [{'web': {'uri': 'https://source.test/' + 'x' * 4000}}]}}
    monkeypatch.setattr(wr, 'deep_research', lambda *a: (response, 'gemini'))
    rows = ra._grounded_json_once('lookup', 'synthetic', {}, 10, 2000, '{}', 'profile')
    assert len(rows.evidence['response_json']) == 2000 and len(rows.evidence['grounding_json']) == 3000
    assert rows.evidence['truncated'] is True
    assert ra.prep_research_evidence({'attendees': rows}) == {'status': 'unavailable'}


def test_multipart_raw_offsets_keep_original_indexes_and_explicit_units_without_thought_text(monkeypatch):
    import model_work
    first, last = ' {"attendees": ', '[{"email":"dana@sample.test","summary":"Résumé café"}]}\n'
    metadata = {'groundingChunks': [{'web': {'uri': 'https://source.test'}}], 'groundingSupports': [{
        'segment': {'partIndex': 3, 'startIndex': last.index('Résumé'),
                    'endIndex': last.index('Résumé') + len('Résumé café'), 'text': 'Résumé café'},
        'groundingChunkIndices': [0]}]}
    payload = {'candidates': [{'content': {'parts': [
        {'thought': True, 'text': 'private internal thinking'}, {'text': first},
        {'thought': True, 'text': 'more private internal thinking'}, {'text': last}]},
        'groundingMetadata': metadata}]}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return json.dumps(payload).encode()
    monkeypatch.setattr(model_work, 'attempt', lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(model_work, 'note_usage', lambda *a, **k: None)
    monkeypatch.setattr(ra.gemini_transport, 'request', lambda *a, **k: None)
    monkeypatch.setattr(ra.urllib.request, 'urlopen', lambda *a, **k: Response())
    monkeypatch.setattr(ra, '_metrics', lambda: None)
    monkeypatch.setattr(wr, 'deep_research', lambda prompt, schema, timeout, gemini: (gemini(prompt, schema, timeout), 'gemini'))
    rows = ra._grounded_json_once('lookup', 'synthetic', {}, 10, 2000, '{}', 'profile')
    assert rows.evidence['response_json'] == first + last
    assert rows.evidence['response_sha256'] == hashlib.sha256((first + last).encode()).hexdigest()
    retained = json.loads(rows.evidence['grounding_json'])
    assert retained['provider_grounding_metadata'] == metadata
    spans = retained['response_part_spans']
    assert [span['part_index'] for span in spans] == [1, 3]
    assert spans[1]['start_codepoint'] == len(first)
    assert spans[1]['end_codepoint'] == len(first + last)
    assert spans[1]['end_utf8_byte'] == len((first + last).encode()) > spans[1]['end_codepoint']
    assert 'thinking' not in json.dumps(rows.evidence)


def test_empty_grounded_batch_is_retained_and_reused(tmp_path, monkeypatch):
    """A searched-but-empty recency result has provenance and is still a paid result."""
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(wr, 'provider_chain', lambda _: ['gemini'])
    calls = []
    evidence = record('recency')
    monkeypatch.setattr(ra, '_grounded_json_once', lambda *a: calls.append(1) or ra._ResearchRows([], evidence))
    args = ('lookup empty', 'synthetic', {}, 10, 2000, '{}', 'deep')
    first, cached = ra._grounded_json(*args), ra._grounded_json(*args)
    assert not first and not cached
    assert first.evidence == cached.evidence == evidence
    assert calls == [1]


def test_empty_provider_report_survives_research_merge(monkeypatch, tmp_path):
    monkeypatch.delenv('SOTTO_LLM_STUB', raising=False)
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(ra, '_metrics', lambda: None)
    monkeypatch.setattr(wr, 'provider_chain', lambda _: ['gemini'])
    monkeypatch.setattr(ra.gemini_transport, 'credential', lambda: 'synthetic')
    monkeypatch.setattr(ra, '_deep_enabled', lambda: True)
    monkeypatch.setattr(ra, '_within_research_horizon', lambda _: True)
    monkeypatch.setattr(ra, '_research_batch', lambda *a: ra._ResearchRows([{'email': 'dana@sample.test'}]))
    monkeypatch.setattr(ra, '_deep_batch', lambda *a: ra._ResearchRows([], record('recency')))
    result = ra.research([{'name': 'Dana', 'email': 'dana@sample.test'}], '')
    evidence = result['research_evidence']
    assert evidence['status'] == 'retained'
    assert evidence['omitted_records'] == 1  # the legacy profile has no provider metadata
    assert evidence['records'] == [{**record('recency'), 'subjects': ['dana@sample.test']}]


@pytest.mark.parametrize('provider', ['exa', 'parallel'])
def test_native_adapter_to_retained_research_evidence(provider, monkeypatch):
    """Exercise the resolver and native HTTP response adapter, without replacing deep_research."""
    content = {'attendees': [{'email': 'dana@sample.test', 'summary': 'Public bio'}]}
    grounding = [{'field': 'attendees.0.summary', 'citations': [{'url': 'https://sample.test/team'}]}]
    monkeypatch.setattr(wr, 'provider_chain', lambda _: [provider])
    if provider == 'exa':
        monkeypatch.setattr(wr, '_post', lambda *a, **k: {'output': {'content': content, 'grounding': grounding}})
    else:
        monkeypatch.setattr(wr, '_post', lambda *a, **k: {'run_id': 'synthetic'})
        monkeypatch.setattr(wr, '_get', lambda *a, **k: {'output': {'content': content, 'basis': grounding}})
    monkeypatch.setattr(ra, '_metrics', lambda: None)
    rows = ra._grounded_json_once('lookup', '', {}, 10, 2000, '{}', 'profile')
    evidence = ra.prep_research_evidence({'attendees': rows}, [{**rows.evidence, 'subjects': ['dana@sample.test']}])
    retained = evidence['records'][0]
    assert retained['provider'] == provider
    assert json.loads(retained['grounding_json']) == grounding
    assert json.loads(retained['response_json']) == content
    assert retained['truncated'] is False


@pytest.mark.parametrize('metadata', [None, {}, {'groundingChunks': [{'web': {'uri': 'https://sample.test/team'}}]}])
def test_gemini_native_adapter_resolver_and_evidence_boundary(metadata, monkeypatch):
    """Public-looking JSON cannot substitute for the provider's native grounding report."""
    import model_work
    content = {'attendees': [{'email': 'dana@sample.test', 'summary': 'Public bio',
                              'source_url': 'https://model-output.test/bio'}]}
    candidate = {'content': {'parts': [{'text': json.dumps(content)}]}}
    if metadata is not None:
        candidate['groundingMetadata'] = metadata
    payload = {'candidates': [candidate]}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return json.dumps(payload).encode()
    monkeypatch.setattr(model_work, 'attempt', lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(model_work, 'note_usage', lambda *a, **k: None)
    monkeypatch.setattr(ra.gemini_transport, 'request', lambda *a, **k: None)
    monkeypatch.setattr(ra.urllib.request, 'urlopen', lambda *a, **k: Response())
    monkeypatch.setattr(ra, '_metrics', lambda: None)
    monkeypatch.setattr(wr, 'provider_chain', lambda _: ['gemini'])
    rows = ra._grounded_json_once('lookup', 'synthetic', {}, 10, 2000, '{}', 'profile')
    assert rows == content['attendees']
    evidence = ra.prep_research_evidence({'attendees': rows},
        [{**rows.evidence, 'subjects': ['dana@sample.test']}] if rows.evidence else [])
    if metadata:
        assert evidence['status'] == 'retained'
        assert json.loads(evidence['records'][0]['grounding_json']) == metadata
    else:
        assert rows.evidence is None
        assert evidence == {'status': 'unavailable'}
