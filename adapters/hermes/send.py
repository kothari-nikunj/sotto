"""Pinned Hermes CLI send contract; transport acceptance is not device delivery."""
import json
import subprocess

CAPABILITY_ERROR = ('Hermes is incompatible with Sotto delivery: `hermes send` must support --json; '
                    'install the pinned Hermes commit from adapters/hermes/hermes.commit')


def send_capability(run=None):
    """Probe without sending; provider message IDs remain mandatory on every real acceptance."""
    try:
        result = (run or subprocess.run)(['hermes', 'send', '--help'], capture_output=True,
                                         text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False, CAPABILITY_ERROR
    help_text = (result.stdout or '') + '\n' + (result.stderr or '')
    return (True, '') if result.returncode == 0 and '--json' in help_text else (False, CAPABILITY_ERROR)


def send_receipt(body, target, timeout=60):
    """Return sanitized provider acceptance evidence, never response/message content."""
    try:
        result = subprocess.run(['hermes', 'send', '--to', target, '--json', '-f', '-'],
                                input=body, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        state = 'not_attempted' if isinstance(exc, OSError) else 'unknown'
        return False, type(exc).__name__, {'acceptance': state, 'target': target}
    if result.returncode:
        if '--json' in (result.stderr or '') and any(word in (result.stderr or '').lower()
                                                   for word in ('unknown', 'unrecognized', 'invalid')):
            return False, CAPABILITY_ERROR, {'acceptance': 'not_attempted', 'target': target}
        return False, f'Hermes send exited {result.returncode}', {'acceptance': 'unknown', 'target': target}
    try:
        receipt = json.loads(result.stdout)
    except (ValueError, TypeError):
        return False, 'Hermes returned no structured acceptance receipt', {'acceptance': 'unknown', 'target': target}
    if not isinstance(receipt, dict) or receipt.get('success') is not True or receipt.get('skipped'):
        return False, 'Hermes did not accept this send', {'acceptance': 'rejected', 'target': target}
    message_id = receipt.get('message_id')
    if not isinstance(message_id, str) or not message_id.strip():
        return False, 'Hermes returned no provider message ID', {'acceptance': 'unknown', 'target': target}
    return True, '', {'acceptance': 'accepted', 'message_id': message_id, 'target': target}


def send(body, target, timeout=60):
    """Compatibility for callers that consume only success and a safe diagnostic."""
    ok, detail, _receipt = send_receipt(body, target, timeout)
    return ok, detail
