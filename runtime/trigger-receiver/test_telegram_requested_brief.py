"""Requested Telegram briefs retain their admitted route through durable work and delivery."""
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import runtime_owner_brief_request as tool
import telegram_channel_proof as telegram
from test_requested_brief import rec, PACK, HERE
from test_telegram_channel_proof import _fixture, OWNER, TOKEN, ZONE
from test_telegram_owner_commands import IDENTITY, ROOT, capability

__all__ = ['rec']


@pytest.fixture
def context(rec, tmp_path, monkeypatch):
    data, link, expected, client, opener, policy = _fixture(tmp_path)
    for key, value in policy.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture-key')
    monkeypatch.setenv('SOTTO_TIMEZONE', ZONE)
    monkeypatch.setenv('SOTTO_SKILLS_ROOT', str(PACK))
    monkeypatch.setattr(rec, 'DATA', str(data))
    monkeypatch.setattr(rec, '_local_now', lambda: datetime.now(timezone.utc).astimezone(ZoneInfo(ZONE)))
    monkeypatch.setattr(rec._runtime_adapter(), '_client', lambda **_: client)
    profile = rec._runtime_adapter()._fixed_five_module()
    monkeypatch.setattr(profile, '_PACKAGED_ROOT', PACK)
    monkeypatch.setattr(profile, '_PACKAGED_RECEIVER', HERE)
    monkeypatch.setattr(profile, '_image_owned', lambda meta: not meta.st_mode & 0o022)
    authority = {'version': 2, 'complete': True, 'revision': 'a' * 64,
                 'bridge_sources': [], 'google_sources': []}
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: authority)
    policy.update(GOOGLE_AI_API_KEY='fixture-key', _configured_tz_name=lambda: ZONE,
                  _tz_resolve=ZoneInfo, _source_authority=lambda: authority)
    return data, link, client, opener, policy, authority


def request(event='d' * 64):
    text = 'Give me my evening brief'
    args = dict(kind='evening', refresh=False, remainder=None, provider_message_id='provider-message')
    canonical = json.dumps(args, sort_keys=True, separators=(',', ':'))
    action = hashlib.sha256(json.dumps([event, 'owner_brief_request', canonical], separators=(',', ':')).encode()).hexdigest()
    return dict(version=1, operation='admit', identity=IDENTITY, action_id=action, event_id=event,
        payload_hash='b' * 64, text_hash=hashlib.sha256(text.encode()).hexdigest(), text=text,
        admitted_at_ms=int(time.time() * 1000), provider_thread_id=IDENTITY['route_id'], args=args)


def call(rec, context, body):
    data, _, client, _, policy, _ = context
    return tool.perform(body, bearer=capability.derive(ROOT, body['identity']), root=ROOT,
        policy=policy, data_root=data, capability=capability, activation=None,
        telegram_verifier=telegram, chat_client=client, queue=rec.WORK_QUEUE,
        admit=rec.request_owner_brief)


def admit(rec, context):
    body = request()
    receipt = call(rec, context, body)
    assert receipt['status'] == 'committed'
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    return body, receipt, job


def test_telegram_admission_is_atomic_exact_and_not_a_generic_owner_bypass(rec, context):
    body, receipt, job = admit(rec, context)
    binding = json.loads(job['admission'])['telegram_binding']
    assert binding['owner_id'] == OWNER
    assert binding['bot_fingerprint'] == IDENTITY['bot_fingerprint']
    assert binding['provider_thread_id'] == 'telegram:' + OWNER
    assert binding['source_revision'] == 'a' * 64
    assert call(rec, context, body) == receipt
    assert call(rec, context, dict(body, operation='inspect')) == receipt
    assert call(rec, context, request(event='e' * 64))['code'] == 'event_conflict'
    assert rec.request_owner_brief(dict(kind='evening', refresh=False, owner=OWNER,
        chat_id=OWNER, message_id='generic-no-admission'))[0] == 403
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1


@pytest.mark.parametrize('change', ['owner', 'bot', 'node-proof', 'source', 'revision', 'profile'])
def test_changed_authority_holds_composition_saved_result_feedback_and_send(rec, context, monkeypatch, change):
    data, link, _, opener, policy, authority = context
    body, receipt, job = admit(rec, context)
    label, jid = job['payload']['label'], job['id']
    result = dict(text='Exact already composed brief', label=label, effects=[])
    claimed = rec.WORK_QUEUE.claim(data, 'first-worker')
    assert claimed['id'] == jid
    rec.WORK_QUEUE.save_result(data, jid, 'first-worker', result)
    rec.WORK_QUEUE.release(data, jid, 'first-worker')
    payload = dict(label=label, run_id=jid, target='telegram', effects=[])
    assert rec._delivery_valid(payload)
    if change == 'owner':
        link.write_text(json.dumps({'bot_token': TOKEN, 'allowed_user': 999}))
    elif change == 'bot':
        link.write_text(json.dumps({'bot_token': '123456789:rotatedSecret_ABCdef0123456789', 'allowed_user': int(OWNER)}))
    elif change == 'node-proof':
        opener.wire['credential_proof'] = '0' * 64
    elif change == 'source':
        authority['complete'] = False
    elif change == 'revision':
        authority['revision'] = 'f' * 64
    else:
        monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host-photon-fixed-five-v1')
        policy['SOTTO_RUNTIME_PROFILE'] = 'self-host-photon-fixed-five-v1'
    assert not rec._requested_telegram_job_current(job)
    assert not rec._delivery_valid(payload)
    with pytest.raises(rec._WorkDeferredError):
        rec._execute_work(job)
    with pytest.raises(rec._WorkDeferredError):
        rec._deliver_text(result['text'], label, run_id=jid)
    with pytest.raises(tool.UnavailableError):
        call(rec, context, dict(body, operation='inspect'))
    claimed = rec.WORK_QUEUE.claim(data, 'restarted-worker')
    monkeypatch.setattr(rec, '_execute_work', lambda *_: pytest.fail('must retain existing composition'))
    monkeypatch.setattr(rec, '_deliver_text', lambda *_, **__: pytest.fail('must not contact delivery'))
    rec._work_one(claimed)
    retained = rec.WORK_QUEUE.get(data, jid)
    assert retained['result'] == result and retained['status'] == 'ready'
    with rec.WORK_QUEUE._db(data) as db:
        db.execute("UPDATE jobs SET status='failed',finished=? WHERE id=?", (time.time(), jid))
    calls = []
    monkeypatch.setattr(rec.OUTBOX, 'enqueue_work_feedback', lambda job, **kw: calls.append(kw) or False)
    rec._requested_brief_feedback()
    assert calls == [{'enqueue': False}]


def test_terminal_feedback_keeps_binding_after_queue_payload_and_feedback_scrub(rec, context, monkeypatch):
    _, _, job = admit(rec, context)
    jid = job['id']
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET status='failed',payload='{}',finished=? WHERE id=?", (time.time(), jid))
    rec._requested_brief_feedback()
    rows = json.loads(Path(rec.OUTBOX.path()).read_text())['rows']
    notice = next(row['payload'] for row in rows if row['payload'].get('feedback_for') == jid)
    assert notice['feedback_owner'] == notice['feedback_chat_id'] == OWNER
    assert rec.WORK_QUEUE.get(rec.DATA, jid)['feedback'] is None
    assert rec._delivery_valid(notice)
    assert not rec._delivery_valid({**notice, 'feedback_owner': '999'})
    context[-1]['complete'] = False
    assert not rec._delivery_valid(notice)
    context[-1]['complete'] = True
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute('DELETE FROM jobs WHERE id=?', (jid,))
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', OWNER)
    assert not rec._delivery_valid(notice), 'pruned binding cannot borrow ambient owner identity'


def test_legacy_telegram_work_cannot_borrow_current_pairing(rec, context):
    label = 'requested:' + 'f' * 32 + ':sotto-evening-brief'
    jid = rec.WORK_QUEUE.enqueue(rec.DATA, 'run', {'label': label}, key='legacy-telegram',
        feedback={'label': label, 'target': 'telegram', 'owner': OWNER, 'chat_id': OWNER})
    job = rec.WORK_QUEUE.get(rec.DATA, jid)
    assert not rec._requested_telegram_job_current(job)
    assert not rec._delivery_valid(dict(label=label, run_id=jid, target='telegram'))


def test_saved_brief_handoff_unknown_receipt_survives_restart_without_resend(rec, context, monkeypatch):
    from test_outbox import _load_outbox
    data, link, _, _, _, _ = context
    _, _, job = admit(rec, context)
    jid, label = job['id'], job['payload']['label']
    body = 'Needs Attention Now\nReview proposal\n\nShould Handle Today\nReply to Alex\n\nComing Up\nTomorrow planning\n\nStill open\nSend document'
    evidence = rec._composition_evidence(label)
    assert evidence
    result = dict(text=body, label=label, effects=[],
        source_revision_at_composition=evidence['source_revision'],
        route_digest_at_composition=evidence['route_digest'],
        source_text_sha256=hashlib.sha256(body.encode()).hexdigest())
    first = rec.WORK_QUEUE.claim(data, 'before-crash')
    rec.WORK_QUEUE.save_result(data, jid, first['owner'], result)
    rec.WORK_QUEUE.release(data, jid, first['owner'])
    sends = []
    monkeypatch.setattr(rec, '_execute_work', lambda *_: pytest.fail('saved composition must not repeat'))
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'delivery_ready', lambda _: True)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda *args, **kwargs: sends.append(args) or
                        (False, 'response lost', {'acceptance': 'unknown', 'message_ids': ['prefix-1']}))
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'gallery_receipt', lambda _: None)
    rec._work_one(rec.WORK_QUEUE.claim(data, 'after-crash'))
    assert len(sends) == 1
    assert rec.WORK_QUEUE.get(data, jid)['status'] == 'done'
    rows = json.loads(Path(rec.OUTBOX.path()).read_text())['rows']
    assert len(rows) == 1 and rows[0]['acceptance'] == 'unknown'
    assert rows[0]['acceptance_uncertain'] is True
    repeated = request(event='e' * 64)
    repeated['args'] = {**repeated['args'], 'provider_message_id': 'next-message'}
    canonical = json.dumps(repeated['args'], sort_keys=True, separators=(',', ':'))
    repeated['action_id'] = hashlib.sha256(json.dumps([repeated['event_id'], 'owner_brief_request', canonical], separators=(',', ':')).encode()).hexdigest()
    assert call(rec, context, repeated)['code'] == 'pending_delivery'
    restarted = _load_outbox('telegram_brief_restarted_outbox')
    restarted.HOOKS.update(rec.OUTBOX.HOOKS)
    link.write_text(json.dumps({'bot_token': TOKEN, 'allowed_user': 999}))
    with rec.CONNECTORS.json_transaction(restarted.path(), default={}) as state:
        state['rows'][0]['next_at'] = 0
    assert restarted._attempt(rows[0]['id']) is False
    assert len(sends) == 1
    retained = json.loads(Path(restarted.path()).read_text())['rows'][0]
    assert retained['acceptance_uncertain'] is True and retained['acceptance'] == 'unknown'
