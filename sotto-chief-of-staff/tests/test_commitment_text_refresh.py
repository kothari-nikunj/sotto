"""Delayed brief gathering must supply observed text evidence to the existing resolver."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import partial
import json
import sys
import threading

import pytest

import compose_brief
import continuity_resolve as cr
import gather_google as gg
import loop_context as lc
import loops_query
import proactive_scan
import source_context as sc
from commitment_journey_fixture import DELIVERY, PHONE, PERSON_ID, reviewed_proposal, seed_capture

CHECK_AT = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-hosted')
    monkeypatch.delenv('SOTTO_PROACTIVE_CAPTURED_SOURCES', raising=False)


class EmptyMail:
    def users(self): return self
    def threads(self): return self
    def get(self, **kwargs): return self
    def execute(self): return {'messages': []}
    def close(self): pass


def gather(tmp_path, monkeypatch, rows, messages, *, hook=lambda: None, native_messages=()):
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/offline-synthetic-api')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: None)
    monkeypatch.setattr(gg, '_refresh_google_token', lambda: None)
    monkeypatch.setattr(gg, 'gather_gmail', lambda *a, **k: [])
    monkeypatch.setattr(gg, '_gmail_service', EmptyMail)
    if hasattr(lc, 'refresh_context'):
        monkeypatch.setattr(lc, 'refresh_context', partial(lc.refresh_context, now=CHECK_AT))
    else:  # The same production-gather regression is also runnable against the pre-fix baseline.
        monkeypatch.setattr(lc, 'refresh', partial(lc.refresh, now=CHECK_AT))
    calls = []
    def read(name, args, *, timeout):
        assert name == 'get_messages' and args == {'identifier': PHONE, 'limit': lc.MAX_MESSAGES}
        assert 0 < timeout <= lc.READ_BUDGET_SECONDS
        calls.append(name)
        hook()
        return {'identifier': PHONE, 'messages': deepcopy(messages)}
    monkeypatch.setattr(sc, 'bridge_call', read)
    paths = {key: tmp_path / (key + '.json') for key in ['local', 'gmail', 'cal', 'receipt']}
    # No fulfillment in the ordinary 24-hour snapshot. The previous day's source was not
    # replayed into this input and no current task state was seeded as historical truth.
    paths['local'].write_text(json.dumps({'imessage': list(native_messages), 'window_hours': 24,
        'source_coverage': {'imessage': {'complete': True, 'requested_since': '2026-10-19T09:00:00Z'}}}))
    monkeypatch.setattr(sys, 'argv', ['gather_google.py', '--loop-local', str(paths['local']),
        '--gmail-out', str(paths['gmail']), '--cal-out', str(paths['cal']),
        '--source-results-out', str(paths['receipt']), '--skip-calendar', '--skip-sent', '--skip-stale'])
    gg.main()
    return json.loads(paths['local'].read_text()), json.loads(paths['receipt'].read_text()), calls


def test_delayed_gather_text_then_exact_closure_suppression_and_restart_explanation(tmp_path, monkeypatch):
    _capture, rows = seed_capture(tmp_path)
    original = deepcopy(cr._load_items())
    local, receipt, calls = gather(tmp_path, monkeypatch, list(rows.values()), [DELIVERY])
    assert calls == ['get_messages']  # two asks and one verified person share a read
    assert local['imessage'] == [{**DELIVERY, '_loop_context': True,
                                 '_loop_context_sources': ['gmail', 'imessage']}]
    assert local['source_coverage']['imessage']['complete'] is True  # existing window unchanged
    context = receipt['gmail']['loop_context']
    assert context['complete'] is False and context['message_counts'] == {'gmail': 0, 'imessage': 1}
    assert context['requests'] <= lc.MAX_REQUESTS and context['targets'] <= lc.MAX_TARGETS
    assert cr._load_items() == original  # source refresh has no ledger effect
    pricing = rows['Send the pricing document']
    update = reviewed_proposal(pricing)
    # The existing critic receipt is required; gathering or sending a text alone cannot close it.
    unreviewed = {k: v for k, v in update.items() if k != 'criticReceipt'}
    before = cr.resolve({'today': '2026-10-20', 'local': local, 'loop_updates': [unreviewed]}, CHECK_AT)
    assert not before['resolved'] and len(before['active']) == 2
    result = cr.resolve({'today': '2026-10-20', 'local': local, 'loop_updates': [update]}, CHECK_AT)
    assert [r['summary'] for r in result['resolved']] == ['Send the pricing document']
    assert [r['summary'] for r in result['active']] == ['Send the launch checklist']
    pending = result['active'][0]['anchor_key']
    assert {r['anchor_key'] for r in compose_brief._protected_action_ledger({'now': CHECK_AT.isoformat()})} == {pending}
    assert {r['anchor_key'] for r in proactive_scan._open_loops()} == {pending}
    # Fresh on-disk history lookup after a later pass retains both actual source references.
    cr.resolve({'today': '2026-10-21'}, CHECK_AT + timedelta(days=1))
    history = loops_query.topic_history(str(tmp_path), ['gmail', 'imessage'], topic='pricing', now=CHECK_AT + timedelta(days=1))
    assert len(history['items']) == 1
    closed = history['items'][0]
    assert closed['status'] == 'resolved' and closed['completion_basis'] == 'source_evidence'
    assert closed['source_refs'] == pricing['source_refs']
    assert closed['resolution_source_refs'] == update['evidence']
    assert closed['closed_at'] == '2026-10-19T08:00:00+00:00'


@pytest.mark.parametrize('change', [
    {'timestamp': '2026-10-18T08:59:59Z'},  # before original promise
    {'timestamp': '2026-10-20T09:00:00.001Z'},  # future at replay time
    {'handle': '+14155550999'}, {'is_group_chat': True},
    {'is_from_me': 'true'}, {'text': 'x' * (lc.MAX_BODY_CHARS + 1)},
])
def test_targeted_read_never_supplies_wrong_identity_or_chronology(tmp_path, monkeypatch, change):
    _capture, rows = seed_capture(tmp_path)
    local, receipt, calls = gather(tmp_path, monkeypatch, list(rows.values()), [{**DELIVERY, **change}])
    assert calls == ['get_messages'] and local['imessage'] == []
    assert receipt['gmail']['loop_context']['complete'] is False
    assert all(row['status'] == 'open' for row in cr._load_items().values())


def test_source_revocation_discards_derived_text_and_preserves_native_rows(tmp_path, monkeypatch):
    _capture, rows = seed_capture(tmp_path)
    grants = {'gmail', 'imessage'}
    monkeypatch.setattr(sc, 'allowed', lambda source: source in grants)
    local, receipt, calls = gather(tmp_path, monkeypatch, list(rows.values()), [DELIVERY],
                                   hook=lambda: grants.discard('gmail'))
    assert calls == ['get_messages'] and local['imessage'] == []
    assert receipt['gmail']['loop_context']['status'] == 'disabled'
    grants.add('gmail')
    derived = {**DELIVERY, '_loop_context_sources': ['gmail', 'imessage']}
    native = {**DELIVERY, 'id': 'native-only', 'text': 'native recent context'}
    payload = {'imessage': [native, derived]}
    assert sc.used_sources(payload, already_projected=True) == ['gmail', 'imessage']
    grants.discard('gmail')
    assert sc.project_local(payload)['imessage'] == [native]
    assert len(payload['imessage']) == 2


def test_verified_graph_rejects_alias_shared_by_another_person(tmp_path):
    import knowledge as kg
    _capture, rows = seed_capture(tmp_path)
    person = kg.PersonFile(name='Same display name', canonical_id='c_def194', identifiers=[PHONE])
    kg.write_person_file(str(tmp_path / 'knowledge/people/c_def194.md'), person)
    plan = lc._imessage_plan(list(rows.values()), now=CHECK_AT, allowed=lambda _: True,
                             aliases=lc.graph_aliases())
    assert plan and all(target[1] != PHONE for target in plan)
    assert lc._imessage_plan([{**row, 'resolution_mode': 'explicit'} for row in rows.values()],
                             now=CHECK_AT, allowed=lambda _: True, aliases=lc.graph_aliases()) == []


def test_selected_handle_reads_share_budget_and_run_concurrently(tmp_path):
    _capture, rows = seed_capture(tmp_path)
    base = next(iter(rows.values()))
    targets = [{**base, 'anchor_key': f'ask-{i}', 'contact_identifier': f'+14155550{i:03d}', 'source_thread_id': f'thread-{i}'} for i in range(8)]
    arrivals = threading.Barrier(lc.MAX_TARGETS // 2)
    calls = []
    def read(identifier, *, timeout):
        calls.append(identifier)
        arrivals.wait(timeout=2)
        return {'identifier': identifier, 'messages': []}
    context, receipt = lc.refresh_context(targets, {}, EmptyMail(), gg.normalize_email,
        now=CHECK_AT, allowed=lambda _: True, aliases=lambda *_: (), read_messages=read)
    assert len(calls) == lc.MAX_TARGETS // 2 and len(set(calls)) == len(calls)
    assert receipt['requests'] <= lc.MAX_REQUESTS and receipt['checked'] <= lc.MAX_TARGETS
    assert context == {'emails': [], 'imessage': []} and 'error' not in receipt


def test_read_failure_keeps_debt_open_and_receipt_content_free(tmp_path, monkeypatch):
    _capture, rows = seed_capture(tmp_path)
    def fail():
        raise RuntimeError('private source body must not appear in receipt')
    local, receipt, calls = gather(tmp_path, monkeypatch, list(rows.values()), [DELIVERY], hook=fail)
    context = receipt['gmail']['loop_context']
    assert calls == ['get_messages'] and local['imessage'] == []
    assert context['error'] == 'read_failed' and context['complete'] is False
    assert 'private source body' not in json.dumps(context)
    assert all(row['status'] == 'open' for row in cr._load_items().values())


def test_wrong_canonical_id_and_unknown_primary_do_not_borrow_phone_alias(tmp_path):
    _capture, rows = seed_capture(tmp_path)
    row = next(iter(rows.values()))
    for changed in ({'canonical_id': 'c_missing'}, {'contact_identifier': 'unknown@example.test'}):
        plan = lc._imessage_plan([{**row, **changed}], now=CHECK_AT,
            allowed=lambda _: True, aliases=lc.graph_aliases())
        assert all(target[1] != PHONE for target in plan)
    assert lc._imessage_plan([{**row, 'created_at': '2026-10-20'}], now=CHECK_AT,
        allowed=lambda _: True, aliases=lc.graph_aliases()) == []


def test_partial_noncompletion_and_generic_sent_text_remain_open_without_review(tmp_path, monkeypatch):
    _capture, rows = seed_capture(tmp_path)
    partial_text = {**DELIVERY, 'text': 'The first pricing table is ready, but the document is incomplete.'}
    generic = {**DELIVERY, 'id': 'just-a-send', 'text': 'I will follow up shortly.'}
    local, _receipt, _calls = gather(tmp_path, monkeypatch, list(rows.values()), [partial_text, generic])
    result = cr.resolve({'today': '2026-10-20', 'local': local}, CHECK_AT)
    assert len(result['active']) == 2 and not result['resolved']


def test_late_native_result_is_not_admitted(tmp_path):
    _capture, rows = seed_capture(tmp_path)
    ticks = [0.0]
    def read(identifier, *, timeout):
        assert timeout == lc.READ_BUDGET_SECONDS
        ticks[0] = lc.READ_BUDGET_SECONDS + 1
        return {'identifier': identifier, 'messages': [DELIVERY]}
    context, receipt = lc.refresh_context(list(rows.values()), {}, EmptyMail(), gg.normalize_email,
        now=CHECK_AT, allowed=lambda _: True, read_messages=read, clock=lambda: ticks[0])
    assert context == {'emails': [], 'imessage': []}
    assert receipt['error'] == 'read_timeout' and receipt['complete'] is False


def test_native_direct_send_without_database_id_preserves_evidence_reference(tmp_path):
    from render_local import message_evidence_id
    _capture, rows = seed_capture(tmp_path)
    native = {key: value for key, value in DELIVERY.items() if key != 'id'}
    native.update(chat_guid=None, is_group_chat=False)
    context, receipt = lc.refresh_context(list(rows.values()), {}, EmptyMail(), gg.normalize_email,
        now=CHECK_AT, allowed=lambda _: True,
        read_messages=lambda identifier, **_: {'identifier': identifier, 'messages': [native]})
    observed = context['imessage'][0]
    assert observed['is_from_me'] is True and observed['handle'] == PHONE
    assert observed['timestamp'] == native['timestamp'] and observed['text'] == native['text']
    assert message_evidence_id(observed, 'imessage') == message_evidence_id(native, 'imessage')
    assert receipt['complete'] is False


def test_historical_inbound_is_loop_evidence_not_daily_triage_or_style(tmp_path, monkeypatch):
    import render_local as rl
    import triage_queue
    import style_extract
    _capture, rows = seed_capture(tmp_path)
    old = {**DELIVERY, 'id': 'old-unrelated', 'is_from_me': False,
           'text': 'Are you free Friday?', 'timestamp': '2026-10-19 08:30:00'}
    local, _receipt, _calls = gather(tmp_path, monkeypatch, list(rows.values()), [old, DELIVERY])
    assert rl._group_messages_into_threads(local['imessage'], 'imessage') == []
    assert triage_queue.local_queue(local, 'imessage') == []
    assert style_extract._adapt_read_local(local)['sent_messages'] == []
    prompt = compose_brief.build_prompt('NEEDS:{{imessage_needs_response}}\nHANDLED:{{imessage_handled}}',
        {'now': CHECK_AT.isoformat(), 'local': local, 'google': {}})
    daily, evidence = prompt.split('## LOOP_EVIDENCE', 1)
    assert old['text'] not in daily and DELIVERY['text'] not in daily
    assert old['text'] in evidence and DELIVERY['id'] in evidence
    assert 'gmail,imessage' in evidence and 'existing obligations only' in evidence
    manifest = compose_brief.build_data_manifest({'now': CHECK_AT.isoformat(), 'local': local})
    assert not manifest['imessage_contacts'] and DELIVERY['id'] in manifest['loop_evidence']
    # Raw rows still bind the existing fulfillment; the projection does not rewrite source IDs.
    proposal = reviewed_proposal(rows['Send the pricing document'])
    proof, why = cr._completion_evidence_result(rows['Send the pricing document'], proposal, local,
                                               observed_before=CHECK_AT)
    assert proof and why == 'evidence_verified'


def test_native_id_collision_stays_current_and_is_not_demoted(tmp_path, monkeypatch):
    import render_local as rl
    import style_extract
    _capture, rows = seed_capture(tmp_path)
    native = {**DELIVERY, 'timestamp': '2026-10-20 08:59:00',
              'text': 'Here is the current version I promised you today.', 'chat_guid': None,
              'is_group_chat': False}
    local, _receipt, _calls = gather(tmp_path, monkeypatch, list(rows.values()), [DELIVERY],
                                   native_messages=[native])
    assert local['imessage'] == [native] and '_loop_context' not in local['imessage'][0]
    assert len(rl._group_messages_into_threads(local['imessage'], 'imessage')) == 1
    assert style_extract._adapt_read_local(local)['sent_messages'][0]['text'] == native['text']
    assert rl._format_loop_message_evidence(local) == ''


@pytest.mark.parametrize('new_obligation', [True, False])
def test_supplemental_text_cannot_mint_a_new_obligation(tmp_path, monkeypatch, new_obligation):
    _capture, rows = seed_capture(tmp_path)
    old = {**DELIVERY, 'id': 'old-unrelated-promise', 'is_from_me': False,
           'text': 'I will send you the unrelated survey tomorrow.'}
    local, _receipt, _calls = gather(tmp_path, monkeypatch, list(rows.values()), [old])
    before = set(cr._load_items())
    action = {'type': 'waiting_on', 'channel': 'imessage', 'contactIdentifier': PHONE,
        'contextSummary': 'Unrelated survey', 'newObligation': new_obligation,
        'evidence': [{'sourceType': 'imessage', 'sourceId': old['id'], 'snippet': old['text']}]}
    cr.resolve({'today': '2026-10-20', 'local': local, 'new_actions': [action]}, CHECK_AT)
    assert set(cr._load_items()) == before


def test_text_failure_cannot_mask_gmail_quota_failure(tmp_path):
    _capture, rows = seed_capture(tmp_path)
    class Limited(EmptyMail):
        def execute(self): raise RuntimeError('private quota detail')
    def offline(*args, **kwargs): raise RuntimeError('private Bridge detail')
    context, receipt = lc.refresh_context(list(rows.values()), {}, Limited(), gg.normalize_email,
        now=CHECK_AT, allowed=lambda _: True, classify_error=lambda _: 'rate_limited', read_messages=offline)
    assert receipt['error'] == 'rate_limited' and receipt['imessage_error'] == 'read_failed'
    assert 'private' not in json.dumps(receipt)
    assert context == {'emails': [], 'imessage': []}


def test_whatsapp_jid_or_display_phone_never_becomes_raw_imessage_target(tmp_path):
    _capture, rows = seed_capture(tmp_path)
    row = next(iter(rows.values()))
    for primary in ('14155550193@s.whatsapp.net', '+1 (415) 555-0193'):
        plan = lc._imessage_plan([{**row, 'contact_identifier': primary}], now=CHECK_AT,
            allowed=lambda _: True, aliases=lambda *_: [PHONE])
        assert plan[0][1] == PHONE
        assert lc._imessage_plan([{**row, 'contact_identifier': primary}], now=CHECK_AT,
            allowed=lambda _: True, aliases=lambda *_: ()) == []


def test_optional_local_merge_failure_preserves_gather_outputs(tmp_path, monkeypatch):
    import jsonstore
    _capture, rows = seed_capture(tmp_path)
    original = jsonstore.write_atomic
    def fail_local(path, value, *args, **kwargs):
        if str(path) == str(tmp_path / 'local.json'):
            raise OSError('private disk reason')
        return original(path, value, *args, **kwargs)
    monkeypatch.setattr(jsonstore, 'write_atomic', fail_local)
    local, receipt, _calls = gather(tmp_path, monkeypatch, list(rows.values()), [DELIVERY])
    assert local['imessage'] == []
    assert json.loads((tmp_path / 'gmail.json').read_text()) == []
    assert json.loads((tmp_path / 'cal.json').read_text()) == []
    assert receipt['gmail']['loop_context']['imessage_error'] == 'context_merge_failed'
    assert 'private disk' not in json.dumps(receipt)


def test_native_naive_clock_respects_original_ask_and_preserves_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    _capture, rows = seed_capture(tmp_path)
    before = {**DELIVERY, 'id': 'earlier-native', 'timestamp': '2026-10-18 01:59:59', 'chat_guid': None}
    after = {**DELIVERY, 'id': 'later-native', 'timestamp': '2026-10-18 02:00:00.001', 'chat_guid': None}
    context, _receipt = lc.refresh_context(list(rows.values()), {}, EmptyMail(), gg.normalize_email,
        now=CHECK_AT, allowed=lambda _: True,
        read_messages=lambda identifier, **_: {'identifier': identifier, 'messages': [before, after]})
    assert [row['id'] for row in context['imessage']] == ['later-native']
    assert context['imessage'][0]['timestamp'] == after['timestamp']


@pytest.mark.parametrize('message_cap', [24, 100])
def test_busy_gmail_cannot_spend_all_output_capacity_before_valid_text(tmp_path, monkeypatch, message_cap):
    import base64
    # The normal cap exercises row pressure; a larger test-only cap isolates character pressure.
    monkeypatch.setattr(lc, 'MAX_MESSAGES', message_cap)
    _capture, rows = seed_capture(tmp_path)
    base = next(iter(rows.values()))
    targets = [{**base, 'anchor_key': f'busy-{i}', 'source_thread_id': '',
                'contact_identifier': f'person{i}@example.test'} for i in range(2)]
    class Busy(EmptyMail):
        def messages(self): return self
        def list(self, **kwargs): self.query = kwargs['q']; self.mode = 'list'; return self
        def get(self, **kwargs): self.tid = kwargs['id']; self.mode = 'get'; return self
        def execute(self):
            if self.mode == 'list':
                return {'messages': [{'threadId': self.query + str(i)} for i in range(2)]}
            return {'messages': [{'id': self.tid + str(i), 'threadId': self.tid,
                'internalDate': str(int(CHECK_AT.timestamp() * 1000) - i), 'labelIds': ['INBOX'],
                'payload': {'mimeType': 'text/plain', 'headers': [],
                'body': {'data': base64.urlsafe_b64encode(('m' * 2000).encode()).decode()}}} for i in range(6)]}
    aliases = lambda identifier, *_: ['+1415555019' + identifier[6]]
    def read(identifier, **_):
        return {'identifier': identifier, 'messages': [{**DELIVERY, 'id': identifier, 'handle': identifier}]}
    context, receipt = lc.refresh_context(targets, {}, Busy(), gg.normalize_email, now=CHECK_AT,
        allowed=lambda _: True, aliases=aliases, read_messages=read)
    assert context['emails'] and len(context['imessage']) == 2
    assert sum(map(len, context.values())) <= lc.MAX_MESSAGES
    assert sum(len(row.get('body') or row.get('text') or '') for lane in context.values() for row in lane) <= lc.MAX_CONTEXT_CHARS
    by_thread = {}
    for row in context['emails']:
        by_thread.setdefault(row['threadId'], []).append(row['id'])
    for thread, ids in by_thread.items():
        # The newest message (0) may contain fulfillment. Never leave older messages without it,
        # and keep each surviving transcript in its original chronological order.
        assert ids == [thread + str(i) for i in range(5, -1, -1)]
    assert len(by_thread) == 3
    assert receipt['omitted_messages']['gmail'] == 6
    assert receipt['message_counts'] == {'gmail': 18, 'imessage': 2}
    assert receipt['messages'] == 20
    assert receipt['requests'] <= lc.MAX_REQUESTS and receipt['complete'] is False


def test_brief_fact_binding_accepts_current_native_but_not_supplemental_text(tmp_path):
    historical = {**DELIVERY, '_loop_context': True,
                  '_loop_context_sources': ['gmail', 'imessage']}
    native = {**DELIVERY, 'id': 'current-fact', 'timestamp': '2026-10-20 08:59:00'}
    out = {'extracted_knowledge': {'person_updates': [{'canonical_id': PERSON_ID, 'facts': [
        {'fact': 'historical-only fact', 'source_ref': DELIVERY['id']},
        {'fact': 'current native fact', 'source_ref': native['id']},
    ]}]}}
    compose_brief._bind_brief_fact_evidence(out, {'local': {'imessage': [historical, native]}})
    facts = out['extracted_knowledge']['person_updates'][0]['facts']
    assert [fact['fact'] for fact in facts] == ['current native fact']
    assert historical['id'] == DELIVERY['id']  # raw closure evidence remains untouched


@pytest.mark.parametrize('message_cap,character_cap', [(24, 48000), (100, 21000)])
def test_gmail_only_admits_whole_thread_or_omits_it_without_losing_newest_reply(
        tmp_path, monkeypatch, message_cap, character_cap):
    import base64
    monkeypatch.setattr(lc, 'MAX_MESSAGES', message_cap)
    monkeypatch.setattr(lc, 'MAX_CONTEXT_CHARS', character_cap)
    _capture, rows = seed_capture(tmp_path)
    base = next(iter(rows.values()))
    targets = [{**base, 'anchor_key': f'capacity-{i}', 'source_thread_id': f'thread-{i}'}
               for i in range(5)]
    class FiveMessages(EmptyMail):
        def get(self, **kwargs): self.tid = kwargs['id']; return self
        def execute(self):
            return {'messages': [{'id': self.tid + str(i), 'threadId': self.tid,
                'internalDate': str(int(CHECK_AT.timestamp() * 1000) - i), 'labelIds': ['INBOX'],
                'payload': {'mimeType': 'text/plain', 'headers': [], 'body': {'data': base64.urlsafe_b64encode(
                    (('Fulfilled ' if i == 0 else 'Original ask ').ljust(1000, 'x')).encode()).decode()}}}
                for i in range(5)]}
    context, receipt = lc.refresh_context(targets, {}, FiveMessages(), gg.normalize_email,
        now=CHECK_AT, allowed=lambda _: True)
    by_thread = {}
    for row in context['emails']:
        by_thread.setdefault(row['threadId'], []).append(row)
    assert len(by_thread) == 4 and len(context['emails']) == 20
    for thread, messages in by_thread.items():
        assert [row['id'] for row in messages] == [thread + str(i) for i in range(4, -1, -1)]
        assert messages[-1]['body'].startswith('Fulfilled ')
    assert context['imessage'] == []
    assert len(context['emails']) <= lc.MAX_MESSAGES
    assert sum(len(row['body']) for row in context['emails']) <= lc.MAX_CONTEXT_CHARS
    assert receipt['omitted_messages'] == {'gmail': 5, 'imessage': 0}
    assert receipt['messages'] == 20 and receipt['complete'] is False
