"""Gift eligibility depends on sustained reciprocal days, not volume/urgency."""
import importlib.util
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parents[1] / '_shared/lib'))
import relationship_importance as ri

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def person(offsets, reciprocal=True):
    dates = [NOW - timedelta(days=d) for d in offsets]
    return {'dates': dates, 'from_me': dates[::2] if reciprocal else [], 'from_them': dates[1::2] if reciprocal else dates}


def test_vip_vvip_and_explainable_evidence():
    vip = ri.classify(ri.activity_evidence(person([0, 1, 8, 9, 16, 17]), {}, NOW), NOW)
    assert vip['tier'] == 'vip' and vip['active_days'] == 6
    assert vip['sent_days'] == vip['received_days'] == 3
    vvip = ri.classify(ri.activity_evidence(person([0, 1, 2, 7, 8, 9, 14, 15, 16, 21, 22, 23]), {}, NOW), NOW)
    assert vvip['tier'] == 'vvip'


def test_burst_one_way_stale_and_future_activity_never_qualify():
    for p in [person([0] * 500), person(list(range(30)), False), person(list(range(50, 80))), person(list(range(-20, 0)))]:
        assert ri.classify(ri.activity_evidence(p, {}, NOW), NOW)['tier'] == 'regular'


def test_overlapping_reads_are_deduplicated_and_rank_expires():
    p = person([0, 1, 8, 9, 16, 17])
    evidence = ri.activity_evidence(p, {}, NOW)
    assert ri.activity_evidence(p, evidence, NOW) == evidence
    assert ri.classify(evidence, NOW + timedelta(days=43))['tier'] == 'regular'


def test_explicit_vip_needs_no_activity_but_volume_and_attention_are_not_proof():
    assert ri.for_contact({'name': 'Alex'}, {}, ['alex'], NOW)['tier'] == 'vip'
    history = {'Alex': {'interactions': 9999, 'priority': 9999}}
    assert ri.for_contact({'name': 'Alex'}, history, [], NOW)['tier'] == 'regular'


def test_identity_collision_or_different_canonical_id_cannot_borrow_rank():
    evidence = ri.activity_evidence(person([0, 1, 8, 9, 16, 17]), {}, NOW)
    history = {'p1': {'name': 'Alex', 'importance_evidence': evidence}, 'p2': {'name': 'Alex'}}
    assert ri.for_contact({'name': 'Alex'}, history, [], NOW)['tier'] == 'regular'
    assert ri.for_contact({'name': 'Renamed Alex', 'canonical_id': 'p1'}, history, [], NOW)['tier'] == 'vip'
    assert ri.for_contact({'name': 'Alex', 'canonical_id': 'p2'}, history, [], NOW)['tier'] == 'regular'


def test_explicit_vip_survives_alias_card_but_not_another_canonical_person():
    assert ri.for_contact({'name': 'Jordan Lee', 'canonical_id': 'p1'}, {}, ['Jordy'], NOW,
                          aliases=['Jordan Lee', 'Jordy'])['tier'] == 'vip'
    assert ri.for_contact({'name': 'Jordan Lee', 'canonical_id': 'p2'}, {}, ['Jordy'], NOW,
                          aliases=['Jordan Lee'])['tier'] == 'regular'


def test_pulse_persists_activity_from_multiple_channels_without_double_counting(monkeypatch):
    path = Path(__file__).parents[1] / 'relationship-pulse/scripts/relationship_pulse.py'
    spec = importlib.util.spec_from_file_location('importance_pulse', path)
    rp = importlib.util.module_from_spec(spec); spec.loader.exec_module(rp)
    monkeypatch.setattr(rp, '_graph_lookup', lambda *a: (1.0, None))
    local = {'contacts': [{'name': 'Alex', 'phones': ['+15555550111'], 'emails': ['alex@example.com']}]}
    dates = [0, 1, 8, 9, 16, 17]
    local['imessage'] = [{'handle': '+15555550111', 'resolved_name': 'Alex', 'timestamp': (NOW-timedelta(days=d)).isoformat(), 'is_from_me': i % 2 == 0} for i,d in enumerate(dates[:2])]
    local['whatsapp'] = [{'partner_name': 'Alex', 'resolved_name': 'Alex', 'timestamp': (NOW-timedelta(days=d)).isoformat(), 'is_from_me': i % 2 == 0} for i,d in enumerate(dates[2:4])]
    # Reuse the pulse's actual email normalization/known-contact lane.
    monkeypatch.setattr(rp, '_email_identities', lambda *a: {'alex@example.com': {'name': 'Alex', 'cid': ''}})
    monkeypatch.setattr(rp, '_own_email', lambda: 'me@example.com')
    local['emails'] = [
        {'from': 'me@example.com', 'to': 'alex@example.com', 'labels': ['SENT'], 'date': (NOW-timedelta(days=16)).isoformat()},
        {'from': 'alex@example.com', 'to': 'me@example.com', 'date': (NOW-timedelta(days=17)).isoformat()}]
    out = rp.compute(local, NOW)
    record = next(r for r in out['history'].values() if r['name'] == 'Alex')
    assert record['importance']['tier'] == 'vip'
    assert record['importance']['active_days'] == 6
    out2 = rp.compute(local, NOW, out['history'])
    assert next(r for r in out2['history'].values() if r['name'] == 'Alex')['importance']['active_days'] == 6


def test_answered_calls_are_relationship_evidence(monkeypatch):
    path = Path(__file__).parents[1] / 'relationship-pulse/scripts/relationship_pulse.py'
    spec = importlib.util.spec_from_file_location('importance_calls_pulse', path)
    rp = importlib.util.module_from_spec(spec); spec.loader.exec_module(rp)
    monkeypatch.setattr(rp, '_graph_lookup', lambda *a: (1.0, None))
    offsets = [0, 1, 8, 9, 16, 17]
    local = {'recent_calls': [
        {'name': 'Alex', 'timestamp': (NOW - timedelta(days=days)).isoformat(),
         'direction': 'outgoing' if i % 2 == 0 else 'incoming', 'duration_seconds': 600}
        for i, days in enumerate(offsets)
    ]}
    alex = rp._interactions_by_contact(local)['Alex']
    assert ri.classify(ri.activity_evidence(alex, {}, NOW), NOW)['tier'] == 'vip'


def test_people_index_is_shared_within_run_and_refreshed_next_run(monkeypatch):
    from types import SimpleNamespace
    path = Path(__file__).parents[1] / 'relationship-pulse/scripts/relationship_pulse.py'
    spec = importlib.util.spec_from_file_location('importance_index_pulse', path)
    rp = importlib.util.module_from_spec(spec); spec.loader.exec_module(rp)
    snapshots, lookups = [], []
    def build():
        snapshot = {'by_identifier': {}, 'revision': len(snapshots)}
        snapshots.append(snapshot)
        return snapshot
    def find(**kwargs):
        lookups.append(kwargs['index'])
        return None
    monkeypatch.setattr(rp, '_knowledge', lambda: SimpleNamespace(build_people_index=build, find_person_file=find))
    local = {'whatsapp': [{'partner_name': name, 'timestamp': NOW.isoformat()} for name in ('Alex', 'Blair')]}
    rp.compute(local, NOW)
    assert len(snapshots) == 1 and len(lookups) == 2
    assert all(index is snapshots[0] for index in lookups)
    rp.compute(local, NOW)
    assert len(snapshots) == 2
    assert all(index is snapshots[1] for index in lookups[2:])
    # A paginated history read includes only a few people. Every absent prior contact must
    # share the same index too, or each page scans the entire graph hundreds of times.
    before = len(lookups)
    history = {f'Past contact {i}': {'name': f'Past contact {i}', 'interactions': 20,
                                  'last_contact': (NOW-timedelta(days=50)).date().isoformat()}
               for i in range(20)}
    out = rp.compute(local, NOW, history)
    assert out['lapsed']
    assert len(snapshots) == 3 and len(lookups) == before + 22
    assert all(index is snapshots[2] for index in lookups[before:])
