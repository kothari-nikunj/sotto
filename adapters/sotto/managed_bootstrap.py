"""Root-only entrypoint for a provisioner-attested managed Photon deployment.

The Railway volume ID is operator authority from the saved creation receipt,
not something inferred from mountinfo. Railway's injected metadata binds this
process to the expected service and attached mount; the supervisor separately
pins the observed kernel mount for the entire runtime.
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
    from . import prepare_fresh, root_supervisor
except ImportError:
    from bootstrap_runtime import clean_environment as _clean_environment, health_only as _health_only
    import prepare_fresh
    import root_supervisor

tenant_lifecycle = root_supervisor.tenant_lifecycle
_MAX_ENVELOPE = 128 * 1024
_PROVIDER_KEYS = frozenset({'projectId', 'environmentId', 'serviceId', 'volumeId',
                            'domain', 'provisionRequest', 'volumeCreationReceipt'})
_UUID = re.compile(r'[0-9a-f-]{36}\Z')
_REQUEST = re.compile(r'[A-Za-z0-9_-]{24,128}\Z')


class BootstrapUnavailableError(ValueError):
    pass


def _refuse() -> None:
    raise BootstrapUnavailableError('managed bootstrap unavailable')


def _envelope(env: dict[str, str]) -> dict:
    raw = env.get('SOTTO_MANAGED_LAUNCH')
    if not isinstance(raw, str):
        _refuse()
    try:
        encoded = raw.encode('utf-8')
    except UnicodeError:
        _refuse()
    if not 0 < len(encoded) <= _MAX_ENVELOPE:
        _refuse()
    value = root_supervisor._strict_json(encoded)
    if (set(value) != {'version', 'provider', 'bundle'} or type(value['version']) is not int
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
                       ('SOTTO_PROVISION_REQUEST', 'provisionRequest')):
        if env.get(key) != provider[field]:
            _refuse()
    if (env.get('RAILWAY_VOLUME_MOUNT_PATH') != '/data'
            or not isinstance(env.get('RAILWAY_VOLUME_NAME'), str)
            or not env['RAILWAY_VOLUME_NAME']):
        _refuse()
    if (bundle.get('tenantRoot') != '/data' or bundle.get('trustedServiceId') != provider['serviceId']
            or bundle.get('publicReceiverAuthority') != {
                'tenantId': bundle.get('manifest', {}).get('tenantId') if isinstance(bundle.get('manifest'), dict) else None,
                'serviceId': provider['serviceId'], 'domain': provider['domain'], 'targetPort': 8080,
            } or not isinstance(bundle.get('receiverAuthority'), dict)
            or bundle['receiverAuthority'].get('SOTTO_VOLUME_ID') != provider['volumeId']
            or env.get('PORT') != '8080'):
        _refuse()
    return value


def _preflight() -> None:
    tenant_lifecycle._topology()
    if set(tenant_lifecycle._processes()) != {1, os.getpid()}:
        _refuse()


def _stage(envelope: dict) -> Path:
    """Create one root-private file outside the tenant volume, without overwrite."""
    bundle = dict(envelope['bundle'])
    fs_type, source, major, minor, _ = root_supervisor._mount_record(Path('/data'))
    bundle['mount'] = {'mountPoint': '/data', 'source': source, 'fsType': fs_type,
                       'major': major, 'minor': minor}
    # Check closed shape and manifest/child crossbindings before touching disk.
    root, _, manifest, authority, identity, public, service_id = root_supervisor._validated_bundle(bundle)
    if root != Path('/data') or authority.get('SOTTO_VOLUME_ID') != envelope['provider']['volumeId']:
        _refuse()
    if envelope['provider']['domain'] != public['domain']:
        _refuse()
    root_supervisor.startup_config.render(manifest)
    root_supervisor.process_handoff.build_child_handoff(
        manifest, authority, stable_identity=identity,
        node_config_path='/data/node-config/config/runtime-config.json',
        data_directory='/data', skills_root='/app/sotto-skills',
        receiver_overrides=bundle['receiverOverrides'],
        public_receiver_authority=public, trusted_service_id=service_id,
        limited_profile=root_supervisor.fixed_five.PROFILE)
    directory = Path(tempfile.mkdtemp(prefix='sotto-operator-', dir='/tmp'))
    os.chmod(directory, 0o700)
    parent = directory.stat(follow_symlinks=False)
    if parent.st_uid != 0 or stat.S_IMODE(parent.st_mode) != 0o700:
        _refuse()
    path = directory / 'runtime.json'
    payload = (json.dumps(bundle, sort_keys=True, separators=(',', ':')) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(payload)
            stream.flush()
        os.fsync(fd)
    finally:
        os.close(fd)
    parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return path


def stage_operator_file(environ: dict[str, str]) -> Path:
    """Validate provider and runtime bindings, then stage the exact private file."""
    _preflight()
    return _stage(_envelope(environ))


def run(mode: str, env: dict[str, str] | None = None) -> None:
    if mode not in ('--prepare', '--run'):
        _refuse()
    supplied = dict(os.environ if env is None else env)
    path = stage_operator_file(supplied)
    try:
        _clean_environment()
        if mode == '--prepare':
            prepare_fresh.prepare(path)
        else:
            root_supervisor.run_operator_file(path)
    finally:
        # The staged file carries every tenant secret; never leave it behind (selfhost does too).
        path.unlink(missing_ok=True)
        path.parent.rmdir()
    if mode == '--prepare':
        _health_only()


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ('--prepare', '--run'):
        print('[sotto] managed bootstrap arguments unavailable', file=sys.stderr)
        return 2
    try:
        run(argv[0])
        return 0
    except (BootstrapUnavailableError, root_supervisor.SupervisorUnavailableError,
            prepare_fresh.FreshPreparationUnavailableError,
            tenant_lifecycle.LifecycleUnavailableError,
            root_supervisor.startup_config.ManifestError,
            root_supervisor.process_handoff.HandoffError, OSError, ValueError, TypeError, KeyError):
        print('[sotto] managed bootstrap unavailable; review tenant before retry', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
