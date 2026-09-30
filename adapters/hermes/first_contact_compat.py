"""Pin-checked Hermes gateway adaptation for dedicated Sotto's first owner reply.

Only an explicit Sotto identity and opt-in config skip the upstream first-contact
intro/profile directive. The separate home-channel hint stays untouched.
"""
import hashlib
import os
from pathlib import Path
import sys
import tempfile


# gateway/run_turn.py at adapters/hermes/hermes.commit (245e4800).
PINNED_SOURCE_SHA256 = '6c81766d136810effa0fb793b1c947e8843a1fefb54a8f772eff25751639559b'
OLD_ANCHOR = '        if not await self.async_session_store.has_any_sessions():\n'
NEW_ANCHOR = '''        # Dedicated Sotto already has its own consent and first-brief onboarding.
        # This switch suppresses only the generic first-contact intro/profile note;
        # the separate missing-home-channel hint below still runs.
        try:
            _sotto_cfg = _load_gateway_config()
            _sotto_agent = _sotto_cfg.get("agent") if isinstance(_sotto_cfg, dict) else None
            _sotto_onboarding = _sotto_cfg.get("onboarding") if isinstance(_sotto_cfg, dict) else None
            _quiet_sotto_first_contact = (
                isinstance(_sotto_agent, dict) and _sotto_agent.get("name") == "Sotto"
                and isinstance(_sotto_onboarding, dict)
                and _sotto_onboarding.get("sotto_quiet_first_contact") is True
            )
        except Exception:
            _quiet_sotto_first_contact = False
        if not await self.async_session_store.has_any_sessions() and not _quiet_sotto_first_contact:
'''


def _original(source):
    original = source.replace(NEW_ANCHOR, OLD_ANCHOR, 1) if NEW_ANCHOR in source else source
    found = hashlib.sha256(original.encode()).hexdigest()
    if found != PINNED_SOURCE_SHA256 or original.count(OLD_ANCHOR) != 1:
        raise RuntimeError('Hermes first-contact gateway differs from the reviewed pin')
    if NEW_ANCHOR in source and source.count(NEW_ANCHOR) != 1:
        raise RuntimeError('Hermes first-contact patch is duplicated')
    return original


def check(path):
    _original(Path(path).read_text())


def patch(path):
    path = Path(path)
    source = path.read_text()
    original = _original(source)
    if source != original:
        return
    fd, temporary = tempfile.mkstemp(prefix='.first-contact-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(original.replace(OLD_ANCHOR, NEW_ANCHOR, 1))
            stream.flush()
            os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv):
    if len(argv) not in (1, 2) or (len(argv) == 2 and argv[0] != '--check'):
        print('usage: first_contact_compat.py [--check] <hermes>/gateway/run_turn.py', file=sys.stderr)
        return 2
    try:
        path = argv[-1]
        check(path) if len(argv) == 2 else patch(path)
    except (OSError, RuntimeError) as error:
        print(f'[sotto] FATAL: {error}; review the Hermes pin before changing the gateway', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
