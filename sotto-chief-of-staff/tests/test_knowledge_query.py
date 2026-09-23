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

import knowledge as kg
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
    assert focused['facts'][0] == {'id': 'f_wrong', 'text': 'Peyton founded Alive.'}

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
    import render_local
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
    # The changed packed format must not trigger paid re-research just because the first fact is short.
    assert render_local._is_high_quality_profile('Alex\n= Runs clinics.\n= Advises procurement boards on pediatric rollouts and governance.')
    assert not render_local._is_high_quality_profile('Alex\n= Runs clinics.\nRelated context about Jo: ' + 'expert ' * 30)


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
