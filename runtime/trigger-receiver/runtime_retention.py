"""One authenticated loopback call for the receiver's existing retention clock.

The receiver supplies canonical UTC cutoffs. The shared Sotto ChatClient owns URL,
bearer, proxy and redirect validation; this module has no timer or TTL values.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


class RetentionCallError(RuntimeError):
    pass


_CLIENT_MODULE = None


def _client_module():
    global _CLIENT_MODULE
    if _CLIENT_MODULE is None:
        for base in (Path(__file__).resolve().parents[2] / 'adapters' / 'sotto',
                     Path('/app/adapters/sotto')):
            path = base / 'client.py'
            if path.is_file():
                spec = importlib.util.spec_from_file_location('sotto_retention_client', path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module  # dataclass resolves this module while loading
                spec.loader.exec_module(module)
                _CLIENT_MODULE = module
                break
        if _CLIENT_MODULE is None:
            raise RetentionCallError('Sotto loopback client is unavailable')
    return _CLIENT_MODULE


def prune(url: str, token: str, cutoffs: dict, *, opener=None) -> dict:
    """Request terminal journal pruning; a missing ACK is an error, never success."""
    if not isinstance(cutoffs, dict) or set(cutoffs) != {'delivery_before', 'action_before'}:
        raise RetentionCallError('Sotto retention cutoffs are invalid')
    try:
        client = _client_module().ChatClient(url, token, timeout=20, opener=opener)
    except (ValueError, OSError) as error:
        raise RetentionCallError('Sotto retention loopback is not configured') from error
    response = client.call('retention', cutoffs)
    if not response.ok:
        raise RetentionCallError('Sotto journal pruning was not acknowledged')
    result = response.value
    # Node also reports `sdkExpired` (expired Mastra state it pruned); it is a count like the others.
    expected = {'replies', 'dispatches', 'actions', 'runs', 'events'}
    if not isinstance(result, dict) or not expected <= set(result) <= expected | {'sdkExpired'} or any(
            type(result[key]) is not int or result[key] < 0 for key in result):
        raise RetentionCallError('Sotto journal pruning response is invalid')
    return result
