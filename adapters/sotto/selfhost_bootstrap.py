"""Root-only entrypoint for a supervised self-host Telegram or Photon lineage.

The operator supplies an explicit private bundle without a mount claim. This
entrypoint observes the current kernel mount and stages the completed bundle
for the existing fresh-preparation and root-supervisor gates. Telegram pairing
is a precondition; this process never discovers an owner or credential.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

if not __package__:
    package_dir = str(Path(__file__).resolve().parent)
    if package_dir not in sys.path:
        sys.path.insert(0, package_dir)

try:
    from .bootstrap_runtime import clean_environment as _clean_environment, health_only as _health_only
    from . import prepare_fresh, root_supervisor, selfhost_configuration
except ImportError:
    from bootstrap_runtime import clean_environment as _clean_environment, health_only as _health_only
    import prepare_fresh
    import root_supervisor
    import selfhost_configuration

tenant_lifecycle = root_supervisor.tenant_lifecycle
_MAX_ENVELOPE = 128 * 1024
_PROVIDER_KEYS = frozenset({'projectId', 'environmentId', 'serviceId', 'volumeId',
                            'domain', 'provisionRequest', 'volumeCreationReceipt'})
_UUID = re.compile(r'[0-9a-f-]{36}\Z')
_REQUEST = re.compile(r'[A-Za-z0-9_-]{24,128}\Z')


class SelfHostBootstrapUnavailableError(ValueError):
    pass


def _refuse():
    raise SelfHostBootstrapUnavailableError('self-host bootstrap unavailable')


def _preflight():
    if os.geteuid() != 0 or os.getpid() == 1:
        _refuse()
    tenant_lifecycle._topology()
    if set(tenant_lifecycle._processes()) != {1, os.getpid()}:
        _refuse()


def _operator_bundle(input_path):
    supplied = root_supervisor._private_input(input_path)
    if set(supplied) != root_supervisor._BUNDLE_KEYS - {'mount'}:
        _refuse()
    manifest = supplied.get('manifest')
    if (supplied.get('tenantRoot') != '/data'
            or not isinstance(manifest, dict)
            or manifest.get('deployment') != 'self_host'
            or manifest.get('channel') not in ('telegram', 'photon')
            or supplied.get('publicReceiverAuthority') is not None
            or supplied.get('trustedServiceId') != ''):
        _refuse()
    fs_type, source, major, minor, _mount_id = root_supervisor._mount_record(Path('/data'))
    if fs_type != 'ext4':
        _refuse()
    bundle = dict(supplied)
    bundle['mount'] = {'mountPoint': '/data', 'source': source, 'fsType': fs_type,
                       'major': major, 'minor': minor}
    root, _, manifest, authority, identity, public, service_id = (
        root_supervisor._validated_bundle(bundle))
    if root != Path('/data') or public is not None or service_id != '':
        _refuse()
    config, _ = root_supervisor.startup_config.render(manifest)
    if (config.get('channel') not in ('telegram', 'photon')
            or not isinstance(config.get('modelDirect'), dict)
            or (config['channel'] == 'photon' and (
                config.get('credentials', {}).get('mode') != 'cloud'
                or (config.get('firstContact') or {}).get('mode') not in ('owner_dm', 'owner_dm_shared_project')
                or config.get('providerThreadId') is not None))
            or not isinstance(authority.get('SOTTO_VOLUME_ID'), str)
            or not authority['SOTTO_VOLUME_ID']):
        _refuse()
    root_supervisor.process_handoff.build_child_handoff(
        manifest, authority, stable_identity=identity,
        node_config_path='/data/node-config/config/runtime-config.json',
        data_directory='/data', skills_root='/app/sotto-skills',
        receiver_overrides=bundle['receiverOverrides'],
        public_receiver_authority=None, trusted_service_id='',
        limited_profile=(root_supervisor.fixed_five.TELEGRAM_PROFILE
                         if config['channel'] == 'telegram' else
                         root_supervisor.fixed_five.SELF_HOST_PHOTON_PROFILE))
    return bundle


def _railway_envelope(env):
    raw = env.get('SOTTO_SELFHOST_LAUNCH')
    if not isinstance(raw, str):
        _refuse()
    try:
        encoded = raw.encode('utf-8')
    except UnicodeError:
        _refuse()
    if not 0 < len(encoded) <= _MAX_ENVELOPE:
        _refuse()
    value = root_supervisor._strict_json(encoded)
    if (not {'version', 'provider', 'bundle'} <= set(value)
            or set(value) - {'version', 'provider', 'bundle', 'telegramCapture'}
            or type(value['version']) is not int
            or value['version'] != 1 or not isinstance(value['provider'], dict)
            or not isinstance(value['bundle'], dict)):
        _refuse()
    provider, bundle = value['provider'], value['bundle']
    if set(provider) != _PROVIDER_KEYS or set(bundle) != root_supervisor._BUNDLE_KEYS - {'mount'}:
        _refuse()
    receipt = provider['volumeCreationReceipt']
    if (not isinstance(receipt, dict) or set(receipt) != {'service_id', 'volume_id'}
            or any(not isinstance(provider.get(key), str) or not _UUID.fullmatch(provider[key])
                   for key in ('projectId', 'environmentId', 'serviceId', 'volumeId'))
            or receipt != {'service_id': provider['serviceId'], 'volume_id': provider['volumeId']}
            or not isinstance(provider['provisionRequest'], str)
            or not _REQUEST.fullmatch(provider['provisionRequest'])):
        _refuse()
    for key, field in (('RAILWAY_PROJECT_ID', 'projectId'),
                       ('RAILWAY_ENVIRONMENT_ID', 'environmentId'),
                       ('RAILWAY_SERVICE_ID', 'serviceId'),
                       ('RAILWAY_PUBLIC_DOMAIN', 'domain'),
                       ('SOTTO_SETUP_REQUEST', 'provisionRequest')):
        if env.get(key) != provider[field]:
            _refuse()
    if (env.get('RAILWAY_VOLUME_MOUNT_PATH') != '/data'
            or not isinstance(env.get('RAILWAY_VOLUME_NAME'), str)
            or not env['RAILWAY_VOLUME_NAME'] or env.get('PORT') != '8080'):
        _refuse()
    manifest = bundle.get('manifest')
    authority = bundle.get('receiverAuthority')
    if (bundle.get('tenantRoot') != '/data' or not isinstance(manifest, dict)
            or manifest.get('deployment') != 'self_host'
            or manifest.get('channel') not in ('photon', 'telegram')
            or not isinstance(authority, dict)
            or authority.get('SOTTO_VOLUME_ID') != provider['volumeId']
            or bundle.get('trustedServiceId') != provider['serviceId']
            or bundle.get('publicReceiverAuthority') != {
                'tenantId': manifest.get('tenantId'), 'serviceId': provider['serviceId'],
                'domain': provider['domain'], 'targetPort': 8080,
            }):
        _refuse()
    if manifest['channel'] == 'telegram':
        try:
            link = selfhost_configuration.validate_telegram_capture(
                manifest['credentials']['telegramToken'], provider['provisionRequest'],
                value.get('telegramCapture'))
        except (ValueError, TypeError, KeyError):
            _refuse()
        if (manifest.get('telegramLink') != link
                or not isinstance(manifest.get('owner'), dict)
                or manifest.get('owner', {}).get('ownerId') != str(link['allowed_user'])
                or manifest.get('providerThreadId') != f"telegram:{link['allowed_user']}"):
            _refuse()
    elif 'telegramCapture' in value:
        _refuse()
    return value


def _railway_stage(envelope):
    bundle = dict(envelope['bundle'])
    fs_type, source, major, minor, _ = root_supervisor._mount_record(Path('/data'))
    if fs_type != 'ext4':
        _refuse()
    bundle['mount'] = {'mountPoint': '/data', 'source': source, 'fsType': fs_type,
                       'major': major, 'minor': minor}
    root, _, manifest, authority, identity, public, service_id = (
        root_supervisor._validated_bundle(bundle))
    if (root != Path('/data') or public is None
            or authority.get('SOTTO_VOLUME_ID') != envelope['provider']['volumeId']
            or service_id != envelope['provider']['serviceId']):
        _refuse()
    config, _ = root_supervisor.startup_config.render(manifest)
    channel = config.get('channel')
    photon = (channel == 'photon' and config.get('credentials', {}).get('mode') == 'cloud'
              and (config.get('firstContact') or {}).get('mode') in ('owner_dm', 'owner_dm_shared_project')
              and config.get('providerThreadId') is None)
    telegram = (channel == 'telegram'
                and config.get('providerThreadId') == f"telegram:{config.get('owner', {}).get('ownerId')}"
                and config.get('firstContact') is None)
    if (not (photon or telegram) or not isinstance(config.get('modelDirect'), dict)
            or config.get('bridge', {}).get('url') != 'http://127.0.0.1:8080/mcp'):
        _refuse()
    root_supervisor.process_handoff.build_child_handoff(
        manifest, authority, stable_identity=identity,
        node_config_path='/data/node-config/config/runtime-config.json',
        data_directory='/data', skills_root='/app/sotto-skills',
        receiver_overrides=bundle['receiverOverrides'],
        public_receiver_authority=public, trusted_service_id=service_id,
        limited_profile=(root_supervisor.fixed_five.TELEGRAM_PROFILE if telegram
                         else root_supervisor.fixed_five.SELF_HOST_PHOTON_PROFILE))
    return _stage(bundle)


def _stage(bundle):
    directory = Path(tempfile.mkdtemp(prefix='sotto-selfhost-', dir='/tmp'))
    try:
        os.chmod(directory, 0o700)
        parent = directory.stat(follow_symlinks=False)
        if parent.st_uid != 0 or stat.S_IMODE(parent.st_mode) != 0o700:
            _refuse()
        path = directory / 'runtime.json'
        payload = (json.dumps(bundle, sort_keys=True, separators=(',', ':')) + '\n').encode()
        if len(payload) > root_supervisor._MAX_BUNDLE_BYTES:
            _refuse()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
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
        checked_fd = root_supervisor._open_directory(directory)
        os.close(checked_fd)
        parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return path
    except BaseException:
        (directory / 'runtime.json').unlink(missing_ok=True)
        directory.rmdir()
        raise


def run(mode, input_path):
    if mode not in ('--prepare', '--run'):
        _refuse()
    _preflight()
    path = _stage(_operator_bundle(input_path))
    try:
        _clean_environment()
        if mode == '--prepare':
            prepare_fresh.prepare(path)
        else:
            root_supervisor.run_operator_file(path)
    finally:
        path.unlink(missing_ok=True)
        path.parent.rmdir()


def run_railway(mode, env=None):
    if mode not in ('--prepare', '--run'):
        _refuse()
    supplied = dict(os.environ if env is None else env)
    _preflight()
    path = _railway_stage(_railway_envelope(supplied))
    try:
        _clean_environment()
        if mode == '--prepare':
            prepare_fresh.prepare(path)
            _health_only()
        else:
            root_supervisor.run_operator_file(path)
    finally:
        path.unlink(missing_ok=True)
        path.parent.rmdir()


def main(argv):
    if (not (len(argv) == 1 and argv[0] in ('--prepare', '--run'))
            and not (len(argv) == 3 and argv[0] in ('--prepare', '--run')
                     and argv[1] == '--operator-file')):
        print('[sotto] self-host bootstrap arguments unavailable', file=sys.stderr)
        return 2
    try:
        if len(argv) == 1:
            run_railway(argv[0])
        else:
            run(argv[0], argv[2])
        return 0
    except (SelfHostBootstrapUnavailableError, root_supervisor.SupervisorUnavailableError,
            prepare_fresh.FreshPreparationUnavailableError,
            tenant_lifecycle.LifecycleUnavailableError,
            root_supervisor.startup_config.ManifestError,
            root_supervisor.process_handoff.HandoffError,
            OSError, ValueError, TypeError, KeyError):
        print('[sotto] self-host bootstrap unavailable; review tenant before retry',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
