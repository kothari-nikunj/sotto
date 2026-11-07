"""Process-private managed control credential, captured before runtime imports."""
import os

_CONTROL = os.environ.pop('SOTTO_CONTROL_TOKEN', '')


def get(fallback_env=None):
    """Production uses the captured value; explicit test environments remain injectable."""
    if _CONTROL:
        return _CONTROL
    return (fallback_env or os.environ).get('SOTTO_CONTROL_TOKEN', '')
