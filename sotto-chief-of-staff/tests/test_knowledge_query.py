"""knowledge_query.py — the READ side of the graph.

Two things it is on the hook for, both of which were silently missing:
  1. `company_knowledge` — render_local has always had a "### Company Context" block and the
     companies/*.md files have always been written; nothing ever packed them.
  2. WHO packs — today's inputs (email + calendar), with the file-mtime window demoted to the
     fallback cohort used only when no inputs are supplied.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

import pytest

import knowledge as kg
import knowledge_query as kq
import knowledge_update as ku

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
QUERY = os.path.join(ROOT, "_shared", "knowledge", "knowledge_query.py")
EDIT = os.path.join(ROOT, "_shared", "knowledge", "knowledge_edit.py")


def _run(tmp_path, *args):
    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    proc = subprocess.run([sys.executable, QUERY, *args], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _cal(tmp_path, *emails):
    p = tmp_path / "cal.json"
    p.write_text(json.dumps([{"summary": "Sync", "attendees": [{"email": e} for e in emails]}]))
    return str(p)


def _topic_cal(tmp_path, email, summary, description):
    p = tmp_path / "topic-cal.json"
    p.write_text(json.dumps([{"summary": summary, "description": description,
                              "attendees": [{"email": email}]}]))
    return str(p)


def _gmail(tmp_path, *addresses, name="gmail.json"):
    p = tmp_path / name
    p.write_text(json.dumps([{"from": f"Someone <{a}>", "to": "me@mine.com", "subject": "hi"}
                             for a in addresses]))
    return str(p)


def _age_people(days=21):
    old = time.time() - days * 86400
    for name in os.listdir(kg.people_dir()):
        os.utime(os.path.join(kg.people_dir(), name), (old, old))


def test_retrieval_marks_legacy_fact_source_reference_unavailable():
    fact = kg.FactMeta(text="Synthetic legacy assertion", source="brief_extraction", conf=0.95)
    packed = kq._fact_text(fact, datetime(2026, 10, 1))
    assert packed == "Synthetic legacy assertion (source reference unavailable)"


def test_person_evidence_reads_persisted_fact_refs_and_observation_dates_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    ku.apply({'person_updates': [{'identifier': 'dana@example.test', 'person_name': 'Dana Rivers',
        'facts': [{'fact': 'Runs the Cedar clinic', 'source': 'observed_message', 'confidence': 0.65,
                   'source_ref': 'gmail:old-message', 'evidence_refs': ['gmail:old-message'],
                   'observed_date': '2026-09-02'}]}]}, now=datetime(2026, 10, 20))
    ku.apply({'person_updates': [{'identifier': 'dana@example.test', 'person_name': 'Dana Rivers',
        'facts': [{'fact': 'Runs the Cedar clinic', 'source': 'observed_message', 'confidence': 0.65,
                   'source_ref': 'gmail:later-message', 'evidence_refs': ['gmail:later-message'],
                   'observed_date': '2026-10-18'}]}]}, now=datetime(2026, 10, 20))
    monkeypatch.setenv('SOTTO_READ_ONLY', '1')
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '["gmail"]')
    result = _run(tmp_path, '--person', 'Dana Rivers', '--person-coverage', '--interactive')
    item, = result['fact_evidence']['items']
    assert item['text'] == 'Runs the Cedar clinic'
    assert item['fact_id'] and item['canonical_id'] in result['person_knowledge']
    assert item['source_ref'] == 'gmail:old-message'
    assert item['evidence_refs'] == ['gmail:old-message', 'gmail:later-message']
    assert (item['first_observed_date'], item['last_observed_date']) == ('2026-09-02', '2026-10-18')
    assert item['source_timestamp'] is None and 'do not establish when' in item['uncertainty']
    assert item['status'] == 'active' and item['effective_confidence'] < 1.0
    assert kq._fits_interactive(result)
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '[]')
    withheld = _run(tmp_path, '--person', 'Dana Rivers', '--person-coverage', '--interactive')
    proof, = withheld['fact_evidence']['items']
    assert proof['provenance_state'] == 'reference_unavailable'
    assert not any(field in proof for field in ('source_ref', 'evidence_refs', 'source', 'first_observed_date'))
    assert 'old-message' not in json.dumps(withheld) and 'later-message' not in json.dumps(withheld)


def test_fact_evidence_keeps_selected_conflicts_related_facts_and_bounds_complete_records(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '["gmail"]')
    monkeypatch.setattr(kq, 'FACT_EVIDENCE_BYTES', 1600)
    facts = [kg.FactMeta(text=f'Cedar assertion {index}', source='observed_message',
                         source_ref=f'gmail:{index}', evidence_refs=[f'gmail:{index}'],
                         first='2026-09-02', last='2026-09-02') for index in range(12)]
    result = {'person_knowledge': {'c_abc111': '\n'.join(f.text for f in facts)}}
    records = [('c_abc111' if i < 6 else 'c_abc222', f'f_{i}', fact, fact.text, 'c_abc111', i)
               for i, fact in enumerate(facts)]
    out = kq.attach_fact_evidence(result, records, datetime(2026, 10, 20), interactive=True,
                                  retained_units={'c_abc111': set(range(len(facts)))})
    assert out['fact_evidence']['items'] and out['fact_evidence']['omitted'] > 0
    assert len(out['fact_evidence']['items']) + out['fact_evidence']['omitted'] == len(facts)
    assert len(json.dumps(out['fact_evidence']).encode()) <= 1600
    assert out['suggestion'] == kq.NARROWER_QUERY and kq._fits_interactive(out)
    for row in out['fact_evidence']['items']:
        assert row['text'] in result['person_knowledge']['c_abc111']
        assert row['source_ref'] == row['evidence_refs'][0]


def test_fact_evidence_never_releases_partial_mixed_source_refs_or_untyped_ids(monkeypatch):
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '["gmail"]')
    for refs in (['gmail:visible', 'whatsapp:PRIVATE'], ['PRIVATE-untyped'], ['mystery:PRIVATE'],
                 ['gmail:visible', 'whatsapp:' + 'PRIVATE' * 100],
                 ['gmail:visible', {'source': 'whatsapp', 'id': 'PRIVATE'}],
                 ['gmail:visible', '']):
        fact = kg.FactMeta(text='Known graph assertion', source='observed_message',
                           source_ref=refs[0], evidence_refs=refs)
        result = kq.attach_fact_evidence({'person_knowledge': {'c_abc111': fact.text}},
            [('c_abc111', 'f_one', fact, fact.text, 'c_abc111', 1)], datetime(2026, 10, 20),
            interactive=True, retained_units={'c_abc111': {1}})
        item, = result['fact_evidence']['items']
        assert item['provenance_state'] == 'reference_unavailable'
        assert 'PRIVATE' not in json.dumps(result) and 'visible' not in json.dumps(result)
    for malformed in ('gmail:visible', {'ref': 'gmail:visible'}, ('gmail:visible',), None):
        fact = kg.FactMeta(text='Known graph assertion', source='observed_message',
                           source_ref='gmail:visible', evidence_refs=malformed)
        result = kq.attach_fact_evidence({'person_knowledge': {'c_abc111': fact.text}},
            [('c_abc111', 'f_one', fact, fact.text, 'c_abc111', 1)], datetime(2026, 10, 20),
            interactive=True, retained_units={'c_abc111': {1}})
        item, = result['fact_evidence']['items']
        assert item['provenance_state'] == 'reference_unavailable'
        assert 'visible' not in json.dumps(result)
    fact = kg.FactMeta(text='Known graph assertion', source='granola', source_ref='gmail:PRIVATE')
    result = kq.attach_fact_evidence({'person_knowledge': {'c_abc111': fact.text}},
        [('c_abc111', 'f_one', fact, fact.text, 'c_abc111', 1)], datetime(2026, 10, 20),
        interactive=True, retained_units={'c_abc111': {1}})
    assert 'PRIVATE' not in json.dumps(result)  # a permitted ref cannot authorize a revoked origin


def test_fact_evidence_accepts_exact_native_versions_only_with_all_source_grants(tmp_path, monkeypatch):
    from render_local import message_version_reference, native_message_source_ref
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    native = {source: message_version_reference({
        'source_ref': native_message_source_ref(source, 'Synthetic-A1'),
        'content_version': 'a' * 64}, source) for source in ('imessage', 'whatsapp')}
    for sources in (['imessage'], ['whatsapp'], ['imessage', 'whatsapp']):
        monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', json.dumps(sources))
        refs = [native[source] for source in sources]
        fact = kg.FactMeta(text='Native supported assertion', source='observed_message',
                           source_ref=refs[0], evidence_refs=refs,
                           first='2026-10-20', last='2026-10-20')
        person = kg.PersonFile(canonical_id='c_abc111', name='Synthetic Dana', facts={'f_one': fact})
        evidence, units = [], []
        packed = kq.pack_person(person, True, datetime(2026, 10, 26),
                                selected_evidence=evidence, units=units)
        result = kq.attach_fact_evidence({'person_knowledge': {'c_abc111': packed}}, evidence,
            datetime(2026, 10, 26), interactive=True,
            retained_units={'c_abc111': set(range(len(units)))})
        item, = result['fact_evidence']['items']
        assert item['source_ref'] == refs[0] and item['evidence_refs'] == refs
        assert item['provenance_state'] != 'reference_unavailable'
        assert item['source_timestamp'] is None


def test_native_fact_evidence_withholds_revoked_mixed_and_malformed_version_sets(monkeypatch):
    from render_local import message_version_reference, native_message_source_ref
    native = {source: message_version_reference({
        'source_ref': native_message_source_ref(source, 'Synthetic-A1'),
        'content_version': 'a' * 64}, source) for source in ('imessage', 'whatsapp')}
    base = native_message_source_ref('imessage', 'Synthetic-A1')
    cases = [([], 'observed_message', [native['imessage']]),
             (['imessage'], 'observed_message', list(native.values())),
             (['whatsapp'], 'observed_message', list(native.values())),
             (['gmail'], 'observed_message', ['gmail:allowed', native['imessage']]),
             (['imessage'], 'whatsapp', [native['imessage']])]
    for malformed in (base, base + ':v:' + 'a' * 63, base + ':v:' + 'z' * 64,
                      native['imessage'] + ':extra'):
        cases.extend((['imessage', 'whatsapp'], origin, [native['whatsapp'], malformed])
                     for origin in ('observed_message', 'imessage'))
    for sources, origin, refs in cases:
        monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', json.dumps(sources))
        fact = kg.FactMeta(text='Native supported assertion', source=origin,
                           source_ref=refs[0], evidence_refs=refs)
        result = kq.attach_fact_evidence({'person_knowledge': {'c_abc111': fact.text}},
            [('c_abc111', 'f_one', fact, fact.text, 'c_abc111', 1)], datetime(2026, 10, 26),
            interactive=True, retained_units={'c_abc111': {1}})
        item, = result['fact_evidence']['items']
        assert item['provenance_state'] == 'reference_unavailable'
        assert not any(field in item for field in ('source_ref', 'evidence_refs', 'source',
                                                   'references_omitted', 'first_observed_date'))
        assert all(ref not in json.dumps(result) for ref in refs)


def test_fact_evidence_reserves_envelope_and_final_omission_metadata_at_transport_boundary(monkeypatch):
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '["gmail"]')
    projected = kq.interactive_projection({'person_knowledge': {'c_one': 'x' * 48900}})
    assert kq._fits_interactive(projected)
    for records in ([], [('c_one', 'f_missing', kg.FactMeta(text='omitted'))] * 100):
        result = kq.attach_fact_evidence(dict(projected), records, datetime(2026, 10, 20), interactive=True)
        assert kq._fits_interactive(result)
        assert result['fact_evidence']['items'] == []
        assert result['fact_evidence']['omitted'] == len(records)


def test_fact_evidence_binds_exact_surviving_group_and_container_not_substrings(monkeypatch):
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '["gmail"]')
    fact = kg.FactMeta(text='Runs Cedar', source='observed_message', source_ref='gmail:PRIVATE')
    for packed in ({'c_one': '= Runs Cedar clinic'}, {'c_other': '= Runs Cedar'}):
        result = kq.attach_fact_evidence({'person_knowledge': packed},
            [('c_one', 'f_one', fact, '= Runs Cedar', 'c_one', 1)], datetime(2026, 10, 20),
            interactive=True, retained_units={'c_one': {1}})
        assert result['fact_evidence']['items'] == []
        assert result['fact_evidence']['omitted'] == 1
        assert 'PRIVATE' not in json.dumps(result)


def test_fact_evidence_requires_retained_group_identity_even_when_text_collides(monkeypatch):
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '["gmail"]')
    fact = kg.FactMeta(text='Runs Cedar', source='observed_message', source_ref='gmail:PRIVATE')
    # A retained note/header may contain the exact rendered text of an omitted assertion.
    result = kq.attach_fact_evidence({'person_knowledge': {'c_one': 'Header\n= Runs Cedar'}},
        [('c_one', 'f_one', fact, '= Runs Cedar', 'c_one', 2)], datetime(2026, 10, 20),
        interactive=True, retained_units={'c_one': {0, 1}})
    assert result['fact_evidence']['items'] == [] and result['fact_evidence']['omitted'] == 1
    assert 'PRIVATE' not in json.dumps(result)


def test_projection_reports_only_group_ordinals_that_survive_budget(monkeypatch):
    monkeypatch.setattr(kq, 'INTERACTIVE_BYTES', 800)
    retained = {}
    groups = ['Header', '= Kept', '= ' + 'x' * 1000]
    result = kq.interactive_projection({'person_knowledge': {'c_one': '\n'.join(groups)}},
        packed_groups={'c_one': groups}, retained_units=retained)
    assert retained == {'c_one': {0, 1}}
    assert result['person_knowledge']['c_one'] == 'Header\n= Kept'
    assert kq._fits_interactive(result)


def test_pack_person_preserves_research_selected_records_contract_and_separate_evidence_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    fact = kg.FactMeta(text='Runs Cedar', source='user_edit', conf=1.0)
    person = kg.PersonFile(canonical_id='c_abc111', name='Dana Rivers', facts={'f_one': fact})
    records, evidence, units = [], [], []
    packed = kq.pack_person(person, True, datetime(2026, 10, 20), selected_records=records,
                            selected_evidence=evidence, units=units)
    assert records == [('c_abc111', 'f_one', fact)]
    cid, fid, selected = records[0]  # Ask research snapshots unpack exactly this public hook.
    assert (cid, fid, selected) == evidence[0][:3]
    assert evidence[0][3] == units[evidence[0][5]] == '= Runs Cedar'
    assert evidence[0][4] == person.canonical_id and evidence[0][3] in packed



def test_retrieval_labels_missing_origin_without_relabelling_user_edits():
    for source in ('', ' ', None):
        fact = kg.FactMeta(text="Synthetic legacy assertion", source=source, conf=0.95)
        assert kq._fact_text(fact, datetime(2026, 10, 1)).endswith("(source reference unavailable)")
        fact.source_ref = "synthetic-message"
        assert "unavailable" not in kq._fact_text(fact, datetime(2026, 10, 1))
    uncertain = kg.FactMeta(text="Legacy guess", source="", conf=0.55)
    rendered = kq._fact_text(uncertain, datetime(2026, 10, 1))
    assert "(unverified)" in rendered and "(source reference unavailable)" in rendered
    authored = kg.FactMeta(text="Owner correction", source="user_edit", conf=1.0)
    assert "unavailable" not in kq._fact_text(authored, datetime(2026, 10, 1))


def test_empty_person_graph_keeps_historical_coverage_and_consent_distinct(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    state = tmp_path / 'config' / 'source-state.json'
    state.parent.mkdir()
    state.write_text(json.dumps({'sources': {
        'imessage': {'status': 'ok', 'read': {'status': 'ok',
            'observed_epoch': time.time(), 'has_data': False}},
        'whatsapp': {'status': 'disabled'},
        'contacts': {'status': 'ok', 'read': {'status': 'ok',
            'observed_epoch': time.time(), 'has_data': True}},
    }}))

    assert _run(tmp_path, '--person', 'Alex') == {}  # existing consumers keep their shape
    out = _run(tmp_path, '--person', '+15551234567', '--person-coverage')
    assert out['person_knowledge'] == {}
    assert out['coverage']['graph'] == 'no_match'
    sources = {row['source']: row for row in out['coverage']['historical_sources']}
    assert (sources['imessage']['status'], sources['imessage']['next_action']) == ('empty', 'check_history')
    assert sources['imessage']['reader'] == sources['whatsapp']['reader'] == 'read_history'
    assert 'limit <= 100' in sources['imessage']['scope']
    assert 'at most 3 pages' in sources['imessage']['scope']
    assert (sources['whatsapp']['status'], sources['whatsapp']['next_action']) == ('disabled', 'explain_limit')
    assert (sources['gmail']['status'], sources['gmail']['next_action']) == ('unknown', 'check_connection')
    assert sources['granola']['reader'] == 'gather_granola'
    assert '14 days of notes and 36 hours of transcripts' in sources['granola']['scope']

    by_name = _run(tmp_path, '--person', 'Alex', '--person-coverage')['coverage']
    assert by_name['identity_resolution']['next_action'] == 'search_contacts'
    assert next(row for row in by_name['historical_sources']
                if row['source'] == 'imessage')['next_action'] == 'resolve_identifier'

    receipt = json.loads(state.read_text())
    receipt['sources']['contacts'] = {'status': 'disabled'}
    state.write_text(json.dumps(receipt))
    no_contacts = _run(tmp_path, '--person', 'Alex', '--person-coverage')['coverage']
    assert no_contacts['identity_resolution']['next_action'] == 'ask_owner_for_identifier'

    # A failed Bridge read is a coverage limit, not proof that its older archive is empty.
    receipt = json.loads(state.read_text())
    receipt['sources']['imessage'] = {'status': 'unavailable'}
    state.write_text(json.dumps(receipt))
    offline = _run(tmp_path, '--person', '+15551234567', '--person-coverage')
    imessage = next(row for row in offline['coverage']['historical_sources']
                    if row['source'] == 'imessage')
    assert (imessage['status'], imessage['next_action']) == ('unavailable', 'explain_limit')


def test_saved_person_still_exposes_history_limits_without_granting_disabled_sources(monkeypatch):
    import source_context
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setattr(source_context, 'allowed', lambda source: source == 'gmail')
    monkeypatch.setattr(source_context, 'source_health', lambda: {'sources': [
        {'source': 'imessage', 'status': 'empty'},
        {'source': 'whatsapp', 'status': 'disabled'},
        {'source': 'contacts', 'status': 'disabled'},
    ]})
    coverage = kq.person_lookup_coverage(True, 'dana@example.test')
    assert coverage['graph'] == 'found'
    assert 'identity_resolution' not in coverage
    sources = {row['source']: row for row in coverage['historical_sources']}
    assert sources['imessage']['next_action'] == sources['gmail']['next_action'] == 'check_history'
    assert sources['whatsapp']['next_action'] == sources['granola']['next_action'] == 'explain_limit'


def test_managed_person_coverage_respects_connected_source_grants(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture-tenant')
    path = tmp_path / 'config' / 'managed-capabilities.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'tenant_id': 'fixture-tenant', 'sources': {
        'gmail': {'consented': True, 'connected': True},
        'granola': {'consented': False, 'connected': True},
    }}))
    out = _run(tmp_path, '--person', 'Alex', '--person-coverage')
    sources = {row['source']: row for row in out['coverage']['historical_sources']}
    assert (sources['gmail']['status'], sources['gmail']['next_action']) == ('connected', 'check_history')
    assert (sources['granola']['status'], sources['granola']['next_action']) == ('disabled', 'explain_limit')


# ── 1. company knowledge finally reaches the brief ───────────────────────────

def test_company_packs_for_a_calendar_attendees_domain(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({
        "person_updates": [{"person_name": "Sarah Chen", "identifier": "sarah@acme.com",
                            "profile_patch": {"company": "Acme Corp"},
                            "facts": [{"fact": "Runs platform", "memory_type": "context",
                                       "confidence": 0.9}]}],
        "company_updates": [{"company_name": "Acme Corp", "domain": "acme.com",
                             "about": "Acme builds developer tools.",
                             "news": [{"text": "Older thing"}, {"text": "Hired a CFO"},
                                      {"text": "Launched v2"}, {"text": "Raised a Series B"}]}],
    })
    out = _run(tmp_path, "--calendar", _cal(tmp_path, "Sarah@Acme.com"))
    ck = out["company_knowledge"]
    # ONE entry: the attendee's domain and her `company` field resolve to the same file (dedup).
    assert list(ck) == ["acme"]
    assert "Acme builds developer tools." in ck["acme"]
    # the 3 NEWEST news lines (knowledge_update stores news newest-first), and only those
    assert ck["acme"].count("\n- ") == 3
    assert "Raised a Series B" in ck["acme"] and "Older thing" not in ck["acme"]


def test_company_packs_for_a_packed_persons_employer(tmp_path, monkeypatch):
    # No calendar at all: the company comes off the person's `company` field.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({
        "person_updates": [{"person_name": "Dana Roe", "identifier": "dana@x.io",
                            "profile_patch": {"company": "Globex"},
                            "facts": [{"fact": "Runs ops", "memory_type": "context",
                                       "confidence": 0.9}]}],
        "company_updates": [{"company_name": "Globex", "about": "Globex makes turbines."}],
    })
    out = _run(tmp_path, "--gmail", _gmail(tmp_path, "dana@x.io"))
    assert "Globex makes turbines." in out["company_knowledge"]["globex"]
    # A person who does NOT pack takes their employer with them.
    out2 = _run(tmp_path, "--gmail", _gmail(tmp_path, "nobody@nowhere.com"))
    assert "company_knowledge" not in out2


def test_company_block_is_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    people, companies = [], []
    for i in range(8):
        people.append({"person_name": f"P{i}", "identifier": f"p{i}@co{i}.com",
                       "profile_patch": {"company": f"Co{i}"},
                       "facts": [{"fact": "Works", "memory_type": "context", "confidence": 0.9}]})
        companies.append({"company_name": f"Co{i}", "domain": f"co{i}.com",
                          "about": "x" * 900, "news": [{"text": "y" * 400}]})
    ku.apply({"person_updates": people, "company_updates": companies})
    out = _run(tmp_path, "--calendar", _cal(tmp_path, *[f"p{i}@co{i}.com" for i in range(8)]))
    ck = out["company_knowledge"]
    assert len(ck) == 5                                   # MAX_COMPANIES_PACKED
    assert all(len(v) <= 600 for v in ck.values())        # MAX_COMPANY_ENTRY_CHARS


def test_company_block_is_absent_when_nothing_is_on_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({"person_updates": [{"person_name": "Solo", "identifier": "solo@nowhere.com",
                                  "profile_patch": {"company": "Nowhere Inc"},
                                  "facts": [{"fact": "Exists", "memory_type": "context",
                                             "confidence": 0.9}]}]})
    assert not os.path.isdir(kg.companies_dir()) or not os.listdir(kg.companies_dir())
    out = _run(tmp_path, "--calendar", _cal(tmp_path, "solo@nowhere.com"))
    assert "company_knowledge" not in out                  # absent, never a crash
    assert out["person_knowledge"]                         # …and the people still packed


# ── 2. today's inputs, not file mtime ────────────────────────────────────────

def test_todays_email_packs_a_person_whose_file_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({"person_updates": [
        {"person_name": "Mailer", "identifier": "mailer@x.com",
         "facts": [{"fact": "Emails you", "memory_type": "context", "confidence": 0.9}]},
        {"person_name": "Quiet One", "identifier": "quiet@x.com",
         "facts": [{"fact": "Went quiet", "memory_type": "context", "confidence": 0.9}]},
    ]})
    _age_people()                       # BOTH files last rewritten three weeks ago
    out = _run(tmp_path, "--gmail", _gmail(tmp_path, "Mailer@X.com"))
    cid = {e["identifiers"][0]: e["canonical_id"] for e in out["contact_index"]}
    assert cid["mailer@x.com"] in out["person_knowledge"]    # in today's inputs → packs
    assert cid["quiet@x.com"] not in out["person_knowledge"]  # absent today → does not


def test_inputs_replace_the_mtime_cohort_but_only_when_supplied(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({"person_updates": [
        {"person_name": "Fresh File", "identifier": "fresh@x.com",
         "facts": [{"fact": "Touched today", "memory_type": "context", "confidence": 0.9}]},
        {"person_name": "Mailer", "identifier": "mailer@x.com",
         "facts": [{"fact": "Emails you", "memory_type": "context", "confidence": 0.9}]},
    ]})
    gm = _gmail(tmp_path, "mailer@x.com")
    out = _run(tmp_path, "--gmail", gm)
    cid = {e["identifiers"][0]: e["canonical_id"] for e in out["contact_index"]}
    # inputs supplied → a fresh mtime alone is NOT a reason to pack
    assert list(out["person_knowledge"]) == [cid["mailer@x.com"]]
    # no inputs → the 7-day mtime window is the fallback cohort, exactly as before
    out2 = _run(tmp_path, "--relevant-days", "7")
    assert set(out2["person_knowledge"]) == set(cid.values())
    # an EMPTY gather is "no inputs", not "nobody" — never a silently empty knowledge block
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    out3 = _run(tmp_path, "--gmail", str(empty))
    assert set(out3["person_knowledge"]) == set(cid.values())


def test_calendar_and_email_are_one_cohort(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({"person_updates": [
        {"person_name": "Meeting", "identifier": "meet@x.com",
         "facts": [{"fact": "On the calendar", "memory_type": "context", "confidence": 0.9}]},
        {"person_name": "Mailer", "identifier": "mailer@x.com",
         "facts": [{"fact": "Emails you", "memory_type": "context", "confidence": 0.9}]},
    ]})
    _age_people()
    out = _run(tmp_path, "--calendar", _cal(tmp_path, "meet@x.com"),
               "--gmail", _gmail(tmp_path, "mailer@x.com"))
    assert len(out["person_knowledge"]) == 2


def test_local_phone_contact_packs_with_unrelated_gmail(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    ku.apply({'person_updates': [{'person_name': 'Local Person', 'identifier': '+14155550199',
        'facts': [{'fact': 'Builds an acoustic instrument company', 'memory_type': 'context', 'confidence': .9}]}]})
    _age_people()
    local = tmp_path / 'local.json'
    local.write_text(json.dumps({'imessage': [{'handle': '+1 (415) 555-0199', 'text': 'Can we meet?'}]}))
    out = _run(tmp_path, '--local', str(local), '--gmail', _gmail(tmp_path, 'unrelated@example.com'))
    assert 'acoustic instrument' in '\n'.join(out['person_knowledge'].values())
    assert '4155550199' in out['memory_participants']


def test_active_loop_participant_packs_without_new_messages(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    ku.apply({'person_updates': [{'person_name': 'Waiting Person', 'identifier': 'waiting@example.com',
        'facts': [{'fact': 'Runs ocean research', 'memory_type': 'context', 'confidence': .9}]}]})
    _age_people()
    loops = tmp_path / 'loops.json'
    loops.write_text(json.dumps({'items': [{'status': 'waiting', 'contact_identifier': 'waiting@example.com'}]}))
    out = _run(tmp_path, '--loops', str(loops), '--gmail', _gmail(tmp_path, 'unrelated@example.com'))
    assert 'ocean research' in '\n'.join(out['person_knowledge'].values())


def test_calendar_topic_keeps_older_relevant_memory_inside_compact_cap(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    now = datetime(2026, 9, 7, 12, 0, 0)
    facts = {
        f'f_recent_{i}': kg.FactMeta(text=f'Recent unrelated operating note number {i}',
            type='context', conf=.95, first='2026-09-06', last='2026-09-06')
        for i in range(8)
    }
    facts['f_topic'] = kg.FactMeta(text='Previously evaluated pediatric clinic procurement pilots',
        type='context', conf=.7, first='2026-01-01', last='2026-01-01')
    facts['f_owner'] = kg.FactMeta(text='User corrected: Alex advises this company',
        type='context', conf=1.0, source='user_edit', first='2026-02-01', last='2026-02-01')
    facts['f_archived'] = kg.FactMeta(text='Pediatric clinic claim that was corrected',
        type='context', conf=1.0, status='archived', first='2026-08-01', last='2026-08-01')
    p = kg.PersonFile(canonical_id='c_123456789abc', name='Alex',
                      identifiers=['alex@example.com'], facts=facts)
    kg.write_person_file(os.path.join(kg.people_dir(), 'c_123456789abc.md'), p, now)

    ordinary = _run(tmp_path, '--calendar', _cal(tmp_path, 'alex@example.com'))
    ordinary_text = next(iter(ordinary['person_knowledge'].values()))
    assert 'pediatric clinic procurement' not in ordinary_text

    focused = _run(tmp_path, '--calendar', _topic_cal(
        tmp_path, 'alex@example.com', 'Pediatric clinic procurement', 'Review pilot rollout'))
    focused_text = next(iter(focused['person_knowledge'].values()))
    assert 'pediatric clinic procurement pilots' in focused_text
    assert 'User corrected: Alex advises this company' in focused_text
    assert 'claim that was corrected' not in focused_text


def test_editable_person_exposes_bounded_active_ids_for_exact_correction(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    ku.apply({'person_updates': [{'person_name': 'Peyton Lewis', 'identifier': 'peyton@example.com',
        'facts': [{'fact': 'Peyton founded Alive.', 'memory_type': 'context', 'confidence': .9}]}]})
    person = _run(tmp_path, '--person', 'peyton@example.com', '--editable-person')
    row = next(iter(person.values()))
    assert row['canonical_id'].startswith('c_')
    assert len(row['facts']) == 1
    assert row['facts'][0]['id'].startswith('f_')
    assert row['facts'][0]['text'] == 'Peyton founded Alive.'


def test_topic_finds_old_wrong_fact_then_exact_correction_replaces_retrieved_memory(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    now = datetime(2026, 9, 7, 12, 0, 0)
    facts = {
        f'f_recent_{i}': kg.FactMeta(text=f'Recent unrelated portfolio update number {i}',
            type='context', conf=.95, first='2026-09-06', last='2026-09-06')
        for i in range(20)
    }
    facts['f_wrong'] = kg.FactMeta(text='Peyton founded Alive.', type='context', conf=.7,
        first='2025-01-01', last='2025-01-01')
    p = kg.PersonFile(canonical_id='c_abcdef123456', name='Peyton Lewis',
                      identifiers=['peyton@example.com'], facts=facts)
    kg.write_person_file(os.path.join(kg.people_dir(), 'c_abcdef123456.md'), p, now)

    ordinary = next(iter(_run(tmp_path, '--person', 'peyton@example.com',
                              '--editable-person').values()))
    assert all(fact['id'] != 'f_wrong' for fact in ordinary['facts'])
    focused = next(iter(_run(tmp_path, '--person', 'peyton@example.com', '--editable-person',
                             '--topic', 'founder Alive').values()))
    assert focused['facts'][0] == {'id': 'f_wrong', 'text': 'Peyton founded Alive.',
        'fact_version': kg.person_fact_version(p.canonical_id,'f_wrong',p.facts['f_wrong'])}

    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    corrected = subprocess.run([sys.executable, EDIT, '--slug', 'c_abcdef123456', '--op', 'correct',
        '--fact-id', 'f_wrong', '--text', 'Peyton serves as chief operating officer at Alive.'],
        capture_output=True, text=True, env=env)
    assert corrected.returncode == 0, corrected.stderr
    retrieved = _run(tmp_path, '--person', 'peyton@example.com', '--topic', 'Alive leadership')
    packed = next(iter(retrieved.values()))
    assert 'chief operating officer at Alive' in packed
    assert 'Peyton founded Alive' not in packed
    active = next(iter(_run(tmp_path, '--person', 'peyton@example.com', '--editable-person',
                            '--topic', 'Alive leadership').values()))
    assert all(fact['id'] != 'f_wrong' for fact in active['facts'])


def _person(tmp_path, *, cid='c_123456789abc', name='Alex', facts=None, **fields):
    now = datetime(2026, 9, 20)
    person = kg.PersonFile(canonical_id=cid, name=name,
        identifiers=fields.pop('identifiers', ['alex@example.com', '+14155550199']),
        facts=facts or {}, **fields)
    kg.write_person_file(os.path.join(kg.people_dir(), cid + '.md'), person, now)
    return person


def _fact(text, **kw):
    return kg.FactMeta(text=text, first='2026-09-20', last='2026-09-20', conf=.95, **kw)


def test_recent_burst_and_summary_share_one_fact_budget(tmp_path, monkeypatch):
    import knowledge_query as query
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    facts = {f'f_{i}': _fact(f'Fresh portfolio observation {i}.') for i in range(100)}
    facts['f_copy'] = _fact('Fresh portfolio observation 0.')
    facts['f_owner'] = _fact('Owner corrected the role to adviser.', source='user_edit')
    p = _person(tmp_path, facts=facts, summary_refs=['f_0', 'f_1', 'f_copy'])
    packed = query.pack_person(p, False, datetime(2026, 9, 20))
    assert packed.count('= ') == 5
    assert packed.count('Fresh portfolio observation 0.') == 1
    assert packed.index('Owner corrected') < packed.index('Fresh portfolio')
    # A compact read is a summary by design: the fact allowance is not an omission to announce
    # to the brief's model, which cannot narrow anything. The expanded read says so.
    assert query.OMITTED_CONTEXT not in packed
    assert query.OMITTED_CONTEXT in query.pack_person(p, True, datetime(2026, 9, 20))
    assert len(packed) <= query.MAX_COMPACT_CHARS
    assert len(p.facts) == 102  # A read budget never deletes the underlying memory.


def test_an_ordinary_compact_block_carries_no_omission_notice(tmp_path, monkeypatch):
    import knowledge_query as query
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    p = _person(tmp_path, facts={f'f_{i}': _fact(f'Fact {i} about a project the person runs.') for i in range(7)})
    packed = query.pack_person(p, False, datetime(2026, 9, 20))
    assert packed.count('= ') == 5 and query.OMITTED_CONTEXT not in packed


def test_char_budget_keeps_whole_assertions_and_bounds_legacy_context(tmp_path, monkeypatch):
    import knowledge_query as query
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    oversized = 'A very long legacy assertion ' + 'details ' * 2000 + 'but not approved.'
    p = _person(tmp_path, facts={'f_long': _fact(oversized), 'f_small': _fact('Runs acoustic research.')},
                talking_points=['legacy ' * 2000] * 20, recent_activity=['activity ' * 2000] * 20)
    for expanded, cap in ((False, query.MAX_COMPACT_CHARS), (True, query.MAX_EXPANDED_CHARS)):
        packed = query.pack_person(p, expanded, datetime(2026, 9, 20))
        assert len(packed) <= cap
        assert 'A very long legacy assertion' not in packed  # Never amputate the qualifier.
        assert 'Runs acoustic research.' in packed
        assert query.OMITTED_CONTEXT in packed


def _crowded_memory(tmp_path):
    facts = {f'f_new_{i}': _fact(f'Unrelated portfolio observation {i}.') for i in range(20)}
    facts['f_clinic'] = kg.FactMeta(text='Pediatric clinic procurement requires a six-month review.',
                                  conf=.65, first='2025-01-01', last='2025-01-01')
    return _person(tmp_path, facts=facts)


def test_email_and_phone_topics_recall_old_facts_without_calendar(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    p = _crowded_memory(tmp_path)
    assert 'six-month review' not in _run(tmp_path, '--person', 'alex@example.com')[p.canonical_id]
    email = tmp_path / 'mail.json'
    email.write_text(json.dumps([{'from': 'Alex <ALEX@example.com>',
                                 'subject': 'Pediatric clinic procurement'}]))
    local = tmp_path / 'local.json'
    local.write_text(json.dumps({'imessage': [{'handle': '+1 (415) 555-0199',
                                              'text': 'Pediatric clinic procurement'}]}))
    for args in (('--gmail', str(email)), ('--local', str(local))):
        packed = _run(tmp_path, *args)['person_knowledge'][p.canonical_id]
        assert 'six-month review' in packed
        assert packed.count('= ') == 5


def test_topic_projection_uses_exact_aliases_and_respects_revocation(tmp_path, monkeypatch):
    import personal_context as pc
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    _crowded_memory(tmp_path)
    inputs = dict(local={'imessage': [
        {'handle': '+1 (415) 555-0199', 'text': 'Pediatric clinic procurement', 'name': 'Alex'},
        {'handle': '+14155550198', 'text': 'Industrial turbines', 'name': 'Alex'},
        {'handle': '+14155550199', 'text': 'Group-only phrase', 'is_group_chat': True}]})
    topics = pc.participant_topics(**inputs)
    assert topics['4155550199'] == 'Pediatric clinic procurement'
    assert topics['4155550198'] == 'Industrial turbines'
    from source_context import record_bridge_status
    record_bridge_status({'source_status': {'imessage': 'disabled'}})
    assert pc.participant_topics(**inputs) == {}
    assert pc.participant_identifiers(**inputs) == set()
    # Disconnect stops new context, not explicit memory queries or owner-managed durable facts.
    explicit = _run(tmp_path, '--person', '+14155550199', '--topic', 'Pediatric clinic procurement')
    assert 'six-month review' in next(iter(explicit.values()))


def test_topic_projection_is_bounded_and_gmail_revocation_is_respected(tmp_path, monkeypatch):
    import personal_context as pc
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    gmail = [{'from': 'alex@example.com', 'subject': 'Pediatric clinic procurement ' + str(i) * 2000}
             for i in range(100)]
    assert len(pc.participant_topics(gmail=gmail)['alex@example.com']) <= pc.TOPIC_CHARS
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/managed-capabilities.json').write_text(json.dumps({
        'tenant_id': 'test', 'sources': {'gmail': {'consented': False, 'connected': True}}}))
    assert pc.participant_topics(gmail=gmail) == {}
    assert pc.participant_identifiers(gmail=gmail) == set()


def test_one_hop_retrieval_is_attributed_bounded_and_does_not_expand_active_cohort(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    relation = kg.Relation(type='introduced_by', slug='c_222222222222', name='Jo')
    p = _crowded_memory(tmp_path)
    p.relations = [relation, kg.Relation(type='works_with', slug='../../elsewhere', name='Impostor')]
    kg.write_person_file(os.path.join(kg.people_dir(), p.canonical_id + '.md'), p)
    _person(tmp_path, cid=relation.slug, name='Jo', identifiers=['jo@example.com'],
        facts={'f_intro': _fact('Discussed pediatric clinic procurement safeguards.'),
               'f_private': _fact('Unrelated holiday planning.')},
        relations=[kg.Relation(type='works_with', slug='c_333333333333', name='Pat')])
    _person(tmp_path, cid='c_333333333333', name='Pat', identifiers=['pat@example.com'],
        facts={'f_two': _fact('Pediatric clinic procurement second-hop detail.')})
    cal = _topic_cal(tmp_path, 'alex@example.com', 'Pediatric clinic procurement', '')
    out = _run(tmp_path, '--calendar', cal)
    packed = out['person_knowledge'][p.canonical_id]
    assert 'Related context about Jo (c_222222222222), introduced_by:' in packed
    assert 'procurement safeguards' in packed
    assert 'holiday planning' not in packed and 'second-hop detail' not in packed
    assert len(out['person_knowledge']) == 1
    assert 'jo@example.com' not in out['memory_participants']
    assert packed.count('= ') + packed.count('Related context about') == 5
    ordinary = _run(tmp_path, '--person', 'alex@example.com')[p.canonical_id]
    assert 'procurement safeguards' not in ordinary


def test_related_name_does_not_substitute_for_a_missing_canonical_file(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    p = _person(tmp_path, relations=[kg.Relation(type='introduced_by', slug='c_444444444444', name='Jo')])
    _person(tmp_path, cid='c_222222222222', name='Jo', identifiers=['jo@example.com'],
            facts={'f_one': _fact('Pediatric clinic procurement safeguards.')})
    packed = _run(tmp_path, '--person', 'alex@example.com', '--topic', 'clinic procurement')[p.canonical_id]
    assert 'procurement safeguards' not in packed


def test_live_conflict_is_atomic_deduplicated_and_disappears_after_correction(tmp_path, monkeypatch):
    import knowledge_edit as edit
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    p = _person(tmp_path, facts={'f_founder': _fact('Alex founded Acme.'),
                               'f_adviser': _fact('Alex advises Acme.')}, summary_refs=['f_founder'])
    (tmp_path / 'knowledge/conflicts.json').write_text(json.dumps({'people': {
        p.canonical_id: {'pairs': [['f_founder', 'f_adviser']]}}}))
    packed = _run(tmp_path, '--person', 'alex@example.com')[p.canonical_id]
    assert 'Unresolved memory conflict' in packed
    assert packed.count('Alex founded Acme.') == 1
    assert packed.count('Alex advises Acme.') == 1
    edit.op_correct(p.canonical_id, 'f_founder', 'Alex advises Acme.')
    packed = _run(tmp_path, '--person', 'alex@example.com')[p.canonical_id]
    assert 'Unresolved memory conflict' not in packed and 'founded Acme' not in packed
    assert packed.count('Alex advises Acme.') == 1


def test_archived_indirect_fact_cannot_return_through_summary_or_related_context(tmp_path, monkeypatch):
    import knowledge_edit as edit
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    _person(tmp_path, relations=[kg.Relation(type='introduced_by', slug='c_222222222222', name='Jo')])
    _person(tmp_path, cid='c_222222222222', name='Jo', identifiers=['jo@example.com'],
            facts={'f_old': _fact('Pediatric clinic procurement safeguards.')}, summary_refs=['f_old'])
    args = ('--person', 'alex@example.com', '--topic', 'Pediatric clinic procurement')
    assert 'procurement safeguards' in next(iter(_run(tmp_path, *args).values()))
    edit.op_archive('c_222222222222', 'f_old')
    assert 'procurement safeguards' not in next(iter(_run(tmp_path, *args).values()))
    assert 'procurement safeguards' not in next(iter(_run(tmp_path, '--person', 'jo@example.com').values()))


def test_corrected_memory_reaches_chat_brief_prep_and_notification_context(tmp_path, monkeypatch):
    import compose_brief as brief
    import compose_meeting_prep as prep
    import compose_notification as notification
    import knowledge_edit as edit
    from datetime import timezone
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    p = _crowded_memory(tmp_path)
    edit.op_correct(p.canonical_id, 'f_clinic', 'Pediatric clinic procurement requires board approval, not a fixed timeline.')
    want = 'requires board approval, not a fixed timeline'
    old = 'requires a six-month review'
    topic = 'Pediatric clinic procurement'
    chat = _run(tmp_path, '--person', '+1 (415) 555-0199', '--topic', topic)
    event = {'summary': topic, 'attendees': [{'email': 'alex@example.com', 'displayName': 'Alex'}]}
    cal = tmp_path / 'cal.json'
    cal.write_text(json.dumps([event]))
    context = _run(tmp_path, '--calendar', str(cal))
    inputs = {'type': 'morning', 'local': {}, 'google': {'userEmail': 'owner@elsewhere.com',
              'events': [event]}, 'prior_knowledge': context}
    brief_prompt = brief.build_prompt(brief._load_prompt(), inputs)
    prep_prompt, meetings = prep.build_prompt(prep._load_prompt(), inputs)
    assert meetings
    item = {'kind': 'actionable', 'person': 'Alex', 'identifier': 'alex@example.com',
            'event': {'subject': topic}, 'channel': 'gmail'}
    notification_context = notification.enrich([item], datetime.now(timezone.utc))[0]['person_facts']
    for value in (json.dumps(chat), brief_prompt, prep_prompt, json.dumps(notification_context)):
        assert want in value and old not in value


def test_resolved_work_does_not_become_a_current_topic(tmp_path, monkeypatch):
    import personal_context as pc
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    loops = [{'status': 'resolved', 'canonical_id': 'c_123456789abc',
              'contact_identifier': 'alex@example.com', 'summary': 'Send the pediatric clinic deck'}]
    assert pc.participant_identifiers(loops=loops) == set()
    assert pc.participant_topics(loops=loops) == {}
    loops[0]['status'] = 'waiting'
    assert pc.participant_topics(loops=loops)['alex@example.com'] == 'Send the pediatric clinic deck'


def test_conflicted_related_memory_is_not_silently_cherry_picked(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    _person(tmp_path, relations=[kg.Relation(type='works_with', slug='c_222222222222', name='Jo')])
    _person(tmp_path, cid='c_222222222222', name='Jo', identifiers=['jo@example.com'],
            facts={'f_a': _fact('Clinic procurement is approved.'), 'f_b': _fact('Clinic procurement is not approved.')})
    (tmp_path / 'knowledge/conflicts.json').write_text(json.dumps({'people': {
        'c_222222222222': {'pairs': [['f_a', 'f_b']]}}}))
    packed = next(iter(_run(tmp_path, '--person', 'alex@example.com', '--topic', 'clinic procurement').values()))
    assert 'procurement is approved' not in packed and 'procurement is not approved' not in packed


def test_malformed_optional_records_cannot_break_primary_retrieval(tmp_path, monkeypatch):
    import knowledge_query as query
    import personal_context as context
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    p = _person(tmp_path, facts={'f_one': _fact('Runs clinic procurement.')},
                relations=[kg.Relation(type='works_with', slug='c_222222222222', name='Jo')])
    (tmp_path / 'knowledge/people/c_222222222222.md').write_text('---\n- malformed YAML shape\n---\n')
    packed = query.pack_person(p, False, datetime(2026, 9, 20), 'clinic procurement')
    assert 'Runs clinic procurement' in packed
    inputs = dict(loops=[None, 'broken', {'status': 'waiting', 'contact_identifier': 'alex@example.com',
                                        'summary': 'Clinic procurement'}],
                  gmail=[None, 'broken'], calendar=[None, 'broken'])
    assert context.participant_identifiers(**inputs) == {'alex@example.com'}
    assert context.participant_topics(**inputs) == {'alex@example.com': 'Clinic procurement'}


def test_history_captured_authority_malformed_shapes_never_use_ambient_grants(monkeypatch):
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda _source: (_ for _ in ()).throw(AssertionError('ambient grant read')))
    for captured in ('{}', '"gmail"', '["gmail", null]', '["gmail", 1]', '["gmail", {}]', '['):
        monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', captured)
        result = kq._person_obligation_history(None, [], 'alex@example.com', '', 'no_match', interactive=True)
        assert result['status'] == 'unavailable' and result['items'] == []
        assert result['absence_is_authoritative'] is False


def test_malformed_person_identity_stays_unavailable_without_crashing_or_choosing_an_alias(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    _person(tmp_path)
    directory = tmp_path/'knowledge/people'
    for malformed in ('---\ncanonical_id: c_999999999999\nname: null\nidentifiers: []\n---\n',
                      '---\n- malformed YAML shape\n---\n'):
        (directory/'unreadable.md').write_text(malformed)
        for selector in ('Alex', 'alex@example.com', 'c_123456789abc'):
            assert kq._unique_person(selector) == (None, [], 'unavailable')


def test_related_limits_and_loaded_identity_are_enforced(tmp_path, monkeypatch):
    import knowledge_query as query
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    relations = []
    for i in range(1, 7):
        cid = f'c_{i:012x}'
        relations.append(kg.Relation(type='works_with', slug=cid, name=f'Colleague {i}'))
        _person(tmp_path, cid=cid, name=f'Colleague {i}', identifiers=[f'c{i}@example.com'],
                facts={f'f_{n}': _fact(f'Clinic procurement colleague {i} observation {n}.') for n in range(4)})
    p = _person(tmp_path, relations=relations)
    packed = query.pack_person(p, True, datetime(2026, 9, 20), 'clinic procurement')
    assert packed.count('Related context about') == 4  # Two people, two facts each.
    assert 'colleague 3 observation' not in packed and 'colleague 6 observation' not in packed
    p.relations = [kg.Relation(type='', slug='c_000000000001', name='Colleague 1')]
    assert 'colleague 1 observation' not in query.pack_person(p, True, datetime(2026, 9, 20), 'clinic procurement')
    p.relations[0].type = 'works_with'
    path = tmp_path / 'knowledge/people/c_000000000001.md'
    path.write_text(path.read_text().replace('canonical_id: c_000000000001', 'canonical_id: c_111111111111'))
    assert 'colleague 1 observation' not in query.pack_person(p, True, datetime(2026, 9, 20), 'clinic procurement')


def test_filler_question_does_not_expand_related_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    _person(tmp_path, relations=[kg.Relation(type='introduced_by', slug='c_222222222222', name='Jo')])
    _person(tmp_path, cid='c_222222222222', name='Jo', identifiers=['jo@example.com'],
            facts={'f_one': _fact('She is the lead for compliance and procurement.')})
    packed = next(iter(_run(tmp_path, '--person', 'alex@example.com',
                           '--topic', 'Can you please tell me about them and what you know?').values()))
    assert 'lead for compliance' not in packed


def test_legacy_notes_are_not_cut_before_a_qualifier(tmp_path, monkeypatch):
    import knowledge_query as query
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    p = _person(tmp_path, notes='The procurement plan has been approved ' + 'detail ' * 100 + 'only if the board agrees.')
    packed = query.pack_person(p, True, datetime(2026, 9, 20))
    assert 'procurement plan has been approved' not in packed
    assert query.OMITTED_CONTEXT in packed
    # …but a note made of several sentences keeps the complete ones that fit, and says the rest is out
    p = _person(tmp_path, notes='Met at the summit in May. Prefers email over calls. '
                + 'The procurement plan has been approved ' + 'detail ' * 100 + 'only if the board agrees.')
    packed = query.pack_person(p, True, datetime(2026, 9, 20))
    assert '# Met at the summit in May. Prefers email over calls. …' in packed
    assert 'procurement plan has been approved' not in packed
    assert query.OMITTED_CONTEXT in packed


def test_legacy_note_line_wrap_cannot_drop_its_qualifier(tmp_path, monkeypatch):
    import knowledge_query as query
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    p = _person(tmp_path, notes='Prefers email.\nThe procurement plan is approved\nonly if '
                + 'the conditions hold ' * 30 + 'and the board agrees.')
    packed = query.pack_person(p, True, datetime(2026, 9, 20))
    assert '# Prefers email. …' in packed
    assert 'procurement plan is approved' not in packed
    assert query.OMITTED_CONTEXT in packed


def test_legacy_note_suffix_shares_the_excerpt_budget(tmp_path, monkeypatch):
    import knowledge_query as query
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    for sentence_size in (kg.NOTES_EXCERPT_CHARS - 2, kg.NOTES_EXCERPT_CHARS - 1):
        first_sentence = 'A' * (sentence_size - 1) + '.'
        p = _person(tmp_path, notes=first_sentence + ' ' + 'More context. ' * 30)
        packed = query.pack_person(p, True, datetime(2026, 9, 20))
        note_lines = [line[2:] for line in packed.splitlines() if line.startswith('# ')]
        assert all(len(line) <= kg.NOTES_EXCERPT_CHARS for line in note_lines)
        assert note_lines == ([first_sentence + ' …'] if sentence_size + 2 <= kg.NOTES_EXCERPT_CHARS else [])
        assert query.OMITTED_CONTEXT in packed


def test_omitted_relation_notice_survives_an_active_conflict(tmp_path, monkeypatch):
    import knowledge_query as query
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    p = _person(tmp_path, facts={'f_a': _fact('Funding is approved.'), 'f_b': _fact('Funding is not approved.')},
                relations=[kg.Relation(type='works_with', slug='c_222222222222', name='Long ' * 1000)])
    (tmp_path / 'knowledge/conflicts.json').write_text(json.dumps({'people': {
        p.canonical_id: {'pairs': [['f_a', 'f_b']]}}}))
    packed = query.pack_person(p, False, datetime(2026, 9, 20))
    assert 'Unresolved memory conflict' in packed
    assert query.OMITTED_CONTEXT in packed


def test_interactive_projection_preserves_multiline_conflict_as_one_unit(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(kq, 'INTERACTIVE_BYTES', 350)
    p = _person(tmp_path, facts={
        'f_a': _fact('The contract is approved.\nOnly after audit.'),
        'f_b': _fact('The contract is pending.\nUntil audit finishes.')})
    (tmp_path / 'knowledge/conflicts.json').write_text(json.dumps({'people': {
        p.canonical_id: {'pairs': [['f_a', 'f_b']]}}}))
    units, weights = [], []
    packed = kq.pack_person(p, False, datetime(2026, 9, 20),
                            units=units, unit_weights=weights)
    assert any('Unresolved memory conflict' in unit and 'Only after audit.' in unit
               and 'Until audit finishes.' in unit for unit in units)
    value = {'person_knowledge': {p.canonical_id: packed}, 'contact_index': [], 'memory_participants': []}
    result = kq.interactive_projection(value, packed_groups={p.canonical_id: units},
                                       packed_weights={p.canonical_id: weights})
    text = result['person_knowledge'][p.canonical_id]
    assert 'Unresolved memory conflict' not in text
    assert result['omitted']['assertions'] == 2
    assert ('The contract is approved.' in text) == ('Only after audit.' in text)
    assert ('The contract is approved.' in text) == ('The contract is pending.' in text)
    assert len(json.dumps(result).encode()) <= 350


def test_interactive_projection_budgets_unicode_and_whole_huge_assertions(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    p = _person(tmp_path, facts={'f_large': _fact('条件' * 10000 + ' only if approved.'),
                                 'f_small': _fact('Works on clinics.')})
    units, stats = [], {}
    packed = kq.pack_person(p, True, datetime(2026, 9, 20), units=units, stats=stats)
    result = kq.interactive_projection({p.canonical_id: packed},
        packed_groups={p.canonical_id: units}, packed_omitted={p.canonical_id: stats['assertions']})
    assert 'only if approved.' not in result[p.canonical_id]
    assert 'Works on clinics.' in result[p.canonical_id]
    assert result['omitted']['assertions'] >= 1
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 48 * 1024
    assert len(json.dumps(result).encode()) <= 48 * 1024


def test_interactive_final_budget_drop_counts_all_retained_assertions(monkeypatch):
    key = 'person'
    units = ['Person (person)', '= First assertion.', '= Second assertion.']
    value = {'person_knowledge': {key: '\n'.join(units)},
             'contact_index': [], 'memory_participants': []}
    options = {'packed_groups': {key: units}, 'packed_weights': {key: [0, 1, 1]},
               'unselected_people': 1}
    full = kq.interactive_projection(value, **options)
    assert key in full['person_knowledge']
    before_reason = json.loads(json.dumps(full))
    before_reason['omitted']['reason'] = None
    monkeypatch.setattr(kq, 'INTERACTIVE_BYTES', len(json.dumps(before_reason).encode()) + 1)
    result = kq.interactive_projection(value, **options)
    assert key not in result['person_knowledge']
    assert result['omitted']['assertions'] == 2
    assert len(json.dumps(result).encode()) <= kq.INTERACTIVE_BYTES


def test_interactive_oversize_company_keeps_closed_result_shape():
    result = kq.interactive_projection({'status': 'saved',
        'company_knowledge': {'company': '条' * 20000}, 'truncated': False})
    assert result == {'status': 'unavailable', 'company_knowledge': {}, 'truncated': False}


def test_current_mail_topic_survives_busy_phone_alias_and_standing_work(tmp_path, monkeypatch):
    import personal_context as pc
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    _crowded_memory(tmp_path)
    gmail = [{'from': 'alex@example.com', 'subject': 'Pediatric clinic procurement'}]
    loops = [{'status': 'waiting', 'contact_identifier': 'alex@example.com',
              'summary': 'Unrelated project ' + str(i) * 1000} for i in range(10)]
    local = {'imessage': [{'handle': '+14155550199', 'text': 'Unrelated project ' + str(i) * 1000}
                         for i in range(10)]}
    topics = pc.participant_topics(local=local, gmail=gmail, loops=loops)
    joined = pc.topic_for_identifiers(topics, ['alex@example.com', '+1 (415) 555-0199'])
    assert 'Pediatric clinic procurement' in joined
    assert len(joined) <= pc.TOPIC_CHARS
    for filename, value in [('mail.json', gmail), ('local.json', local), ('loops.json', loops)]:
        (tmp_path / filename).write_text(json.dumps(value))
    out = _run(tmp_path, '--gmail', str(tmp_path / 'mail.json'), '--local', str(tmp_path / 'local.json'),
               '--loops', str(tmp_path / 'loops.json'))
    assert 'six-month review' in next(iter(out['person_knowledge'].values()))


def test_topic_budget_follows_work_cohort_even_during_chat_burst(tmp_path, monkeypatch):
    import personal_context as pc
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    loops = [{'status': 'waiting', 'identifier': f'person{i}@example.com', 'ask': f'Clinic procurement {i}'}
             for i in range(pc.PARTICIPANT_LIMIT)]
    local = {'imessage': [{'handle': f'+1415555{i:04d}', 'text': 'Unrelated'} for i in range(100)]}
    topics = pc.participant_topics(local=local, loops=loops)
    assert len(topics) == pc.PARTICIPANT_LIMIT
    assert all(key.startswith('person') for key in topics)


def test_person_context_carries_measured_history_gaps_without_source_content(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture-tenant')
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'managed-capabilities.json').write_text(json.dumps({
        'tenant_id': 'fixture-tenant', 'sources': {
            'gmail': {'consented': True, 'connected': True},
            'whatsapp': {'consented': False, 'connected': True}}}))
    ku.apply({'person_updates': [{'person_name': 'Dana Reed', 'identifier': 'dana@example.test',
                                 'facts': [{'fact': 'Runs Cedar', 'confidence': 0.9}]}]})
    path = tmp_path / 'knowledge/history-state.json'
    window = {'since': 1791936000, 'until': 1792022400, 'complete': True,
              'initial_complete': True, 'pages': 3, 'window_page_base': 2,
              'cursor': 'SECRET-CURSOR', 'error': 'PRIVATE-MESSAGE',
              'observation_totals': dict(pages=1, rows=20, eligible=20, selected=10,
                                        skipped=0, omitted=10, truncated=2, rejected_people=0)}
    path.write_text(json.dumps({'sources': {'gmail': window, 'whatsapp': window}}))
    out = _run(tmp_path, '--person', 'dana@example.test', '--person-coverage', '--interactive')
    assert out['coverage']['graph'] == 'found'
    sources = {row['source']: row for row in out['coverage']['historical_sources']}
    learned = sources['gmail']['learning']
    assert learned['checkpoint'] == 'present'
    assert learned['review_scope'] == 'partial'
    assert learned['outside_window_review'] == 'unknown'
    assert learned['initial_traversal_complete'] is True
    assert learned['observation_counts']['omitted'] == 10
    assert sources['whatsapp']['learning'] == {'checkpoint': 'withheld'}
    assert 'SECRET' not in json.dumps(out) and 'PRIVATE' not in json.dumps(out)
    assert 'not person- or topic-specific completeness' in out['coverage']['learning_scope']
    assert sources['gmail']['next_action'] == 'check_history'

    # Legacy checkpoint completion cannot manufacture measured coverage.
    path.write_text(json.dumps({'sources': {'gmail': {'complete': True}}}))
    legacy = _run(tmp_path, '--person', 'dana@example.test', '--person-coverage')
    gmail = next(row for row in legacy['coverage']['historical_sources'] if row['source'] == 'gmail')
    assert gmail['learning']['review_scope'] == 'unknown'
    path.write_text('{broken')
    broken = _run(tmp_path, '--person', 'dana@example.test', '--person-coverage')
    gmail = next(row for row in broken['coverage']['historical_sources'] if row['source'] == 'gmail')
    assert gmail['learning'] == {'checkpoint': 'unknown'}


def test_saved_biography_does_not_require_an_address_book_read(monkeypatch):
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda source: True)
    monkeypatch.setattr(source_context, 'source_health', lambda: {'sources': [
        {'source': 'contacts', 'status': 'ok'}, {'source': 'imessage', 'status': 'ok'}]})
    coverage = kq.person_lookup_coverage(True, 'Dana Reed')
    assert 'identity_resolution' not in coverage
    assert coverage['historical_sources']  # outcome questions still have bounded retrieval guidance


def test_saved_identifier_does_not_request_contacts_again(monkeypatch):
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda source: True)
    monkeypatch.setattr(source_context, 'source_health', lambda: {'sources': [
        {'source': 'contacts', 'status': 'ok'}, {'source': 'imessage', 'status': 'ok'}]})
    for identifiers, expected in [(['dana@example.test'], 'check_history'),
                                  (['+15555550123'], 'check_history'),
                                  ([], 'resolve_identifier'), (['Dana Reed'], 'resolve_identifier')]:
        coverage = kq.person_lookup_coverage(True, 'Dana Reed', identifiers)
        assert next(row for row in coverage['historical_sources'] if row['source'] == 'imessage')['next_action'] == expected


def test_history_identity_failures_never_authorize_absence_inference():
    for identity_status in ('ambiguous','unavailable'):
        result=kq._person_obligation_history(None,[],'Alex','',identity_status,interactive=True)
        assert result['absence_is_authoritative'] is False
        assert result['items']==[] and result['omitted']==0


def test_partial_person_lookup_points_to_saved_identity_discovery_without_selecting_history(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    people = tmp_path / 'knowledge/people'
    people.mkdir(parents=True)
    for cid, name, email in [('c_abc111', 'Dana Rivers', 'dana.rivers@example.test'),
                             ('c_abc222', 'Dana Woods', 'dana.woods@example.test')]:
        kg.write_person_file(str(people / f'{cid}.md'), kg.PersonFile(
            canonical_id=cid, name=name, identifiers=[email]))
    unresolved = _run(tmp_path, '--person', 'Dana', '--person-coverage', '--interactive')
    assert unresolved['person_knowledge'] == {}
    assert unresolved['obligation_history']['items'] == []
    discovery = unresolved['coverage']['identity_resolution']['saved_identity_discovery']
    assert discovery['reader'] == 'knowledge-query' and discovery['argv'] == []
    assert 'bounded' in discovery['scope'] and 'not a partial-name match' in discovery['scope']
    assert 'absence' in discovery['scope']
    candidates = _run(tmp_path, '--interactive')['contact_index']
    assert {row['canonical_id'] for row in candidates} == {'c_abc111', 'c_abc222'}
    exact = _run(tmp_path, '--person', 'Dana Rivers', '--person-coverage', '--interactive')
    assert exact['coverage']['graph'] == 'found'
    assert 'identity_resolution' not in exact['coverage']
    identifier = _run(tmp_path, '--person', 'nobody@example.test', '--person-coverage')
    assert 'saved_identity_discovery' not in identifier['coverage']['identity_resolution']


def test_ambiguous_exact_name_does_not_offer_candidate_as_resolved_person(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    people = tmp_path / 'knowledge/people'
    people.mkdir(parents=True)
    for cid in ['c_abc111', 'c_abc222']:
        kg.write_person_file(str(people / f'{cid}.md'), kg.PersonFile(
            canonical_id=cid, name='Dana Rivers', identifiers=[cid + '@example.test']))
    result = _run(tmp_path, '--person', 'Dana Rivers', '--person-coverage', '--interactive')
    assert result['coverage']['identity_resolution'] == {
        'status': 'ambiguous', 'next_action': 'ask_owner_for_identifier'}
    assert result['person_knowledge'] == {} and result['obligation_history']['items'] == []


def test_recent_identity_discovery_can_omit_old_person_without_blocking_exact_history(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_READ_ONLY', '1')
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '["gmail"]')
    people = tmp_path / 'knowledge/people'
    people.mkdir(parents=True)
    identities = [('c_abc111', 'Older Person', 'older@example.test'),
                  ('c_abc222', 'Current Person', 'current@example.test')]
    for cid, name, email in identities:
        path = people / f'{cid}.md'
        kg.write_person_file(str(path), kg.PersonFile(canonical_id=cid, name=name, identifiers=[email]))
        old = time.time() - 21 * 86400
        os.utime(path, (old, old))
    continuity = tmp_path / 'knowledge/continuity'
    continuity.mkdir()
    for cid, name, email in identities:
        row = {'anchor_key': f'follow_up:cid:{cid}', 'canonical_id': cid,
               'contact_name': name, 'contact_identifier': email, 'channel': 'gmail',
               'action_type': 'follow_up', 'status': 'resolved' if cid == 'c_abc111' else 'open',
               'created_at': datetime.now().strftime('%Y-%m-%dT09:00:00'),
               'ask': 'Send the research document', 'summary': 'Research document delivery',
               'source': 'gmail', 'source_thread_id': cid + '-thread',
               'source_message_id': cid + '-original',
               'source_refs': [{'sourceType': 'gmail', 'sourceId': cid + '-original',
                                'snippet': 'I will send the research document.'}]}
        if row['status'] == 'resolved':
            row.update(resolution='replied', closed_at=datetime.now().isoformat(),
                       resolution_reason='Research document supplied.',
                       resolution_source_refs=[{'sourceType': 'gmail', 'sourceId': cid + '-closing',
                                                'snippet': 'Here is the research document.'}])
        (continuity / f'{cid}.md').write_text('---\n' + json.dumps(row) + '\n---\n')
    cohort = _run(tmp_path, '--interactive')
    assert {entry['canonical_id'] for entry in cohort['contact_index']} == {'c_abc222'}
    assert 'c_abc111' not in cohort['person_knowledge']
    unresolved = _run(tmp_path, '--person', 'Older', '--person-coverage', '--interactive')
    hint = unresolved['coverage']['identity_resolution']['saved_identity_discovery']['scope']
    assert 'current/recent' in hint and 'older/outside-cohort' in hint.lower()
    assert 'not all saved identities' in hint and 'do not establish absence' in hint
    exact = _run(tmp_path, '--person', 'Older Person', '--person-coverage', '--interactive')
    assert exact['coverage']['graph'] == 'found'
    history = exact['obligation_history']
    assert history['status'] == 'matched' and history['absence_is_authoritative'] is False
    assert [(row['canonical_id'], row['status']) for row in history['items']] == [('c_abc111', 'resolved')]
    assert history['items'][0]['source_refs'][0]['sourceId'] == 'c_abc111-original'
    assert history['items'][0]['resolution_source_refs'][0]['sourceId'] == 'c_abc111-closing'


def test_topic_only_query_reads_closed_obligation_evidence_without_graph_cohort(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '["gmail"]')
    directory = tmp_path / 'knowledge/continuity'
    directory.mkdir(parents=True)
    row = {'anchor_key': 'orion', 'canonical_id': 'c_abc111', 'contact_name': 'Alex Reed',
        'contact_identifier': 'alex@example.test', 'status': 'resolved', 'action_type': 'follow_up',
        'source': 'gmail', 'channel': 'gmail', 'source_thread_id': 'orion-thread',
        'source_refs': [{'sourceType': 'gmail', 'sourceId': 'original', 'snippet': 'Orion approval'}],
        'ask': 'Orion approval follow-up', 'created_at': '2026-10-18',
        'resolution': 'replied', 'resolution_evidence': 'Approved and sent.',
        'resolution_source_refs': [{'sourceType': 'gmail', 'sourceId': 'sent', 'snippet': 'Approved.'}]}
    path = directory / 'orion.md'
    path.write_text('---\n' + json.dumps(row) + '\n---\n')
    before = path.read_bytes()
    def forbidden(*_args, **_kwargs):
        raise AssertionError('topic-only query traversed the graph or broad cohort')
    monkeypatch.setattr(kg, 'people_dir', forbidden)
    monkeypatch.setattr(kq, '_unique_person', forbidden)
    monkeypatch.setattr(kq, 'active_loop_participants', forbidden)
    monkeypatch.setattr(kq, 'pack_companies', forbidden)
    args = SimpleNamespace(company=None, person=None, topic='Orion', interactive=True,
                           calendar=None, gmail=None, local=None, loops=None)
    kq.query(args, datetime.now())
    result = json.loads(capsys.readouterr().out)
    assert result['person_knowledge'] == {} and result['contact_index'] == []
    assert result['memory_participants'] == []
    history = result['obligation_history']
    assert history['status'] == 'matched' and history['absence_is_authoritative'] is False
    assert history['items'][0]['completion_basis'] == 'source_evidence'
    assert history['items'][0]['source_refs'][0]['sourceId'] == 'original'
    assert history['items'][0]['resolution_source_refs'][0]['sourceId'] == 'sent'
    assert kq._fits_interactive(result)
    assert path.read_bytes() == before


def test_native_topic_query_keeps_empty_and_withheld_outputs_identical(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_READ_ONLY', '1')
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '["imessage"]')
    empty = _run(tmp_path, '--topic', 'Orion', '--interactive')
    assert empty['obligation_history']['status'] == 'no_match'
    directory = tmp_path / 'knowledge/continuity'
    directory.mkdir(parents=True, exist_ok=True)
    row = {'anchor_key': 'orion', 'canonical_id': 'c_abc111', 'contact_name': 'PRIVATE-NAME',
        'contact_identifier': 'private@example.test', 'status': 'resolved', 'action_type': 'follow_up',
        'source': 'gmail', 'channel': 'gmail', 'source_thread_id': 'PRIVATE-THREAD',
        'source_refs': [{'sourceType': 'gmail', 'sourceId': 'PRIVATE-ORIGINAL'}],
        'ask': 'Orion PRIVATE-ASK', 'created_at': '2026-10-18', 'resolution': 'replied',
        'resolution_evidence': 'PRIVATE-CLOSURE',
        'resolution_source_refs': [{'sourceType': 'gmail', 'sourceId': 'PRIVATE-SENT'}]}
    path = directory / 'orion.md'
    path.write_text('---\n' + json.dumps(row) + '\n---\n')
    assert _run(tmp_path, '--topic', 'Orion', '--interactive') == empty
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '["gmail"]')
    visible = _run(tmp_path, '--topic', 'Orion', '--interactive')
    assert visible['obligation_history']['status'] == 'matched'
    assert visible['obligation_history']['items'][0]['anchor_key'] == 'orion'
    for captured in ('invalid', '{"gmail":true}'):
        monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', captured)
        unknown = _run(tmp_path, '--topic', 'Orion', '--interactive')['obligation_history']
        assert unknown['status'] == 'unavailable' and unknown['items'] == []
        assert unknown['omitted'] == 0 and unknown['absence_is_authoritative'] is False
    monkeypatch.delenv('SOTTO_CAPTURED_SOURCES')
    unknown = _run(tmp_path, '--topic', 'Orion', '--interactive')['obligation_history']
    assert unknown['status'] == 'unavailable' and unknown['items'] == []


def test_complete_topic_result_does_not_invent_omitted_context():
    import copy
    history = {'status': 'matched', 'items': [{'anchor_key': 'pricing', 'truncated_fields': []}],
               'omitted': 0, 'absence_is_authoritative': False,
               'coverage': 'Source archives were not searched.',
               'learning': {'gmail': {'status': 'partial'}}}
    value = {'person_knowledge': {}, 'contact_index': [], 'memory_participants': [],
             'obligation_history': history}
    before = copy.deepcopy(value)
    result = kq.interactive_projection(value)
    assert 'suggestion' not in result
    assert result['obligation_history'] == history
    assert value == before
    for change in ({'omitted': 1}, {'items': [{'anchor_key': 'pricing', 'truncated_fields': ['ask']}]}):
        limited = kq.interactive_projection({**value, 'obligation_history': {**history, **change}})
        assert limited['suggestion'] == kq.NARROWER_QUERY
    assert kq.interactive_projection(value, unselected_people=1)['suggestion'] == kq.NARROWER_QUERY


def test_fact_evidence_unknown_observation_dates_are_null(monkeypatch):
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '["gmail"]')
    fact = kg.FactMeta(text='Known graph assertion', source='observed_message',
                       source_ref='gmail:visible', first=None, last='not-a-date')
    result = kq.attach_fact_evidence({'person_knowledge': {'c_one': fact.text}},
        [('c_one', 'f_one', fact)], datetime(2026, 10, 20), interactive=False)
    item, = result['fact_evidence']['items']
    assert item['first_observed_date'] is None
    assert item['last_observed_date'] is None


@pytest.mark.parametrize('interactive', [False, True])
def test_facts_only_keeps_evidence_and_coverage_without_reading_obligations(tmp_path, monkeypatch, capsys, interactive):
    import ledger_io
    import protected_ledger_io

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_READ_ONLY', '1')
    monkeypatch.setenv('SOTTO_CAPTURED_SOURCES', '["gmail"]')
    ku.apply({'person_updates': [{'identifier': 'dana@example.test', 'person_name': 'Dana Rivers',
        'facts': [{'fact': 'Builds coastal sensors', 'source': 'observed_message', 'confidence': 0.95,
                   'source_ref': 'gmail:sensors', 'evidence_refs': ['gmail:sensors']}]}]}, now=datetime.now())
    sentinel = 'OBLIGATION-ONLY-cedar-packet-7913'
    directory = tmp_path / 'knowledge/continuity'
    directory.mkdir(parents=True, exist_ok=True)
    row = {'anchor_key': 'cedar-packet', 'contact_name': 'Dana Rivers',
           'contact_identifier': 'dana@example.test', 'status': 'open', 'action_type': 'follow_up',
           'source': 'gmail', 'channel': 'gmail', 'source_thread_id': 'packet-thread',
           'source_refs': [{'sourceType': 'gmail', 'sourceId': 'packet-request'}],
           'ask': sentinel, 'created_at': '2026-10-20'}
    (directory / 'cedar-packet.md').write_text('---\n' + json.dumps(row) + '\n---\n')
    argv = [QUERY, '--person', 'dana@example.test', '--person-coverage']
    if interactive:
        argv.append('--interactive')

    def forbidden(*args, **kwargs):
        raise AssertionError('Facts-only must not read the obligation ledger')

    with monkeypatch.context() as guarded:
        # Guard the canonical loaders themselves, including the protected history reader.
        guarded.setattr(ledger_io, 'load_entries', forbidden)
        guarded.setattr(ledger_io, 'load_active', forbidden)
        guarded.setattr(protected_ledger_io, 'load_protected_entries', forbidden)
        guarded.setattr(sys, 'argv', [*argv, '--facts-only'])
        kq.main()
        result = json.loads(capsys.readouterr().out)
        assert 'obligation_history' not in result
        assert sentinel not in json.dumps(result)
        assert 'Builds coastal sensors' in json.dumps(result['person_knowledge'])
        assert result['fact_evidence']['items']
        assert result['coverage']['historical_sources']
        assert 'learning' in next(s for s in result['coverage']['historical_sources'] if s['source'] == 'gmail')
        guarded.setattr(sys, 'argv', argv)
        with pytest.raises(AssertionError, match='obligation ledger'):
            kq.main()

    # The real full view must return the seeded obligation in both modes.
    monkeypatch.setattr(sys, 'argv', argv)
    kq.main()
    full = json.loads(capsys.readouterr().out)
    assert sentinel in json.dumps(full)
    assert full['obligation_history']['items'][0]['ask'] == sentinel


def test_facts_only_cli_rejects_incoherent_scope(tmp_path):
    for args in [[], ['--person', 'Dana'], ['--person-coverage'],
                 ['--person', 'Dana', '--person-coverage', '--editable-person'],
                 ['--person', 'Dana', '--person-coverage', '--relevant-days', '7'],
                 ['--person', 'Dana', '--person-coverage', '--company', 'Cedar']]:
        proc = subprocess.run([sys.executable, QUERY, '--facts-only', *args],
                              capture_output=True, text=True, env=dict(os.environ, SOTTO_DATA=str(tmp_path)))
        assert proc.returncode == 2
        assert '--facts-only requires' in proc.stderr
