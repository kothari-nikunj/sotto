"""Fact correction, coverage and typed identity state for canonical graph writers."""
import hashlib
import json
import re

OWNER_SOURCE = "user_edit"
PROFILE_VALUE_MAX = 200
FACT_OUTCOMES_MAX = 256


def valid_identity(identity):
    return all(type(value) is str and value and len(value) <= PROFILE_VALUE_MAX and value.strip() == value
        and not re.search(r'[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]', value)
        for value in identity.values())


def correct_fact_state(before, after, args, today, source):
    import knowledge as kg
    fact_id, text = args['fact_id'], args['text']
    target = before.facts[fact_id]
    identity = {field:args[field] for field in ('title','company') if field in args}
    changed = False
    tgt = after.facts.get(fact_id)
    if tgt is not None and (tgt.status != "archived" or tgt.archived_reason != "user_corrected"):
        tgt.status = "archived"
        tgt.archived_text = tgt.text
        tgt.archived_reason = "user_corrected"
        changed = True
    if not any(f.status != "archived" and f.text == text for f in after.facts.values()):
        fid = kg.generate_fact_id(after.canonical_id, text, today)
        after.facts[fid] = kg.FactMeta(text=text, type=target.type, status="active", seen=1,
                                    conf=1.0, source=source, source_ref="",
                                    first=today, last=today)
        changed = True
    # Omitted fields withdraw only identity supported by the corrected fact.
    for field in ('title', 'company'):
        support = before.identity_support.get(field, {})
        legacy_research = (not support and target.source == 'web_research' and target.type == 'context'
                           and target.text.startswith('Per web search: '))
        if field in identity:
            replacement_id = min(fid for fid, fact in after.facts.items()
                if fact.status != 'archived' and fact.text == text)
            setattr(after, field, identity[field])
            after.identity_support[field] = {'source': source, 'fact_id': replacement_id}
            changed = True
        elif support.get('fact_id') == fact_id or legacy_research:
            setattr(after, field, None)
            after.identity_support[field] = {'source': source, 'fact_id': fact_id}
            changed = True
    return changed


def validate_post(post, args, target, *, fact_id_re, error):
    source = OWNER_SOURCE
    identity = {field:args[field] for field in ('title','company') if field in args}
    if identity and post.get('identity') != {field:{'value':value, 'support':{'source':source,
            'fact_id':post.get('replacement_fact_id')}} for field,value in identity.items()}:
        raise error('fact receipt identity unavailable')
    if (set(post) != {'canonical_id','fact_id','old_fact','replacement_fact_id','replacement_fact'} | ({'identity'} if identity else set())
            or post['canonical_id'] != args['canonical_id'] or post['fact_id'] != args['fact_id']
            or not fact_id_re.fullmatch(post['replacement_fact_id'])
            or post['old_fact'].get('status') != 'archived'
            or post['old_fact'].get('archived_reason') != 'user_corrected'
            or post['old_fact'].get('archived_text') != target.get('text')
            or post['replacement_fact'].get('status') == 'archived'
            or post['replacement_fact'].get('text') != args['text']):
        raise error('fact receipt unavailable')


def merge_identity(dst, src):
    for name in ('company', 'title'):
        dst_support = dst.identity_support.get(name, {})
        src_support = src.identity_support.get(name, {})
        if dst_support.get('source') == 'user_edit':
            continue
        if src_support.get('source') == 'user_edit' or not getattr(dst, name):
            setattr(dst, name, getattr(src, name))
            if src_support:
                dst.identity_support[name] = dict(src_support)


def apply_profile(person, patch, source, fact_id=None, *, company_slug=None):
    for field in ('title', 'company'):
        if not patch.get(field):
            continue
        if person.identity_support.get(field, {}).get('source') == 'user_edit' and (fact_id is not None or source != 'user_edit'):
            continue
        if fact_id is None:
            old, new = getattr(person, field), patch[field]
            if field == 'company':
                old, new = (company_slug(value or '').replace('-', '') for value in (old, new))
            if old == new and source != 'user_edit':
                continue
        setattr(person, field, patch[field])
        person.identity_support[field] = {'source':source, 'fact_id':fact_id or ''}


def identity_post(person, args, fact_id, source, error):
    identity = {field:{'value':getattr(person, field), 'support':person.identity_support.get(field)}
        for field in ('title','company') if field in args}
    if identity != {field:{'value':args[field], 'support':{'source':source,'fact_id':fact_id}}
            for field in identity}:
        raise error('fact correction identity unconfirmed')
    return {'identity':identity} if identity else {}


def identity_frontmatter(p):
    d = {}
    if p.company is not None:
        d["company"] = p.company
    if p.title is not None:
        d["title"] = p.title
    d["identifiers"] = p.identifiers
    if p.x_user_id is not None:
        d["x_user_id"] = p.x_user_id
    if p.x_handles:
        d["x_handles"] = p.x_handles
    if p.x_resolution:
        d["x_resolution"] = p.x_resolution
    if p.linkedin is not None:
        d["linkedin"] = p.linkedin
    if p.identity_support:
        d["identity_support"] = p.identity_support
    return d


ACCEPTED_FACT_DISPOSITIONS = frozenset({'added', 'confirmed', 'updated', 'superseded', 'duplicate'})
RESEARCH_COVERED_REJECTIONS = frozenset({'protected_owner_correction', 'similar_fact_exists',
                                          'older_observation'})


def fact_establishes_research_coverage(outcome: dict) -> bool:
    return (outcome['disposition'] in ACCEPTED_FACT_DISPOSITIONS
            or (outcome['disposition'] == 'rejected'
                and outcome.get('reason') in RESEARCH_COVERED_REJECTIONS)
            or (outcome['disposition'] == 'ignored' and outcome.get('reason') == 'archived_match'
                and bool(outcome.get('fact_id')) and outcome.get('tombstone_reason')
                    in {'user_corrected', 'user_archived'}))



def record_fact_outcome(fact_outcomes, person_index, fact_index, fact, outcome):
    fact_outcomes['total'] += 1
    disposition = outcome['disposition']
    fact_outcomes['counts'][disposition] = fact_outcomes['counts'].get(disposition, 0) + 1
    if len(fact_outcomes['items']) < FACT_OUTCOMES_MAX:
        digest = hashlib.sha256(json.dumps(fact, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        fact_outcomes['items'].append({'person_index': person_index, 'fact_index': fact_index,
                                       'digest': digest, **outcome})
    else:
        fact_outcomes['truncated'] = True
