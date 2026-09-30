"""Private loopback gallery transport; no credentials, paths or content in diagnostics."""
import importlib.util
import fcntl
import hashlib
import importlib
import time
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import quote


def _ledger_obligation(obligation_id, target, content, states):
    """Prove the gateway owns this exact reply; never derive identity from its words."""
    if (not isinstance(obligation_id, str) or not re.fullmatch(r'[0-9a-f]{24}', obligation_id)
            or not isinstance(content, str) or not target.startswith('photon:')):
        return False
    try:
        ledger = importlib.import_module('gateway.delivery_ledger')
        database = ledger._db_path()
        stale_after = ledger.STALE_AFTER_SECONDS
        if not isinstance(stale_after, (int, float)) or stale_after <= 0:
            return False
        with closing(sqlite3.connect('file:' + quote(str(database)) + '?mode=ro', uri=True, timeout=1)) as conn:
            row = conn.execute('''SELECT platform, chat_id, content, state, created_at
                                  FROM delivery_obligations WHERE obligation_id=?''',
                               (obligation_id,)).fetchone()
        return bool(row and row[0] == 'photon' and row[1] == target.partition(':')[2]
                    and row[2] == content and row[3] in states
                    and isinstance(row[4], (float, int))
                    and 0 <= time.time() - row[4] <= stale_after)
    except (AttributeError, ImportError, OSError, sqlite3.Error, TypeError, ValueError):
        return False


def _call(endpoint, payload, timeout=60):
    record = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))) / 'runtime/photon-sidecar.json'
    try:
        state = json.loads(record.read_text())
        port, token = int(state['port']), state['token']
        if not 1 <= port <= 65535 or not token:
            raise ValueError('invalid sidecar record')
        request = urllib.request.Request(f'http://127.0.0.1:{port}/{endpoint}',
                    data=json.dumps(payload).encode(),
                    headers={'Content-Type': 'application/json', 'X-Hermes-Sidecar-Token': token})
    except (OSError, ValueError, KeyError):
        return False, 'not_attempted', {}
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
            return True, 'accepted', json.load(response)
    except urllib.error.HTTPError as error:
        return False, ({404: 'not_attempted', 400: 'rejected'}.get(error.code, 'unknown')), {}
    except (OSError, ValueError, urllib.error.URLError):
        return False, 'unknown', {}


def available():
    ok, _, result = _call('gallery-capability', {}, timeout=5)
    return ok and isinstance(result, dict) and result.get('galleryVersion') == 1


def send(paths, summary, target, timeout=60, dispatch_id=None):
    # The receiver's resolved destination, not an arbitrary model-supplied recipient.
    chat_id = target.partition(':')[2] or os.environ.get('PHOTON_HOME_CHANNEL', '')
    if (target != 'photon' and not target.startswith('photon:')) or not chat_id or not isinstance(dispatch_id, str):
        return False, 'gallery target unavailable', {'acceptance': 'not_attempted'}
    if os.environ.get('SOTTO_DEPLOYMENT_MODE') == 'managed':
        spec = importlib.util.spec_from_file_location('gallery_owner_policy',
                    (Path(__file__).parent / 'sotto_photon/__init__.py'
                     if (Path(__file__).parent / 'sotto_photon').is_dir() else Path(__file__).with_name('__init__.py')))
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        if not policy.owner_destination(chat_id):
            return False, 'gallery destination refused', {'acceptance': 'not_attempted'}
    if not isinstance(summary, str) or not summary or len(summary) > 1000:
        return False, 'gallery summary invalid', {'acceptance': 'not_attempted'}
    root = (Path(os.environ.get('SOTTO_DATA', '/data')) / 'cache/visual-briefs').resolve()
    try:
        validated = [Path(p).resolve(strict=True) for p in paths]
        if len(validated) != 4 or any(root not in p.parents or p.suffix != '.png'
                                             or p.stat().st_size > 5_000_000 for p in validated):
            raise ValueError('invalid media')
    except (OSError, ValueError, TypeError):
        return False, 'gallery files unavailable', {'acceptance': 'not_attempted'}
    ok, acceptance, result = _call('send-gallery', {'dispatchId': dispatch_id, 'spaceId': chat_id,
        'paths': list(map(str, validated)), 'summary': summary}, timeout)
    if not isinstance(result, dict):
        return False, 'gallery acceptance unconfirmed', {'acceptance': 'unknown'}
    identifier = result.get('messageId')
    if ok and result.get('ok') is True and isinstance(identifier, str) and identifier:
        ids = result.get('messageIds') or []
        return True, '', {'acceptance': 'accepted', 'message_id': identifier,
                           'message_ids': [x for x in ids if isinstance(x, str)]}
    return False, 'gallery acceptance unconfirmed', {'acceptance': 'unknown' if ok else acceptance}


def receipt(dispatch_id, timeout=5):
    """Read a content-free late operation receipt; this endpoint never sends."""
    if not isinstance(dispatch_id, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,160}', dispatch_id):
        return None
    ok, _, result = _call('gallery-receipt', {'dispatchId': dispatch_id}, timeout)
    if not ok or not isinstance(result, dict) or result.get('found') is not True:
        return None
    value = result.get('receipt')
    if not isinstance(value, dict) or value.get('dispatchId') != dispatch_id:
        return None
    acceptance = value.get('acceptance')
    if acceptance not in ('accepted', 'unknown', 'in_flight', 'not_attempted', 'rejected'):
        return None
    clean = {'acceptance': 'unknown' if acceptance == 'in_flight' else acceptance}
    if (clean['acceptance'] == 'accepted' and isinstance(value.get('messageId'), str)
            and 0 < len(value['messageId']) <= 512):
        clean['message_id'] = value['messageId']
        message_ids = value.get('messageIds') if isinstance(value.get('messageIds'), list) else []
        clean['message_ids'] = [x for x in message_ids[:8]
                                if isinstance(x, str) and 0 < len(x) <= 512]
    elif clean['acceptance'] == 'accepted':
        clean['acceptance'] = 'unknown'
    return clean


def prepare_prep(content):
    """Recognize the focused skill's output, then use the same renderer as scheduled briefs."""
    if os.environ.get('SOTTO_VISUAL_BRIEFS', '1') != '1':
        return None
    # Do not turn ordinary chat/progress messages or the compact multi-meeting sweep into cards.
    if not all(re.search(pattern, content, re.I | re.M) for pattern in (
            r'^\W*Your thread\W*$', r'^\W*What .+ builds\W*$',
            r'^\W*(?:Angles|Questions to ask|Talking points)\W*$')):
        return None
    if not available():
        return None
    home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    library = home / 'skills/sotto/_shared/lib'
    try:
        # The installed shared skill pack owns fonts, parsing and rendering in every tier.
        if str(library) not in sys.path:
            sys.path.insert(0, str(library))
        import visual_brief  # noqa: PLC0415 - optional installed renderer
        deck = visual_brief.build(content, 'prep')
        if not deck:
            return None
        manifest = visual_brief.render(deck, Path(os.environ.get('SOTTO_DATA', '/data')) / 'cache/visual-briefs')
        return {'images': manifest['images'], 'summary': manifest['summary']}
    except (OSError, ValueError, TypeError, ImportError):
        return None  # No send attempted; ordinary text remains safe.



def send_once(paths, summary, target, reply_to=None, obligation_id=None, content=None):
    """Keep an interactive send receipt beside its immutable cards across gateway restarts.

    This is a transport receipt, not a work queue: Hermes retains the final-response obligation.
    A matching unresolved dispatch cannot be retried or replaced with text. Cache retention owns
    its lifetime. The reply anchor separates a new user request from delivery retries.
    """
    unknown = (False, 'gallery acceptance unconfirmed', {'acceptance': 'unknown'})
    if obligation_id is not None and not _ledger_obligation(obligation_id, target, content,
                                                              ('pending', 'attempting', 'failed')):
        return unknown
    root = (Path(os.environ.get('SOTTO_DATA', '/data')) / 'cache/visual-briefs').resolve()
    try:
        directory = Path(paths[0]).resolve(strict=True).parent
        if root not in directory.parents:
            return False, 'gallery files unavailable', {'acceptance': 'not_attempted'}
        identity = obligation_id if obligation_id is not None else str(reply_to or '')
        identifier = hashlib.sha256((target + '\n' + identity).encode()).hexdigest()[:24]
        path = directory / ('delivery-' + identifier + '.json')
        # The sidecar dedupes globally; the content-addressed deck keeps different decks distinct.
        dispatch_id = hashlib.sha256((target + '\n' + identity + '\n'
                                      + directory.name).encode()).hexdigest()[:24]
        fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    except (OSError, ValueError, IndexError, TypeError):
        return False, 'gallery receipt unavailable', {'acceptance': 'not_attempted'}
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            receipt = json.loads(path.read_text())
            if receipt.get('obligation_id') != obligation_id:
                return unknown
            if receipt.get('acceptance') == 'accepted':
                return True, '', receipt
            if receipt.get('acceptance') == 'unknown':
                return unknown
        def save(receipt):
            temporary = path.with_suffix('.tmp')
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as handle:
                json.dump({**receipt, 'obligation_id': obligation_id,
                           'target_hash': hashlib.sha256(target.encode()).hexdigest(),
                           'recorded_at': time.time()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            parent_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        save({'acceptance': 'unknown'})  # durable before touching the provider
        result = send(paths, summary, target, 60, dispatch_id)
        save(result[2])
        return result
    except (OSError, ValueError):
        return unknown  # busy/corrupt/unwritable receipt must never authorize another send
    finally:
        os.close(fd)



def recovery_receipt(content, target, obligation_id=None):
    """Hermes recovery calls send() without an inbound anchor; consult existing artifact receipts.

    Scan immutable manifests instead of rebuilding a versioned deck: a release may change layout
    while an old response obligation is still unresolved. No renderer, capability probe or send.
    """
    try:
        delivery_ledger = importlib.import_module('gateway.delivery_ledger')
        marker = next((m for m in (delivery_ledger.RECOVERED_MARKER, delivery_ledger.RECONNECTED_MARKER)
                       if content.startswith(m)), None)
        raw_original = content[len(marker):] if marker else content
        if marker:
            # This exact formatter is copied beside the plugin at installation.
            from .chatfmt import to_imessage  # noqa: PLC0415
            original = to_imessage(raw_original, normalize_style=False).strip()
    except ImportError:
        return None
    # Only a live, unresolved ledger identity can attribute acceptance to this reply.
    # Text and receipt timestamps alone never establish which obligation was sent.
    identity_valid = _ledger_obligation(obligation_id, target, raw_original,
                                        ('pending', 'attempting', 'failed'))
    if obligation_id is not None and not identity_valid:
        return {'acceptance': 'unknown'}  # a failed identity check cannot authorize text fallback
    root = Path(os.environ.get('SOTTO_DATA', '/data')) / 'cache/visual-briefs'
    target_hash = hashlib.sha256(target.encode()).hexdigest()
    if identity_valid:
        # The obligation ID, not a rendered manifest, locates this transport receipt.
        # A later formatter or deck layout cannot turn a proven acceptance into text.
        identifier = hashlib.sha256((target + '\n' + obligation_id).encode()).hexdigest()[:24]
        exact = []
        for path in root.glob('*/delivery-' + identifier + '.json'):
            try:
                value = json.loads(path.read_text())
                if value.get('obligation_id') == obligation_id and value.get('target_hash') == target_hash:
                    exact.append(value)
            except (OSError, ValueError, AttributeError):
                return {'acceptance': 'unknown'}
        if exact:
            if len(exact) != 1:
                return {'acceptance': 'unknown'}
            value = exact[0]
            if value.get('acceptance') in ('not_attempted', 'rejected'):
                return None  # the provider did not accept media; ordinary text may retry
            if (value.get('acceptance') == 'accepted' and
                    (not isinstance(value.get('message_id'), str) or not value['message_id'])):
                return {'acceptance': 'unknown'}
            return value if value.get('acceptance') in ('accepted', 'unknown') else {'acceptance': 'unknown'}
    if not marker:
        return None
    matching_manifest = False
    for manifest_path in root.glob('*/manifest.json'):
        try:
            if json.loads(manifest_path.read_text()).get('full_text') != original:
                continue
        except (OSError, ValueError, AttributeError):
            continue  # an unreadable deck is not evidence about this reply
        try:
            for path in manifest_path.parent.glob('delivery-*.json'):
                receipt = json.loads(path.read_text())
                if receipt.get('target_hash') != target_hash:
                    continue
                if (identity_valid and receipt.get('obligation_id') and
                        receipt['obligation_id'] != obligation_id):
                    continue  # identical words from a different known reply are unrelated
                matching_manifest = True
        except (OSError, ValueError, AttributeError):
            # Corruption is not evidence that an attempted send was rejected.
            return {'acceptance': 'unknown'}
    # Words alone never identify the gateway obligation. A legacy matching
    # gallery without a distinct obligation ID remains uncertain regardless of age.
    return {'acceptance': 'unknown'} if matching_manifest else None
