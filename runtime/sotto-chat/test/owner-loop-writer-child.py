"""Credential-free one-shot fixture using the canonical Python continuity writer."""
import importlib.util
import json
import os
import stat
from datetime import datetime
from pathlib import Path
import sys


def main():
    if len(sys.argv) != 2:
        raise SystemExit(2)
    root = Path(sys.argv[1]).resolve()
    source = root / 'sotto-chief-of-staff' / '_shared' / 'knowledge' / 'knowledge_edit.py'
    spec = importlib.util.spec_from_file_location('sotto_fixture_knowledge_edit', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    provenance = importlib.import_module('ledger_provenance')
    protected_io = importlib.import_module('protected_ledger_io')

    request = json.loads(sys.stdin.read(4096))
    text = request['text']
    when = datetime.fromisoformat(request['now'])
    if request['action'] in ('write', 'write_fail_dirsync'):
        real_fsync = os.fsync
        real_replace = os.replace
        durability_trace = []

        def traced_fsync(fd):
            is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            durability_trace.append('directory_fsync' if is_directory else 'file_fsync')
            if is_directory and request['action'] == 'write_fail_dirsync':
                raise OSError('synthetic directory fsync failure')
            return real_fsync(fd)

        def traced_replace(src, dst):
            durability_trace.append('replace')
            return real_replace(src, dst)

        os.fsync = traced_fsync
        os.replace = traced_replace
        try:
            result = module.op_loop_add_admitted_owner_text(
                text, owner_text_span=text, owner_event_id=request['event_id'], now=when)
            response = {'ok': True, 'result': result, 'durability_trace': durability_trace}
        except module.OwnerLoopPreEffectRefusalError as exc:
            response = {'ok': False, 'error': exc.code, 'durability_trace': durability_trace}
        except module.EditError:
            response = {'ok': False, 'error': 'writer_refused', 'durability_trace': durability_trace}
        except OSError:
            response = {'ok': False, 'error': 'uncertain', 'durability_trace': durability_trace}
        finally:
            os.fsync = real_fsync
            os.replace = real_replace
    elif request['action'] == 'seed_source_collision':
        # The real legacy manual writer makes the colliding anchor; the real
        # continuity serializer persists a synthetic Gmail-origin correction.
        module.op_loop_add(text, now=when)
        resolver = module._load_continuity()
        rows = resolver._load_items()
        if len(rows) != 1:
            raise RuntimeError('fixture needs one canonical row')
        row = next(iter(rows.values()))
        row.update(source='gmail', channel='gmail', source_thread_id='gmail:fixture-thread')
        row.pop('owner_event_id', None)
        row['content_provenance'] = {'version': 1, 'kind': 'sources', 'sources': ['gmail'],
                                     'digest': provenance.digest_frontmatter(row)}
        resolver._persist(row)
        # The canonical transition writer may add proof_transition on first save.
        # Bind the fixture's source provenance to the final canonical fields.
        row = next(iter(resolver._load_items().values()))
        row['content_provenance']['digest'] = provenance.digest_frontmatter(row)
        resolver._persist(row)
        response = {'ok': True}
    elif request['action'] == 'inspect':
        rows = protected_io.load_protected_entries(request['data_root'], strict=True)
        response = {'ok': True, 'rows': [{
            'event_id': row.get('owner_event_id'), 'summary': row.get('summary'),
            'anchor': row.get('anchor_key'), 'source': row.get('source'),
            'provenance': provenance.classify_ledger_sources(row)[0] if row.get('content_provenance') else 'legacy',
        } for row in rows]}
    else:
        raise RuntimeError('unsupported fixture action')
    print(json.dumps(response), flush=True)


if __name__ == '__main__':
    main()
