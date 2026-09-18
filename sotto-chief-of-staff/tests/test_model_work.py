import importlib
import io
import json
import sqlite3
from urllib.error import HTTPError

import pytest

import gemini
import model_work
import usage_accounting as usage


def test_native_cache_and_thinking_are_priced_once():
    counts = usage.normalize({'promptTokenCount': 1000, 'cachedContentTokenCount': 800,
                              'candidatesTokenCount': 100, 'thoughtsTokenCount': 200})
    assert counts['billed_output_tokens'] == 300
    assert usage.estimate('gemini-3.8-flash', counts, 1789680000) == pytest.approx(.001335)
    chat = usage.normalize({'prompt_tokens': 1000, 'prompt_tokens_details': {'cached_tokens': 800},
                            'completion_tokens': 300, 'completion_tokens_details': {'reasoning_tokens': 200}})
    assert chat == counts


def test_missing_metadata_and_expired_prices_are_unknown():
    assert usage.estimate('gemini-3.8-flash', usage.normalize({})) is None
    counts = usage.normalize({'prompt_tokens': 20, 'completion_tokens': 10})
    assert counts['cached_input_tokens'] is None
    assert counts['visible_output_tokens'] is None
    assert usage.estimate('gemini-3.8-flash', counts) is None
    complete = usage.normalize({'promptTokenCount': 20, 'candidatesTokenCount': 10})
    assert usage.estimate('gemini-3.8-flash', complete, usage.INTRO_END) is None


def test_attempts_survive_reload_and_changed_evidence_can_run(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    sent = []
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: sent.append(1) or '{}')
    for _ in range(2):
        with model_work.scope('notification', ['same']):
            gemini.model_once('gemini', 'gemini-3.8-flash', 'key', 'private')
    importlib.reload(model_work)
    with model_work.scope('notification', ['same']), pytest.raises(model_work.ModelWorkHeldError):
        gemini.model_once('gemini', 'gemini-3.8-flash', 'key', 'private')
    with model_work.scope('notification', ['changed']):
        gemini.model_once('gemini', 'gemini-3.8-flash', 'key', 'private')
    assert len(sent) == 3
    assert b'private' not in (tmp_path / 'events/model-work.sqlite3').read_bytes()


def test_invalid_request_parks_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    def reject(*a, **kw):
        raise HTTPError('https://provider', 400, 'bad shape', {}, io.BytesIO())
    monkeypatch.setattr(gemini, '_gemini_once', reject)
    with model_work.scope('memory_extract', 'page'), pytest.raises(HTTPError):
        gemini.model_once('gemini', 'gemini-3.8-flash', 'key', 'prompt')
    with model_work.scope('memory_extract', 'page'), pytest.raises(model_work.ModelWorkHeldError):
        gemini.model_once('gemini', 'gemini-3.8-flash', 'key', 'prompt')


def test_model_request_policy_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'test')
    seen = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({'candidates': [{'content': {'parts': [{'text': '{}'}]}}]}).encode()
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', lambda req, **kw: seen.append(req) or Response())
    with model_work.scope('notification', 'bundle'):
        gemini.model_once('gemini', 'gemini-3.8-flash', 'test', 'private')
    request = seen[0]
    config = json.loads(request.data)['generationConfig']
    assert config['maxOutputTokens'] == 4096
    assert config['thinkingConfig'] == {'thinkingLevel': 'low'}
    assert request.get_header('X-sotto-workload') == 'notification'
    assert request.get_header('X-sotto-attempt') == '1'
    with sqlite3.connect(tmp_path / 'events/model-work.sqlite3') as db:
        assert db.execute('SELECT attempts FROM operations').fetchone()[0] == 1


def test_concurrent_retry_claims_never_exceed_operation_policy(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    def run(_):
        try:
            with model_work.scope('notification', 'same'), model_work.attempt('gemini', 'model', 'body'):
                return 1
        except model_work.ModelWorkHeldError:
            return 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(run, range(6))) == 2
    with sqlite3.connect(tmp_path / 'events/model-work.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 2


def test_malformed_usage_cannot_break_successful_provider_response():
    counts = usage.normalize({'prompt_tokens': 20, 'completion_tokens': 10,
                              'prompt_tokens_details': 42, 'completion_tokens_details': ['bad']})
    assert counts['usage_completeness'] == 'partial'
    assert usage.estimate('gemini-3.8-flash', counts) is None


def test_failed_request_shape_stays_parked_after_another_shape_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    with model_work.scope('brief', 'inputs'):
        with pytest.raises(HTTPError), model_work.attempt('gemini', 'model', 'prompt', schema={'v': 1}):
            raise HTTPError('https://provider', 400, 'bad shape', {}, io.BytesIO())
        with model_work.attempt('gemini', 'model', 'prompt', schema={'v': 2}):
            pass
        with pytest.raises(model_work.ModelWorkHeldError), model_work.attempt('gemini', 'model', 'prompt', schema={'v': 1}):
            pytest.fail('parked shape dispatched again')


def test_client_repair_and_route_change_release_exhausted_work(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    for _ in range(2):
        with model_work.scope('memory_extract', 'unchanged page'), model_work.attempt('gemini', 'model', 'prompt'):
            pass
    with model_work.scope('memory_extract', 'unchanged page'), pytest.raises(model_work.ModelWorkHeldError):
        with model_work.attempt('gemini', 'model', 'prompt'):
            pytest.fail('exhausted operation dispatched')
    monkeypatch.setattr(model_work, 'implementation_revision', lambda: 'repaired-client')
    with model_work.scope('memory_extract', 'unchanged page') as changed, model_work.attempt('gemini', 'model', 'prompt'):
        first = changed['id']
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://repaired-route.example')
    with model_work.scope('memory_extract', 'unchanged page') as changed, model_work.attempt('gemini', 'model', 'prompt'):
        assert changed['id'] != first


def test_brief_preserves_large_output_and_stage_retries(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'test')
    monkeypatch.delenv('SOTTO_LLM_STUB', raising=False)
    monkeypatch.setattr(gemini, '_PRIMARY_RETRY_BACKOFF_S', 0)
    sent = []
    def respond(*args, **kwargs):
        sent.append(model_work.current()['task'])
        if sent.count(sent[-1]) <= 2:
            raise HTTPError('https://provider', 429, 'busy', {}, io.BytesIO())
        return '{}'
    monkeypatch.setattr(gemini, '_gemini_once', respond)
    with model_work.scope('brief', '170k input / 9k visible + 24k thinking'):
        for stage in ('extract', 'critic', 'revise'):
            with model_work.brief_stage(stage):
                config = model_work.generation_config('gemini-3.8-flash')
                assert config['maxOutputTokens'] >= 9000 + 24000
                assert config['maxOutputTokens'] == 65536 and 'thinkingConfig' not in config
                assert gemini.call_gemini('evidence', {}) == '{}'
    assert len(sent) == 9


def test_new_job_can_repeat_success_but_queue_retry_keeps_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    def llm(*args, **kwargs):
        with model_work.attempt('gemini', 'model', 'same candidate page'):
            return '{}'
    for job in ('first', 'second', 'third'):
        monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', job)
        assert model_work.call('memory_curate', 'same', {}, llm=llm) == '{}'
    assert model_work.call('memory_curate', 'same', {}, llm=llm) == '{}'
    with pytest.raises(model_work.ModelWorkHeldError):
        model_work.call('memory_curate', 'same', {}, llm=llm)


def test_two_killed_workers_can_recover_without_erasing_unknown_spend(tmp_path, monkeypatch):
    import subprocess
    import sys
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    script = """
import os, sys
sys.path.insert(0, sys.argv[1])
import model_work
with model_work.scope('notification', 'crash'), model_work.attempt('gemini', 'model', 'body'):
    os._exit(1)
"""
    from pathlib import Path
    for _ in range(2):
        assert subprocess.run([sys.executable, '-c', script, str(Path(model_work.__file__).parent)]).returncode == 1
    with model_work.scope('notification', 'crash'), model_work.attempt('gemini', 'model', 'body'):
        pass
    with sqlite3.connect(tmp_path / 'events/model-work.sqlite3') as db:
        statuses = [r[0] for r in db.execute('SELECT status FROM attempts ORDER BY attempt')]
    assert statuses == ['interrupted_unknown', 'interrupted_unknown', 'succeeded']


def test_recovery_is_bounded_and_live_claims_are_not_refunded(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    clock = [10000]
    monkeypatch.setattr(model_work.time, 'time', lambda: clock[0])
    for _ in range(4):
        with model_work.scope('notification', 'crash'), pytest.raises(KeyboardInterrupt):
            with model_work.attempt('gemini', 'model', 'body'):
                raise KeyboardInterrupt
    with model_work.scope('notification', 'crash'), pytest.raises(model_work.ModelWorkHeldError):
        with model_work.attempt('gemini', 'model', 'body'):
            pytest.fail('unbounded recovery')


def test_cache_unknown_cost_is_a_range_and_blocked_native_usage_is_retained():
    counts = usage.normalize({'prompt_tokens': 1000, 'completion_tokens': 300})
    bounds = usage.estimate_range('gemini-3.8-flash', counts, 1789680000)
    assert bounds == {'lower': .0012, 'upper': .001875, 'basis': 'cache_unknown'}
    blocked = usage.normalize({'promptTokenCount': 1000, 'totalTokenCount': 1000})
    assert blocked['billed_output_tokens'] == 0
    assert usage.estimate('gemini-3.8-flash', blocked, 1789680000) == .00075
    assert usage.estimate_range('unknown', counts, 1789680000) is None
    assert usage.estimate_range('gemini-3.8-flash', counts, usage.INTRO_END) is None


def test_thinking_capabilities_are_deliberate_subset_of_pricing():
    assert model_work.LOW_THINKING_MODELS <= usage.PRICES.keys()
    assert model_work.LOW_THINKING_MODELS <= gemini.CONTEXT_WINDOWS.keys()
    import ast
    from pathlib import Path
    proxy = ast.parse((Path(__file__).resolve().parents[2] / 'cloud/model-proxy/server.py').read_text())
    supported = next(ast.literal_eval(n.value) for n in proxy.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'MODELS' for t in n.targets))
    assert supported <= model_work.LOW_THINKING_MODELS
    with model_work.scope('notification', 'test'):
        assert 'thinkingConfig' not in model_work.generation_config('gemini-3.6-flash')


def test_bad_brief_contract_stays_parked_in_later_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    for index in range(2):
        monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', f'job-{index}')
        with model_work.scope('brief', 'same sources', occurrence=True), model_work.brief_stage('extract'):
            with pytest.raises(HTTPError if index == 0 else model_work.ModelWorkHeldError):
                with model_work.attempt('gemini', 'model', 'body'):
                    raise HTTPError('https://provider', 400, 'bad shape', {}, io.BytesIO())


def test_live_claim_is_not_recovered_before_lease_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    clock = [10000]
    monkeypatch.setattr(model_work.time, 'time', lambda: clock[0])
    with model_work.scope('notification', 'live') as operation:
        for _ in range(2):
            with model_work.attempt('gemini', 'model', 'body'):
                pass
        with sqlite3.connect(tmp_path / 'events/model-work.sqlite3') as db:
            db.execute("UPDATE attempts SET status='started'")
        with pytest.raises(model_work.ModelWorkHeldError):
            with model_work.attempt('gemini', 'model', 'body'):
                pytest.fail('healthy claim refunded')
        clock[0] += model_work.ATTEMPT_LEASE_SECONDS + 1
        with model_work.attempt('gemini', 'model', 'body'):
            assert operation['attempt'] == 3
