"""Bounded numeric answers inherit intent only from the open-task done question."""
import re

MAX_PROVIDER_ID = 512
MAX_SELECTIONS = 20
INVALID_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")


def short_numbered_completion(review, selection, clause, indices, residual):
    complete = complete_open_selection(review)
    return (review.get('scope') == 'open' and len(selection['clauses']) == 1 and clause['correction'] == selection['correction']
            and (re.fullmatch(r'(?:\s|,|&|\band\b)*[.!]?', residual, re.I) is not None
                 or complete and len(indices) == len(review['rows'])
                 and re.fullmatch(r'all\s*\?(?:\s|,|&|\band\b)*[.!?]?', residual.strip(), re.I) is not None))


def complete_open_selection(review):
    return (review.get('scope') == 'open'
            and (review['anchor_keys'] == [row['anchor_key'] for row in review['rows']]
                 if 'anchor_keys' in review else review['remaining'] == 0))


def explicit_displayed_reference(reference):
    return type(reference) is str and re.fullmatch(r'all of (?:them|this|that|these|those)', reference, re.I) is not None


def all_open_selection(review, reference):
    return (complete_open_selection(review)
            or review.get('scope') == 'open' and 'anchor_keys' not in review
            and bool(review['rows']) and explicit_displayed_reference(reference))


def validate_resolve_many_args(args):
    delivered = isinstance(args, dict) and args.get('selection_mode') == 'delivered'
    if delivered:
        delivery = args.get('delivery')
        # quoted=True marks an exact provider quote; the receiver then applies the threaded window.
        if (type(delivery) is not dict or set(delivery) - {'quoted'} != {'dispatch_id', 'message_id', 'route_digest', 'payload_hash'}
                or delivery.get('quoted', True) is not True
                or not re.fullmatch(r'[a-f0-9]{16,64}', str(delivery['dispatch_id']))
                or not isinstance(delivery['message_id'], str) or not 0 < len(delivery['message_id']) <= MAX_PROVIDER_ID
                or any(not re.fullmatch(r'[a-f0-9]{64}', str(delivery[k])) for k in ('route_digest', 'payload_hash'))
                or args.get('allow_partial') is not True):
            raise ValueError('invalid delivered task selection')
    if (type(args) is not dict or set(args)-({'delivery','allow_partial'} if delivered else {'allow_partial'})!={'operation','source_revision','selection_mode','items'} or ('allow_partial' in args and args['allow_partial'] is not True)
            or args['operation']!='resolve_many' or type(args['source_revision']) is not str
            or args['selection_mode'] not in ('named_people','owner_tasks','delivered')
            or re.fullmatch(r'[a-f0-9]{64}',args['source_revision']) is None
            or type(args['items']) is not list or not 1<=len(args['items'])<=MAX_SELECTIONS):
        raise ValueError('multi resolve arguments invalid')
    names,anchors=set(),set()
    for item in args['items']:
        if type(item) is not dict or set(item) not in ({'name','correction'},{'name','anchor_key','loop_version','loop_identity','correction'}):
            raise ValueError('multi resolve row invalid')
        if delivered and 'anchor_key' in item:
            raise ValueError('only the accepted message supplies task bindings')
        for key,bound in (('name',500),('anchor_key',256),('correction',16000)):
            if key not in item:
                continue
            value=item[key]
            if type(value) is not str or not value or value!=value.strip() or len(value)>bound or INVALID_TEXT.search(value):
                raise ValueError('multi resolve text invalid')
        if item['correction']!=item['name'] or item['name'].casefold() in names or item.get('anchor_key') in anchors:
            raise ValueError('multi resolve selection invalid')
        names.add(item['name'].casefold())
        if 'anchor_key' not in item:
            continue
        anchors.add(item['anchor_key'])
        if any(type(item[k]) is not str or re.fullmatch(r'[a-f0-9]{64}',item[k]) is None for k in ('loop_version','loop_identity')):
            raise ValueError('multi resolve proof invalid')
