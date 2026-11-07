"""A closed shared skill invocation, admitted through the existing owner work queue."""
import hashlib
import json
import re

from runtime_meeting_prep_selection import validate_selection as validate_meeting_prep_selection

from runtime_read_admission import (READ_CONTEXT_WINDOW_MS, owner_read_request, contextual_read_request,
                                    owner_subject_mention, valid_research_subject, research_target_answer, explicit_read_reference,
                                    prep_research_request)

from owner_tool_admission import validate_tool_admission, UnavailableError
from shared_lib import load_shared_lib


def is_owner_meeting_prep_request(text, mode):
    value = text.strip().replace('’', "'").replace('‘', "'")
    if re.search(r"""["“”]|(?:^|\s)'|\b(?:don\x27t|do\s+not|no|not|never|without|instead\s+of|stop|cancel)\b""", value, re.I) or re.search(
            r'\b(?:blurb|(?:short|quick|brief)\s+summary|\d+(?:[-–]\d+)?\s+sentences?)\b', value, re.I):
        return False
    if mode == 'sweep':
        if not re.match(r'^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:prep(?:are)?|meeting\s+prep|who|brief\s+me|get\s+me\s+ready)\b', value, re.I):
            return False
        when = r"(?:today|tonight|tomorrow|this\s+(?:morning|afternoon|week))"
        patterns = [
            rf"prep(?:are)?(?:\s+me)?\s+(?:for\s+)?(?:(?:my|the|all|upcoming|{when}'s)\s+)+(?:meetings|calls|calendar)",
            rf"prep(?:are)?(?:\s+me)?\s+for\s+{when}",
            rf"meeting\s+prep(?=(?:\s+please)?[.!?]*$|\s+for\s+(?:(?:my|the|all)\s+meetings|{when}))",
            r"who\s+am\s+i\s+meeting", r"who(?:'s|\s+is)\s+in\s+my\s+meetings",
            r"brief\s+me\s+on\s+my\s+calendar",
            rf"get\s+me\s+ready\s+for\s+(?:{when}|(?:my|{when}'s)\s+meetings)",
        ]
        return bool(re.search(r'\b(?:' + '|'.join(patterns) + r')\b', value, re.I))
    # A remark about prep that already happened ("prep for Dana was great") is not a request.
    if re.search(r'^(?:the\s+|my\s+|that\s+|this\s+|your\s+)?prep(?:are)?(?:\s+for\s+[^.!?\n]*?)?\s+(?:is|was|went|looked|looks|seemed|felt)\s+(?:(?:really|very|so|super|pretty|all)\s+)?(?:done|great|good|fine|well|helpful|useful|perfect|ok|okay|amazing|excellent|finished|complete|ready|thorough)\b', value, re.I):
        return False
    return bool(re.search(r"""^(?:please\s+)?(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?(?:(?:(?:give|send|show)\s+me\s+)?(?:(?:full|meeting)\s+)?prep(?:are)?\b|(?:help\s+me\s+)?get\s+me\s+ready\s+for\b|help\s+me\s+get\s+ready\s+for\b|what\s+should\s+i\s+know\s+before\s+my\s+(?:meeting|call)\b|(?:redo|update|refresh)\s+[^.!?\n]+\bprep\b|same\s+(?:person|meeting),?\s+(?:updated|full)\s+prep\b)""", value, re.I)
                # Prep that names nobody ("full prep, please?") selects nobody; as a reply it can only
                # accept an offer, through the separate accepted-offer path.
                and not re.fullmatch(r"(?:please )?(?:(?:can|could|would) you (?:please )?)?(?:(?:give|send|show) me )?"
                                     r"(?:(?:full|meeting) )?prep(?:are)?(?: me)?(?: (?:please|pls|plz))?",
                                     ' '.join(re.sub(r'[^\w\s]', ' ', value.lower()).split())))


def is_owner_urgent_check(text):
    """Original attended trigger only; historical receipt validation is unchanged."""
    return re.fullmatch(
        r'(?:please\s+)?(?:check|(?:can|could|would)\s+you\s+(?:please\s+)?check)'
        r'\s+for\s+anything\s+urgent(?:\s+please)?[.!?]*', text.strip(), re.I) is not None


def is_owner_relationship_pulse_request(text):
    """Require a current explicit pulse request; prior history is context only."""
    value = re.sub(r'\b(?:pls|plz)\b', 'please', text.strip().replace('’', "'"), flags=re.I)
    if re.fullmatch(
            r"(?:please\s+)?(?:who\s+am\s+i\s+losing\s+touch\s+with|who\s+(?:haven't|have\s+not)\s+i\s+talked\s+to|who(?:'s|\s+is)\s+waiting\s+on\s+me)(?:,?\s+please)?[.!?]*", value, re.I):
        return True
    return bool(re.fullmatch(
        r'(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?'
        r'(?:(?:show|give|send|run|get)(?:\s+me)?\s+)?'
        r'(?:(?:a|my|the|quick|current|fresh)\s+)*(?:relationship[ -])?pulse'
        r'(?:\s+again)?(?:,?\s+please)?[.!?]*', value, re.I))


def is_owner_followup_request(text):
    """Bound offers use their separate admission; history alone is not authority."""
    value = re.sub(r'\b(?:pls|plz)\b', 'please', text.strip().replace('’', "'"), flags=re.I)
    return re.fullmatch(
        r'(?:please\s+)?(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?'
        r'(?:who\s+do\s+i\s+owe\s+(?:a\s+)?repl(?:y|ies)(?:\s+to)?|'
        r'(?:(?:show|list|check|run|get|give|send)(?:\s+me)?\s+'
        r'(?:(?:a|any|my|the|recent|backlog|open|post[ -]meeting)\s+)*follow[ -]?ups?|'
        r'(?:(?:any|my|recent|backlog|open|post[ -]meeting)\s+)*follow[ -]?ups)(?:\s+(?:now|again))?|'
        r"what\s+follow[ -]?ups\s+do\s+i\s+have|follow\s+up\s+on\s+(?:my|today's)\s+meetings|"
        r"what\s+did\s+i\s+commit\s+to|what\s+did\s+i\s+promise\s+in\s+(?:yesterday's|today's|my|recent)\s+meetings|"
        r'open\s+items\s+from\s+my\s+(?:granola\s+)?(?:notes|meetings)|'
        r'anything\s+to\s+send\s+after\s+my\s+meetings)(?:,?\s+please)?[.!?]*', value, re.I) is not None


def selection_digest(value):
    """Comparison-only admission metadata must not retain source text or route IDs."""
    return hashlib.sha256(value.encode()).hexdigest() if type(value) is str else None


def selected_company_matches(selected, company, thread):
    # Receipts written before digest metadata remain inspectable and usable within
    # the same existing 15-minute/source/identity fence. Never rewrite their proof.
    if 'company_digest' in selected or 'thread_digest' in selected:
        return (selected.get('company_digest') == selection_digest(company)
                and selected.get('thread_digest') == selection_digest(thread))
    return selected.get('company') == company and selected.get('provider_thread_id') == thread


def validate_args(args, text=None):
    if type(args) is not dict or not {'skill', 'selection'} <= set(args) or not set(args) <= {'skill', 'selection', 'source_revision', 'prior_selection', 'prior_request', 'prior_prep'} or args['skill'] not in ('meeting-prep', 'ask-research', 'followup', 'relationship-pulse', 'proactive'):
        raise ValueError('skill request invalid')
    if type(args['selection']) is not dict:
        raise ValueError('skill selection invalid')
    extra = set(args) - {'skill', 'selection'}
    if extra and args['skill'] != 'ask-research':
        raise ValueError('skill context invalid')
    if 'source_revision' in args and (type(args['source_revision']) is not str or not re.fullmatch(r'[a-f0-9]{64}', args['source_revision'])):
        raise ValueError('skill source revision invalid')
    selection = args['selection']
    if len(set(args) & {'prior_selection', 'prior_request', 'prior_prep'}) > 1:
        raise ValueError('conflicting research context')
    if 'prior_prep' in args:
        prior = args['prior_prep']
        if (type(prior) is not dict or set(prior) != {'provider_message_id', 'source_revision', 'route_digest'}
                or any(type(prior[k]) is not str or not re.fullmatch(r'[a-f0-9]{64}', prior[k])
                       for k in ('source_revision', 'route_digest'))
                or type(prior['provider_message_id']) is not str or not 0 < len(prior['provider_message_id']) <= 512
                or any(ord(c) < 32 or ord(c) == 127 for c in prior['provider_message_id'])
                or set(selection) != {'mode', 'company'} or selection.get('mode') != 'company'
                or args.get('source_revision') != prior['source_revision']):
            raise ValueError('prior prep selection invalid')
    if 'prior_selection' in args or 'prior_request' in args:
        prior = args.get('prior_selection', args.get('prior_request'))
        receipt = (type(prior) is dict and set(prior) == {'event_id', 'action_id', 'args_hash'}
                   and all(type(v) is str and re.fullmatch(r'[a-f0-9]{64}', v) for v in prior.values()))
        turn = (type(prior) is dict and set(prior) == {'event_id', 'text', 'admitted_at_ms',
                'accepted_dispatch_id', 'source_revision', 'thread_digest'}
                and all(type(prior[k]) is str and re.fullmatch(r'[a-f0-9]{64}', prior[k])
                        for k in ('event_id', 'source_revision', 'thread_digest'))
                and type(prior['text']) is str and 0 < len(prior['text']) <= 1000
                and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in prior['text'])
                and type(prior['admitted_at_ms']) is int
                and type(prior['accepted_dispatch_id']) is str and 0 < len(prior['accepted_dispatch_id']) <= 160)
        # The model resolves the follow-up; the receiver checks the selected subject
        # and trusted context proof, not a duplicate grammar of owner wording.
        if ('source_revision' not in args or not (receipt or turn)
                or receipt and (args['selection'].get('mode') != 'company' or 'prior_request' in args)):
            raise ValueError('prior research selection invalid')
    if args['skill'] in ('relationship-pulse', 'proactive'):
        if selection != {'mode': 'current'}:
            raise ValueError('relationship pulse selection invalid')
        return
    if args['skill'] == 'followup':
        if set(selection) != {'mode'} or selection.get('mode') not in ('recent', 'backlog'):
            raise ValueError('followup selection invalid')
        return
    if args['skill'] == 'ask-research':
        if selection.get('mode') == 'company':
            company = selection.get('company')
            if (not {'mode', 'company'} <= set(selection) or not set(selection) <= {'mode', 'company', 'operation'}
                    or ('operation' in selection and selection['operation'] != 'website') or type(company) is not str
                    or not company or company.strip() != company or len(company) > 200
                    or any(ord(c) < 32 or 127 <= ord(c) <= 159 or ord(c) in (0x2028, 0x2029)
                           or 0xD800 <= ord(c) <= 0xDFFF for c in company)
                    or text is not None and not owner_subject_mention(text, company) and not set(args) & {'prior_selection', 'prior_request', 'prior_prep'}):
                raise ValueError('Ask company invalid')
            return
        person = selection.get('person')
        if (set(selection) != {'mode', 'person'} or selection.get('mode') != 'person'
                or type(person) is not str or not person or person.strip() != person or len(person) > 200
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in person)
                or text is not None and not owner_subject_mention(text, person) and 'prior_selection' not in args and 'prior_request' not in args):
            raise ValueError('Ask person invalid')
        return
    validate_meeting_prep_selection(selection)


def perform(body, *, bearer, root, policy, data_root, capability, activation, queue, admit,
            telegram_verifier=None, chat_client=None):
    proof = validate_tool_admission(body, operations=('admit', 'inspect'), kind='owner_skill_request',
        domain='sotto.owner-skill-request.v1', bearer=bearer, root=root, capability=capability,
        policy=policy, data_root=data_root, activation=activation,
        telegram_verifier=telegram_verifier, chat_client=chat_client)
    validate_args(body['args'], body['text'])
    if body['identity']['channel'] != 'photon':
        raise UnavailableError('skill profile unavailable')
    proof_hash = hashlib.sha256(json.dumps(proof, sort_keys=True, ensure_ascii=False,
                                          separators=(',', ':')).encode()).hexdigest()
    original = queue.admission_for_event(data_root, proof['event_id'])
    if original is not None:
        if original.get('proof_hash') != proof_hash:
            return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
        return original['receipt']
    if body['operation'] == 'inspect':
        return {'version': 1, 'status': 'not_found'}
    if body['args']['skill'] == 'meeting-prep' and not is_owner_meeting_prep_request(body['text'], body['args']['selection']['mode']):
        return {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
    if body['args']['skill'] == 'followup' and not is_owner_followup_request(body['text']):
        return {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
    if body['args']['skill'] == 'relationship-pulse' and not is_owner_relationship_pulse_request(body['text']):
        return {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
    if body['args']['skill'] == 'proactive' and not is_owner_urgent_check(body['text']):
        return {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
    args = body['args']
    if args['skill'] == 'ask-research':
        subject = args['selection'].get('company', args['selection'].get('person', ''))
        if (not valid_research_subject(subject, args['selection']['mode'])
                or not (research_target_answer(body['text'], subject, args['selection']['mode']) if 'prior_request' in args
                        else contextual_read_request(body['text']) if 'prior_selection' in args
                        else owner_read_request(body['text']))):
            return {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
    if 'source_revision' in args:
        source = policy.get('_source_authority')
        current = source() if callable(source) else {}
        if current.get('complete') is not True or current.get('revision') != args['source_revision']:
            return {'version': 1, 'status': 'rejected', 'code': 'source_unavailable'}
    if 'prior_prep' in args:
        prior = args['prior_prep']
        route = policy.get('_retained_route_digest')
        if (not prep_research_request(body['text'])
                or not callable(route) or route() != prior['route_digest']):
            return {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
        reader = load_shared_lib('protected_brief_detail', anchor=__file__)
        try:
            domain = reader.accepted_research_company(data_root, prior['provider_message_id'],
                source_revision=args['source_revision'], route_digest=prior['route_digest'],
                admitted_at_ms=proof['admitted_at_ms'], context_window_ms=READ_CONTEXT_WINDOW_MS)
        except (OSError, ValueError, TypeError):
            domain = None
        if domain != subject or route() != prior['route_digest']:
            return {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
        if source() != current:
            return {'version': 1, 'status': 'rejected', 'code': 'source_unavailable'}
    if 'prior_selection' in args or 'prior_request' in args:
        prior = args.get('prior_selection', args.get('prior_request'))
        if 'text' in prior:
            # Runtime owns journal/transport provenance; this is NOT a second delivery lookup.
            # Independently constrain the current request, complete evidence, subject,
            # source revision and expiry. Only prior owner text can anchor a read.
            if ('prior_selection' in args and not explicit_read_reference(body['text'])
                    and not owner_read_request(prior['text'])):
                return {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
            if (prior['event_id'] == proof['event_id']
                    or prior['source_revision'] != args['source_revision']
                    or prior['thread_digest'] != selection_digest(proof['provider_thread_id'])
                    or not 0 <= proof['admitted_at_ms']-prior['admitted_at_ms'] <= READ_CONTEXT_WINDOW_MS
                    or not (owner_read_request(prior['text']) if 'prior_request' in args
                            else owner_subject_mention(prior['text'], subject))):
                return {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
        else:
            # Legacy receipt form: retain recovery/continuation for pre-upgrade queued research.
            saved = queue.admission_for_event(data_root, prior['event_id'])
            selected = saved.get('selected_company', {}) if saved else {}
            receipt = saved.get('receipt', {}) if saved else {}
            if (receipt.get('status') != 'committed' or receipt.get('action_id') != prior['action_id']
                    or receipt.get('args_hash') != prior['args_hash']
                    or not selected_company_matches(selected, args['selection']['company'], proof['provider_thread_id'])
                    or selected.get('identity_digest') != proof['identity_digest']
                    or selected.get('source_revision') != args['source_revision']
                    or type(selected.get('admitted_at_ms')) is not int
                    or not 0 <= proof['admitted_at_ms'] - selected['admitted_at_ms'] <= READ_CONTEXT_WINDOW_MS
                    or prior['event_id'] == proof['event_id']):
                return {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
    work_key = 'skill:' + proof['event_id']
    request_id = queue.job_id_for('run', work_key)
    receipt = {'version': 1, 'status': 'committed', **{key: proof[key] for key in
        ('action_id', 'event_id', 'payload_hash', 'args_hash')}, 'outcome': {
        'operation': 'admit', 'skill': body['args']['skill'], 'request_id': request_id, 'status': 'queued'}}
    metadata = {'version': 1, 'event_id': proof['event_id'], 'proof_hash': proof_hash, 'receipt': receipt}
    if 'source_revision' in args:
        metadata['source_revision'] = args['source_revision']
        if args['selection']['mode'] == 'company':
            metadata['selected_company'] = {'company_digest': selection_digest(args['selection']['company']),
                'thread_digest': selection_digest(proof['provider_thread_id']),
                'source_revision': args['source_revision'], **{key: proof[key] for key in
                ('identity_digest', 'admitted_at_ms')}}
    question = body['text']
    if 'prior_request' in args:
        question = args['prior_request']['text'] + '\nOwner-supplied lookup identity: ' + subject
    elif 'prior_selection' in args or 'prior_prep' in args:
        # Only the validated owner-bound continuation can add the selected subject.
        # Keep the fixed procedure's exact-target guard and original follow-up wording.
        question += '\nSubject from accepted conversation: ' + args['selection'].get('company', args['selection'].get('person', ''))
    elif args['skill'] == 'ask-research' and subject not in question:
        # Admission already matched the owner's whole subject case-insensitively.
        # Carry its exact selected spelling to the packaged worker's literal binding.
        question += '\nSubject from owner request: ' + subject
    try:
        code, result = admit(body['args']['selection'], work_key=work_key, admission=metadata,
                             feedback_identity=body['identity'], feedback_thread_id=body['provider_thread_id'],
                             **({'skill': 'ask-research', 'question': question}
                                if body['args']['skill'] == 'ask-research' else
                                {'skill': body['args']['skill']} if body['args']['skill'] in ('followup', 'relationship-pulse', 'proactive') else {}))
    except ValueError as error:
        if str(error) == 'work admission conflict':
            return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
        raise
    if code != 200 or result.get('status') != 'queued':
        return {'version': 1, 'status': 'rejected', 'code': result.get('status', 'unavailable')}
    if result.get('request_id') != request_id:
        raise UnavailableError('skill admission unavailable')
    return receipt
