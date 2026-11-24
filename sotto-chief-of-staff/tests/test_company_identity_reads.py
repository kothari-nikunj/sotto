import json
import os
import subprocess
import sys

import pytest

import knowledge_query as kq
import knowledge_update as ku


def save_company(directory, slug, domain):
    path = directory / (slug + '.md')
    path.write_text(f'---\nnormalized: {slug}\ndomain: {domain}\n---\n## About\nBuilds {slug} tools.\n')
    return path


@pytest.fixture
def companies(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    directory = tmp_path / 'knowledge' / 'companies'
    directory.mkdir(parents=True)
    return directory


def test_calendar_brief_withholds_duplicate_domain_and_packs_unrelated_company(companies, tmp_path):
    for slug in ('cedar-east', 'cedar-west'):
        save_company(companies, slug, 'cedar.example')
    save_company(companies, 'alder', 'alder.example')
    calendar = tmp_path / 'calendar.json'
    calendar.write_text(json.dumps([{'attendees': [{'email': 'lee@cedar.example'}, {'email': 'ari@alder.example'}]}]))

    def query(*args):
        return json.loads(subprocess.check_output([sys.executable, kq.__file__, *args], text=True))

    assert query('--company', 'cedar.example')['status'] == 'ambiguous'
    assert set(query('--calendar', str(calendar))['company_knowledge']) == {'alder'}


def test_company_update_does_not_pick_the_first_owner_of_a_domain(companies):
    first = save_company(companies, 'cedar-east', 'cedar.example')
    second = save_company(companies, 'cedar-west', 'cedar.example')
    before = {p.name: p.read_bytes() for p in companies.iterdir()}
    update = {'company_name': 'Cedar', 'domain': 'cedar.example', 'about': 'New observation.'}
    result = ku.apply({'company_updates': [update]})
    assert result['company_files'] == []
    assert {p.name: p.read_bytes() for p in companies.iterdir()} == before

    corrected = ku.apply({'company_updates': [{'company_slug': second.stem, 'about': 'Corrected by owner.'}]})
    assert corrected['company_files'] == [second.name]
    assert 'Corrected by owner.' in second.read_text()
    assert first.read_bytes() == before[first.name]


def test_company_pack_reads_directory_once_per_nonempty_request(companies, monkeypatch):
    one = save_company(companies, 'cedar', 'cedar.example')
    save_company(companies, 'alder', 'alder.example')
    scans = []

    def count_scans(original):
        def counted(path, *args, **kwargs):
            if path == str(companies) or path == companies:
                scans.append(path)
            return original(path, *args, **kwargs)
        return counted

    for name in ('scandir', 'listdir'):
        monkeypatch.setattr(os, name, count_scans(getattr(os, name)))

    assert kq.pack_companies([], []) == {}
    assert not scans
    assert set(kq.pack_companies(['missing.example', 'cedar.example', 'alder.example'], ['cedar'])) == {'cedar', 'alder'}
    assert len(scans) == 1
    scans.clear()
    one.unlink()
    assert set(kq.pack_companies(['cedar.example', 'alder.example'], [])) == {'alder'}
    assert len(scans) == 1
