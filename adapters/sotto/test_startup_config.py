"""Offline fixture tests; no provider, Keychain, or live receiver access."""
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('startup_config', Path(__file__).with_name('startup_config.py'))
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def fixture(channel='telegram'):
    base = {
        'version': 1, 'deployment': 'self_host', 'channel': channel, 'tenantId': '',
        'owner': {'accountId': 'account-fixed', 'ownerId': '123456789'},
        'lineKey': 'line-fixed', 'target': channel, 'providerThreadId': 'telegram:123456789',
        'stateDirectory': '/tmp/sotto-rendered-state', 'mediaRoot': '/tmp/sotto-rendered-media',
        'instructions': 'Packaged offline instruction fixture.',
        'runtime': {'port': 19981, 'bearerToken': 'runtime-bearer-only-12345678901234567890'},
        'relay': {'receiverPort': 19982, 'rootBearer': 'root-relay-never-output-1234567890'},
        'modelProxy': {'origin': 'https://proxy.example', 'bearer': 'model-bearer-fixture',
                       'chatModelId': 'chat-v1', 'nativeMediaModelId': 'native-v1'},
        'credentials': {'telegramToken': 'bot-fixture'},
        'telegramLink': {'bot_token': 'bot-fixture', 'allowed_user': 123456789},
    }
    if channel == 'photon':
        project = 'project-fixture'
        base.update(deployment='managed', tenantId='tenant-fixed',
                    owner={'accountId': 'account-fixed', 'ownerId': '+15551234567'},
                    providerThreadId='imessage:iMessage;-;+15551234567~+15557654321',
                    photonLineNumber='+15557654321',
                    credentials={'mode': 'cloud', 'photonProjectId': project,
                                 'photonProjectSecret': 'project-secret-fixture'},
                    activationReceipt={'tenant_id': 'tenant-fixed', 'owner': '+15551234567',
                                       'chat_id': 'iMessage;-;+15551234567', 'activated': True,
                                       'project_fingerprint': hashlib.sha256(
                                           ('sotto-photon-project-v1:' + project).encode()).hexdigest()})
        del base['telegramLink']
    return base


def first_contact_fixture():
    value = fixture('photon')
    value.pop('providerThreadId')
    value.pop('activationReceipt')
    value['firstContact'] = {'mode': 'owner_dm', 'expectedProviderLine': value['photonLineNumber']}
    return value


def selfhost_photon_fixture():
    value = first_contact_fixture()
    value['deployment'] = 'self_host'
    value['modelDirect'] = {'provider': 'google', 'apiKey': 'direct-key-fixture-0123456789',
                            'chatModelId': 'gemini-chat-fixture',
                            'nativeMediaModelId': 'gemini-media-fixture'}
    value.pop('modelProxy')
    return value


class StartupConfigTests(unittest.TestCase):
    def test_v40_persisted_managed_config_restarts_without_rewriting(self):
        # Golden bytes come from v40's renderer, not the current capability text.
        golden = json.loads(Path(__file__).with_name('test_startup_config_v40_golden.json').read_text())
        config, receiver = module.render(golden['manifest'])
        self.assertEqual(config, golden['config'])
        self.assertEqual(receiver, golden['receiver_projection'])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'config'
            module.stage(output, golden['config'], golden['receiver_projection'])
            before = {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                      for p in output.iterdir()}
            module.stage(output, config, receiver)
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                                      for p in output.iterdir()})
            changed = dict(config, instructions=config['instructions'] + '\nChanged capability text.')
            with self.assertRaisesRegex(module.ManifestError, 'staging destination conflicts'):
                module.stage(output, changed, receiver)
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                                      for p in output.iterdir()})

    def test_selfhost_photon_direct_owner_commands_preserve_photon_identity_and_persona(self):
        value = selfhost_photon_fixture()
        value['instructions'] = 'Owner persona.\nSpeak plainly.'
        config, _ = module.render(value)
        self.assertEqual(config['instructions'], value['instructions'] + '\n\n' +
                         module.SELF_HOST_PHOTON_CAPABILITIES)
        self.assertEqual(config['ownerLoop']['identity']['route_id'], value['photonLineNumber'])
        self.assertEqual(config['ownerLoop']['identity']['route_mode'], 'first_contact')
        self.assertEqual(config['ownerLoop']['identity']['project_fingerprint'], hashlib.sha256(
            ('sotto-photon-project-v1:' + value['credentials']['photonProjectId']).encode()).hexdigest())
        self.assertNotEqual(config['ownerLoop']['bearer'], config['bridge']['readBearer'])
        self.assertNotIn(value['relay']['rootBearer'], json.dumps(config))
        activated = fixture('photon')
        activated.update(deployment='self_host', modelDirect=value['modelDirect'])
        activated.pop('modelProxy')
        fixed, _ = module.render(activated)
        self.assertEqual(fixed['ownerLoop']['identity']['route_mode'], 'fixed')
        self.assertEqual(fixed['ownerLoop']['identity']['route_id'], activated['providerThreadId'])
        invalid = selfhost_photon_fixture()
        invalid['tenantId'] = ''
        with self.assertRaisesRegex(module.ManifestError, 'tenant identity'):
            module.render(invalid)
        telegram = fixture()
        telegram['tenantId'] = 'tenant-fixed'
        telegram['modelDirect'] = value['modelDirect']
        telegram.pop('modelProxy')
        telegram_config, _ = module.render(telegram)
        self.assertEqual(telegram_config['ownerLoop']['identity']['route_id'], 'telegram:123456789')
        self.assertEqual(telegram_config['ownerLoop']['identity']['bot_fingerprint'], hashlib.sha256(
            ('sotto-telegram-bot-v1:' + telegram['credentials']['telegramToken']).encode()).hexdigest())
        self.assertNotEqual(telegram_config['ownerLoop']['bearer'], config['ownerLoop']['bearer'])
        self.assertTrue(telegram_config['instructions'].endswith(module.SELF_HOST_TELEGRAM_CAPABILITIES))
        missing_tenant = {**telegram, 'tenantId': ''}
        self.assertNotIn('ownerLoop', module.render(missing_tenant)[0])

    def test_managed_photon_capability_guidance_preserves_operator_prose(self):
        value = fixture('photon')
        value['instructions'] = 'Owner persona.\nSpeak plainly.\n'
        config, _ = module.render(value)
        self.assertEqual(config['instructions'], value['instructions'] + '\n\n' + module.MANAGED_PHOTON_CAPABILITIES)
        for phrase in ('`/loop add <text>`', '`owner_open_loops`', '`/loop close <anchor_key>`',
                       '`/reminder at <ISO8601-with-offset> <text>`', '`/reminder list`',
                       '`/reminder cancel <int_id>`', '15-minute',
                       '`/quiet for <Nh|Nm>`', '`/quiet off`',
                       '`/stop`', '`/new`', 'fixed schedules', 'change other preferences',
                       'write Gmail or Calendar', 'send through Bridge', 'external draft',
                       'confirmed receipt'):
            self.assertIn(phrase, config['instructions'])
        self.assertIn('ownerLoop', config)
        first_contact, _ = module.render(first_contact_fixture())
        self.assertTrue(first_contact['instructions'].endswith(module.MANAGED_PHOTON_CAPABILITIES))

        telegram = fixture()
        telegram['instructions'] = value['instructions']
        self.assertEqual(module.render(telegram)[0]['instructions'], telegram['instructions'])
        self_host_photon = fixture('photon')
        self_host_photon['deployment'] = 'self_host'
        self.assertEqual(module.render(self_host_photon)[0]['instructions'], self_host_photon['instructions'])

    def test_managed_photon_combined_instruction_limit_is_explicit(self):
        suffix = '\n\n' + module.MANAGED_PHOTON_CAPABILITIES
        value = fixture('photon')
        value['instructions'] = 'a' * (32000 - module._utf16_units(suffix))
        self.assertEqual(module._utf16_units(module.render(value)[0]['instructions']), 32000)
        value['instructions'] += 'a'
        with self.assertRaisesRegex(module.ManifestError, '^instructions are invalid$'):
            module.render(value)

    def test_receiver_owned_channel_expectation_is_private_and_secret_free(self):
        for value in (fixture('photon'), first_contact_fixture()):
            config, _ = module.render(value)
            self.assertFalse(config['turnPresence'])
            expected = module.receiver_channel_expectation(config)
            self.assertEqual(expected['projectFingerprint'], value.get(
                'activationReceipt', {}).get('project_fingerprint', expected['projectFingerprint']))
            self.assertNotIn(config['bearerToken'], json.dumps(expected))
            self.assertNotIn(config['credentials']['photonProjectSecret'], json.dumps(expected))
            self.assertNotIn('bearerToken', expected)
            with tempfile.TemporaryDirectory() as root:
                final = Path(root) / 'receiver-owned'
                module.stage_receiver_channel_expectation(final, expected)
                path = final / 'expected-channel.json'
                self.assertEqual(module.read_receiver_channel_expectation(path), expected)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                module.stage_receiver_channel_expectation(final, expected)
                path.write_text(path.read_text()[:-1] + ',"lineKey":"other"}')
                with self.assertRaises(module.ManifestError):
                    module.read_receiver_channel_expectation(path)
                with self.assertRaises(module.ManifestError):
                    module.stage_receiver_channel_expectation(final, expected)

    def test_receiver_staging_preserves_only_private_fresh_routine_files(self):
        config, _ = module.render(first_contact_fixture())
        expected = module.receiver_channel_expectation(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            final = root / 'runtime'
            module.stage_receiver_channel_expectation(final, expected)
            original = (final / 'expected-channel.json').read_bytes()
            for name in ('owner-routines.json', 'owner-routines.json.lock'):
                (final / name).write_bytes(b'{}' if name.endswith('.json') else b'')
                (final / name).chmod(0o600)
            module.stage_receiver_channel_expectation(final, expected)
            self.assertEqual((final / 'expected-channel.json').read_bytes(), original)
            self.assertEqual((final / 'owner-routines.json').read_bytes(), b'{}')
            (final / 'unknown.json').write_bytes(b'{}')
            with self.assertRaises(module.ManifestError):
                module.stage_receiver_channel_expectation(final, expected)
            (final / 'unknown.json').unlink()
            (final / 'owner-routines.json').chmod(0o644)
            with self.assertRaises(module.ManifestError):
                module.stage_receiver_channel_expectation(final, expected)
            (final / 'owner-routines.json').unlink()
            (final / 'owner-routines.json').symlink_to(final / 'expected-channel.json')
            with self.assertRaises(module.ManifestError):
                module.stage_receiver_channel_expectation(final, expected)

    def test_receiver_restaging_removes_routine_temp_orphaned_by_a_killed_receiver(self):
        config, receiver = module.render(first_contact_fixture())
        expected = module.receiver_channel_expectation(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            final = root / 'runtime'
            module.stage_receiver_channel_expectation(final, expected)   # first launch
            (final / 'owner-routines.json').write_bytes(b'{}')
            (final / 'owner-routines.json').chmod(0o600)
            # SIGKILL inside preferences._durable_preferences_replace, after mkstemp/fchmod
            # and before its rename: same-epoch restart staging used to refuse forever.
            fd, orphan = tempfile.mkstemp(prefix='.preferences-', dir=final)
            os.fchmod(fd, 0o600)
            os.write(fd, b'{"routines":')
            os.close(fd)
            module.stage_receiver_channel_expectation(final, expected)
            self.assertFalse(os.path.lexists(orphan))
            self.assertEqual((final / 'owner-routines.json').read_bytes(), b'{}')
            # Only a plain single-link receiver-owned temp is removed; anything else conflicts.
            linked = final / '.preferences-linked'
            os.link(final / 'owner-routines.json', linked)
            with self.assertRaises(module.ManifestError):
                module.stage_receiver_channel_expectation(final, expected)
            linked.unlink()
            (final / '.preferences-symlink').symlink_to(final / 'owner-routines.json')
            with self.assertRaises(module.ManifestError):
                module.stage_receiver_channel_expectation(final, expected)
            (final / '.preferences-symlink').unlink()
            module.stage_receiver_channel_expectation(final, expected)
            # Node bundles never contain receiver temps; the same name there is a conflict.
            node = root / 'node'
            module.stage(node, config, receiver)
            (node / '.preferences-abc12345').write_bytes(b'{}')
            with self.assertRaises(module.ManifestError):
                module.stage(node, config, receiver)

    def test_only_a_restart_restage_replaces_rotated_node_config_atomically(self):
        config, receiver = module.render(fixture('photon'))
        rotated = json.loads(json.dumps(config))
        rotated['bearerToken'] = 'r' * len(config['bearerToken'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            output = root / 'config'
            module.stage(output, config, receiver)
            projection = (output / 'receiver-projection.json').read_bytes()
            # A first launch never overwrites; a same-epoch restart re-renders rotated secrets
            # (this used to be a permanent "staging destination conflicts" restart refusal).
            with self.assertRaisesRegex(module.ManifestError, 'staging destination conflicts'):
                module.stage(output, rotated, receiver)
            with patch.object(module.os, 'replace', side_effect=OSError('killed')):
                with self.assertRaises(OSError):
                    module.stage(output, rotated, receiver, restart=True)
            self.assertEqual((output / 'runtime-config.json').read_bytes(),
                             module._payload_bytes(config))
            self.assertEqual(sorted(p.name for p in output.iterdir()),
                             ['receiver-projection.json', 'runtime-config.json'])
            # A SIGKILL between mkstemp and rename leaves this stager's own temp behind.
            (output / '.sotto-stage-abcd1234').write_bytes(b'{"partial":')
            module.stage(output, rotated, receiver, restart=True)
            self.assertEqual((output / 'runtime-config.json').read_bytes(),
                             module._payload_bytes(rotated))
            self.assertEqual((output / 'receiver-projection.json').read_bytes(), projection)
            self.assertEqual(stat.S_IMODE((output / 'runtime-config.json').stat().st_mode), 0o600)
            self.assertEqual(sorted(p.name for p in output.iterdir()),
                             ['receiver-projection.json', 'runtime-config.json'])
            module.stage(output, rotated, receiver)   # identical rerun stays a no-op
            (output / 'unknown.json').write_bytes(b'{}')
            with self.assertRaises(module.ManifestError):
                module.stage(output, config, receiver, restart=True)

    def assert_node_loader(self, config_path, check=''):
        package = Path(os.environ.get('SOTTO_CHAT_PACKAGE', ''))
        built = package / 'dist/bootstrap/config.js'
        source = package / 'src/bootstrap/config.ts'
        if not built.is_file() and not source.is_file():
            self.skipTest('set SOTTO_CHAT_PACKAGE to a built or source local runtime')
        # Source checkouts must not silently load stale output from an earlier build.
        module_path = source if source.is_file() else built
        script = ('import {loadRuntimeConfig} from ' + json.dumps(module_path.as_uri()) +
                  '; const config=await loadRuntimeConfig(process.argv[1]);' + check)
        command = ['node'] + (['--import', 'tsx'] if source.is_file() else [])
        subprocess.run([*command, '--input-type=module', '-e', script, str(config_path)],
                       check=True, capture_output=True, text=True, cwd=package)

    def test_fresh_photon_cloud_first_contact_is_explicit_and_node_compatible(self):
        value = first_contact_fixture()
        config, receiver = module.render(value)
        self.assertEqual(config['firstContact'], value['firstContact'])
        self.assertNotIn('providerThreadId', config)
        self.assertNotIn('activationReceipt', config)
        self.assertEqual(receiver['SOTTO_CHAT_RUNTIME_URL'], 'http://127.0.0.1:19981')
        with tempfile.TemporaryDirectory() as root:
            staged = Path(root) / 'staged'
            module.stage(staged, config, receiver)
            self.assert_node_loader(staged / 'runtime-config.json',
                'if(config.firstContact?.expectedProviderLine!=="+15557654321" || "providerThreadId" in config) process.exit(2);')

    def test_first_contact_rejects_mixed_or_untrusted_line_inputs(self):
        for mutate in (
            lambda m: m.update(providerThreadId='imessage:iMessage;-;+15551234567~+15557654321'),
            lambda m: m.update(activationReceipt=fixture('photon')['activationReceipt']),
            lambda m: m['firstContact'].update(expectedProviderLine='+15550000000'),
            lambda m: m['firstContact'].update(mode='any_message'),
            lambda m: m['firstContact'].pop('expectedProviderLine'),
            lambda m: m['firstContact'].update(expectedProviderLine='15557654321'),
            lambda m: m['credentials'].update(mode='self_host'),
        ):
            value = first_contact_fixture()
            mutate(value)
            with self.assertRaises(module.ManifestError):
                module.render(value)
        telegram = fixture()
        telegram.pop('providerThreadId')
        telegram['firstContact'] = {'mode': 'owner_dm', 'expectedProviderLine': '+15557654321'}
        with self.assertRaises(module.ManifestError):
            module.render(telegram)

    def test_telegram_binding_and_read_only_relay_derivation(self):
        value = fixture()
        value['instructions'] = 'First line.\nSecond line.\n'
        config, receiver = module.render(value)
        self.assertEqual(config['instructions'], value['instructions'])
        self.assertEqual(config['credentials']['telegramLinkFile']['allowed_user'], 123456789)
        self.assertEqual(config['target'], 'telegram')
        self.assertEqual(receiver['SOTTO_CHAT_RUNTIME_URL'], 'http://127.0.0.1:19981')
        self.assertNotIn(value['relay']['rootBearer'], json.dumps((config, receiver)))
        self.assertNotEqual(config['bridge']['readBearer'], value['relay']['rootBearer'])

    def test_activated_photon_binding_and_fresh_gap(self):
        value = fixture('photon')
        config, _ = module.render(value)
        self.assertEqual(config['providerThreadId'], 'imessage:iMessage;-;+15551234567~+15557654321')
        for mutate in (
            lambda m: m.pop('activationReceipt'),
            lambda m: m['activationReceipt'].update(owner='+15550000000'),
            lambda m: m['activationReceipt'].update(project_fingerprint='0' * 64),
            lambda m: m.update(providerThreadId='imessage:other-guid'),
            lambda m: (m['activationReceipt'].update(chat_id='iMessage;group;room'),
                       m.update(providerThreadId='imessage:iMessage;group;room~+15557654321')),
            lambda m: m.update(providerThreadId='imessage:iMessage;-;+15551234567'),
            lambda m: m.update(providerThreadId='imessage:iMessage;-;+15551234567~+15550000000'),
        ):
            changed = fixture('photon')
            mutate(changed)
            with self.assertRaises(module.ManifestError):
                module.render(changed)

    def test_unsupported_channel_pairing_and_identity_changes_refused(self):
        for change in (
            {'channel': 'whatsapp', 'target': 'whatsapp'},
            {'target': 'owner'},
            {'providerThreadId': 'telegram:999999999'},
            {'owner': {'accountId': 'account-fixed', 'ownerId': '999999999'}},
        ):
            value = fixture()
            value.update(change)
            with self.assertRaises(module.ManifestError):
                module.render(value)
        value = fixture()
        value['telegramLink']['bot_token'] = 'old-bot'
        with self.assertRaises(module.ManifestError):
            module.render(value)
        for field, replacement in (
            ('lineKey', ' line-fixed'), ('instructions', '\x00'),
            ('modelProxy', {'origin': 'https://proxy.example:abc', 'bearer': 'model-bearer-fixture',
                            'chatModelId': 'chat-v1', 'nativeMediaModelId': 'native-v1'}),
            ('relay', {'receiverPort': 19981, 'rootBearer': 'root-relay-never-output-1234567890'}),
            ('runtime', {'port': 19981, 'bearerToken': 'root-relay-never-output-1234567890'}),
        ):
            value = fixture()
            value[field] = replacement
            with self.assertRaises(module.ManifestError):
                module.render(value)
        for origin in ('https://127.1', 'https://0x7f000001', 'https://proxy.example:443',
                       'https://bad_label.example'):
            value = fixture()
            value['modelProxy']['origin'] = origin
            with self.assertRaises(module.ManifestError):
                module.render(value)
        value = fixture()
        value['owner']['ownerId'] = '9007199254740992'
        value['providerThreadId'] = 'telegram:9007199254740992'
        value['telegramLink']['allowed_user'] = 9007199254740992
        with self.assertRaises(module.ManifestError):
            module.render(value)
        value = fixture()
        value['instructions'] = '😀' * 6000
        with self.assertRaises(module.ManifestError):
            module.render(value)

    def test_private_input_rejects_duplicate_symlink_fifo_and_oversize(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / 'manifest.json'
            source.write_text('{"version":1,"version":1}')
            source.chmod(0o600)
            with self.assertRaises(module.ManifestError):
                module.read_private_manifest(source)
            source.write_text('{"value":NaN}')
            with self.assertRaises(module.ManifestError):
                module.read_private_manifest(source)
            source.write_text('{"value":1e9999}')
            with self.assertRaises(module.ManifestError):
                module.read_private_manifest(source)
            alias = root / 'alias.json'
            alias.symlink_to(source)
            with self.assertRaises(OSError):
                module.read_private_manifest(alias)
            pipe = root / 'manifest.pipe'
            os.mkfifo(pipe, 0o600)
            with self.assertRaises(module.ManifestError):
                module.read_private_manifest(pipe)
            source.write_bytes(b'0' * (module.MAX_MANIFEST_BYTES + 1))
            with self.assertRaises(module.ManifestError):
                module.read_private_manifest(source)
            source.write_text(json.dumps(fixture()))
            source.chmod(0o644)
            with self.assertRaises(module.ManifestError):
                module.read_private_manifest(source)

    def test_private_stage_is_new_only_and_matches_node_loader(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps(fixture('photon')))
            manifest.chmod(0o600)
            preview = subprocess.run(['python3', str(Path(module.__file__)), str(manifest)],
                                     check=True, capture_output=True, text=True)
            self.assertNotIn('project-secret-fixture', preview.stdout + preview.stderr)
            self.assertNotIn('root-relay-never-output', preview.stdout + preview.stderr)
            self.assertNotIn('+15551234567', preview.stdout + preview.stderr)
            config, receiver = module.render(module.read_private_manifest(manifest))
            output = root / 'staged'
            module.stage(output, config, receiver)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((output / 'runtime-config.json').stat().st_mode), 0o600)
            module.stage(output, config, receiver)  # identical input is idempotent
            changed = dict(config, lineKey='different-line')
            with self.assertRaises(module.ManifestError):
                module.stage(output, changed, receiver)
            first = root / 'first-dir'
            contender = root / 'existing-empty-dir'
            first.mkdir()
            contender.mkdir()
            with self.assertRaises(module.ManifestError):
                module._publish_noreplace(first, contender)
            self.assertTrue(first.is_dir())
            self.assertTrue(contender.is_dir())
            self.assertEqual(json.loads((output / 'receiver-projection.json').read_text()), receiver)
            # The real Node parser validates staged bytes, with no SDK startup.
            self.assert_node_loader(output / 'runtime-config.json')


    def test_retry_repairs_parent_barrier_after_successful_publication(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            config, receiver = module.render(fixture())
            output = root / 'staged'
            actual_sync = module._fsync_directory
            parent_calls = []

            def fail_first_parent(path):
                if path == root:
                    parent_calls.append(path)
                    if len(parent_calls) == 1:
                        raise OSError('synthetic directory barrier failure')
                return actual_sync(path)

            with patch.object(module, '_fsync_directory', side_effect=fail_first_parent):
                with self.assertRaises(OSError):
                    module.stage(output, config, receiver)
                self.assertTrue(output.is_dir())
                before = (output / 'runtime-config.json').read_bytes()
                module.stage(output, config, receiver)
                self.assertEqual((output / 'runtime-config.json').read_bytes(), before)
                self.assertEqual(len(parent_calls), 2)


if __name__ == '__main__':
    unittest.main()


def test_shared_project_mode_is_closed_and_distinct_from_physical_line():
    value = first_contact_fixture()
    value['firstContact'] = {'mode': 'owner_dm_shared_project'}
    config, _ = module.render(value)
    assert config['firstContact'] == {'mode': 'owner_dm_shared_project'}
    identity = config['ownerLoop']['identity']
    assert identity['route_mode'] == 'first_contact_shared_project'
    assert identity['route_id'] == 'shared_project'
    expectation = module.receiver_channel_expectation(config)
    assert expectation['route'] == {'mode': 'first_contact_shared_project'}
    assert module._validate_receiver_channel_expectation(expectation) == expectation
    for changed in ({'mode': 'owner_dm_shared_project', 'expectedProviderLine': '+15557654321'},
                    {'mode': 'owner_dm_shared_project', 'extra': True}):
        value['firstContact'] = changed
        with unittest.TestCase().assertRaises(module.ManifestError):
            module.render(value)
