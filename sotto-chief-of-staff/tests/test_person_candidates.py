"""A person read by a partial name lists saved candidates instead of an empty no_match."""
import json
import os
import subprocess
import sys
from datetime import datetime

import knowledge as kg

QUERY = os.path.join(os.path.dirname(__file__), '..', '_shared', 'knowledge', 'knowledge_query.py')


def _save(cid, name, **fields):
    person = kg.PersonFile(canonical_id=cid, name=name, identifiers=[f'{cid}@example.test'], **fields)
    kg.write_person_file(os.path.join(kg.people_dir(), cid + '.md'), person, datetime(2026, 11, 1))


def _read(tmp_path, selector):
    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    proc = subprocess.run([sys.executable, QUERY, '--person', selector, '--person-coverage', '--interactive'],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_first_name_lists_the_saved_people_it_could_mean(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    os.makedirs(kg.people_dir(), exist_ok=True)
    _save('c_aaaaaaaaaaaa', 'Avery Quill', title='Partner', company='Example Ventures')
    _save('c_bbbbbbbbbbbb', 'Dana Reyes')
    result = _read(tmp_path, 'avery')
    assert result['person_knowledge'] == {}           # nothing is picked silently
    assert result['candidates']['items'] == [
        {'name': 'Avery Quill', 'canonical_id': 'c_aaaaaaaaaaaa', 'identity': 'Partner, Example Ventures'}]
    exact = _read(tmp_path, 'c_aaaaaaaaaaaa')
    assert 'c_aaaaaaaaaaaa' in exact['person_knowledge'] and 'candidates' not in exact


def test_a_shared_full_name_lists_both_people(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    os.makedirs(kg.people_dir(), exist_ok=True)
    _save('c_aaaaaaaaaaaa', 'Sam Lee')
    _save('c_bbbbbbbbbbbb', 'Sam Lee')
    result = _read(tmp_path, 'Sam Lee')
    assert result['coverage']['identity_resolution']['status'] == 'ambiguous'
    assert [row['canonical_id'] for row in result['candidates']['items']] == ['c_aaaaaaaaaaaa', 'c_bbbbbbbbbbbb']
