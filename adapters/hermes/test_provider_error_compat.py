import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import types

import pytest


HERE = Path(__file__).parent
FIXTURE = HERE / 'provider_error_pinned_excerpt.py.txt'
FIXTURE_SHA256 = '3962c276109dae6321de2cb839b3890af337460e16ed544944aeefd02b3e3fce'
TURN_FIXTURE = HERE / 'provider_error_turn_runner_excerpt.py.txt'
TURN_FIXTURE_SHA256 = '64d8265e9302d02958780fe4043ec16d81135e5bb986eb29347cc39f99a0319f'
spec = importlib.util.spec_from_file_location(
    'provider_error_compat', HERE / 'provider_error_compat.py')
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


def load_patched(tmp_path, monkeypatch):
    source = FIXTURE.read_text()
    assert hashlib.sha256(source.encode()).hexdigest() == FIXTURE_SHA256
    target = tmp_path / 'run.py'
    target.write_text(source)
    monkeypatch.setattr(compat, 'PINNED_SOURCE_SHA256', FIXTURE_SHA256)
    compat.patch(target)
    compat.patch(target)

    agent = types.ModuleType('agent')
    sanitization = types.ModuleType('agent.message_sanitization')
    sanitization._sanitize_surrogates = lambda text: text
    monkeypatch.setitem(sys.modules, 'agent', agent)
    monkeypatch.setitem(sys.modules, 'agent.message_sanitization', sanitization)
    namespace = {}
    exec(compile(target.read_text(), str(target), 'exec'), namespace)
    return namespace, target


def seed_runtime(tmp_path, monkeypatch, *, pin_gateway=True, pin_turn_runner=True):
    """Write both fixture files as a Hermes gateway pair and point the pins at them."""
    run = tmp_path / 'run.py'
    run.write_text(FIXTURE.read_text())
    turn_runner = tmp_path / 'run_turn_runner.py'
    turn_runner.write_text(TURN_FIXTURE.read_text())
    if pin_gateway:
        monkeypatch.setattr(compat, 'PINNED_SOURCE_SHA256', FIXTURE_SHA256)
    if pin_turn_runner:
        monkeypatch.setattr(compat, 'PINNED_TURN_RUNNER_SHA256', TURN_FIXTURE_SHA256)
    return run, turn_runner


MULTILINE_503 = '''API call failed after 3 retries: HTTP 503 [
  {
    "error": {
      "code": 503,
      "message": "This model is currently experiencing high demand",
      "status": "UNAVAILABLE",
      "details": [
        {"reason": "MODEL_HIGH_DEMAND"}
      ]
    }
  }
]'''

# The OpenAI SDK renders a provider failure as its repr, then logs each retry underneath.
OPENAI_SDK_503 = (
    "Error code: 503 - {'error': {'message': 'The server had an error while processing your "
    "request. Sorry about that!', 'type': 'server_error', 'code': None}, "
    "'request_id': 'req_9f3c2', 'model': 'gpt-x'}\n"
    "Retrying in 2s...\n"
    "Retrying in 4s...\n"
    "Retrying in 8s...\n"
    "Retrying in 16s...\n"
    "Retrying in 32s...\n"
    "Giving up after 5 attempts; the last request_id was req_9f3c2."
)

# A raw gateway body with no "API call failed" preamble at all.
BARE_HTTP_503 = '''HTTP 503 [
  {
    "error": {
      "code": 503,
      "message": "Service is overloaded for model gemini-3.8-flash",
      "status": "SERVICE_BUSY"
    }
  }
]'''

# Prose sits between the preamble and the JSON payload, so the payload is not adjacent.
PROSE_THEN_JSON = '''API call failed after 3 retries: Internal error occurred.
{
  "error": {
    "code": 500,
    "message": "internal",
    "request_id": "req_44ab",
    "model": "claude-sonnet-x",
    "prompt_tokens": 812345
  }
}'''

# A per-attempt retry log: no JSON at all, just many lines of diagnostics.
RATE_LIMITED_ATTEMPTS = 'Rate limited after 5 retries\n' + ''.join(
    f'  attempt {index}: 429 for model gemini-3.8-flash, prompt tokens 812345\n'
    for index in range(6))

# Gemini's real quota-exhaustion body: HTTP 429, but waiting will never clear it.
GEMINI_QUOTA_EXHAUSTED = (
    "API call failed after 3 retries: Error code: 429 - {'error': {'code': 429,\n"
    "  'message': 'You exceeded your current quota, please check your plan and billing "
    "details.',\n"
    "  'status': 'RESOURCE_EXHAUSTED',\n"
    "  'details': [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',\n"
    "               'violations': [{'quotaMetric': 'generate_content_free_tier_requests'}]}]}}\n"
    "  attempt 0: 429 for model gemini-3.8-flash\n"
)

# Anthropic's equivalent: also an owner-must-act stop, worded differently again.
ANTHROPIC_CREDIT_BALANCE = (
    'API call failed after 3 retries: Error code: 400 - {"type":"error","error":'
    '{"type":"invalid_request_error","message":"Your credit balance is too low to access '
    'the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."}}'
)


@pytest.mark.parametrize('raw, expected, absent', [
    (MULTILINE_503, "temporarily unavailable", ('503', '"status"', 'high demand')),
    ('API call failed after 3 retries: HTTP 503 ' + ('provider diagnostic ' * 80),
     "temporarily unavailable", ('503', 'provider diagnostic')),
    ('API call failed after 3 retries: HTTP 429 rate limit exceeded',
     'too many requests', ('429', 'rate limit')),
    ('HTTP 402 {"error":{"code":"sotto_budget_exhausted","message":"quota exhausted"}}',
     'usage allowance', ('402', 'quota exhausted', 'try again')),
    ('API call failed: insufficient_quota; billing token allowance exhausted',
     'usage allowance', ('insufficient_quota', 'try again')),
    # Shapes the length/newline early-out used to let through verbatim.
    (OPENAI_SDK_503, 'temporarily unavailable',
     ('503', 'req_9f3c2', 'gpt-x', 'server_error', 'Retrying in')),
    (BARE_HTTP_503, 'temporarily unavailable',
     ('503', 'gemini-3.8-flash', 'SERVICE_BUSY', 'overloaded')),
    (PROSE_THEN_JSON, "couldn't get an answer",
     ('500', 'req_44ab', 'claude-sonnet-x', '812345', 'internal')),
    (RATE_LIMITED_ATTEMPTS, 'too many requests',
     ('429', 'gemini-3.8-flash', '812345', 'attempt')),
    # Owner-must-act allowance stops that HTTP 429 alone would misfile as transient load.
    (GEMINI_QUOTA_EXHAUSTED, 'usage allowance',
     ('429', 'RESOURCE_EXHAUSTED', 'billing', 'quota', 'gemini-3.8-flash', 'try again')),
    (ANTHROPIC_CREDIT_BALANCE, 'usage allowance',
     ('400', 'credit balance', 'Anthropic', 'Plans & Billing', 'try again')),
])
def test_final_chat_boundary_replaces_provider_envelopes(tmp_path, monkeypatch, raw, expected, absent):
    module, _ = load_patched(tmp_path, monkeypatch)
    assert module['_looks_like_gateway_provider_error'](raw)
    for platform in ('telegram', 'photon'):
        reply = module['_sanitize_gateway_final_response'](platform, raw)
        assert expected in reply
        for phrase in absent:
            assert phrase.lower() not in reply.lower()


@pytest.mark.parametrize('raw', [
    OPENAI_SDK_503, BARE_HTTP_503, PROSE_THEN_JSON, RATE_LIMITED_ATTEMPTS, GEMINI_QUOTA_EXHAUSTED])
def test_detection_no_longer_depends_on_length_or_line_count(tmp_path, monkeypatch, raw):
    # Each of these is past the old `len > 400 or newlines > 4` early-out that used to send the
    # raw body straight to chat. Keep them past it, or this stops being a regression test.
    assert len(raw) > 400 or raw.count('\n') > 4
    module, _ = load_patched(tmp_path, monkeypatch)
    assert module['_looks_like_gateway_provider_error'](raw)


def test_programmatic_surfaces_preserve_raw_provider_diagnostics(tmp_path, monkeypatch):
    module, _ = load_patched(tmp_path, monkeypatch)
    for platform in ('local', 'api_server', 'webhook', 'msgraph_webhook'):
        assert module['_sanitize_gateway_final_response'](platform, MULTILINE_503) == MULTILINE_503
        assert module['_prepare_gateway_status_message'](platform, 'warn', MULTILINE_503) == MULTILINE_503


@pytest.mark.parametrize('raw', [MULTILINE_503, OPENAI_SDK_503, RATE_LIMITED_ATTEMPTS])
def test_chat_status_path_reports_the_failure_rather_than_going_silent(tmp_path, monkeypatch, raw):
    # A FAILED turn publishes no stream payload on a natively streaming surface, so a silent
    # status callback can lose the failure entirely. The status path says the same sentence.
    module, _ = load_patched(tmp_path, monkeypatch)
    for platform in ('photon', 'telegram'):
        status = module['_prepare_gateway_status_message'](platform, 'warn', raw)
        assert status == module['_sanitize_gateway_final_response'](platform, raw)
        assert 'service' in status.lower() or 'account' in status.lower()
        assert raw not in status


@pytest.mark.parametrize('raw', [MULTILINE_503, GEMINI_QUOTA_EXHAUSTED, PROSE_THEN_JSON])
def test_sanitized_copy_survives_a_second_pass_unchanged(tmp_path, monkeypatch, raw):
    # Idempotence bounds the status/final duplicate to the same sentence twice.
    module, _ = load_patched(tmp_path, monkeypatch)
    once = module['_sanitize_gateway_final_response']('photon', raw)
    assert module['_sanitize_gateway_final_response']('photon', once) == once
    assert module['_prepare_gateway_status_message']('photon', 'warn', once) == once
    assert not module['_looks_like_gateway_provider_error'](once)


@pytest.mark.parametrize('preamble', [
    'The report mentions an HTTP 503 error.',
    'API call failed: this is the heading in your error report.',
    # The marker cannot be its own evidence: a write-up that OPENS with a status line is
    # still prose unless a payload, status code or attempt line follows it.
    'HTTP 503 explained:',
    'Error code: what your monitoring dashboard is telling you.',
])
def test_ordinary_long_error_discussion_is_not_a_provider_envelope(tmp_path, monkeypatch, preamble):
    module, _ = load_patched(tmp_path, monkeypatch)
    ordinary = preamble + '\n' + ('Here is how to fix it.\n' * 20)
    assert not module['_looks_like_gateway_provider_error'](ordinary)
    assert module['_sanitize_gateway_final_response']('telegram', ordinary) == ordinary


def test_patch_refuses_unreviewed_source(tmp_path, monkeypatch):
    source = FIXTURE.read_text()
    monkeypatch.setattr(compat, 'PINNED_SOURCE_SHA256', FIXTURE_SHA256)
    target = tmp_path / 'run.py'
    target.write_text(source.replace('rate-limiting requests', 'requests are busy'))
    with pytest.raises(RuntimeError, match='reviewed pin'):
        compat.patch(target)


def test_check_mode_accepts_the_pinned_pair_without_writing(tmp_path, monkeypatch, capsys):
    run, turn_runner = seed_runtime(tmp_path, monkeypatch)
    before = (run.read_text(), turn_runner.read_text())
    compat.check_runtime(run)
    assert compat.main(['--check', str(run)]) == 0
    assert (run.read_text(), turn_runner.read_text()) == before
    assert 'matches the reviewed provider-error pin' in capsys.readouterr().out


@pytest.mark.parametrize('pin_gateway, pin_turn_runner, expected_pin', [
    (False, True, compat.PINNED_SOURCE_SHA256),
    (True, False, compat.PINNED_TURN_RUNNER_SHA256),
])
def test_check_mode_fails_a_pin_bump_with_one_fatal_line(
        tmp_path, monkeypatch, capsys, pin_gateway, pin_turn_runner, expected_pin):
    # A hermes.commit bump has to fail the image build, not crash-loop every container.
    run, _ = seed_runtime(tmp_path, monkeypatch,
                          pin_gateway=pin_gateway, pin_turn_runner=pin_turn_runner)
    assert compat.main(['--check', str(run)]) == 1
    errors = capsys.readouterr().err.strip().splitlines()
    assert len(errors) == 1
    assert errors[0].startswith('[sotto] FATAL: Hermes ')
    assert f'expected {expected_pin[:8]}' in errors[0]
    assert 'found ' in errors[0] and 'adapters/hermes/README.md' in errors[0]
    assert 'Traceback' not in errors[0]


def test_missing_gateway_source_is_also_one_fatal_line(tmp_path, capsys):
    assert compat.main([str(tmp_path / 'absent' / 'run.py')]) == 1
    errors = capsys.readouterr().err.strip().splitlines()
    assert len(errors) == 1
    assert errors[0].startswith('[sotto] FATAL: cannot read the pinned Hermes gateway source')


def test_apply_mode_patches_both_files_and_is_idempotent(tmp_path, monkeypatch):
    run, turn_runner = seed_runtime(tmp_path, monkeypatch)
    compat.patch_runtime(run)
    compat.patch_runtime(run)
    assert '_SOTTO_PROVIDER_PAYLOAD_RE' in run.read_text()
    assert '_sanitize_gateway_final_response(ctx.source.platform, fr)' in turn_runner.read_text()
    compat.check_runtime(run)


def test_native_stream_finalization_uses_the_chat_sanitizer(tmp_path, monkeypatch):
    source = TURN_FIXTURE.read_text()
    assert hashlib.sha256(source.encode()).hexdigest() == TURN_FIXTURE_SHA256
    target = tmp_path / 'run_turn_runner.py'
    target.write_text(source)
    monkeypatch.setattr(compat, 'PINNED_TURN_RUNNER_SHA256', TURN_FIXTURE_SHA256)
    compat.patch_turn_runner(target)
    compat.patch_turn_runner(target)

    gateway = types.ModuleType('gateway')
    gateway_run = types.ModuleType('gateway.run')
    gateway_run._sanitize_gateway_final_response = lambda platform, text: f'safe:{platform}:{len(text)}'
    monkeypatch.setitem(sys.modules, 'gateway', gateway)
    monkeypatch.setitem(sys.modules, 'gateway.run', gateway_run)
    namespace = {}
    exec(compile(target.read_text(), str(target), 'exec'), namespace)
    delivered = []
    consumer = types.SimpleNamespace(finish=lambda text=None: delivered.append(text))
    ctx = types.SimpleNamespace(source=types.SimpleNamespace(platform='photon'))
    namespace['Fixture']().finish({'final_response': MULTILINE_503}, consumer, ctx)
    assert delivered == [f'safe:photon:{len(MULTILINE_503)}']


def test_actual_checkout_matches_declared_pin_when_available():
    configured = os.environ.get('HERMES_PINNED_CHECKOUT')
    candidates = ([Path(configured)] if configured else []) + [
        HERE.parents[2].parent / 'hermes-pinned-proof',
        Path('/usr/local/lib/hermes-agent'),
    ]
    paths = [checkout / 'gateway/run.py' for checkout in candidates]
    target = next((path for path in paths if path.is_file()), None)
    if target is None:
        pytest.skip('full reviewed Hermes checkout is unavailable')
    assert compat._original_source(target.read_text()) == target.read_text()
    turn_runner = target.with_name('run_turn_runner.py')
    assert hashlib.sha256(turn_runner.read_bytes()).hexdigest() == compat.PINNED_TURN_RUNNER_SHA256
    # The anchors must still resolve in that checkout, not just the whole-file hashes.
    compat.check_runtime(target)
