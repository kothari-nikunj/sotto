"""Offline checks for the public Railway self-host setup boundary."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from adapters.sotto import selfhost_bootstrap
from adapters.sotto import process_handoff


spec = importlib.util.spec_from_file_location('setup_selfhost_railway',
                                             Path(__file__).with_name('setup_selfhost_railway.py'))
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
ROOT = Path(__file__).resolve().parents[1]
REAL_HEALTH = setup._health
MARKER = {'schema': 1, 'tenant_id': 'fixture-local-tenant', 'volume_id': 'volume-id'}


def config(tmp_path):
    return {'project_id': '11111111-1111-1111-1111-111111111111',
            'environment_id': '22222222-2222-2222-2222-222222222222',
            'service_name': 'sotto-selfhost', 'source_path': str(tmp_path),
            'source_commit': 'a' * 40, 'source_manifest_sha256': 'b' * 64,
            'tenant_id': 'fixture-local-tenant', 'owner_phone': '+15551234567',
            'photon_line_number': '+15557654321', 'photon_project_id': 'synthetic-project',
            'photon_project_secret': 'private-photon-secret', 'gemini_api_key': 'private-gemini-key',
            'sotto_runtime': {'account_id': 'fixture-account', 'line_key': 'physical-line-a',
                              'instructions': 'Answer briefly.', 'chat_model_id': 'gemini-chat-fixture',
                              'native_media_model_id': 'gemini-media-fixture', 'timezone': 'UTC'}}


class FakeRailway:
    """Synthetic Railway: each CLI upload becomes one deployment carrying the current start command."""

    def __init__(self, project, environment):
        self.service = None
        self.volume = None
        self.domain = None
        self.volume_hidden_reads = 0
        self.domain_hidden_reads = 0
        self.vars = {}
        self.history = {}
        self.messages = {}
        self.starts = {}
        self.start = None
        self.uploads = 0
        self.new_status = 'SUCCESS'
        self.operations = []
        self.settings = {'dockerfilePath': None, 'rootDirectory': None,
                         'healthcheckPath': None, 'healthcheckTimeout': 300}
        self.marker = 'pending'
        self.marker_reads = 0

    def close(self):
        self.operations.append('close')

    def target(self):
        self.operations.append('target')

    def services(self):
        return [self.service] if self.service else []

    def create_service(self, name, marker):
        self.operations.append('create_service')
        self.service = {'id': 'service-id', 'name': name}
        self.vars['SOTTO_SETUP_REQUEST'] = marker
        return self.service

    def variables(self, service):
        values = dict(self.vars)
        if self.domain:
            values['RAILWAY_PUBLIC_DOMAIN'] = self.domain['domain']
            values['RAILWAY_SERVICE_SOTTO_SELFHOST_URL'] = self.domain['domain']
        return values

    def set_variable(self, service, name, value):
        self.operations.append('set_variable:' + name)
        self.vars[name] = value

    def volumes(self):
        if self.volume and self.volume_hidden_reads:
            self.volume_hidden_reads -= 1
            return []
        return [self.volume] if self.volume else []

    def create_volume(self, service):
        self.operations.append('create_volume')
        self.volume = {'id': 'volume-id', 'service_id': service, 'mount_path': '/data'}
        return 'volume-id'

    def domains(self, service):
        if self.domain and self.domain_hidden_reads:
            self.domain_hidden_reads -= 1
            return []
        return [self.domain] if self.domain else []

    def create_domain(self, service):
        self.operations.append('create_domain')
        self.domain = {'domain': 'selfhost.example.test', 'target_port': 8080}
        return {'domain': self.domain['domain'], 'targetPort': 8080}

    def instance(self, service):
        latest = None
        if self.history:
            identifier = list(self.history)[-1]
            latest = {'id': identifier, 'status': self.history[identifier]}
        return {'startCommand': self.start, 'source': {}, 'latestDeployment': latest}

    def build_settings(self, service):
        return dict(self.settings)

    def set_build_settings(self, service, settings):
        self.operations.append('set_build_settings')
        self.settings.update(settings)

    def set_start(self, service, start):
        self.operations.append('set_start:' + start)
        self.start = start

    def upload(self, service, source, message):
        self.operations.append('upload')
        self.uploads += 1
        identifier = f'deployment-{self.uploads}'
        self.messages[identifier] = message
        self.starts[identifier] = self.start
        self.history[identifier] = self.new_status

    def deployment(self, identifier, service):
        return {'id': identifier, 'status': self.history[identifier], 'meta': {
            'cliMessage': self.messages.get(identifier), 'imageDigest': 'sha256:' + 'c' * 64,
            'serviceManifest': {'deploy': {'startCommand': self.starts.get(identifier)}}}}

    def deployment_statuses(self, service):
        return dict(self.history)

    def volume_marker(self, volume):
        self.marker_reads += 1
        if self.marker == 'pending':
            raise setup.VolumeInspectionPendingError('file access needs a running container')
        return self.marker


class Healthy:
    status = 200

    def read(self, *_args):
        return json.dumps({'status': 'ok', 'work': {}, 'delivery': {}, 'model_lease': {}}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def offline(monkeypatch, c):
    """Pin the synthetic source and record each health probe as (prepared, domain)."""
    monkeypatch.setattr(setup, 'source_receipt', lambda *_a, **_k: {
        'source_commit': c['source_commit'],
        'source_manifest_sha256': c['source_manifest_sha256']})
    health = []
    monkeypatch.setattr(setup, '_health', lambda domain, *, prepared: health.append((prepared, domain)))
    return health


def run_setup(c, state_path, provider, **kwargs):
    return setup.reconcile(c, state_path, provider_factory=lambda *_: provider, **kwargs)


def test_fresh_setup_uploads_pinned_source_twice_with_prepare_then_run(tmp_path, monkeypatch):
    c = config(tmp_path)
    health = offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    state_path = tmp_path / 'new-state-directory' / 'state.json'
    result = run_setup(c, state_path, provider)
    assert result['stage'] == 'receiver_reachable'
    assert result['model_status'] == 'unverified' and result['setup_status'] == 'pending'
    assert result['image_digest'] == 'sha256:' + 'c' * 64
    assert state_path.stat().st_mode & 0o077 == 0
    state = setup.protected_json(state_path)
    assert (state['initializer_deployment_id'], state['runtime_deployment_id']) == (
        'deployment-1', 'deployment-2')
    assert provider.starts == {'deployment-1': setup.SOTTO_PREPARE_START,
                               'deployment-2': setup.SOTTO_RUNTIME_START}
    marker = state['request_marker']
    assert provider.messages == {
        'deployment-1': f'sotto-selfhost {marker} initializer {c["source_commit"]}',
        'deployment-2': f'sotto-selfhost {marker} runtime {c["source_commit"]}'}
    assert health == [(True, 'selfhost.example.test'), (False, 'selfhost.example.test')]
    assert set(provider.vars) == {'SOTTO_SETUP_REQUEST', 'SOTTO_SELFHOST_LAUNCH'}
    envelope = json.loads(provider.vars['SOTTO_SELFHOST_LAUNCH'])
    assert envelope['provider']['volumeCreationReceipt'] == {
        'service_id': 'service-id', 'volume_id': 'volume-id'}
    assert envelope['bundle']['manifest']['photonLineNumber'] == c['photon_line_number']
    assert envelope['bundle']['manifest']['owner']['ownerId'] == c['owner_phone']
    assert envelope['bundle']['publicReceiverAuthority']['serviceId'] == 'service-id'
    assert envelope['bundle']['trustedServiceId'] == 'service-id'
    ops = provider.operations
    assert ops.index('create_volume') < ops.index('create_domain') < ops.index(
        'set_variable:SOTTO_SELFHOST_LAUNCH') < ops.index('set_build_settings') < ops.index(
        'set_start:' + setup.SOTTO_PREPARE_START) < ops.index('upload')
    assert 'private-photon-secret' not in json.dumps(result)
    assert 'private-gemini-key' not in json.dumps(result)
    run_setup(c, state_path, provider)
    for step in ('create_service', 'create_volume', 'create_domain'):
        assert provider.operations.count(step) == 1
    assert provider.uploads == 2


def test_slow_build_resumes_same_deployment_without_second_upload(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    provider.new_status = 'BUILDING'
    monkeypatch.setattr(setup, 'DEPLOYMENT_POLLS', 2)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: None)
    state_path = tmp_path / 'state.json'
    with pytest.raises(setup.SetupPendingError, match='saved deployment will be reused'):
        run_setup(c, state_path, provider)
    assert setup.protected_json(state_path)['initializer_deployment_id'] == 'deployment-1'
    provider.history['deployment-1'] = 'SUCCESS'
    provider.new_status = 'SUCCESS'
    assert run_setup(c, state_path, provider)['stage'] == 'receiver_reachable'
    assert provider.uploads == 2 and provider.operations.count('create_volume') == 1


def test_pending_cli_explains_resume_without_traceback(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / 'input.json'
    setup.save_state(config_path, {})

    def pending(*_args, **_kwargs):
        raise setup.SetupPendingError('Continue with the same command.')

    monkeypatch.setattr(setup, 'reconcile', pending)
    assert setup.main(['--config', str(config_path), '--state', str(tmp_path / 'state.json')]) == 2
    assert json.loads(capsys.readouterr().out) == {
        'stage': 'deployment_pending', 'message': 'Continue with the same command.'}


def test_generated_railway_variables_allowed_but_other_app_variables_rejected(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    run_setup(c, tmp_path / 'state.json', provider)
    assert provider.variables('service-id')['RAILWAY_PUBLIC_DOMAIN'] == 'selfhost.example.test'
    provider.vars['PHOTON_ALLOW_ALL_USERS'] = 'true'
    with pytest.raises(setup.SetupError, match='Existing service variables differ'):
        run_setup(c, tmp_path / 'state.json', provider)


def test_conflicts_fail_closed_without_touching_other_service_or_volume(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    provider.service = {'id': 'unowned', 'name': c['service_name']}
    with pytest.raises(setup.SetupError, match='not owned'):
        run_setup(c, tmp_path / 'state.json', provider)
    assert 'create_volume' not in provider.operations
    provider.service = None
    provider.volume = {'id': 'foreign-volume', 'service_id': 'service-id', 'mount_path': '/data'}
    with pytest.raises(setup.SetupError, match='ambiguous'):
        run_setup(c, tmp_path / 'state.json', provider)
    assert 'upload' not in provider.operations


def test_rejects_changed_input_and_second_active_deployment(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    state_path = tmp_path / 'state.json'
    run_setup(c, state_path, provider)
    changed = {**c, 'owner_phone': '+15555550202'}
    with pytest.raises(setup.SetupError, match='differs'):
        run_setup(changed, state_path, provider)
    provider.history['other-active'] = 'SUCCESS'
    with pytest.raises(setup.SetupError, match='no longer latest|Another Sotto runtime deployment'):
        run_setup(c, state_path, provider)


def test_changed_source_checkout_stops_before_provider_contact(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    monkeypatch.setattr(setup, 'source_receipt', lambda *_a, **_k: {
        'source_commit': 'd' * 40, 'source_manifest_sha256': c['source_manifest_sha256']})
    provider = FakeRailway(c['project_id'], c['environment_id'])
    with pytest.raises(setup.SetupError, match='differs from the pinned clean checkout'):
        run_setup(c, tmp_path / 'state.json', provider)
    assert provider.operations == []


def test_source_receipt_parses_actual_railway_toml_not_comment_text(tmp_path):
    (tmp_path / 'Dockerfile').write_text('FROM scratch\n')
    (tmp_path / 'VERSION').write_text('test\n')
    (tmp_path / 'railway.toml').write_text(
        '# builder = "DOCKERFILE"\n# dockerfilePath = "Dockerfile"\n'
        '[build]\nbuilder = "NIXPACKS"\ndockerfilePath = "other"\n'
        '[deploy]\nhealthcheckPath = "/health"\n')
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'Dockerfile', 'VERSION', 'railway.toml'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.test', 'commit', '-qm', 'fixture'], check=True)
    with pytest.raises(setup.SetupError, match='build/health'):
        setup.source_receipt(tmp_path)


def test_created_volume_must_attach_before_source_upload(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 0)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    provider.create_volume = lambda service: 'unattached-volume-id'
    with pytest.raises(setup.SetupError, match='did not attach'):
        run_setup(c, tmp_path / 'state.json', provider)
    assert 'upload' not in provider.operations


def test_transient_volume_and_domain_absence_resolve_without_second_creation(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: None)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    provider.volume_hidden_reads = 2
    provider.domain_hidden_reads = 2
    assert run_setup(c, tmp_path / 'state.json', provider)['stage'] == 'receiver_reachable'
    assert provider.operations.count('create_volume') == 1
    assert provider.operations.count('create_domain') == 1
    assert provider.uploads == 2


def test_wrong_new_attachment_fails_immediately_without_upload(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: pytest.fail('wrong attachment was polled'))
    provider = FakeRailway(c['project_id'], c['environment_id'])

    def wrong_volume(_service):
        provider.operations.append('create_volume')
        provider.volume = {'id': 'volume-id', 'service_id': 'another-service', 'mount_path': '/data'}
        return 'volume-id'

    provider.create_volume = wrong_volume
    with pytest.raises(setup.SetupError, match='attached differently'):
        run_setup(c, tmp_path / 'state.json', provider)
    assert 'upload' not in provider.operations


def test_saved_volume_wait_timeout_reruns_without_duplicate_mutation(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    provider.volume_hidden_reads = 100
    state_path = tmp_path / 'state.json'
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 0)
    with pytest.raises(setup.SetupError, match='did not attach'):
        run_setup(c, state_path, provider)
    state = setup.protected_json(state_path)
    assert state['volume_create_attempted'] and state['volume_id'] == 'volume-id'
    assert provider.operations.count('create_volume') == 1
    assert 'upload' not in provider.operations
    provider.volume_hidden_reads = 2
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 30)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: None)
    assert run_setup(c, state_path, provider)['stage'] == 'receiver_reachable'
    assert provider.operations.count('create_volume') == 1


def test_wrong_new_domain_fails_immediately_without_upload(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: pytest.fail('wrong domain was polled'))
    provider = FakeRailway(c['project_id'], c['environment_id'])

    def wrong_domain(_service):
        provider.operations.append('create_domain')
        provider.domain = {'domain': 'other.example.test', 'target_port': 8080}
        return {'domain': 'selfhost.example.test', 'targetPort': 8080}

    provider.create_domain = wrong_domain
    with pytest.raises(setup.SetupError, match='attached differently'):
        run_setup(c, tmp_path / 'state.json', provider)
    assert 'upload' not in provider.operations


def test_saved_domain_wait_timeout_reruns_without_duplicate_mutation(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    provider.domain_hidden_reads = 100
    state_path = tmp_path / 'state.json'
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 0)
    with pytest.raises(setup.SetupError, match='did not attach'):
        run_setup(c, state_path, provider)
    state = setup.protected_json(state_path)
    assert state['domain_create_attempted'] and state['domain'] == 'selfhost.example.test'
    assert provider.operations.count('create_domain') == 1
    assert 'upload' not in provider.operations
    provider.domain_hidden_reads = 2
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 30)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: None)
    assert run_setup(c, state_path, provider)['stage'] == 'receiver_reachable'
    assert provider.operations.count('create_domain') == 1


@pytest.mark.parametrize('prepared,body,message', [
    (True, b'{"status": "ok"}', 'preparation health is not exact'),
    (False, b'{"status": "ok"}', 'not the reviewed receiver'),
    # The initializer's body is not runtime health: setup keeps waiting for the runtime.
    (False, b'OK', 'not ready'),
])
def test_health_must_be_the_exact_phase_response(monkeypatch, prepared, body, message):
    class Response(Healthy):
        def read(self, *_args):
            return body

    monkeypatch.setattr(setup.urllib.request, 'urlopen', lambda *_a, **_k: Response())
    with pytest.raises(setup.SetupError, match=message):
        setup._health('selfhost.example.test', prepared=prepared)


def test_deployment_must_carry_the_phase_start_command(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    original = provider.upload

    def railway_ignores_start(*args):
        original(*args)
        provider.starts[f'deployment-{provider.uploads}'] = None

    provider.upload = railway_ignores_start
    with pytest.raises(setup.SetupError, match='unexpected start command'):
        run_setup(c, tmp_path / 'state.json', provider)
    assert provider.uploads == 1


def test_railway_api_keeps_secret_out_of_argv_and_protects_temp_file(monkeypatch):
    observed = []
    secret = 'private-provider-credential'

    def run(argv, **_kwargs):
        assert secret not in ' '.join(argv)
        path = Path(argv[argv.index('--variables') + 1][1:])
        assert path.stat().st_mode & 0o077 == 0
        assert json.loads(path.read_text()) == {'input': {'secret': secret}}
        observed.append(path)
        return subprocess.CompletedProcess(argv, 0, json.dumps({'data': {'ok': True}}), '')

    monkeypatch.setattr(setup.subprocess, 'run', run)
    provider = setup.Railway('project', 'environment')
    try:
        assert provider.api('query Test', {'input': {'secret': secret}}) == {'ok': True}
        assert not observed[0].exists()
    finally:
        provider.close()


def test_target_requires_selected_single_environment(monkeypatch):
    provider = setup.Railway('project', 'environment')
    monkeypatch.setattr(provider, 'api', lambda *_args: {
        'project': {'id': 'project', 'environments': {'edges': [
            {'node': {'id': 'other', 'sourceEnvironment': None}}],
            'pageInfo': {'hasNextPage': False}}},
        'environment': {'id': 'environment', 'projectId': 'project',
                        'isEphemeral': False, 'sourceEnvironment': None}})
    try:
        with pytest.raises(setup.SetupError, match='fresh'):
            provider.target()
    finally:
        provider.close()


def fail_once(monkeypatch, provider, name, *, lands, sent=True, skip=0):
    """Railway call fails once (after `skip` successes): never sent, or sent and it landed or not."""
    original = getattr(provider, name)
    calls = {'n': 0}

    def failing(*args):
        calls['n'] += 1
        if calls['n'] <= skip:
            return original(*args)
        monkeypatch.setattr(provider, name, original)
        if not sent:
            raise setup.NotSentError('Railway CLI could not start')
        if lands:
            original(*args)
        raise setup.SetupError('Railway operation failed or timed out')

    monkeypatch.setattr(provider, name, failing)


STEPS = [('create_service', 0), ('create_volume', 0), ('create_domain', 0),
         ('upload', 0), ('upload', 1), ('set_start', 0), ('set_start', 1)]


@pytest.mark.parametrize('name,skip', STEPS)
@pytest.mark.parametrize('lands', [False, True])
def test_failed_railway_step_resumes_from_observed_state(tmp_path, monkeypatch, name, skip, lands):
    c = config(tmp_path)
    offline(monkeypatch, c)
    monkeypatch.setattr(setup.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 0)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    fail_once(monkeypatch, provider, name, lands=lands, sent=lands, skip=skip)
    state_path = tmp_path / 'state.json'
    with pytest.raises(setup.SetupError, match='failed or timed out|could not start'):
        run_setup(c, state_path, provider)
    assert run_setup(c, state_path, provider)['stage'] == 'receiver_reachable'
    # A call that never left this machine is retried once; one that landed is adopted, never repeated.
    for step in ('create_service', 'create_volume', 'create_domain'):
        assert provider.operations.count(step) == 1
    assert provider.uploads == 2
    state = setup.protected_json(state_path)
    assert (state['service_id'], state['volume_id'], state['domain'], state['runtime_deployment_id']) == (
        'service-id', 'volume-id', 'selfhost.example.test', 'deployment-2')


@pytest.mark.parametrize('name,skip', STEPS)
def test_unconfirmed_mutation_is_observed_not_repeated(tmp_path, monkeypatch, capsys, name, skip):
    """A sent request whose outcome is unknown stays pending until Railway shows it or the operator confirms."""
    c = config(tmp_path)
    offline(monkeypatch, c)
    monkeypatch.setattr(setup.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 0)  # the observation window expires empty
    provider = FakeRailway(c['project_id'], c['environment_id'])
    fail_once(monkeypatch, provider, name, lands=False, skip=skip)
    state_path = tmp_path / 'state.json'
    with pytest.raises(setup.SetupError, match='failed or timed out'):
        run_setup(c, state_path, provider)
    sent = provider.operations.count('upload') + sum(
        op.startswith('set_start') for op in provider.operations)
    for _ in range(2):
        with pytest.raises(setup.UnconfirmedCreateError, match='--retry-unconfirmed'):
            run_setup(c, state_path, provider)
    assert provider.operations.count('upload') + sum(
        op.startswith('set_start') for op in provider.operations) == sent
    # The CLI reports it as resumable, not as a failure.
    reconcile = setup.reconcile
    monkeypatch.setattr(setup, 'reconcile', lambda *a, **k: reconcile(
        *a, **{'provider_factory': lambda *_: provider, **k}))
    config_path = tmp_path / 'config.json'
    setup.save_state(config_path, c)
    assert setup.main(['--config', str(config_path), '--state', str(state_path)]) == 2
    assert json.loads(capsys.readouterr().out)['stage'] == 'create_unconfirmed'
    # After checking Railway, the operator's explicit retry sends it exactly once more.
    assert reconcile(c, state_path, provider_factory=lambda *_: provider,
                     retry_unconfirmed=True)['stage'] == 'receiver_reachable'
    assert provider.uploads == 2
    for step in ('create_service', 'create_volume', 'create_domain'):
        assert provider.operations.count(step) == 1
    assert [op for op in provider.operations if op.startswith('set_start')] == [
        'set_start:' + setup.SOTTO_PREPARE_START, 'set_start:' + setup.SOTTO_RUNTIME_START]


def test_lost_create_waits_for_late_visibility_instead_of_duplicating(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    monkeypatch.setattr(setup.time, 'sleep', lambda *_: None)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    fail_once(monkeypatch, provider, 'create_volume', lands=True)
    state_path = tmp_path / 'state.json'
    with pytest.raises(setup.SetupError):
        run_setup(c, state_path, provider)
    provider.volume_hidden_reads = 3
    assert run_setup(c, state_path, provider)['stage'] == 'receiver_reachable'
    assert provider.operations.count('create_volume') == 1


def test_ambiguous_state_after_lost_create_stays_stopped(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    monkeypatch.setattr(setup.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 0)
    state_path = tmp_path / 'state.json'

    # An unmarked service with our name is not adopted, even after our own create failed.
    provider = FakeRailway(c['project_id'], c['environment_id'])
    fail_once(monkeypatch, provider, 'create_service', lands=False)
    with pytest.raises(setup.SetupError):
        run_setup(c, state_path, provider)
    provider.service = {'id': 'unmarked', 'name': c['service_name']}
    with pytest.raises(setup.SetupError, match='not owned'):
        run_setup(c, state_path, provider)
    assert provider.operations.count('create_service') == 0

    # A second volume beside ours is never adopted or deleted.
    state_path.unlink()
    provider = FakeRailway(c['project_id'], c['environment_id'])
    fail_once(monkeypatch, provider, 'create_volume', lands=True)
    with pytest.raises(setup.SetupError):
        run_setup(c, state_path, provider)
    extra = {'id': 'second-volume', 'service_id': 'service-id', 'mount_path': '/data'}
    provider.volumes = lambda: [provider.volume, extra]
    with pytest.raises(setup.SetupError, match='Volume creation is ambiguous'):
        run_setup(c, state_path, provider)
    assert provider.operations.count('create_volume') == 1 and setup.protected_json(state_path)['volume_id'] is None

    # A deployment that does not carry this phase's source identity is neither adopted nor re-uploaded.
    for message in (None, 'someone else'):
        state_path.unlink()
        provider = FakeRailway(c['project_id'], c['environment_id'])
        fail_once(monkeypatch, provider, 'upload', lands=False)
        with pytest.raises(setup.SetupError):
            run_setup(c, state_path, provider)
        provider.history['foreign'] = 'SUCCESS'
        provider.messages['foreign'] = message
        with pytest.raises(setup.SetupError, match='unrelated deployment|upload unresolved'):
            run_setup(c, state_path, provider)
        assert provider.uploads == 0


def test_saved_service_missing_is_not_recreated(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    state_path = tmp_path / 'state.json'
    run_setup(c, state_path, provider)
    provider.service = None
    with pytest.raises(setup.SetupError, match='Saved service is missing'):
        run_setup(c, state_path, provider)
    assert provider.operations.count('create_service') == 1


def test_two_new_deployments_stop_instead_of_pending_forever(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    original = provider.upload

    def double(*args):
        original(*args)
        original(*args)

    provider.upload = double
    with pytest.raises(setup.SetupError, match='More than one new Sotto deployment') as raised:
        run_setup(c, tmp_path / 'state.json', provider, retry_unconfirmed=True)
    assert not isinstance(raised.value, setup.SetupPendingError)


def test_prepared_health_polls_through_fresh_domain_lag(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    monkeypatch.setattr(setup, '_health', REAL_HEALTH)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    monkeypatch.setattr(setup.time, 'sleep', lambda *_: None)
    calls = []

    def urlopen(*_a, **_k):
        calls.append(1)
        if len(calls) <= 3:
            raise OSError('fresh domain DNS/TLS lag')
        response = Healthy()
        if len(calls) == 4:
            response.read = lambda *_: b'OK'  # the initializer's exact preparation body
        return response

    monkeypatch.setattr(setup.urllib.request, 'urlopen', urlopen)
    state_path = tmp_path / 'state.json'
    assert run_setup(c, state_path, provider)['stage'] == 'receiver_reachable'
    assert len(calls) == 5
    state = setup.protected_json(state_path)
    assert state['prepared_proof'] == 'health' and state['prepared_health_observed']
    assert provider.marker_reads == 0 and provider.uploads == 2


def unready_prepare(monkeypatch, *, now=1000.0):
    """The preparation listener is never reachable; the clock is controlled by the test."""
    clock = {'now': now}
    monkeypatch.setattr(setup.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(setup.time, 'time', lambda: clock['now'])
    monkeypatch.setattr(setup, 'HEALTH_WAIT_SECONDS', 0)

    def health(_domain, *, prepared):
        if prepared:
            raise setup.SetupPendingError('Sotto health endpoint is not ready; resume exact deployment')

    monkeypatch.setattr(setup, '_health', health)
    return clock


def test_unseen_preparation_resumes_from_exact_volume_marker(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    unready_prepare(monkeypatch)
    state_path = tmp_path / 'state.json'
    with pytest.raises(setup.SetupPendingError, match='within 15 minutes') as raised:
        run_setup(c, state_path, provider, retry_unconfirmed=True)
    # Inside the listener window the operator flag changes nothing.
    assert not isinstance(raised.value, setup.PreparationUnconfirmedError)
    provider.marker = MARKER
    assert run_setup(c, state_path, provider)['stage'] == 'receiver_reachable'
    assert setup.protected_json(state_path)['prepared_proof'] == 'volume_marker'
    assert provider.uploads == 2


def test_wrong_marker_or_failed_initializer_stops(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    unready_prepare(monkeypatch)
    provider.marker = {**MARKER, 'tenant_id': 'another-tenant'}
    with pytest.raises(setup.SetupError, match='marker differs'):
        run_setup(c, tmp_path / 'state.json', provider)
    provider.marker = 'pending'
    provider.history['deployment-1'] = 'CRASHED'
    with pytest.raises(setup.SetupError, match='did not succeed|stopped before preparation') as raised:
        run_setup(c, tmp_path / 'state.json', provider, retry_unconfirmed=True)
    assert not isinstance(raised.value, setup.SetupPendingError)
    assert provider.uploads == 1


def test_closed_listener_needs_operator_and_runtime_marker(tmp_path, monkeypatch, capsys):
    c = config(tmp_path)
    offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    clock = unready_prepare(monkeypatch)
    state_path = tmp_path / 'state.json'  # the listener exited, so no container serves file access
    with pytest.raises(setup.SetupPendingError, match='within 15 minutes'):
        run_setup(c, state_path, provider)
    clock['now'] += setup.PREPARED_LISTENER_SECONDS + setup.PREPARED_LISTENER_MARGIN_SECONDS
    with pytest.raises(setup.PreparationUnconfirmedError, match='--retry-unconfirmed'):
        run_setup(c, state_path, provider)
    config_path = tmp_path / 'config.json'
    setup.save_state(config_path, c)
    reconcile = setup.reconcile
    monkeypatch.setattr(setup, 'reconcile', lambda *a, **k: reconcile(
        *a, **{'provider_factory': lambda *_: provider, **k}))
    assert setup.main(['--config', str(config_path), '--state', str(state_path)]) == 2
    assert json.loads(capsys.readouterr().out)['stage'] == 'preparation_unconfirmed'
    assert provider.uploads == 1
    # Confirmed: the runtime replaces the closed initializer, which is never redeployed,
    # and setup succeeds only once the running runtime shows the exact marker.
    with pytest.raises(setup.SetupPendingError, match='marker is not readable yet'):
        reconcile(c, state_path, provider_factory=lambda *_: provider, retry_unconfirmed=True)
    assert provider.uploads == 2
    provider.marker = None
    with pytest.raises(setup.SetupError, match='no preparation marker'):
        reconcile(c, state_path, provider_factory=lambda *_: provider)
    provider.marker = MARKER
    assert reconcile(c, state_path, provider_factory=lambda *_: provider)['stage'] == 'receiver_reachable'
    state = setup.protected_json(state_path)
    assert state['prepared_proof'] == 'listener_closed' and state['prepared_marker_verified']
    assert not state.get('prepared_health_observed') and provider.uploads == 2


def test_closed_listener_with_definitely_missing_marker_stops(tmp_path, monkeypatch):
    c = config(tmp_path)
    offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    clock = unready_prepare(monkeypatch)
    provider.marker = None
    state_path = tmp_path / 'state.json'
    with pytest.raises(setup.SetupPendingError):
        run_setup(c, state_path, provider)
    clock['now'] += setup.PREPARED_LISTENER_SECONDS + setup.PREPARED_LISTENER_MARGIN_SECONDS
    with pytest.raises(setup.SetupError, match='did not write its /data volume marker'):
        run_setup(c, state_path, provider, retry_unconfirmed=True)
    assert provider.uploads == 1


@pytest.mark.parametrize('code,diagnostic,expected', [
    (1, 'Service has no active deployment in environment x. Deploy or restart the service '
        'before using file commands', 'pending'),
    (1, 'Failed to initialize SFTP session: timeout', 'pending'),
    (1, 'Error: failed to stat remote path /.sotto-volume.json\nCaused by: No such file or directory', None),
    (1, 'permission denied private-gemini-key', 'error'),
    (0, '', 'marker'),
])
def test_railway_volume_marker_classifies_without_forwarding_output(monkeypatch, code, diagnostic, expected):
    provider = setup.Railway('project', 'environment')
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        if not code:
            Path(argv[argv.index('download') + 2]).write_text('{"schema": 1}')
        return subprocess.CompletedProcess(argv, code, '', diagnostic)

    monkeypatch.setattr(setup.subprocess, 'run', run)
    try:
        if expected == 'pending':
            with pytest.raises(setup.VolumeInspectionPendingError):
                provider.volume_marker('volume')
        elif expected == 'error':
            with pytest.raises(setup.SetupError) as raised:
                provider.volume_marker('volume')
            assert 'private-gemini-key' not in str(raised.value)
        elif expected is None:
            assert provider.volume_marker('volume') is None
        else:
            assert provider.volume_marker('volume') == {'schema': 1}
    finally:
        provider.close()
    assert seen[0][:7] == ['railway', 'volume', '--project', 'project', '--environment', 'environment', 'files']
    assert seen[0][7:11] == ['--volume', 'volume', 'download', '/.sotto-volume.json']


def test_config_is_exact_and_does_not_accept_other_state(tmp_path, monkeypatch):
    c = config(tmp_path)
    assert setup.validate(c) is None
    for changed, message in (
            ({**c, 'runtime_image': 'example.invalid/sotto@sha256:' + 'c' * 64}, 'missing or unexpected'),
            ({key: value for key, value in c.items() if key != 'source_commit'}, 'missing or unexpected'),
            ({**c, 'source_commit': 'HEAD'}, 'Source pin'),
            ({**c, 'source_path': 'relative/checkout'}, 'absolute'),
            ({**c, 'photon_line_number': '5557654321'}, 'E.164'),
            ({**c, 'sotto_runtime': {**c['sotto_runtime'], 'chat_model_id': 'bad model'}}, 'manifest is invalid')):
        with pytest.raises(setup.SetupError, match=message):
            setup.validate(changed)
    offline(monkeypatch, c)
    for schema in (1, 2):
        state_path = tmp_path / f'state-{schema}.json'
        setup.save_state(state_path, {'schema': schema, 'identity': setup.identity(c)})
        with pytest.raises(setup.SetupError, match='Saved setup state differs'):
            setup.reconcile(c, state_path, provider_factory=FakeRailway)


def test_envelope_matches_public_bootstrap_contract(tmp_path):
    c = config(tmp_path)
    state = {'service_id': '33333333-3333-3333-3333-333333333333',
             'volume_id': '44444444-4444-4444-4444-444444444444',
             'domain': 'selfhost.example.test', 'request_marker': 'request-marker-0123456789',
             'runtime_bearer': 'r' * 48, 'bridge_token': 'b' * 48, 'control_token': 'c' * 48}
    envelope = setup.launch_envelope(c, state)
    env = {'SOTTO_SELFHOST_LAUNCH': json.dumps(envelope),
           'RAILWAY_PROJECT_ID': c['project_id'], 'RAILWAY_ENVIRONMENT_ID': c['environment_id'],
           'RAILWAY_SERVICE_ID': state['service_id'], 'RAILWAY_PUBLIC_DOMAIN': state['domain'],
           'SOTTO_SETUP_REQUEST': state['request_marker'], 'RAILWAY_VOLUME_MOUNT_PATH': '/data',
           'RAILWAY_VOLUME_NAME': 'sotto-data', 'PORT': '8080'}
    assert selfhost_bootstrap._railway_envelope(env) == envelope
    with pytest.raises(selfhost_bootstrap.SelfHostBootstrapUnavailableError):
        selfhost_bootstrap._railway_envelope({**env, 'RAILWAY_SERVICE_ID': c['project_id']})


@pytest.mark.parametrize('choice', [None, False, True])
def test_protected_background_choice_reaches_real_guard_only_when_opted_in(tmp_path, monkeypatch, choice):
    c = config(tmp_path)
    c['gemini_api_key'] = 'g' * 48
    c['photon_project_secret'] = 'p' * 48
    if choice is not None:
        c['sotto_runtime']['background_unmetered'] = choice
    setup.validate(c)
    state = {'service_id': 'fixture-service', 'volume_id': 'fixture-volume',
             'domain': 'selfhost.example.test', 'request_marker': 'request-marker-0123456789',
             'runtime_bearer': 'r' * 48, 'bridge_token': 'b' * 48, 'control_token': 'c' * 48}
    bundle = setup.launch_envelope(c, state)['bundle']
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', 'true')  # ambient spending is not authority
    handoff = process_handoff.build_child_handoff(
        bundle['manifest'], bundle['receiverAuthority'], stable_identity=bundle['stableIdentity'],
        node_config_path='/data/node-config/config/runtime-config.json', data_directory='/data',
        skills_root='/app/sotto-skills', receiver_overrides=bundle['receiverOverrides'],
        limited_profile='self-host-photon-fixed-five-v1')
    assert handoff.receiver_env['SOTTO_BACKGROUND_UNMETERED'] == ('true' if choice is True else 'false')
    assert 'SOTTO_BACKGROUND_UNMETERED' not in handoff.node_env
    transport_spec = importlib.util.spec_from_file_location(
        'setup_guard_transport', ROOT / 'sotto-chief-of-staff/_shared/lib/gemini_transport.py')
    transport = importlib.util.module_from_spec(transport_spec)
    transport_spec.loader.exec_module(transport)
    monkeypatch.setattr(transport.urllib.request, 'urlopen',
                        lambda *a, **kw: pytest.fail('background guard contacted network'))
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in handoff.receiver_env.items():
        monkeypatch.setenv(key, value)
    if choice is True:
        with transport.background_learning():
            assert transport.credential() == c['gemini_api_key']
    else:
        with pytest.raises(transport.BackgroundModelHeldError) as error:
            with transport.background_learning():
                pytest.fail('unapproved background spending admitted')
        assert error.value.reason == 'background_budget_not_configured'


@pytest.mark.parametrize('choice', ['true', 'false', 0, 1, None, {}, []])
def test_background_choice_requires_json_boolean(tmp_path, choice):
    c = config(tmp_path)
    c['sotto_runtime']['background_unmetered'] = choice
    with pytest.raises(setup.SetupError, match='runtime settings'):
        setup.validate(c)


def test_same_state_second_process_refused_before_provider_contact(tmp_path):
    state_path = tmp_path / 'state.json'
    script = (
        'import importlib.util, pathlib, sys\n'
        'spec=importlib.util.spec_from_file_location("helper", sys.argv[1])\n'
        'helper=importlib.util.module_from_spec(spec)\n'
        'spec.loader.exec_module(helper)\n'
        'with helper._state_lock(pathlib.Path(sys.argv[2])):\n'
        '    print("unexpected-lock")\n'
    )
    with setup._state_lock(state_path):
        result = subprocess.run([sys.executable, '-c', script,
                                 str(Path(setup.__file__).resolve()), str(state_path)],
                                text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert 'Another Sotto setup owns' in result.stderr
    assert 'unexpected-lock' not in result.stdout
    with setup._state_lock(state_path):
        pass


def git_source(root):
    (root / 'Dockerfile').write_text('FROM scratch\n')
    (root / 'VERSION').write_text('test\n')
    (root / 'railway.toml').write_text('[build]\nbuilder = "DOCKERFILE"\ndockerfilePath = "Dockerfile"\n'
                                       '[deploy]\nhealthcheckPath = "/health"\n')
    (root / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n.venv/\n')
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                    'commit', '-qm', 'fixture'], check=True)


def test_ignored_files_do_not_dirty_source_or_reach_upload(tmp_path):
    root = tmp_path / 'public'
    root.mkdir()
    git_source(root)
    clean = setup.source_receipt(root)
    for ignored in ('__pycache__/x.pyc', '.pytest_cache/v/cache', '.venv/bin/python'):
        (root / ignored).parent.mkdir(parents=True, exist_ok=True)
        (root / ignored).write_text('ignored')
    assert setup.source_receipt(root) == clean
    staged = tmp_path / 'staged'
    staged.mkdir()
    assert setup.source_receipt(root, stage=staged) == clean
    assert sorted(str(p.relative_to(staged)) for p in staged.rglob('*') if p.is_file()) == [
        '.gitignore', 'Dockerfile', 'VERSION', 'railway.toml']
    (root / 'untracked.txt').write_text('new')
    with pytest.raises(setup.SetupError, match='dirty'):
        setup.source_receipt(root)
    (root / 'untracked.txt').unlink()
    (root / 'VERSION').write_text('changed\n')
    with pytest.raises(setup.SetupError, match='dirty'):
        setup.source_receipt(root)


def test_cli_setup_error_is_one_line_without_traceback(tmp_path):
    config_path = tmp_path / 'input.json'
    setup.save_state(config_path, {'gemini_api_key': 'private-gemini-key'})
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('setup_selfhost_railway.py')),
                             '--config', str(config_path), '--state', str(tmp_path / 'state.json')],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1 and result.stdout == ''
    assert result.stderr == 'Sotto setup stopped: Self-host input has missing or unexpected fields\n'
    assert 'private-gemini-key' not in result.stderr


def test_health_probe_uses_honest_user_agent(monkeypatch):
    seen = []
    monkeypatch.setattr(setup.urllib.request, 'urlopen', lambda request, **_k: seen.append(request) or Healthy())
    setup._health('selfhost.example.test', prepared=False)
    assert seen[0].full_url == 'https://selfhost.example.test/health'
    assert seen[0].get_header('User-agent') == 'sotto-selfhost-setup'


def test_start_commands_are_the_root_image_command_with_tini_at_the_platform_boundary():
    provider = object.__new__(setup.Railway)
    provider.environment, provider.project = 'environment', 'project'
    emitted = []
    provider.api = lambda query, variables: emitted.append(variables)
    provider.set_start('service', setup.SOTTO_PREPARE_START)
    provider.set_start('service', setup.SOTTO_RUNTIME_START)
    assert [row['i'] for row in emitted] == [
        {'startCommand': '/usr/bin/tini -- python3 -I -B /app/adapters/sotto/selfhost_bootstrap.py --prepare'},
        {'startCommand': '/usr/bin/tini -- python3 -I -B /app/adapters/sotto/selfhost_bootstrap.py --run'}]
    # Railway's custom start command replaces the image ENTRYPOINT; the runtime command is the
    # root image's own ENTRYPOINT + CMD, and the repository's railway.toml passes the source check.
    lines = (ROOT / 'Dockerfile').read_text().splitlines()
    entrypoint = json.loads(next(line for line in lines if line.startswith('ENTRYPOINT '))[11:])
    command = json.loads([line for line in lines if line.startswith('CMD ')][-1][4:])
    assert ' '.join(entrypoint + command) == setup.SOTTO_RUNTIME_START
    railway = setup.tomllib.loads((ROOT / 'railway.toml').read_text())
    assert railway['build'] == {'builder': 'DOCKERFILE', 'dockerfilePath': 'Dockerfile'}
    assert railway['deploy']['healthcheckPath'] == '/health' and 'startCommand' not in railway['deploy']
