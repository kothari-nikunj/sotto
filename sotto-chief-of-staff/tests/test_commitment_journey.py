"""Chronological email promise -> text delivery -> exact closure -> consumer suppression."""
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json

import pytest

import compose_brief
import continuity_resolve as cr
import ledger_io
import loops_query
import proactive_scan
from delivery_effects import loop_version
from ledger_provenance import classify_ledger_sources, digest_frontmatter, source_loop_sources
from commitment_journey_fixture import (CLOSE_AT, DELIVERY, IDENTIFIER, PHONE, PROMISE,
                                        capture_payload, reviewed_proposal, run_journey, seed_capture)


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-hosted')
    monkeypatch.delenv('SOTTO_PROACTIVE_CAPTURED_SOURCES', raising=False)


@pytest.mark.parametrize('stamped', [False, True])
def test_email_promise_later_text_closes_exact_ask_and_suppresses_brief_and_reminder(tmp_path, monkeypatch, stamped):
    journey = run_journey(tmp_path, stamped=stamped)
    closed = next(row for row in journey['rows'] if row['status'] == 'resolved')
    pending = next(row for row in journey['rows'] if row['status'] == 'open')
    assert closed['summary'] == 'Send the pricing document'
    assert pending['summary'] == 'Send the launch checklist'
    assert closed['created_at'] == '2026-10-18T09:00:00+00:00'
    assert closed['closed_at'] == '2026-10-19T08:00:00+00:00'
    assert closed['source_thread_id'] == PROMISE['threadId']
    assert closed['source_refs'][0]['sourceId'] == PROMISE['id']
    assert closed['resolution_source_refs'] == journey['completion_proposal']['evidence']
    assert source_loop_sources(closed) == {'gmail', 'imessage'}
    if stamped:
        assert classify_ledger_sources(closed) == ('sources', frozenset({'gmail', 'imessage'}))
    else:
        assert 'content_provenance' not in closed

    # A new pass reloads the actual files; original promise replay cannot recreate paid debt.
    before_restart = deepcopy(cr._load_items())
    replay = cr.resolve(journey['capture_payload'], CLOSE_AT)
    assert {row['anchor_key'] for row in replay['active']} == {pending['anchor_key']}
    assert cr._load_items()[closed['anchor_key']] == before_restart[closed['anchor_key']]

    permitted = loops_query.query_permitted_rows(str(tmp_path), ['gmail', 'imessage'], now=CLOSE_AT)
    assert [row['anchor_key'] for row in permitted['you_owe']] == [pending['anchor_key']]
    brief = compose_brief._protected_action_ledger({'now': CLOSE_AT.isoformat()})
    assert [row['anchor_key'] for row in brief] == [pending['anchor_key']]
    monkeypatch.setenv('SOTTO_PROACTIVE_CAPTURED_SOURCES', '["gmail","imessage"]')
    due = proactive_scan._open_loops()
    assert [row['anchor_key'] for row in due] == [pending['anchor_key']]
    nudges = proactive_scan.scan([], due, {}, 'owner@example.test', CLOSE_AT)['nudges']
    assert [row['anchor_key'] for row in nudges if row['kind'] == 'commitment'] == [pending['anchor_key']]
    # Historical evidence remains available only while BOTH source grants are present.
    assert closed['anchor_key'] in {row['anchor_key'] for row in loops_query.permitted_rows(
        str(tmp_path), ['gmail', 'imessage'])}
    for grants in (['gmail'], ['imessage'], []):
        assert closed['anchor_key'] not in {row['anchor_key'] for row in loops_query.permitted_rows(str(tmp_path), grants)}


@pytest.mark.parametrize('enabled', [['imessage'], ['gmail']])
def test_origin_and_closing_source_must_still_be_authorized(tmp_path, monkeypatch, enabled):
    _payload, rows = seed_capture(tmp_path)
    row = rows['Send the pricing document']
    update = reviewed_proposal(row)
    monkeypatch.setenv('SOTTO_PROACTIVE_CAPTURED_SOURCES', json.dumps(enabled))
    result = cr.resolve({'today': '2026-10-19', 'local': {'imessage': [DELIVERY]},
                         'loop_updates': [update]}, CLOSE_AT)
    assert not result['resolved'] and len(result['active']) == 2
    assert cr._load_items()[row['anchor_key']]['status'] == 'open'


@pytest.mark.parametrize('text', ['I have not sent the pricing document yet.',
                                 'I might have sent something; please check.',
                                 'Hi Maya, how are you?',
                                 'The first pricing table is ready; the document is incomplete.'])
def test_negative_ambiguous_unrelated_or_partial_text_has_no_unreviewed_closure(tmp_path, text):
    _payload, rows = seed_capture(tmp_path)
    row = rows['Send the pricing document']
    message = {**DELIVERY, 'text': text}
    update = reviewed_proposal(row, message)
    update.pop('criticReceipt')
    result = cr.resolve({'today': '2026-10-19', 'local': {'imessage': [message]},
                         'loop_updates': [update]}, CLOSE_AT)
    assert not result['resolved'] and len(result['active']) == 2


def test_same_display_name_is_not_cross_channel_identity(tmp_path):
    _payload, rows = seed_capture(tmp_path)
    row = rows['Send the pricing document']
    stranger = {**DELIVERY, 'handle': '+14155550999', 'contact_name': 'Maya Journey'}
    update = reviewed_proposal(row, stranger)
    result = cr.resolve({'today': '2026-10-19', 'local': {'imessage': [stranger]},
                         'loop_updates': [update]}, CLOSE_AT)
    assert not result['resolved'] and len(result['active']) == 2


@pytest.mark.parametrize('sent', [False, True])
@pytest.mark.parametrize('markers', [
    {'labelIds': ['DRAFT']}, {'labelIds': ['SENT', 'draft']}, {'labels': ['draft']},
    {'labelIds': ['SENT'], 'labels': ['DRAFT']}, {'labelIds': 'SENT'},
    {'labelIds': ['SENT', None]},
])
def test_draft_email_never_supplies_completion_even_with_critic_approval(tmp_path, sent, markers):
    _payload, rows = seed_capture(tmp_path)
    row = rows['Send the pricing document']
    draft = {'id': 'unsent-draft', 'from': 'owner@example.test', 'to': IDENTIFIER,
             'isSent': sent, **markers, 'date': '2026-10-19T08:00:00Z',
             'body': 'Here is the complete pricing document.'}
    update = {'loopId': row['anchor_key'], 'loopVersion': loop_version(row),
              'status': 'resolved', 'evidence': [{'sourceType': 'email', 'sourceId': draft['id'],
                                                  'snippet': draft['body']}]}
    update['criticReceipt'] = cr.record_critic_approval(update)
    result = cr.resolve({'today': '2026-10-19', 'local': {'emails': [draft]},
                         'loop_updates': [update]}, CLOSE_AT)
    assert not result['resolved'] and len(result['active']) == 2


@pytest.mark.parametrize('markers', [
    {'labelIds': ['DRAFT']}, {'labelIds': ['SENT', 'draft']},
    {'labelIds': ['SENT'], 'labels': ['DRAFT']}, {'labelIds': 'DRAFT'},
])
def test_draft_cannot_capture_or_reopen_a_fresh_request_after_completion(tmp_path, markers):
    journey = run_journey(tmp_path)
    closed = next(row for row in journey['rows'] if row['status'] == 'resolved')
    draft = {**PROMISE, 'id': 'new-draft', 'date': '2026-10-20T08:00:00Z',
             'from': IDENTIFIER, 'to': 'owner@example.test', 'isSent': False,
             'body': 'Please send the revised pricing document.', **markers}
    action = {'type': 'reply', 'channel': 'email', 'contactName': 'Maya Journey',
              'contactIdentifier': IDENTIFIER, 'contextSummary': 'Send revised pricing document',
              'loopId': closed['anchor_key'], 'emailMessageId': draft['id'],
              'evidence': [{'sourceType': 'email', 'sourceId': draft['id'], 'snippet': draft['body']}]}
    at = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
    for separate in (False, True):
        action['newObligation'] = separate
        cr.resolve({'today': '2026-10-20', 'local': {'emails': [draft]}, 'new_actions': [action]}, at)
        assert cr._load_items()[closed['anchor_key']] == closed
        assert len(cr._load_items()) == 2


@pytest.mark.parametrize('future_first', [False, True])
def test_every_required_evidence_timestamp_is_checked_independent_of_order(tmp_path, future_first):
    _payload, rows = seed_capture(tmp_path)
    row = rows['Send the pricing document']
    future = {**DELIVERY, 'id': 'future-text', 'timestamp': '2026-10-20T08:00:00Z'}
    update = reviewed_proposal(row)
    update['evidence'].append({'sourceType': 'imessage', 'sourceId': future['id'], 'snippet': future['text']})
    if future_first:
        update['evidence'].reverse()
    update['criticReceipt'] = cr.record_critic_approval(update)
    preview = {'loop_updates': [update]}
    still_open = compose_brief._project_loop_updates(preview,
        {'now': CLOSE_AT.isoformat(), 'local': {'imessage': [DELIVERY, future]}}, list(rows.values()))
    assert {entry['anchor_key'] for entry in still_open} == {entry['anchor_key'] for entry in rows.values()}
    assert preview['_loop_projection']['resolved'] == []
    assert preview['_loop_projection']['rejected_by_reason'] == {'future_evidence': 1}
    result = cr.resolve({'today': '2026-10-19', 'local': {'imessage': [DELIVERY, future]},
                         'loop_updates': [update]}, CLOSE_AT)
    assert not result['resolved'] and len(result['active']) == 2


@pytest.mark.parametrize('capture_zone,read_zone,promise_at,delivery_at,completed', [
    ('America/Los_Angeles','UTC','2026-10-18T09:00:00Z','2026-10-18T08:30:00Z',False),
    ('America/Los_Angeles','UTC','2026-10-18T09:00:00Z','2026-10-18T09:30:00Z',True),
    ('UTC','America/Los_Angeles','2026-10-18T09:00:00Z','2026-10-18T09:30:00Z',True),
    ('America/Los_Angeles','America/Los_Angeles','2026-11-01T01:30:00-08:00','2026-11-01T01:15:00-08:00',False),
    ('America/Los_Angeles','America/Los_Angeles','2026-11-01T01:30:00-08:00','2026-11-01T01:45:00-08:00',True),
    ('UTC','UTC','2026-10-18T09:00:00.900000Z','2026-10-18T09:00:00.500000Z',False),
])
def test_source_capture_preserves_instant_across_timezone_fold_and_precision(
        tmp_path,monkeypatch,capture_zone,read_zone,promise_at,delivery_at,completed):
    promise_time=datetime.fromisoformat(promise_at.replace('Z','+00:00'))
    clock=promise_time.astimezone(timezone.utc)+timedelta(hours=2)
    payload=capture_payload()
    payload['today']=promise_time.date().isoformat()
    payload['local']['emails'][0]['date']=promise_at
    monkeypatch.setenv('SOTTO_TIMEZONE',capture_zone)
    _payload,rows=seed_capture(tmp_path,payload=payload,capture_at=clock-timedelta(hours=1))
    row=rows['Send the pricing document']
    message={**DELIVERY,'timestamp':delivery_at}
    monkeypatch.setenv('SOTTO_TIMEZONE',read_zone)
    update=reviewed_proposal(row,message)
    preview={'loop_updates':[update]}
    visible=compose_brief._project_loop_updates(preview,
        {'now':clock.isoformat(),'local':{'imessage':[message]}},list(rows.values()))
    result=cr.resolve({'today':clock.date().isoformat(),'local':{'imessage':[message]},
                       'loop_updates':[update]},clock)
    assert bool(result['resolved']) is completed
    assert len(result['active'])==len(visible)==(1 if completed else 2)
    captured=datetime.fromisoformat(row['created_at'])
    assert captured.tzinfo is not None
    assert captured.astimezone(timezone.utc)==promise_time.astimezone(timezone.utc)
    if not completed:
        assert preview['_loop_projection']['rejected_by_reason']=={'time_mismatch':1}
    # A timezone change must not rewrite the original task or its source proof.
    retained=cr._load_items()[row['anchor_key']]
    assert retained['created_at']==row['created_at']
    assert retained['source_refs']==row['source_refs']


@pytest.mark.parametrize('latest_first', [False, True])
def test_completion_time_is_latest_required_message_independent_of_order(tmp_path, latest_first):
    _payload, rows = seed_capture(tmp_path)
    row = rows['Send the pricing document']
    latest = {**DELIVERY, 'id': 'latest-text', 'timestamp': '2026-10-19T08:30:00Z'}
    update = reviewed_proposal(row)
    update['evidence'].append({'sourceType': 'imessage', 'sourceId': latest['id'], 'snippet': latest['text']})
    if latest_first:
        update['evidence'].reverse()
    update['criticReceipt'] = cr.record_critic_approval(update)
    preview = {'loop_updates': [update]}
    still_open = compose_brief._project_loop_updates(preview,
        {'now': CLOSE_AT.isoformat(), 'local': {'imessage': [DELIVERY, latest]}}, list(rows.values()))
    assert [entry['summary'] for entry in still_open] == ['Send the launch checklist']
    assert preview['_loop_projection']['resolved'] == [row['anchor_key']]
    result = cr.resolve({'today': '2026-10-19', 'local': {'imessage': [DELIVERY, latest]},
                         'loop_updates': [update]}, CLOSE_AT)
    assert result['resolved'][0]['closed_at'] == '2026-10-19T08:30:00+00:00'


def _stamped_capture(tmp_path):
    _payload, rows = seed_capture(tmp_path)
    row = rows['Send the pricing document']
    row['content_provenance'] = {'version': 1, 'kind': 'sources', 'sources': ['gmail'],
                                 'digest': digest_frontmatter(row)}
    cr._persist(row)
    return cr._load_items()[row['anchor_key']]


def test_stale_stamped_original_cannot_be_healed_by_automatic_resolution(tmp_path):
    row = _stamped_capture(tmp_path)
    row['summary'] = 'Edited source content after its proof'
    cr._persist(row)
    update = reviewed_proposal(row)
    result = cr.resolve({'today': '2026-10-19', 'local': {'imessage': [DELIVERY]},
                         'loop_updates': [update]}, CLOSE_AT)
    assert not result['resolved']
    retained = cr._load_items()[row['anchor_key']]
    assert retained['status'] == 'open'
    with pytest.raises(ValueError, match='stale content provenance'):
        classify_ledger_sources(retained)


def test_recapture_touch_preserves_valid_source_provenance_without_rewriting_content(tmp_path):
    row = _stamped_capture(tmp_path)
    action = {'type': 'follow_up', 'channel': 'email', 'contactName': 'Maya Journey',
        'contactIdentifier': IDENTIFIER, 'loopId': row['anchor_key'],
        'contextSummary': 'Fresh conversational presentation', 'contextAsk': 'do this today',
        'deadlineDate': '2026-10-20'}
    cr.resolve({'today': '2026-10-19', 'new_actions': [action]}, CLOSE_AT)
    retained = cr._load_items()[row['anchor_key']]
    assert retained['summary'] == row['summary']
    assert retained['ask'] == row['ask'] and retained['deadline'] == row['deadline']
    assert retained['source_brief_at'] != row['source_brief_at']
    assert classify_ledger_sources(retained) == ('sources', frozenset({'gmail'}))


@pytest.mark.parametrize('field,value', [('summary', 'Different task'),
    ('contact_identifier', '+14155550999'), ('source_refs', [{'sourceType': 'email', 'sourceId': 'other-mail'}])])
def test_audited_resolution_writer_rejects_changes_to_task_identity_or_original_evidence(tmp_path, field, value):
    row = _stamped_capture(tmp_path)
    update = reviewed_proposal(row)
    proof, reason = cr._completion_evidence_result(row, update, {'imessage': [DELIVERY]})
    assert proof, reason
    candidate = deepcopy(row)
    cr._terminate(candidate, 'resolved', 'replied', '2026-10-19', now=CLOSE_AT)
    candidate.update(closed_at='2026-10-19T08:00:00+00:00', resolution_evidence=proof['description'],
                     resolution_source_refs=update['evidence'], resolution_loop_version=update['loopVersion'],
                     resolution_reason=update['reason'])
    candidate[field] = value
    with pytest.raises(ValueError, match='unrelated content'):
        cr._persist(candidate, source_resolution={'update': update, 'local': {'imessage': [DELIVERY]},
            'person_index': cr.person_identity(), 'now': CLOSE_AT, 'today': '2026-10-19'})
    assert cr._load_items()[row['anchor_key']] == row


def test_audited_resolution_writer_refuses_explicit_source_row(tmp_path):
    row = _stamped_capture(tmp_path)
    row['resolution_mode'] = 'explicit'
    row['content_provenance']['digest'] = digest_frontmatter(row)
    cr._persist(row)
    update = reviewed_proposal(row)
    with pytest.raises(ValueError, match='revision is invalid'):
        cr._persist(row, source_resolution={'update': update, 'local': {'imessage': [DELIVERY]},
            'person_index': cr.person_identity(), 'now': CLOSE_AT, 'today': '2026-10-19'})
    assert cr._load_items()[row['anchor_key']] == row


def test_closure_retention_policy_is_unchanged(tmp_path):
    journey = run_journey(tmp_path)
    closed = journey['result']['resolved'][0]
    later = CLOSE_AT + timedelta(days=32)
    cr.resolve({'today': later.date().isoformat()}, later)
    assert closed['anchor_key'] not in cr._load_items()


def _capture_with_precise_asks(tmp_path):
    payload = capture_payload()
    for action, task in zip(payload['new_actions'], ['pricing document', 'launch checklist']):
        action.update(contextSummary=f'Promised {task} delivery', contextAsk=f'send the {task} tomorrow')
    return seed_capture(tmp_path, payload=payload)[1]


def test_reviewed_full_ask_on_verified_channel_does_not_get_still_open_duplicate(tmp_path):
    rows = _capture_with_precise_asks(tmp_path)
    pricing = rows['Promised pricing document delivery']
    checklist = rows['Promised launch checklist delivery']
    update = reviewed_proposal(pricing)
    out = {'brief_markdown': ('## Should Handle Today\n\n'
        f'**Maya Journey**<!--id:{PHONE}|ch:imessage--> - You sent the pricing document this morning '
        f'<!--loop:{pricing["anchor_key"]}-->, but you still need to send the launch checklist '
        f'<!--loop:{checklist["anchor_key"]}-->. I can draft a reply.\n\n## Filtered\n0 items filtered'),
        'loop_updates': [update]}
    inputs = {'now': CLOSE_AT.isoformat(), 'local': {'imessage': [DELIVERY]}}
    mentions = compose_brief._task_mention_review(out, inputs)
    mention = next(item for item in mentions.values() if item['loop_id'] == checklist['anchor_key'])
    assert mention['identity_proven']
    assert mention['current_conversation']
    # Explicit offline semantic-review acceptance, bound to the production candidate.
    out['_reviewed_mentions'] = {checklist['anchor_key']: (mention['loop_version'], mention['displayed_line'])}
    projected = compose_brief._project_loop_updates(out, inputs, list(rows.values()))
    assert [row['anchor_key'] for row in projected] == [checklist['anchor_key']]
    compose_brief._append_still_open(out, projected, CLOSE_AT, '2026-10-19')
    assert '## Still open' not in out['brief_markdown']
    assert out['brief_markdown'].count(f'<!--loop:{checklist["anchor_key"]}-->') == 1


def test_unverified_channel_cannot_claim_a_source_bound_display_review(tmp_path):
    row = _capture_with_precise_asks(tmp_path)['Promised launch checklist delivery']
    out = {'brief_markdown': ('## Should Handle Today\n'
        '**Maya Journey**<!--id:+14155550999|ch:imessage--> - Send the launch checklist '
        f'<!--loop:{row["anchor_key"]}-->.')}
    mentions = compose_brief._task_mention_review(out,
        {'now': CLOSE_AT.isoformat(), 'local': {'imessage': [DELIVERY]}})
    assert not next(iter(mentions.values()))['identity_proven']


def test_recapture_preserves_original_task_and_dates_across_three_briefs(tmp_path):
    rows = _capture_with_precise_asks(tmp_path)
    pricing = rows['Promised pricing document delivery']
    checklist = rows['Promised launch checklist delivery']
    update = reviewed_proposal(pricing)
    action = {'type': 'follow_up', 'channel': 'email', 'contactName': 'Maya Journey',
        'contactIdentifier': IDENTIFIER, 'loopId': checklist['anchor_key'],
        'contextSummary': 'Sent the pricing document this morning, but still need to send the launch checklist.',
        'contextAsk': 'owe the launch checklist due today', 'deadlineDate': '2026-10-20',
        'evidence': [{'sourceType': 'imessage', 'sourceId': DELIVERY['id'],
                      'snippet': 'The launch checklist is still pending.'}]}
    result = cr.resolve({'today': '2026-10-19', 'new_actions': [action],
        'local': {'imessage': [DELIVERY]}, 'loop_updates': [update]}, CLOSE_AT)
    pending, = result['active']
    canonical = ('summary', 'ask', 'deadline', 'source_refs', 'source_thread_id', 'source_message_id', 'created_at')
    assert {key: pending.get(key) for key in canonical} == {key: checklist.get(key) for key in canonical}
    assert pending['created_at'] == '2026-10-18T09:00:00+00:00'
    assert pending['source_refs'] == checklist['source_refs']
    closed, = result['resolved']
    assert closed['closed_at'] == '2026-10-19T08:00:00+00:00'
    assert closed['resolution_source_refs'] == update['evidence']
    # Next-day protected context contains the pending task only, so a capture date
    # cannot be presented as the date of the sibling's separately evidenced completion.
    next_day = CLOSE_AT + timedelta(days=1)
    recaptured = cr.resolve({'today': next_day.date().isoformat(), 'new_actions': [action]}, next_day)
    assert {key: recaptured['active'][0].get(key) for key in canonical} == {
        key: checklist.get(key) for key in canonical}
    protected = compose_brief._protected_action_ledger({'now': next_day.isoformat()})
    assert [row['summary'] for row in protected] == [checklist['summary']]
    line = compose_brief._open_loop_line(protected, next_day)
    assert 'pricing' not in line and 'this morning' not in line
    assert checklist['summary'] in line and 'open since Oct 18' in line
    assert protected[0]['ask'] == 'send the launch checklist tomorrow'
    assert protected[0]['deadline'] == '2026-10-19'


def test_explicit_owner_correction_and_deadline_writers_remain_authoritative(tmp_path):
    import knowledge_edit
    rows = _capture_with_precise_asks(tmp_path)
    checklist = rows['Promised launch checklist delivery']
    knowledge_edit.op_loop_deadline(checklist['anchor_key'], '2026-10-22')
    # Today's capture cannot undo an explicit date correction with its original date.
    replay = capture_payload()
    cr.resolve(replay, CLOSE_AT)
    assert cr._load_items()[checklist['anchor_key']]['deadline'] == '2026-10-22'
    correction = knowledge_edit.op_loop_retarget(checklist['anchor_key'],
        'Maya will send the revised launch checklist', contact='Maya Journey',
        identifier=IDENTIFIER, deadline='2026-10-23', direction='them', now=CLOSE_AT)
    corrected = cr._load_items()[correction['anchor_key']]
    assert corrected['summary'] == 'Maya will send the revised launch checklist'
    assert corrected['deadline'] == '2026-10-23'
    assert corrected['action_type'] == 'waiting_on'
    assert corrected['source'] == 'user_correction'
    assert corrected['created_at'] == '2026-10-19 09:00:00'
    assert cr._load_items()[checklist['anchor_key']]['resolution'] == 'user_retargeted'


@pytest.mark.parametrize('day,overdue', [('2026-10-18', 0), ('2026-10-19', 0), ('2026-10-20', 1)])
def test_prompt_and_review_share_absolute_deadline_independent_of_task_age(tmp_path, day, overdue):
    from datetime import datetime, timezone
    rows = _capture_with_precise_asks(tmp_path)
    row = rows['Promised launch checklist delivery']
    clock = datetime.fromisoformat(day).replace(hour=9, tzinfo=timezone.utc)
    inputs = {'now': clock.isoformat(), 'local': {'imessage': [DELIVERY]}}
    manifest = compose_brief.build_data_manifest(inputs)
    assert f'deadline: 2026-10-19 | days_overdue: {overdue}' in manifest['open_commitments']
    line = (f'**Maya Journey**<!--id:{IDENTIFIER}|ch:email--> - Send the launch checklist, '
            f'deadline October 19.<!--loop:{row["anchor_key"]}-->')
    out = {'brief_markdown': '## Should Handle Today\n' + line}
    mention = next(item for item in compose_brief._task_mention_review(out, inputs).values()
                   if item['loop_id'] == row['anchor_key'])
    assert mention['recorded_at'] == '2026-10-18T09:00:00+00:00'
    assert mention['deadline'] == '2026-10-19' and mention['days_overdue'] == overdue
    if day > '2026-10-18':
        hint = compose_brief.brief_validate._dropped_historical_task_hint(row, day)
        assert 'absolute deadline 2026-10-19' in hint
        assert f'days overdue: {overdue}' in hint
        assert 'Saved relative words may be paraphrased' in hint
    # Relative/unknown deadline prose never falls back to the request's age.
    assert ledger_io.deadline_context({**row, 'deadline': 'Friday'}, day) == {
        'deadline': None, 'days_overdue': None}


def test_historical_narrative_and_reviewed_absolute_date_survive_postprocessing(tmp_path):
    from delivery_effects import loop_version
    rows = _capture_with_precise_asks(tmp_path)
    row = rows['Promised launch checklist delivery']
    marker = f'<!--loop:{row["anchor_key"]}-->'
    for prose in (
        f'You {row["ask"]}{marker}.',
        f'From Oct 18: {row["summary"]} — {row["ask"]}{marker}.',
        f'Send the launch checklist, due October 19 and one day overdue.{marker}',
    ):
        out = {'brief_markdown': '## Should Handle Today\n'
            f'**Maya Journey**<!--id:{IDENTIFIER}|ch:email--> - {prose}'}
        original = deepcopy(out)
        assert compose_brief._date_marked_historical_requests(out, [row], CLOSE_AT + timedelta(days=1)) == original
    line = out['brief_markdown'].splitlines()[-1]
    out['_reviewed_mentions'] = {row['anchor_key']: (loop_version(row), line)}
    compose_brief._date_marked_historical_requests(out, [row], CLOSE_AT + timedelta(days=1))
    compose_brief._append_still_open(out, [row], CLOSE_AT + timedelta(days=1), '2026-10-20')
    assert '## Still open' not in out['brief_markdown']
    assert out['brief_markdown'].splitlines()[-1] == line


@pytest.mark.parametrize('kind', ['verified_alias', 'stranger', 'ambiguous_alias'])
def test_action_recipient_validator_uses_exact_verified_alias_ownership(tmp_path, kind):
    import knowledge
    _payload, rows = seed_capture(tmp_path)
    row = rows['Send the launch checklist']
    claimed = PHONE if kind != 'stranger' else '+14155550999'
    if kind == 'ambiguous_alias':
        knowledge.write_person_file(str(tmp_path / 'knowledge/people/c_abc194.md'), knowledge.PersonFile(
            name='Maya Journey', canonical_id='c_abc194', identifiers=[PHONE, 'other@example.test']))
    recipients = compose_brief._verified_loop_recipients(list(rows.values()), {'imessage': [DELIVERY]})
    action = {'loopId': row['anchor_key'], 'contactIdentifier': claimed}
    violations = compose_brief.brief_validate._check_action_loop_recipients(
        [action], list(rows.values()), recipients)
    assert bool(violations) is (kind != 'verified_alias')
    # An arbitrary model field cannot grant identity when caller proof is absent.
    action['verified_loop_recipients'] = {row['anchor_key']: [claimed]}
    assert compose_brief.brief_validate._check_action_loop_recipients([action], list(rows.values()))


@pytest.mark.parametrize('change', ['unknown_primary', 'wrong_canonical_id', 'group'])
def test_optional_alias_context_cannot_override_unknown_identity_or_group(tmp_path, change):
    _payload, rows = seed_capture(tmp_path)
    row = deepcopy(rows['Send the launch checklist'])
    if change == 'unknown_primary':
        row['contact_identifier'] = 'stranger@example.test'
    elif change == 'wrong_canonical_id':
        row['canonical_id'] = 'c_abc194'
    else:
        row['group_id'] = 'iMessage;+;a-distinct-group'
    recipients = compose_brief._verified_loop_recipients([row], {'imessage': [DELIVERY]})
    assert cr.alias_identifier(PHONE) not in recipients.get(row['anchor_key'], set())


def test_optional_alias_failure_falls_back_per_row_to_primary_identity(tmp_path, monkeypatch):
    _payload, rows = seed_capture(tmp_path)
    pricing = rows['Send the pricing document']
    checklist = rows['Send the launch checklist']
    # The eval harness can reload the resolver during test collection. Patch the
    # dependency compose actually imports, rather than a stale module reference.
    continuity = compose_brief._continuity()
    original = continuity._verified_identifiers
    visited = []
    def aliases(row, local, people):
        visited.append(row['anchor_key'])
        if row['anchor_key'] == pricing['anchor_key']:
            raise ValueError('malformed optional contact')
        return original(row, local, people)
    monkeypatch.setattr(continuity, '_verified_identifiers', aliases)
    recipients = compose_brief._verified_loop_recipients(list(rows.values()), {'imessage': [DELIVERY]})
    assert visited == [pricing['anchor_key'], checklist['anchor_key']]
    assert pricing['anchor_key'] not in recipients
    assert cr.alias_identifier(PHONE) in recipients[checklist['anchor_key']]
    assert not compose_brief.brief_validate._check_action_loop_recipients(
        [{'loopId': pricing['anchor_key'], 'contactIdentifier': IDENTIFIER}], list(rows.values()), recipients)
    assert compose_brief.brief_validate._check_action_loop_recipients(
        [{'loopId': pricing['anchor_key'], 'contactIdentifier': PHONE}], list(rows.values()), recipients)


def test_approved_verified_cross_channel_ask_needs_no_false_revision(tmp_path, monkeypatch):
    journey = run_journey(tmp_path)
    row = journey['result']['active'][0]
    monkeypatch.setenv('SOTTO_CRITIC', 'always')
    line = (f'**Maya Journey**<!--id:{IDENTIFIER}|ch:email--> - Send the launch checklist today. '
            f'<!--loop:{row["anchor_key"]}-->')
    draft = {'brief_markdown': '## Should Handle Today\n' + line, 'loop_updates': [],
        'actions': [{'type': 'follow_up', 'channel': 'imessage', 'contactName': 'Maya Journey',
            'contactIdentifier': PHONE, 'loopId': row['anchor_key'], 'newObligation': False,
            'contextSummary': 'Checklist delivery remains outstanding',
            'contextAsk': 'Send the launch checklist today.', 'helpKind': 'none',
            'evidence': [{'sourceType': 'imessage', 'sourceId': DELIVERY['id'],
                          'snippet': 'The launch checklist is still pending.'}]}]}
    inputs = {'type': 'morning', 'now': CLOSE_AT.isoformat(), 'local': {'imessage': [DELIVERY]}}
    mentions = compose_brief._task_mention_review(draft, inputs)
    candidate = next(item for item in mentions.values() if item['loop_id'] == row['anchor_key'])
    assert candidate['identity_proven']
    calls, checked_contexts = [], []
    original_validate = compose_brief.brief_validate.validate
    def validate(*args, **kwargs):
        checked_contexts.append(deepcopy(kwargs.get('verified_loop_recipients')))
        return original_validate(*args, **kwargs)
    monkeypatch.setattr(compose_brief.brief_validate, 'validate', validate)
    def llm(prompt, options, **kwargs):
        calls.append('critic' if options.get('_critic') else 'revise' if options.get('_revise') else 'extract')
        if options.get('_critic'):
            return json.dumps({'patches': [], 'score': 95, 'summary': 'Accurate complete task',
                'loop_verdicts': [], 'mention_verdicts': [{'proposal_id': candidate['proposal_id'], 'verdict': 'approve'}]})
        assert not options.get('_revise'), 'a verified channel alias must not force a rewrite'
        return json.dumps(draft)
    result = compose_brief.compose(inputs, llm=llm, critic=True)
    assert calls == ['extract', 'critic']
    assert len(checked_contexts) == 2 and checked_contexts[0] == checked_contexts[1]
    assert cr.alias_identifier(PHONE) in checked_contexts[0][row['anchor_key']]
    assert result['_critic']['actionable'] == 0
    assert result['_final_validation']['passed']
    assert '## Still open' not in result['brief_markdown']


@pytest.mark.parametrize('shared_identifier', [IDENTIFIER, PHONE])
def test_ambiguous_graph_identifier_never_adopts_an_arbitrary_person(tmp_path, shared_identifier):
    import knowledge as kg
    (tmp_path / 'knowledge/people').mkdir(parents=True)
    kg.write_person_file(str(tmp_path / 'knowledge/people/c_def194.md'), kg.PersonFile(
        name='Another Maya', canonical_id='c_def194',
        identifiers=[shared_identifier, 'another.maya@example.test']))
    _payload, rows = seed_capture(tmp_path)
    index = cr.person_identity()
    assert cr.alias_identifier(shared_identifier) in index['ambiguous_identifiers']
    assert cr.resolve_canonical_id('Maya Journey', shared_identifier, index) == ''
    # A unique handle on the same graph still resolves; only contested ownership is refused.
    assert cr.resolve_canonical_id('Another Maya', 'another.maya@example.test', index) == 'c_def194'
    if shared_identifier == IDENTIFIER:
        assert all(not row.get('canonical_id') for row in rows.values())
    update = reviewed_proposal(rows['Send the pricing document'])
    result = cr.resolve({'today': '2026-10-19', 'local': {'imessage': [DELIVERY]},
                         'loop_updates': [update]}, CLOSE_AT)
    assert not result['resolved'] and len(result['active']) == 2


@pytest.mark.parametrize('recent_cid', ['c_abc193', 'c_def194'])
def test_same_name_people_cannot_absorb_a_name_only_commitment(tmp_path, capsys, recent_cid):
    import os
    import knowledge as kg
    _payload, rows = seed_capture(tmp_path)
    people = tmp_path / 'knowledge/people'
    kg.write_person_file(str(people / 'c_def194.md'), kg.PersonFile(
        name='Maya Journey', canonical_id='c_def194', identifiers=['other.maya@example.test']))
    # The generic graph index intentionally picks its most recently touched display name.
    # Neither ordering may authenticate an obligation's counterpart.
    for cid in ('c_abc193', 'c_def194'):
        stamp = 2000000000 if cid == recent_cid else 1900000000
        os.utime(people / (cid + '.md'), (stamp, stamp))
    before = deepcopy(cr._load_items())
    index = cr.person_identity()
    assert cr.resolve_canonical_id('Maya Journey', '', index) == ''
    assert cr.resolve_canonical_id('Maya Journey', IDENTIFIER, index) == 'c_abc193'
    assert cr.resolve_canonical_id('Maya Journey', 'other.maya@example.test', index) == 'c_def194'
    action = {'type': 'commitment', 'channel': 'email', 'contactName': 'Maya Journey',
              'contextSummary': 'Send the private investment memo'}
    result = cr.resolve({'today': '2026-10-19', 'new_actions': [action]}, CLOSE_AT)
    assert 'ambiguous counterpart name; capture held' in capsys.readouterr().err
    assert cr._load_items() == before
    assert {row['anchor_key'] for row in result['active']} == set(before)
    # An exact email still supplies the missing identity and cannot edit either existing ask.
    cr.resolve({'today': '2026-10-19', 'new_actions': [
        {**action, 'contactIdentifier': 'other.maya@example.test'}]}, CLOSE_AT)
    after = cr._load_items()
    assert all(after[key] == row for key, row in before.items())
    added, = [row for key, row in after.items() if key not in before]
    assert added['canonical_id'] == 'c_def194'
    assert added['summary'] == action['contextSummary']
    assert len(rows) == 2 and all(row['status'] == 'open' for row in after.values())


@pytest.mark.parametrize('direction', ['false', 'true', 1])
@pytest.mark.parametrize('source', ['imessage', 'whatsapp'])
def test_nonboolean_native_direction_cannot_close_a_commitment(tmp_path, direction, source):
    _payload, rows = seed_capture(tmp_path)
    message = {**DELIVERY, 'is_from_me': direction}
    update = reviewed_proposal(rows['Send the pricing document'], message)
    update['evidence'][0]['sourceType'] = source
    update['criticReceipt'] = cr.record_critic_approval(update)
    result = cr.resolve({'today': '2026-10-19', 'local': {source: [message]},
                         'loop_updates': [update]}, CLOSE_AT)
    assert not result['resolved'] and len(result['active']) == 2
    assert all(row['status'] == 'open' for row in cr._load_items().values())


@pytest.mark.parametrize('direction', [None, 0, ''])
@pytest.mark.parametrize('source', ['imessage', 'whatsapp'])
def test_unknown_native_direction_cannot_prove_an_inbound_reply(tmp_path, direction, source):
    _payload, rows = seed_capture(tmp_path)
    row = {**rows['Send the pricing document'], 'action_type': 'waiting_on'}
    message = {**DELIVERY, 'is_from_me': direction}
    update = reviewed_proposal(row, message)
    update['evidence'][0]['sourceType'] = source
    update['criticReceipt'] = cr.record_critic_approval(update)
    proof, reason = cr._completion_evidence_result(row, update, {source: [message]},
                                                  observed_before=CLOSE_AT)
    assert proof is None and reason == 'direction_mismatch'


@pytest.mark.parametrize('direction_fields,closes', [
    ({'isSent': 'false'}, False), ({'isSent': 'true'}, False), ({'isSent': 1}, False),
    ({}, False), ({'isSent': True}, True),
    ({'labelIds': ['SENT']}, True), ({'labels': ['sent']}, True),
    ({'isSent': False, 'labelIds': ['SENT']}, True),
    ({'isSent': 'false', 'labelIds': ['SENT']}, True),
])
def test_email_completion_requires_true_flag_or_native_sent_label(tmp_path, direction_fields, closes):
    _payload, rows = seed_capture(tmp_path)
    message = {'id': 'pricing-delivery-mail', 'from': 'owner@example.test', 'to': IDENTIFIER,
               'date': DELIVERY['timestamp'], 'body': DELIVERY['text'], 'text': DELIVERY['text'],
               **direction_fields}
    update = reviewed_proposal(rows['Send the pricing document'], message)
    update['evidence'][0]['sourceType'] = 'email'
    update['criticReceipt'] = cr.record_critic_approval(update)
    result = cr.resolve({'today': '2026-10-19', 'local': {'emails': [message]},
                         'loop_updates': [update]}, CLOSE_AT)
    assert bool(result['resolved']) is closes
    assert len(result['active']) == (1 if closes else 2)
    if closes:
        assert [row['summary'] for row in result['active']] == ['Send the launch checklist']


@pytest.mark.parametrize('direction_fields,inbound', [
    ({'isSent': 'true'}, False), ({'isSent': 'false'}, False),
    ({'isSent': 1}, False), ({'isSent': 0}, False), ({'isSent': None}, False),
    ({'isSent': False}, True), ({}, True), ({'labelIds': ['INBOX']}, True),
    ({'isSent': 'false', 'labelIds': ['SENT']}, False),
])
def test_email_inbound_proof_preserves_unknown_direction(tmp_path, direction_fields, inbound):
    _payload, rows = seed_capture(tmp_path)
    row = {**rows['Send the pricing document'], 'action_type': 'waiting_on'}
    message = {'id': 'incoming-pricing-mail', 'from': IDENTIFIER, 'to': 'owner@example.test',
               'date': DELIVERY['timestamp'], 'body': DELIVERY['text'],
               'text': DELIVERY['text'], **direction_fields}
    update = reviewed_proposal(row, message)
    update['evidence'][0]['sourceType'] = 'email'
    update['criticReceipt'] = cr.record_critic_approval(update)
    proof, reason = cr._completion_evidence_result(row, update, {'emails': [message]},
                                                  observed_before=CLOSE_AT)
    if inbound:
        assert proof is not None
    else:
        assert proof is None and reason == 'direction_mismatch'


@pytest.mark.parametrize('direction', ['true', 'false', 1, 0, None])
def test_malformed_email_direction_cannot_prove_a_new_inbound_request(tmp_path, direction):
    _payload, rows = seed_capture(tmp_path)
    message = {'id': 'incoming-pricing-mail', 'from': IDENTIFIER, 'to': 'owner@example.test',
               'date': DELIVERY['timestamp'], 'body': DELIVERY['text'], 'isSent': direction}
    action = {**rows['Send the pricing document'],
              'source_refs': [{'sourceType': 'email', 'sourceId': message['id'],
                               'snippet': DELIVERY['text']}]}
    assert cr._observed_action_source(action, {'emails': [message]}, CLOSE_AT,
                                      inbound_only=True) is None


def test_incomplete_identity_scan_cannot_adopt_an_arbitrary_person(tmp_path, monkeypatch):
    seed_capture(tmp_path)
    assert cr.resolve_canonical_id('Maya Journey', IDENTIFIER, cr.person_identity()) == 'c_abc193'
    def unreadable(*args, **kwargs):
        raise OSError('fixture: person file disappeared after indexing')
    monkeypatch.setattr(cr, 'open', unreadable, raising=False)
    index = cr.person_identity()
    assert cr.resolve_canonical_id('Maya Journey', IDENTIFIER, index) == ''


def test_incomplete_person_scan_does_not_duplicate_recaptured_bound_obligations(tmp_path, monkeypatch):
    payload, rows = seed_capture(tmp_path)
    before = cr._load_items()
    assert any(row.get('canonical_id') for row in before.values())
    bad = Path(cr._kg.people_dir()) / 'unreadable.md'
    bad.write_text('unreadable person fixture')
    parse = cr._kg.parse_person_file
    def unreadable(text):
        if text == 'unreadable person fixture':
            raise ValueError('cannot read person fixture')
        return parse(text)
    monkeypatch.setattr(cr._kg, 'parse_person_file', unreadable)
    index = cr.person_identity()
    assert index['identity_scan_complete'] is False and not index['unique_identifiers']
    result = cr.resolve(payload, CLOSE_AT)
    assert {row['anchor_key'] for row in result['active']} == set(before)
    assert set(cr._load_items()) == set(before)


@pytest.mark.parametrize('ambiguous', [False, True])
def test_incomplete_graph_cannot_establish_new_or_conflicting_bindings(tmp_path, monkeypatch, capsys, ambiguous):
    payload, _rows = seed_capture(tmp_path)
    before = deepcopy(cr._load_items())
    original = cr.person_identity()
    identifier = cr.alias_identifier(IDENTIFIER)
    if not ambiguous:
        payload = deepcopy(payload)
        for action in payload['new_actions']:
            action['contact_identifier'] = 'unbound@example.test'
            action.pop('contactIdentifier', None)
    broken = {**original, 'identity_scan_complete': False, 'unique_identifiers': {},
              'conflicting_identifiers': {identifier} if ambiguous else set()}
    monkeypatch.setattr(cr, 'person_identity', lambda: broken)
    result = cr.resolve(payload, CLOSE_AT)
    assert not result['resolved']
    assert 'identity read incomplete; capture held' in capsys.readouterr().err
    assert set(cr._load_items()) == set(before)
