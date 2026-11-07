"""Research failures, terminal coverage and corrected typed identity."""
import json
import os
from datetime import datetime, timedelta, timezone

from test_prep_persist import (
    pp, kg, ku, RESEARCH, ATTENDEES_IN, FOCUS_RESEARCH, _person, _load,
)


def test_company_lookup_failure_disclosed_and_person_not_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    def fail(**kwargs):
        raise OSError("company lookup unavailable")
    monkeypatch.setattr(ku, "company_knowledge", fail)
    result = pp.persist(FOCUS_RESEARCH)
    assert "company lookup unavailable" in result.get("error", "")
    assert result["complete"] is False
    assert not pp.profile_is_fresh("spencer", "spencer@commenda.io")


def test_company_write_failure_receipt_and_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    writer = ku._write_company
    def fail(*args, **kwargs):
        raise OSError("company volume unavailable")
    monkeypatch.setattr(ku, "_write_company", fail)
    result = pp.persist(FOCUS_RESEARCH)
    assert result["persisted"] == 1
    assert result["complete"] is False and result["companies"] == []
    assert not pp.profile_is_fresh("spencer", "spencer@commenda.io")
    monkeypatch.setattr(ku, "_write_company", writer)
    result = pp.persist(FOCUS_RESEARCH)
    assert result["complete"] is True and result["companies"] == ["Commenda"]
    assert pp.profile_is_fresh("spencer", "spencer@commenda.io")


def test_owner_bio_correction_invalidates_typed_identity_and_survives_research(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(RESEARCH, ATTENDEES_IN)
    path, person = _person(tmp_path)
    edit = _load("research_identity_edit", "_shared/knowledge/knowledge_edit.py")
    edit.op_correct(os.path.basename(path)[:-3], next(iter(person.facts)), "Dana is CTO at Acme, not CEO.")
    _, corrected = _person(tmp_path)
    assert corrected.title is None and corrected.company is None
    query = _load("research_identity_query", "_shared/knowledge/knowledge_query.py")
    packed = query.pack_person(corrected, True, datetime.now(timezone.utc))
    assert "CEO @ Acme" not in packed and "Dana is CTO at Acme, not CEO." in packed
    result = pp.persist(RESEARCH, ATTENDEES_IN)
    _, replayed = _person(tmp_path)
    assert replayed.title is None and replayed.company is None
    assert result["persisted"] == 0
    assert result["outcomes"]["people"][0]["status"] == "rejected"


def test_research_identity_support_roundtrip_and_unrelated_correction(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(RESEARCH, ATTENDEES_IN)
    path, person = _person(tmp_path)
    bio_id = next(iter(person.facts))
    assert person.identity_support['title'] == {'source': 'web_research', 'fact_id': bio_id}
    edit = _load("research_personal_edit", "_shared/knowledge/knowledge_edit.py")
    edit.op_add(os.path.basename(path)[:-3], "Likes hiking in spring.", "personal")
    path, person = _person(tmp_path)
    personal_id = next(fid for fid, fact in person.facts.items() if fact.type == 'personal')
    edit.op_correct(os.path.basename(path)[:-3], personal_id, "Likes cycling in spring.")
    _, person = _person(tmp_path)
    assert person.title == 'CEO' and person.company == 'Acme'


def test_partial_retry_skips_saved_company_and_rejects_changed_input_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    writer = ku.apply
    writes = []
    def fail_person(payload):
        if payload['person_updates']:
            raise OSError('person volume unavailable')
        return writer(payload)
    monkeypatch.setattr(ku, 'apply', fail_person)
    receipt = pp.persist(FOCUS_RESEARCH)
    assert not receipt['complete'] and receipt['companies'] == ['Commenda']
    def track(payload):
        writes.append(payload)
        return writer(payload)
    monkeypatch.setattr(ku, 'apply', track)
    result = pp.persist(FOCUS_RESEARCH, previous_receipt=receipt)
    assert result['complete'] and len(writes) == 1
    assert writes[0]['person_updates'] and not writes[0]['company_updates']
    writes.clear()
    changed = json.loads(json.dumps(FOCUS_RESEARCH))
    changed['attendees'][0]['company_deep']['market'] = 'Changed verified market.'
    pp.persist(changed, previous_receipt=result)
    assert [bool(payload["company_updates"]) for payload in writes] == [True, False]


def test_owner_identity_withdrawal_survives_merge_in_both_directions():
    owner = kg.PersonFile(title=None, company=None, identity_support={
        'title': {'source': 'user_edit', 'fact_id': 'corrected'},
        'company': {'source': 'user_edit', 'fact_id': 'corrected'}})
    automatic = kg.PersonFile(title='CEO', company='Acme')
    merged = kg.merge_person(automatic, owner)
    assert merged.title is None and merged.company is None
    merged = kg.merge_person(owner, kg.PersonFile(title='CEO', company='Acme'))
    assert merged.title is None and merged.company is None


def test_legacy_research_identity_correction_withdraws_pair(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    pp.persist(RESEARCH, ATTENDEES_IN)
    path, person = _person(tmp_path)
    person.identity_support.clear()
    kg.write_person_file(path, person)
    edit = _load('legacy_research_identity_edit', '_shared/knowledge/knowledge_edit.py')
    edit.op_correct(os.path.basename(path)[:-3], next(iter(person.facts)), 'Dana is CTO at Acme.')
    _, corrected = _person(tmp_path)
    assert corrected.title is None and corrected.company is None
    assert corrected.identity_support['title']['source'] == 'user_edit'


def test_owner_protected_research_counts_coverage_without_applying_fact(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    old = datetime.now(timezone.utc) - timedelta(days=40)
    pp.persist(RESEARCH, ATTENDEES_IN, now=old)
    path, person = _person(tmp_path)
    edit = _load('coverage_identity_edit', '_shared/knowledge/knowledge_edit.py')
    edit.op_correct(os.path.basename(path)[:-3], next(iter(person.facts)), 'Dana is CTO at Acme, not CEO.')
    assert not pp.profile_is_fresh('Dana Roe', 'dana@acme.com')
    receipt = pp.persist(RESEARCH, ATTENDEES_IN)
    assert receipt['persisted'] == 0
    assert receipt['outcomes']['people'][0]['status'] == 'rejected'
    assert receipt['outcomes']['people'][0]['fresh'] is True
    assert pp.profile_is_fresh('Dana Roe', 'dana@acme.com')
    _, person = _person(tmp_path)
    assert person.title is None


def test_legacy_personal_research_correction_keeps_typed_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    research = json.loads(json.dumps(RESEARCH))
    research['attendees'][0]['personal'] = ['Enjoys hiking https://example.com/hiking']
    pp.persist(research, ATTENDEES_IN)
    path, person = _person(tmp_path)
    person.identity_support.clear()
    kg.write_person_file(path, person)
    personal_id = next(fid for fid, fact in person.facts.items() if fact.type == 'personal')
    edit = _load('legacy_personal_correction_edit', '_shared/knowledge/knowledge_edit.py')
    edit.op_correct(os.path.basename(path)[:-3], personal_id, 'Enjoys cycling.')
    _, person = _person(tmp_path)
    assert person.title == 'CEO' and person.company == 'Acme'


def test_research_coverage_rejection_whitelist_does_not_trust_invalid_facts():
    for reason in ('protected_owner_correction', 'similar_fact_exists', 'older_observation'):
        assert ku.fact_establishes_research_coverage({'disposition': 'rejected', 'reason': reason})
    for reason in ('low_confidence', 'non_assertion', 'withdrawn_source_evidence',
                   'invalid_duplicate_lineage', 'missing_person_identity', 'unknown'):
        assert not ku.fact_establishes_research_coverage({'disposition': 'rejected', 'reason': reason})
    assert not ku.fact_establishes_research_coverage({'disposition': 'ignored', 'reason': 'archived_match'})


def test_partial_invalid_fact_outcomes_do_not_stamp_person_freshness(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    result = ku.apply({'person_updates': [{'person_name': 'Dana Roe', 'identifier': 'dana@acme.com',
        'last_researched': datetime.now(timezone.utc).strftime('%Y-%m-%d'), 'facts': [
            {'fact': 'Builds developer tools.', 'confidence': 0.8, 'source': 'web_research'},
            {'fact': 'Unsupported unrelated claim.', 'confidence': 0.1, 'source': 'web_research'}]}]})
    assert result['applied']['new'] == 1
    assert not pp.profile_is_fresh('Dana Roe', 'dana@acme.com')


def test_company_failure_withholds_coverage_then_retry_stamps_protected_person(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    old = datetime.now(timezone.utc) - timedelta(days=40)
    pp.persist(FOCUS_RESEARCH, now=old)
    path, person = _person(tmp_path, 'spencer', 'spencer@commenda.io')
    edit = _load('protected_retry_edit', '_shared/knowledge/knowledge_edit.py')
    edit.op_correct(os.path.basename(path)[:-3], next(iter(person.facts)), 'Spencer is CTO, not CEO.')
    writer = ku._write_company
    def fail(*args, **kwargs):
        raise OSError('fixture company write failed')
    monkeypatch.setattr(ku, '_write_company', fail)
    receipt = pp.persist(FOCUS_RESEARCH)
    assert not receipt['complete'] and not receipt['outcomes']['people'][0]['fresh']
    assert not pp.profile_is_fresh('spencer', 'spencer@commenda.io')
    monkeypatch.setattr(ku, '_write_company', writer)
    result = pp.persist(FOCUS_RESEARCH, previous_receipt=receipt)
    assert result['complete'] and result['outcomes']['people'][0]['fresh']
    assert result['persisted'] == 0
    assert pp.profile_is_fresh('spencer', 'spencer@commenda.io')


def test_low_overlap_owner_correction_is_traced_research_coverage(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    pp.persist(RESEARCH, ATTENDEES_IN, now=datetime.now(timezone.utc) - timedelta(days=40))
    path, person = _person(tmp_path)
    fact_id = next(iter(person.facts))
    edit = _load('low_overlap_owner_edit', '_shared/knowledge/knowledge_edit.py')
    edit.op_correct(os.path.basename(path)[:-3], fact_id, 'She resigned yesterday.')
    result = pp.persist(RESEARCH, ATTENDEES_IN)
    outcome = result['outcomes']['people'][0]
    assert outcome['fact_outcomes']['items'][0]['reason'] == 'archived_match'
    assert outcome['fresh']
    assert outcome['fact_outcomes']['items'][0]['fact_id'] == fact_id
    assert pp.profile_is_fresh('Dana Roe', 'dana@acme.com')
    assert result['persisted'] == 0


def test_owner_archived_research_is_coverage_but_withdrawn_archive_is_not(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    pp.persist(RESEARCH, ATTENDEES_IN, now=datetime.now(timezone.utc) - timedelta(days=40))
    path, person = _person(tmp_path)
    fact_id = next(iter(person.facts))
    edit = _load('archived_owner_edit', '_shared/knowledge/knowledge_edit.py')
    edit.op_archive(os.path.basename(path)[:-3], fact_id)
    assert pp.persist(RESEARCH, ATTENDEES_IN)['outcomes']['people'][0]['fresh']
    path, person = _person(tmp_path)
    person.last_researched = None
    person.facts[fact_id].archived_reason = 'source_invalidated'
    kg.write_person_file(path, person)
    receipt = pp.persist(RESEARCH, ATTENDEES_IN)
    assert not receipt['outcomes']['people'][0]['fresh']
    assert not pp.profile_is_fresh('Dana Roe', 'dana@acme.com')


def test_reworded_mid_overlap_owner_archive_keeps_research_coverage(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    before = json.loads(json.dumps(RESEARCH))
    before['attendees'][0]['summary'] = 'Builds infrastructure tools across distributed storage systems serving enterprise engineers.'
    after = json.loads(json.dumps(RESEARCH))
    after['attendees'][0]['summary'] = 'Builds marine biology tools near coastal laboratories while teaching advanced oceanography seminars.'
    old_words, new_words = (kg.make_dedupe_key(pp.research_bio(row['attendees'][0])) for row in (before, after))
    lower_overlap, upper_overlap = 0.3, 0.5
    assert lower_overlap <= len(old_words & new_words) / min(len(old_words), len(new_words)) <= upper_overlap
    pp.persist(before, ATTENDEES_IN, now=datetime.now(timezone.utc) - timedelta(days=40))
    path, person = _person(tmp_path)
    fact_id = next(iter(person.facts))
    edit = _load('mid_overlap_owner_edit', '_shared/knowledge/knowledge_edit.py')
    edit.op_archive(os.path.basename(path)[:-3], fact_id)
    assert kg.find_similar_fact(person.facts, pp.research_bio(after['attendees'][0]), 'context', False)[0] == kg.SUPERSEDE
    result = pp.persist(after, ATTENDEES_IN)
    outcome = result['outcomes']['people'][0]
    assert outcome['fact_outcomes']['items'][0]['reason'] == 'archived_match'
    assert outcome['fact_outcomes']['items'][0]['fact_id'] == fact_id
    assert outcome['fresh'] and pp.profile_is_fresh('Dana Roe', 'dana@acme.com')
    _, archived = _person(tmp_path)
    assert kg.find_similar_fact(archived.facts, pp.research_bio(after['attendees'][0]), 'context', False) == (kg.SKIP, None)


def test_archived_owner_fact_keeps_ignored_disposition_and_archive_coverage_policy(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    for source in ('brief_extraction', 'web_research'):
        for archive_reason in ('user_archived', 'source_invalidated'):
            email = f'{source}-{archive_reason}@acme.com'
            fact = {'fact': 'Dana likes sailing.', 'source': 'user_edit', 'confidence': 1.0, 'memory_type': 'context'}
            ku.apply({'person_updates': [{'person_name': email, 'identifier': email, 'facts': [fact]}]})
            path = kg.find_person_file(identifier=email)
            person = kg.parse_person_file(open(path).read())
            fid = next(iter(person.facts))
            person.facts[fid].status = 'archived'
            person.facts[fid].archived_reason = archive_reason
            kg.write_person_file(path, person)
            result = ku.apply({'person_updates': [{'person_name': email, 'identifier': email,
                'last_researched': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
                'facts': [{**fact, 'source': source}]}]})
            outcome = result['fact_outcomes']['items'][0]
            assert outcome['disposition'] == 'ignored' and outcome['reason'] == 'archived_match'
            assert outcome['fact_id'] == fid and outcome['tombstone_reason'] == archive_reason
            assert result['fact_outcomes']['counts'].get('rejected', 0) == 0
            assert pp.profile_is_fresh(email, email) == (source == 'web_research' and archive_reason == 'user_archived')
