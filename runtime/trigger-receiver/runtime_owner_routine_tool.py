"""Fresh owner routines require an accepted proposal and a distinct explicit yes."""
import re

import fresh_routines as store
import owner_route_authority as authority
from owner_tool_admission import validate_tool_admission, UnavailableError

__all__ = ['perform', 'UnavailableError']


def current_binding(identity, policy, data_root, activation, *, telegram_verifier=None, chat_client=None):
    channel = identity['channel']
    profiles = {'photon': ('managed-photon-fixed-five-v1', 'self-host-photon-fixed-five-v1'),
                'telegram': ('self-host-telegram-fixed-five-v1',)}
    if channel not in profiles or policy.get('SOTTO_RUNTIME_PROFILE') not in profiles[channel]:
        raise UnavailableError('fresh routine profile unavailable')
    epoch = policy.get('SOTTO_ROUTINE_EPOCH', '')
    if type(epoch) is not str or not re.fullmatch('[a-f0-9]{32}', epoch):
        raise UnavailableError('routine epoch unavailable')
    authority.validate_authority(identity, policy, data_root)
    if channel == 'telegram':
        thread = f"telegram:{identity['owner_id']}"
        authority.validate_route(thread, identity, policy, data_root, activation,
                                 telegram_verifier=telegram_verifier, chat_client=chat_client)
        route = {'platform': 'telegram', 'chat_id': identity['owner_id'], 'thread_id': thread}
    else:
        route = activation.verified_current_photon_route(data_root, policy)
        line = 'shared' if identity['route_mode'] == 'first_contact_shared_project' else policy.get('SOTTO_IMESSAGE_NUMBER', '')
        thread = f"imessage:{route['chat_id']}~{line}"
        authority.validate_route(thread, identity, policy, data_root, activation, chat_client=chat_client)
    return {'identity': identity, 'profile': policy['SOTTO_RUNTIME_PROFILE'], 'epoch': epoch, 'route': route}


def perform(body, *, bearer, root, policy, data_root, capability, activation,
            telegram_verifier=None, chat_client=None):
    proof = validate_tool_admission(body, operations=('preview', 'list', 'apply', 'inspect'),
        kind='owner_routine_tool', domain=store.DOMAIN, bearer=bearer, root=root,
        capability=capability, policy=policy, data_root=data_root, activation=activation,
        telegram_verifier=telegram_verifier, chat_client=chat_client)
    binding = current_binding(body['identity'], policy, data_root, activation,
                              telegram_verifier=telegram_verifier, chat_client=chat_client)
    store.retire_other_bindings(data_root, binding)  # a proven new route retires old-route routines
    zone = policy['_configured_tz_name']()
    if body['operation'] == 'preview':
        if type(body['args']) is dict and body['args'].get('operation') == 'propose_adjust':
            try:
                old, value = store.preview_adjustment(data_root, body['args'], binding)
            except store.RefusalError as error:
                return {'version':1,'status':'rejected','code':error.code}
            return {'version':1,'status':'preview','event_id':proof['event_id'],'payload_hash':proof['payload_hash'],
                    'operation':'adjust','routine_id':old['routine_id'],'previous_definition':old['definition'],
                    'previous_definition_hash':old['definition_hash'],'definition':value,'definition_hash':store.digest(value)}
        try:
            value = store.definition(body['args'], zone, destination=store.DESTINATIONS[body['identity']['channel']])
            store.check_available(store.load(data_root), value, binding)
        except store.RefusalError as error:
            return {'version': 1, 'status': 'rejected', 'code': error.code}
        return {'version': 1, 'status': 'preview', 'event_id': proof['event_id'],
                'payload_hash': proof['payload_hash'], 'definition': value, 'definition_hash': store.digest(value)}
    if body['operation'] == 'list':
        if body['args'] is not None:
            raise ValueError('routine list arguments invalid')
        doc = store.load(data_root)
        rows = {rid: row for rid, row in doc['routines'].items() if row['binding'] == binding}
        return {'version': 1, 'status': 'list', 'event_id': proof['event_id'],
                'payload_hash': proof['payload_hash'], 'coverage': 'fresh_only', 'routines': store.list_rows({'routines': rows})}
    args = body['args']
    if type(args) is not dict:
        raise ValueError('routine arguments invalid')
    proof['confirmed'] = store.YES.fullmatch(body['text'].strip()) is not None
    existing = None
    try:
        existing = store.apply(data_root, proof, binding, inspect=True)
    except store.RefusalError as error:
        return {'version': 1, 'status': 'rejected', 'code': error.code}
    if args.get('operation') in ('create','adjust') and body['operation'] == 'apply' and existing is None:
        value = args.get('definition')
        store.validate_definition(value)
        if (args['operation'] == 'create' and value['zone'] != zone) or value['destination'] != store.DESTINATIONS[body['identity']['channel']]:
            return {'version': 1, 'status': 'rejected', 'code': 'unsupported_definition'}
        current = policy['_source_authority']()
        if not current.get('complete') or args.get('proposal', {}).get('source_revision') != current.get('revision'):
            return {'version': 1, 'status': 'rejected', 'code': 'proposal_unavailable'}
    try:
        outcome = existing if existing is not None else store.apply(data_root, proof, binding, inspect=body['operation'] == 'inspect')
    except store.RefusalError as error:
        return {'version': 1, 'status': 'rejected', 'code': error.code}
    if outcome is None:
        return {'version': 1, 'status': 'not_found'}
    return {'version': 1, 'status': 'committed', 'action_id': proof['action_id'],
            'event_id': proof['event_id'], 'payload_hash': proof['payload_hash'],
            'args_hash': proof['args_hash'], 'outcome': outcome}
