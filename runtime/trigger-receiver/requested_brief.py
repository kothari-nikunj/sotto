"""Backend-neutral owner brief requests. No model call is needed to replay an accepted artifact."""
import hashlib
import json
import math
from pathlib import Path
import re

LABEL = re.compile(r'^requested:([a-f0-9]{32}):sotto-(morning|evening)-brief$')


def telegram_binding(proof, authority):
    """Retain the exact independently proved owner route, including the bot identity."""
    fields = ('route_digest', 'bot_fingerprint', 'provider_thread_id', 'tenant_id',
              'owner_id', 'account_id', 'line_key')
    if (not isinstance(proof, dict) or proof.get('version') != 1
            or proof.get('status') != 'current_channel_proved'
            or proof.get('action_authorized') is not False
            or any(not isinstance(proof.get(key), str) or not proof[key] for key in fields)
            or any(re.fullmatch('[a-f0-9]{64}', proof[key]) is None
                   for key in ('route_digest', 'bot_fingerprint'))
            or not proof['owner_id'].isascii() or not proof['owner_id'].isdigit()
            or proof['provider_thread_id'] != 'telegram:' + proof['owner_id']):
        raise ValueError('requested Telegram route unavailable')
    if (not isinstance(authority, dict) or authority.get('complete') is not True
            or not isinstance(authority.get('revision'), str)
            or re.fullmatch('[a-f0-9]{64}', authority['revision']) is None):
        raise ValueError('requested Telegram source authority unavailable')
    return {**{key: proof[key] for key in fields}, 'source_revision': authority['revision']}


def known_brief_kind(payload):
    """Only canonical named morning/evening lanes prove an unrelated edition."""
    label = payload.get('label') if isinstance(payload, dict) else None
    if not isinstance(label, str):
        return None
    match = LABEL.fullmatch(label) or re.fullmatch(
        r'(?:brief|cron|run-now):sotto-(morning|evening)-brief', label)
    return match.groups()[-1] if match else None


def owner_destination(root, owner, chat_id, tenant_id):
    """Verify the normalized owner route independently of the agent backend."""
    if not owner:
        return False
    if chat_id == owner:
        return True
    try:
        receipt = json.loads((Path(root) / 'config/photon-activation.json').read_text())
        return (receipt.get('activated') is True and receipt.get('owner') == owner
                and receipt.get('tenant_id') == tenant_id and receipt.get('chat_id') == chat_id)
    except (OSError, ValueError, AttributeError):
        return False


def identity(owner, chat_id, message_id):
    return hashlib.sha256(json.dumps([owner, chat_id, message_id], separators=(',', ':')).encode()).hexdigest()[:32]


def accepted_artifact(root, day, kind, allowed):
    """Use an exact accepted run binding, never assume that composing meant delivery.

    Pre-binding legacy artifacts are intentionally not guessed. Revoked or missing provenance
    needs fresh composition; historical content must not bypass today's source permissions.
    """
    root = Path(root)
    try:
        rows = json.loads((root / 'events/outbox.json').read_text())['rows']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    accepted = {r['id'] for r in rows if isinstance(r, dict) and r.get('status') == 'delivered'
                and r.get('day') == day and (r.get('payload') or {}).get('label', '').endswith(
                    ':sotto-' + kind + '-brief')}
    if not accepted:
        return None
    candidates = []
    for path in (root / 'events/work-inputs').glob('brief-*/artifact.json'):
        try:
            artifact = json.loads(path.read_text())
            if (not accepted.intersection(artifact.get('_delivery_run_ids', []))
                    or not isinstance(artifact.get('brief_text'), str)
                    or not artifact['brief_text'].strip()):
                continue
            if artifact.get('_composition_direction'):
                continue
            sources = artifact.get('_source_permissions')
            if not isinstance(sources, list) or not all(isinstance(s, str) and allowed(s) for s in sources):
                continue
            # Offers are transactional: never replay an old actionable question.
            if any(artifact.get(key) for key in ('_review_candidate', '_review_question', '_parking_notices')):
                continue
            stamp = float(artifact.get('composed_at') or 0)
            if not math.isfinite(stamp) or stamp <= 0:
                continue
            candidates.append(artifact)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return max(candidates, key=lambda a: float(a.get('composed_at') or 0), default=None)
