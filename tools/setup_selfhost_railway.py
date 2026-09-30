"""Resumable single-service Railway setup for the public self-host runtime.

Use a fresh, explicitly selected Railway project/environment and a protected
mode-0600 JSON input. The input names `project_id`, `environment_id`,
`service_name`, `source_path`, `source_commit`, `source_manifest_sha256`,
`owner_phone`, `photon_project_id`, `photon_project_secret`, and
`gemini_api_key`. This command creates only its own marked service, /data
volume and domain. Its protected state contains the generated Bridge token.
It never sends a message or makes a paid model probe. A create
or upload whose outcome is unknown is only observed afterwards, never sent
again, unless the operator passes --retry-unconfirmed.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
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


class NotSentError(SetupError):
    """The Railway CLI never started, so the request provably never left this machine."""


ATTACHMENT_WAIT_SECONDS = 30
ATTACHMENT_POLL_SECONDS = 2
DEPLOYMENT_POLLS = 360  # A cold Hermes image commonly takes more than five minutes.


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


def validate(config):
    required = {'project_id', 'environment_id', 'service_name', 'source_path',
                'source_commit', 'source_manifest_sha256', 'owner_phone',
                'photon_project_id', 'photon_project_secret', 'gemini_api_key'}
    if not isinstance(config, dict) or set(config) != required:
        raise SetupError('Self-host input has missing or unexpected fields')
    for name in ('project_id', 'environment_id'):
        if not isinstance(config[name], str) or not re.fullmatch(r'[0-9a-f-]{36}', config[name]):
            raise SetupError(f'Invalid {name}')
    if (not isinstance(config['service_name'], str)
            or not re.fullmatch(r'[a-z][a-z0-9-]{2,48}', config['service_name'])):
        raise SetupError('Invalid service name')
    if (not isinstance(config['owner_phone'], str)
            or not re.fullmatch(r'\+[1-9][0-9]{7,14}', config['owner_phone'])):
        raise SetupError('Owner must be one exact E.164 phone number')
    for name in ('photon_project_id', 'photon_project_secret', 'gemini_api_key'):
        if not isinstance(config[name], str) or not config[name].strip():
            raise SetupError(f'Missing {name}')
    if (not isinstance(config['source_commit'], str)
            or not re.fullmatch(r'[0-9a-f]{40}', config['source_commit'])
            or not isinstance(config['source_manifest_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', config['source_manifest_sha256'])):
        raise SetupError('Source pin must contain exact commit and manifest SHA-256')
    if not isinstance(config['source_path'], str) or not Path(config['source_path']).is_absolute():
        raise SetupError('Source path must be absolute')
    seen = source_receipt(config['source_path'])
    if any(config[key] != seen[key] for key in seen):
        raise SetupError('Public source differs from the pinned clean checkout')


def identity(config):
    return {key: value for key, value in config.items()
            if key not in ('photon_project_secret', 'gemini_api_key', 'source_path')} | {
        'photon_secret_sha256': hashlib.sha256(config['photon_project_secret'].encode()).hexdigest(),
        'gemini_key_sha256': hashlib.sha256(config['gemini_api_key'].encode()).hexdigest()}


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


def desired_variables(config, state):
    # SOTTO_DEPLOYMENT_MODE stays unset: start.sh treats unset as self-host (see the preset).
    return {'SOTTO_SETUP_REQUEST': state['request_marker'],
            'SOTTO_DATA': '/data',
            'SOTTO_CRON_DELIVER': 'photon', 'PHOTON_ALLOW_ALL_USERS': 'false',
            'BRIDGE_TOKEN': state['bridge_token'],
            'PHOTON_PROJECT_ID': config['photon_project_id'],
            'PHOTON_PROJECT_SECRET': config['photon_project_secret'],
            'PHOTON_HOME_CHANNEL': config['owner_phone'],
            'PHOTON_ALLOWED_USERS': config['owner_phone'],
            'GOOGLE_AI_API_KEY': config['gemini_api_key']}


def _variables(provider, service, config, state):
    desired = desired_variables(config, state)
    existing = provider.variables(service)
    # Railway's raw variables query includes generated RAILWAY_* entries after
    # domain creation (including the new service URL). They are platform-owned,
    # not extra application configuration.
    if any(not key.startswith('RAILWAY_') and (key not in desired or desired[key] != value)
           for key, value in existing.items()):
        raise SetupError('Existing service variables differ from this protected setup')
    for key, value in desired.items():
        if key not in existing:
            provider.set_variable(service, key, value)
    if any(provider.variables(service).get(key) != value for key, value in desired.items()):
        raise SetupError('Service variables did not converge')


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


def _upload(provider, service, config, state, state_path, retry_unconfirmed=False):
    instance = provider.instance(service)
    if instance['startCommand'] not in (None, '') or (instance['source'] or {}).get('image') or (
            instance['source'] or {}).get('repo'):
        raise SetupError('Service start/source settings differ from public Dockerfile defaults')
    message = 'sotto-selfhost ' + state['request_marker'] + ' ' + config['source_commit']
    latest = instance['latestDeployment']
    send = not state['source_upload_attempted']
    if state['source_upload_attempted'] and not state['deployment_id']:
        # Any deployment is resolved (or refused) below, never re-uploaded. No deployment yet is
        # not proof the earlier upload was refused, so it is sent again only on operator confirmation.
        def deployments():
            return provider.instance(service)['latestDeployment'] or provider.deployment_statuses(service)
        send = not observe_after_attempt(deployments, latest or provider.deployment_statuses(service))
        if send and not retry_unconfirmed:
            raise UnconfirmedCreateError('source upload')
    if send:
        if latest is not None:
            raise SetupError('Service deployed before reviewed source upload')
        with tempfile.TemporaryDirectory(prefix='sotto-source-') as staged:
            # Upload a copy of exactly the pinned tracked bytes, so ignored files never ride along.
            if source_receipt(config['source_path'], stage=staged) != {key: config[key] for key in (
                    'source_commit', 'source_manifest_sha256')}:
                raise SetupError('Public source changed before upload')
            attempt(state, state_path, 'source_upload_attempted',
                    lambda: provider.upload(service, staged, message))
    # Railway's exact CLI message (setup marker + pinned commit) and target are the only
    # acceptable receipt; an unattributable deployment stops setup for inspection.
    for _ in range(30):
        latest = provider.instance(service)['latestDeployment']
        if latest and (latest.get('meta') or {}).get('cliMessage') == message:
            row = provider.deployment(latest['id'], service)
            if state['deployment_id'] and state['deployment_id'] != row['id']:
                raise SetupError('Saved source deployment differs from Railway')
            state['deployment_id'] = row['id']
            save_state(state_path, state)
            break
        if latest and (latest.get('meta') or {}).get('cliMessage') not in (None, message):
            raise SetupError('An unrelated deployment appeared')
        time.sleep(2)
    if not state['deployment_id']:
        raise SetupError('Source upload unresolved; inspect Railway before retrying')
    previous_status = None
    for _ in range(DEPLOYMENT_POLLS):
        row = provider.deployment(state['deployment_id'], service)
        if row['status'] != previous_status:
            previous_status = row['status']
            print('Sotto deployment: ' + str(previous_status), file=sys.stderr, flush=True)
        if row['status'] == 'SUCCESS':
            meta = row.get('meta') or {}
            digest = meta.get('imageDigest')
            manifest = meta.get('serviceManifest') or {}
            deploy = manifest.get('deploy') or {}
            if (digest and re.fullmatch(r'sha256:[0-9a-f]{64}', digest)
                    and 'startCommand' in deploy):
                if deploy['startCommand'] not in (None, ''):
                    raise SetupError('Runtime has an unexpected start command')
                if state.get('image_digest') not in (None, digest):
                    raise SetupError('Saved runtime image differs from Railway')
                state['image_digest'] = digest
                save_state(state_path, state)
                return
        if row['status'] in ('FAILED', 'CRASHED', 'REMOVED', 'CANCELED', 'CANCELLED', 'SKIPPED'):
            raise SetupError('Reviewed runtime deployment did not succeed')
        time.sleep(5)
    raise SetupPendingError('Railway is still building or starting Sotto. Run the same command again to continue; the saved deployment will be reused.')


def reconcile(config, state_path, *, provider_factory=Railway, retry_unconfirmed=False):
    validate(config)
    state_path = Path(state_path)
    if state_path.exists():
        state = protected_json(state_path)
        if state.get('schema') != 1 or state.get('identity') != identity(config):
            raise SetupError('Saved setup state differs from protected input')
    else:
        state = {'schema': 1, 'identity': identity(config),
                 'request_marker': secrets.token_urlsafe(24),
                 'bridge_token': secrets.token_urlsafe(48),
                 'service_create_attempted': False, 'service_id': None,
                 'volume_create_attempted': False, 'volume_id': None,
                 'domain_create_attempted': False, 'domain': None,
                 'source_upload_attempted': False, 'deployment_id': None}
        save_state(state_path, state)
    provider = provider_factory(config['project_id'], config['environment_id'])
    try:
        provider.target()
        service = _service(provider, config, state, state_path, retry_unconfirmed)
        _volume(provider, service, state, state_path, retry_unconfirmed)
        _domain(provider, service, state, state_path, retry_unconfirmed)
        _variables(provider, service, config, state)
        _build_settings(provider, service)
        _upload(provider, service, config, state, state_path, retry_unconfirmed)
        request = urllib.request.Request('https://' + state['domain'] + '/health',
                                         headers={'User-Agent': 'sotto-selfhost-setup'})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status != 200:
                    raise SetupError('Runtime health endpoint is not ready')
                health = json.load(response)
                if (not isinstance(health, dict) or health.get('status') != 'ok'
                        or not isinstance(health.get('work'), dict)
                        or not isinstance(health.get('delivery'), dict)
                        or not isinstance(health.get('model_lease'), dict)):
                    raise SetupError('Receiver health response is not the reviewed runtime')
        except (OSError, ValueError) as error:
            raise SetupError('Runtime health endpoint is not ready') from error
        latest = provider.instance(service)['latestDeployment']
        if latest['id'] != state['deployment_id'] or latest['status'] != 'SUCCESS':
            raise SetupError('Another runtime deployment appeared')
        history = provider.deployment_statuses(service)
        if history.get(state['deployment_id']) != 'SUCCESS' or any(
                identifier != state['deployment_id'] and status not in (
                    'REMOVED', 'FAILED', 'CRASHED', 'CANCELED', 'CANCELLED', 'SKIPPED')
                for identifier, status in history.items()):
            raise SetupError('Another runtime deployment may be active')
        return {'stage': 'receiver_reachable', 'model_status': 'unverified',
                'setup_status': 'pending', 'service_id': service,
                'volume_id': state['volume_id'], 'origin': 'https://' + state['domain'],
                'deployment_id': state['deployment_id'], 'bridge_token_state': str(state_path)}
    finally:
        provider.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspect-source', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--retry-unconfirmed', action='store_true',
                        help='re-send a create or upload only after checking Railway shows none')
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
