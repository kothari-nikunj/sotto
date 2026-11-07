"""Optional typed identity on the existing admitted fact correction."""
import re

from owner_tool_admission import _text

PROFILE_VALUE_MAX = 200
FACT_TEXT_MAX = 500


def validate_args(args):
    required = {'operation','canonical_id','fact_id','fact_version','text','source_revision'}
    if not required <= set(args) or set(args) - required - {'title','company'}:
        raise ValueError('owner fact arguments invalid')
    if (type(args['canonical_id']) is not str or not re.fullmatch(r'c_[0-9a-f]{6,64}', args['canonical_id'])
            or type(args['fact_id']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args['fact_id'])):
        raise ValueError('owner fact target invalid')
    for name in ('fact_version','source_revision'):
        if type(args[name]) is not str or not re.fullmatch(r'[a-f0-9]{64}', args[name]):
            raise ValueError('owner fact binding invalid')
    for field in ('title','company'):
        if field in args:
            _text(args[field], bound=400, whitespace=False)
            if (len(args[field]) > PROFILE_VALUE_MAX or args[field].strip() != args[field]
                    or re.search(r'[\x7f-\x9f\u2028\u2029]', args[field])):
                raise ValueError('owner identity span invalid')
    _text(args['text'], bound=1000)
    if len(args['text']) > FACT_TEXT_MAX or args['text'].strip() != args['text']:
        raise ValueError('owner fact text invalid')
