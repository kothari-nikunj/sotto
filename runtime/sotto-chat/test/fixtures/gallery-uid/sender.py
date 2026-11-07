import argparse
import importlib.util
import json
import os
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--adapter-root', required=True)
parser.add_argument('--port', required=True, type=int)
parser.add_argument('--data-root', required=True)
parser.add_argument('--bearer', required=True)
args = parser.parse_args()
sys.path.insert(0, args.adapter_root)
spec = importlib.util.spec_from_file_location('sotto_runtime_api_fixture', os.path.join(args.adapter_root, 'runtime_api.py'))
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)
os.environ['SOTTO_DATA'] = args.data_root
os.environ['SOTTO_CHAT_RUNTIME_URL'] = f'http://127.0.0.1:{args.port}'
os.environ['SOTTO_CHAT_RUNTIME_TOKEN'] = args.bearer
artifact = os.path.join(args.data_root, 'cache', 'visual-briefs', 'a' * 24)
paths = [os.path.join(artifact, f'{index:02d}.png') for index in range(1, 5)]
first = api.send_gallery({'images': paths, 'summary': 'synthetic offline gallery'}, 'owner', 'gallery-uid-smoke')
second = api.send_gallery({'images': paths, 'summary': 'synthetic offline gallery'}, 'owner', 'gallery-uid-smoke')
health = api._client(timeout=5).call('health')
print(json.dumps({'first': first, 'second': second, 'health': health.value if health.ok else None}, separators=(',', ':')))
if not first[0] or not second[0] or not health.ok:
    raise SystemExit('sender did not obtain durable accepted receipt')
