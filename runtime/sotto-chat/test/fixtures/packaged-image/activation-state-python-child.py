"""One-shot stdlib verifier for the real private Node activation-state endpoint."""
import importlib.util
import json
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        raise SystemExit(2)
    # The caller supplies a fixture marker only; exercise the client packaged in the image.
    Path(sys.argv[1]).resolve()
    source = Path('/app/adapters/sotto/client.py')
    spec = importlib.util.spec_from_file_location('sotto_activation_contract_client', source)
    if spec is None or spec.loader is None:
        raise SystemExit(2)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    raw = sys.stdin.read(16 * 1024 + 1)
    if len(raw) > 16 * 1024:
        raise SystemExit(2)
    request = json.loads(raw)
    if set(request) != {'url', 'token', 'expected'}:
        raise SystemExit(2)
    try:
        value = module.ChatClient(request['url'], request['token'], timeout=3).activation_state(request['expected'])
        admission = value['activation']
        output = {'matched': True, 'status': admission['status'],
                  'mode': admission['mode'], 'event_id': admission.get('event_id'),
                  'thread': (admission.get('binding') or {}).get('provider_thread_id')}
    except (ValueError, RuntimeError, OSError, TypeError):
        output = {'matched': False}
    print(json.dumps(output), flush=True)


if __name__ == '__main__':
    main()
