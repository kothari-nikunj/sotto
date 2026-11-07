"""One admitted public URL through the original provider ladder; immutable contact proof."""
import hashlib
import importlib.util
from pathlib import Path
import time
import sys
from functools import lru_cache

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jsonstore  # noqa: E402


@lru_cache(maxsize=1)
def _web():
    path = Path(__file__).resolve().parents[1] / 'scripts/web_research.py'
    spec = importlib.util.spec_from_file_location('sotto_attended_web_research', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _receipt_valid(receipt, proof):
    if (type(receipt) is not dict or set(receipt) != {'version', 'status', 'action_id', 'event_id', 'payload_hash', 'args_hash', 'outcome'}
            or receipt.get('version') != 1 or receipt.get('status') != 'committed'
            or any(receipt.get(key) != proof[key] for key in ('action_id', 'event_id', 'payload_hash', 'args_hash'))):
        return False
    outcome = receipt['outcome']
    if type(outcome) is not dict or set(outcome) != {'operation', 'result'} or outcome['operation'] != 'read':
        return False
    result = outcome['result']
    return (type(result) is dict and set(result) in ({'url', 'title', 'text', 'provider'}, {'url', 'title', 'text', 'provider', 'error'})
            and result.get('url') == proof['args']['url'] and type(result.get('title')) is str and len(result['title']) <= 200
            and type(result.get('text')) is str and len(result['text']) <= 8000
            and result.get('provider') in ('exa', 'gemini', 'browseruse', None)
            and (('error' not in result and bool(result['text'])) or not result['text']
                 and result.get('error') in ('could not read the page', 'no read provider available')))


def perform(data_root, proof, *, operation, authority, cancelled=None, web=None, owner_text=None):
    if proof['args'].get('operation') in ('docsend', 'propose_docsend', 'confirm_docsend'):
        return _perform_docsend(data_root, proof, operation=operation, authority=authority, cancelled=cancelled, owner_text=owner_text)
    root = Path(data_root) / 'events/research-artifacts'
    path = str(root / ('web-' + proof['event_id'] + '.json'))
    revision = proof['args']['source_revision']
    def captured(value):
        return value.get('revision') if value.get('complete') is True else None
    def reject(code):
        return {'version': 1, 'status': 'rejected', 'code': code}
    with jsonstore.lock(str(root / ('lock-web-' + hashlib.sha256(proof['event_id'].encode()).hexdigest()[:2]))):
        saved = jsonstore.read(path, default=None, strict=True)
        if saved is not None and saved.get('proof') != proof:
            return reject('event_conflict')
        if saved is None and operation == 'inspect':
            return {'version': 1, 'status': 'not_found'}
        if saved is not None and saved.get('status') == 'contacting':
            session = saved.get('session')
            if operation == 'inspect' and session and saved.get('session_status') != 'stop_accepted':
                web = web or _web()
                try:
                    web._browseruse_stop(session, {'X-Browser-Use-API-Key': web._key('browseruse')}, strict=True)
                    saved['session_status'] = 'stop_accepted'
                    jsonstore.write_atomic(path, saved)
                except Exception:
                    pass
            return {'version': 1, 'status': 'unknown'}
        before = authority()
        if captured(before) != revision:
            return reject('source_unavailable')
        if saved is None:
            age = time.time() * 1000 - proof['admitted_at_ms']
            if not -300000 <= age <= 86400000 or cancelled is not None and cancelled():
                return reject('unavailable')
            saved = {'proof': proof, 'status': 'contacting'}
            jsonstore.write_atomic(path, saved)
            web = web or _web()
            def lifecycle(event, session):
                if session is not None:
                    if type(session) is not str or not session or len(session) > 512:
                        raise web.WebReadHeldError('browser session proof unavailable')
                    saved['session'] = session
                saved['session_status'] = event
                jsonstore.write_atomic(path, saved)
            try:
                import model_work
                with model_work.scope('web_fetch', [proof['args']['url'], proof['args_hash']], occurrence=proof['event_id']) as scope:
                    saved['model_operation'] = scope['id']
                    jsonstore.write_atomic(path, saved)
                    result = web.fetch_url(proof['args']['url'], lifecycle=lifecycle, cancelled=cancelled)
                provider = result.get('provider')
                if provider not in ('exa', 'gemini', 'browseruse', None) or result.get('url') != proof['args']['url']:
                    raise ValueError('web result unavailable')
                text, title = result.get('text'), result.get('title')
                if type(text) is not str or type(title) is not str:
                    raise ValueError('web result unavailable')
                result = {'url': result['url'], 'title': title[:200], 'text': text[:8000], 'provider': provider}
                if not result['text']:
                    result['error'] = 'could not read the page' if web.provider_chain('fetch_url') else 'no read provider available'
                receipt = {'version': 1, 'status': 'committed', **{key: proof[key] for key in
                    ('action_id', 'event_id', 'payload_hash', 'args_hash')},
                    'outcome': {'operation': 'read', 'result': result}}
                saved.update(status='completed', receipt=receipt)
                jsonstore.write_atomic(path, saved)
            except BaseException:  # contact may have succeeded; the original marker never authorizes a retry
                return {'version': 1, 'status': 'unknown'}
        if saved.get('status') != 'completed' or not _receipt_valid(saved.get('receipt'), proof):
            raise ValueError('web receipt unavailable')
        after = authority()
        if after != before or captured(after) != revision:
            return reject('source_unavailable')
        return saved['receipt']


@lru_cache(maxsize=1)
def _deck():
    path = Path(__file__).resolve().parents[1] / 'scripts/docsend_fetch.py'
    spec = importlib.util.spec_from_file_location('sotto_attended_docsend', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical(value):
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _docsend_render(review):
    lines = [f"Read this DocSend deck using {review['viewer_email']}?", review['normalized_url'],
             'The sender can see your email and the view.']
    if review['fresh']:
        lines.append('Open it again even if a saved copy exists.')
    if 'passcode' in review:
        lines.append('Use the passcode you supplied.')
    return '\n'.join(lines + ['', 'Read this deck?'])


def _docsend_email(deck):
    import re
    email = deck.configured_user_email()
    if (type(email) is not str or len(email.encode('utf-8')) > 320
            or not re.fullmatch(r'[^\s@]+@[^\s@]+', email)
            or email != email.lower() or any(ord(c) < 32 or 127 <= ord(c) <= 159
                or 0xD800 <= ord(c) <= 0xDFFF for c in email)):
        raise ValueError('configured viewer unavailable')
    return email


def _docsend_review(args, deck):
    from runtime_owner_web_read import validate_docsend_args
    validate_docsend_args(args, args.get('passcode', ''))
    review = {'operation': 'docsend', 'url': args['url'],
              'normalized_url': deck.normalized_url(args['url']),
              'viewer_email': _docsend_email(deck), 'fresh': args['fresh'],
              'source_revision': args['source_revision']}
    if 'passcode' in args:
        review['passcode'] = args['passcode']
    review['review_hash'] = hashlib.sha256(_canonical(review).encode()).hexdigest()
    return review


def _sync(path):
    import preferences
    preferences._sync_existing_preferences(path)


def _docsend_result(out, deck, url):
    if out.get('status') == 'ok':
        if (type(out.get('title')) is not str or type(out.get('text')) is not str
                or not out['text'] or type(out.get('pages')) is not int
                or not 1 <= out['pages'] <= 30 or type(out.get('cached')) is not bool
                or out.get('url') != url):
            raise ValueError('deck result invalid')
        if len(out['title'].encode()) > 4096 or len(out['text'].encode()) > 65536:
            return {'status': 'error', 'url': url, 'step': 'limit',
                    'error': 'This deck exceeds the supported reading limit.'}
        view_id = deck.VIEW_RE.fullmatch(url).group('id')
        expected = Path(deck.deck_paths(view_id)[0])
        pdf = ''
        if len(view_id) <= 80 and out.get('pdf') == str(expected) and expected.is_file():
            with expected.open('rb') as file:
                if file.read(5) == b'%PDF-':
                    pdf = '/api/decks/' + view_id + '.pdf'
        return {k: out[k] for k in ('status', 'url', 'title', 'pages', 'text', 'cached')} | {'pdf': pdf}
    step = out.get('step')
    messages = {'url': 'This is not a supported DocSend link.', 'email': 'Your configured email is unavailable.',
                'unattended': 'Ask to read this deck in chat.', 'open': 'The deck could not be opened.',
                'gate': 'This deck requires a passcode or email verification. Open it yourself or ask the sender for a PDF.',
                'pages': 'The deck pages could not be read.', 'vision': 'The deck images could not be read.',
                'limit': 'This deck exceeds the supported reading limit.'}
    if step not in messages:
        raise ValueError('deck error invalid')
    if step == 'gate':
        if out.get('error') == 'this deck requires a passcode — pass --passcode':
            messages['gate'] = 'This deck requires a passcode. Supply the passcode to read it.'
        elif type(out.get('error')) is str and out['error'].startswith('This deck requires email verification.'):
            messages['gate'] = ('This deck requires email verification. Open it in your browser to view it yourself, '
                                'or ask the sender for a PDF or a link that does not require verification.')
    return {'status': 'error', 'url': url, 'step': step, 'error': messages[step]}



def _docsend_result_valid(result, url, deck):
    if type(result) is not dict or result.get('url') != url:
        return False
    if result.get('status') == 'ok':
        view_id = deck.VIEW_RE.fullmatch(url).group('id')
        return (set(result) == {'status','url','title','pages','text','cached','pdf'}
                and type(result['title']) is str and len(result['title'].encode()) <= 4096
                and type(result['text']) is str and bool(result['text']) and len(result['text'].encode()) <= 65536
                and type(result['pages']) is int and 1 <= result['pages'] <= 30
                and type(result['cached']) is bool
                and (result['pdf'] == '' or len(view_id) <= 80 and result['pdf'] == '/api/decks/'+view_id+'.pdf'))
    return (set(result) == {'status','url','step','error'} and result.get('status') == 'error'
            and result.get('step') in ('url','email','unattended','open','gate','pages','vision','limit')
            and type(result.get('error')) is str and 0 < len(result['error'].encode()) <= 2048)

def _perform_docsend(data_root, proof, *, operation, authority, cancelled=None, owner_text=None):
    import re
    from fresh_routines import YES
    deck = _deck()
    args = proof['args']
    reject = lambda code: {'version': 1, 'status': 'rejected', 'code': code}
    root = Path(data_root) / 'events/research-artifacts'
    path = str(root / ('web-' + proof['event_id'] + '.json'))
    with jsonstore.lock(str(root / ('lock-web-' + hashlib.sha256(proof['event_id'].encode()).hexdigest()[:2]))), jsonstore.lock(str(root / 'lock-docsend-proposals')):
        saved = jsonstore.read(path, default=None, strict=True)
        if saved is not None and saved.get('proof') != proof:
            return reject('event_conflict')
        if saved is not None and saved.get('status') == 'contacting':
            return {'version': 1, 'status': 'unknown'}
        if saved is None and operation == 'inspect':
            return {'version': 1, 'status': 'not_found'}
        if args['operation'] == 'confirm_docsend':
            review, proposal = args['review'], args['proposal']
            if (type(review) is not dict or set(review) not in (
                    {'operation','url','normalized_url','viewer_email','fresh','source_revision','review_hash'},
                    {'operation','url','normalized_url','viewer_email','fresh','source_revision','review_hash','passcode'})
                    or review.get('operation') != 'docsend' or type(review.get('fresh')) is not bool
                    or type(proposal) is not dict or set(proposal) !=
                    {'event_id','action_id','args_hash','accepted_dispatch_id','source_revision'}
                    or any(type(proposal[k]) is not str or not re.fullmatch('[a-f0-9]{64}', proposal[k])
                           for k in ('event_id','action_id','args_hash','source_revision'))
                    or type(proposal['accepted_dispatch_id']) is not str
                    or not 1 <= len(proposal['accepted_dispatch_id']) <= 160):
                raise ValueError('DocSend approval invalid')
            original = {'operation':'propose_docsend', 'url':review['url'], 'fresh':review['fresh'],
                        'source_revision':review['source_revision']}
            if 'passcode' in review:
                original['passcode'] = review['passcode']
            encoded = _canonical(original)
            import json
            action = hashlib.sha256(json.dumps([proposal['event_id'],'owner_web_read',encoded],
                ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            if (proposal['args_hash'] != hashlib.sha256(encoded.encode()).hexdigest()
                    or proposal['action_id'] != action or proposal['source_revision'] != review['source_revision']
                    or review['review_hash'] != hashlib.sha256(_canonical({k:v for k,v in review.items()
                        if k != 'review_hash'}).encode()).hexdigest()
                    or review['normalized_url'] != deck.normalized_url(review['url'])):
                raise ValueError('DocSend review invalid')
            if not YES.fullmatch((owner_text or '').strip()) or proposal['event_id'] == proof['event_id']:
                return reject('approval_required')
            revision = review['source_revision']
        else:
            review = None
            revision = args['source_revision']
        before = authority()
        if before.get('complete') is not True or before.get('revision') != revision:
            return reject('source_unavailable')
        try:
            email = _docsend_email(deck)
        except ValueError:
            return reject('unavailable')
        if review is not None and _docsend_review(original, deck) != review:
            return reject('target_changed')
        if saved is not None and saved.get('status') == 'completed':
            receipt = saved.get('receipt')
            if (type(receipt) is not dict or set(receipt) != {'version','status','action_id','event_id','payload_hash','args_hash','outcome'}
                    or receipt.get('version') != 1 or receipt.get('status') != 'committed'
                    or any(receipt.get(k) != proof[k] for k in ('action_id','event_id','payload_hash','args_hash'))
                    or type(receipt.get('outcome')) is not dict or set(receipt['outcome']) != {'operation','result'}
                    or receipt['outcome']['operation'] != 'docsend'
                    or saved.get('normalized_url') != deck.normalized_url(review['url'] if review else args['url'])
                    or not _docsend_result_valid(receipt['outcome']['result'], saved.get('normalized_url'), deck)):
                raise ValueError('deck receipt invalid')
            if saved.get('viewer_email') != email:
                return reject('target_changed')
            _sync(path)
            if authority() != before:
                return reject('source_unavailable')
            return receipt
        if operation == 'preview':
            review = _docsend_review(args, deck)
            if authority() != before:
                return reject('source_unavailable')
            return {'version':1, 'status':'preview', 'event_id':proof['event_id'],
                    'payload_hash':proof['payload_hash'], 'review':review}
        if review is not None:
            for existing in root.glob('web-*.json'):
                entry = jsonstore.read(str(existing), default=None, strict=True)
                if (type(entry) is dict and type(entry.get('proposal')) is dict
                        and entry['proposal'].get('action_id') == proposal['action_id']):
                    return reject('proposal_consumed')
        target = review['url'] if review else args['url']
        normalized = deck.normalized_url(target)
        fresh = review['fresh'] if review else False
        passcode = (review or args).get('passcode', '')
        cached = deck.load_cached(deck.VIEW_RE.fullmatch(target).group('id'))
        if cached and cached.get('url') != normalized and not fresh:
            return reject('target_changed')
        age = time.time()*1000 - proof['admitted_at_ms']
        if not -300000 <= age <= 86400000 or cancelled is not None and cancelled():
            return reject('unavailable')
        saved = {'proof':proof, 'status':'contacting', 'viewer_email':email, 'normalized_url':normalized}
        if review is not None:
            saved.update(review=review, proposal=proposal)
        jsonstore.write_atomic(path, saved)
        _sync(path)
        def guard():
            if (cancelled is not None and cancelled() or authority() != before
                    or _docsend_email(deck) != email):
                raise deck.DocSendHeldError('deck authority changed')
        try:
            http = deck.AttendedHttp(target, guard)
            def vision(images, title):
                guard()
                return deck._gemini_read(images, title, timeout=min(120,http.remaining()), max_body=8*1024*1024, guard=guard)
            guard()
            out = deck.read_deck(target, email, passcode, fresh, http=http, vision=vision, exact_cache=True)
            guard()
            result = _docsend_result(out, deck, normalized)
            for artifact in deck.deck_paths(deck.VIEW_RE.fullmatch(target).group('id')):
                if Path(artifact).is_file():
                    _sync(artifact)
            guard()
            receipt = {'version':1,'status':'committed', **{k:proof[k] for k in
                ('action_id','event_id','payload_hash','args_hash')},
                'outcome':{'operation':'docsend','result':result}}
            saved.update(status='completed',receipt=receipt)
            jsonstore.write_atomic(path,saved)
            _sync(path)
            return receipt
        except BaseException:
            return {'version':1,'status':'unknown'}
