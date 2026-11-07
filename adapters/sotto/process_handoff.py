"""Build explicit child argv/environment projections for a future Sotto supervisor.

This is a pure boundary helper, not a launcher or selector. Callers must authenticate
the renderer input and deployment authorities before calling it. Returned environments
contain secrets by design and must be passed directly to exec, never logged or serialized.
The returned config digest is over startup_config's canonical config bytes; it does not
attest that the caller staged those bytes at node_config_path.

The default projects only the local loopback receiver contract. An optional, independently
verified provider attachment can project the existing receiver's public listener and domain.
Its shape validation does not itself authenticate provider state or enable runtime selection.
"""
import hashlib
import hmac
import re
from pathlib import Path
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from .startup_config import ManifestError, _owner_loop_capability, _payload_bytes, receiver_channel_expectation
    from .startup_config import render as render_startup
except ImportError:  # allows direct execution from an isolated adapter directory
    from startup_config import ManifestError, _owner_loop_capability, _payload_bytes, receiver_channel_expectation
    from startup_config import render as render_startup


class HandoffError(ValueError):
    """A sanitized configuration rejection; exception text never includes input values."""


_TOKEN = re.compile(r"\S{32,4096}\Z")
_MODEL_OVERRIDE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_TIMEZONE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*\Z")
_SERVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_RECEIVER_COMMON = frozenset((
    "SOTTO_MCP_TOKEN", "SOTTO_CONTROL_TOKEN", "SOTTO_TENANT_ID", "SOTTO_DATA",
    "SOTTO_HOME", "SOTTO_VOLUME_ID",
))
_RECEIVER_REQUIRED = _RECEIVER_COMMON | frozenset((
    "SOTTO_MCP_TOKEN", "SOTTO_CONTROL_TOKEN", "SOTTO_MODEL_PROXY_URL",
    "SOTTO_MODEL_PROXY_TOKEN", "SOTTO_TENANT_ID", "SOTTO_DATA", "SOTTO_HOME",
    "SOTTO_VOLUME_ID",
))
_RECEIVER_OVERRIDES = frozenset((
    "SOTTO_BRIEF_MODEL", "SOTTO_GEMINI_MODEL", "SOTTO_FALLBACK_MODEL",
    "SOTTO_RESEARCH_DEEP", "SOTTO_TIMEZONE", "SOTTO_BACKGROUND_UNMETERED",
))


@dataclass(frozen=True, repr=False)
class ChildHandoff:
    node_argv: tuple[str, ...]
    receiver_argv: tuple[str, ...]
    node_launch_uid: int
    receiver_wrapper_uid: int
    receiver_workload_uid: int
    runtime_config_sha256: str
    receiver_channel_expectation: object = field(repr=False)
    node_env: object = field(repr=False)
    receiver_env: object = field(repr=False)

    def __repr__(self):
        return "ChildHandoff(<environment values redacted>)"


def _reject():
    raise HandoffError("child handoff configuration is invalid")


def _absolute(value):
    return (isinstance(value, str) and value.startswith("/") and "\x00" not in value
            and all(part not in (".", "..") for part in value.split("/")))


def _secret(value):
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _exec_safe(value):
    if not isinstance(value, str) or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _origin(value):
    if not isinstance(value, str):
        _reject()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        _reject()
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost")
            or parsed.username or parsed.password or parsed.path != "/mcp"
            or parsed.query or parsed.fragment or port is None):
        _reject()
    return port


def _public_attachment(value, *, tenant, receiver_port, trusted_service_id):
    """Validate a caller-verified provider attachment without discovering one."""
    if (not isinstance(value, dict)
            or set(value) != {'tenantId', 'serviceId', 'domain', 'targetPort'}
            or value.get('tenantId') != tenant
            or not isinstance(trusted_service_id, str)
            or _SERVICE_ID.fullmatch(trusted_service_id) is None
            or value.get('serviceId') != trusted_service_id
            or type(value.get('targetPort')) is not int
            or value['targetPort'] != receiver_port):
        _reject()
    domain = value['domain']
    if (not isinstance(domain, str) or not 1 <= len(domain) <= 253
            or any(_DNS_LABEL.fullmatch(label) is None for label in domain.split('.'))
            or '.' not in domain):
        _reject()
    return domain


def build_child_handoff(manifest, receiver_authority, *,
                        stable_identity,
                        node_config_path, data_directory, skills_root,
                        receiver_overrides=None,
                        public_receiver_authority=None, trusted_service_id=None,
                        limited_profile=None):
    """Return direct-exec argv/env for Node and the receipt/native receiver.

    `manifest` is passed through startup_config.render so that renderer remains the
    schema authority. `stable_identity` and `receiver_authority` are explicit values
    authenticated by a trusted supervisor, not
    ``os.environ``. Only listed native receiver capabilities cross into the receiver.
    `receiver_overrides` is likewise an explicit owner-selected mapping; omitted keys
    mean no environment override, not ambient discovery.
    Node's channel and model credentials and derived read-only Bridge token remain in
    its private config file. Photon activation validation also requires the receiver's
    existing project identity/secret fields, so those exact credentials are projected
    to that process as well. No caller-provided arbitrary environment is copied.

    The helper does not start either process, prove provider flow, or treat activation
    ACK as a startup prerequisite. Node local service lifecycle may start before the
    receiver; scheduled delivery remains governed by the Node activation gate.
    """
    if (not isinstance(manifest, dict)
            or manifest.get("deployment") not in ("managed", "self_host")):
        _reject()
    if not all(_absolute(path) for path in (node_config_path, data_directory, skills_root)):
        _reject()
    try:
        config, receiver_projection = render_startup(manifest)
    except (ManifestError, TypeError, AttributeError):
        _reject()
    channel = config.get("channel")
    managed_photon = manifest['deployment'] == 'managed' and channel == 'photon'
    selfhost_photon = manifest['deployment'] == 'self_host' and channel == 'photon'
    telegram = manifest['deployment'] == 'self_host' and channel == 'telegram'
    if (not (managed_photon or selfhost_photon or telegram)
            or config.get("target") != channel
            or type(config.get("version")) is not int or config.get("version") != 1):
        _reject()
    if receiver_overrides is None:
        receiver_overrides = {}
    if (not isinstance(receiver_overrides, dict)
            or receiver_overrides.keys() - _RECEIVER_OVERRIDES):
        _reject()
    for name, value in receiver_overrides.items():
        if not isinstance(value, str) or not value or len(value) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            _reject()
        if name in ("SOTTO_BRIEF_MODEL", "SOTTO_GEMINI_MODEL", "SOTTO_FALLBACK_MODEL"):
            if not _MODEL_OVERRIDE.fullmatch(value):
                _reject()
        elif name == "SOTTO_RESEARCH_DEEP":
            if value not in ("0", "1"):
                _reject()
        elif name == "SOTTO_BACKGROUND_UNMETERED":
            if managed_photon or value not in ("true", "false"):
                _reject()
        else:
            if not _TIMEZONE.fullmatch(value):
                _reject()
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                _reject()
    owner = config.get("owner")
    if (not isinstance(owner, dict) or not isinstance(owner.get("accountId"), str)
            or not owner["accountId"] or not isinstance(owner.get("ownerId"), str)
            or not owner["ownerId"] or not isinstance(config.get("lineKey"), str)
            or not config["lineKey"]):
        _reject()
    credentials = config.get("credentials")
    if telegram:
        if (not isinstance(credentials, dict)
                or set(credentials) != {'telegramToken', 'telegramLinkFile'}
                or not _secret(credentials.get('telegramToken'))
                or not isinstance(credentials.get('telegramLinkFile'), dict)
                or credentials['telegramLinkFile'] != {
                    'bot_token': credentials['telegramToken'],
                    'allowed_user': int(owner['ownerId'])}):
            _reject()
        project_fingerprint = None
        bot_fingerprint = hashlib.sha256(
            ('sotto-telegram-bot-v1:' + credentials['telegramToken']).encode()).hexdigest()
    else:
        if (not isinstance(credentials, dict) or credentials.get("mode") != "cloud"
                or not isinstance(credentials.get("photonProjectId"), str)
                or not credentials["photonProjectId"]
                or not isinstance(credentials.get("photonProjectSecret"), str)
                or not credentials["photonProjectSecret"]):
            _reject()
        project_fingerprint = hashlib.sha256(
            ("sotto-photon-project-v1:" + credentials["photonProjectId"]).encode("utf-8")).hexdigest()
    tenant = config.get("tenantId", "")
    if not isinstance(tenant, str) or not tenant:
        _reject()
    first_contact = config.get("firstContact")
    if first_contact is not None and not isinstance(first_contact, dict):
        _reject()
    identity_keys = ({'channel', 'target', 'tenantId', 'accountId', 'ownerId',
                      'lineKey', 'providerThreadId', 'botFingerprint'} if telegram else
                     {'channel', 'target', 'tenantId', 'accountId', 'ownerId',
                      'lineKey', 'providerThreadId', 'expectedProviderLine',
                      'projectFingerprint'})
    shared_project = first_contact == {'mode': 'owner_dm_shared_project'}
    if shared_project:
        identity_keys = identity_keys | {'firstContactMode'}
    if not isinstance(stable_identity, dict) or set(stable_identity) != identity_keys:
        _reject()
    identity_values = {
        "channel": channel, "target": config.get("target"), "tenantId": tenant,
        "accountId": owner["accountId"], "ownerId": owner["ownerId"],
        "lineKey": config["lineKey"],
        "providerThreadId": config.get("providerThreadId"),
    }
    if telegram:
        identity_values['botFingerprint'] = bot_fingerprint
    else:
        identity_values['expectedProviderLine'] = (first_contact or {}).get('expectedProviderLine')
        identity_values['projectFingerprint'] = project_fingerprint
    if shared_project:
        identity_values['firstContactMode'] = 'owner_dm_shared_project'
    if identity_values != stable_identity:
        _reject()
    if channel == "photon":
        fixed_thread = config.get("providerThreadId")
        first_contact = config.get("firstContact")
        if (fixed_thread is None) == (first_contact is None):
            _reject()
        if first_contact is not None:
            if not shared_project and (not isinstance(first_contact, dict) or first_contact.get("mode") != "owner_dm"
                    or not isinstance(first_contact.get("expectedProviderLine"), str)
                    or not first_contact["expectedProviderLine"].startswith("+")):
                _reject()
        elif not isinstance(fixed_thread, str) or not fixed_thread.startswith("imessage:"):
            _reject()
    elif (config.get('providerThreadId') != f"telegram:{owner['ownerId']}"
          or first_contact is not None):
        _reject()

    bearer = config.get("bearerToken")
    bridge = config.get("bridge")
    model_proxy = config.get("modelProxy")
    model_direct = config.get('modelDirect')
    if (not _secret(bearer) or not isinstance(bridge, dict)
            or not _secret(bridge.get("readBearer"))):
        _reject()
    if not managed_photon:
        if (model_proxy is not None or not isinstance(model_direct, dict)
                or model_direct.get('provider') != 'google'
                or not _secret(model_direct.get('apiKey'))):
            _reject()
    else:
        if not isinstance(model_proxy, dict) or not _secret(model_proxy.get('bearer')):
            _reject()
        try:
            proxy_scheme = urlsplit(model_proxy.get("origin", "")).scheme
        except ValueError:
            _reject()
        if proxy_scheme != "https":
            _reject()
    runtime_port = config.get("port")
    receiver_port = _origin(bridge.get("url"))
    if (type(runtime_port) is not int or not 1 <= runtime_port <= 65535
            or runtime_port == receiver_port):
        _reject()
    expected_projection = {
        "SOTTO_CHAT_RUNTIME_URL": f"http://127.0.0.1:{runtime_port}",
        "SOTTO_CHAT_RUNTIME_TOKEN": bearer,
    }
    if receiver_projection != expected_projection:
        _reject()

    authority_keys = _RECEIVER_REQUIRED if managed_photon else _RECEIVER_COMMON
    if not isinstance(receiver_authority, dict) or set(receiver_authority) != authority_keys:
        _reject()
    native = dict(receiver_authority)
    if (not all(_secret(native.get(key)) for key in (
            "SOTTO_MCP_TOKEN", "SOTTO_CONTROL_TOKEN"))
            or native["SOTTO_TENANT_ID"] != tenant
            or not all(_absolute(native.get(key)) for key in ("SOTTO_DATA", "SOTTO_HOME"))
            or native["SOTTO_HOME"] != native["SOTTO_DATA"].rstrip("/") + "/sotto-home"
            or not isinstance(native["SOTTO_VOLUME_ID"], str) or not native["SOTTO_VOLUME_ID"]
            or len(native["SOTTO_VOLUME_ID"]) > 256 or any(ord(c) < 32 for c in native["SOTTO_VOLUME_ID"])
            or native["SOTTO_DATA"] != data_directory):
        _reject()
    if managed_photon and (not _secret(native.get('SOTTO_MODEL_PROXY_TOKEN'))
                         or native['SOTTO_MODEL_PROXY_URL'] != model_proxy.get('origin')
                         or native['SOTTO_MODEL_PROXY_TOKEN'] != model_proxy.get('bearer')):
        _reject()
    root_bearer = native["SOTTO_MCP_TOKEN"]
    if managed_photon:
        capability_secrets = (root_bearer, native['SOTTO_CONTROL_TOKEN'],
                              native['SOTTO_MODEL_PROXY_TOKEN'], bearer)
    else:
        capability_secrets = (root_bearer, native['SOTTO_CONTROL_TOKEN'], bearer,
                              model_direct['apiKey'])
        if telegram:
            capability_secrets += (credentials['telegramToken'],)
        else:
            capability_secrets += (credentials['photonProjectSecret'],)
    if len(set(capability_secrets)) != len(capability_secrets):
        _reject()
    derived = hmac.new(root_bearer.encode(), b"sotto-mcp", hashlib.sha256).hexdigest()
    if not hmac.compare_digest(derived, bridge["readBearer"]):
        _reject()
    owner_loop = config.get("ownerLoop")
    owner_command_profile = managed_photon or ((selfhost_photon or telegram)
                                                and isinstance(model_direct, dict))
    if not owner_command_profile and owner_loop is not None:
        _reject()
    if owner_command_profile:
        if (not isinstance(owner_loop, dict) or set(owner_loop) != {"mode", "identity", "bearer"}
                or owner_loop.get("mode") != "explicit_command"):
            _reject()
        try:
            expected_loop_bearer = _owner_loop_capability().derive(root_bearer, owner_loop["identity"])
        except (ValueError, OSError):
            _reject()
        if (not isinstance(owner_loop["bearer"], str)
                or not hmac.compare_digest(expected_loop_bearer, owner_loop["bearer"])):
            _reject()
        if owner_loop["bearer"] in (*capability_secrets, derived):
            _reject()
    if channel == 'photon':
        if config.get("firstContact") is not None:
            physical_line = manifest.get('photonLineNumber') if shared_project else config["firstContact"].get("expectedProviderLine")
        else:
            fixed = config.get("providerThreadId")
            if not isinstance(fixed, str) or "~" not in fixed:
                _reject()
            physical_line = fixed.rsplit("~", 1)[1]
        if (not isinstance(physical_line, str) or not re.fullmatch(r"\+[1-9][0-9]{7,14}", physical_line)
                or not re.fullmatch(r"\+[1-9][0-9]{7,14}", owner["ownerId"])):
            _reject()

    node_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/sotto-runtime",
        "USER": "sotto-runtime", "LOGNAME": "sotto-runtime", "LANG": "C.UTF-8",
    }
    receiver_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/sotto",
        "USER": "sotto", "LOGNAME": "sotto", "LANG": "C.UTF-8",
        "SOTTO_HOME": native["SOTTO_HOME"], "SOTTO_DATA": native["SOTTO_DATA"],
        "SOTTO_DEPLOYMENT_MODE": manifest['deployment'],
        "SOTTO_TENANT_ID": tenant,
        "SOTTO_SKILLS_ROOT": skills_root,
        "SOTTO_CRON_DELIVER": channel,
        "SOTTO_TRIGGER_PORT": str(receiver_port),
        "SOTTO_VOLUME_ID": native["SOTTO_VOLUME_ID"],
        "SOTTO_MCP_TOKEN": native["SOTTO_MCP_TOKEN"],
        "SOTTO_CONTROL_TOKEN": native["SOTTO_CONTROL_TOKEN"],
        **expected_projection,
    }
    if not managed_photon:
        receiver_env['GOOGLE_AI_API_KEY'] = model_direct['apiKey']
    if telegram:
        receiver_env.update({
            'TELEGRAM_BOT_TOKEN': credentials['telegramToken'],
            'TELEGRAM_ALLOWED_USERS': owner['ownerId'],
            'TELEGRAM_HOME_CHANNEL': owner['ownerId'],
        })
    else:
        receiver_env.update({
            'SOTTO_IMESSAGE_NUMBER': physical_line,
            'PHOTON_HOME_CHANNEL': owner['ownerId'],
            'PHOTON_ALLOWED_USERS': owner['ownerId'],
            'PHOTON_PROJECT_ID': credentials['photonProjectId'],
            'PHOTON_PROJECT_SECRET': credentials['photonProjectSecret'],
        })
        if managed_photon:
            receiver_env.update({
                'SOTTO_MODEL_PROXY_URL': native['SOTTO_MODEL_PROXY_URL'],
                'SOTTO_MODEL_PROXY_TOKEN': native['SOTTO_MODEL_PROXY_TOKEN'],
            })
    receiver_env.update(receiver_overrides)
    if limited_profile is not None:
        expected_profile = ('managed-photon-fixed-five-v1' if managed_photon else
                            'self-host-telegram-fixed-five-v1' if telegram else
                            'self-host-photon-fixed-five-v1')
        if limited_profile != expected_profile:
            _reject()
        tenant_root = Path(data_directory)
        if (Path(node_config_path) != tenant_root / 'node-config/config/runtime-config.json'
                or Path(config['stateDirectory']) != tenant_root / 'node-state'
                or Path(config['mediaRoot']) != tenant_root / 'node-media'):
            # The root-owned sticky tenant directory is traversable, while
            # these three disjoint children remain private to Node UID10000.
            # All three are included by the existing tenant archive boundary.
            _reject()
        receiver_env['SOTTO_RUNTIME_PROFILE'] = limited_profile
    if public_receiver_authority is not None:
        public_domain = _public_attachment(
            public_receiver_authority, tenant=tenant, receiver_port=receiver_port,
            trusted_service_id=trusted_service_id)
        receiver_env['PORT'] = str(receiver_port)
        receiver_env['RAILWAY_PUBLIC_DOMAIN'] = public_domain
    node_argv = ("/usr/local/bin/node", "/app/runtime/sotto-chat/dist/bootstrap/main.js",
                 "--config", node_config_path)
    # The wrapper captures control_vault before dropping to UID 10001, then
    # makes the receiver nondumpable before importing its runtime. The wrapper
    # itself must start as root; this argv/env builder does not execute or drop IDs.
    receiver_argv = ("/usr/local/bin/python3", "/app/adapters/sotto/managed_exec.py", "receiver",
                     "/app/trigger-receiver/receiver.py")
    if (not all(_exec_safe(arg) for arg in node_argv + receiver_argv)
            or any(not _exec_safe(key) or not _exec_safe(value)
                   for env in (node_env, receiver_env) for key, value in env.items())):
        _reject()
    config_bytes = _payload_bytes(config)
    return ChildHandoff(node_argv, receiver_argv, 10000, 0, 10001,
                        hashlib.sha256(config_bytes).hexdigest(),
                        MappingProxyType(receiver_channel_expectation(config)),
                        MappingProxyType(node_env), MappingProxyType(receiver_env))
