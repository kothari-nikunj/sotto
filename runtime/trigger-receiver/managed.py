"""Managed capability gates; absent capability state never implies connected sources."""
import json
import os
from pathlib import Path
import re
import sys

try:
    from source_catalog import SOURCE_FIELDS as BRIDGE_SOURCE_FIELDS
except ModuleNotFoundError:  # source checkout; the image copies this shared leaf beside us
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff/_shared/lib'))
    from source_catalog import SOURCE_FIELDS as BRIDGE_SOURCE_FIELDS


# Consent IDs and payload projections come from the same map as the shared reader.

NOTICE_LABEL = 'status:no-sources'
NOTICE_TEXT = 'Scheduled briefs start once you connect a context source. You can still chat with Sotto here.'


def enabled():
    return os.environ.get("SOTTO_DEPLOYMENT_MODE") == "managed"


def read_state(data, name):
    try:
        state = json.loads((Path(data) / "config" / name).read_text())
        if state.get("tenant_id") != os.environ.get("SOTTO_TENANT_ID"):
            return {}
        return state
    except (OSError, ValueError, AttributeError):
        return {}


def messaging_activated(data):
    state = read_state(data, "photon-activation.json")
    return (state.get("activated") is True
            and state.get("owner") == os.environ.get("PHOTON_HOME_CHANNEL"))


def has_sources(data):
    # Written by authenticated consent/provisioning, not from event counts or Bridge
    # liveness: an established local source stays connected when its Mac sleeps.
    sources = read_state(data, "managed-capabilities.json").get("sources", {})
    return isinstance(sources, dict) and any(
        isinstance(v, dict) and v.get("consented") is True and v.get("connected") is True
        for key, v in sources.items() if key not in ("contacts", "google_contacts"))


def connection_status(data):
    """Metadata for the setup UI, read from the existing gates and delivery receipt."""
    try:
        first = json.loads((Path(data) / 'config/onboarding.json').read_text())
        phase = first.get('phase', 'waiting') if isinstance(first, dict) else 'waiting'
    except (OSError, ValueError):
        phase = 'waiting'
    if phase not in ('waiting', 'composing', 'queued', 'delivered', 'existing'):
        phase = 'waiting'
    return {'messaging_active': messaging_activated(data), 'context_connected': has_sources(data),
            'first_brief': phase}


def record_google_consent(data, scopes):
    """Called only after the setup-authenticated receiver completes Google OAuth.

    Persist actual grants; a declined permission never opens the source gate.
    Preserve unrelated Bridge capabilities under the shared transaction lock.
    """
    from connectors import json_transaction
    if not enabled():
        return
    if isinstance(scopes, str):
        scopes = scopes.split()
    path = str(Path(data) / 'config/managed-capabilities.json')
    with json_transaction(path, default={}) as state:
        tenant = os.environ['SOTTO_TENANT_ID']
        if state.get('tenant_id') != tenant:
            state.clear()
        state['tenant_id'] = tenant
        sources = state.setdefault('sources', {})
        grants = {
            'gmail': {'gmail.readonly', 'gmail.modify'},
            'calendar': {'calendar.readonly', 'calendar', 'calendar.events'},
            'google_contacts': {'contacts.readonly', 'contacts'},
        }
        for source, accepted in grants.items():
            granted = any('https://www.googleapis.com/auth/' + scope in scopes for scope in accepted)
            sources[source] = {'consented': granted, 'connected': granted}


def brief_hold(data):
    if not enabled():
        return None
    if not messaging_activated(data):
        return "Text Sotto once to activate iMessage delivery"
    if not has_sources(data):
        return "Connect a context source to receive scheduled briefs"
    return None


def record_bridge_consent(data, enabled_sources, connected_sources, allow_send=False):
    """The authenticated Bridge reports explicit toggles and successful local reads."""
    from connectors import json_transaction
    allowed = set(BRIDGE_SOURCE_FIELDS)
    if (not isinstance(allow_send, bool)
            or not isinstance(enabled_sources, list) or not isinstance(connected_sources, list)
            or not all(isinstance(s, str) for s in enabled_sources + connected_sources)
            or set(enabled_sources) - allowed or set(connected_sources) - set(enabled_sources)):
        raise ValueError('Invalid local source consent')
    with json_transaction(str(Path(data) / 'config/managed-capabilities.json'), default={}) as state:
        if state.get('tenant_id') != os.environ['SOTTO_TENANT_ID']:
            state.clear()
        state['tenant_id'] = os.environ['SOTTO_TENANT_ID']
        # Sending is an independent opt-in, never inferred from permission to read.
        # Older apps omit it and therefore keep sending disabled.
        state['allow_send'] = allow_send
        sources = state.setdefault('sources', {})
        for source in allowed:
            consented = source in enabled_sources
            connected = consented and (source in connected_sources or sources.get(source, {}).get('connected') is True)
            sources[source] = {'consented': consented, 'connected': connected}


def notice_pending(data):
    return (enabled() and messaging_activated(data) and not has_sources(data)
            and read_state(data, 'managed-status.json').get('no_sources_sent') is not True)


def validate_local_ingress(data, body, *, events=False):
    """Reject raw Bridge content from any unconsented source before staging/triage."""
    if not enabled():
        return
    sources = read_state(data, 'managed-capabilities.json').get('sources', {})
    allowed = {s for s in BRIDGE_SOURCE_FIELDS if sources.get(s, {}).get('consented') is True}
    if events:
        rows = body.get('events', [])
        if not isinstance(rows, list) or any(not isinstance(e, dict) or e.get('source') not in allowed for e in rows):
            raise ValueError('Bridge source is not enabled')
        return
    local = body.get('local_data') or {}
    if not isinstance(local, dict):
        raise ValueError('Invalid local data')
    permitted = {'generated_at', 'window_hours', 'source_status'}
    for source in allowed:
        permitted.update(BRIDGE_SOURCE_FIELDS[source])
    def content(value):
        if isinstance(value, dict):
            return any(content(v) for v in value.values())
        if isinstance(value, list):
            return bool(value)
        return bool(value)
    if any(content(value) for key, value in local.items() if key not in permitted):
        raise ValueError('Bridge payload contains an unconsented source')


def _tool_payload(result):
    if not isinstance(result, dict) or result.get('isError') is True:
        return {} if isinstance(result, dict) else None
    if isinstance(result.get('structuredContent'), dict):
        return result['structuredContent']
    content = result.get('content')
    if (isinstance(content, list) and content and isinstance(content[0], dict)
            and isinstance(content[0].get('text'), str)):
        try:
            value = json.loads(content[0]['text'])
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _consented_sources(data):
    sources = read_state(data, 'managed-capabilities.json').get('sources', {})
    return {key for key in BRIDGE_SOURCE_FIELDS
            if isinstance(sources.get(key), dict) and sources[key].get('consented') is True}


def _tool_allowed(data, name, arguments):
    """THE managed allow-decision for one Bridge tool. Both gates read it, so the gate that
    refuses before the Mac executes and the gate that inspects the result cannot disagree.

    `arguments=None` asks the name-only question ("may this tool ever be called here?"), which
    is what tools/list filtering needs. Unknown tools are denied. Sending needs its own
    explicit local opt-in and an operation ID for the Mac's durable receipt.

    An absent/empty `read_local` scope means "everything this Bridge has" to older builds and is
    how every skill calls it (`read_local(since_hours=...)`). It stays allowed here because the
    per-field consent check belongs to the result: `validate_tool_response` runs the returned
    payload through `validate_local_ingress` and withholds the whole result if an unconsented
    source carried content.
    """
    probe = arguments is None          # name-only question; no scope to judge yet
    if not probe and not isinstance(arguments, dict):
        return False
    arguments = {} if probe else arguments
    allowed = _consented_sources(data)
    if name == 'send_message':
        if read_state(data, 'managed-capabilities.json').get('allow_send') is not True:
            return False
        if probe:
            return True
        from message_targets import normalized_target
        channel = arguments.get('channel', 'auto')
        recipient = arguments.get('to')
        body = arguments.get('body')
        request_id = arguments.get('request_id')
        return (channel in ('imessage', 'sms', 'auto')
                and isinstance(recipient, str)
                and bool(normalized_target('imessage' if channel == 'auto' else channel, recipient))
                and isinstance(body, str) and bool(body.strip())
                and isinstance(request_id, str)
                and re.fullmatch(r'[A-Za-z0-9_-]{16,128}', request_id) is not None)
    if name == 'health':
        return True
    if name == 'read_history':
        requested = arguments.get('source')
        return probe or (isinstance(requested, str) and requested in allowed)
    if name == 'read_local':
        requested = arguments.get('sources')
        if requested is not None and not (isinstance(requested, list)
                                          and all(isinstance(item, str) for item in requested)):
            return False
        return not requested or set(requested) <= allowed
    if name == 'get_messages':
        return 'imessage' in allowed
    if name == 'get_contacts':
        return 'contacts' in allowed
    return False


def validate_tool_request(data, request):
    """The pre-execution gate: the relay refuses a denied `tools/call` WITHOUT forwarding it, so the
    Mac never runs a tool whose result managed mode would then have to throw away — which is no gate
    at all for a tool that acts (`send_message`)."""
    if not enabled():
        return True
    params = request.get('params') if isinstance(request, dict) else None
    if not isinstance(params, dict) or request.get('method') != 'tools/call':
        return False
    arguments = params.get('arguments', {})
    if not isinstance(arguments, dict):
        return False
    return _tool_allowed(data, params.get('name'), arguments)


def filter_tool_list(data, result):
    """Do not advertise a tool this deployment would refuse; the model should not see a
    `send_message` it can only be denied."""
    if not enabled() or not isinstance(result, dict) or not isinstance(result.get('tools'), list):
        return result
    return {**result, 'tools': [tool for tool in result['tools'] if isinstance(tool, dict)
                                and _tool_allowed(data, tool.get('name'), None) is True]}


def validate_tool_response(data, request, result):
    """Authorize content using the pending request and inspect the actual Bridge result.

    The same `_tool_allowed` decision the relay applied before forwarding runs again here —
    consent can change while a call is in flight — and then the payload itself is checked.
    """
    if not enabled():
        return True
    if not isinstance(request, dict):
        return False
    params = request.get('params')
    if request.get('method') != 'tools/call':
        return True
    if not isinstance(params, dict):
        return False
    name, arguments = params.get('name'), params.get('arguments', {})
    if not isinstance(arguments, dict):
        return False
    if name == 'health':
        return True
    if _tool_allowed(data, name, arguments) is not True:
        return False
    payload = _tool_payload(result)
    if payload is None:
        return False
    if isinstance(result, dict) and result.get('isError') is True:
        return True
    if name == 'read_history':
        requested = arguments.get('source')
        if not isinstance(requested, str):
            return False
        permitted = {'source', 'status', 'rows', 'complete', 'since', 'until', 'next_cursor'}
        return payload.get('source') == requested and not any(
            key not in permitted and bool(value) for key, value in payload.items())
    if name == 'read_local':
        try:
            validate_local_ingress(data, {'local_data': payload})
            return True
        except (ValueError, TypeError, AttributeError):
            return False
    return True   # get_messages / get_contacts: consent is the whole decision
