"""One accepted meeting-prep offer; trusted ingress selects, the existing queue acts."""
import hashlib
import json
import re

from owner_tool_admission import validate_tool_admission, _text, UnavailableError
from installed_writers import preferences_for

_preferences = preferences_for(__file__)

YES = re.compile(r'^(yes|yes please|yes, please|yes pls|yep|yup|sure|do it|full prep please|full prep pls)[.!]?$', re.I)
NAMED_PREP = re.compile(r'^(?:please\s+)?(?:(?:give|send|pull|get)(?:\s+me)?\s+)?(?:the\s+)?(?:full\s+|meeting\s+)?prep\s+(?:on|for)\s+([^\W\d_][^\d_]*?)[.!?]?$', re.I)
OFFER_ID = re.compile(r'[a-f0-9]{32}(?:[a-f0-9]{32})?')


class _InvalidPrepTargetError(ValueError):
    """A validated proposal's exact source target can no longer be offered."""


def selected_message_id(args):
    """Preserve the provider reply anchor; optionally bind a trusted accepted dispatch."""
    fields = {'reply_to_message_id', 'provider_sent_at_ms', 'provider_message_id'}
    if type(args) is not dict or set(args) not in (fields, fields | {'selected_offer'}):
        raise ValueError('offer arguments invalid')
    _text(args['provider_message_id'], bound=512, whitespace=False)
    anchor = args['reply_to_message_id']
    if anchor is not None:
        _text(anchor, bound=512, whitespace=False)
    stamp = args['provider_sent_at_ms']
    if stamp is not None and (type(stamp) is not int or not 0 < stamp <= 9007199254740991):
        raise ValueError('offer timestamp invalid')
    if 'selected_offer' not in args:
        return anchor
    selected = args['selected_offer']
    if (anchor is not None or stamp is None or type(selected) is not dict
            or set(selected) != {'mode', 'dispatch_id', 'message_id', 'provider_sent_at_ms'}
            or selected['mode'] != 'accepted_dispatch'):
        raise ValueError('offer selection invalid')
    _text(selected['dispatch_id'], bound=512, whitespace=False)
    _text(selected['message_id'], bound=512, whitespace=False)
    selected_stamp = selected['provider_sent_at_ms']
    if type(selected_stamp) is not int or not 0 < selected_stamp < stamp:
        raise ValueError('offer selection timestamp invalid')
    return selected['message_id']



def _proposal_authority(policy, revision):
    current = policy['_source_authority']()
    if (current.get('complete') is not True or current.get('revision') != revision
            or 'calendar' not in current.get('google_sources', [])):
        raise UnavailableError('source_unavailable')


def _proposal_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _provider_prep_start(event):
    """A complete source event must retain an offset-bearing timed occurrence."""
    from datetime import datetime
    try:
        stamp = datetime.fromisoformat(event.get('start', {}).get('dateTime', '').replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError) as error:
        raise _InvalidPrepTargetError('invalid_target') from error
    if stamp.tzinfo is None:
        raise _InvalidPrepTargetError('invalid_target')
    return stamp


def _proposal_event(args, reader):
    from datetime import datetime, timedelta, timezone
    from runtime_calendar_id import validate_calendar_id
    validate_calendar_id(args['calendar_id'])
    _text(args['event_id'], bound=256, whitespace=False)
    _text(args['attendee_email'], bound=320, whitespace=False)
    _text(args['start'], bound=128, whitespace=False)
    _text(args['source_revision'], bound=64, whitespace=False)
    if (re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', args['attendee_email']) is None
            or not re.fullmatch('[a-f0-9]{64}', args['source_revision'])):
        raise ValueError('prep proposal invalid')
    start = datetime.fromisoformat(args['start'].replace('Z', '+00:00'))
    if start.tzinfo is None or not 0 <= (start - _proposal_now()).total_seconds() <= 72 * 3600:
        raise _InvalidPrepTargetError('invalid_target')
    calendar = args['calendar_id']
    if calendar == 'primary':
        # Resolve the alias only from the existing consented, complete Calendar read.
        window = {key: (start + timedelta(seconds=offset)).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                  for key, offset in (('start', -1), ('end', 1))}
        primary = reader('calendar_get', {'calendar_id': 'primary', 'event_id': args['event_id']})
        original = primary.get('item')
        if primary.get('observation', {}).get('complete') is not True:
            raise UnavailableError('prep proposal source incomplete')
        if not isinstance(original, dict):
            raise _InvalidPrepTargetError('invalid_target')
        listed = reader('calendar_list', window)
        rows = listed.get('items', [])
        matches = [row for row in rows if row.get('id') == args['event_id']
                   and row.get('calendar_id') not in (None, '', 'primary')
                   and _provider_prep_start(row) == _provider_prep_start(original)
                   and row.get('summary') == original.get('summary')
                   and any(a.get('email', '').casefold() == args['attendee_email'].casefold()
                           for a in row.get('attendees', []))]
        if listed.get('observation', {}).get('complete') is not True:
            raise UnavailableError('prep proposal source incomplete')
        if len(matches) != 1:
            raise _InvalidPrepTargetError('invalid_target')
        calendar = matches[0]['calendar_id']
    result = reader('calendar_get', {'calendar_id': calendar, 'event_id': args['event_id']})
    event = result.get('item')
    if result.get('observation', {}).get('complete') is not True:
        raise UnavailableError('prep proposal source incomplete')
    if (not isinstance(event, dict)
            or event.get('id') != args['event_id'] or event.get('calendar_id') != calendar
            or event.get('owner_participates') is not True or event.get('status') in ('cancelled', 'canceled')):
        raise _InvalidPrepTargetError('invalid_target')
    actual = _provider_prep_start(event)
    if actual != start or (args['calendar_id'] == 'primary'
            and (_provider_prep_start(original) != actual
                 or any(event.get(key) != original.get(key) for key in ('id', 'summary', 'attendees', 'owner_participates')))):
        raise _InvalidPrepTargetError('invalid_target')
    from calendar_context import work_attendees
    from timeutil import configured_user_email
    people = work_attendees(event, configured_user_email())
    email = args['attendee_email'].casefold()
    if len(people) != 1 or people[0]['email'] != email:
        raise _InvalidPrepTargetError('invalid_target')
    title = event.get('summary')
    try:
        _text(title, bound=200)  # same 200 UTF-16 units/no DEL or surrogates as the typed client
    except ValueError as error:
        raise _InvalidPrepTargetError('invalid_target') from error
    if title.strip() != title or any(ord(c) < 32 for c in title):
        raise _InvalidPrepTargetError('invalid_target')
    return event, calendar, actual, email


def _proposal(body, proof, *, policy, data_root, activation, offers, calendar_reader):
    from pathlib import Path
    import os
    import stat
    prefs = _preferences()
    args = body['args']
    if set(args) != {'operation', 'calendar_id', 'event_id', 'start', 'attendee_email', 'source_revision'}:
        raise ValueError('prep proposal invalid')
    route = activation.verified_current_photon_route(data_root, policy)
    if route is None:
        raise UnavailableError('meeting prep route unavailable')
    parent = Path(data_root) / 'events'
    parent.mkdir(mode=0o700, exist_ok=True)
    path = parent / 'owner-meeting-prep-offers.json'
    for candidate in (parent, path):
        if candidate.exists() or candidate.is_symlink():
            meta = candidate.lstat()
            if (meta.st_uid != os.geteuid() or meta.st_mode & 0o022
                    or candidate == parent and not stat.S_ISDIR(meta.st_mode)
                    or candidate == path and (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
                                              or stat.S_IMODE(meta.st_mode) != 0o600)):
                raise ValueError('prep proposal storage invalid')
    with prefs.jsonstore.lock(str(path)):
        doc = prefs.jsonstore.read(str(path), {'version': 1, 'actions': {}}, strict=True)
        # Detached questions expire with the existing pending-offer window. Never let
        # old previews exhaust the active-record bound or recreate authority on replay.
        from retention import STAGED_DAYS
        cutoff = (_proposal_now().timestamp() - STAGED_DAYS * 86400) * 1000
        active = {event: row for event, row in doc['actions'].items()
                  if offers._fresh_offer(row.get('offer')) or
                  (row.get('accepted_delivery') and row['proof']['admitted_at_ms'] > cutoff)}
        if len(active) != len(doc['actions']):
            doc['actions'] = active
            prefs._durable_preferences_replace(str(path), doc)
        saved = doc['actions'].get(proof['event_id'])
        if saved is not None and saved['proof'] != proof:
            return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
        if saved is None:
            if body['operation'] == 'inspect':
                return {'version': 1, 'status': 'not_found'}
            if body['operation'] != 'preview':
                raise _InvalidPrepTargetError('prep proposal unavailable')
            if not 0 <= _proposal_now().timestamp()*1000 - proof['admitted_at_ms'] < offers.DEFAULT_TTL_MIN*60*1000:
                raise _InvalidPrepTargetError('prep proposal expired')
            _proposal_authority(policy, args['source_revision'])
            if sum(not row.get('recorded') for row in doc['actions'].values()) >= 1024 or calendar_reader is None:
                raise UnavailableError('prep proposal unavailable')
            event, calendar, start, email = _proposal_event(args, calendar_reader)
            _proposal_authority(policy, args['source_revision'])
            from timeutil import configured_tz, _resolve_tz
            local = start.astimezone(_resolve_tz(configured_tz()) or start.tzinfo)
            at = local.strftime('%I:%M %p').lstrip('0')
            question = f"I can prepare for {event['summary']} on {local.strftime('%A')} at {at}. Want me to?"
            scoped = 'gcal_' + hashlib.sha256(json.dumps([calendar, args['event_id']], separators=(',', ':')).encode()).hexdigest()
            meeting = {'event_id': scoped, 'start': event['start']['dateTime']}
            offer = offers.set_offer('meeting_prep', question, person=email, meeting=meeting, prepare_only=True)
            from datetime import datetime, timezone, timedelta
            admitted = datetime.fromtimestamp(proof['admitted_at_ms']/1000, timezone.utc)
            offer['ts'] = admitted.isoformat()
            offer['expires_at'] = (admitted + timedelta(minutes=offers.DEFAULT_TTL_MIN)).isoformat()
            review = {'question': question, 'title': event['summary'], 'calendar_id': calendar,
                      'provider_event_id': args['event_id'], 'start': meeting['start'], 'attendee_email': email,
                      'person': email, 'meeting': meeting, 'source_revision': args['source_revision'],
                      'offer_id': offer['offer_id']}
            review['review_hash'] = hashlib.sha256(json.dumps(review, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            saved = {'proof': proof, 'review': review, 'offer': offer, 'chat_id': route['chat_id']}
            doc['actions'][proof['event_id']] = saved
            prefs._durable_preferences_replace(str(path), doc)
        if saved['chat_id'] != route['chat_id']:
            raise UnavailableError('meeting prep route changed')
        review = saved['review']
        if body['operation'] == 'record_offer':
            delivery = body.get('accepted_delivery')
            if (type(delivery) is not dict or set(delivery) != {'accepted_dispatch_id', 'provider_message_id', 'source_revision', 'review_hash', 'payload_hash', 'accepted_at_ms'}
                    or delivery['source_revision'] != review['source_revision'] or delivery['review_hash'] != review['review_hash']
                    or delivery['payload_hash'] != hashlib.sha256(review['question'].encode()).hexdigest()
                    or type(delivery['accepted_at_ms']) is not int or not 0 < delivery['accepted_at_ms'] <= 9007199254740991
                    or not proof['admitted_at_ms'] <= delivery['accepted_at_ms'] <= _proposal_now().timestamp()*1000
                    or delivery['accepted_at_ms'] >= proof['admitted_at_ms'] + offers.DEFAULT_TTL_MIN*60000):
                raise ValueError('prep proposal acceptance invalid')
            _text(delivery['accepted_dispatch_id'], bound=160, whitespace=False)
            _text(delivery['provider_message_id'], bound=512, whitespace=False)
            if saved.get('accepted_delivery') not in (None, delivery):
                return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
            saved['accepted_delivery'] = delivery
            prefs._durable_preferences_replace(str(path), doc)
        base = {'version': 1, **{key: proof[key] for key in ('action_id', 'event_id', 'payload_hash', 'args_hash')}}
        if 'accepted_delivery' not in saved:
            return {**base, 'status': 'preview', 'review': review}
        delivery = saved['accepted_delivery']
        import delivery_effects
        if not saved.get('recorded'):
            live = offers.get_offer(message_id=delivery['provider_message_id'], latest=False,
                                    target='photon', chat_id=route['chat_id'], before=None)
            if (live.get('offer_id') == review['offer_id']
                    and live.get('question') == review['question']
                    and live.get('meeting') == review['meeting']
                    and live.get('person') == review['person']
                    and live.get('accepted_at') == delivery['accepted_at_ms']/1000):
                saved['recorded'] = True
                prefs._durable_preferences_replace(str(path), doc)
        if not saved.get('recorded') and body['operation'] != 'record_offer':
            return {'version': 1, 'status': 'unknown'}
        if not saved.get('recorded') and not offers._fresh_offer(saved['offer']):
            raise _InvalidPrepTargetError('prep proposal expired')
        if not saved.get('recorded'):
            _proposal_authority(policy, review['source_revision'])
            # A delivered question is not a fresh Calendar receipt: recheck the exact
            # canonical occurrence before making it actionable, never broaden its selection.
            if calendar_reader is None:
                raise UnavailableError('prep proposal unavailable')
            event, calendar, start, email = _proposal_event(
                {**args, 'calendar_id': review['calendar_id']}, calendar_reader)
            _proposal_authority(policy, review['source_revision'])
            if (event['summary'] != review['title'] or calendar != review['calendar_id']
                    or start != offers._parse(review['start']) or email != review['person']):
                raise _InvalidPrepTargetError('invalid_target')
        if not saved.get('recorded') and not delivery_effects.finalize([{'kind': 'pending_offer', 'offer': saved['offer']}],
                {'message_id': delivery['provider_message_id'], 'target': 'photon', 'chat_id': route['chat_id'],
                 'accepted_at': delivery['accepted_at_ms'] / 1000}):
            return {'version': 1, 'status': 'unknown'}
        saved['recorded'] = True
        prefs._durable_preferences_replace(str(path), doc)
        return {**base, 'status': 'recorded', 'offer_id': review['offer_id'], 'review_hash': review['review_hash'],
                **{key: delivery[key] for key in ('accepted_dispatch_id', 'provider_message_id', 'accepted_at_ms')}}


def perform(body, *, bearer, root, policy, data_root, capability, activation, queue, admit,
            offers, telegram_verifier=None, chat_client=None, calendar_reader=None):
    if isinstance(body.get('args'), dict) and body['args'].get('operation') == 'propose_prep':
        if 'accepted_delivery' in body and body.get('operation') != 'record_offer':
            raise ValueError('prep proposal acceptance invalid')
        admission = {key: value for key, value in body.items() if key != 'accepted_delivery'}
        proof = validate_tool_admission(admission, operations=('preview', 'record_offer', 'inspect'),
            kind='owner_meeting_prep_offer', domain='sotto.owner-meeting-prep-offer.v1',
            bearer=bearer, root=root, capability=capability, policy=policy, data_root=data_root,
            activation=activation, telegram_verifier=telegram_verifier, chat_client=chat_client)
        if body['identity']['channel'] != 'photon':
            raise UnavailableError('meeting prep profile unavailable')
        if body['operation'] == 'record_offer' and (
                type(body.get('accepted_delivery')) is not dict or set(body['accepted_delivery']) != {
                    'accepted_dispatch_id', 'provider_message_id', 'source_revision', 'review_hash', 'payload_hash', 'accepted_at_ms'}):
            raise ValueError('prep proposal acceptance invalid')
        try:
            return _proposal(body, proof, policy=policy, data_root=data_root, activation=activation,
                             offers=offers, calendar_reader=calendar_reader)
        except _InvalidPrepTargetError:
            return {'version': 1, 'status': 'rejected', 'code': 'invalid_target'}
    proof = validate_tool_admission(body, operations=('admit', 'inspect', 'preview'),
        kind='owner_meeting_prep_request', domain='sotto.owner-meeting-prep-request.v1',
        bearer=bearer, root=root, capability=capability, policy=policy, data_root=data_root,
        activation=activation, telegram_verifier=telegram_verifier, chat_client=chat_client)
    if body['identity']['channel'] != 'photon':
        raise UnavailableError('meeting prep profile unavailable')
    args = body['args']
    message_id = selected_message_id(args)
    preview = body['operation'] == 'preview'
    named = NAMED_PREP.fullmatch(body['text'].strip())
    if not YES.fullmatch(body['text'].strip()) and not named:
        raise ValueError('meeting prep affirmation invalid')
    digest = hashlib.sha256(json.dumps(proof, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':')).encode()).hexdigest()
    original = None if preview else queue.admission_for_event(data_root, proof['event_id'])
    if original is not None:
        if original.get('proof_hash') != digest:
            return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
        return original['receipt']
    if body['operation'] == 'inspect':
        return {'version': 1, 'status': 'not_found'}
    route = activation.verified_current_photon_route(data_root, policy)
    if route is None:
        raise UnavailableError('meeting prep route unavailable')
    if message_id is None:
        return {'version': 1, 'status': 'no_match'} if preview else {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
    offer = offers.get_offer(message_id=message_id,
        latest=False, target='photon', chat_id=route['chat_id'],
        before=None)
    code = ('ambiguous_offer' if offer.get('ambiguous') else 'no_matching_offer' if not offer
            else 'unsupported_offer' if offer.get('kind') != 'meeting_prep' else None)
    if code:
        if preview:
            return {'version': 1, 'status': 'no_match'}
        return {'version': 1, 'status': 'rejected', 'code': code}
    if named:
        # Resolve a first name against this exact delivered offer, never a new
        # first-name web search or a different offered calendar occurrence.
        person = ' '.join(str(offer.get('person', '')).casefold().split())
        requested = ' '.join(named.group(1).casefold().split())
        if not person or requested not in (person, person.split()[0]):
            return {'version': 1, 'status': 'no_match'} if preview else {'version': 1, 'status': 'rejected', 'code': 'no_matching_offer'}
    if (not isinstance(offer.get('offer_id'), str) or OFFER_ID.fullmatch(offer['offer_id']) is None
            or not offers._accepted_at(offer)):
        return {'version': 1, 'status': 'no_match'} if preview else {'version': 1, 'status': 'rejected', 'code': 'no_matching_offer'}
    occurrence = offer.get('meeting')
    if (type(occurrence) is not dict or set(occurrence) != {'event_id', 'start'}
            or not isinstance(occurrence['event_id'], str) or not occurrence['event_id']
            or not isinstance(occurrence['start'], str) or not occurrence['start']):
        return {'version': 1, 'status': 'no_match'} if preview else {'version': 1, 'status': 'rejected', 'code': 'unavailable'}
    if preview:
        return {'version': 1, 'status': 'match', 'offer_id': offer['offer_id'], 'kind': offer['kind']}
    request_id = queue.job_id_for('run', 'meeting-prep:' + offer['offer_id'])
    if queue.get(data_root, request_id) is not None:
        return {'version': 1, 'status': 'rejected', 'code': 'already_requested'}
    receipt = {'version': 1, 'status': 'committed', **{key: proof[key] for key in
        ('action_id', 'event_id', 'payload_hash', 'args_hash')}, 'outcome': {
        'operation': 'admit', 'request_id': request_id, 'offer_id': offer['offer_id'], 'status': 'queued'}}
    metadata = {'version': 1, 'event_id': proof['event_id'], 'proof_hash': digest, 'receipt': receipt}
    try:
        code, result = admit(offer, admission=metadata, feedback_identity=body['identity'],
                             feedback_thread_id=body['provider_thread_id'])
    except ValueError as error:
        if str(error) == 'work admission conflict':
            return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
        raise
    if code != 200 or result.get('status') != 'queued':
        return {'version': 1, 'status': 'rejected', 'code': result.get('status', 'unavailable')}
    if result.get('request_id') != request_id:
        raise UnavailableError('meeting prep admission unavailable')
    return receipt
