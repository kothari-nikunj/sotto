"""Optional detail fits the existing gallery; help repair uses the existing model stages."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest
import compose_brief as cb
import visual_brief as visual


@pytest.mark.parametrize("layout_failure", [False, True])
def test_weekly_optional_names_share_quiet_detail_budget_and_preserve_required_content(tmp_path, monkeypatch, layout_failure):
    import retune_scan, ledger_io
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-hosted')
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    monkeypatch.setattr(cb, 'explicit_prefs', lambda: {})
    monkeypatch.setattr(ledger_io, 'load_entries', lambda: [{'status': 'parked'}, {'status': 'parked'}])
    rows = [{'anchor_key': f'weekly-{i}', 'status': 'open', 'contact_name': f'Robin Person{chr(65+i)}',
             'created_at': '2026-10-01', 'summary': 'Review the planning material and decide on the proposed next steps. ' * 5}
            for i in range(3)]
    scan = [{'anchor_key': row['anchor_key'], 'name': row['contact_name'], 'what': row['summary'],
             'times_surfaced': 60 if i == 0 else 1, 'surface_count_provenance': 'accepted_delivery'}
            for i, row in enumerate(rows)]
    monkeypatch.setattr(retune_scan, 'scan', lambda **kwargs: {'stale_loops': scan})
    md = ('# Friday\n## Needs Attention Now\n' + '\n'.join(
        f'**Alex Person{chr(65+i)}**: Review the source-backed proposal, preserve its separate deliverables and respond with a decision before tomorrow.'
        for i in range(4)) + '\n**Coming Up**\n' + '\n'.join(
        f'- Fri Oct 23 {i+8}:00 AM: Planning meeting' for i in range(5)) +
        '\n## Still open\n' + '\n'.join(
        f'- **Existing Person{chr(65+i)}**: Send the requested research document and confirm its next review date. <!--loop:mandatory-{i}-->'
        for i in range(10)) + '\n- 3 other open loops. Ask me what\'s still open.\n' +
        '## What moved today\n- Parks tomorrow unless you say *keep*: one existing request.\n## Filtered\n1 promotion\n')
    assert visual.fits(visual.build(md))
    offers = []
    import pending_offer
    monkeypatch.setattr(pending_offer, 'set_offer', lambda *args, **kwargs: offers.append((args, kwargs)))
    if layout_failure:
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic layout failure')
        monkeypatch.setattr(cb, '_optional_loop_detail_fits', fail)
    out = {'brief_markdown': md}
    before = deepcopy(rows)
    cb._append_weekly_review(out, {'type': 'evening', 'google': {'userTimezone': 'UTC'}},
                             datetime(2026, 10, 23, 18, tzinfo=timezone.utc), open_ledger=rows)
    if not layout_failure:
        assert visual.fits(visual.build(out['brief_markdown']))
    for i in range(10):
        assert f'<!--loop:mandatory-{i}-->' in out['brief_markdown']
    assert 'Parks tomorrow unless you say' in out['brief_markdown']
    assert '2 parked because' in out['brief_markdown']
    assert '60 brief deliveries. Keep it, snooze it, or let it go?' in out['brief_markdown']
    assert out['_review_question'] == 'weekly_repeated'
    assert '<!--loop:weekly-0-->' in out['brief_markdown']
    assert rows[0]['summary'] in out['brief_markdown']
    shown = sum(f'<!--loop:weekly-{i}-->' in out['brief_markdown'] for i in range(3))
    assert shown == 3 if layout_failure else shown < 3
    assert len(offers) == 1
    assert offers[0][0][1] in out['brief_markdown']
    if layout_failure:
        assert rows == before
        return
    # Recount after weekly selection; no optional task is silently lost or falsely named.
    cb._expand_quiet_loops(out, rows, today='2026-10-23')
    assert f'- {3-shown} other open loops.' in out['brief_markdown']
    assert rows == before
    assert len(out.get('_rendered_loop_lines', {})) == shown


def test_selected_help_coherence_reaches_single_revision_and_keeps_other_ask(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-hosted')
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    identifier = 'dana@example.test'
    bad = '**Dana Reed**<!--id:dana@example.test-->: Dana proposed a call next week.'
    full_ask = 'Confirm Monday at 11 AM or Tuesday at 2 PM for our call.'
    good = '**Dana Reed**<!--id:dana@example.test-->: ' + full_ask
    other = '**Alex Stone**<!--id:alex@example.test-->: Send the separate acquisition memo.'
    action = {'type': 'reply', 'channel': 'gmail', 'contactName': 'Reed, Dana',
              'contactIdentifier': identifier, 'contextSummary': 'Call scheduling',
              'contextAsk': full_ask, 'helpKind': 'draft', 'newObligation': True,
              'evidence': [{'sourceType': 'gmail', 'sourceId': 'mail-1', 'snippet': full_ask}]}
    inputs = {'now': '2026-10-23T18:00:00Z', 'google': {'emails': [{'id': 'mail-1', 'messageId': 'mail-1',
        'from': 'Dana Reed <dana@example.test>', 'body': full_ask, 'date': '2026-10-23T12:00:00Z'},
        {'id': 'mail-2', 'from': 'Alex Stone <alex@example.test>',
         'body': 'Send the separate acquisition memo.', 'date': '2026-10-23T12:00:00Z'}]}}
    calls = []
    def model(prompt, options, **kwargs):
        calls.append(options)
        if options.get('_critic'):
            assert 'help-offer-coherence:' in prompt
            return json.dumps({'patches': [], 'score': 90, 'summary': 'Repair selected request coherence.'})
        if options.get('_revise'):
            assert full_ask in prompt
            return json.dumps({'brief_markdown': '## Needs Attention Now\n' + good + '\n' + other,
                               'actions': [{**action, 'contactName': 'Dana Reed'}], 'loop_updates': []})
        return json.dumps({'brief_markdown': '## Needs Attention Now\n' + bad + '\n' + other,
                           'actions': [action], 'loop_updates': []})
    result = cb.compose(inputs, llm=model, critic=True)
    assert len(calls) == 3
    assert result['brief_markdown'].count('I can draft a reply.') == 1
    assert full_ask in result['brief_markdown'] and other in result['brief_markdown']
    assert result['actions'][0]['contactIdentifier'] == identifier
    assert result['actions'][0]['contextAsk'] == full_ask
    assert cb._help_offer_coherence_violations(result, {identifier}) == []


@pytest.mark.parametrize('case', ['first_name', 'group', 'birthday', 'wrong_identity', 'partial_ask'])
def test_unsupported_offers_remain_absent(case):
    name = 'Dana Reed';identifier = 'dana@example.test';ask = 'Send the complete diligence report.'
    if case == 'first_name':
        name = 'Dana'
    if case == 'group':
        identifier = 'any;+;group'
    line = f'**{name}**<!--id:{identifier}-->: ' + ask
    if case == 'birthday':
        line = f'**{name}**<!--id:{identifier}-->: Birthday today.'
    if case == 'partial_ask':
        line = f'**{name}**<!--id:{identifier}-->: Send the report.'
    action = {'type': 'reply', 'helpKind': 'draft', 'contactName': name,
              'contactIdentifier': identifier, 'contextAsk': ask}
    if case == 'wrong_identity':
        action['contactIdentifier'] = 'other@example.test'
    out = {'brief_markdown': '## Needs Attention Now\n' + line, 'actions': [action]}
    cb._add_help_offers(out, {identifier})
    assert 'I can draft a reply.' not in out['brief_markdown']
    if case != 'partial_ask':
        assert cb._help_offer_coherence_violations(out, {identifier}) == []


def test_trimmed_weekly_generic_question_uses_visible_target(monkeypatch):
    monkeypatch.setattr(cb, '_optional_loop_detail_fits', lambda *args, **kwargs: False)
    result = cb._budget_weekly_review_details('## Still open\n- Existing task.\n',
        ['- Optional request. <!--loop:optional-->'],
        ['Keep, snooze or dismiss any of these when you are ready.'], 1)
    assert 'Keep, snooze or dismiss any of the unfinished tasks above when you are ready.' in result
    assert not any('<!--loop:optional-->' in line for line in result)


def test_prep_on_nonmeeting_requests_none_without_inventing_calendar():
    out = {'brief_markdown': '## Needs Attention Now\n**Dana Reed**<!--id:dana@example.test-->: Send the report.',
           'actions': [{'type': 'reply', 'helpKind': 'prep', 'contactName': 'Dana Reed',
                        'contactIdentifier': 'dana@example.test', 'contextAsk': 'Send the full report tomorrow.'}]}
    findings = cb._help_offer_coherence_violations(out, {'dana@example.test'})
    assert len(findings) == 1 and 'non-meeting action' in findings[0]
    assert 'If no supported calendar action exists, use helpKind none' in findings[0]
    out['actions'][0]['helpKind'] = 'none'
    assert cb._help_offer_coherence_violations(out, {'dana@example.test'}) == []


def test_separate_same_person_asks_and_multimarker_line_are_coherent():
    asks = ['Send the complete diligence report.', 'Confirm the separate review date.']
    actions = [{'type': 'reply', 'helpKind': 'draft', 'contactName': 'Dana Reed',
                'contactIdentifier': 'dana@example.test', 'contextAsk': ask} for ask in asks]
    actions.append({'type': 'reply', 'helpKind': 'draft', 'contactName': 'Alex Stone',
                    'contactIdentifier': 'alex@example.test', 'contextAsk': 'Send the acquisition memo.'})
    lines = ['**Dana Reed**<!--id:dana@example.test--><!--id:alex@example.test-->: ' + asks[0],
             '**Dana Reed**<!--id:dana@example.test-->: ' + asks[1],
             '**Alex Stone**<!--id:alex@example.test-->: Send the acquisition memo.']
    out = {'brief_markdown': '## Needs Attention Now\n' + '\n'.join(lines), 'actions': actions}
    assert cb._help_offer_coherence_violations(out, {'dana@example.test', 'alex@example.test'}) == []
    before = deepcopy(out)
    cb._add_help_offers(out, {'dana@example.test', 'alex@example.test'})
    assert out['actions'] == before['actions']
    for line in lines:
        assert line in out['brief_markdown']
    # One missing action yields one finding, regardless of how many other marked lines exist.
    actions[1]['contextAsk'] = 'Return the separate signed agreement.'
    findings = cb._help_offer_coherence_violations(out, {'dana@example.test', 'alex@example.test'})
    assert len(findings) == 1
