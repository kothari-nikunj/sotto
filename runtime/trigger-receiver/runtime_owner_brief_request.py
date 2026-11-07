"""Scoped owner brief admission; the existing queue owns effects and receipts."""
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path

from owner_tool_admission import validate_tool_admission, _text, UnavailableError
import requested_brief


_BRIEF_CLAUSE = re.compile(
    r"(?:please\s+)?(?:(?:(?:can|could|would)\s+you|(?:can|could)\s+i)\s+)?"
    r"(?:(?:(?:try|run|prepare|create|make|send|show|give|get|refresh|update)(?:\s+me)?|i(?:\s+would|'d)\s+like)\s+)?"
    r'(?:(?:a|an|my|the)\s+)?(?:(fresh|current|updated)\s+)?(?:(morning|evening|daily)\s+)?'
    r'brief(?:\s+again)?(?:,?\s+please)?[.!?]*', re.I)
_NEGATION = re.compile(r"\b(?:don't|do not|not|never|without|instead of|cancel|stop)\b", re.I)
_WITHDRAWAL = re.compile(r"(?:(?:actually|then|but|so|oh|ok|okay|and|please)[\s,]+)*"
                         r"(?:don't|do not|not|never|nevermind|no|nope|without|instead|cancel|stop|skip|forget|scratch|ignore|hold|wait)\b", re.I)


def parse_owner_brief_request(text):
    """Only a bounded composition continuation extends a complete request; it cannot select new
    request arguments. Composition direction is context for the existing writer.
    Mirrors ownerBriefIntent; the fast path remains narrower."""
    value = re.sub(r'\b(?:pls|plz)\b', 'please', text.strip().replace('’', "'").replace('‘', "'"), flags=re.I)
    if re.search(r"[\"“”]|(?:^|\s)'", value):
        return None
    extended = re.fullmatch(r'(.+?)(?:\s*[,;]\s*|\s+[—–]\s+)(.+)', value, flags=re.S)
    if (not _BRIEF_CLAUSE.fullmatch(value) and extended and _BRIEF_CLAUSE.fullmatch(extended[1])
            and not re.match(r'and\s+', extended[2], re.I)):
        modifier = extended[2]
        if (not re.match(r'(?:including|focus(?:ing|ed)|prioritiz(?:e|ing)|keep|covering|emphasiz(?:e|ing)|with)\b', modifier, re.I)
                or re.search(r'\b(?:morning|evening|daily|fresh|current|updated|refresh|update|retry|again|cached|brief)\b', modifier, re.I)
                or _NEGATION.search(modifier)
                or any(_WITHDRAWAL.match(clause) for clause in re.split(
                    r'\s*,?\s+and\s+|[.!?;,]\s*|\s+[—–]\s+', modifier, flags=re.I))):
            return None
        original = re.fullmatch(r'(.+?)(?:\s*[,;]\s*|\s+[—–]\s+)(.+)', text.strip(), flags=re.S)
        return {**parse_owner_brief_request(extended[1]), 'composition_direction': original[2]}
    clauses = [clause.strip() for clause in re.split(r'\s*,?\s+and\s+|[.!?]\s+', value, flags=re.I)]
    matches = [_BRIEF_CLAUSE.fullmatch(clause) for clause in clauses]
    if (sum(1 for match in matches if match) != 1
            or any(_NEGATION.search(clause) if match else _WITHDRAWAL.match(clause) for clause, match in zip(clauses, matches))):
        return None
    match = next(match for match in matches if match)
    intent = {'kind': match[2].lower() if match[2] and match[2].lower() != 'daily' else 'auto',
              'refresh': bool(match[1]) or bool(re.search(r'\b(?:refresh|update)\b', match[0], re.I))}
    if any(clause and not matched for clause, matched in zip(clauses, matches)):
        intent['composition_direction'] = text.strip()
    return intent


def owner_brief_intent(text):
    intent = parse_owner_brief_request(text)
    return {key: intent[key] for key in ('kind', 'refresh')} if intent else None


def perform(body, *, bearer, root, policy, data_root, capability, activation, queue, admit, telegram_verifier=None, chat_client=None):
    proof = validate_tool_admission(body, operations=('admit', 'inspect'), kind='owner_brief_request',
        domain='sotto.owner-brief-request.v1', bearer=bearer, root=root, capability=capability,
        policy=policy, data_root=data_root, activation=activation,
        telegram_verifier=telegram_verifier, chat_client=chat_client)
    args = body['args']
    if (type(args) is not dict or set(args) != {'kind', 'refresh', 'remainder', 'provider_message_id'}
            or args['kind'] not in ('morning', 'evening', 'auto') or type(args['refresh']) is not bool):
        raise ValueError('owner brief arguments invalid')
    _text(args['provider_message_id'], bound=512, whitespace=False)
    if args['remainder'] is not None:
        _text(args['remainder'], bound=16_000)
        if args['remainder'] not in body['text'] or args['remainder'] != args['remainder'].strip():
            raise ValueError('owner brief remainder invalid')
    zone = policy['_configured_tz_name']()
    tz = policy['_tz_resolve'](zone)
    if not zone or tz is None:
        raise UnavailableError('owner brief timezone unavailable')
    when = datetime.fromtimestamp(body['admitted_at_ms'] / 1000, timezone.utc).astimezone(tz)
    kind = args['kind'] if args['kind'] != 'auto' else ('morning' if when.hour < 12 else 'evening')
    telegram_binding = None
    if body['identity']['channel'] == 'telegram':
        try:
            telegram_binding = requested_brief.telegram_binding(telegram_verifier.check_current_channel(
                chat_client=chat_client, expected_channel_path=Path(data_root) / 'runtime/expected-channel.json',
                receiver_data_root=data_root, receiver_policy=policy,
                configured_zone_name=policy['_configured_tz_name'], expected_zone=zone),
                policy['_source_authority']())
            if (any(telegram_binding[key] != body['identity'][key] for key in
                    ('tenant_id', 'owner_id', 'account_id', 'line_key', 'bot_fingerprint'))
                    or telegram_binding['provider_thread_id'] != body['provider_thread_id']):
                raise ValueError('requested brief route changed')
        except Exception as error:
            raise UnavailableError('owner brief route unavailable') from error
        chat_id = telegram_binding['owner_id']
    else:
        route = activation.verified_current_photon_route(data_root, policy)
        if route is None:
            raise UnavailableError('owner brief route unavailable')
        chat_id = route['chat_id']
    identity = requested_brief.identity(body['identity']['owner_id'], chat_id, args['provider_message_id'])
    request_id = queue.job_id_for('run', 'requested:' + identity)
    receipt = {'version': 1, 'status': 'committed', **{key: proof[key] for key in
               ('action_id', 'event_id', 'payload_hash', 'args_hash')}, 'outcome': {
               'operation': 'admit', 'request_id': request_id, 'kind': kind,
               'refresh': args['refresh'], 'status': 'queued'}}
    metadata = {'version': 1, 'event_id': proof['event_id'], 'proof_hash': hashlib.sha256(
        json.dumps(proof, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
        'receipt': receipt}
    if telegram_binding is not None:
        metadata['telegram_binding'] = telegram_binding
    prior = queue.get(data_root, request_id)
    if prior is not None:
        original = json.loads(prior['admission']) if prior.get('admission') else None
        if (not original or original.get('event_id') != proof['event_id']
                or original.get('proof_hash') != metadata['proof_hash']):
            return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
        if original.get('telegram_binding') != telegram_binding:
            # The original effect still exists. A changed source/route cannot turn an
            # uncertain committed admission into a terminal rejection or a fresh retry.
            raise UnavailableError('owner brief original authority unavailable')
        return original['receipt']
    if queue.admission_for_event(data_root, proof['event_id']) is not None:
        return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
    if body['operation'] == 'inspect':
        return {'version': 1, 'status': 'not_found'}
    intent = parse_owner_brief_request(body['text'])
    if (not intent or intent['kind'] != 'auto' and args['kind'] != intent['kind']
            or args['refresh'] and not intent['refresh']):
        return {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
    direction = intent.get('composition_direction', '')
    try:
        code, outcome = admit({'kind': kind, 'refresh': args['refresh'], 'owner': body['identity']['owner_id'],
            'chat_id': chat_id, 'message_id': args['provider_message_id'],
            **({'composition_direction': direction} if direction else {})},
            admitted_when=when, admission=metadata)
    except ValueError as error:
        if str(error) == 'work admission conflict':
            return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
        raise
    if code == 200 and outcome.get('status') == 'known_request':
        original = queue.get(data_root, request_id)
        metadata_saved = json.loads(original['admission']) if original and original.get('admission') else None
        if metadata_saved and metadata_saved.get('proof_hash') == metadata['proof_hash']:
            if metadata_saved.get('telegram_binding') != telegram_binding:
                raise UnavailableError('owner brief original authority unavailable')
            return metadata_saved['receipt']
        return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
    if code != 200 or outcome.get('status') != 'queued':
        return {'version': 1, 'status': 'rejected', 'code': outcome.get('status', 'unavailable')}
    if outcome.get('request_id') != request_id:
        raise UnavailableError('owner brief admission unavailable')
    return receipt
