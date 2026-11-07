"""Bounded explicit owner preferences using the existing canonical preference writer."""
from owner_tool_admission import validate_tool_admission, UnavailableError
from installed_writers import preferences_for

import re
_preferences = preferences_for(__file__)

__all__ = ['perform', 'UnavailableError']


def _one_off_tone_request(text):
    """Mirror the chat admission guard; exact copied words alone are not lasting consent."""
    shortening = r'(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:make\s+(?:it|this|that)\s+(?:(?:a\s+(?:bit|little)\s+)?shorter|more\s+(?:concise|terse))|shorten\s+(?:it|this|that)|tighten\s+(?:it|this|that)\s+up|too\s+long|be\s+more\s+(?:concise|terse)|(?:a\s+(?:bit|little)\s+)?shorter)(?:,?\s+(?:please|pls))?[.!?]*'
    original = r'(?:please\s+)?(?:(?:make\s+(?:it|this|that)\s+)?(?:a\s+bit\s+)?(?:shorter|longer|more\s+(?:concise|terse|casual|formal))|(?:shorten|rewrite|rephrase)\s+(?:it|this|that))(?:,?\s+(?:please|pls))?[.!?]*'
    return any(re.fullmatch(pattern, text.strip(), re.IGNORECASE) is not None for pattern in (shortening, original))


def perform(body, *, bearer, root, policy, data_root, capability, activation,
            telegram_verifier=None, chat_client=None):
    proof = validate_tool_admission(body, operations=('read', 'apply', 'inspect', 'preview'),
        kind='owner_preference_tool', domain='sotto.owner-preference-tool.v1', bearer=bearer, root=root, capability=capability,
        policy=policy, data_root=data_root, activation=activation,
        telegram_verifier=telegram_verifier, chat_client=chat_client)
    writer = _preferences()
    if type(body['args']) is dict and body['args'].get('operation') in ('usefulness-list', 'usefulness'):
        return _usefulness(body, proof, writer, policy)
    if type(body['args']) is dict and body['args'].get('operation') in ('snooze-nudges','unsnooze-nudges'):
        from datetime import datetime, timezone
        args = body['args']
        writer.owner_preference_args(args)
        if body['operation'] not in ('apply','inspect'):
            raise ValueError('quiet preference operation invalid')
        try:
            outcome = writer.admitted_owner_preference(proof, inspect=True)
            if outcome is None and body['operation'] == 'apply':
                if writer.owner_nudge_quiet_args(body['text']) != args:
                    return {'version':1,'status':'rejected','code':'owner_span_unavailable'}
                zone = policy['_configured_tz_name']()
                tz = policy['_tz_resolve'](zone)
                if not zone or tz is None:
                    raise UnavailableError('quiet preference timezone unavailable')
                when = datetime.fromtimestamp(body['admitted_at_ms']/1000, timezone.utc).astimezone(tz)
                outcome = writer.admitted_owner_preference(proof, now_local=when,
                    ambiguous_resume=writer.normalize_owner_quiet_text(body['text']).lower() == 'back to normal')
        except writer.OwnerPreferenceRefusalError as error:
            return {'version':1,'status':'rejected','code':error.code}
        if outcome is None:
            return {'version':1,'status':'not_found'}
        return {'version':1,'status':'committed',
                **{key:proof[key] for key in ('action_id','event_id','payload_hash','args_hash')}, 'outcome':outcome}
    if body['operation'] == 'read':
        if body['args'] is not None:
            raise ValueError('preference read arguments invalid')
        return {'version': 1, 'status': 'read', 'event_id': proof['event_id'],
                'payload_hash': proof['payload_hash'], 'coverage': 'explicit_only',
                **writer.read_owner_preferences()}
    if type(body['args']) is dict and body['args'].get('operation') == 'prioritize':
        from runtime_owner_loop import _load_writer
        _load_writer()
        import master_file
        args = body['args']
        if set(args) != {'operation', 'name'} or body['operation'] not in ('apply', 'inspect'):
            raise ValueError('priority command invalid')
        review = writer.owner_priority_cta(args['name'])
        try:
            saved = writer.admitted_owner_procedure(proof, master_file, review, None, inspect=True)
            if saved is None and body['operation'] != 'inspect':
                if not writer.priority_cta_matches(body['text'], args['name']):
                    return {'version': 1, 'status': 'rejected', 'code': 'owner_span_unavailable'}
                saved = writer.admitted_owner_procedure(proof, master_file, review, None)
        except writer.OwnerPreferenceRefusalError as error:
            return {'version': 1, 'status': 'rejected', 'code': error.code}
        if saved is None:
            return {'version': 1, 'status': 'not_found'}
        if saved['status'] == 'contacting':
            return {'version': 1, 'status': 'unknown'}
        if saved['status'] == 'rejected':
            return {'version': 1, 'status': 'rejected', 'code': saved['code'],
                    **({'capacity': saved['capacity']} if 'capacity' in saved else {})}
        return {'version': 1, 'status': 'committed',
                **{key: proof[key] for key in ('action_id', 'event_id', 'payload_hash', 'args_hash')},
                'outcome': saved['outcome']}
    if type(body['args']) is dict and body['args'].get('operation') in ('propose-procedure', 'append-procedure', 'propose-priorities', 'replace-priorities', 'propose-context', 'append-context'):
        import hashlib
        import json
        from runtime_owner_loop import _load_writer
        from fresh_routines import YES
        _load_writer()
        import master_file
        args = body['args']
        priorities = args['operation'] in ('propose-priorities', 'replace-priorities')
        context = args['operation'] in ('propose-context', 'append-context')
        proposed = 'propose-priorities' if priorities else 'propose-context' if context else 'propose-procedure'
        applied = 'replace-priorities' if priorities else 'append-context' if context else 'append-procedure'
        field = 'text' if priorities or context else 'rule'
        def canonical(value):
            return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        if body['operation'] == 'preview':
            if set(args) != ({'operation', 'section', field} if context else {'operation', field}) or args['operation'] != proposed:
                raise ValueError('standing rule proposal invalid')
            try:
                review = (writer.priorities_review(args[field], master_file) if priorities else
                          writer.owner_context_review(args['section'], args[field], master_file) if context else
                          writer.procedure_rule_review(args[field], master_file))
            except writer.OwnerPreferenceRefusalError as error:
                return {'version': 1, 'status': 'rejected', 'code': error.code}
            if args[field] not in body['text']:
                return {'version': 1, 'status': 'rejected', 'code': 'owner_span_unavailable'}
            return {'version': 1, 'status': 'preview', 'event_id': proof['event_id'],
                    'payload_hash': proof['payload_hash'], 'review': review}
        if set(args) != {'operation', 'review', 'proposal'} or args['operation'] != applied:
            raise ValueError('standing rule confirmation invalid')
        review, proposal = args['review'], args['proposal']
        if (type(review) is not dict or set(review) != ({'operation', 'section', 'text', 'previous_text', 'review_hash'} if priorities else {'operation', 'section', 'text', 'review_hash'} if context else {'operation', 'section', 'rule', 'review_hash'})
                or (writer.priorities_review(review.get('text'), master_file, previous_text=review.get('previous_text')) if priorities else writer.owner_context_review(review.get('section'), review.get('text'), master_file) if context else writer.procedure_rule_review(review.get('rule'), master_file)) != review):
            raise ValueError('standing rule review invalid')
        if (type(proposal) is not dict
                or set(proposal) != {'event_id', 'action_id', 'args_hash', 'accepted_dispatch_id', 'source_revision'}
                or proposal['source_revision'] != 'owner-only-context-v1'
                or any(type(proposal[key]) is not str or len(proposal[key]) != 64
                       or any(c not in '0123456789abcdef' for c in proposal[key])
                       for key in ('event_id', 'action_id', 'args_hash'))
                or type(proposal['accepted_dispatch_id']) is not str or not proposal['accepted_dispatch_id']
                or len(proposal['accepted_dispatch_id']) > 160
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in proposal['accepted_dispatch_id'])):
            raise ValueError('standing rule approval invalid')
        encoded = canonical({'operation': proposed, field: review[field], **({'section': review['section']} if context else {})})
        expected_action = hashlib.sha256(json.dumps([proposal['event_id'], 'owner_preference_tool', encoded],
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        if (proposal['action_id'] != expected_action
                or proposal['args_hash'] != hashlib.sha256(encoded.encode()).hexdigest()):
            raise ValueError('standing rule proposal proof invalid')
        # Historical saved proof is primary; confirmation grammar matters only for new contact.
        try:
            saved = writer.admitted_owner_procedure(proof, master_file, review, proposal, inspect=True)
        except writer.OwnerPreferenceRefusalError as error:
            return {'version': 1, 'status': 'rejected', 'code': error.code}
        if saved is None and body['operation'] != 'inspect':
            if proposal['event_id'] == proof['event_id'] or not YES.fullmatch(body['text'].strip()):
                return {'version': 1, 'status': 'rejected', 'code': 'target_unavailable'}
            try:
                saved = writer.admitted_owner_procedure(proof, master_file, review, proposal)
            except writer.OwnerPreferenceRefusalError as error:
                return {'version': 1, 'status': 'rejected', 'code': error.code}
        if saved is None:
            return {'version': 1, 'status': 'not_found'}
        if saved['status'] == 'contacting':
            return {'version': 1, 'status': 'unknown'}
        if saved['status'] == 'rejected':
            return {'version': 1, 'status': 'rejected', 'code': saved['code'], 'capacity': saved['capacity']}
        return {'version': 1, 'status': 'committed', 'action_id': proof['action_id'],
                'event_id': proof['event_id'], 'payload_hash': proof['payload_hash'],
                'args_hash': proof['args_hash'], 'outcome': saved['outcome']}
    if type(body['args']) is dict and body['args'].get('operation') == 'accept-procedure-offer':
        from runtime_owner_meeting_prep_request import selected_message_id, YES
        from runtime_owner_loop import _load_writer
        if set(body['args']) != {'operation', 'selection'}:
            raise ValueError('procedure offer arguments invalid')
        message = selected_message_id(body['args']['selection'])
        if body['identity']['channel'] != 'photon':
            raise UnavailableError('procedure offer profile unavailable')
        _load_writer()  # same installed canonical library roots as existing private writers
        import pending_offer
        import master_file
        route = activation.verified_current_photon_route(data_root, policy)
        def select():
            if (message is None or not YES.fullmatch(body['text'].strip()) or route is None):
                raise writer.OwnerPreferenceRefusalError('target_unavailable')
            offer = pending_offer.get_offer(message_id=message, latest=False,
                target='photon', chat_id=route['chat_id'])
            if not offer or offer.get('kind') != 'procedure' or not pending_offer._accepted_at(offer):
                raise writer.OwnerPreferenceRefusalError('target_unavailable')
            rule = offer.get('detail')
            if (type(rule) is not str or not rule or rule.strip() != rule or len(rule) > 300
                    or any((ord(c) < 32 and c not in '\t\r\n') or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in rule)):
                raise writer.OwnerPreferenceRefusalError('owner_span_unavailable')
            if master_file._HEADING.search(rule):
                raise writer.OwnerPreferenceRefusalError('target_unavailable')
            return offer
        if body['operation'] == 'preview':
            try:
                offer = select()
            except writer.OwnerPreferenceRefusalError:
                return {'version': 1, 'status': 'no_match'}
            return {'version': 1, 'status': 'match', 'offer_id': offer['offer_id'], 'kind': 'procedure'}
        try:
            saved = writer.admitted_procedure_offer(proof, pending_offer, master_file, select,
                inspect=body['operation'] == 'inspect')
        except writer.OwnerPreferenceRefusalError as error:
            return {'version': 1, 'status': 'rejected', 'code': error.code}
        if saved is None:
            return {'version': 1, 'status': 'not_found'}
        if saved['status'] == 'contacting':
            return {'version': 1, 'status': 'unknown'}
        if saved['status'] == 'rejected':
            return {'version': 1, 'status': 'rejected', 'code': saved['code']}
        return {'version': 1, 'status': 'committed', 'action_id': proof['action_id'],
            'event_id': proof['event_id'], 'payload_hash': proof['payload_hash'],
            'args_hash': proof['args_hash'], 'outcome': saved['outcome']}
    if body['operation'] == 'preview':
        raise ValueError('preference preview arguments invalid')
    writer.owner_preference_args(body['args'])
    if body['args']['operation'] == 'add' and body['args']['value'] not in body['text']:
        return {'version': 1, 'status': 'rejected', 'code': 'owner_span_unavailable'}
    try:
        outcome = writer.admitted_owner_preference(proof, inspect=True)
        if outcome is None and body['operation'] != 'inspect':
            if (body['args']['operation'] == 'add' and body['args']['field'] == 'tone_notes'
                    and _one_off_tone_request(body['text'])):
                return {'version': 1, 'status': 'rejected', 'code': 'owner_span_unavailable'}
            outcome = writer.admitted_owner_preference(proof)
    except writer.OwnerPreferenceRefusalError as error:
        return {'version': 1, 'status': 'rejected', 'code': error.code}
    if outcome is None:
        return {'version': 1, 'status': 'not_found'}
    return {'version': 1, 'status': 'committed', 'action_id': proof['action_id'],
            'event_id': proof['event_id'], 'payload_hash': proof['payload_hash'],
            'args_hash': proof['args_hash'], 'outcome': outcome}


def _usefulness(body, proof, writer, policy):
    import json
    import re
    from runtime_owner_loop import _load_writer
    _load_writer()
    import usefulness_feedback as feedback
    from personal_context import item_locator
    args = body['args']
    listing = args['operation'] == 'usefulness-list'
    required = {'operation', 'source_revision'} if listing else {'operation', 'rating', 'reference', 'archive_version', 'excerpt', 'source_revision'}
    if set(args) != required or type(args['source_revision']) is not str or not re.fullmatch(r'[a-f0-9]{64}', args['source_revision']):
        raise ValueError('usefulness arguments invalid')
    if listing and body['operation'] != 'read' or not listing and body['operation'] not in ('apply', 'inspect'):
        raise ValueError('usefulness operation invalid')
    if not listing:
        if (args['rating'] not in ('useful', 'not_useful') or type(args['reference']) is not str
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}_(morning|evening|welcome)', args['reference'])
                or type(args['archive_version']) is not str or not re.fullmatch(r'[a-f0-9]{64}', args['archive_version'])
                or type(args['excerpt']) is not str or len(args['excerpt']) > 320 or args['excerpt'] != args['excerpt'].strip()
                or any((ord(c) < 32 and c not in '\t\r\n') or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in args['excerpt'])):
            raise ValueError('usefulness target invalid')
    captured = None
    def guard():
        nonlocal captured
        current = policy['_source_authority']()
        if (type(current) is not dict or current.get('complete') is not True
                or current.get('revision') != args['source_revision']):
            raise writer.OwnerPreferenceRefusalError('source_unavailable')
        grants = frozenset(current.get('bridge_sources', []) + current.get('google_sources', []) + current.get('connector_sources', []))
        if captured is None:
            captured = grants
        elif captured != grants:
            raise writer.OwnerPreferenceRefusalError('source_unavailable')
    guard.permitted = lambda source: source in captured
    def select():
        guard()
        items = feedback.items(permitted=guard.permitted, versions=True)
        guard()
        selected = next((item for item in items if item['reference'] == args['reference']), None)
        if selected is None:
            raise writer.OwnerPreferenceRefusalError('target_unavailable')
        if selected['archive_version'] != args['archive_version']:
            raise writer.OwnerPreferenceRefusalError('target_changed')
        try:
            return item_locator(selected['text'], args['excerpt'].strip())
        except ValueError:
            raise writer.OwnerPreferenceRefusalError('target_unavailable') from None
    try:
        if listing:
            guard()
            items = feedback.items(permitted=guard.permitted, versions=True)
            guard()
            result = {'version': 1, 'status': 'usefulness_list', 'event_id': proof['event_id'], 'payload_hash': proof['payload_hash'], 'source_revision': args['source_revision'], 'items': items}
            if len(json.dumps(result).encode()) > 1048576:
                raise UnavailableError('usefulness listing unavailable')
            return result
        saved = writer.admitted_usefulness(proof, feedback, select, guard, inspect=body['operation'] == 'inspect')
    except writer.OwnerPreferenceRefusalError as error:
        return {'version': 1, 'status': 'rejected', 'code': error.code}
    if saved is None:
        return {'version': 1, 'status': 'not_found'}
    if saved['status'] == 'contacting':
        return {'version': 1, 'status': 'unknown'}
    return {'version': 1, 'status': 'committed', **{key: proof[key] for key in ('action_id', 'event_id', 'payload_hash', 'args_hash')}, 'outcome': saved['outcome']}


def http_response_bound(body, result):
    if (body['operation'] == 'read' and type(body.get('args')) is dict
            and body['args'].get('operation') == 'usefulness-list'
            and result.get('status') == 'usefulness_list'):
        return 1024 * 1024
    return 512 * 1024
