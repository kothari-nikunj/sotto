"""Obligation identity, source-bound completion and Friday review contracts."""
import importlib.util
from datetime import datetime
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
for directory in ('_shared/scripts', '_shared/lib'):
    sys.path.insert(0, str(ROOT / directory))
import compose_brief as cb
from delivery_effects import loop_version

spec = importlib.util.spec_from_file_location('obligation_resolver', ROOT / 'morning-brief/scripts/continuity_resolve.py')
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)
NOW = datetime(2026, 9, 18, 18, 0)


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')


def action(text, **extra):
    return {'type': 'reply', 'channel': 'imessage', 'contactName': 'Maya Chen',
            'contactIdentifier': '+14155551234', 'contextSummary': text,
            'created_at': '2026-09-10 09:00:00', **extra}


def rows():
    return list(cr._load_items().values())


def message(**extra):
    return {'rowid': 42, 'handle': '+14155551234', 'text': 'The signed contract is attached.',
            'is_from_me': True, 'timestamp': '2026-09-18 10:00:00', **extra}


def proposal(row, msg, **extra):
    update = {'loopId': row['anchor_key'], 'loopVersion': loop_version(row), 'status': 'resolved',
              'evidence': [{'sourceType': 'imessage', 'sourceId': str(msg['rowid']),
                            'snippet': msg['text']}], **extra}
    return reviewed(update)


def reviewed(update):
    """The receipt compose_brief stamps once a completed critic pass reviewed this closure."""
    update = {k: v for k, v in update.items() if k != 'criticReceipt'}
    if update.get('status') == 'resolved':
        update['criticReceipt'] = cr.record_critic_approval(update)
    return update


def resolve(**payload):
    return cr.resolve({'today': '2026-09-18', **payload}, NOW, resolve_existing=False)


def test_two_asks_same_message_and_paraphrase_carry_forward():
    proof = [{'sourceType': 'imessage', 'sourceId': '1', 'snippet': 'Contract and lunch?'}]
    source = {'rowid': 1, 'handle': '+14155551234', 'text': 'Contract and lunch?',
              'is_from_me': False, 'timestamp': '2026-09-10 08:00:00'}
    result = resolve(new_actions=[action('Send the signed contract', evidence=proof),
                                  action('Confirm lunch on Tuesday', evidence=proof,
                                         newObligation=True)],
                     local={'imessage': [source]})
    assert len(result['active']) == 2
    first = next(row for row in rows() if row['summary'].startswith('Send'))
    second = next(row for row in rows() if row['summary'].startswith('Confirm'))
    assert second['origin_key'] and ':origin:' in second['anchor_key']
    # Replaying with different prose targets the persisted origin without redefining
    # its original task or creating a third content-hash row.
    resolve(new_actions=[action('Lock down Tuesday lunch', evidence=proof, newObligation=True)],
            local={'imessage': [source]})
    assert len(rows()) == 2
    assert cr._load_items()[second['anchor_key']]['summary'] == second['summary']
    resolve(new_actions=[action('Send the executed agreement', loopId=first['anchor_key'])])
    assert len(rows()) == 2
    assert cr._load_items()[first['anchor_key']]['summary'] == first['summary']


@pytest.mark.parametrize('summary', [
    'Send Patrick a belated birthday greeting', 'Happy birthday Patrick',
])
def test_calendar_birthday_suggestion_does_not_create_a_ledger_debt(summary):
    result = resolve(new_actions=[action(summary, contextAsk='Wish Patrick a happy birthday')])
    assert not result['active']
    assert not rows()


def test_sourced_request_for_birthday_help_remains_a_real_obligation():
    source = message(rowid=7, is_from_me=False,
                     text='Please send Mom a birthday greeting for me.',
                     timestamp='2026-09-10 08:00:00')
    evidence = [{'sourceType': 'imessage', 'sourceId': '7',
                 'snippet': 'Please send Mom a birthday greeting for me.'}]
    result = resolve(new_actions=[action('Send Mom a birthday greeting for Maya',
                                         evidence=evidence)], local={'imessage': [source]})
    assert len(result['active']) == 1


def test_first_flagged_action_owns_base_and_all_replays_stay_one_row():
    proof = [{'sourceType': 'imessage', 'sourceId': '10', 'snippet': 'Send the contract'}]
    source = message(rowid=10, is_from_me=False, text='Send the contract please.',
                     timestamp='2026-09-10 08:00:00')
    resolve(new_actions=[action('Send the signed contract', evidence=proof, newObligation=True)],
            local={'imessage': [source]})
    first = rows()[0]
    assert first['anchor_key'] == cr.compute_anchor_key(first) and first['origin_key']
    resolve(new_actions=[action('Get the agreement over')])
    resolve(new_actions=[action('Send the executed agreement', evidence=proof,
                                newObligation=True)], local={'imessage': [source]})
    assert len(rows()) == 1
    assert rows()[0]['anchor_key'] == first['anchor_key']
    assert rows()[0]['summary'] == first['summary']


def test_unique_evidence_recovers_active_sibling_without_loop_id():
    contract = message(rowid=10, is_from_me=False, text='Send the signed contract please.')
    lunch = message(rowid=11, is_from_me=False, text='Please confirm lunch on Tuesday.')
    contract_refs = [{'sourceType': 'imessage', 'sourceId': '10'}]
    lunch_refs = [{'sourceType': 'imessage', 'sourceId': '11'}]
    resolve(new_actions=[action('Send the signed contract', evidence=contract_refs),
                         action('Confirm lunch on Tuesday', evidence=lunch_refs, newObligation=True)],
            local={'imessage': [contract, lunch]})
    by_summary = {r['summary']: r['anchor_key'] for r in rows()}
    resolve(new_actions=[action('Lock down Tuesday lunch', evidence=lunch_refs)],
            local={'imessage': [lunch]})
    current = cr._load_items()
    assert len(current) == 2
    assert current[by_summary['Send the signed contract']]['summary'] == 'Send the signed contract'
    assert current[by_summary['Confirm lunch on Tuesday']]['summary'] == 'Confirm lunch on Tuesday'
    # A later request message can be a reminder. Disjoint evidence alone does not create a task.
    reminder = message(rowid=12, is_from_me=False, text='Any update on sending the contract?')
    resolve(new_actions=[action('Send the executed agreement', evidence=[
        {'sourceType': 'imessage', 'sourceId': '12'}])], local={'imessage': [reminder]})
    assert len(rows()) == 2
    assert cr._load_items()[by_summary['Send the signed contract']]['summary'] == 'Send the signed contract'


def test_shared_evidence_cannot_choose_an_active_sibling():
    proof = [{'sourceType': 'imessage', 'sourceId': '13'}]
    source = message(rowid=13, is_from_me=False, text='Please handle contract and lunch.')
    resolve(new_actions=[action('Send the signed contract', evidence=proof),
                         action('Confirm lunch on Tuesday', evidence=proof, newObligation=True)],
            local={'imessage': [source]})
    items = cr._load_items()
    base = cr.compute_anchor_key(cr._normalize_action(action('Send the signed contract')))
    # Reverse insertion order so an arbitrary first reference match would pick the sibling.
    incoming = cr._normalize_action(action('Send the executed agreement', evidence=proof))
    assert cr._obligation_key(incoming, dict(reversed(list(items.items())))) == base


def test_two_flagged_actions_from_same_source_replay_as_two_rows():
    proof = [{'sourceType': 'imessage', 'sourceId': '11', 'snippet': 'contract and lunch'}]
    source = message(rowid=11, is_from_me=False, text='Please handle contract and lunch.',
                     timestamp='2026-09-10 08:00:00')
    batch = [action('Send the signed contract', evidence=proof, newObligation=True),
             action('Confirm lunch on Tuesday', evidence=proof, newObligation=True)]
    resolve(new_actions=batch, local={'imessage': [source]})
    assert len(rows()) == 2
    keys = {row['anchor_key'] for row in rows()}
    resolve(new_actions=[dict(batch[0], contextSummary='Send the executed agreement'),
                         dict(batch[1], contextSummary='Lock down Tuesday lunch')],
            local={'imessage': [source]})
    assert len(rows()) == 2 and {row['anchor_key'] for row in rows()} == keys


def test_unobserved_new_obligation_is_rejected_and_bad_loop_id_uses_counterpart_base(capsys):
    resolve(new_actions=[action('Send the signed contract')])
    base = rows()[0]
    resolve(new_actions=[action('Send the executed agreement', newObligation=True,
                                evidence=[{'sourceType': 'imessage', 'sourceId': 'missing'}])])
    assert len(rows()) == 1 and rows()[0]['anchor_key'] == base['anchor_key']
    assert rows()[0]['summary'] == 'Send the signed contract'
    future = message(rowid=88, is_from_me=False, text='Please send the future version.',
                     timestamp='2026-09-19 08:00:00')
    resolve(new_actions=[action('Send the future version', newObligation=True, evidence=[
        {'sourceType': 'imessage', 'sourceId': '88', 'snippet': 'future version'}])],
        local={'imessage': [future]})
    assert len(rows()) == 1 and rows()[0]['summary'] == 'Send the signed contract'
    resolve(new_actions=[action('Send the final agreement', loopId='made-up')])
    assert len(rows()) == 1 and rows()[0]['summary'] == base['summary']
    err = capsys.readouterr().err
    assert 'newObligation evidence not observed' in err
    assert 'unknown or mismatched loopId' in err


def test_mismatched_loop_id_never_mutates_the_other_person():
    resolve(new_actions=[action('Send the signed contract')])
    maya = rows()[0]
    other = action('Confirm the budget', contactName='Noah Kim',
                   contactIdentifier='+14155559999', loopId=maya['anchor_key'])
    resolve(new_actions=[other])
    current = rows()
    assert len(current) == 2
    assert next(row for row in current if row['anchor_key'] == maya['anchor_key'])['summary'] == 'Send the signed contract'
    assert next(row for row in current if row['contact_name'] == 'Noah Kim')['summary'] == 'Confirm the budget'


def test_model_claimed_canonical_id_cannot_redirect_a_different_identifier():
    resolve(new_actions=[action('Send the signed contract')])
    maya = rows()[0]
    resolve(new_actions=[action('Confirm the budget', contactName='Noah Kim',
                                contactIdentifier='+14155559999',
                                canonical_id='claimed-maya', loopId=maya['anchor_key'])])
    assert len(rows()) == 2
    assert cr._load_items()[maya['anchor_key']]['summary'] == 'Send the signed contract'


def test_name_only_loop_id_cannot_rewrite_another_task_or_lose_the_debt():
    resolve(new_actions=[action('Send the signed contract')])
    maya = rows()[0]
    resolve(new_actions=[action('Confirm the travel plan', contactIdentifier='',
                                contactName='Alex Kim', loopId=maya['anchor_key'])])
    current = {row['contact_name']: row for row in rows()}
    assert current['Maya Chen']['summary'] == 'Send the signed contract'
    assert current['Alex Kim']['summary'] == 'Confirm the travel plan'


def test_named_completion_closes_only_one_obligation():
    proof = [{'sourceType': 'imessage', 'sourceId': '2', 'snippet': 'contract and lunch'}]
    source = message(rowid=2, is_from_me=False, text='Please handle contract and lunch.',
                     timestamp='2026-09-10 08:00:00')
    resolve(new_actions=[action('Send the signed contract'),
                         action('Confirm lunch on Tuesday', evidence=proof, newObligation=True)],
            local={'imessage': [source]})
    row = next(row for row in rows() if row['summary'].startswith('Send'))
    msg = message()
    out = resolve(loop_updates=[proposal(row, msg)], local={'imessage': [msg]})
    assert len(out['resolved']) == 1
    assert [item['summary'] for item in out['active']] == ['Confirm lunch on Tuesday']
    assert rows()[0].get('resolution_source_refs') or rows()[1].get('resolution_source_refs')


@pytest.mark.parametrize('mutation', ['wrong_id', 'wrong_quote', 'wrong_person', 'wrong_direction',
                                     'old_message', 'wrong_version', 'group_message'])
def test_unsupported_completion_stays_open(mutation):
    resolve(new_actions=[action('Send the signed contract')])
    row = rows()[0]
    msg = message()
    update = proposal(row, msg)
    if mutation == 'wrong_id':
        update['evidence'][0]['sourceId'] = '99'
    elif mutation == 'wrong_quote':
        update['evidence'][0]['snippet'] = 'This was never said'
    elif mutation == 'wrong_person':
        msg['handle'] = '+14155559999'
    elif mutation == 'wrong_direction':
        msg['is_from_me'] = False
    elif mutation == 'old_message':
        msg['timestamp'] = '2026-09-09 10:00:00'
    elif mutation == 'wrong_version':
        update['loopVersion'] = 'stale'
    else:
        msg['is_group_chat'] = True
        msg['chat_guid'] = 'iMessage;+;group'
    # Restamp so each mutation is rejected by its own integrity check, not by a stale receipt.
    out = resolve(loop_updates=[reviewed(update)], local={'imessage': [msg]})
    assert not out['resolved'] and len(out['active']) == 1


def test_user_correction_invalidates_inflight_proposal():
    resolve(new_actions=[action('Send the signed contract')])
    row = rows()[0]
    msg = message()
    update = proposal(row, msg)
    row['summary'] = 'Send the corrected signed contract'
    cr._persist(row)
    assert not resolve(loop_updates=[update], local={'imessage': [msg]})['resolved']


def test_rewrite_cannot_redefine_task_before_completion():
    resolve(new_actions=[action('Send the signed contract')])
    row = rows()[0]
    msg = message()
    out = resolve(new_actions=[action('Send birthday wishes', loopId=row['anchor_key'])],
                  loop_updates=[proposal(row, msg)], local={'imessage': [msg]})
    assert out['resolved'][0]['summary'] == 'Send the signed contract'


def test_generic_contact_and_age_never_pay_off_obligations():
    resolve(new_actions=[action('Send the signed contract', deadlineDate='2026-09-11')])
    msg = message(text='Here are the weekend photos: https://example.test/photos')
    out = cr.resolve({'today': '2026-09-18', 'local': {'imessage': [msg]},
                      'signals': {'replied_thread_ids': ['a'], 'handled': [
                          {'identifier': '+14155551234', 'channel': 'imessage'}]}}, NOW)
    assert not out['resolved'] and not out['expired'] and len(out['active']) == 1


def test_waiting_promise_defers_only_named_task_once():
    proof = [{'sourceType': 'imessage', 'sourceId': '3', 'snippet': 'contract and tax documents'}]
    source = message(rowid=3, is_from_me=False, text='I will send the contract and tax documents.',
                     timestamp='2026-09-10 08:00:00')
    resolve(new_actions=[action('Receive the signed contract', type='waiting_on'),
                         action('Receive the tax documents', type='waiting_on', evidence=proof,
                                newObligation=True)], local={'imessage': [source]})
    row = next(row for row in rows() if 'contract' in row['summary'])
    msg = message(is_from_me=False, text="I'll send the signed contract by Monday.")
    update = proposal(row, msg, status='waiting')
    resolve(loop_updates=[update], local={'imessage': [msg]})
    current = cr._load_items()[row['anchor_key']]
    assert current['chase_after'] == '2026-09-21'
    assert sum(bool(r.get('last_heard_at')) for r in rows()) == 1
    resolve(loop_updates=[update], local={'imessage': [msg]})
    assert cr._load_items()[row['anchor_key']]['chase_after'] == '2026-09-21'


@pytest.mark.parametrize(('text', 'expected'), [
    ("I'll send it tomorrow.", '2026-09-11'),
    ("I'm working on it.", '2026-09-13'),
])
def test_delayed_waiting_promise_uses_message_time_and_keeps_past_check_date(text, expected):
    resolve(new_actions=[action('Receive the signed contract', type='waiting_on',
                                created_at='2026-09-09 09:00:00')])
    row = rows()[0]
    msg = message(is_from_me=False, text=text, timestamp='2026-09-10 08:00:00')
    update = proposal(row, msg, status='waiting')
    resolve(loop_updates=[update], local={'imessage': [msg]})
    current = cr._load_items()[row['anchor_key']]
    assert current['last_heard_at'] == '2026-09-10 08:00:00'
    assert current['chase_after'] == expected
    assert cr.chase_due(current, '2026-09-18', NOW)


def test_shadowed_legacy_paraphrases_fold():
    resolve(new_actions=[action('Send the signed contract')])
    row = rows()[0]
    duplicate = dict(row, summary='Confirm lunch on Tuesday')
    directory = Path(row['_path']).parent
    (directory / 'legacy-second.md').write_text('---\n' + yaml.safe_dump(
        {k: v for k, v in duplicate.items() if k != '_path'}) + '---\n')
    out = cr.resolve({'today': '2026-09-18'}, NOW)
    assert len(out['active']) == 1
    stored = [cr.ledger_io.parse_frontmatter(path.read_text()) for path in directory.glob('*.md')]
    assert any(row.get('resolution') == 'merged_duplicate' for row in stored)


def test_shadowed_duplicate_folds_into_stable_base_without_rewriting_existing_task_key(tmp_path):
    base = cr._normalize_action(action('Send the signed contract'))
    base_key = cr.compute_anchor_key(base)
    lunch = cr._normalize_action(action('Confirm lunch on Tuesday'))
    task_key = f"{base_key}:task:4de47ef8393fd210"
    directory = tmp_path / 'knowledge' / 'continuity'
    directory.mkdir(parents=True)
    keeper = {**base, 'anchor_key': base_key, 'status': 'open', 'created_at': '2026-09-10',
              '_path': str(directory / 'base.md')}
    task_keeper = {**lunch, 'anchor_key': task_key, 'status': 'open', 'created_at': '2026-09-11',
                   'chased_count': 1, '_path': str(directory / 'task.md')}
    shadow = {**lunch, 'anchor_key': base_key, 'status': 'open', 'created_at': '2026-09-09',
              'chased_count': 2, 'last_chased_at': '2026-09-17',
              '_path': str(directory / 'shadow.md')}
    for row in (keeper, task_keeper, shadow):
        cr._persist(row)

    items, shadowed = cr._load_items(with_shadowed=True)
    cr._fold_shadowed(items, shadowed, '2026-09-18')

    assert items[base_key]['chased_count'] == 2
    assert items[base_key]['last_chased_at'] == '2026-09-17'
    assert items[task_key]['chased_count'] == 1
    assert cr.ledger_io.parse_frontmatter(Path(shadow['_path']).read_text())['resolution'] == 'merged_duplicate'


def test_identity_migration_retires_live_row_when_same_debt_already_terminal(tmp_path, monkeypatch):
    legacy = cr._normalize_action(action('Send the signed contract'))
    canonical = dict(legacy, canonical_id='person_maya')
    old_key, terminal_key = cr.compute_anchor_key(legacy), cr.compute_anchor_key(canonical)
    directory = tmp_path / 'knowledge' / 'continuity'
    directory.mkdir(parents=True)
    live = {**legacy, 'anchor_key': old_key, 'status': 'open', 'created_at': '2026-09-10',
            '_path': str(directory / 'legacy.md')}
    terminal = {**canonical, 'anchor_key': terminal_key, 'status': 'dismissed',
                'resolution': 'user_dismissed', 'resolution_mode': 'explicit',
                'created_at': '2026-09-10', '_path': str(directory / 'terminal.md')}
    for row in (live, terminal):
        cr._persist(row)
    items = {old_key: live, terminal_key: terminal}
    monkeypatch.setattr(cr, 'canonicalize_counterpart', lambda row, *_: canonical)

    cr._migrate_identity(items, {}, '2026-09-18', {})

    assert old_key not in items
    assert items[terminal_key]['resolution'] == 'user_dismissed'
    retired = cr.ledger_io.parse_frontmatter(Path(live['_path']).read_text())
    assert retired['resolution'] == 'merged_duplicate'
    assert retired['merged_into'] == terminal_key


def test_identity_migration_never_resurrects_differently_worded_terminal_alias(tmp_path,
                                                                               monkeypatch):
    legacy = cr._normalize_action(action('Send the signed contract'))
    canonical = dict(legacy, canonical_id='person_maya')
    old_key = cr.compute_anchor_key(legacy)
    terminal_key = 'imessage:follow_up:cid:person_maya'
    canonical_base = cr.compute_anchor_key(canonical)
    directory = tmp_path / 'knowledge' / 'continuity'
    directory.mkdir(parents=True)
    live = {**legacy, 'anchor_key': old_key, 'status': 'open',
            'summary': 'Send the signed contract', 'created_at': '2026-09-10',
            '_path': str(directory / 'legacy-live.md')}
    terminal = {**canonical, 'anchor_key': terminal_key, 'status': 'dismissed',
                'summary': 'Get the executed agreement over', 'resolution': 'user_dismissed',
                'resolution_mode': 'explicit', 'created_at': '2026-09-10',
                '_path': str(directory / 'terminal-alias.md')}
    for row in (live, terminal):
        cr._persist(row)
    items = {old_key: live, terminal_key: terminal}
    monkeypatch.setattr(cr, 'canonicalize_counterpart', lambda row, *_: canonical)

    cr._migrate_identity(items, {}, '2026-09-18', {})

    assert canonical_base not in items and old_key not in items
    assert items[terminal_key]['resolution'] == 'user_dismissed'
    retired = cr.ledger_io.parse_frontmatter(Path(live['_path']).read_text())
    assert retired['resolution'] == 'merged_duplicate'
    assert retired['merged_into'] == terminal_key


def test_known_source_cannot_reopen_among_multiple_closed_tasks():
    contract_ref = [{'sourceType': 'imessage', 'sourceId': '101',
                     'snippet': 'Can you send the contract?'}]
    lunch_ref = [{'sourceType': 'imessage', 'sourceId': '202',
                  'snippet': 'Can you confirm lunch?'}]
    initial_messages = [
        message(rowid=101, is_from_me=False, text='Can you send the contract?',
                timestamp='2026-09-10 08:00:00'),
        message(rowid=202, is_from_me=False, text='Can you confirm lunch?',
                timestamp='2026-09-10 08:01:00'),
    ]
    resolve(new_actions=[action('Send the signed contract', evidence=contract_ref),
                         action('Confirm lunch on Tuesday', evidence=lunch_ref,
                                newObligation=True)], local={'imessage': initial_messages})
    for row in rows():
        row['status'] = 'dismissed'
        row['resolution'] = 'user_dismissed'
        # The contract source predates its own closure, but postdates the other task's closure.
        # It must bind back to the contract tombstone rather than look fresh relative to lunch.
        row['closed_at'] = ('2026-09-18 09:00:00' if 'contract' in row['summary']
                            else '2026-09-18 07:00:00')
        row['resolved_at'] = '2026-09-18'
        cr._persist(row)
    old_request = message(rowid=101, is_from_me=False, text='Can you send the contract?',
                          timestamp='2026-09-18 08:00:00')
    out = resolve(new_actions=[action('Please get the agreement over', evidence=contract_ref)],
                  local={'imessage': [old_request]})
    assert out['active'] == []
    assert len(rows()) == 2 and all(row['status'] == 'dismissed' for row in rows())


def test_weekly_review_only_friday_evening_and_respects_mute(monkeypatch):
    resolve(new_actions=[action('Send the signed contract')])
    original = {'brief_markdown': '## Filtered\nNothing else.'}
    assert 'Weekly review' not in cb._append_weekly_review(dict(original), {'type': 'morning'}, NOW)['brief_markdown']
    assert 'Weekly review' not in cb._append_weekly_review(dict(original), {'type': 'evening'}, datetime(2026, 9, 17, 18))['brief_markdown']
    out = cb._append_weekly_review(dict(original), {'type': 'evening'}, NOW)
    assert 'Weekly review' in out['brief_markdown'] and 'Send the signed contract' in out['brief_markdown']
    monkeypatch.setattr(cb, 'explicit_prefs', lambda: {'mute_sections': ['weekly_review']})
    assert cb._append_weekly_review(dict(original), {'type': 'evening'}, NOW) == original


def test_brief_schema_and_normalization_keep_proposals():
    update = {'loopId': 'x', 'loopVersion': 'v', 'status': 'resolved', 'evidence': []}
    assert 'loopUpdates' in cb.BRIEF_RESPONSE_SCHEMA['properties']
    assert cb._normalize_output({'loopUpdates': [update]})['loop_updates'] == [update]


def test_fact_corroboration_requires_observed_reference():
    out = {'extracted_knowledge': {'person_updates': [{'person_name': 'Maya', 'facts': [
        {'fact': 'Runs legal', 'source_ref': 'observed-1'},
        {'fact': 'Runs legal', 'source_ref': 'invented-2'},
    ]}]}}
    cb._bind_brief_fact_evidence(out, {'google': {'emails': [
        {'id': 'observed-1', 'date': '2026-09-10T08:00:00Z', 'body': 'I run legal.'}]}})
    facts = out['extracted_knowledge']['person_updates'][0]['facts']
    assert facts == [{'fact': 'Runs legal', 'source_ref': 'observed-1', 'observed_date': '2026-09-10'}]


def test_weekly_review_survives_photo_selection():
    import visual_brief
    brief = ('Good evening\n\nNeeds Attention Now\nMaya: approve the contract.\n\n'
             'Should Handle Today\nSam: confirm lunch.\n\nComing Up\n10:00 AM: Team sync\n\n'
             'Weekly review\nMaya: send the signed contract.\nKeep, snooze or dismiss when ready.\n')
    deck = visual_brief.build(brief)
    assert deck
    assert any('signed contract' in block['text'] for card in deck['cards'] for block in card['blocks'])


def test_identity_growth_replay_keeps_terminal_intro_and_distinct_open_meeting():
    import knowledge as kg
    request = message(rowid=901, is_from_me=False, text='Introduce me to Lee and check whether we meet.',
                      timestamp='2026-09-10 08:00:00')
    proof = [{'sourceType':'imessage', 'sourceId':'901', 'snippet':request['text']}]
    actions = [action('Introduce Maya to Lee', evidence=proof),
               action('Check whether Maya and Lee meet', evidence=proof, newObligation=True)]
    resolve(new_actions=actions, local={'imessage':[request]})
    intro = next(row for row in rows() if row['summary'].startswith('Introduce'))
    meeting = next(row for row in rows() if row['summary'].startswith('Check'))
    sent = message(rowid=902, text='Maya, meet Lee. Lee, meet Maya.')
    assert resolve(loop_updates=[proposal(intro, sent)], local={'imessage':[sent]})['resolved']
    closed = cr._load_items()[intro['anchor_key']].copy()
    p = kg.PersonFile(name='Maya Chen', canonical_id='c_aaaaaa', identifiers=['+14155551234'])
    path = Path(kg.people_dir()) / 'c_aaaaaa.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    kg.write_person_file(str(path), p)
    resolve(new_actions=actions, local={'imessage':[request]})
    saved = cr._load_items()
    assert set(saved) == {intro['anchor_key'], meeting['anchor_key']}
    assert saved[intro['anchor_key']] == closed
    assert saved[meeting['anchor_key']]['status'] == 'open'


def test_identity_growth_keeps_two_flagged_asks_from_one_source_separate():
    import knowledge as kg
    source = message(rowid=910, is_from_me=False, text='Send the contract and confirm lunch.',
                     timestamp='2026-09-10 08:00:00')
    proof = [{'sourceType':'imessage', 'sourceId':'910', 'snippet':source['text']}]
    actions = [action('Send the contract', evidence=proof, newObligation=True),
               action('Confirm lunch on Tuesday', evidence=proof, newObligation=True)]
    resolve(new_actions=actions, local={'imessage':[source]})
    before = {row['anchor_key']: row['origin_key'] for row in rows()}
    p = kg.PersonFile(name='Maya Chen', canonical_id='c_aaaaaa', identifiers=['+14155551234'])
    path = Path(kg.people_dir()) / 'c_aaaaaa.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    kg.write_person_file(str(path), p)
    resolve(new_actions=actions, local={'imessage':[source]})
    resolve(new_actions=actions, local={'imessage':[source]})
    assert {row['anchor_key']: row['origin_key'] for row in rows()} == before
    assert len(rows()) == 2


@pytest.mark.parametrize('boundary', ['different_person', 'unknown_identifier', 'ambiguous_identifier',
                                     'missing_identifier', 'group', 'direction', 'shared_task_evidence'])
def test_identity_alias_never_supplies_missing_person_or_task_authority(boundary):
    import knowledge as kg
    people = Path(kg.people_dir())
    people.mkdir(parents=True, exist_ok=True)
    p = kg.PersonFile(name='Maya Chen', canonical_id='c_aaaaaa',
                      identifiers=['+14155551234', 'maya@example.test'])
    kg.write_person_file(str(people / 'c_aaaaaa.md'), p)
    other = kg.PersonFile(name='Maya Chen', canonical_id='c_bbbbbb', identifiers=['other@example.test'])
    if boundary == 'ambiguous_identifier':
        other.identifiers.append('+14155551234')
    kg.write_person_file(str(people / 'c_bbbbbb.md'), other)
    old = {'action_type':'reply', 'contact_identifier':'+14155551234', 'contact_name':'Maya Chen',
           'channel':'imessage', 'source_refs':[['imessage','910']]}
    old_key = cr.compute_anchor_key(old)
    incoming = {**old, 'canonical_id':'c_aaaaaa', 'contact_identifier':'maya@example.test'}
    if boundary == 'different_person':
        incoming.update(canonical_id='c_bbbbbb', contact_identifier='other@example.test')
    elif boundary == 'unknown_identifier':
        old['contact_identifier'] = 'unknown@example.test'
    elif boundary == 'missing_identifier':
        old['contact_identifier'] = ''
    elif boundary == 'group':
        incoming['group_id'] = 'group-platform-id'
    elif boundary == 'direction':
        incoming['action_type'] = 'waiting_on'
    items = {old_key:old}
    if boundary == 'shared_task_evidence':
        items[old_key+':task:second'] = {**old, 'summary':'A separate ask'}
    assert cr._obligation_key(incoming, items, person_index=cr.person_identity()) not in items



def test_ambiguous_retained_alias_origin_is_held_without_third_row():
    import knowledge as kg
    source = message(rowid=920, is_from_me=False, text='Send the signed contract please.',
                     timestamp='2026-09-10 08:00:00')
    proof = [{'sourceType':'imessage', 'sourceId':'920', 'snippet':source['text']}]
    capture = action('Send the signed contract', evidence=proof, newObligation=True)
    resolve(new_actions=[capture], local={'imessage':[source]})
    first = rows()[0]
    duplicate = {**first, 'anchor_key':first['anchor_key']+':origin:legacy-copy',
                 '_path':str(Path(cr._dir()) / 'legacy-copy.md')}
    cr._persist(duplicate)
    before = cr._load_items()
    people = Path(kg.people_dir())
    people.mkdir(parents=True, exist_ok=True)
    kg.write_person_file(str(people / 'c_aaaaaa.md'), kg.PersonFile(name='Maya Chen',
        canonical_id='c_aaaaaa', identifiers=['+14155551234']))
    resolve(new_actions=[capture], local={'imessage':[source]})
    assert cr._load_items() == before and len(rows()) == 2



def test_shared_request_evidence_after_identity_growth_is_held_without_new_row():
    import knowledge as kg
    source = message(rowid=930, is_from_me=False, text='Send the contract and confirm lunch on Tuesday.',
                     timestamp='2026-09-10 08:00:00')
    proof = [{'sourceType':'imessage', 'sourceId':'930', 'snippet':source['text']}]
    resolve(new_actions=[action('Send the contract', evidence=proof, newObligation=True),
                         action('Confirm lunch on Tuesday', evidence=proof, newObligation=True)],
            local={'imessage':[source]})
    before = cr._load_items()
    assert len(before) == 2
    people = Path(kg.people_dir())
    people.mkdir(parents=True, exist_ok=True)
    kg.write_person_file(str(people/'c_aaaaaa.md'), kg.PersonFile(name='Maya Chen',
        canonical_id='c_aaaaaa', identifiers=['+14155551234']))
    resolve(new_actions=[action('Handle the outstanding request', evidence=proof)], local={'imessage':[source]})
    assert cr._load_items() == before


def test_same_message_siblings_survive_reorder_and_one_sibling_closing():
    source = message(rowid=950, is_from_me=False, text='Send the contract and confirm lunch.',
                     timestamp='2026-09-10 08:00:00')
    proof = [{'sourceType': 'imessage', 'sourceId': '950', 'snippet': source['text']}]
    batch = [action('Send the contract', evidence=proof, newObligation=True),
             action('Confirm lunch on Tuesday', evidence=proof, newObligation=True)]
    resolve(new_actions=batch, local={'imessage': [source]})
    initial = {r['summary']: r['anchor_key'] for r in rows()}
    resolve(new_actions=list(reversed(batch)), local={'imessage': [source]})
    assert {r['summary']: r['anchor_key'] for r in rows()} == initial
    contract = next(r for r in rows() if r['summary'] == 'Send the contract')
    delivered = message()
    resolve(local={'imessage': [delivered]}, loop_updates=[proposal(contract, delivered)])
    resolve(new_actions=[batch[1]], local={'imessage': [source]})
    assert [(r['anchor_key'], r['summary']) for r in rows() if r['status'] == 'open'] == [
        (initial['Confirm lunch on Tuesday'], 'Confirm lunch on Tuesday')]


def test_ambiguous_paraphrase_of_same_message_siblings_cannot_select_by_order():
    source = message(rowid=951, is_from_me=False, text='Send the contract and confirm lunch.',
                     timestamp='2026-09-10 08:00:00')
    proof = [{'sourceType': 'imessage', 'sourceId': '951', 'snippet': source['text']}]
    resolve(new_actions=[action('Send the contract', evidence=proof, newObligation=True),
                         action('Confirm lunch on Tuesday', evidence=proof, newObligation=True)],
            local={'imessage': [source]})
    before = cr._load_items()
    resolve(new_actions=[action('Please confirm our lunch plans', evidence=proof, newObligation=True)],
            local={'imessage': [source]})
    assert cr._load_items() == before
