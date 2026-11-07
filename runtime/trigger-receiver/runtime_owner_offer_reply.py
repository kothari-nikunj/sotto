"""One exact accepted source-loop offer; declines never change an obligation."""
from datetime import datetime, timezone
import hashlib
import json
import re

from owner_tool_admission import validate_tool_admission, UnavailableError

from runtime_owner_meeting_prep_request import selected_message_id

OFFER_ID = re.compile(r'[a-f0-9]{32}(?:[a-f0-9]{32})?')


def perform(body, *, bearer, root, policy, data_root, capability, activation,
            offers, writer, telegram_verifier=None, chat_client=None):
    proof = validate_tool_admission(body, operations=('apply', 'inspect', 'preview'),
        kind='owner_offer_reply', domain='sotto.owner-offer-reply.v1',
        bearer=bearer, root=root, capability=capability, policy=policy, data_root=data_root,
        activation=activation, telegram_verifier=telegram_verifier, chat_client=chat_client)
    if body['identity']['channel'] != 'photon':
        raise UnavailableError('offer reply profile unavailable')
    args = body['args']
    message_id = selected_message_id(args)
    preview = body['operation'] == 'preview'
    if preview and 'selected_offer' not in args:
        raise ValueError('offer preview selection required')
    text = ' '.join(body['text'].lower().rstrip(' .!…\t\r\n').split())
    action = ('declined' if text in offers.DECLINE_OFFER else
              'resolved' if text in offers.DISMISS_RESOLVED else
              'dismissed' if text in offers.DISMISS_DROPPED else None)
    if action is None:
        raise ValueError('offer reply wording invalid')
    if 'selected_offer' in args and action != 'declined':
        raise ValueError('unthreaded offer reply wording invalid')
    digest = hashlib.sha256(json.dumps(proof, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':')).encode()).hexdigest()
    saved = {'event_id': proof['event_id'], 'proof_hash': digest}
    if not preview:
        try:
            original = offers.inspect_reply(proof['event_id'], digest)
            if original is None:
                original = writer.op_source_offer_reply(None, saved,
                    now=datetime.fromtimestamp(body['admitted_at_ms']/1000, timezone.utc), inspect=True)
        except (ValueError, writer.OwnerLoopToolRefusalError):
            return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
        if original is not None:
            return original
        if body['operation'] == 'inspect':
            return {'version': 1, 'status': 'not_found'}
    if message_id is None:
        return {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
    route = activation.verified_current_photon_route(data_root, policy)
    if route is None:
        raise UnavailableError('offer reply route unavailable')
    offer = offers.get_offer(message_id=message_id, latest=False,
                             target='photon', chat_id=route['chat_id'])
    code = ('ambiguous_offer' if offer.get('ambiguous') else 'no_matching_offer' if not offer
            else 'unsupported_offer' if (offer.get('kind') not in offers.LOOP_KINDS
                and not (action == 'declined' and offer.get('kind') in ('meeting_prep', 'procedure', 'intention'))) else None)
    if code:
        if preview:
            return {'version': 1, 'status': 'no_match'}
        return {'version': 1, 'status': 'rejected', 'code': code}
    if (not isinstance(offer.get('offer_id'), str) or OFFER_ID.fullmatch(offer['offer_id']) is None
            or not offers._accepted_at(offer)):
        return {'version': 1, 'status': 'no_match'} if preview else {'version': 1, 'status': 'rejected', 'code': 'no_matching_offer'}
    if preview:
        return {'version': 1, 'status': 'match', 'offer_id': offer['offer_id'], 'kind': offer['kind']}
    receipt = {'version': 1, 'status': 'committed', **{key: proof[key] for key in
        ('action_id', 'event_id', 'payload_hash', 'args_hash')}, 'outcome': {
        'operation': 'apply', 'offer_id': offer['offer_id'], 'action': action,
        'loop_changed': action != 'declined'}}
    saved['receipt'] = receipt
    try:
        return offers.settle_reply(offer, saved, apply=(lambda: writer.op_source_offer_reply(
            offer.get('loop_snapshot'), saved,
            now=datetime.fromtimestamp(body['admitted_at_ms']/1000, timezone.utc)))
            if offer['kind'] in offers.LOOP_KINDS else None)
    except writer.OwnerLoopToolRefusalError as error:
        return {'version': 1, 'status': 'rejected', 'code': error.code}
    except ValueError as error:
        code = 'target_changed' if str(error) == 'offer reply target changed' else 'event_conflict'
        return {'version': 1, 'status': 'rejected', 'code': code}
