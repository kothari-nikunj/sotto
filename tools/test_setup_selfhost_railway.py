"""Offline checks for the public Railway self-host setup boundary."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


spec = importlib.util.spec_from_file_location('setup_selfhost_railway',
                                             Path(__file__).with_name('setup_selfhost_railway.py'))
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def config(tmp_path):
    return {'project_id': '11111111-1111-1111-1111-111111111111',
            'environment_id': '22222222-2222-2222-2222-222222222222',
            'service_name': 'sotto-selfhost', 'source_path': str(tmp_path),
            'source_commit': 'a' * 40, 'source_manifest_sha256': 'b' * 64,
            'owner_phone': '+15555550101', 'photon_project_id': 'project',
            'photon_project_secret': 'private-photon-secret',
            'gemini_api_key': 'private-gemini-key'}


class FakeRailway:
    def __init__(self, project, environment):
        self.service = None
        self.volume = None
        self.domain = None
        self.volume_hidden_reads = 0
        self.domain_hidden_reads = 0
        self.vars = {}
        self.latest = None
        self.history = {}
        self.operations = []
        self.upload_fails_after_accept = False
        self.settings = {'dockerfilePath': None, 'rootDirectory': None,
                         'healthcheckPath': None, 'healthcheckTimeout': 300}

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
        return {'startCommand': None, 'source': {}, 'latestDeployment': self.latest}

    def build_settings(self, service):
        return dict(self.settings)

    def set_build_settings(self, service, settings):
        self.operations.append('set_build_settings')
        self.settings.update(settings)

    def upload(self, service, source, message):
        self.operations.append('upload')
        self.latest = {'id': 'deployment-id', 'status': 'SUCCESS',
                       'meta': {'cliMessage': message}}
        self.history['deployment-id'] = 'SUCCESS'
        if self.upload_fails_after_accept:
            raise setup.SetupError('ambiguous upload response')

    def deployment(self, identifier, service):
        return {'id': identifier, 'status': self.history[identifier], 'meta': {
            'imageDigest': 'sha256:' + 'c' * 64,
            'serviceManifest': {'deploy': {'startCommand': None}}}}

    def deployment_statuses(self, service):
        return dict(self.history)


class Healthy:
    status = 200

    def read(self, *_args):
        return json.dumps({'status': 'ok', 'work': {}, 'delivery': {}, 'model_lease': {}}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def patch_offline(monkeypatch, c):
    monkeypatch.setattr(setup, 'source_receipt', lambda *_a, **_k: {
        'source_commit': c['source_commit'],
        'source_manifest_sha256': c['source_manifest_sha256']})
    monkeypatch.setattr(setup.urllib.request, 'urlopen', lambda *_a, **_k: Healthy())


def test_setup_saves_receipts_before_deploy_and_reruns_without_replacement(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    state_path = tmp_path / 'state.json'
    first = setup.reconcile(c, state_path, provider_factory=lambda *_: provider)
    assert first['stage'] == 'receiver_reachable'
    assert first['model_status'] == 'unverified' and first['setup_status'] == 'pending'
    assert state_path.stat().st_mode & 0o077 == 0
    state = setup.protected_json(state_path)
    assert state['service_id'] == 'service-id' and state['volume_id'] == 'volume-id'
    assert state['deployment_id'] == 'deployment-id'
    assert state['image_digest'] == 'sha256:' + 'c' * 64
    assert provider.vars['PHOTON_ALLOWED_USERS'] == c['owner_phone']
    assert provider.vars['PHOTON_HOME_CHANNEL'] == c['owner_phone']
    assert provider.vars['SOTTO_CRON_DELIVER'] == 'photon'
    assert provider.vars['PHOTON_ALLOW_ALL_USERS'] == 'false'
    assert provider.vars['BRIDGE_TOKEN'] == state['bridge_token']
    assert provider.operations.index('create_volume') < provider.operations.index('upload')
    assert provider.operations.index('create_domain') < provider.operations.index('upload')
    assert provider.operations.index('set_variable:GOOGLE_AI_API_KEY') < provider.operations.index('upload')
    assert provider.operations.index('set_build_settings') < provider.operations.index('upload')
    assert 'private-photon-secret' not in json.dumps(first)
    assert 'private-gemini-key' not in json.dumps(first)
    setup.reconcile(c, state_path, provider_factory=lambda *_: provider)
    assert provider.operations.count('create_service') == 1
    assert provider.operations.count('create_volume') == 1
    assert provider.operations.count('upload') == 1


def test_slow_build_resumes_same_deployment_without_second_upload(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    original_upload = provider.upload

    def slow_upload(*args):
        original_upload(*args)
        provider.history['deployment-id'] = 'BUILDING'
        provider.latest['status'] = 'BUILDING'

    provider.upload = slow_upload
    monkeypatch.setattr(setup, 'DEPLOYMENT_POLLS', 2)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: None)
    state_path = tmp_path / 'state.json'
    with pytest.raises(setup.SetupPendingError, match='saved deployment will be reused'):
        setup.reconcile(c, state_path, provider_factory=lambda *_: provider)
    assert setup.protected_json(state_path)['deployment_id'] == 'deployment-id'
    provider.history['deployment-id'] = 'SUCCESS'
    provider.latest['status'] = 'SUCCESS'
    result = setup.reconcile(c, state_path, provider_factory=lambda *_: provider)
    assert result['stage'] == 'receiver_reachable'
    assert provider.operations.count('upload') == 1
    assert provider.operations.count('create_volume') == 1


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
    patch_offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    setup.reconcile(c, tmp_path / 'state.json', provider_factory=lambda *_: provider)
    assert provider.variables('service-id')['RAILWAY_PUBLIC_DOMAIN'] == 'selfhost.example.test'
    provider.vars['PHOTON_ALLOW_ALL_USERS'] = 'true'
    with pytest.raises(setup.SetupError, match='Existing service variables differ'):
        setup.reconcile(c, tmp_path / 'state.json', provider_factory=lambda *_: provider)


def test_lost_upload_response_resolves_exact_message_without_second_upload(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    provider.upload_fails_after_accept = True
    state_path = tmp_path / 'state.json'
    with pytest.raises(setup.SetupError, match='ambiguous upload'):
        setup.reconcile(c, state_path, provider_factory=lambda *_: provider)
    assert setup.protected_json(state_path)['source_upload_attempted'] is True
    setup.reconcile(c, state_path, provider_factory=lambda *_: provider)
    assert provider.operations.count('upload') == 1


def test_conflicts_fail_closed_without_touching_other_service_or_volume(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    provider.service = {'id': 'unowned', 'name': c['service_name']}
    with pytest.raises(setup.SetupError, match='not owned'):
        setup.reconcile(c, tmp_path / 'state.json', provider_factory=lambda *_: provider)
    assert 'create_volume' not in provider.operations
    provider.service = None
    provider.volume = {'id': 'foreign-volume', 'service_id': 'service-id', 'mount_path': '/data'}
    with pytest.raises(setup.SetupError, match='ambiguous'):
        setup.reconcile(c, tmp_path / 'state.json', provider_factory=lambda *_: provider)
    assert 'upload' not in provider.operations


def test_rejects_changed_input_and_second_active_deployment(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    state_path = tmp_path / 'state.json'
    setup.reconcile(c, state_path, provider_factory=lambda *_: provider)
    changed = {**c, 'owner_phone': '+15555550202'}
    with pytest.raises(setup.SetupError, match='differs'):
        setup.reconcile(changed, state_path, provider_factory=lambda *_: provider)
    provider.history['other-active'] = 'SUCCESS'
    with pytest.raises(setup.SetupError, match='Another runtime deployment'):
        setup.reconcile(c, state_path, provider_factory=lambda *_: provider)


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
    patch_offline(monkeypatch, c)
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 0)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    provider.create_volume = lambda service: 'unattached-volume-id'
    with pytest.raises(setup.SetupError, match='did not attach'):
        setup.reconcile(c, tmp_path / 'state.json', provider_factory=lambda *_: provider)
    assert 'upload' not in provider.operations


def test_transient_volume_and_domain_absence_resolve_without_second_creation(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: None)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    provider.volume_hidden_reads = 2
    provider.domain_hidden_reads = 2

    result = setup.reconcile(c, tmp_path / 'state.json', provider_factory=lambda *_: provider)

    assert result['stage'] == 'receiver_reachable'
    assert provider.operations.count('create_volume') == 1
    assert provider.operations.count('create_domain') == 1
    assert provider.operations.count('upload') == 1


def test_wrong_new_attachment_fails_immediately_without_upload(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: pytest.fail('wrong attachment was polled'))
    provider = FakeRailway(c['project_id'], c['environment_id'])

    def wrong_volume(_service):
        provider.operations.append('create_volume')
        provider.volume = {'id': 'volume-id', 'service_id': 'another-service', 'mount_path': '/data'}
        return 'volume-id'

    provider.create_volume = wrong_volume
    with pytest.raises(setup.SetupError, match='attached differently'):
        setup.reconcile(c, tmp_path / 'state.json', provider_factory=lambda *_: provider)
    assert 'upload' not in provider.operations


def test_saved_volume_wait_timeout_reruns_without_duplicate_mutation(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    provider.volume_hidden_reads = 100
    state_path = tmp_path / 'state.json'
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 0)

    with pytest.raises(setup.SetupError, match='did not attach'):
        setup.reconcile(c, state_path, provider_factory=lambda *_: provider)
    state = setup.protected_json(state_path)
    assert state['volume_create_attempted'] and state['volume_id'] == 'volume-id'
    assert provider.operations.count('create_volume') == 1
    assert 'upload' not in provider.operations

    provider.volume_hidden_reads = 2
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 30)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: None)
    assert setup.reconcile(c, state_path, provider_factory=lambda *_: provider)['stage'] == 'receiver_reachable'
    assert provider.operations.count('create_volume') == 1


def test_wrong_new_domain_fails_immediately_without_upload(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: pytest.fail('wrong domain was polled'))
    provider = FakeRailway(c['project_id'], c['environment_id'])

    def wrong_domain(_service):
        provider.operations.append('create_domain')
        provider.domain = {'domain': 'other.example.test', 'target_port': 8080}
        return {'domain': 'selfhost.example.test', 'targetPort': 8080}

    provider.create_domain = wrong_domain
    with pytest.raises(setup.SetupError, match='attached differently'):
        setup.reconcile(c, tmp_path / 'state.json', provider_factory=lambda *_: provider)
    assert 'upload' not in provider.operations


def test_saved_domain_wait_timeout_reruns_without_duplicate_mutation(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    provider.domain_hidden_reads = 100
    state_path = tmp_path / 'state.json'
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 0)

    with pytest.raises(setup.SetupError, match='did not attach'):
        setup.reconcile(c, state_path, provider_factory=lambda *_: provider)
    state = setup.protected_json(state_path)
    assert state['domain_create_attempted'] and state['domain'] == 'selfhost.example.test'
    assert provider.operations.count('create_domain') == 1
    assert 'upload' not in provider.operations

    provider.domain_hidden_reads = 2
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 30)
    monkeypatch.setattr(setup.time, 'sleep', lambda _: None)
    assert setup.reconcile(c, state_path, provider_factory=lambda *_: provider)['stage'] == 'receiver_reachable'
    assert provider.operations.count('create_domain') == 1


def test_receiver_health_cannot_be_any_http_200(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])

    class GenericOK(Healthy):
        def read(self, *_args):
            return b'OK'

    monkeypatch.setattr(setup.urllib.request, 'urlopen', lambda *_a, **_k: GenericOK())
    with pytest.raises(setup.SetupError, match='health endpoint'):
        setup.reconcile(c, tmp_path / 'state.json', provider_factory=lambda *_: provider)


def test_runtime_manifest_start_command_must_remain_image_default(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    original = provider.deployment

    def wrong_start(identifier, service):
        row = original(identifier, service)
        row['meta']['serviceManifest']['deploy']['startCommand'] = 'other-runtime'
        return row

    provider.deployment = wrong_start
    with pytest.raises(setup.SetupError, match='unexpected start'):
        setup.reconcile(c, tmp_path / 'state.json', provider_factory=lambda *_: provider)


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


def run_setup(c, state_path, provider):
    return setup.reconcile(c, state_path, provider_factory=lambda *_: provider)


def fail_once(monkeypatch, provider, name, *, lands, sent=True):
    """Railway call fails once: never sent (`sent=False`), or sent and it landed or not."""
    original = getattr(provider, name)

    def failing(*args):
        monkeypatch.setattr(provider, name, original)
        if not sent:
            raise setup.NotSentError('Railway CLI could not start')
        if lands:
            original(*args)
        raise setup.SetupError('Railway operation failed or timed out')

    monkeypatch.setattr(provider, name, failing)


@pytest.mark.parametrize('name', ['create_service', 'create_volume', 'create_domain', 'upload'])
@pytest.mark.parametrize('lands', [False, True])
def test_failed_railway_step_resumes_from_observed_state(tmp_path, monkeypatch, name, lands):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    monkeypatch.setattr(setup.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 0)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    fail_once(monkeypatch, provider, name, lands=lands, sent=lands)
    state_path = tmp_path / 'state.json'
    with pytest.raises(setup.SetupError, match='failed or timed out|could not start'):
        run_setup(c, state_path, provider)
    assert run_setup(c, state_path, provider)['stage'] == 'receiver_reachable'
    # A call that never landed is retried once; one that landed is adopted, never repeated.
    for step in ('create_service', 'create_volume', 'create_domain', 'upload'):
        assert provider.operations.count(step) == 1
    state = setup.protected_json(state_path)
    assert (state['service_id'], state['volume_id'], state['domain'], state['deployment_id']) == (
        'service-id', 'volume-id', 'selfhost.example.test', 'deployment-id')


@pytest.mark.parametrize('name', ['create_service', 'create_volume', 'create_domain', 'upload'])
def test_unconfirmed_mutation_is_observed_not_repeated(tmp_path, monkeypatch, capsys, name):
    """A sent request whose outcome is unknown stays pending until Railway shows it or the operator confirms."""
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    monkeypatch.setattr(setup.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(setup, 'ATTACHMENT_WAIT_SECONDS', 0)  # the observation window expires empty
    provider = FakeRailway(c['project_id'], c['environment_id'])
    fail_once(monkeypatch, provider, name, lands=False)
    state_path = tmp_path / 'state.json'
    with pytest.raises(setup.SetupError, match='failed or timed out'):
        run_setup(c, state_path, provider)
    for _ in range(2):
        with pytest.raises(setup.UnconfirmedCreateError, match='--retry-unconfirmed'):
            run_setup(c, state_path, provider)
    assert provider.operations.count(name) == 0
    # The CLI reports it as resumable, not as a failure.
    reconcile = setup.reconcile
    monkeypatch.setattr(setup, 'reconcile', lambda *a, **k: reconcile(
        *a, **{'provider_factory': lambda *_: provider, **k}))
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps(c))
    config_path.chmod(0o600)
    assert setup.main(['--config', str(config_path), '--state', str(state_path)]) == 2
    assert json.loads(capsys.readouterr().out)['stage'] == 'create_unconfirmed'
    # After checking Railway, the operator's explicit retry sends it exactly once more.
    assert setup.reconcile(c, state_path, provider_factory=lambda *_: provider,
                           retry_unconfirmed=True)['stage'] == 'receiver_reachable'
    assert provider.operations.count(name) == 1


def test_lost_create_waits_for_late_visibility_instead_of_duplicating(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
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
    patch_offline(monkeypatch, c)
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

    # A deployment that does not carry our source identity is neither adopted nor re-uploaded.
    for meta in ({}, {'cliMessage': 'someone else'}):
        state_path.unlink()
        provider = FakeRailway(c['project_id'], c['environment_id'])
        fail_once(monkeypatch, provider, 'upload', lands=False)
        with pytest.raises(setup.SetupError):
            run_setup(c, state_path, provider)
        provider.latest = {'id': 'foreign', 'status': 'SUCCESS', 'meta': meta}
        provider.history['foreign'] = 'SUCCESS'
        with pytest.raises(setup.SetupError, match='unrelated deployment|upload unresolved'):
            run_setup(c, state_path, provider)
        assert provider.operations.count('upload') == 0


def test_saved_service_missing_is_not_recreated(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    provider = FakeRailway(c['project_id'], c['environment_id'])
    state_path = tmp_path / 'state.json'
    run_setup(c, state_path, provider)
    provider.service = None
    with pytest.raises(setup.SetupError, match='Saved service is missing'):
        run_setup(c, state_path, provider)
    assert provider.operations.count('create_service') == 1


def git_source(root, *, extra_ignored=()):
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


def test_helper_matches_photon_preset_contract(tmp_path, monkeypatch):
    preset = json.loads((Path(__file__).resolve().parents[1] / 'deploy' / 'railway-photon-preset.json').read_text())
    variables = setup.desired_variables(config(tmp_path), {'request_marker': 'm', 'bridge_token': 't'})
    assert not set(variables) & set(preset['mustNotSet'])
    assert set(preset['variables']) <= set(variables)
    assert preset['service']['port'].startswith('8080')
    sent = []
    provider = setup.Railway('project', 'environment')
    monkeypatch.setattr(provider, 'api', lambda query, values: sent.append(values) or {
        'serviceDomainCreate': {'domain': 'd', 'targetPort': 8080}})
    try:
        provider.create_domain('service')
    finally:
        provider.close()
    assert sent[0]['input']['targetPort'] == 8080


def test_health_probe_uses_honest_user_agent(tmp_path, monkeypatch):
    c = config(tmp_path)
    patch_offline(monkeypatch, c)
    seen = []
    monkeypatch.setattr(setup.urllib.request, 'urlopen', lambda request, **_k: seen.append(request) or Healthy())
    run_setup(c, tmp_path / 'state.json', FakeRailway(c['project_id'], c['environment_id']))
    assert seen[0].get_header('User-agent') == 'sotto-selfhost-setup'
