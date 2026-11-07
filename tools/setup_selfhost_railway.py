"""Resumable single-service Railway setup for a fresh self-host Sotto Photon install.

Use a fresh, explicitly selected Railway project/environment and a protected
mode-0600 JSON input naming `project_id`, `environment_id`, `service_name`,
`source_path`, `source_commit`, `source_manifest_sha256`, `tenant_id`,
`owner_phone`, `photon_line_number`, `photon_project_id`,
`photon_project_secret`, `gemini_api_key` and `sotto_runtime`. This command
creates only its own marked service, /data volume and domain, stores the
private launch envelope as one service variable, and uploads the pinned clean
source checkout twice: once built and started with the one-time `--prepare`
start command, then with the `--run` start command on the same volume. Its
protected state holds the generated runtime, Bridge and control tokens. It
never sends a message or makes a paid model probe. A create, upload or start
setting whose outcome is unknown is only observed afterwards, never sent
again, unless the operator passes --retry-unconfirmed. The initializer serves
its preparation health for 15 minutes; if that closes unseen, the /data volume
marker is the proof, and only the operator's --retry-unconfirmed lets the
runtime (which must then show the exact marker) replace an initializer whose
preparation was not observed.
"""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


try:
    import tomllib
except ImportError:
    raise SystemExit('Sotto setup needs Python 3.11 or newer. Run this command with a supported Python interpreter.') from None


class SetupError(RuntimeError):
    pass


class SetupPendingError(SetupError):
    """The saved deployment can be observed again without uploading a second build."""
    stage = 'deployment_pending'


class UnconfirmedCreateError(SetupPendingError):
    """An earlier create or upload may have landed unseen; only observation is safe."""
    stage = 'create_unconfirmed'

    def __init__(self, what):
        super().__init__(f'An earlier Railway {what} request may still appear. Run the same command '
                         f'again later; only if Railway still shows no {what} for this setup, rerun '
                         'it with --retry-unconfirmed.')


class PreparationUnconfirmedError(SetupPendingError):
    """The initializer's bounded listener closed before this helper saw preparation finish."""
    stage = 'preparation_unconfirmed'

    def __init__(self):
        super().__init__('The Sotto initializer health listener (15 minutes) has closed and its /data '
                         'volume marker is not readable. Check that the initializer deployment logs '
                         'show no "self-host bootstrap unavailable" line, then rerun the same command '
                         'with --retry-unconfirmed; the runtime deployment then must show the exact '
                         'volume marker before setup reports success.')


class VolumeInspectionPendingError(SetupPendingError):
    """Railway file access needs a running container; a later observation may succeed."""


class NotSentError(SetupError):
    """The Railway CLI never started, so the request provably never left this machine."""


ATTACHMENT_WAIT_SECONDS = 30
ATTACHMENT_POLL_SECONDS = 2
DEPLOYMENT_POLLS = 360  # A cold source build commonly takes more than five minutes.
UPLOAD_POLLS = 30  # Two-second polls: about a minute for Railway to list an uploaded deployment.
HEALTH_WAIT_SECONDS = 300  # A fresh Railway domain can lag DNS/TLS behind deployment SUCCESS.
HEALTH_POLL_SECONDS = 10
# adapters/sotto/selfhost_bootstrap.py --prepare serves /health for 900 seconds after preparation
# and then exits. The margin covers preparation itself and Railway status lag; past it, the
# initializer can no longer be preparing the volume.
PREPARED_LISTENER_SECONDS = 900
PREPARED_LISTENER_MARGIN_SECONDS = 300
_STOPPED = ('FAILED', 'CRASHED', 'REMOVED', 'CANCELED', 'CANCELLED', 'SKIPPED')


def wait_for_attachment(read, expected, *, missing, different, initial=None):
    """Wait for a saved Railway receipt to appear, without making another mutation."""
    deadline = time.monotonic() + ATTACHMENT_WAIT_SECONDS
    while True:
        actual = read() if initial is None else initial
        initial = None
        if actual == expected:
            return
        if actual:
            raise SetupError(different)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SetupError(missing)
        time.sleep(min(ATTACHMENT_POLL_SECONDS, remaining))


DOMAIN_PORT = 8080
# Railway custom start commands replace the image ENTRYPOINT; keep Tini as PID 1.
SOTTO_PREPARE_START = '/usr/bin/tini -- python3 -I -B /app/adapters/sotto/selfhost_bootstrap.py --prepare'
SOTTO_RUNTIME_START = '/usr/bin/tini -- python3 -I -B /app/adapters/sotto/selfhost_bootstrap.py --run'


def observe_after_attempt(read, initial):
    """Give a create whose response was lost time to appear before calling it absent."""
    deadline = time.monotonic() + ATTACHMENT_WAIT_SECONDS
    actual = initial
    while not actual:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return actual
        time.sleep(min(ATTACHMENT_POLL_SECONDS, remaining))
        actual = read()
    return actual


def attempt(state, state_path, flag, call):
    """Record an attempt before a mutation; forget it only when the request was never sent."""
    state[flag] = True
    save_state(state_path, state)
    try:
        return call()
    except NotSentError:
        state[flag] = False
        save_state(state_path, state)
        raise


def protected_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise SetupError(f'{path.name} must be a protected regular JSON file (mode 0600)')
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise SetupError(f'{path.name} is invalid JSON') from error


def save_state(path, value):
    path = Path(path)
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_mode & 0o077)):
        raise SetupError('State path must be a protected regular file')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.selfhost-railway-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def source_receipt(path, stage=None):
    """Hash exactly the tracked files; with `stage`, copy those same bytes there for upload."""
    supplied = Path(path)
    if supplied.is_symlink():
        raise SetupError('Source path is a symlink')
    root = supplied.resolve()
    if not root.is_dir():
        raise SetupError('Source checkout is missing')

    def git(*args):
        result = subprocess.run(['git', *args], cwd=root, capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise SetupError('Cannot inspect public source checkout')
        return result.stdout

    if Path(os.fsdecode(git('rev-parse', '--show-toplevel')).strip()).resolve() != root:
        raise SetupError('Source path must be the repository root')
    commit = git('rev-parse', 'HEAD').decode().strip()
    # Gitignored caches and virtualenvs do not count: they are outside the manifest and the
    # staged upload. Tracked edits and untracked, non-ignored files still make the source dirty.
    if git('status', '--porcelain=v1', '--untracked-files=all').strip():
        raise SetupError('Public source is dirty')
    paths = [os.fsdecode(item) for item in git('ls-files', '-z').split(b'\0') if item]
    if not {'Dockerfile', 'railway.toml', 'VERSION'} <= set(paths):
        raise SetupError('Public source lacks runtime deployment files')
    try:
        railway = tomllib.loads((root / 'railway.toml').read_text())
    except (OSError, ValueError) as error:
        raise SetupError('Source Railway configuration is unreadable') from error
    build, deploy = railway.get('build'), railway.get('deploy')
    if (not isinstance(build, dict) or not isinstance(deploy, dict)
            or build.get('builder') != 'DOCKERFILE'
            or build.get('dockerfilePath') != 'Dockerfile'
            or deploy.get('healthcheckPath') != '/health'
            or deploy.get('startCommand')):
        raise SetupError('Source Railway build/health settings are unexpected')
    digest = hashlib.sha256()
    for relative in sorted(paths):
        item = root / relative
        if item.is_symlink() or not item.is_file():
            raise SetupError('Public source contains a symlink or missing tracked file')
        data = item.read_bytes()
        digest.update(relative.encode() + b'\0' + hashlib.sha256(data).digest())
        if stage is not None:
            staged = Path(stage) / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(data)
            staged.chmod(item.stat().st_mode & 0o755)
    return {'source_commit': commit, 'source_manifest_sha256': digest.hexdigest()}


_SOURCE_PIN = ('source_commit', 'source_manifest_sha256')
_FIELDS = {'project_id', 'environment_id', 'service_name', 'source_path', *_SOURCE_PIN,
           'tenant_id', 'owner_phone', 'photon_line_number', 'photon_project_id',
           'photon_project_secret', 'gemini_api_key', 'sotto_runtime'}


def validate(config):
    """Check the protected input's shape and private manifest without touching the source or Railway."""
    if not isinstance(config, dict) or set(config) != _FIELDS:
        raise SetupError('Self-host input has missing or unexpected fields')
    for name in ('project_id', 'environment_id'):
        if not isinstance(config[name], str) or not re.fullmatch(r'[0-9a-f-]{36}', config[name]):
            raise SetupError(f'Invalid {name}')
    if not isinstance(config['service_name'], str) or not re.fullmatch(r'[a-z][a-z0-9-]{2,48}', config['service_name']):
        raise SetupError('Invalid service name')
    if (not isinstance(config['source_commit'], str)
            or not re.fullmatch(r'[0-9a-f]{40}', config['source_commit'])
            or not isinstance(config['source_manifest_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', config['source_manifest_sha256'])):
        raise SetupError('Source pin must contain exact commit and manifest SHA-256')
    if not isinstance(config['source_path'], str) or not Path(config['source_path']).is_absolute():
        raise SetupError('Source path must be absolute')
    for name in ('owner_phone', 'photon_line_number'):
        if not isinstance(config[name], str) or not re.fullmatch(r'\+[1-9][0-9]{7,14}', config[name]):
            raise SetupError(f'{name} must be one exact E.164 number')
    for name in ('tenant_id', 'photon_project_id', 'photon_project_secret', 'gemini_api_key'):
        if not isinstance(config[name], str) or not config[name].strip():
            raise SetupError(f'Missing {name}')
    settings = config['sotto_runtime']
    required_settings = {
            'account_id', 'line_key', 'instructions', 'chat_model_id',
            'native_media_model_id', 'timezone'}
    if (not isinstance(settings, dict) or not required_settings <= settings.keys()
            or settings.keys() - required_settings - {'background_unmetered'}):
        raise SetupError('Sotto runtime settings are invalid')
    if (any(not isinstance(settings[name], str) or not settings[name].strip()
            for name in required_settings)
            or type(settings.get('background_unmetered', False)) is not bool):
        raise SetupError('Sotto runtime settings are invalid')
    # Load from the reviewed public checkout even when invoked as a standalone script.
    spec = importlib.util.spec_from_file_location(
        'sotto_startup_config', Path(__file__).resolve().parents[1] / 'adapters/sotto/startup_config.py')
    startup_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(startup_config)
    # Reject an unusable private manifest before creating a paid provider resource.
    sample = {'service_id': 'sample-service', 'volume_id': 'sample-volume',
              'domain': 'sample.example.test', 'request_marker': 'sample-request',
              'runtime_bearer': 'r' * 48, 'bridge_token': 'b' * 48,
              'control_token': 'c' * 48}
    try:
        startup_config.render(launch_envelope(config, sample)['bundle']['manifest'])
    except startup_config.ManifestError as error:
        raise SetupError('Sotto runtime manifest is invalid') from error


def verify_source(config):
    """The checkout must still be the clean pinned source before any provider resource exists."""
    seen = source_receipt(config['source_path'])
    if any(config[key] != seen[key] for key in _SOURCE_PIN):
        raise SetupError('Public source differs from the pinned clean checkout')


def identity(config):
    value = {key: item for key, item in config.items()
             if key not in ('photon_project_secret', 'gemini_api_key', 'source_path')}
    value['photon_secret_sha256'] = hashlib.sha256(config['photon_project_secret'].encode()).hexdigest()
    value['gemini_key_sha256'] = hashlib.sha256(config['gemini_api_key'].encode()).hexdigest()
    return value


class Railway:
    def __init__(self, project, environment):
        self.project, self.environment = project, environment
        self.workspace = tempfile.TemporaryDirectory(prefix='sotto-selfhost-')

    def close(self):
        self.workspace.cleanup()

    def run(self, argv, *, cwd=None, timeout=120):
        try:
            result = subprocess.run(argv, cwd=cwd or self.workspace.name, text=True,
                                    capture_output=True, timeout=timeout, check=False)
        except OSError as error:
            raise NotSentError('Railway CLI could not start') from error
        except subprocess.TimeoutExpired as error:
            raise SetupError('Railway operation failed or timed out') from error
        if result.returncode:
            # Railway errors can echo secret variable values; never forward them.
            raise SetupError(f'Railway operation failed: {argv[1]}')
        return result.stdout

    def api(self, query, variables):
        fd, path = tempfile.mkstemp(prefix='railway-input-', dir=self.workspace.name)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(variables, stream)
            result = json.loads(self.run(['railway', 'api', query, '--variables', '@' + path]))
        except ValueError as error:
            raise SetupError('Railway returned invalid JSON') from error
        finally:
            Path(path).unlink(missing_ok=True)
        if result.get('errors'):
            raise SetupError('Railway API operation failed')
        return result.get('data') or result

    def target(self):
        data = self.api('query($p:String!,$e:String!){project(id:$p){id environments(first:100){edges{node{id sourceEnvironment{id}}} pageInfo{hasNextPage}}} environment(id:$e,projectId:$p){id projectId isEphemeral sourceEnvironment{id}}}',
                        {'p': self.project, 'e': self.environment})
        project, environment = data['project'], data['environment']
        rows = project['environments']
        if (project['id'] != self.project or environment['id'] != self.environment
                or environment['projectId'] != self.project or environment['isEphemeral']
                or rows['pageInfo']['hasNextPage'] or environment['sourceEnvironment'] is not None
                or len(rows['edges']) != 1
                or rows['edges'][0]['node']['id'] != self.environment):
            raise SetupError('Select one exact fresh, non-fork Railway environment')

    def services(self):
        rows = self.api('query($p:String!){project(id:$p){services(first:100){edges{node{id name}} pageInfo{hasNextPage}}}}',
                        {'p': self.project})['project']['services']
        if rows['pageInfo']['hasNextPage']:
            raise SetupError('Railway project service list exceeds safe page size')
        return [edge['node'] for edge in rows['edges']]

    def create_service(self, name, marker):
        return self.api('mutation($input:ServiceCreateInput!){serviceCreate(input:$input){id name}}',
                        {'input': {'projectId': self.project, 'environmentId': self.environment,
                                   'name': name, 'variables': {'SOTTO_SETUP_REQUEST': marker}}})['serviceCreate']

    def variables(self, service):
        return self.api('query($e:String!,$p:String!,$s:String){variables(environmentId:$e,projectId:$p,serviceId:$s)}',
                        {'e': self.environment, 'p': self.project, 's': service})['variables']

    def set_variable(self, service, name, value):
        self.api('mutation($input:VariableUpsertInput!){variableUpsert(input:$input)}',
                 {'input': {'projectId': self.project, 'environmentId': self.environment,
                            'serviceId': service, 'name': name, 'value': value,
                            'skipDeploys': True}})

    def volumes(self):
        rows = self.api('query($e:String!,$p:String!){environment(id:$e,projectId:$p){volumeInstances(first:100){edges{node{volumeId serviceId mountPath}} pageInfo{hasNextPage}}}}',
                        {'e': self.environment, 'p': self.project})['environment']['volumeInstances']
        if rows['pageInfo']['hasNextPage']:
            raise SetupError('Railway volume list exceeds safe page size')
        return [{'id': e['node']['volumeId'], 'service_id': e['node']['serviceId'],
                 'mount_path': e['node']['mountPath']} for e in rows['edges']]

    def create_volume(self, service):
        return self.api('mutation($input:VolumeCreateInput!){volumeCreate(input:$input){id}}',
                        {'input': {'projectId': self.project, 'environmentId': self.environment,
                                   'serviceId': service, 'mountPath': '/data'}})['volumeCreate']['id']

    def domains(self, service):
        data = self.api('query($e:String!,$s:String!){serviceInstance(environmentId:$e,serviceId:$s){domains{serviceDomains{domain targetPort} customDomains{domain}}}}',
                        {'e': self.environment, 's': service})['serviceInstance']['domains']
        if data['customDomains']:
            raise SetupError('Existing custom domain requires manual review')
        return [{'domain': item['domain'], 'target_port': item['targetPort']}
                for item in data['serviceDomains']]

    def create_domain(self, service):
        # 8080 is the target the pilot verified end to end with the receiver's injected PORT.
        return self.api('mutation($input:ServiceDomainCreateInput!){serviceDomainCreate(input:$input){domain targetPort}}',
                        {'input': {'environmentId': self.environment, 'serviceId': service,
                                   'targetPort': DOMAIN_PORT}})['serviceDomainCreate']

    def instance(self, service):
        return self.api('query($e:String!,$s:String!){serviceInstance(environmentId:$e,serviceId:$s){startCommand source{image repo} latestDeployment{id status meta}}}',
                        {'e': self.environment, 's': service})['serviceInstance']

    def build_settings(self, service):
        return self.api('query($e:String!,$s:String!){serviceInstance(environmentId:$e,serviceId:$s){dockerfilePath rootDirectory healthcheckPath healthcheckTimeout}}',
                        {'e': self.environment, 's': service})['serviceInstance']

    def set_build_settings(self, service, settings):
        self.api('mutation($e:String!,$s:String!,$i:ServiceInstanceUpdateInput!){serviceInstanceUpdate(environmentId:$e,serviceId:$s,input:$i)}',
                 {'e': self.environment, 's': service, 'i': settings})

    def deployment(self, identifier, service):
        row = self.api('query($id:String!){deployment(id:$id){id projectId environmentId serviceId status meta}}',
                       {'id': identifier})['deployment']
        if (row['id'], row['projectId'], row['environmentId'], row['serviceId']) != (
                identifier, self.project, self.environment, service):
            raise SetupError('Deployment belongs to another Railway target')
        return row

    def deployment_statuses(self, service):
        statuses, cursor, seen = {}, None, set()
        for _ in range(100):
            rows = self.api('query($input:DeploymentListInput!,$after:String){deployments(input:$input,first:100,after:$after){edges{node{id projectId environmentId serviceId status}} pageInfo{hasNextPage endCursor}}}',
                            {'input': {'projectId': self.project, 'environmentId': self.environment,
                                       'serviceId': service}, 'after': cursor})['deployments']
            for edge in rows['edges']:
                row = edge['node']
                if ((row['projectId'], row['environmentId'], row['serviceId']) !=
                        (self.project, self.environment, service) or row['id'] in statuses):
                    raise SetupError('Deployment history differs from exact service')
                statuses[row['id']] = row['status']
            if not rows['pageInfo']['hasNextPage']:
                return statuses
            cursor = rows['pageInfo']['endCursor']
            if not cursor or cursor in seen or not rows['edges']:
                raise SetupError('Deployment history pagination did not advance')
            seen.add(cursor)
        raise SetupError('Deployment history exceeds safe page limit')

    def upload(self, service, source, message):
        self.run(['railway', 'up', '--project', self.project, '--environment', self.environment,
                  '--service', service, '--yes', '--detach', '--json', '--message', message],
                 cwd=source, timeout=600)

    def set_start(self, service, start):
        self.api('mutation($e:String!,$s:String!,$i:ServiceInstanceUpdateInput!){serviceInstanceUpdate(environmentId:$e,serviceId:$s,input:$i)}',
                 {'e': self.environment, 's': service, 'i': {'startCommand': start}})

    def volume_marker(self, volume):
        """Read the root-owned preparation marker; None only when Railway reports it absent.

        The same read Sotto Cloud's managed provisioner uses. Railway output is only classified,
        never forwarded, because a CLI diagnostic can echo private values.
        """
        with tempfile.TemporaryDirectory(prefix='sotto-marker-', dir=self.workspace.name) as directory:
            destination = Path(directory) / 'marker.json'
            try:
                result = subprocess.run(
                    ['railway', 'volume', '--project', self.project, '--environment', self.environment,
                     'files', '--volume', volume, 'download', '/.sotto-volume.json', str(destination),
                     '--json'], cwd=self.workspace.name, text=True, capture_output=True,
                    timeout=120, check=False)
            except OSError as error:
                raise SetupError('Railway CLI could not start') from error
            except subprocess.TimeoutExpired as error:
                raise VolumeInspectionPendingError('Sotto volume file access timed out') from error
            if result.returncode:
                diagnostic = (result.stdout + result.stderr).lower()
                if (('has no active deployment in environment' in diagnostic
                     and 'deploy or restart the service before using file commands' in diagnostic)
                        or ('failed to initialize sftp session' in diagnostic and 'timeout' in diagnostic)):
                    raise VolumeInspectionPendingError('Sotto volume file access needs a running container')
                if (re.search(r'failed to stat remote path /(?:data/)?\.sotto-volume\.json(?:\s|$)', diagnostic)
                        and any(phrase in diagnostic for phrase in (
                            'no such file or directory', 'not found', 'does not exist'))):
                    return None
                raise SetupError('Could not inspect the Sotto /data volume marker')
            try:
                return json.loads(destination.read_text())
            except (OSError, ValueError) as error:
                raise SetupError('Sotto volume marker is unreadable') from error


def _service(provider, config, state, state_path, retry_unconfirmed=False):
    def named():
        services = provider.services()
        matches = [item for item in services if item['name'] == config['service_name']]
        if len(services) != len(matches):
            raise SetupError('Selected Railway project contains another service; use a fresh project')
        if len(matches) > 1:
            raise SetupError('More than one service has the requested name')
        return matches

    matches = named()
    if not matches and state['service_id']:
        raise SetupError('Saved service is missing from Railway; inspect exact target')
    if not matches and state['service_create_attempted']:
        matches = observe_after_attempt(named, matches)
        if not matches and not retry_unconfirmed:
            # An empty listing is not proof the earlier create was refused.
            raise UnconfirmedCreateError('service')
    if matches:
        # Only this setup's create call carries the private marker from protected state.
        service = matches[0]
        if (state['service_id'] not in (None, service['id'])
                or provider.variables(service['id']).get('SOTTO_SETUP_REQUEST') != state['request_marker']):
            raise SetupError('Existing service name is not owned by this setup')
    else:
        service = attempt(state, state_path, 'service_create_attempted', lambda: provider.create_service(
            config['service_name'], state['request_marker']))
        if service['name'] != config['service_name']:
            raise SetupError('Railway returned an unexpected service name')
    state['service_id'] = service['id']
    save_state(state_path, state)
    return service['id']


def _volume(provider, service, state, state_path, retry_unconfirmed=False):
    attached = provider.volumes()

    def expected(volume):
        return [{'id': volume, 'service_id': service, 'mount_path': '/data'}]

    if state['volume_id']:
        wait_for_attachment(provider.volumes, expected(state['volume_id']),
                            missing='Saved /data volume did not become visible',
                            different='Saved /data volume differs from Railway attachment',
                            initial=attached)
        return
    if state['volume_create_attempted']:
        attached = observe_after_attempt(provider.volumes, attached)
        # The environment holds only our service, so the sole volume at its /data is ours.
        if len(attached) == 1 and attached[0]['id'] and attached == expected(attached[0]['id']):
            state['volume_id'] = attached[0]['id']
            save_state(state_path, state)
            return
        if not attached and not retry_unconfirmed:
            raise UnconfirmedCreateError('/data volume')
    if attached:
        raise SetupError('Volume creation is ambiguous; inspect it before continuing')
    if provider.instance(service)['latestDeployment'] is not None:
        raise SetupError('Service deployed before /data volume was created')
    state['volume_id'] = attempt(state, state_path, 'volume_create_attempted',
                                 lambda: provider.create_volume(service))
    save_state(state_path, state)
    wait_for_attachment(provider.volumes, expected(state['volume_id']),
                        missing='Created /data volume did not attach to the exact service',
                        different='Created /data volume attached differently from the exact service')


def _domain(provider, service, state, state_path, retry_unconfirmed=False):
    def names():
        attached = provider.domains(service)
        if any(item.get('target_port') not in (DOMAIN_PORT, None) for item in attached):
            raise SetupError('Existing domain targets another port; inspect exact target')
        return [item['domain'] for item in attached]

    domains = names()
    if state['domain']:
        wait_for_attachment(names, [state['domain']],
                            missing='Saved domain did not become visible',
                            different='Saved domain differs from Railway attachment',
                            initial=domains)
        return
    if state['domain_create_attempted']:
        domains = observe_after_attempt(names, domains)
        if len(domains) == 1 and domains[0]:
            state['domain'] = domains[0]
            save_state(state_path, state)
            return
        if not domains and not retry_unconfirmed:
            raise UnconfirmedCreateError('domain')
    if domains:
        raise SetupError('Domain creation is ambiguous; inspect it before continuing')
    domain = attempt(state, state_path, 'domain_create_attempted', lambda: provider.create_domain(service))
    if domain.get('targetPort') != DOMAIN_PORT:
        raise SetupError('Railway returned an unexpected domain port')
    state['domain'] = domain['domain']
    save_state(state_path, state)
    wait_for_attachment(names, [state['domain']],
                        missing='Created domain did not attach to the exact service',
                        different='Created domain attached differently from the exact service')


def _build_settings(provider, service):
    desired = {'dockerfilePath': 'Dockerfile', 'rootDirectory': '/',
               'healthcheckPath': '/health', 'healthcheckTimeout': 600}
    actual = provider.build_settings(service)
    if any(actual.get(key) != value for key, value in desired.items()):
        if provider.instance(service)['latestDeployment'] is not None:
            raise SetupError('Existing Dockerfile or health settings differ after deployment')
        provider.set_build_settings(service, desired)
    if any(provider.build_settings(service).get(key) != value for key, value in desired.items()):
        raise SetupError('Dockerfile or health settings did not converge')


def launch_envelope(config, state):
    """Use the shared native/helper compiler's sole envelope construction."""
    spec = importlib.util.spec_from_file_location(
        'sotto_selfhost_configuration',
        Path(__file__).resolve().parents[1] / 'adapters/sotto/selfhost_configuration.py')
    configuration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(configuration)
    return configuration.build_launch_envelope(config, state)


def _variables(provider, service, config, state):
    desired = {'SOTTO_SETUP_REQUEST': state['request_marker'],
               'SOTTO_SELFHOST_LAUNCH': json.dumps(launch_envelope(config, state),
                                                   sort_keys=True, separators=(',', ':'))}
    existing = provider.variables(service)
    if any(not key.startswith('RAILWAY_') and (key not in desired or desired[key] != value)
           for key, value in existing.items()):
        raise SetupError('Existing service variables differ from this protected setup')
    for key, value in desired.items():
        if key not in existing:
            provider.set_variable(service, key, value)
    if any(provider.variables(service).get(key) != value for key, value in desired.items()):
        raise SetupError('Service variables did not converge')


def _set_start(provider, service, start, *, prior=()):
    instance = provider.instance(service)
    source = instance.get('source') or {}
    if (source.get('repo') or source.get('image')
            or instance['startCommand'] not in (None, '', start, *prior)):
        raise SetupError('Service source or start command differs from the reviewed Sotto setup')
    if instance['startCommand'] == start:
        return
    if instance['latestDeployment'] is not None and instance['startCommand'] not in prior:
        raise SetupError('Service settings differ after deployment')
    provider.set_start(service, start)
    if provider.instance(service)['startCommand'] != start:
        raise SetupError('Sotto start command did not converge')


def _upload_once(provider, service, config, state, state_path, phase, retry_unconfirmed=False):
    """Upload the pinned source once for this phase; adopt only the deployment carrying its message."""
    key = phase + '_deployment_id'
    if state.get(key):
        return state[key]
    before = set(state[phase + '_deployment_before'])
    message = ' '.join(('sotto-selfhost', state['request_marker'], phase, config['source_commit']))

    def newly_observed():
        statuses = provider.deployment_statuses(service)
        if not before <= statuses.keys():
            raise SetupError('Saved deployment history differs from Railway')
        return sorted(set(statuses) - before)

    attempted = phase + '_upload_attempted'
    new = newly_observed()
    if not new and state.get(attempted):
        new = observe_after_attempt(newly_observed, new)
        if not new and not retry_unconfirmed:
            # No deployment yet is not proof the earlier upload was refused; a second upload could
            # replace (and interrupt) the first, so only the operator re-sends it.
            raise UnconfirmedCreateError('source upload')
    if not new:
        with tempfile.TemporaryDirectory(prefix='sotto-source-') as staged:
            # Upload a copy of exactly the pinned tracked bytes, so ignored files never ride along.
            if source_receipt(config['source_path'], stage=staged) != {
                    name: config[name] for name in _SOURCE_PIN}:
                raise SetupError('Public source changed before upload')
            attempt(state, state_path, attempted, lambda: provider.upload(service, staged, message))
    # Railway's exact CLI message (setup marker, phase and pinned commit) is the only acceptable
    # receipt; an unattributable deployment stops setup for inspection.
    for _ in range(UPLOAD_POLLS):
        new = newly_observed()
        if len(new) > 1:
            raise SetupError('More than one new Sotto deployment appeared; inspect exact history before continuing')
        if new:
            seen = (provider.deployment(new[0], service).get('meta') or {}).get('cliMessage')
            if seen == message:
                state[key] = new[0]
                save_state(state_path, state)
                return new[0]
            if seen is not None:
                raise SetupError('An unrelated deployment appeared')
        time.sleep(2)
    raise SetupError('Source upload unresolved; inspect Railway before retrying')


def _wait_deployment(provider, service, identifier, start):
    """Wait for the exact deployment; return its image digest once Railway shows the reviewed command."""
    previous_status = None
    for _ in range(DEPLOYMENT_POLLS):
        row = provider.deployment(identifier, service)
        if row['status'] != previous_status:
            previous_status = row['status']
            print('Sotto deployment: ' + str(previous_status), file=sys.stderr, flush=True)
        if row['status'] == 'SUCCESS':
            meta = row.get('meta') or {}
            digest = meta.get('imageDigest')
            deploy = (meta.get('serviceManifest') or {}).get('deploy') or {}
            if digest and re.fullmatch(r'sha256:[0-9a-f]{64}', digest) and 'startCommand' in deploy:
                if deploy['startCommand'] != start:
                    raise SetupError('Sotto deployment has an unexpected start command')
                return digest
        if row['status'] in _STOPPED:
            raise SetupError('Sotto deployment did not succeed')
        time.sleep(5)
    raise SetupPendingError('Railway is still building or starting Sotto. Run the same command again '
                            'to continue; the saved deployment will be reused.')


def _phase(provider, service, config, state, state_path, phase, start, *, prior=(),
           retry_unconfirmed=False):
    before_key = phase + '_deployment_before'
    if before_key not in state:
        history = provider.deployment_statuses(service)
        latest = provider.instance(service)['latestDeployment']
        if phase == 'initializer':
            if history or latest is not None:
                raise SetupError('Sotto service deployed before fresh initialization')
        elif (history != {state['initializer_deployment_id']: 'SUCCESS'}
              or not latest or latest['id'] != state['initializer_deployment_id']):
            raise SetupError('Sotto initializer is not the sole completed deployment')
        state[before_key] = sorted(history)
        save_state(state_path, state)
    settings_key = phase + '_settings_attempted'

    def matches():
        return provider.instance(service)['startCommand'] == start

    matched = matches()
    if state.get(settings_key) and not matched:
        matched = observe_after_attempt(matches, matched)
        if not matched and not retry_unconfirmed:
            # The update is value-idempotent, but its outcome is unknown; the operator confirms
            # Railway shows the previous command before it is sent again.
            raise UnconfirmedCreateError('Sotto settings')
    if not matched:
        attempt(state, state_path, settings_key,
                lambda: _set_start(provider, service, start, prior=prior))
    else:
        _set_start(provider, service, start, prior=prior)
    identifier = _upload_once(provider, service, config, state, state_path, phase, retry_unconfirmed)
    digest = _wait_deployment(provider, service, identifier, start)
    if state.get(phase + '_image_digest') not in (None, digest):
        raise SetupError('Saved Sotto image differs from Railway')
    state[phase + '_image_digest'] = digest
    save_state(state_path, state)
    latest = provider.instance(service)['latestDeployment']
    if not latest or latest['id'] != identifier or latest['status'] != 'SUCCESS':
        raise SetupError('Sotto deployment is no longer latest and successful')
    return identifier


def _health(domain, *, prepared):
    request = urllib.request.Request('https://' + domain + '/health',
                                     headers={'User-Agent': 'sotto-selfhost-setup'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(16384)
            if response.status != 200:
                raise SetupError('Sotto health endpoint is not ready')
        if prepared:
            if body != b'OK':
                raise SetupError('Sotto preparation health is not exact')
        else:
            health = json.loads(body)
            if (not isinstance(health, dict) or health.get('status') != 'ok'
                    or not isinstance(health.get('work'), dict)
                    or not isinstance(health.get('delivery'), dict)
                    or not isinstance(health.get('model_lease'), dict)):
                raise SetupError('Sotto runtime health is not the reviewed receiver')
    except (OSError, ValueError) as error:
        raise SetupPendingError('Sotto health endpoint is not ready; resume exact deployment') from error


def _wait_health(domain, *, prepared):
    """Poll a bounded while: a fresh Railway domain can lag behind deployment SUCCESS."""
    deadline = time.monotonic() + HEALTH_WAIT_SECONDS
    while True:
        try:
            return _health(domain, prepared=prepared)
        except SetupPendingError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(HEALTH_POLL_SECONDS, remaining))


def _marker(provider, config, state):
    """Return True for the exact marker, None if absent, 'pending' if unreadable for now."""
    try:
        marker = provider.volume_marker(state['volume_id'])
    except VolumeInspectionPendingError:
        return 'pending'
    if marker is None:
        return None
    if marker != {'schema': 1, 'tenant_id': config['tenant_id'], 'volume_id': state['volume_id']}:
        raise SetupError('Mounted /data volume marker differs from this setup')
    return True


def _prepared(provider, service, config, state, state_path, retry_unconfirmed):
    """Prove fresh preparation finished before the runtime may replace the initializer.

    Proof is the initializer's exact health body, or the root-owned volume marker that
    preparation writes last. Once the bounded listener has provably closed, the operator may
    confirm with --retry-unconfirmed; the runtime then must show the exact marker.
    """
    if 'initializer_success_observed_at' not in state:
        state['initializer_success_observed_at'] = time.time()
        save_state(state_path, state)
    proof = None
    try:
        _wait_health(state['domain'], prepared=True)
        proof = 'health'
    except SetupPendingError:
        marker = _marker(provider, config, state)
        if marker is True:
            proof = 'volume_marker'
    if proof is None:
        if provider.deployment(state['initializer_deployment_id'], service)['status'] in _STOPPED:
            raise SetupError('Sotto initializer stopped before preparation was observed; inspect the volume')
        closed = time.time() >= (state['initializer_success_observed_at']
                                 + PREPARED_LISTENER_SECONDS + PREPARED_LISTENER_MARGIN_SECONDS)
        if not closed:
            raise SetupPendingError('Sotto preparation is not observable yet (health or /data marker). '
                                    'Run the same command again within 15 minutes of the initializer starting.')
        if marker is None:
            raise SetupError('Sotto initializer did not write its /data volume marker; inspect the volume')
        if not retry_unconfirmed:
            raise PreparationUnconfirmedError()
        proof = 'listener_closed'
    state['prepared_proof'] = proof
    if proof == 'health':
        state['prepared_health_observed'] = True
    save_state(state_path, state)


def _runtime_marker(provider, config, state, state_path):
    """After an operator-confirmed preparation, require the marker from the running runtime."""
    if state.get('prepared_proof') != 'listener_closed' or state.get('prepared_marker_verified'):
        return
    marker = _marker(provider, config, state)
    if marker == 'pending':
        raise SetupPendingError('Sotto runtime is up but its /data volume marker is not readable yet; '
                                'run the same command again')
    if marker is None:
        raise SetupError('Sotto runtime volume has no preparation marker; inspect the volume')
    state['prepared_marker_verified'] = True
    save_state(state_path, state)


@contextmanager
def _state_lock(state_path):
    """Serialize the setup state machine before reading or minting its marker."""
    lock_path = Path(str(state_path) + '.lock')
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise SetupError('Sotto state lock unavailable') from error
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077):
            raise SetupError('Sotto state lock must be a protected regular file')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SetupPendingError('Another Sotto setup owns this protected state path') from error
        yield
    finally:
        os.close(descriptor)


def reconcile(config, state_path, *, provider_factory=Railway, retry_unconfirmed=False):
    validate(config)
    with _state_lock(state_path):
        return _reconcile_locked(config, state_path, provider_factory=provider_factory,
                                 retry_unconfirmed=retry_unconfirmed)


def _reconcile_locked(config, state_path, *, provider_factory=Railway, retry_unconfirmed=False):
    state_path = Path(state_path)
    if state_path.exists():
        state = protected_json(state_path)
        if state.get('schema') != 3 or state.get('identity') != identity(config):
            raise SetupError('Saved setup state differs from protected Sotto input')
    else:
        state = {'schema': 3, 'identity': identity(config),
                 'request_marker': secrets.token_urlsafe(24),
                 'bridge_token': secrets.token_urlsafe(48),
                 'control_token': secrets.token_urlsafe(48),
                 'runtime_bearer': secrets.token_urlsafe(48),
                 'service_create_attempted': False, 'service_id': None,
                 'volume_create_attempted': False, 'volume_id': None,
                 'domain_create_attempted': False, 'domain': None}
    verify_source(config)
    save_state(state_path, state)
    provider = provider_factory(config['project_id'], config['environment_id'])
    try:
        provider.target()
        service = _service(provider, config, state, state_path, retry_unconfirmed)
        _volume(provider, service, state, state_path, retry_unconfirmed)
        _domain(provider, service, state, state_path, retry_unconfirmed)
        _variables(provider, service, config, state)
        _build_settings(provider, service)
        if not (state.get('prepared_health_observed') or state.get('prepared_proof')):
            _phase(provider, service, config, state, state_path, 'initializer', SOTTO_PREPARE_START,
                   retry_unconfirmed=retry_unconfirmed)
            _prepared(provider, service, config, state, state_path, retry_unconfirmed)
        elif not state.get('initializer_deployment_id'):
            raise SetupError('Saved preparation receipt is missing')
        deployment = _phase(provider, service, config, state, state_path, 'runtime',
                            SOTTO_RUNTIME_START, prior=(SOTTO_PREPARE_START,),
                            retry_unconfirmed=retry_unconfirmed)
        _wait_health(state['domain'], prepared=False)
        _runtime_marker(provider, config, state, state_path)
        history = provider.deployment_statuses(service)
        if (history.get(deployment) != 'SUCCESS'
                or any(identifier not in (state['initializer_deployment_id'], deployment)
                       and status not in _STOPPED
                       for identifier, status in history.items())):
            raise SetupError('Another Sotto runtime deployment may be active')
        return {'stage': 'receiver_reachable', 'model_status': 'unverified',
                'setup_status': 'pending', 'service_id': service,
                'volume_id': state['volume_id'], 'origin': 'https://' + state['domain'],
                'deployment_id': deployment, 'image_digest': state['runtime_image_digest'],
                'protected_state': str(state_path)}
    finally:
        provider.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspect-source', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--retry-unconfirmed', action='store_true',
                        help='re-send a create, upload or start setting only after checking Railway shows '
                             'none; or confirm a Sotto preparation whose 15-minute listener closed unseen')
    args = parser.parse_args(argv)
    try:
        if args.inspect_source:
            if args.config or args.state or args.retry_unconfirmed:
                parser.error('--inspect-source is used alone')
            print(json.dumps(source_receipt(args.inspect_source), sort_keys=True))
            return 0
        if not args.config or not args.state:
            parser.error('--config and --state are required')
        result = reconcile(protected_json(args.config), args.state,
                           retry_unconfirmed=args.retry_unconfirmed)
    except SetupPendingError as error:
        print(json.dumps({'stage': error.stage, 'message': str(error)}, sort_keys=True))
        return 2
    except SetupError as error:
        # Helper messages are fixed text, never Railway output or credential values.
        print('Sotto setup stopped: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
