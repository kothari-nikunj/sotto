"""Every entry script finds every pack module its functions import, in a fresh process.

conftest.py puts every pack directory on sys.path, so a lazy `import knowledge` inside a function
passes the suite even when the script that runs it in production never added that directory; it
then fails only in a fresh `python3 path/script.py` (the composer's graph-name mute check went
quietly blank this way). This suite starts one isolated interpreter per entry script, with no
PYTHONPATH and empty HOME and SOTTO_DATA, and lets fresh_import_probe.py resolve every import
written inside a function of the script and of the pack modules it reaches.

Entry scripts: each .py with a `__main__` block in a skill's `scripts/` directory, `_shared/scripts`,
`_shared/lib` or `_shared/knowledge`; those are the files the receiver, procedure runner and
skills launch as subprocesses. tools/ and evals/ are maintainer commands and are not covered.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import knowledge as kg

PACK = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).with_name('fresh_import_probe.py')
MAIN = re.compile(r'''^if __name__ == ['"]__main__['"]''', re.M)
SHARED = ('_shared/scripts', '_shared/lib', '_shared/knowledge')


def entry_scripts():
    found = []
    for path in sorted(PACK.glob('*/*/*.py')):
        rel = path.relative_to(PACK).as_posix()
        in_scope = rel.rsplit('/', 1)[0] in SHARED or (
            not rel.startswith('_shared/') and path.parent.name == 'scripts')
        if in_scope and MAIN.search(path.read_text(encoding='utf-8')):
            found.append(path)
    return found


# Port 9 (discard) on loopback is closed: any HTTP(S) request a regression makes fails at once,
# locally, instead of reaching a model provider.
NO_NETWORK = 'http://127.0.0.1:9'


def fresh_env(root):
    """No PYTHONPATH, no credentials, no network, nothing from this test process but PATH."""
    (root / 'home').mkdir(parents=True)
    (root / 'data').mkdir()
    return {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8',
            'HOME': str(root / 'home'), 'SOTTO_DATA': str(root / 'data'),
            'HTTPS_PROXY': NO_NETWORK, 'HTTP_PROXY': NO_NETWORK, 'https_proxy': NO_NETWORK,
            'http_proxy': NO_NETWORK, 'NO_PROXY': '', 'no_proxy': ''}


def _probe(script, root):
    done = subprocess.run([sys.executable, '-I', str(PROBE), str(script), str(PACK)],
                          env=fresh_env(root), cwd=root, capture_output=True, text=True, timeout=120)
    rel = script.relative_to(PACK).as_posix()
    if done.returncode or not done.stdout.strip():
        return rel, [f'probe exited {done.returncode}: {done.stderr.strip()[-400:]}']
    result = json.loads(done.stdout.strip().splitlines()[-1])
    return rel, ([f'top level failed: {result["error"]}'] if result['error'] else []) + result['missing']


def test_entry_set_covers_the_launched_scripts():
    names = {p.relative_to(PACK).as_posix() for p in entry_scripts()}
    for launched in ('_shared/scripts/compose_notification.py', '_shared/scripts/memory_cycle.py',
                     'event-triage/scripts/triage_event.py', '_shared/knowledge/knowledge_query.py',
                     '_shared/lib/relationship_importance.py', 'proactive/scripts/proactive_scan.py'):
        assert launched in names


def test_every_entry_script_resolves_its_lazy_imports_in_a_fresh_process(tmp_path):
    scripts = entry_scripts()
    with ThreadPoolExecutor(max_workers=max(2, min(8, os.cpu_count() or 2))) as pool:
        results = list(pool.map(lambda s: _probe(s[1], tmp_path / str(s[0])), enumerate(scripts)))
    broken = {rel: problems for rel, problems in results if problems}
    assert not broken, 'imports that fail outside the test suite\'s sys.path:\n' + json.dumps(
        broken, indent=1)


def test_fresh_composer_suppresses_a_sender_muted_by_graph_name(tmp_path):
    """The real composer, run as the receiver runs it, finds the graph name and stays silent."""
    env = fresh_env(tmp_path)
    data = Path(env['SOTTO_DATA'])
    os.environ['SOTTO_DATA'] = str(data)  # conftest restores the environment after the test
    people = data / 'knowledge/people'
    people.mkdir(parents=True)
    kg.write_person_file(str(people / 'c_dana00000001.md'), kg.PersonFile(
        canonical_id='c_dana00000001', name='Dana Graphname', identifiers=['dana@outside.example']))
    (data / 'preferences.json').write_text(json.dumps({'explicit': {'mute_people': ['Dana Graphname']}}))
    event = {'source': 'email', 'id': 'm1', 'threadId': 't1', 'from': 'dana@outside.example',
             'date': '2026-11-07T10:58:00Z', 'text': 'Please review the proposal urgently.'}
    bundle = data / 'bundle.json'
    bundle.write_text(json.dumps({'events': [{'decision_id': 'd1', 'class': 'urgent',
                                              'sender': 'dana@outside.example', 'event': event}]}))
    done = subprocess.run(
        [sys.executable, '-I', str(PACK / '_shared/scripts/compose_notification.py'),
         json.dumps({'kind': 'event', 'bundle_path': str(bundle)})],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert (done.returncode, done.stdout.strip()) == (0, 'NO_NUDGES'), done.stderr[-2000:]
