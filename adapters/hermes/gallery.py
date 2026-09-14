"""Private loopback gallery transport; no credentials, paths or content in diagnostics."""
import importlib.util
import fcntl
import hashlib
import time
import json
import os
import re
import sys
from pathlib import Path
import urllib.error
import urllib.request


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


def send(paths, summary, target, timeout=60):
    # The receiver's resolved destination, not an arbitrary model-supplied recipient.
    chat_id = target.partition(':')[2] or os.environ.get('PHOTON_HOME_CHANNEL', '')
    if (target != 'photon' and not target.startswith('photon:')) or not chat_id:
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
    ok, acceptance, result = _call('send-gallery', {'spaceId': chat_id, 'paths': list(map(str, validated)), 'summary': summary}, timeout)
    if not isinstance(result, dict):
        return False, 'gallery acceptance unconfirmed', {'acceptance': 'unknown'}
    identifier = result.get('messageId')
    if ok and result.get('ok') is True and isinstance(identifier, str) and identifier:
        ids = result.get('messageIds') or []
        return True, '', {'acceptance': 'accepted', 'message_id': identifier,
                           'message_ids': [x for x in ids if isinstance(x, str)]}
    return False, 'gallery acceptance unconfirmed', {'acceptance': 'unknown' if ok else acceptance}


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



def send_once(paths, summary, target, reply_to=None):
    """Keep an interactive send receipt beside its immutable cards across gateway restarts.

    This is a transport receipt, not a work queue: Hermes retains the final-response obligation.
    A matching unresolved dispatch cannot be retried or replaced with text. Cache retention owns
    its lifetime. The reply anchor separates a new user request from delivery retries.
    """
    unknown = (False, 'gallery acceptance unconfirmed', {'acceptance': 'unknown'})
    root = (Path(os.environ.get('SOTTO_DATA', '/data')) / 'cache/visual-briefs').resolve()
    try:
        directory = Path(paths[0]).resolve(strict=True).parent
        if root not in directory.parents:
            return False, 'gallery files unavailable', {'acceptance': 'not_attempted'}
        identifier = hashlib.sha256((target + '\n' + str(reply_to or '')).encode()).hexdigest()[:24]
        path = directory / ('delivery-' + identifier + '.json')
        fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    except (OSError, ValueError, IndexError, TypeError):
        return False, 'gallery receipt unavailable', {'acceptance': 'not_attempted'}
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            receipt = json.loads(path.read_text())
            if receipt.get('acceptance') == 'accepted':
                return True, '', receipt
            if receipt.get('acceptance') == 'unknown':
                return unknown
        def save(receipt):
            temporary = path.with_suffix('.tmp')
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as handle:
                json.dump({**receipt, 'target_hash': hashlib.sha256(target.encode()).hexdigest(),
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
        result = send(paths, summary, target)
        save(result[2])
        return result
    except (OSError, ValueError):
        return unknown  # busy/corrupt/unwritable receipt must never authorize another send
    finally:
        os.close(fd)



def recovery_receipt(content, target):
    """Hermes recovery calls send() without an inbound anchor; consult existing artifact receipts.

    Scan immutable manifests instead of rebuilding a versioned deck: a release may change layout
    while an old response obligation is still unresolved. No renderer, capability probe or send.
    """
    try:
        from gateway.delivery_ledger import RECOVERED_MARKER, RECONNECTED_MARKER  # noqa: PLC0415
        marker = next((m for m in (RECOVERED_MARKER, RECONNECTED_MARKER) if content.startswith(m)), None)
        if not marker:
            return None
        # This exact formatter is copied beside the plugin at installation.
        from .chatfmt import to_imessage  # noqa: PLC0415
        original = to_imessage(content[len(marker):], normalize_style=False).strip()
    except ImportError:
        return None
    root = Path(os.environ.get('SOTTO_DATA', '/data')) / 'cache/visual-briefs'
    target_hash = hashlib.sha256(target.encode()).hexdigest()
    for manifest_path in root.glob('*/manifest.json'):
        try:
            if json.loads(manifest_path.read_text()).get('full_text') != original:
                continue
            for path in manifest_path.parent.glob('delivery-*.json'):
                receipt = json.loads(path.read_text())
                if receipt.get('target_hash') == target_hash and receipt.get('acceptance') in ('accepted', 'unknown'):
                    return receipt
        except (OSError, ValueError, AttributeError):
            # Corruption is not evidence that an attempted send was rejected.
            return {'acceptance': 'unknown'}
    return None
