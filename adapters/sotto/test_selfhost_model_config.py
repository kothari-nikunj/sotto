"""Focused offline tests for explicit direct-model renderer credentials."""
import hashlib
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('startup_config', Path(__file__).with_name('startup_config.py'))
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def fixture():
    return {
        'version': 1, 'deployment': 'self_host', 'channel': 'telegram', 'tenantId': '',
        'owner': {'accountId': 'account-fixed', 'ownerId': '123456789'},
        'lineKey': 'line-fixed', 'target': 'telegram', 'providerThreadId': 'telegram:123456789',
        'stateDirectory': '/tmp/sotto-rendered-state', 'mediaRoot': '/tmp/sotto-rendered-media',
        'instructions': 'Packaged offline instruction fixture.',
        'runtime': {'port': 19981, 'bearerToken': 'runtime-bearer-fixture-0123456789'},
        'relay': {'receiverPort': 19982, 'rootBearer': 'root-relay-fixture-0123456789abcdef'},
        'modelDirect': {'provider': 'google', 'apiKey': 'fixture-google-key-0123456789',
                        'chatModelId': 'gemini-3.8-flash', 'nativeMediaModelId': 'gemini-3.8-flash'},
        'credentials': {'telegramToken': 'fixture-bot-token'},
        'telegramLink': {'bot_token': 'fixture-bot-token', 'allowed_user': 123456789},
    }


def managed_photon_fixture():
    value = fixture()
    project = 'photon-project-fixture'
    value.update(deployment='managed', tenantId='tenant-fixed', channel='photon', target='photon',
                 owner={'accountId': 'account-fixed', 'ownerId': '+15551234567'},
                 providerThreadId='imessage:iMessage;-;+15551234567~+15557654321',
                 photonLineNumber='+15557654321',
                 credentials={'mode': 'cloud', 'photonProjectId': project,
                              'photonProjectSecret': 'photon-project-secret-fixture'},
                 activationReceipt={'tenant_id': 'tenant-fixed', 'owner': '+15551234567',
                                    'chat_id': 'iMessage;-;+15551234567', 'activated': True,
                                    'project_fingerprint': hashlib.sha256(
                                        ('sotto-photon-project-v1:' + project).encode()).hexdigest()})
    value.pop('telegramLink')
    return value


class SelfHostModelConfigTests(unittest.TestCase):
    def test_direct_google_is_self_host_only_and_node_config_is_exact(self):
        manifest = fixture()
        config, _ = module.render(manifest)
        self.assertEqual(config['deployment'], 'self_host')
        self.assertEqual(config['modelDirect'], manifest['modelDirect'])
        self.assertNotIn('modelProxy', config)

        managed = managed_photon_fixture()
        with self.assertRaisesRegex(module.ManifestError, 'direct model credentials require self-host'):
            module.render(managed)

    def test_exactly_one_model_configuration_and_managed_proxy_compatibility(self):
        for mutation in ('both', 'none'):
            manifest = fixture()
            if mutation == 'both':
                manifest['modelProxy'] = {'origin': 'https://proxy.example', 'bearer': 'proxy-fixture',
                                          'chatModelId': 'chat-v1', 'nativeMediaModelId': 'media-v1'}
            else:
                manifest.pop('modelDirect')
            with self.assertRaisesRegex(module.ManifestError, 'exactly one explicit model'):
                module.render(manifest)

        managed = managed_photon_fixture()
        managed.pop('modelDirect')
        managed['modelProxy'] = {'origin': 'https://proxy.example', 'bearer': 'proxy-fixture',
                                 'chatModelId': 'chat-v1', 'nativeMediaModelId': 'media-v1'}
        config, _ = module.render(managed)
        self.assertEqual(config['modelProxy'], managed['modelProxy'])
        self.assertNotIn('modelDirect', config)

    def test_secret_reuse_character_and_model_bounds_are_rejected(self):
        manifest = fixture()
        manifest['modelDirect']['apiKey'] = manifest['relay']['rootBearer']
        with self.assertRaisesRegex(module.ManifestError, 'relay root cannot be reused'):
            module.render(manifest)
        for api_key in ('', ' contains-space', 'has\nnewline', 'x' * 4097):
            manifest = fixture()
            manifest['modelDirect']['apiKey'] = api_key
            with self.assertRaises(module.ManifestError):
                module.render(manifest)
        for model_id in ('', 'bad/model', 'x' * 129):
            manifest = fixture()
            manifest['modelDirect']['chatModelId'] = model_id
            with self.assertRaises(module.ManifestError):
                module.render(manifest)
        manifest = fixture()
        manifest['modelDirect']['provider'] = 'openai'
        with self.assertRaisesRegex(module.ManifestError, 'direct model provider is unsupported'):
            module.render(manifest)


if __name__ == '__main__':
    unittest.main()
