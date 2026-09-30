"""Select the dedicated Sotto first-contact behavior in Hermes config.yaml."""
import os
from pathlib import Path
import sys
import tempfile

import yaml


GENERIC_AGENT_NAMES = (None, '', 'Hermes')


def select(path):
    path = Path(path)
    try:
        config = yaml.safe_load(path.read_text()) if path.exists() else None
    except (OSError, UnicodeError, yaml.YAMLError):
        print('[sotto] Hermes config.yaml is unreadable; leaving it unchanged', file=sys.stderr)
        return
    if config is None:
        config = {}
    if isinstance(config, dict):
        agent = {} if config.get('agent') is None else config['agent']
        onboarding = {} if config.get('onboarding') is None else config['onboarding']
    if not isinstance(config, dict) or not isinstance(agent, dict) or not isinstance(onboarding, dict):
        print('[sotto] Hermes config.yaml is not the expected mapping; leaving it unchanged', file=sys.stderr)
        return
    # A self-host owner's persona name is theirs; only the missing/generic default becomes Sotto.
    name = agent.get('name') if agent.get('name') not in GENERIC_AGENT_NAMES else 'Sotto'
    if (agent.get('name') == name and onboarding.get('profile_build') == 'off'
            and onboarding.get('sotto_quiet_first_contact') is True):
        return
    config['agent'], config['onboarding'] = agent, onboarding
    agent['name'] = name
    onboarding.update(profile_build='off', sotto_quiet_first_contact=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.sotto-first-contact-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            yaml.safe_dump(config, stream, sort_keys=False)
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == '__main__':
    select(sys.argv[1])
