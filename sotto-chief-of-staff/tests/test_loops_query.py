"""loops_query.py — splits the continuity ledger into you-owe vs waiting-on, deadline first."""
import importlib.util, os

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
spec = importlib.util.spec_from_file_location("lq", os.path.join(ROOT, "_shared", "scripts", "loops_query.py"))
lq = importlib.util.module_from_spec(spec); spec.loader.exec_module(lq)


def _write(d, name, fm):
    import yaml
    (d / f"{name}.md").write_text("---\n" + yaml.safe_dump(fm) + "---\nbody\n")


def test_splits_direction_and_orders(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    cdir = tmp_path / "knowledge" / "continuity"
    cdir.mkdir(parents=True)
    today = lq._now_local("+00:00")
    old = (today.replace(microsecond=0)).strftime("%Y-%m-%dT%H:%M:%S")
    import datetime
    days5 = (today - datetime.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
    days1 = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    overdue_dl = (today - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    _write(cdir, "a", {"status": "open", "action_type": "reply", "contact_name": "Dhruv",
                       "summary": "reply re LOI", "created_at": days1})
    _write(cdir, "b", {"status": "open", "action_type": "reply", "contact_name": "Old",
                       "summary": "old thread", "created_at": days5, "deadline": overdue_dl})
    _write(cdir, "c", {"status": "waiting", "action_type": "waiting_on", "contact_name": "Vendor",
                       "summary": "awaiting quote", "created_at": days5})
    _write(cdir, "d", {"status": "resolved", "action_type": "reply", "contact_name": "Done",
                       "summary": "closed", "created_at": old})  # terminal → excluded

    out = lq.query()
    assert out["counts"] == {"you_owe": 2, "waiting_on_them": 1}
    assert [e["name"] for e in out["you_owe"]] == ["Old", "Dhruv"]   # overdue first, then older
    assert out["you_owe"][0]["overdue"] is True
    assert out["waiting_on_them"][0]["name"] == "Vendor"
    assert all(e["name"] != "Done" for e in out["you_owe"])          # resolved excluded


def test_empty_when_no_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    out = lq.query()
    assert out["counts"] == {"you_owe": 0, "waiting_on_them": 0}


def test_deadlines_win_over_capture_age_in_both_directions_and_parked_view():
    from copy import deepcopy
    from datetime import datetime
    from zoneinfo import ZoneInfo
    # The owner's day is still Oct 14 even though UTC has reached Oct 15.
    now = datetime(2026, 10, 14, 20, tzinfo=ZoneInfo('America/Los_Angeles'))
    examples = [
        ('undated-old', None, '2026-08-01'),
        ('due-tomorrow', '2026-10-15', '2026-10-14'),
        ('overdue-recent', '2026-10-13', '2026-08-01'),
        ('due-today-new', '2026-10-14', '2026-10-14'),
        ('overdue-earliest', '2026-10-12', '2026-10-11'),
        ('due-today-old', '2026-10-14', '2026-10-01'),
        ('invalid-date', '2026-02-30', '2026-09-01'),
        ('relative-date', 'tonight', '2026-10-01'),
        ('undated-new-b', None, '2026-10-14'),
        ('undated-new-a', None, '2026-10-14'),
    ]
    expected = ['overdue-earliest', 'overdue-recent', 'undated-old', 'due-today-old',
                'due-today-new', 'invalid-date', 'relative-date',
                'undated-new-a', 'undated-new-b', 'due-tomorrow']
    for action_type, lane in [('reply', 'you_owe'), ('waiting_on', 'waiting_on_them')]:
        rows = [dict(anchor_key=key, contact_name=key, summary='Recorded commitment',
                     status='open', action_type=action_type, deadline=deadline, created_at=created)
                for key, deadline, created in examples]
        before = deepcopy(rows)
        parked = [{**row, 'status': 'parked'} for row in rows]
        out = lq._query_rows(rows, parked, log_diagnostic=False, now=now)
        assert [e['anchor_key'] for e in out[lane]] == expected
        assert [e['anchor_key'] for e in out['parked']] == expected
        assert [e['anchor_key'] for e in out[lane] if e['overdue']] == expected[:2]
        assert out['counts'][lane] == len(rows) and out['counts']['parked'] == len(rows)
        assert rows == before
        reverse = lq._query_rows(list(reversed(rows)), None, log_diagnostic=False, now=now)
        assert reverse[lane] == out[lane]


def test_projection_keeps_legacy_date_rows_and_raw_proofs_without_inventing_dates():
    from copy import deepcopy
    from datetime import datetime, timezone
    fields = ('deadline', 'chase_pending', 'last_chased_at', 'chase_after', 'handoff_asked_at')
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    for value, expected in [('Tonight', None), ('tomorrow', None), ('2026-02-30', None),
                            ('2026-1-2', None), ('20261010', None), ('2026-W41-6', None),
                            ('2026-10-10garbage', None), ('2026-10-10T99:99:99Z', None),
                            ('2026-10-10', '2026-10-10'), ('2026-10-10T23:00:00Z', '2026-10-10')]:
        row = {'anchor_key': 'legacy-source', 'action_type': 'reply', 'channel': 'email',
               'contact_name': 'Other Person', 'contact_identifier': 'other@example.com',
               'source': 'gmail', 'source_message_id': 'legacy-message',
               'created_at': '2026-10-01', 'summary': 'Send the requested report',
               **dict.fromkeys(fields, value)}
        before = deepcopy(row)
        result = lq._query_rows([row], None, log_diagnostic=False, now=now)
        assert result['counts'] == {'you_owe': 1, 'waiting_on_them': 0}
        projected = result['you_owe'][0]
        assert all(projected[field] == expected for field in fields)
        assert projected['overdue'] is (expected is not None)
        assert projected['loop_version'] == lq.loop_version(before)
        assert projected['loop_identity'] == lq.loop_identity(before)
        assert projected['anchor_key'] == row['anchor_key']
        assert row == before

def test_precise_ask_drives_label_and_is_bounded_in_request_context():
    today = lq._now_local("+00:00")
    row = {'contact_name': 'Austin', 'summary': 'Long introductory backstory',
           'ask': 'Respond about Project Cedar ' + 'detail ' * 40}
    standard = lq._entry(row, today, today.date().isoformat())
    contextual = lq._entry(row, today, today.date().isoformat(), include_request_context=True)
    assert 'ask' not in standard
    assert contextual['ask'] == row['ask'][:200] and len(contextual['ask']) == 200
    assert contextual['what'] == standard['what'] == row['ask'][:200]
    assert {key: value for key, value in contextual.items() if key in standard} == standard


def test_chase_state_is_surfaced_so_the_answer_can_name_it(tmp_path, monkeypatch):
    """"What am I waiting on" should be able to say "chased once, Tuesday" — the fields come from
    the ledger, which continuity_resolve alone writes."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    cdir = tmp_path / "knowledge" / "continuity"
    cdir.mkdir(parents=True)
    _write(cdir, "w", {"status": "open", "action_type": "waiting_on", "contact_name": "Vendor",
                       "summary": "awaiting quote", "created_at": "2026-08-01",
                       "chased_count": 1, "last_chased_at": "2026-08-04", "chase_after": "2026-08-07"})
    _write(cdir, "o", {"status": "open", "action_type": "reply", "contact_name": "Dhruv",
                       "summary": "reply re LOI", "created_at": "2026-08-01"})
    out = lq.query()
    w = out["waiting_on_them"][0]
    assert (w["chased_count"], w["last_chased_at"], w["chase_after"]) == (1, "2026-08-04", "2026-08-07")
    assert out["you_owe"][0]["chased_count"] == 0 and out["you_owe"][0]["last_chased_at"] is None


def test_follow_up_stale_is_something_you_owe_them(tmp_path, monkeypatch):
    """One predicate, in ledger_io: `waiting_on` is what THEY owe you. `follow_up_stale` resolves on
    YOUR outgoing message and expires on YOUR clock, so it belongs under you_owe — which is also
    where the resolver has always treated it. Variant spellings classify identically."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    cdir = tmp_path / "knowledge" / "continuity"
    cdir.mkdir(parents=True)
    _write(cdir, "s", {"status": "open", "action_type": "follow_up_stale", "contact_name": "Priya",
                       "summary": "sent the LOI", "created_at": "2026-08-01"})
    _write(cdir, "v", {"status": "open", "action_type": "Waiting-On ", "contact_name": "Vendor",
                       "summary": "awaiting quote", "created_at": "2026-08-01"})
    out = lq.query()
    assert [e["name"] for e in out["you_owe"]] == ["Priya"]
    assert [e["name"] for e in out["waiting_on_them"]] == ["Vendor"]


def test_chased_out_and_chase_pending_are_exposed_to_the_nudge_lane(tmp_path, monkeypatch):
    """The hand-off ("chased its quota") and today's proposed chase are both data, so the lanes
    that surface them need no second ledger reader."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    cdir = tmp_path / "knowledge" / "continuity"
    cdir.mkdir(parents=True)
    _write(cdir, "done", {"status": "open", "action_type": "waiting_on", "anchor_key": "done",
                          "contact_name": "Acme", "summary": "the contract", "chased_count": 2,
                          "created_at": "2026-08-01"})
    _write(cdir, "todo", {"status": "open", "action_type": "waiting_on", "anchor_key": "todo",
                          "contact_name": "Vendor", "summary": "a quote", "created_at": "2026-08-01",
                          "chase_pending": "2026-08-09"})
    rows = {e["anchor_key"]: e for e in lq.query()["waiting_on_them"]}
    assert rows["done"]["chased_out"] is True and rows["done"]["chase_pending"] is None
    assert rows["todo"]["chased_out"] is False and rows["todo"]["chase_pending"] == "2026-08-09"


def test_thread_id_is_surfaced_so_an_email_draft_can_be_threaded(tmp_path, monkeypatch):
    """The ledger has carried `source_thread_id` since the first row; the READ view dropped it, so
    an offer made off a loop had no way to thread. A reply that starts a new thread is worse than a
    link — so the thread id travels with the loop. Non-email loops carry ""."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    cdir = tmp_path / "knowledge" / "continuity"
    cdir.mkdir(parents=True)
    _write(cdir, "a", {"status": "open", "action_type": "reply", "channel": "email",
                       "contact_name": "Pegah", "contact_identifier": "pegah@example.com",
                       "summary": "SAFE allocation call", "source_thread_id": "T7"})
    _write(cdir, "b", {"status": "open", "action_type": "reply", "channel": "imessage",
                       "contact_name": "Dhruv", "contact_identifier": "+15551234567",
                       "summary": "ping back"})
    out = lq.query()
    by_name = {e["name"]: e for e in out["you_owe"]}
    assert by_name["Pegah"]["thread_id"] == "T7"
    assert by_name["Dhruv"]["thread_id"] == ""


def test_person_history_does_not_present_retired_bookkeeping_rows_as_dismissed_tasks(tmp_path):
    directory = tmp_path/'knowledge/continuity'
    directory.mkdir(parents=True)
    for anchor, status, resolution in (('open', 'open', ''), ('merged', 'dismissed', 'merged_duplicate'),
                                       ('retargeted', 'dismissed', 'user_retargeted'),
                                       ('dismissed', 'dismissed', 'user_dismissed'), ('done', 'resolved', 'user_resolved')):
        _write(directory, anchor, {'anchor_key':anchor, 'status':status, 'resolution':resolution,
                                  'canonical_id':'c_123456789abc', 'action_type':'follow_up',
                                  'source':'gmail', 'channel':'gmail', 'source_thread_id':'thread',
                                  'source_refs':[{'sourceType':'gmail', 'sourceId':'promise'}],
                                  'ask':anchor, 'created_at':'2026-10-18'})
    before = {path:path.read_bytes() for path in directory.glob('*.md')}
    result = lq.person_history(str(tmp_path), ['gmail'], canonical_id='c_123456789abc')
    assert {row['anchor_key'] for row in result['items']} == {'open', 'dismissed', 'done'}
    assert result['omitted'] == 0
    assert lq.person_history(str(tmp_path), [], canonical_id='c_123456789abc')['items'] == []
    assert {path:path.read_bytes() for path in directory.glob('*.md')} == before


def test_historical_relative_wording_has_capture_context_not_an_invented_deadline():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    rows = [
        {'contact_name': 'Austin', 'summary': 'Send the update tonight',
         'created_at': '2026-10-05', 'action_type': 'reply'},
        {'contact_name': 'Patrick', 'summary': 'Birthday greeting today',
         'created_at': '2026-10-04', 'action_type': 'follow_up'},
        {'contact_name': 'Dana', 'summary': 'Return signed agreement',
         'created_at': '2026-10-03', 'deadline': '2026-10-10', 'action_type': 'reply'},
    ]
    out = lq._query_rows(rows, None, log_diagnostic=False, now=now)
    by_name = {row['name']: row for row in out['you_owe']}
    for name, age in [('Austin', 6), ('Patrick', 7)]:
        assert by_name[name]['age_days'] == age
        assert by_name[name]['deadline'] is None and by_name[name]['overdue'] is False
    # A legacy birthday row remains an ordinary visible task; no new birthday classifier.
    assert by_name['Patrick']['what'] == 'Birthday greeting today'
    assert by_name['Dana']['deadline'] == '2026-10-10' and by_name['Dana']['overdue'] is True
    assert rows[0]['summary'] == by_name['Austin']['what'] == 'Send the update tonight'


def test_loop_presentation_guidance_preserves_visibility_without_relative_deadlines():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for skill in ('ask', 'loops'):
        text = (root / skill / 'SKILL.md').read_text()
        assert 'Keep birthday greetings visible' in text
        assert 'do not group them as urgent or time-sensitive merely because a birthday is mentioned' in text
        assert 'do not repeat it as a current deadline or resolve it from capture age' in text
        assert 'Do not infer completion or hide an open task because its wording is old' in text


def test_legacy_weekday_annotation_projects_only_the_explicit_calendar_day():
    from datetime import datetime, timezone
    # A historical display label is not evidence to change its explicit date.
    for suffix in ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun', 'FRI'):
        assert lq._calendar_day(f'2026-08-20 ({suffix})') == '2026-08-20'
    assert lq._calendar_day('2026-08-20T23:30:00-07:00') == '2026-08-20'
    for value in ('2026-02-30 (fri)', '2026-08-200 (fri)', '2026-8-20 (fri)',
                  '2026-08-20 (soon)', '2026-08-20 (fri) tomorrow',
                  'due 2026-08-20 (fri)', 'tomorrow (fri)', '2026-08-20-ish'):
        assert lq._calendar_day(value) is None
    row = {'contact_name': 'Dana', 'summary': 'Return the agreement',
           'action_type': 'reply', 'deadline': '2026-08-20 (fri)'}
    original = dict(row)
    out = lq._query_rows([row], None, log_diagnostic=False,
                         now=datetime(2026, 8, 21, tzinfo=timezone.utc))
    assert out['you_owe'][0]['deadline'] == '2026-08-20'
    assert out['you_owe'][0]['overdue'] is True
    assert row == original


def test_undated_reply_survives_real_followup_and_notification_caps(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from pathlib import Path
    import compose_notification as notification
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(notification, 'allowed', lambda source: False)
    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    rows = [dict(anchor_key=f'dated-{i:03}', contact_name='Dana', contact_identifier='dana@acme.com',
                 summary=f'Dated task {i}', status='open', action_type='reply',
                 deadline='2026-10-16', created_at='2026-10-15') for i in range(45)]
    rows.append(dict(anchor_key='old-reply', contact_name='Dana', contact_identifier='dana@acme.com',
                     summary='Answer the old question', status='open', action_type='reply',
                     created_at='2026-08-01'))
    view = lq._query_rows(rows, None, log_diagnostic=False, now=now)
    path = Path(ROOT) / 'followup/scripts/compose_followup.py'
    spec = importlib.util.spec_from_file_location('fair_followup', path)
    followup = importlib.util.module_from_spec(spec); spec.loader.exec_module(followup)
    context = followup._open_loops_context({'open_loops': view})
    assert 'old-reply | Answer the old question' in context
    assert len(context.splitlines()) == 40
    # The actual no-transcript notification renderer cuts to three lines.
    text = notification._post_meeting({'title': 'Dana call', 'open_loops': view['you_owe']})
    assert 'Dated task 0' in text and 'Dated task 1' in text and 'Answer the old question' in text
    future = [{**row, 'deadline': '2027-01-01'} for row in view['you_owe'] if row['deadline']]
    assert lq.rank_entries(future + [view['you_owe'][2]], '2026-10-16')[0]['anchor_key'] == 'old-reply'

    # Reapply fairness AFTER narrowing a crowded cross-person list to Dana.
    others = [{**view['you_owe'][2], 'anchor_key': f'other-{i}', 'identifier': 'sam@acme.com',
               'name': 'Sam', 'age_days': 100} for i in range(45)]
    crowded = lq.rank_entries(view['you_owe'] + others, now.date().isoformat())
    monkeypatch.setattr(notification.loops_query, 'query', lambda: {**view, 'you_owe': crowded})
    monkeypatch.setattr(notification, '_read_json_script', lambda *args: {})
    item = notification.enrich([{'kind': 'post_meeting', 'identifier': 'dana@acme.com',
                                 'person': 'Dana', 'title': 'Dana call'}], now)[0]
    assert len(item['open_loops']) == 46
    assert item['open_loops'][2]['anchor_key'] == 'old-reply'
    assert 'Answer the old question' in notification._post_meeting(item)


def test_person_history_empty_and_unpermitted_matches_have_identical_absence_scope(tmp_path):
    from datetime import datetime, timezone
    directory=tmp_path/'knowledge/continuity';directory.mkdir(parents=True)
    now=datetime(2026,10,20,tzinfo=timezone.utc)
    read=lambda grants:lq.person_history(str(tmp_path),grants,canonical_id='c_123456789abc',now=now)
    empty=read(['imessage'])
    assert empty['status']=='no_match' and empty['absence_is_authoritative'] is False
    assert 'no matching permitted rows' in empty['coverage']
    assert 'whether other canonical records exist' in empty['coverage']
    for status in ('open','resolved'):
        row={'anchor_key':status,'canonical_id':'c_123456789abc','contact_identifier':'private@example.test',
             'action_type':'follow_up','status':status,'created_at':'2026-10-18','ask':'PRIVATE-WITHHELD-ASK',
             'source':'gmail','channel':'gmail','source_thread_id':'private-thread',
             'source_refs':[{'sourceType':'gmail','sourceId':'private-promise'}]}
        if status=='resolved':
            row.update(resolution='replied',resolved_at='2026-10-19',resolution_evidence='PRIVATE-WITHHELD-DELIVERY',
                resolution_source_refs=[{'sourceType':'imessage','sourceId':'private-delivery'}])
        _write(directory,status,row)
        assert read(['imessage'])==empty,'all fields, including absence scope/counts, are independent of withheld matches'
    before={path:path.read_bytes() for path in directory.glob('*.md')}
    visible=read(['gmail','imessage'])
    assert visible['status']=='matched' and len(visible['items'])==2
    assert visible['absence_is_authoritative'] is False
    assert {path:path.read_bytes() for path in directory.glob('*.md')}==before
    unresolved=lq.person_history(str(tmp_path),['imessage'],now=now)
    assert unresolved['status']=='identity_unresolved' and unresolved['absence_is_authoritative'] is False


def _topic_row(anchor, **changes):
    return {'anchor_key': anchor, 'canonical_id': 'c_123456789abc',
            'contact_name': 'Alex Reed', 'contact_identifier': 'alex@example.test',
            'status': 'resolved', 'action_type': 'follow_up', 'created_at': '2026-10-18',
            'resolved_at': '2026-10-19T03:00:00Z', 'ask': 'Orion approval follow-up',
            'summary': 'Orion approval', 'source': 'gmail', 'channel': 'gmail',
            'source_thread_id': anchor + '-thread',
            'source_refs': [{'sourceType': 'gmail', 'sourceId': anchor + '-promise',
                             'snippet': 'Please confirm Orion approval.'}],
            'resolution': 'replied', 'resolution_evidence': 'Owner approved the final document.',
            'resolution_source_refs': [{'sourceType': 'imessage', 'sourceId': anchor + '-sent',
                                        'snippet': 'The final document was sent.'}], **changes}


def test_topic_history_projects_entire_source_union_before_matches_or_counts(tmp_path):
    from datetime import datetime, timezone
    directory = tmp_path / 'knowledge/continuity'
    directory.mkdir(parents=True)
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    read = lambda grants: lq.topic_history(str(tmp_path), grants, topic='Orion', now=now)
    empty = read(['gmail'])
    _write(directory, 'hidden', _topic_row('hidden'))
    _write(directory, 'unknown', _topic_row('unknown', source_refs=[{'sourceType': 'unknown', 'sourceId': 'no'}]))
    assert read(['gmail']) == empty  # missing closing-source permission exposes no existence/count
    result = read(['gmail', 'imessage'])
    assert result['status'] == 'matched' and result['omitted'] == 0
    assert result['absence_is_authoritative'] is False
    item = result['items'][0]
    assert item['completion_basis'] == 'source_evidence'
    assert item['source_refs'][0]['sourceId'] == 'hidden-promise'
    assert item['resolution_source_refs'][0]['sourceId'] == 'hidden-sent'
    assert item['resolution_evidence'] == 'Owner approved the final document.'
    before = {p: p.read_bytes() for p in directory.glob('*.md')}
    assert read(['gmail', 'imessage']) == result
    assert {p: p.read_bytes() for p in directory.glob('*.md')} == before


def test_history_keeps_original_and_latest_references_and_current_update_markers(tmp_path):
    from datetime import datetime, timezone
    directory = tmp_path / 'knowledge/continuity'
    directory.mkdir(parents=True)
    refs = [{'sourceType': 'gmail', 'sourceId': f'message-{i}',
             'timestamp': f'2026-10-{i + 1:02d}T09:00:00Z', 'snippet': f'Orion update {i}'}
            for i in range(8)]
    _write(directory, 'reopened', _topic_row('reopened', status='open', resolution='',
        resolution_evidence='', resolution_source_refs=[], source_refs=refs,
        reopened_at='2026-10-19', parked_at='2026-10-18', last_chased_at='2026-10-20',
        chased_count=2, source_timestamp='2026-10-01T09:00:00Z'))
    _write(directory, 'sibling', _topic_row('sibling', ask='Orion invoice', summary='Orion invoice'))
    result = lq.person_history(str(tmp_path), ['gmail', 'imessage'], canonical_id='c_123456789abc',
                               now=datetime(2026, 10, 21, tzinfo=timezone.utc))
    rows = {row['anchor_key']: row for row in result['items']}
    assert set(rows) == {'reopened', 'sibling'}
    row = rows['reopened']
    assert [ref['sourceId'] for ref in row['source_refs']] == ['message-0', 'message-5', 'message-6', 'message-7']
    assert row['source_timestamp'] == refs[0]['timestamp']
    assert row['source_timestamp_state'] == 'recorded'
    assert (row['status'], row['completion_basis']) == ('open', 'not_recorded')
    assert row['reopened_at'] == '2026-10-19' and row['parked_at'] == '2026-10-18'
    assert row['last_chased_at'] == '2026-10-20' and row['chased_count'] == 2
    assert row['truncated_fields'] == ['source_refs']
    assert rows['sibling']['status'] == 'resolved'
    assert rows['sibling']['completion_basis'] == 'source_evidence'
    _write(directory, 'malformed-chase', _topic_row('malformed-chase', chased_count={'unreadable': True}))
    malformed = lq.person_history(str(tmp_path), ['gmail', 'imessage'], canonical_id='c_123456789abc',
                                   now=datetime(2026, 10, 21, tzinfo=timezone.utc))
    assert next(row for row in malformed['items'] if row['anchor_key'] == 'malformed-chase')['chased_count'] is None


@pytest.mark.parametrize('active_status', ['open', 'waiting', 'failed', 'blocked'])
def test_topic_history_keeps_same_named_people_and_same_person_obligations_independent(tmp_path, active_status):
    from datetime import datetime, timezone
    directory = tmp_path / 'knowledge/continuity'
    directory.mkdir(parents=True)
    _write(directory, 'approval', _topic_row('approval'))
    _write(directory, 'invoice', _topic_row('invoice', ask='Orion invoice', summary='Orion invoice',
        status=active_status, resolution='', resolution_evidence='', resolution_source_refs=[]))
    _write(directory, 'other-person', _topic_row('other-person', canonical_id='c_987654321abc',
        contact_identifier='other-alex@example.test'))
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    result = lq.topic_history(str(tmp_path), ['gmail', 'imessage'], topic='Orion', now=now)
    assert result['status'] == 'ambiguous'
    assert {r['anchor_key'] for r in result['items']} == {'approval', 'invoice', 'other-person'}
    assert {r['canonical_id'] for r in result['items']} == {'c_123456789abc', 'c_987654321abc'}
    exact = lq.topic_history(str(tmp_path), ['gmail', 'imessage'], topic='Orion invoice', now=now)
    assert exact['status'] == 'matched'
    assert [(r['anchor_key'], r['status'], r['completion_basis']) for r in exact['items']] == [
        ('invoice', active_status, 'not_recorded')]
    assert exact['items'][0]['resolution_source_refs'] == []


def test_topic_history_requires_literal_tokens_and_never_falls_back_to_all_rows(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    directory = tmp_path / 'knowledge/continuity'
    directory.mkdir(parents=True)
    _write(directory, 'substring', _topic_row('substring', ask='Orionian approval', summary='Orionian approval',
        source_refs=[{'sourceType': 'gmail', 'sourceId': 'original', 'snippet': 'Orionian approval'}]))
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert lq.topic_history(str(tmp_path), ['gmail', 'imessage'], topic='Orion', now=now)['items'] == []
    monkeypatch.setattr(lq, 'permitted_rows', lambda *a: (_ for _ in ()).throw(AssertionError('unbounded fallback')))
    for topic in ('', '?!', '\u0301'):
        result = lq.topic_history(str(tmp_path), ['gmail'], topic=topic, now=now)
        assert result['status'] == 'needs_topic' and result['items'] == []
        assert result['absence_is_authoritative'] is False


def test_topic_history_caps_and_truncation_count_only_matching_permitted_rows(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timezone
    directory = tmp_path / 'knowledge/continuity'
    directory.mkdir(parents=True)
    monkeypatch.setattr(lq, 'HISTORY_LIMIT', 2)
    for i in range(5):
        _write(directory, f'visible-{i}', _topic_row(f'visible-{i}', resolution_evidence='approved ' + '界' * 2000))
    _write(directory, 'unrelated', _topic_row('unrelated', ask='Cedar procurement', summary='Cedar procurement',
        source_refs=[{'sourceType': 'gmail', 'sourceId': 'unrelated'}]))
    _write(directory, 'withheld', _topic_row('withheld', source='whatsapp', channel='whatsapp',
        source_thread_id='', source_refs=[{'sourceType': 'whatsapp', 'sourceId': 'private'}]))
    result = lq.topic_history(str(tmp_path), ['gmail', 'imessage'], topic='Orion',
                             now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert result['status'] == 'ambiguous' and 0 < len(result['items']) <= 2
    assert result['omitted'] == 5 - len(result['items'])
    assert all('resolution_evidence' in row['truncated_fields'] for row in result['items'])
    evidence = {key: value for key, value in result.items() if key != 'learning'}
    assert len(json.dumps(evidence).encode('utf-8')) <= lq.HISTORY_BYTES
    assert len(json.dumps(evidence, ensure_ascii=False).encode('utf-8')) <= lq.HISTORY_BYTES
    assert len(json.dumps(result).encode('utf-8')) <= lq.HISTORY_BYTES + 4 * 1024 + 32
    monkeypatch.setattr(lq, 'HISTORY_BYTES', 64 * 1024)
    larger = lq.topic_history(str(tmp_path), ['gmail', 'imessage'], topic='Orion',
                             now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert len(larger['items']) == 2 and larger['omitted'] == 3


def test_topic_learning_never_displaces_an_evidence_row_that_fits_history_budget(tmp_path):
    import json
    from datetime import datetime, timezone
    from test_history_coverage import _window, _write as write_checkpoint
    directory = tmp_path / 'knowledge/continuity'
    directory.mkdir(parents=True)
    refs = lambda source: [{'sourceType': source, 'sourceId': str(i), 'snippet': 'Orion ' + 'x' * 1194}
                           for i in range(4)]
    row = _topic_row('large', ask='Orion ' + 'a' * 1594, summary='Orion',
        resolution_evidence='e' * 1600, resolution_reason='r' * 1600, source_snippet='s' * 1200,
        source_refs=refs('gmail'), resolution_source_refs=refs('imessage'))
    _write(directory, 'large', row)
    write_checkpoint(tmp_path, {source: _window() for source in ('gmail', 'imessage', 'whatsapp')})
    result = lq.topic_history(str(tmp_path), ['gmail', 'imessage', 'whatsapp'], topic='Orion',
                              now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert len(result['items']) == 1 and result['omitted'] == 0
    assert result['items'][0]['resolution_source_refs'] == row['resolution_source_refs']
    evidence = {key: value for key, value in result.items() if key != 'learning'}
    assert 15 * 1024 < len(json.dumps(evidence).encode()) <= lq.HISTORY_BYTES
    assert len(json.dumps(result).encode()) > lq.HISTORY_BYTES
    assert len(json.dumps(result).encode()) <= 23 * 1024


def test_topic_history_unicode_whole_words_and_short_company_names(tmp_path):
    from datetime import datetime, timezone
    directory = tmp_path / 'knowledge/continuity'
    directory.mkdir(parents=True)
    topics = ['HP', 'AI', 'Café', '東京', 'दीपक']
    for i, topic in enumerate(topics):
        _write(directory, str(i), _topic_row(str(i), ask=f'{topic} approval', summary=f'{topic} approval'))
    _write(directory, 'pieces', _topic_row('pieces', ask='HPE AIs Caféteria Caféine 東京都 दीपकजी',
                                          summary='HPE AIs Caféteria Caféine 東京都 दीपकजी'))
    for i, topic in enumerate(['hp', 'AI', 'Cafe\u0301', '東京', 'दीपक']):
        result = lq.topic_history(str(tmp_path), ['gmail', 'imessage'], topic=topic,
                                 now=datetime(2026, 10, 20, tzinfo=timezone.utc))
        assert result['status'] == 'matched'
        assert [row['anchor_key'] for row in result['items']] == [str(i)]


def test_topic_history_can_find_subject_in_original_or_closing_reference(tmp_path):
    from datetime import datetime, timezone
    directory = tmp_path / 'knowledge/continuity'
    directory.mkdir(parents=True)
    _write(directory, 'original', _topic_row('original', ask='Approve document', summary='Approval'))
    _write(directory, 'closing', _topic_row('closing', ask='Send document', summary='Delivery',
        source_refs=[{'sourceType': 'gmail', 'sourceId': 'original'}],
        resolution_source_refs=[{'sourceType': 'imessage', 'sourceId': 'sent', 'snippet': 'Cedar document supplied.'}]))
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    for topic, expected in [('Orion', 'original'), ('Cedar', 'closing')]:
        result = lq.topic_history(str(tmp_path), ['gmail', 'imessage'], topic=topic, now=now)
        assert result['status'] == 'matched'
        assert [row['anchor_key'] for row in result['items']] == [expected]
