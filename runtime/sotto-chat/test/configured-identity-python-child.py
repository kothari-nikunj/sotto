"""One-shot real Python client verifier for the loopback configured-channel contract."""
import importlib.util
import json
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        raise SystemExit(2)
    source_root = Path(sys.argv[1]).resolve()
    path = source_root / 'adapters' / 'sotto' / 'client.py'
    spec = importlib.util.spec_from_file_location('sotto_configured_chat_client', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    payload = json.loads(sys.stdin.read(16 * 1024))
    try:
        client = module.ChatClient(payload['url'], payload['token'], timeout=3)
        accepted = client.verify_configured_identity(payload['expected'])
        print(json.dumps({'matched': accepted is True}), flush=True)
    except (ValueError, RuntimeError, OSError):
        print(json.dumps({'matched': False}), flush=True)


if __name__ == '__main__':
    main()
