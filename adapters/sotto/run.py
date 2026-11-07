#!/usr/bin/env python3
"""Receiver subprocess boundary: one unattended prompt in, final text out.

The receiver owns job persistence, retries, delivery and effects. This command
only calls the explicitly configured local agent. It never selects a provider,
discovers credentials or posts the result to a channel.
"""
import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

MODEL_ADMISSION_BUSY_EXIT = 78  # work_queue.MODEL_ADMISSION_BUSY_EXIT


def normalize_usage(document):
    """Normalize observed AI SDK totals; never invent prices or unknown counts."""
    if not isinstance(document, dict):
        return None
    raw = document.get('usage', document)
    if not isinstance(raw, dict):
        return None
    output = {}
    for target, aliases in {
        'input_tokens': ('input_tokens', 'inputTokens'),
        'output_tokens': ('output_tokens', 'outputTokens'),
        'cost': ('cost', 'cost_usd'),
    }.items():
        for key in aliases:
            value = raw.get(key)
            if isinstance(value, dict):
                value = value.get('total')
            if type(value) in (int, float) and 0 <= value <= 9_007_199_254_740_991 and math.isfinite(value):
                output[target] = value
                break
    model = document.get('model', raw.get('model'))
    if isinstance(model, str) and model.strip():
        output['model'] = model.strip()[:80]
    return output or None


def read_usage(path):
    if not path:
        return None
    try:
        with open(path, encoding='utf-8') as stream:
            data = stream.read(65537)
        return normalize_usage(json.loads(data)) if len(data) <= 65536 else None
    except (OSError, ValueError, TypeError):
        return None


def _write_usage(path, usage):
    """Optional receipt accounting must not turn a successful model run into a retry."""
    if not path or not usage:
        return
    temporary = None
    try:
        destination = Path(path)
        fd, temporary = tempfile.mkstemp(prefix='.usage-', dir=destination.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(usage, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError:
        pass
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _client_module():
    module = sys.modules.get('sotto_unattended_client')
    if module is not None:
        return module
    path = Path(__file__).with_name('client.py')
    spec = importlib.util.spec_from_file_location('sotto_unattended_client', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _client():
    return _client_module().ChatClient(os.environ.get('SOTTO_CHAT_RUNTIME_URL', ''),
                             os.environ.get('SOTTO_CHAT_RUNTIME_TOKEN', ''), timeout=600)


def main(argv=None, *, client_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--usage-file')
    parser.add_argument('prompt')
    args = parser.parse_args(argv)
    run_id = os.environ.get('SOTTO_DELIVERY_RUN_ID', '')
    if os.environ.get('SOTTO_UNATTENDED') != '1' or not re.fullmatch(r'[A-Za-z0-9._:-]{1,192}', run_id):
        print('Unattended receiver identity is required', file=sys.stderr)
        return 2
    if not args.prompt.strip() or len(args.prompt) > 32768:
        print('Unattended prompt exceeds its allowed bound', file=sys.stderr)
        return 2
    try:
        result = (client_factory or _client)().run(args.prompt, run_id)
        text = result.get('text') if isinstance(result, dict) else None
        if not isinstance(text, str) or len(text) > 1_000_000:
            raise ValueError('Invalid result')
    except Exception as error:  # noqa: BLE001 — sanitize errors at the receiver subprocess boundary
        # Exceptions may contain private URLs, bearer values or source content.
        if isinstance(error, _client_module().ModelAdmissionBusyError):
            print('Local model admission busy', file=sys.stderr)
            return MODEL_ADMISSION_BUSY_EXIT
        print('Local unattended runtime failed', file=sys.stderr)
        return 1
    _write_usage(args.usage_file, normalize_usage(result))
    print(text, end='' if text.endswith('\n') else '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
