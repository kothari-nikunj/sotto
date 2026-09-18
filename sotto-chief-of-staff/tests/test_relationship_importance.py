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
                                  'last_contact': (NOW-timedelta(days=50)).date().isoformat(),
                                  'importance_evidence': {'sent_days': ['2026-08-01', '2026-08-02'],
                                                          'received_days': ['2026-08-03', '2026-08-04']}}
               for i in range(20)}
    out = rp.compute(local, NOW, history)
    assert out['lapsed']
    assert len(snapshots) == 3 and len(lookups) == before + 22
    assert all(index is snapshots[2] for index in lookups[before:])


def test_reply_signals_are_distinct_finite_and_never_accelerate_chase():
    events = [
        {'at': NOW-timedelta(days=12), 'from_me': False, 'conversation_id': 'a', 'native_id': '1'},
        {'at': NOW-timedelta(days=10), 'from_me': True, 'conversation_id': 'a', 'native_id': '2'},
        {'at': NOW-timedelta(days=8), 'from_me': False, 'conversation_id': 'a', 'native_id': '3'},
        {'at': NOW-timedelta(days=4), 'from_me': True, 'conversation_id': 'a', 'native_id': '4'},
        {'at': NOW-timedelta(days=2), 'from_me': False, 'conversation_id': 'a', 'native_id': '5'}]
    engagement = ri.engagement_signals({'events': events}, {}, NOW)
    assert 0 < ri.attention_tiebreak(engagement, NOW) < 1
    assert engagement['counterpart_reply_lag_days'] >= 1
    default = NOW - timedelta(days=3)
    assert ri.learned_chase_after(default, engagement, NOW) >= default
    assert ri.attention_tiebreak(engagement, NOW + timedelta(days=61)) == 0


def test_old_reply_cadence_applies_to_the_current_obligation_anchor():
    engagement = {
        "counterpart_reply_lag_days": 6,
        "counterpart_reply_lag_expires": "2026-11-04T00:00:00+00:00",
        "last_owner_message_at": "2026-09-01T09:00:00+00:00",
    }
    obligation = datetime(2026, 9, 15, 9, tzinfo=timezone.utc)
    ordinary_default = datetime(2026, 9, 18, 9, tzinfo=timezone.utc)
    evaluated = datetime(2026, 9, 18, 8, tzinfo=timezone.utc)
    assert ri.learned_chase_after(ordinary_default, engagement, evaluated, obligation) == \
        datetime(2026, 9, 21, 9, tzinfo=timezone.utc)


def test_reply_burst_dedup_and_thread_boundaries_do_not_invent_samples():
    events = [
        {'at': NOW-timedelta(days=6), 'from_me': True, 'conversation_id': 'a', 'native_id': '1'},
        {'at': NOW-timedelta(days=5), 'from_me': True, 'conversation_id': 'a', 'native_id': '2'},
        {'at': NOW-timedelta(days=4), 'from_me': False, 'conversation_id': 'a', 'native_id': '3'},
        {'at': NOW-timedelta(days=3), 'from_me': False, 'conversation_id': 'a', 'native_id': '3'},
        {'at': NOW-timedelta(days=2), 'from_me': True, 'conversation_id': 'b', 'native_id': '4'},
        {'at': NOW-timedelta(days=1), 'from_me': False, 'conversation_id': 'c', 'native_id': '5'}]
    result = ri.engagement_signals({'events': events}, {}, NOW)
    assert 'counterpart_reply_lag_days' not in result  # one real reply is insufficient


def test_same_identifier_on_different_channels_never_becomes_a_reply_thread():
    """A phone-shaped iMessage handle and WhatsApp partner id may be textually identical, but an
    inbound on one followed by an outbound on the other is not a reply in either conversation."""
    events = [
        {'at': NOW-timedelta(days=8), 'from_me': False, 'channel': 'imessage',
         'conversation_id': '+14155550100', 'native_id': 'im-1'},
        {'at': NOW-timedelta(days=7), 'from_me': True, 'channel': 'whatsapp',
         'conversation_id': '+14155550100', 'native_id': 'wa-1'},
        {'at': NOW-timedelta(days=4), 'from_me': False, 'channel': 'imessage',
         'conversation_id': '+14155550100', 'native_id': 'im-2'},
        {'at': NOW-timedelta(days=3), 'from_me': True, 'channel': 'whatsapp',
         'conversation_id': '+14155550100', 'native_id': 'wa-2'},
    ]
    result = ri.engagement_signals({'events': events}, {}, NOW)
    assert 'reply_samples' not in result
    assert 'owner_reply_lag_days' not in result
    assert 'counterpart_reply_lag_days' not in result


def test_held_meetings_strengthen_reciprocal_relationship_but_cannot_establish_one():
    meetings = [NOW - timedelta(days=d) for d in (1, 8, 15)]
    held_only = ri.activity_evidence({'held_meetings': meetings}, {}, NOW)
    assert ri.classify(held_only, NOW)['tier'] == 'regular'
    dates = [NOW - timedelta(days=d) for d in (0, 7, 14, 21)]
    evidence = ri.activity_evidence({'dates': dates, 'from_me': dates[:2],
                                     'from_them': dates[2:], 'held_meetings': meetings}, {}, NOW)
    result = ri.classify(evidence, NOW)
    assert result['tier'] == 'vip' and result['held_meetings'] == 3


def test_only_one_counterpart_is_eligible_held_meeting_evidence():
    assert ri.relationship_meeting_attendees(
        ['me@example.com', 'alex@example.com'], 'me@example.com') == ['alex@example.com']
    assert ri.relationship_meeting_attendees(['alex@example.com'], '') == ['alex@example.com']
    assert ri.relationship_meeting_attendees(
        ['me@example.com', 'alex@example.com', 'sam@example.com'], 'me@example.com') == []
    assert ri.relationship_meeting_attendees(['alex@example.com', 'sam@example.com'], '') == []
    assert ri.relationship_meeting_attendees(['me@example.com'], 'me@example.com') == []


def test_overlapping_burst_pages_do_not_count_one_owner_reply_twice():
    first = ri.engagement_signals({'events': [
        {'at': NOW-timedelta(hours=120), 'from_me': False, 'channel': 'gmail',
         'conversation_id': 'thread', 'native_id': 'b1'},
        {'at': NOW-timedelta(hours=119), 'from_me': True, 'channel': 'gmail',
         'conversation_id': 'thread', 'native_id': 'a1'}]}, {}, NOW)
    overlap = ri.engagement_signals({'events': [
        {'at': NOW-timedelta(hours=120), 'from_me': False, 'channel': 'gmail',
         'conversation_id': 'thread', 'native_id': 'b1'},
        {'at': NOW-timedelta(hours=119, minutes=30), 'from_me': False, 'channel': 'gmail',
         'conversation_id': 'thread', 'native_id': 'b2'},
        {'at': NOW-timedelta(hours=119), 'from_me': True, 'channel': 'gmail',
         'conversation_id': 'thread', 'native_id': 'a1'}]}, first, NOW)
    assert len(overlap['reply_samples']) == 1
    assert 'attention_boost' not in overlap


def test_outgoing_burst_extension_and_earlier_reply_do_not_rekey_one_turn():
    first = ri.engagement_signals({'events': [
        {'at': NOW-timedelta(hours=120), 'from_me': False, 'channel': 'gmail',
         'conversation_id': 'thread', 'native_id': 'b1'},
        {'at': NOW-timedelta(hours=118), 'from_me': True, 'channel': 'gmail',
         'conversation_id': 'thread', 'native_id': 'a2'}]}, {}, NOW)
    overlap = ri.engagement_signals({'events': [
        {'at': NOW-timedelta(hours=120), 'from_me': False, 'channel': 'gmail',
         'conversation_id': 'thread', 'native_id': 'b1'},
        {'at': NOW-timedelta(hours=119), 'from_me': True, 'channel': 'gmail',
         'conversation_id': 'thread', 'native_id': 'a1'},
        {'at': NOW-timedelta(hours=118), 'from_me': True, 'channel': 'gmail',
         'conversation_id': 'thread', 'native_id': 'a2'}]}, first, NOW)
    assert len(overlap['reply_samples']) == 1
    assert overlap['reply_samples'][0]['replied_at'] == (NOW-timedelta(hours=119)).isoformat()
    assert 'attention_boost' not in overlap


def test_reply_samples_combine_across_pages_and_replay_is_idempotent():
    page_one = {'events': [
        {'at': NOW-timedelta(days=8), 'from_me': True, 'conversation_id': 'a', 'native_id': '1'},
        {'at': NOW-timedelta(days=7), 'from_me': False, 'conversation_id': 'a', 'native_id': '2'}]}
    first = ri.engagement_signals(page_one, {}, NOW)
    assert len(first['reply_samples']) == 1 and 'counterpart_reply_lag_days' not in first
    page_two = {'events': [
        {'at': NOW-timedelta(days=4), 'from_me': True, 'conversation_id': 'b', 'native_id': '3'},
        {'at': NOW-timedelta(days=2), 'from_me': False, 'conversation_id': 'b', 'native_id': '4'}]}
    second = ri.engagement_signals(page_two, first, NOW)
    assert len(second['reply_samples']) == 2 and second['counterpart_reply_lag_days'] == 2
    replay = ri.engagement_signals(page_two, second, NOW)
    assert replay['reply_samples'] == second['reply_samples']


def test_sparse_pages_never_pair_events_across_an_unobserved_gap():
    first = ri.engagement_signals({'events': [
        {'at': NOW-timedelta(days=5), 'from_me': True, 'conversation_id': 'a', 'native_id': '1'}]}, {}, NOW)
    second = ri.engagement_signals({'events': [
        {'at': NOW-timedelta(days=4), 'from_me': False, 'conversation_id': 'a', 'native_id': '2'}]}, first, NOW)
    assert 'reply_samples' not in second and 'counterpart_reply_lag_days' not in second


def test_same_instant_on_two_channels_without_native_ids_keeps_both_events():
    """Dedupe is channel-scoped even without native ids: two conversations, two replies."""
    events = []
    for channel in ('imessage', 'whatsapp'):
        events += [{'at': NOW-timedelta(days=8), 'from_me': False, 'channel': channel,
                    'conversation_id': '+14155550100'},
                   {'at': NOW-timedelta(days=7), 'from_me': True, 'channel': channel,
                    'conversation_id': '+14155550100'},
                   {'at': NOW-timedelta(days=4), 'from_me': False, 'channel': channel,
                    'conversation_id': '+14155550100'},
                   {'at': NOW-timedelta(days=3), 'from_me': True, 'channel': channel,
                    'conversation_id': '+14155550100'}]
    result = ri.engagement_signals({'events': events}, {}, NOW)
    keys = [sample['conversation_key'] for sample in result['reply_samples']]
    assert len(result['reply_samples']) == 6 and len(set(keys)) == 2   # three turns per channel


def test_nothing_learned_survives_below_the_sample_floor():
    """A boost earned from a fuller past does not outlive the evidence: once the retained samples
    fall below REPLY_MIN_SAMPLES the learned values go, not just their expiry."""
    previous = {'reply_samples': [{'id': 'old', 'direction': 'owner', 'lag_days': 0.1,
                                   'started_at': (NOW-timedelta(days=59)).isoformat(),
                                   'replied_at': (NOW-timedelta(days=59)).isoformat()}],
                'attention_boost': 0.9, 'owner_reply_lag_days': 0.1,
                'attention_boost_expires': (NOW+timedelta(days=1)).isoformat(),
                'counterpart_reply_lag_days': 2,
                'counterpart_reply_lag_expires': (NOW+timedelta(days=1)).isoformat()}
    result = ri.engagement_signals({'events': []}, previous, NOW)
    for key in ('attention_boost', 'owner_reply_lag_days', 'counterpart_reply_lag_days'):
        assert key not in result
