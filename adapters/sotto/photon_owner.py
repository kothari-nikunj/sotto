"""Photon owner policy and the first-contact activation receipt the receiver writes."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


def managed():
    return os.environ.get("SOTTO_DEPLOYMENT_MODE") == "managed"


def project_fingerprint(project_id):
    """Bind an activation receipt to a project without persisting its credential."""
    if not isinstance(project_id, str) or not project_id.strip():
        return None
    return hashlib.sha256(('sotto-photon-project-v1:' + project_id).encode()).hexdigest()


def restricted_allowlist():
    return (os.environ.get('PHOTON_ALLOW_ALL_USERS', '').strip().lower()
            in ('', 'false', '0', 'no', 'off'))


def permits(source, owner):
    return bool(owner and source and source.chat_type == "dm" and source.user_id == owner)


def _sync_directory(path):
    """Persist a renamed receipt's directory entry before acknowledging activation."""
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def record_owner_activation(source):
    """Only an authenticated owner DM can open this instance's first-brief gate."""
    owner = os.environ.get('PHOTON_HOME_CHANNEL', '')
    chat = getattr(source, 'chat_id', None)
    if (not restricted_allowlist() or os.environ.get('PHOTON_ALLOWED_USERS') != owner
            or not permits(source, owner)
            or not isinstance(chat, str) or not chat):
        return False
    fingerprint = project_fingerprint(os.environ.get('PHOTON_PROJECT_ID'))
    if not managed() and not fingerprint:
        return False
    root = Path(os.environ.get('SOTTO_DATA', '/data')) / 'config'
    root.mkdir(parents=True, exist_ok=True)
    # Repeat the parent barrier on retry: a prior mkdir may have succeeded while
    # its directory fsync failed. SOTTO_DATA is the pre-existing volume root.
    _sync_directory(root.parent)
    receipt = root / 'photon-activation.json'
    payload = {'tenant_id': os.environ.get('SOTTO_TENANT_ID', ''), 'owner': owner,
               'chat_id': chat, 'activated': True}
    if fingerprint:
        payload['project_fingerprint'] = fingerprint
    try:
        existing = json.loads(receipt.read_text())
    except (OSError, ValueError):
        existing = None
    if existing == payload:
        # A prior rename may have succeeded but its directory fsync failed.
        _sync_directory(root)
        return True
    # A receipt for an earlier owner/tenant/conversation must not strand a new
    # invited user. Unique temporary files also tolerate simultaneous callbacks.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=root, prefix='.photon-', delete=False) as out:
            temporary = Path(out.name)
            json.dump(payload, out)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(receipt)
        _sync_directory(root)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True
