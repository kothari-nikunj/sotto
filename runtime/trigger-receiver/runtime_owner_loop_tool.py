"""Natural owner task tools preserve exact admitted owner text-span attribution."""
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
import re

import runtime_owner_loop as loops
import owner_route_authority as authority
from owner_tool_admission import _text, action_identity, validate_tool_envelope

UnavailableError = authority.UnavailableError
_HEX64 = re.compile(r'[a-f0-9]{64}\Z')


def _numeric_completion_matches(text, items):
    # Numeric tokens are exact references; the existing whole-request check owns supported wording.
    refs = list(re.finditer(r'(?<![\w#:/.-])#?[1-9][0-9]*(?![\w:/-]|\.[0-9])', text))
    if len(refs) != len(items) or any(len(ref[0].lstrip('#')) > 2 for ref in refs) or len({i['index'] for i in items}) != len(items):
        return False
    if any(not any(i['correction'] == ref[0] and i['index'] == int(ref[0].lstrip('#')) for i in items) for ref in refs):
        return False
    claim = re.sub(r'[.!]\s*(?:(?:you can|please)\s+)?close (?:them|those|these)[.!]?$', '', text, flags=re.I).strip()
    return resolve_many_names_match(claim, [{'name':i['correction'], 'correction':i['correction']} for i in items], 'owner_tasks')


def cleanup_selections_match(text, items, *, allow_stored_mixed_resolve=False):
    if items and all(item.get('action')=='resolve' for item in items):
        return (_numeric_completion_matches(text, items) or (allow_stored_mixed_resolve
            and resolve_many_names_match(text, [{'name':item['correction'], 'correction':item['correction']} for item in items], 'owner_tasks')))
    if not allow_stored_mixed_resolve and any(item.get('action')=='resolve' for item in items):
        return False

    spans = []
    for item in items:
        correction = item['correction']
        if text.count(correction) != 1:
            return False
        start = text.index(correction)
        spans.append((start, start + len(correction)))
    spans.sort()
    ws = r'[ \t\r\n]'
    if not spans or re.fullmatch(ws + '*', text[:spans[0][0]]) is None:
        return False
    separator = r'(?:[ \t\r\n,;]|\band\b)*'
    for previous, current in zip(spans, spans[1:]):
        if previous[1] > current[0] or re.fullmatch(separator, text[previous[1]:current[0]], re.I) is None:
            return False
    suffix = text[spans[-1][1]:]
    return re.fullmatch(separator + r'\.?'+ ws + '*', suffix, re.I) is not None


_WITHHELD_REVERSAL = re.compile(r"\b(?:not|never|neither|don't|don’t|do not|actually|kidding|unsure|wait|undo|cancel|scratch|them|both|it|those|these|all|everything|anything)\b", re.I)


def resolve_many_names_match(text, items, mode, *, exact_clause=False):
    claim=text.strip() if exact_clause else re.sub(r'(?:[.!]\s*|\s+[—–-]\s*)(?:what(?:(?:\'s|’s| is)? left|(?:\'s|’s)?(?: else| still)?(?: is)? open| remains)|any(?: [A-Za-z0-9_-]+){0,6} tasks? left|anything else open|what else)\?$', '', text.strip(), flags=re.I).strip()
    claim=claim if exact_clause else re.sub(r'[.!]\s*(?:you can|please) close (?:them|those|these)[.!]?$', '',claim,flags=re.I).strip()
    restatement=None if exact_clause else re.search(r'[.!]\s*(?:please\s+)?(?:close|resolve|mark)\s+(?:those|these|the)\s+(two|three|[2-9]|1[0-9]|20)(?:\s+(?:done|closed))?[.!]?$',claim,re.I)
    if restatement:
        count={'two':2,'three':3}.get(restatement[1].lower()) or int(restatement[1])
        if count!=len(items):
            return False
        claim=claim[:restatement.start()].strip()
    withheld=None if exact_clause else re.search(r'(?:[;,]\s*|\bbut\s+|(?<=\bdone)\s+)(?:leave\s+)?(.+?)\s+(?:still\s+)?open[.!]?$',claim,re.I)
    if withheld:
        # A carve-out names a specific remaining item; a hedge, reversal or blanket pronoun withdraws the claim.
        if _WITHHELD_REVERSAL.search(withheld[1]):
            return False
        claim=claim[:withheld.start()].strip()
    claim=re.sub(r'[.!]$', '', claim).strip()
    if not claim or '?' in claim:
        return False
    clauses=re.split(r'[.;]\s+',claim)
    if len(clauses)>1:
        groups=[[item for item in items if item['correction'] in clause] for clause in clauses]
        return (sum(map(len,groups))==len(items) and len({id(i) for group in groups for i in group})==len(items)
            and all(group and resolve_many_names_match(clause,group,mode,exact_clause=exact_clause) for clause,group in zip(clauses,groups,strict=True)))

    spans=[]
    for item in items:
        name=item['name'].casefold()
        found=list(re.finditer(r'(?<![^\W_])'+re.escape(item['name'])+r'(?![^\W_])',claim,re.I))
        if len(found)!=1 or item['correction']!=found[0][0] or (withheld is not None and name in withheld[1].casefold()):
            return False
        index=found[0].start()
        spans.append((index,found[0].end()))
    if any(a<c_end and c<a_end for i,(a,a_end) in enumerate(spans) for j,(c,c_end) in enumerate(spans) if i!=j):
        return False
    remainder=claim
    for start,end in sorted(spans,reverse=True):
        remainder=remainder[:start]+' '+remainder[end:]
    if re.search(r'\b(?:not|never|neither|don\'t|do not|isn\'t|aren\'t|might|maybe|perhaps|if|unless|should|could|would|open|pending|waiting|unfinished)\b',remainder,re.I):
        return False
    if re.search(r'\bboth\b',remainder,re.I) and len(items)!=2:
        return False
    connector=r'(?:\s|,|&|\band\b)*'
    return (re.fullmatch(r'(?:I|we|I\'ve|we\'ve|I have|we have)?\s*(?:finished|completed|closed|resolved|sorted)(?:\s+with)?\s*'+connector,remainder.strip(),re.I) is not None
        or re.fullmatch(connector+r'(?:loops?\s+)?(?:(?:are|were|is|have been)\s+)?(?:both\s+)?(?:closed|done|handled|finished|complete|completed|resolved|sorted)(?:\s+(?:too|now))?',remainder.strip(),re.I) is not None
        or re.fullmatch(r'(?:please\s+)?(?:close|resolve|complete)\s*'+connector,remainder.strip(),re.I) is not None
        or re.fullmatch(r'(?:please\s+)?mark\s+'+connector+r'(?:as\s+)?(?:done|complete|completed|closed|resolved)',remainder.strip(),re.I) is not None)


_identity = partial(action_identity, kind="owner_loop_tool")


def perform(body, *, bearer, root, policy, data_root, capability, activation,
            telegram_verifier=None, chat_client=None):
    proof, identity = validate_tool_envelope(body, operations=('apply', 'inspect', 'preview'),
        kind='owner_loop_tool', domain='sotto.owner-loop-tool.v1', bearer=bearer, root=root,
        capability=capability, policy=policy, data_root=data_root)
    admitted = body['admitted_at_ms']
    args = body['args']
    if type(args) is not dict or args.get('operation') not in ('add', 'close', 'correct', 'retarget', 'fact_correct', 'snooze', 'keep', 'add_identifier', 'propose_relation', 'relation', 'propose_identifier', 'identifier', 'propose_cleanup', 'cleanup', 'resolve_many', 'propose_fact', 'fact_add'):
        raise ValueError('owner task arguments are invalid')
    cleanup = args['operation'] in ('propose_cleanup','cleanup','resolve_many')
    relation = args['operation'] in ('propose_relation','relation')
    reviewed_fact = args['operation'] in ('propose_fact','fact_add')
    reviewed_identifier = args['operation'] in ('propose_identifier','identifier')
    if body['operation']=='preview' and args['operation'] not in ('propose_relation','propose_identifier','propose_fact','propose_cleanup'):
        raise ValueError('owner task preview invalid')
    source = args['operation'] in ('correct', 'retarget', 'snooze', 'keep')
    fact = args['operation'] == 'fact_correct'
    identifier = args['operation'] == 'add_identifier'
    if cleanup:
        if args['operation']=='resolve_many':
            if body['operation']=='preview':
                raise ValueError('multi resolve preview invalid')
            loops._load_writer().validate_resolve_many_args(args)
        else:
            loops._load_writer().validate_cleanup_args(args,preview=body['operation']=='preview')
        key = None
    elif reviewed_fact:
        _validate_fact_review_args(args,preview=body['operation']=='preview')
        key = 'text' if body['operation']=='preview' else None
    elif reviewed_identifier:
        _validate_identifier_review_args(args,preview=body['operation']=='preview')
        key = 'identifier' if body['operation']=='preview' else None
    elif relation:
        _validate_relation_args(args, preview=body['operation']=='preview')
        key = None
    elif identifier:
        if set(args) != {'operation','canonical_id','identifier','source_revision'}:
            raise ValueError('owner identifier arguments invalid')
        if type(args['canonical_id']) is not str or not re.fullmatch(r'c_[0-9a-f]{6,64}', args['canonical_id']):
            raise ValueError('owner identifier target invalid')
        if type(args['source_revision']) is not str or not _HEX64.fullmatch(args['source_revision']):
            raise ValueError('owner identifier binding invalid')
        _text(args['identifier'], bound=400, whitespace=False)
        if len(args['identifier']) > 200 or args['identifier'].strip() != args['identifier'] or any(c in args['identifier'] for c in '\u0085\u2028\u2029'):
            raise ValueError('owner identifier span invalid')
        key = 'identifier'
    elif fact:
        from runtime_owner_fact_correction import validate_args
        validate_args(args)
        key = 'text'
    elif source:
        common = {'operation','anchor','loop_version','loop_identity','source_revision','correction'}
        required = common | ({'to'} if args['operation']=='correct' else {'days'} if args['operation']=='snooze' else set() if args['operation']=='keep' else {'direction'})
        optional = set() if args['operation'] in ('correct','snooze','keep') else {'text','contact','identifier'}
        if not required <= set(args) or not set(args) <= required | optional:
            raise ValueError('owner source correction arguments invalid')
        for name in ('loop_version','loop_identity','source_revision'):
            if type(args[name]) is not str or _HEX64.fullmatch(args[name]) is None:
                raise ValueError('owner source correction binding invalid')
        for name,bound in (('anchor',256),('correction',16000),('text',500),('contact',120),('identifier',200)):
            if name in args:
                if name in ('contact','identifier') and args[name] == '':
                    continue
                _text(args[name],bound=bound if name=='correction' else 2*bound,
                      whitespace=name=='correction')
                if len(args[name])>bound or args[name].strip()!=args[name]:
                    raise ValueError('owner source correction span invalid')
        if ((args['operation']=='correct' and args['to'] not in ('resolved','dismissed'))
                or (args['operation']=='retarget' and args['direction'] not in ('you','them'))):
            raise ValueError('owner source correction transition invalid')
        if args['operation']=='snooze' and (type(args['days']) is not int or not 1 <= args['days'] <= 3652058):
            raise ValueError('owner snooze days invalid')
        key = 'correction'
    else:
        key = 'text' if args['operation'] == 'add' else 'anchor'
        if (args['operation']=='add' and (not {'operation','text'} <= set(args) or set(args)-{'operation','text','direction','contact','identifier'}
                or ('contact' in args or 'identifier' in args) and 'direction' not in args)) or (args['operation']!='add' and (not {'operation',key}<=set(args) or set(args)-{'operation',key,'correction'})):
            raise ValueError('owner task arguments are invalid')
        _text(args[key], bound=1000 if key == 'text' else 512, whitespace=False)
        if len(args[key]) > (500 if key == 'text' else 256) or args[key].strip() != args[key]:
            raise ValueError('owner task argument is invalid')
    if args['operation']=='close' and 'correction' in args:
        _text(args['correction'],bound=500,whitespace=False)
        if args['correction'].strip()!=args['correction'] or not resolve_many_names_match(body['text'],[{'name':args['correction'],'correction':args['correction']}],'owner_tasks'):
            return {'version':1,'status':'rejected','code':'owner_span_unavailable'}
    if args['operation']=='add':
        if 'direction' in args and args['direction'] not in ('you','them'):
            raise ValueError('owner task direction invalid')
        for field,bound in (('contact',120),('identifier',200)):
            if field in args:
                _text(args[field],bound=bound*2,whitespace=False)
                if len(args[field])>bound or args[field].strip()!=args[field] or any(127<=ord(c)<=159 or c in '\u2028\u2029' for c in args[field]):
                    raise ValueError('owner task contact invalid')
    authority.validate_route(body['provider_thread_id'], identity, policy, data_root, activation,
                             telegram_verifier=telegram_verifier, chat_client=chat_client)
    if args['operation']=='add' and any(args[field] not in body['text'] for field in ('contact','identifier') if field in args):
        return {'version':1,'status':'rejected','code':'owner_span_unavailable'}
    if fact and any(args[field] not in body['text'] for field in ('title','company') if field in args):
        return {'version':1,'status':'rejected','code':'owner_span_unavailable'}
    if key in ('text','correction','identifier') and args[key] not in body['text']:
        return {'version': 1, 'status': 'rejected', 'code': 'owner_span_unavailable'}
    if cleanup and args['operation']=='cleanup':
        if 'selection' in args:
            try:
                derived = loops._load_writer().cleanup_selection_items(args['list'], args['selection'], owner_text=body['text'],
                    completion_matches=lambda text, items: resolve_many_names_match(text, items, 'owner_tasks', exact_clause=True))
                matched = derived == args['items']
            except ValueError:
                matched = False
        else:
            matched = cleanup_selections_match(body['text'], args['items'], allow_stored_mixed_resolve=body['operation']=='inspect')
        if not matched:
            return {'version':1,'status':'rejected','code':'owner_span_unavailable'}
    if args['operation']=='resolve_many' and not resolve_many_names_match(body['text'],args['items'],args['selection_mode']):
        return {'version':1,'status':'rejected','code':'owner_span_unavailable'}
    zone = policy['_configured_tz_name']()
    tz = policy['_tz_resolve'](zone)
    if not zone or tz is None or len(zone) > 64:
        raise UnavailableError('owner task timezone unavailable')
    when = datetime.fromtimestamp(admitted / 1000, timezone.utc).astimezone(tz)
    writer = loops._load_writer()
    if cleanup:
        try:
            if body['operation']=='preview':
                review=writer.cleanup_list(args,now=when,authority=policy['_source_authority'])
                return {'version':1,'status':'preview','event_id':proof['event_id'],'payload_hash':proof['payload_hash'],'review':review}
            if args['operation']=='cleanup':
                proposal=args['proposal']
                original={'operation':'propose_cleanup','source_revision':args['list']['source_revision']}
                if args['list'].get('scope')=='open':
                    original['scope']='open'
                if 'anchor_keys' in args['list']:
                    original['anchor_keys']=args['list']['anchor_keys']
                expected_action,expected_hash=_identity(proposal['event_id'],original)
                if proposal['action_id']!=expected_action or proposal['args_hash']!=expected_hash:
                    raise ValueError('cleanup proposal invalid')
            receipt=writer.op_admitted_loop_cleanup(args,proof,now=when,inspect=body['operation']=='inspect',authority=policy['_source_authority'])
            if body['operation']=='apply' and args['operation']=='cleanup' and type(receipt) is dict and receipt.get('status')=='unknown':
                return {**receipt,'cleanup_stage':'writer','cleanup_category':'unavailable'}
            return receipt if receipt is not None else {'version':1,'status':'not_found'}
        except writer.OwnerLoopToolRefusalError as error:
            return {'version':1,'status':'rejected','code':error.code}
        except (writer.EditError, OSError) as error:
            return {'version':1,'status':'unknown', **({'cleanup_stage':'writer', 'cleanup_category':'io' if isinstance(error, OSError) else 'edit'}
                    if body['operation']=='apply' and args['operation']=='cleanup' else {})}
    if relation or reviewed_identifier or reviewed_fact:
        try:
            review_operation = 'propose_fact' if reviewed_fact else 'propose_identifier' if reviewed_identifier else 'propose_relation'
            invoke = writer.op_admitted_fact_add if reviewed_fact else writer.op_admitted_identifier_reviewed if reviewed_identifier else writer.op_admitted_relation
            if body['operation']=='preview':
                review = (writer.fact_add_review if reviewed_fact else writer.identifier_review if reviewed_identifier else writer.relation_review)(args,authority=policy['_source_authority'])
                return {'version':1,'status':'preview','event_id':proof['event_id'],'payload_hash':proof['payload_hash'],'review':review}
            review,proposal = args['review'],args['proposal']
            original_args = {'operation':review_operation,**{k:review[k] for k in (('canonical_id','text','source_revision') if reviewed_fact else ('canonical_id','identifier','source_revision') if reviewed_identifier else ('mode','canonical_id','other_canonical_id','relation_type','date','source_revision'))}}
            expected_action,expected_hash = _identity(proposal['event_id'],original_args)
            if (proposal['action_id']!=expected_action or proposal['args_hash']!=expected_hash
                    or proposal['source_revision']!=review['source_revision']):
                raise ValueError('relationship proposal invalid')
            receipt = invoke(review,proposal,proof,now=when,inspect=True,authority=policy['_source_authority'])
            if receipt is None and body['operation']!='inspect':
                from runtime_owner_meeting_prep_request import YES
                if proposal['event_id']==proof['event_id'] or not YES.fullmatch(body['text'].strip()):
                    return {'version':1,'status':'rejected','code':'target_unavailable'}
                receipt = invoke(review,proposal,proof,now=when,authority=policy['_source_authority'])
            return receipt if receipt is not None else {'version':1,'status':'not_found'}
        except writer.OwnerLoopToolRefusalError as error:
            return {'version':1,'status':'rejected','code':error.code}
    if fact or identifier:
        try:
            invoke = writer.op_admitted_identifier_add if identifier else writer.op_admitted_fact_correct
            receipt = invoke(args, proof, now=when,
                inspect=body['operation']=='inspect', authority=policy['_source_authority'])
        except writer.OwnerLoopToolRefusalError as error:
            return {'version':1,'status':'rejected','code':error.code}
        return receipt if receipt is not None else {'version':1,'status':'not_found'}
    if source:
        try:
            invoke = writer.op_admitted_loop_keep if args['operation']=='keep' else writer.op_admitted_loop_snooze if args['operation']=='snooze' else writer.op_source_loop_tool
            receipt = invoke(args, proof, now=when,
                inspect=body['operation']=='inspect', authority=policy['_source_authority'])
        except writer.OwnerLoopToolRefusalError as error:
            return {'version':1,'status':'rejected','code':error.code}
        return receipt if receipt is not None else {'version':1,'status':'not_found'}
    try:
        row = writer.op_loop_tool(args, proof, now=when, inspect=body['operation'] == 'inspect')
    except writer.OwnerLoopToolRefusalError as error:
        return {'version': 1, 'status': 'rejected', 'code': error.code}
    if row is None:
        return {'version': 1, 'status': 'not_found'}
    direction = {}
    if args['operation']=='add' and 'direction' in args:
        actual = 'them' if row.get('action_type')=='waiting_on' else 'you' if row.get('action_type')=='follow_up' else None
        if actual!=args['direction']:
            raise UnavailableError('owner task direction unavailable')
        direction={'direction':actual}
    return {**direction,'version': 1, 'status': 'committed', 'action_id': proof['action_id'], 'event_id': body['event_id'],
            'payload_hash': body['payload_hash'], 'args_hash': proof['args_hash'], 'operation': args['operation'],
            'anchor_key': row['anchor_key'], 'post_digest': row['content_provenance']['digest'],
            'loop_status': row['status']}


def _validate_relation_args(args, *, preview):
    from datetime import date
    common = {'mode','canonical_id','other_canonical_id','relation_type','date','source_revision'}
    if preview:
        if set(args)!={'operation'}|common or args['operation']!='propose_relation':
            raise ValueError('relationship preview invalid')
        value=args
    else:
        if set(args)!={'operation','review','proposal'} or args['operation']!='relation':
            raise ValueError('relationship confirmation invalid')
        value=args['review'];proposal=args['proposal']
        if (type(value) is not dict or set(value)!={'operation','name','other_name','present','relation_version','review_hash'}|common
                or value['operation']!='relation' or type(value['present']) is not bool):
            raise ValueError('relationship review invalid')
        for name in ('name','other_name'):
            _text(value[name],bound=1000,whitespace=False)
            if len(value[name])>500 or value[name].strip()!=value[name] or any(127<=ord(c)<=159 or c in '\u2028\u2029' for c in value[name]):
                raise ValueError('relationship name invalid')
        for name in ('relation_version','review_hash'):
            if type(value[name]) is not str or not _HEX64.fullmatch(value[name]):
                raise ValueError('relationship digest invalid')
        digest=hashlib.sha256(json.dumps({k:v for k,v in value.items() if k!='review_hash'},sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        if digest!=value['review_hash']:
            raise ValueError('relationship review invalid')
        if (type(proposal) is not dict or set(proposal)!={'event_id','action_id','args_hash','accepted_dispatch_id','source_revision'}
                or any(type(proposal[k]) is not str or not _HEX64.fullmatch(proposal[k]) for k in ('event_id','action_id','args_hash','source_revision'))):
            raise ValueError('relationship approval invalid')
        _text(proposal['accepted_dispatch_id'],bound=320,whitespace=False)
        if len(proposal['accepted_dispatch_id'])>160:
            raise ValueError('relationship dispatch invalid')
    if value['mode'] not in ('add','remove') or value['relation_type'] not in ('introduced_by','introduced','works_with','family_of','partner_of','met_through','connected'):
        raise ValueError('relationship operation invalid')
    for name in ('canonical_id','other_canonical_id'):
        if type(value[name]) is not str or not re.fullmatch(r'c_[0-9a-f]{6,64}',value[name]):
            raise ValueError('relationship target invalid')
    if type(value['source_revision']) is not str or not _HEX64.fullmatch(value['source_revision']):
        raise ValueError('relationship source invalid')
    if type(value['date']) is not str or value['mode']=='remove' and value['date']:
        raise ValueError('relationship date invalid')
    if value['date']:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value['date']) or date.fromisoformat(value['date']).isoformat()!=value['date']:
            raise ValueError('relationship date invalid')


def _validate_identifier_review_args(args, *, preview):
    if preview:
        if set(args)!={'operation','canonical_id','identifier','source_revision'} or args['operation']!='propose_identifier':
            raise ValueError('identifier preview invalid')
        value=args
    else:
        if set(args)!={'operation','review','proposal'} or args['operation']!='identifier':
            raise ValueError('identifier confirmation invalid')
        value,proposal=args['review'],args['proposal']
        if (type(value) is not dict or set(value)!={'operation','canonical_id','name','identifier','normalized_identifier','present','source_revision','review_hash'}
                or value['operation']!='identifier' or type(value['present']) is not bool):
            raise ValueError('identifier review invalid')
        _text(value['name'],bound=1000,whitespace=False)
        if len(value['name'])>500 or value['name'].strip()!=value['name'] or any(127<=ord(c)<=159 or c in '\u2028\u2029' for c in value['name']):
            raise ValueError('identifier name invalid')
        _text(value['normalized_identifier'],bound=400,whitespace=False)
        if len(value['normalized_identifier'])>200:
            raise ValueError('identifier normalized value invalid')
        digest=hashlib.sha256(json.dumps({k:v for k,v in value.items() if k!='review_hash'},sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        if value['review_hash']!=digest:
            raise ValueError('identifier review digest invalid')
        if (type(proposal) is not dict or set(proposal)!={'event_id','action_id','args_hash','accepted_dispatch_id','source_revision'}
                or any(type(proposal[k]) is not str or not _HEX64.fullmatch(proposal[k]) for k in ('event_id','action_id','args_hash','source_revision'))):
            raise ValueError('identifier proposal invalid')
        _text(proposal['accepted_dispatch_id'],bound=320,whitespace=False)
        if len(proposal['accepted_dispatch_id'])>160:
            raise ValueError('identifier dispatch invalid')
    if type(value['canonical_id']) is not str or not re.fullmatch(r'c_[0-9a-f]{6,64}',value['canonical_id']):
        raise ValueError('identifier target invalid')
    if type(value['source_revision']) is not str or not _HEX64.fullmatch(value['source_revision']):
        raise ValueError('identifier source invalid')
    _text(value['identifier'],bound=400,whitespace=False)
    if len(value['identifier'])>200 or value['identifier'].strip()!=value['identifier'] or any(c in value['identifier'] for c in '\u0085\u2028\u2029'):
        raise ValueError('identifier span invalid')


def _validate_fact_review_args(args, *, preview):
    fields={'canonical_id','text','source_revision'}
    if preview:
        if set(args)!={'operation'}|fields or args['operation']!='propose_fact':
            raise ValueError('fact preview invalid')
        value=args
    else:
        if set(args)!={'operation','review','proposal'} or args['operation']!='fact_add':
            raise ValueError('fact confirmation invalid')
        value,proposal=args['review'],args['proposal']
        if (type(value) is not dict or set(value)!={'operation','name','identity_hint','person_version','present','review_hash'}|fields
                or value['operation']!='fact_add' or type(value['present']) is not bool):
            raise ValueError('fact review invalid')
        _text(value['name'],bound=1000,whitespace=False)
        if len(value['name'])>500 or value['name'].strip()!=value['name'] or any(127<=ord(c)<=159 or c in '\u2028\u2029' for c in value['name']):
            raise ValueError('fact person invalid')
        _text(value['identity_hint'],bound=400,whitespace=False)
        if len(value['identity_hint'])>200 or value['identity_hint'].strip()!=value['identity_hint'] or any(127<=ord(c)<=159 or c in '\u2028\u2029' for c in value['identity_hint']):
            raise ValueError('fact identity hint invalid')
        if type(value['person_version']) is not str or not _HEX64.fullmatch(value['person_version']):
            raise ValueError('fact identity invalid')
        digest=hashlib.sha256(json.dumps({k:v for k,v in value.items() if k!='review_hash'},sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        if value['review_hash']!=digest:
            raise ValueError('fact review digest invalid')
        if (type(proposal) is not dict or set(proposal)!={'event_id','action_id','args_hash','accepted_dispatch_id','source_revision'}
                or any(type(proposal[k]) is not str or not _HEX64.fullmatch(proposal[k]) for k in ('event_id','action_id','args_hash','source_revision'))):
            raise ValueError('fact proposal invalid')
        _text(proposal['accepted_dispatch_id'],bound=320,whitespace=False)
        if len(proposal['accepted_dispatch_id'])>160:
            raise ValueError('fact dispatch invalid')
    if type(value['canonical_id']) is not str or not re.fullmatch(r'c_[0-9a-f]{6,64}',value['canonical_id']):
        raise ValueError('fact target invalid')
    if type(value['source_revision']) is not str or not _HEX64.fullmatch(value['source_revision']):
        raise ValueError('fact source invalid')
    _text(value['text'],bound=1000)
    if len(value['text'])>500 or value['text'].strip()!=value['text']:
        raise ValueError('fact span invalid')


def http_failure(body, error, stage):
    if (body.get('operation') == 'apply' and type(body.get('args')) is dict
            and body['args'].get('operation') == 'cleanup'):
        category = ('io' if isinstance(error, OSError) else 'type' if isinstance(error, (TypeError, KeyError))
                    else 'import' if isinstance(error, ImportError)
                    else 'unavailable' if isinstance(error, UnavailableError) else 'other')
        return {'cleanup_stage': stage, 'cleanup_category': category, 'cleanup_http_status': 503}
    return {'error': 'owner reminder unavailable'}
