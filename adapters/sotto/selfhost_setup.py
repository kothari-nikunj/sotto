"""Link one Telegram owner before preparing a fresh supervised self-host volume.

Run as root under tini with a private PID namespace. The setup and resulting
operator files live outside /data. No Node gateway exists while the existing
Telegram linker owns getUpdates; a failed link leaves /data untouched.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys

if not __package__:
    package_dir = str(Path(__file__).resolve().parent)
    if package_dir not in sys.path:
        sys.path.insert(0, package_dir)

try:
    from . import selfhost_bootstrap, root_supervisor, startup_config
except ImportError:
    import selfhost_bootstrap
    import root_supervisor
    import startup_config

receiver_dir = str(Path(__file__).resolve().parents[2] / 'runtime' / 'trigger-receiver')
if not Path(receiver_dir, 'telegram_link.py').is_file():
    receiver_dir = '/app/trigger-receiver'
if receiver_dir not in sys.path:
    sys.path.insert(0, receiver_dir)
import telegram_link  # noqa: E402 — use the existing getMe/getUpdates/offset owner capture


class SelfHostSetupUnavailableError(ValueError):
    pass


_INPUT_KEYS = frozenset({'version', 'tenantId', 'accountId', 'lineKey', 'volumeId',
                         'instructions', 'botToken', 'pairingPhrase', 'modelDirect',
                         'runtime', 'relay', 'controlToken', 'timezone'})
_PHRASE = re.compile(r'[A-Za-z0-9_-]{24,64}\Z')


def _refuse():
    raise SelfHostSetupUnavailableError('self-host owner setup unavailable')


def _bundle(config, owner):
    if (not isinstance(config, dict) or set(config) != _INPUT_KEYS
            or type(config['version']) is not int or config['version'] != 1
            or not isinstance(owner, int) or type(owner) is bool
            or not 0 < owner <= 9007199254740991):
        _refuse()
    owner_id = str(owner)
    token = config['botToken']
    manifest = {
        'version': 1, 'deployment': 'self_host', 'channel': 'telegram',
        'tenantId': config['tenantId'],
        'owner': {'accountId': config['accountId'], 'ownerId': owner_id},
        'lineKey': config['lineKey'], 'target': 'telegram',
        'providerThreadId': f'telegram:{owner_id}',
        'telegramLink': {'bot_token': token, 'allowed_user': owner},
        'stateDirectory': '/data/node-state', 'mediaRoot': '/data/node-media',
        'instructions': config['instructions'], 'runtime': config['runtime'],
        'relay': config['relay'], 'modelDirect': config['modelDirect'],
        'credentials': {'telegramToken': token},
    }
    authority = {
        'SOTTO_MCP_TOKEN': config['relay']['rootBearer'],
        'SOTTO_CONTROL_TOKEN': config['controlToken'],
        'SOTTO_TENANT_ID': config['tenantId'], 'SOTTO_DATA': '/data',
        'SOTTO_HOME': '/data/sotto-home', 'SOTTO_VOLUME_ID': config['volumeId'],
    }
    identity = {
        'channel': 'telegram', 'target': 'telegram',
        'tenantId': config['tenantId'], 'accountId': config['accountId'],
        'ownerId': owner_id, 'lineKey': config['lineKey'],
        'providerThreadId': f'telegram:{owner_id}',
        'botFingerprint': hashlib.sha256(
            ('sotto-telegram-bot-v1:' + token).encode()).hexdigest(),
    }
    return {
        'version': 1, 'tenantRoot': '/data', 'manifest': manifest,
        'receiverAuthority': authority, 'stableIdentity': identity,
        'publicReceiverAuthority': None, 'trustedServiceId': '',
        'receiverOverrides': {'SOTTO_TIMEZONE': config['timezone']},
    }


def _validated_input(config):
    if (not isinstance(config, dict) or set(config) != _INPUT_KEYS
            or not isinstance(config.get('pairingPhrase'), str)
            or _PHRASE.fullmatch(config['pairingPhrase']) is None
            or not isinstance(config.get('botToken'), str)
            or not re.fullmatch(r'[0-9]{1,15}:[A-Za-z0-9_-]{20,4095}', config['botToken'])):
        _refuse()
    # Check all model, identity, secret, timezone, and role bindings before polling.
    bundle = _bundle(config, 1)
    manifest = bundle['manifest']
    startup_config.render(manifest)
    root_supervisor.process_handoff.build_child_handoff(
        manifest, bundle['receiverAuthority'], stable_identity=bundle['stableIdentity'],
        node_config_path='/data/node-config/config/runtime-config.json',
        data_directory='/data', skills_root='/app/sotto-skills',
        receiver_overrides=bundle['receiverOverrides'],
        limited_profile=root_supervisor.fixed_five.TELEGRAM_PROFILE)
    return config


def _private_parent(path):
    path = Path(path)
    if (not path.is_absolute() or path.name in ('', '.', '..')
            or path == Path('/data') or Path('/data') in path.parents):
        _refuse()
    fd = root_supervisor._open_directory(path.parent)
    parent = os.fstat(fd)
    if parent.st_uid != 0 or stat.S_IMODE(parent.st_mode) != 0o700:
        os.close(fd)
        _refuse()
    return fd


def _empty_volume():
    """Read only: require the same ext4, root, and empty namespace as preparation."""
    fresh = selfhost_bootstrap.prepare_fresh
    fresh._empty_private_namespace()
    fd = root_supervisor._open_directory(Path('/data'))
    try:
        root = os.fstat(fd)
        if root.st_uid != 0 or root.st_gid != 0 or stat.S_IMODE(root.st_mode) not in (0o700, 0o755):
            _refuse()
        fs_type, source, major, minor, _ = root_supervisor._mount_record(Path('/data'))
        fresh._mount(fd, {'mountPoint': '/data', 'source': source, 'fsType': fs_type,
                          'major': major, 'minor': minor})
        fresh._fresh_entries(fd, claimed=False)
    finally:
        os.close(fd)


def _volume_lock():
    """One read-only directory flock prevents concurrent initial-link pollers."""
    fd = root_supervisor._open_directory(Path('/data'))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError as error:
        os.close(fd)
        raise SelfHostSetupUnavailableError('self-host volume setup already active') from error


def _capture(path, token, phrase):
    selected = Path(path)
    if os.path.lexists(selected):
        # The linker's historical record has no durable acknowledgement proof.
        # A leftover capture is operator evidence, never a fresh setup grant.
        _refuse()
    result = telegram_link.ensure_link(
        token, phrase, path=str(selected),
        announce=lambda link: print('[sotto] link your private Telegram chat: ' + link,
                                    file=sys.stderr, flush=True))
    if not result.get('ok'):
        _refuse()
    saved = root_supervisor._private_input(selected)
    if (saved.get('bot_token') != token
            or type(saved.get('allowed_user')) is not int
            or saved['allowed_user'] != result.get('user_id')):
        _refuse()
    fd = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    parent_fd = _private_parent(selected)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return saved['allowed_user']


def _publish(path, bundle):
    selected = Path(path)
    parent_fd = _private_parent(selected)
    try:
        if os.path.lexists(selected):
            _refuse()
        payload = (json.dumps(bundle, sort_keys=True, separators=(',', ':')) + '\n').encode()
        if len(payload) > root_supervisor._MAX_BUNDLE_BYTES:
            _refuse()
        temporary = selected.parent / ('.sotto-operator-' + os.urandom(12).hex())
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600)
            try:
                os.fchmod(fd, 0o600)
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        _refuse()
                    remaining = remaining[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            startup_config._publish_noreplace(temporary, selected)
            os.fsync(parent_fd)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        os.close(parent_fd)


def run(setup_path, operator_path):
    selfhost_bootstrap._preflight()
    config = _validated_input(root_supervisor._private_input(setup_path))
    operator = Path(operator_path)
    parent_fd = _private_parent(operator)
    try:
        if os.path.lexists(operator) or operator == Path(setup_path):
            _refuse()
        capture = operator.parent / 'telegram-capture.json'
        if capture in (operator, Path(setup_path)):
            _refuse()
    finally:
        os.close(parent_fd)
    volume_fd = _volume_lock()
    try:
        _empty_volume()
        owner = _capture(capture, config['botToken'], config['pairingPhrase'])
        bundle = _bundle(config, owner)
        # Bind captured owner to every downstream role before publishing output.
        manifest = bundle['manifest']
        startup_config.render(manifest)
        root_supervisor.process_handoff.build_child_handoff(
            manifest, bundle['receiverAuthority'], stable_identity=bundle['stableIdentity'],
            node_config_path='/data/node-config/config/runtime-config.json',
            data_directory='/data', skills_root='/app/sotto-skills',
            receiver_overrides=bundle['receiverOverrides'],
            limited_profile=root_supervisor.fixed_five.TELEGRAM_PROFILE)
        _empty_volume()
        _publish(operator, bundle)
        selfhost_bootstrap.run('--prepare', operator)
    finally:
        os.close(volume_fd)


def main(argv):
    if (len(argv) != 4 or argv[0] != '--setup-file' or argv[2] != '--operator-file'):
        print('[sotto] self-host setup arguments unavailable', file=sys.stderr)
        return 2
    try:
        run(argv[1], argv[3])
        return 0
    except (SelfHostSetupUnavailableError,
            selfhost_bootstrap.SelfHostBootstrapUnavailableError,
            root_supervisor.SupervisorUnavailableError,
            selfhost_bootstrap.prepare_fresh.FreshPreparationUnavailableError,
            root_supervisor.tenant_lifecycle.LifecycleUnavailableError,
            startup_config.ManifestError, root_supervisor.process_handoff.HandoffError,
            OSError, ValueError, TypeError, KeyError):
        print('[sotto] self-host owner setup unavailable; review private setup and volume',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
