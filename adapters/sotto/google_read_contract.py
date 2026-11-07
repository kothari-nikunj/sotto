"""Finite, content-free Google read diagnostics shared with the Node runtime."""
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PATHS = (_ROOT / 'runtime/sotto-chat/src/tools/google-read-contract.json',
          _ROOT / 'runtime/sotto-chat/dist/tools/google-read-contract.json')
_CONTRACT = json.loads(next(path for path in _PATHS if path.is_file()).read_text())
STAGES = frozenset(_CONTRACT['stages'])
RECEIVER_STAGES = frozenset(stage for stage, allowed in _CONTRACT['stages'].items() if allowed)
PHASE_TOOLS = _CONTRACT['phases']


def http_status(value):
    return (type(value) is int
            and _CONTRACT['httpStatus']['minimum'] <= value <= _CONTRACT['httpStatus']['maximum'])


def phase_matches(tool, phase):
    return isinstance(phase, str) and phase in PHASE_TOOLS and PHASE_TOOLS[phase] == tool


def receiver_failure(error, stage, operation):
    category = getattr(error, 'sotto_read_stage', stage)
    if not isinstance(category, str) or category not in RECEIVER_STAGES:
        category = 'unknown'
    diagnostic = {'error': 'Google read unavailable', 'read_stage': category}
    status = getattr(error, 'sotto_read_http_status', None)
    phase = getattr(error, 'sotto_read_phase', None)
    if http_status(status):
        diagnostic['read_http_status'] = status
    if phase_matches(operation, phase):
        diagnostic['read_phase'] = phase
    return diagnostic
