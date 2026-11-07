"""Pure child-boundary tests; no process, provider, socket, or credential access."""
import copy
import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name('startup_config.py')
sys.path.insert(0, str(SOURCE.parent))
HANDOFF_SPEC = importlib.util.spec_from_file_location('process_handoff', Path(__file__).with_name('process_handoff.py'))
handoff_module = importlib.util.module_from_spec(HANDOFF_SPEC)
HANDOFF_SPEC.loader.exec_module(handoff_module)
HandoffError = handoff_module.HandoffError
build_child_handoff = handoff_module.build_child_handoff

SPEC = importlib.util.spec_from_file_location('startup_config', SOURCE)
startup_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(startup_config)


def photon_manifest():
    project = 'synthetic-project'
    root_token = 'root-mcp-fixture-token-0123456789'
    return {
        'version': 1, 'deployment': 'managed', 'channel': 'photon', 'tenantId': 'tenant-fixture',
        'owner': {'accountId': 'account-fixture', 'ownerId': '+15551234567'},
        'lineKey': 'physical-line-a', 'target': 'photon',
        'providerThreadId': 'imessage:iMessage;-;+15551234567~+15557654321',
        'photonLineNumber': '+15557654321',
        'stateDirectory': '/data/runtime/state', 'mediaRoot': '/data/runtime/media',
        'instructions': 'Synthetic test instruction.',
        'runtime': {'port': 19981, 'bearerToken': 'runtime-api-fixture-token-0123456789'},
        'relay': {'receiverPort': 19982, 'rootBearer': root_token},
        'modelProxy': {'origin': 'https://proxy.example', 'bearer': 'model-proxy-fixture-token-0123456789',
                       'chatModelId': 'chat-v1', 'nativeMediaModelId': 'media-v1'},
        'credentials': {'mode': 'cloud', 'photonProjectId': project,
                        'photonProjectSecret': 'photon-project-secret-fixture'},
        'activationReceipt': {
            'tenant_id': 'tenant-fixture', 'owner': '+15551234567',
            'chat_id': 'iMessage;-;+15551234567', 'activated': True,
            'project_fingerprint': hashlib.sha256(('sotto-photon-project-v1:' + project).encode()).hexdigest(),
        },
    }


def selfhost_photon_manifest():
    value = photon_manifest()
    value.update(deployment='self_host', stateDirectory='/data/node-state', mediaRoot='/data/node-media',
                 firstContact={'mode': 'owner_dm', 'expectedProviderLine': '+15557654321'},
                 modelDirect={'provider': 'google', 'apiKey': 'direct-google-fixture-0123456789',
                              'chatModelId': 'gemini-chat-fixture',
                              'nativeMediaModelId': 'gemini-media-fixture'})
    for key in ('providerThreadId', 'activationReceipt', 'modelProxy'):
        value.pop(key)
    return value


def authorities(config):
    return {
        'SOTTO_MCP_TOKEN': 'root-mcp-fixture-token-0123456789',
        'SOTTO_CONTROL_TOKEN': 'control-plane-fixture-token-0123456789',
        'SOTTO_MODEL_PROXY_URL': config['modelProxy']['origin'],
        'SOTTO_MODEL_PROXY_TOKEN': config['modelProxy']['bearer'],
        'SOTTO_TENANT_ID': config['tenantId'],
        'SOTTO_DATA': '/data', 'SOTTO_HOME': '/data/sotto-home',
        'SOTTO_VOLUME_ID': 'synthetic-volume-id',
        # A supplied extra is deliberately rejected rather than copied to either child.
    }


def stable_identity(config):
    return {
        'channel': config['channel'], 'target': config['target'],
        'tenantId': config.get('tenantId', ''), 'accountId': config['owner']['accountId'],
        'ownerId': config['owner']['ownerId'], 'lineKey': config['lineKey'],
        'providerThreadId': config.get('providerThreadId'),
        'expectedProviderLine': (config.get('firstContact') or {}).get('expectedProviderLine'),
        'projectFingerprint': hashlib.sha256(
            ('sotto-photon-project-v1:' + config['credentials']['photonProjectId']).encode()).hexdigest(),
    }


class ProcessHandoffTests(unittest.TestCase):
    def test_selfhost_telegram_owner_command_binds_bot_and_private_bearer(self):
        # Standalone lifecycle fixtures import this module without pytest installed.
        from test_telegram_launcher import fixture as telegram_fixture  # noqa: PLC0415
        manifest, authority, identity = telegram_fixture()
        config, _ = startup_config.render(manifest)

        def build(candidate=manifest, source=authority, stable=identity):
            return build_child_handoff(candidate, source, stable_identity=stable,
                                       node_config_path='/data/node-config/config/runtime-config.json',
                                       data_directory='/data', skills_root='/app/sotto-skills',
                                       limited_profile='self-host-telegram-fixed-five-v1')

        handoff = build()
        command = config['ownerLoop']
        self.assertEqual(command['identity']['channel'], 'telegram')
        self.assertEqual(command['identity']['route_id'], 'telegram:8675309')
        self.assertEqual(command['identity']['bot_fingerprint'], identity['botFingerprint'])
        self.assertEqual(handoff.receiver_env['SOTTO_RUNTIME_PROFILE'], 'self-host-telegram-fixed-five-v1')
        self.assertNotIn(command['bearer'], handoff.receiver_env.values())
        self.assertNotIn(command['bearer'], handoff.node_env.values())
        self.assertNotIn('SOTTO_MODEL_PROXY_URL', handoff.receiver_env)
        changed = copy.deepcopy(manifest)
        changed['credentials']['telegramToken'] = 'different-bot-token'
        changed['telegramLink']['bot_token'] = 'different-bot-token'
        with self.assertRaises(HandoffError):
            build(candidate=changed)
        with self.assertRaises(HandoffError):
            build(stable={**identity, 'ownerId': '9876543'})
        with self.assertRaises(HandoffError):
            build(source={**authority, 'SOTTO_MODEL_PROXY_TOKEN': 'forbidden'})

    def test_selfhost_photon_owner_command_uses_private_derived_bearer_without_proxy(self):
        manifest = selfhost_photon_manifest()
        config, _ = startup_config.render(manifest)
        authority = {'SOTTO_MCP_TOKEN': manifest['relay']['rootBearer'],
                     'SOTTO_CONTROL_TOKEN': 'control-plane-fixture-token-0123456789',
                     'SOTTO_TENANT_ID': manifest['tenantId'], 'SOTTO_DATA': '/data',
                     'SOTTO_HOME': '/data/sotto-home', 'SOTTO_VOLUME_ID': 'synthetic-volume-id'}
        def build(candidate=manifest, source=authority, overrides=None):
            return build_child_handoff(candidate, source,
                                       stable_identity=stable_identity(config),
                                       node_config_path='/data/node-config/config/runtime-config.json',
                                       data_directory='/data', skills_root='/app/sotto-skills',
                                       limited_profile='self-host-photon-fixed-five-v1',
                                       receiver_overrides=overrides)

        handoff = build()
        self.assertEqual(config['ownerLoop']['identity']['route_id'], '+15557654321')
        self.assertEqual(handoff.receiver_env['SOTTO_RUNTIME_PROFILE'], 'self-host-photon-fixed-five-v1')
        self.assertEqual(handoff.receiver_env['GOOGLE_AI_API_KEY'], manifest['modelDirect']['apiKey'])
        self.assertNotIn('SOTTO_MODEL_PROXY_URL', handoff.receiver_env)
        self.assertNotIn(config['ownerLoop']['bearer'], handoff.receiver_env.values())
        self.assertNotIn(config['ownerLoop']['bearer'], handoff.node_env.values())
        self.assertNotIn('SOTTO_BACKGROUND_UNMETERED', handoff.receiver_env)
        for choice in ('true', 'false'):
            selected = build(overrides={'SOTTO_BACKGROUND_UNMETERED': choice})
            self.assertEqual(selected.receiver_env['SOTTO_BACKGROUND_UNMETERED'], choice)
            self.assertNotIn('SOTTO_BACKGROUND_UNMETERED', selected.node_env)
        for choice in ('1', 'TRUE', '', True, None):
            with self.assertRaises(HandoffError):
                build(overrides={'SOTTO_BACKGROUND_UNMETERED': choice})
        changed = copy.deepcopy(manifest)
        changed['credentials']['photonProjectId'] = 'other-project'
        with self.assertRaises(HandoffError):
            build(changed)
        with self.assertRaises(HandoffError):
            build(source={**authority, 'SOTTO_MODEL_PROXY_TOKEN': 'forbidden'})

    def setUp(self):
        self.manifest = photon_manifest()
        self.config, self.projection = startup_config.render(self.manifest)
        self.authority = authorities(self.config)

    def test_managed_background_spending_cannot_bypass_proxy(self):
        for choice in ('true', 'false'):
            with self.assertRaises(HandoffError):
                build_child_handoff(
                    self.manifest, self.authority, stable_identity=stable_identity(self.config),
                    node_config_path='/run/sotto-node/runtime-config.json', data_directory='/data',
                    skills_root='/app/sotto-skills',
                    receiver_overrides={'SOTTO_BACKGROUND_UNMETERED': choice})

    def build(self, **changes):
        return build_child_handoff(
            changes.get('manifest', self.manifest), changes.get('authority', self.authority),
            stable_identity=changes.get('identity', stable_identity(self.config)),
            node_config_path='/run/sotto-node/runtime-config.json', data_directory='/data',
            skills_root='/app/sotto-skills',
            receiver_overrides=changes.get('overrides'),
            public_receiver_authority=changes.get('public'),
            trusted_service_id=changes.get('service_id'),
        )

    def test_explicit_verified_public_attachment_only_changes_receiver_environment(self):
        local = self.build()
        attachment = {'tenantId': 'tenant-fixture', 'serviceId': 'service-fixture',
                      'domain': 'synthetic.up.railway.app', 'targetPort': 19982}
        public = self.build(public=attachment, service_id='service-fixture')
        self.assertEqual(dict(public.node_env), dict(local.node_env))
        self.assertEqual(public.runtime_config_sha256, local.runtime_config_sha256)
        self.assertEqual(public.receiver_env['PORT'], '19982')
        self.assertEqual(public.receiver_env['RAILWAY_PUBLIC_DOMAIN'], 'synthetic.up.railway.app')
        self.assertNotIn('PORT', local.receiver_env)
        self.assertNotIn('RAILWAY_PUBLIC_DOMAIN', local.receiver_env)
        self.assertNotIn('RAILWAY_PUBLIC_DOMAIN', public.node_env)

    def test_public_attachment_rejects_unverified_or_mismatched_provider_metadata(self):
        good = {'tenantId': 'tenant-fixture', 'serviceId': 'service-fixture',
                'domain': 'synthetic.up.railway.app', 'targetPort': 19982}
        with self.assertRaises(HandoffError):
            self.build(public=good)  # No separately trusted service identity.
        with self.assertRaises(HandoffError):
            self.build(public=good, service_id='other-service')
        for key, value in (
                ('tenantId', 'other-tenant'), ('serviceId', 'other-service'),
                ('targetPort', 8080), ('targetPort', True), ('targetPort', '19982'),
                ('domain', 'https://synthetic.up.railway.app'),
                ('domain', 'synthetic.up.railway.app:8080'),
                ('domain', 'synthetic.up.railway.app/evil'),
                ('domain', 'Synthetic.up.railway.app'),
                ('domain', 'bad-.railway.app'),
                ('domain', 'synthetic.up.railway.app\nSOTTO_MCP_TOKEN=stolen')):
            with self.subTest(key=key, value=value), self.assertRaises(HandoffError):
                self.build(public={**good, key: value}, service_id='service-fixture')
        with self.assertRaises(HandoffError):
            self.build(public={**good, 'extra': 'unexpected'}, service_id='service-fixture')

    def test_projection_separates_native_receiver_authority_from_node_channel_authority(self):
        handoff = self.build()
        expected_digest = hashlib.sha256(startup_config._payload_bytes(self.config)).hexdigest()
        self.assertEqual(handoff.runtime_config_sha256, expected_digest)
        self.assertEqual(dict(handoff.receiver_channel_expectation),
                         startup_config.receiver_channel_expectation(self.config))
        self.assertNotIn(self.config['bearerToken'], repr(handoff))
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / 'receiver-channel'
            startup_config.stage_receiver_channel_expectation(
                destination, handoff.receiver_channel_expectation)
            self.assertEqual(startup_config.read_receiver_channel_expectation(
                destination / 'expected-channel.json'),
                dict(handoff.receiver_channel_expectation))
        self.assertEqual(handoff.node_argv[-2:], ('--config', '/run/sotto-node/runtime-config.json'))
        self.assertEqual(handoff.receiver_argv[-1], '/app/trigger-receiver/receiver.py')
        self.assertEqual(handoff.receiver_argv[1:3], ('/app/adapters/sotto/managed_exec.py', 'receiver'))
        self.assertEqual((handoff.node_launch_uid, handoff.receiver_wrapper_uid,
                          handoff.receiver_workload_uid), (10000, 0, 10001))
        self.assertEqual(handoff.node_env['HOME'], '/home/sotto-runtime')
        self.assertEqual(handoff.receiver_env['HOME'], '/home/sotto')
        self.assertNotIn('PHOTON_PROJECT_SECRET', handoff.node_env)
        self.assertNotIn('SOTTO_MCP_TOKEN', handoff.node_env)
        self.assertNotIn('SOTTO_CONTROL_TOKEN', handoff.node_env)
        self.assertNotIn('SOTTO_MODEL_PROXY_TOKEN', handoff.node_env)
        self.assertNotIn('BRIDGE_TOKEN', handoff.node_env)
        self.assertEqual(handoff.receiver_env['PHOTON_PROJECT_SECRET'],
                         'photon-project-secret-fixture')  # needed by existing activation writer
        self.assertNotIn('BRIDGE_TOKEN', handoff.receiver_env)
        self.assertEqual(handoff.receiver_env['SOTTO_MCP_TOKEN'], self.authority['SOTTO_MCP_TOKEN'])
        command = self.config['ownerLoop']
        self.assertEqual(command['mode'], 'explicit_command')
        self.assertEqual(command['identity']['route_mode'], 'fixed')
        self.assertEqual(command['identity']['route_id'], self.config['providerThreadId'])
        self.assertRegex(command['bearer'], r'^[a-f0-9]{64}$')
        self.assertNotEqual(command['bearer'], self.authority['SOTTO_MCP_TOKEN'])
        self.assertNotIn(command['bearer'], handoff.node_env.values())
        self.assertNotIn(command['bearer'], handoff.receiver_env.values())
        self.assertEqual(handoff.receiver_env['SOTTO_CONTROL_TOKEN'], self.authority['SOTTO_CONTROL_TOKEN'])
        self.assertEqual(handoff.receiver_env['SOTTO_MODEL_PROXY_URL'], self.config['modelProxy']['origin'])
        self.assertEqual(handoff.receiver_env['SOTTO_MODEL_PROXY_TOKEN'], self.config['modelProxy']['bearer'])
        self.assertNotIn('SOTTO_RUNTIME', handoff.receiver_env)  # Sotto is the only runtime.
        self.assertEqual(handoff.receiver_env['SOTTO_CRON_DELIVER'], 'photon')
        self.assertEqual(handoff.receiver_env['SOTTO_TRIGGER_PORT'], '19982')
        self.assertNotIn('PORT', handoff.receiver_env)  # PORT would make receiver bind publicly.
        self.assertEqual(handoff.receiver_env['SOTTO_IMESSAGE_NUMBER'], '+15557654321')
        self.assertEqual(handoff.receiver_env['PHOTON_HOME_CHANNEL'], '+15551234567')
        self.assertEqual(handoff.receiver_env['PHOTON_ALLOWED_USERS'], '+15551234567')
        self.assertEqual(handoff.receiver_env['PHOTON_PROJECT_ID'], 'synthetic-project')
        self.assertEqual(handoff.receiver_env['PHOTON_PROJECT_SECRET'], 'photon-project-secret-fixture')
        self.assertEqual(handoff.receiver_env['SOTTO_VOLUME_ID'], 'synthetic-volume-id')
        self.assertEqual(handoff.node_env['PATH'], '/usr/local/bin:/usr/bin:/bin')
        self.assertEqual(handoff.receiver_env['PATH'], '/usr/local/bin:/usr/bin:/bin')
        self.assertNotIn('PYTHONPATH', handoff.receiver_env)
        self.assertNotIn('NODE_OPTIONS', handoff.node_env)
        self.assertIn('environment values redacted', repr(handoff))
        self.assertNotIn(self.authority['SOTTO_CONTROL_TOKEN'], repr(handoff))

    def test_exact_activation_and_local_routes_are_required(self):
        for port in (19981, 0, 65536):
            bad = dict(self.manifest)
            bad['relay'] = dict(bad['relay'], receiverPort=port)
            with self.assertRaises(HandoffError):
                build_child_handoff(bad, self.authority, stable_identity=stable_identity(self.config),
                                    node_config_path='/run/config', data_directory='/data', skills_root='/app/skills')
        changed = {**self.authority, 'SOTTO_MCP_TOKEN': 'different-root-token-012345678901'}
        with self.assertRaises(HandoffError):
            self.build(authority=changed)

    def test_identity_changes_fail_closed(self):
        altered_identity = dict(stable_identity(self.config), ownerId='+15550000000')
        with self.assertRaises(HandoffError):
            self.build(identity=altered_identity)
        for field, changed in (('target', 'telegram'), ('tenantId', 'other-tenant'),
                               ('lineKey', 'other-line'), ('providerThreadId', 'imessage:other-chat')):
            manifest = dict(self.manifest)
            manifest[field] = changed
            with self.assertRaises(HandoffError):
                build_child_handoff(manifest, self.authority,
                                    stable_identity=stable_identity(self.config),
                                    node_config_path='/run/config', data_directory='/data', skills_root='/app/skills')
        changed_project = copy.deepcopy(self.manifest)
        changed_project['credentials']['photonProjectId'] = 'different-project'
        changed_project['activationReceipt']['project_fingerprint'] = hashlib.sha256(
            b'sotto-photon-project-v1:different-project').hexdigest()
        with self.assertRaises(HandoffError):
            build_child_handoff(changed_project, self.authority,
                                stable_identity=stable_identity(self.config),
                                node_config_path='/run/config', data_directory='/data', skills_root='/app/skills')
        malformed = dict(self.manifest, firstContact='untrusted')
        with self.assertRaises(HandoffError):
            build_child_handoff(malformed, self.authority,
                                stable_identity=stable_identity(self.config),
                                node_config_path='/run/config', data_directory='/data', skills_root='/app/skills')

    def test_environment_is_allowlisted_and_proxy_authority_matches(self):
        for extra in ({'AWS_SECRET_ACCESS_KEY': 'synthetic-secret'},):
            with self.assertRaises(HandoffError):
                self.build(authority={**self.authority, **extra})
        with self.assertRaises(HandoffError):
            self.build(authority={**self.authority, 'SOTTO_MODEL_PROXY_URL': 'https://other.example'})
        with self.assertRaises(HandoffError):
            self.build(authority={**self.authority, 'SOTTO_TENANT_ID': 'other-tenant'})
        with self.assertRaises(HandoffError):
            self.build(authority={**self.authority,
                                  'SOTTO_CONTROL_TOKEN': self.authority['SOTTO_MCP_TOKEN']})
        with self.assertRaises(HandoffError) as caught:
            self.build(authority={**self.authority, 'SOTTO_MCP_TOKEN': 'x' * 32 + '\ud800'})
        self.assertNotIn('x' * 32, str(caught.exception))
        with self.assertRaises(HandoffError):
            build_child_handoff(self.manifest, self.authority,
                                stable_identity=stable_identity(self.config),
                                node_config_path='/run/bad\ud800', data_directory='/data',
                                skills_root='/app/sotto-skills')

    def test_explicit_native_overrides_are_preserved_and_never_discovered(self):
        handoff = self.build(overrides={
            'SOTTO_BRIEF_MODEL': 'gemini-3.8-flash',
            'SOTTO_GEMINI_MODEL': 'gemini-3.9-pro-preview',
            'SOTTO_FALLBACK_MODEL': 'gemini-3-flash-preview',
            'SOTTO_RESEARCH_DEEP': '0',
            'SOTTO_TIMEZONE': 'America/Los_Angeles',
        })
        for key, expected in {
            'SOTTO_BRIEF_MODEL': 'gemini-3.8-flash',
            'SOTTO_GEMINI_MODEL': 'gemini-3.9-pro-preview',
            'SOTTO_FALLBACK_MODEL': 'gemini-3-flash-preview',
            'SOTTO_RESEARCH_DEEP': '0',
            'SOTTO_TIMEZONE': 'America/Los_Angeles',
        }.items():
            self.assertEqual(handoff.receiver_env[key], expected)
        no_override = self.build()
        for key in ('SOTTO_BRIEF_MODEL', 'SOTTO_GEMINI_MODEL', 'SOTTO_FALLBACK_MODEL',
                    'SOTTO_RESEARCH_DEEP', 'SOTTO_TIMEZONE', 'TZ'):
            self.assertNotIn(key, no_override.receiver_env)
        for invalid in ({'SOTTO_GEMINI_MODEL': 'gemini flash'},
                        {'SOTTO_RESEARCH_DEEP': 'false'},
                        {'SOTTO_TIMEZONE': 'Not/A_Real_Zone'},
                        {'SOTTO_UNDECLARED': 'value'}):
            with self.assertRaises(HandoffError):
                self.build(overrides=invalid)

    def test_self_host_projection_is_outside_managed_proxy_handoff_scope(self):
        with self.assertRaises(HandoffError):
            self.build(manifest={**self.manifest, 'deployment': 'self_host'})
        local_proxy = {**self.manifest, 'modelProxy': {
            **self.manifest['modelProxy'], 'origin': 'http://127.0.0.1:9443'}}
        local_config, _ = startup_config.render(local_proxy)
        with self.assertRaises(HandoffError):
            build_child_handoff(local_proxy, authorities(local_config),
                                stable_identity=stable_identity(local_config),
                                node_config_path='/run/config', data_directory='/data',
                                skills_root='/app/skills')

    def test_first_contact_uses_explicit_receiver_line_without_a_guessed_thread(self):
        manifest = photon_manifest()
        manifest.pop('providerThreadId')
        manifest.pop('activationReceipt')
        manifest['firstContact'] = {'mode': 'owner_dm', 'expectedProviderLine': '+15557654321'}
        config, _projection = startup_config.render(manifest)
        handoff = build_child_handoff(
            manifest, authorities(config), stable_identity=stable_identity(config),
            node_config_path='/run/sotto-node/runtime-config.json', data_directory='/data',
            skills_root='/app/sotto-skills')
        self.assertNotIn('providerThreadId', config)
        self.assertEqual(handoff.receiver_env['SOTTO_IMESSAGE_NUMBER'], '+15557654321')


if __name__ == '__main__':
    unittest.main()


def test_shared_project_handoff_requires_explicit_stable_mode_and_keeps_display_line():
    manifest = selfhost_photon_manifest()
    manifest['firstContact'] = {'mode': 'owner_dm_shared_project'}
    config, _ = startup_config.render(manifest)
    identity = stable_identity(config)
    identity['firstContactMode'] = 'owner_dm_shared_project'
    authority = {'SOTTO_MCP_TOKEN': manifest['relay']['rootBearer'],
                 'SOTTO_CONTROL_TOKEN': 'control-plane-fixture-token-0123456789',
                 'SOTTO_TENANT_ID': manifest['tenantId'], 'SOTTO_DATA': '/data',
                 'SOTTO_HOME': '/data/sotto-home', 'SOTTO_VOLUME_ID': 'synthetic-volume-id'}
    result = build_child_handoff(manifest, authority, stable_identity=identity,
        node_config_path='/data/node-config/config/runtime-config.json',
        data_directory='/data', skills_root='/app/sotto-skills', receiver_overrides={},
        limited_profile='self-host-photon-fixed-five-v1')
    assert result.receiver_env['SOTTO_IMESSAGE_NUMBER'] == manifest['photonLineNumber']
    assert result.receiver_env['SOTTO_IMESSAGE_NUMBER'] != 'shared'
    for key in ('firstContactMode', 'projectFingerprint', 'ownerId'):
        changed = dict(identity)
        changed[key] = 'wrong'
        with unittest.TestCase().assertRaises(HandoffError):
            build_child_handoff(manifest, authority, stable_identity=changed,
                node_config_path='/data/node-config/config/runtime-config.json',
                data_directory='/data', skills_root='/app/sotto-skills', receiver_overrides={},
                limited_profile='self-host-photon-fixed-five-v1')
