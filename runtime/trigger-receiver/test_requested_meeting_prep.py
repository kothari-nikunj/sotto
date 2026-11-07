"""The real exact composer runs after bounded synthetic provider inputs, never a sweep."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
import requested_meeting_prep as runner

PACK = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'


@pytest.mark.parametrize("name_evidence", ["calendar", "contacts", "research", "gmail"])
def test_single_occurrence_depth_and_changed_calendar_refusal(tmp_path, monkeypatch, name_evidence):
    for path in ('_shared/lib', '_shared/scripts', 'meeting-prep/scripts', 'proactive/scripts'):
        monkeypatch.syspath_prepend(str(PACK / path))
    import source_context
    import compose_meeting_prep as composer
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'c'*32)
    revoked = []
    disabled = set()
    monkeypatch.setattr(source_context, 'allowed', lambda source: source != 'granola' and source not in disabled
                        and not (source == 'calendar' and revoked))
    local = {'contacts': [], 'imessage': [], 'source_status': {'contacts': 'ok'}}
    monkeypatch.setattr(source_context, 'read_local', lambda hours: local)
    # Real scanner identity enrichment, not a stubbed person matcher. Google often
    # omits displayName even though the accepted nudge knew the full name.
    person = 'Dana' if name_evidence == 'calendar' else 'Dana Example'
    import personal_context
    envelopes = []
    monkeypatch.setattr(personal_context, 'conversation_snapshot', lambda: envelopes)
    if name_evidence == 'contacts':
        local['contacts'] = [{'name': person, 'emails': ['dana@outside.com']}]
    if name_evidence == 'research':
        cache = tmp_path / 'cache' / ('research_' + datetime.now(timezone.utc).strftime('%Y-%m-%d') + '.json')
        cache.parent.mkdir()
        cache.write_text(json.dumps({'attendees': [{'email': 'dana@outside.com', 'full_name': person}]}))
    if name_evidence == 'gmail':
        envelopes.append(({'source': 'gmail', 'from': person + ' <dana@outside.com>'}, None, None))
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@fund.example')
    monkeypatch.setattr(composer.ledger_io, 'load_entries', lambda: [])
    start = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    offered = {'id': 'offered', 'summary': 'Dana planning', 'start': start,
               'attendees': [{'email': 'dana@outside.com', 'displayName': 'Dana'}]}
    if name_evidence != 'calendar':
        offered['attendees'][0].pop('displayName')
    offered['attendees'].append({'email': 'owner@fund.example', 'displayName': 'Owner'})
    extra = {**offered, 'id': 'unrelated'}
    current = [offered, extra]
    phases = []
    request = {'pack': str(PACK), 'offer': {'kind': 'meeting_prep', 'person': person,
               'offer_id': 'a'*64, 'meeting': {'event_id': 'offered', 'start': start}}}
    revoke_after_model = []
    revoke_during_research = []
    def command(script, *args, decode=True):
        phases.append(script)
        if script.endswith('gather_google.py'):
            if '--attendee-comms' in args:
                Path(args[args.index('--comms-out')+1]).write_text(json.dumps({'dana@outside.com': []}))
            else:
                Path(args[args.index('--gmail-out')+1]).write_text(json.dumps([]))
                Path(args[args.index('--cal-out')+1]).write_text(json.dumps(current))
                Path(args[args.index('--source-results-out')+1]).write_text(json.dumps({'calendar': {'status': 'ok'}}))
            return ''
        if script.endswith('knowledge_query.py'):
            assert [e['id'] for e in json.loads(Path(args[1]).read_text())] == ['offered']
            return {'person_knowledge': {}}
        if script.endswith('select_attendees.py'):
            return []  # Known focus attendee must still survive into research/comms.
        if script.endswith('persist_prep.py'):
            return {'kept': 1, 'skipped_fresh': 0} if '--filter-fresh' in args else {'status': 'ok'}
        if script.endswith('x_connectivity.py'):
            Path(args[args.index('--out')+1]).write_text(json.dumps({'attendees': []}))
            return ''
        if script.endswith('research_attendees.py'):
            assert args[args.index('--focus')+1] == person
            assert json.loads(Path(args[args.index('--comms')+1]).read_text()) == {'dana@outside.com': []}
            assert [e['id'] for e in json.loads(Path(args[args.index('--context')+1]).read_text())] == ['offered']
            if revoke_during_research:
                revoked.append(True)
            return {'attendees': [{'email': 'dana@outside.com', 'summary': 'Company planning context'}]}
        pytest.fail('unexpected provider command')
    def model(prompt, inputs):
        phases.append('meeting-prep-model')
        assert inputs['attendee_research']['attendees'] and inputs['attendee_comms']
        assert inputs['google']['userEmail'] == 'owner@fund.example'
        assert composer.require_exact_meeting(inputs) == [{'name': person, 'email': 'dana@outside.com'}]
        assert 'prior_knowledge' in inputs
        if revoke_after_model:
            revoked.append(True)
        return json.dumps({'prep_markdown': 'Specific Dana prep',
            'meetings': [{'event_id': 'offered', 'start': start,
                          'talking_points': ['Discuss their September launch and new enterprise pricing.']}]})
    monkeypatch.setattr(composer, 'call_gemini', model)
    assert runner.run(request, command) == 'Specific Dana prep'
    # Matching names attached to another email cannot rescue the selected occurrence.
    phases.clear()
    offered['attendees'][0] = {'email': 'someone-else@outside.com'}
    with pytest.raises(ValueError, match='focused meeting occurrence unavailable'):
        runner.run(request, command)
    assert phases == ['_shared/scripts/gather_google.py']
    offered['attendees'][0] = {'email': 'dana@outside.com'}
    if name_evidence == 'calendar':
        offered['attendees'][0]['displayName'] = person
    if name_evidence in ('contacts', 'gmail'):
        phases.clear()
        disabled.add(name_evidence)
        with pytest.raises(ValueError, match='focused meeting occurrence unavailable'):
            runner.run(request, command)
        assert phases == ['_shared/scripts/gather_google.py']
        disabled.clear()
    if name_evidence == 'gmail':
        phases.clear()
        envelopes.append(({'source': 'gmail', 'from': 'Another Person <dana@outside.com>'}, None, None))
        with pytest.raises(ValueError, match='focused meeting occurrence unavailable'):
            runner.run(request, command)
        assert phases == ['_shared/scripts/gather_google.py']
        envelopes.pop()
    effects = json.loads((tmp_path/'events'/('delivery-effects-'+'c'*32+'.json')).read_text())['effects']
    assert sum(e['kind'] == 'accepted_offer_clear' for e in effects) == 1
    assert sum(e['kind'] == 'meeting_prep_delivered' and e['mode'] == 'full' for e in effects) == 1
    phases.clear()
    current[:] = [extra]
    with pytest.raises(RuntimeError, match='occurrence unavailable'):
        runner.run(request, command)
    assert phases == ['_shared/scripts/gather_google.py']
    current[:] = [offered, extra]
    phases.clear()
    revoke_after_model.append(True)
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'd'*32)
    with pytest.raises(RuntimeError, match='source permission changed during prep composition'):
        runner.run(request, command)
    after = json.loads((tmp_path/'events'/('delivery-effects-'+'d'*32+'.json')).read_text())['effects']
    assert not any(e['kind'] == 'accepted_offer_clear' for e in after)
    revoked.clear()
    revoke_after_model.clear()
    revoke_during_research.append(True)
    phases.clear()
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'e'*32)
    with pytest.raises(RuntimeError, match='source permission changed during prep research'):
        runner.run(request, command)
    assert 'meeting-prep-model' not in phases
    assert not (tmp_path/'events'/('delivery-effects-'+'e'*32+'.json')).exists()
