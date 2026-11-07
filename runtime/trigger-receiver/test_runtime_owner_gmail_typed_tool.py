"""Authentic selected typed review lineage and local learning after provider commitment."""
import json

import pytest

import runtime_owner_gmail_draft_tool as tool
from test_runtime_owner_gmail_send_tool import sending, setup, context  # noqa: F401
from test_runtime_owner_gmail_draft_tool import request

__all__ = ['sending', 'setup', 'context']


def typed(sending, *, send=True, baseline=None, event='d' * 64, text='I cannot attend.'):
    args = {'operation': 'propose_send_typed' if send else 'propose_typed',
            'message_id': 'message-123', 'body': text, 'action_type': 'decline'}
    if baseline is not None:
        args['baseline'] = baseline
    body = request(args, 'preview', event=event, text='Draft a decline to Alice.')
    preview = sending[-1](body)
    assert preview['status'] == 'preview'
    proposal = {key: body[key] for key in ('event_id', 'action_id')}
    proposal.update(args_hash=tool.digest(args), accepted_dispatch_id='accepted-typed-review', source_revision='c' * 64)
    return body, preview, proposal


def rows(data, name):
    path = data / name
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_preview_replay_no_offered_row_until_accepted_and_revoked_withheld(sending):
    body, preview, _ = typed(sending)
    assert not rows(sending[0], 'events/drafts.jsonl')
    sending[2].get = lambda **kw: pytest.fail('historical preview must not re-fetch')
    assert sending[-1]({**body, 'operation': 'inspect'}) == preview
    sending[1]['_source_authority'] = lambda: {'complete': False}
    assert sending[-1]({**body, 'operation': 'inspect'})['code'] == 'authority_changed'
    assert not rows(sending[0], 'events/drafts.jsonl')


def test_typed_decline_edit_uses_root_and_logs_even_below_fuzzy_threshold(sending):
    _, original, baseline = typed(sending)
    _, edited, proposal = typed(sending, baseline=baseline, event='e' * 64,
                               text='Thank you for inviting me. Another commitment prevents joining the gathering.')
    assert edited['review']['draft_origin'] == original['review']['draft_origin']
    offered = rows(sending[0], 'events/drafts.jsonl')
    assert len(offered) == 1 and offered[0]['text'] == 'I cannot attend.'
    body = request({'operation': 'send', 'review': edited['review'], 'proposal': proposal})
    result = sending[-1](body)
    assert result['status'] == 'committed' and result['learning'] == 'recorded'
    learned = rows(sending[0], 'outcomes.jsonl')
    assert len(learned) == 1
    assert learned[0]['outcome'] == 'edited_and_sent' and learned[0]['tier'] == 'review'
    assert learned[0]['action_type'] == 'decline' and learned[0]['source_event_id'] == 'email:sent-123'
    assert 'source_event_ts' not in learned[0]
    assert len(sending[2].posts) == 1
    assert sending[-1]({**body, 'operation': 'inspect'}) == result
    assert len(rows(sending[0], 'outcomes.jsonl')) == 1


def test_learning_loss_reopen_repairs_without_resend(sending, monkeypatch):
    _, preview, proposal = typed(sending)
    body = request({'operation': 'send', 'review': preview['review'], 'proposal': proposal})
    import log_outcome
    original = log_outcome.log
    monkeypatch.setattr(log_outcome, 'log', lambda rec: (_ for _ in ()).throw(OSError('local log unavailable')))
    result = sending[-1](body)
    assert result['status'] == 'committed' and result['learning'] == 'pending'
    assert len(sending[2].posts) == 1 and not rows(sending[0], 'outcomes.jsonl')
    monkeypatch.setattr(log_outcome, 'log', original)
    repaired = sending[-1]({**body, 'operation': 'inspect'})
    assert repaired['outcome'] == result['outcome'] and repaired['learning'] == 'recorded'
    assert len(sending[2].posts) == 1
    assert rows(sending[0], 'outcomes.jsonl')[0]['outcome'] == 'executed'
    # Completed local learning survives original exhaust retention.
    (sending[0] / 'outcomes.jsonl').unlink()
    assert sending[-1]({**body, 'operation': 'inspect'}) == repaired
    assert not (sending[0] / 'outcomes.jsonl').exists()


def test_pending_learning_survives_body_compaction_until_exact_repair(sending, monkeypatch):
    _, preview, proposal = typed(sending)
    body = request({'operation': 'send', 'review': preview['review'], 'proposal': proposal})
    import log_outcome
    original = log_outcome.log
    monkeypatch.setattr(log_outcome, 'log', lambda rec: (_ for _ in ()).throw(OSError('local log unavailable')))
    result = sending[-1](body)
    assert result['status'] == 'committed' and result['learning'] == 'pending'
    assert len(sending[2].posts) == 1
    path = sending[0] / 'events/owner-gmail-drafts.json'
    aged_at = body['admitted_at_ms'] / 1000 + 31 * 86400
    assert tool.compact(path, now=aged_at) == 1  # the aged preview compacts; pending learning does not
    assert tool._load(path)['actions'][body['event_id']]['learning_record']
    assert preview['review']['body'] in path.read_text()
    monkeypatch.setattr(log_outcome, 'log', original)
    repaired = sending[-1]({**body, 'operation': 'inspect'})
    assert repaired['status'] == 'committed' and repaired['learning'] == 'recorded'
    assert len(sending[2].posts) == 1
    assert len(rows(sending[0], 'outcomes.jsonl')) == 1
    assert tool.compact(path, now=aged_at) == 1
    assert tool._load(path)['actions'][body['event_id']]['proof']['compacted'] is True
    assert preview['review']['body'] not in path.read_text()
    assert sending[-1]({**body, 'operation': 'inspect'}) == repaired
    assert len(sending[2].posts) == 1


def test_unrepairable_pending_learning_still_ages_out_after_revocation(sending, monkeypatch):
    # Revoked consent blocks the learning repair forever; the reviewed words must not outlive
    # a second retention window, while the content-free committed receipt stays replayable.
    _, preview, proposal = typed(sending)
    body = request({'operation': 'send', 'review': preview['review'], 'proposal': proposal})
    import log_outcome
    monkeypatch.setattr(log_outcome, 'log', lambda rec: (_ for _ in ()).throw(OSError('local failure')))
    result = sending[-1](body)
    assert result['status'] == 'committed' and result['learning'] == 'pending'
    sending[1]['_source_authority'] = lambda: {'complete': False}
    path = sending[0] / 'events/owner-gmail-drafts.json'
    start = body['admitted_at_ms'] / 1000
    tool.compact(path, now=start + 31 * 86400)
    assert preview['review']['body'] in path.read_text()  # one window of grace for a repair
    tool.compact(path, now=start + 61 * 86400)
    saved = tool._load(path)['actions'][body['event_id']]
    assert saved['proof']['compacted'] is True and saved['status'] == 'committed'
    assert 'learning_record' not in saved and 'row' not in saved
    assert preview['review']['body'] not in path.read_text()
    assert len(sending[2].posts) == 1


def test_save_only_draft_created_unknown_send_never_executed(sending):
    _, preview, proposal = typed(sending, send=False)
    body = request({'operation': 'save', 'review': preview['review'], 'proposal': proposal})
    result = sending[-1](body)
    assert result['learning'] == 'recorded' and not sending[2].posts
    assert rows(sending[0], 'outcomes.jsonl')[0]['outcome'] == 'draft_created'


def test_substitution_and_unaccepted_baseline_refused(sending):
    _, _, baseline = typed(sending)
    baseline['action_id'] = 'f' * 64
    args = {'operation': 'propose_send_typed', 'message_id': 'message-123',
            'body': 'Different', 'action_type': 'decline', 'baseline': baseline}
    assert sending[-1](request(args, 'preview', event='e' * 64))['status'] == 'rejected'
    assert not rows(sending[0], 'events/drafts.jsonl') and not sending[2].posts


def test_bookkeeping_ledger_read_failure_keeps_known_send(sending, monkeypatch):
    _, preview, proposal = typed(sending)
    body = request({'operation': 'send', 'review': preview['review'], 'proposal': proposal})
    writer = tool._preferences()
    monkeypatch.setattr(tool, '_preferences', lambda: writer)
    original = writer._durable_preferences_replace
    def persist(path, doc):
        original(path, doc)
        if doc['actions'].get(body['event_id'], {}).get('status') == 'committed':
            monkeypatch.setattr(tool, '_load', lambda path: (_ for _ in ()).throw(OSError('bookkeeping read unavailable')))
    monkeypatch.setattr(writer, '_durable_preferences_replace', persist)
    result = sending[-1](body)
    assert result['status'] == 'committed' and result['learning'] == 'pending'
    assert len(sending[2].posts) == 1


def test_unknown_provider_no_executed_outcome(sending):
    _, preview, proposal = typed(sending)
    sending[2].loss = True
    body = request({'operation': 'send', 'review': preview['review'], 'proposal': proposal})
    assert sending[-1](body)['status'] == 'unknown'
    assert not rows(sending[0], 'outcomes.jsonl')
    assert sending[-1]({**body, 'operation': 'inspect'})['status'] == 'unknown'
    assert len(sending[2].posts) == 1


@pytest.mark.parametrize('channel,expected', [('email', 1), ('imessage', 0), ('unknown', 0)])
def test_executed_confirms_only_existing_permitted_extracted_sample(sending, monkeypatch, channel, expected):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host')
    data = sending[0]
    sample = {'channel': channel, 'date': '2026-10-05', 'recipient': 'alice@example.com',
              'text': 'I cannot attend.', 'bucket': 'work_email', 'source': 'extracted'}
    path = data / 'style.json'
    path.write_text(json.dumps({'recent': [sample], 'canonical': {}, 'confirmed': []}))
    _, preview, proposal = typed(sending)
    body = request({'operation': 'send', 'review': preview['review'], 'proposal': proposal})
    assert sending[-1](body)['learning'] == 'recorded'
    assert len(json.loads(path.read_text())['confirmed']) == expected


def test_revoked_learning_stays_pending_but_send_receipt_known(sending, monkeypatch):
    _, preview, proposal = typed(sending)
    body = request({'operation': 'send', 'review': preview['review'], 'proposal': proposal})
    import log_outcome
    original = log_outcome.log
    monkeypatch.setattr(log_outcome, 'log', lambda rec: (_ for _ in ()).throw(OSError('local failure')))
    result = sending[-1](body)
    sending[1]['_source_authority'] = lambda: {'complete': False}
    monkeypatch.setattr(log_outcome, 'log', original)
    assert sending[-1]({**body, 'operation': 'inspect'}) == result
    assert len(sending[2].posts) == 1 and not rows(sending[0], 'outcomes.jsonl')


@pytest.mark.parametrize('send', [True, False])
def test_original_matcher_first_and_delayed_save_do_not_duplicate_or_regress(sending, monkeypatch, send):
    from datetime import datetime, timedelta
    _, preview, proposal = typed(sending, send=send)
    body = request({'operation': 'send' if send else 'save', 'review': preview['review'], 'proposal': proposal})
    import log_outcome
    original = log_outcome.log
    monkeypatch.setattr(log_outcome, 'log', lambda rec: (_ for _ in ()).throw(OSError('delay learning')))
    result = sending[-1](body)
    assert result['learning'] == 'pending'
    monkeypatch.setattr(log_outcome, 'log', original)
    row = rows(sending[0], 'events/drafts.jsonl')[0]
    sent_at = datetime.fromisoformat(row['ts'].replace('Z', '+00:00')) + timedelta(seconds=1)
    signal = {'verdict_class': 'signal', 'event': {'source': 'email', 'id': 'sent-123',
              'is_from_me': True, 'to': 'alice@example.com', 'text': row['text'], 'timestamp': sent_at.isoformat()}}
    (sending[0] / 'events/queue.jsonl').write_text(json.dumps(signal) + '\n')
    import draft_outcomes
    assert draft_outcomes.run(now=sent_at + timedelta(minutes=1))['executed'] == 1
    observed = rows(sending[0], 'outcomes.jsonl')
    assert len(observed) == 1 and observed[0]['tier'] == 'review'
    repaired = sending[-1]({**body, 'operation': 'inspect'})
    assert repaired['learning'] == 'recorded'
    assert rows(sending[0], 'outcomes.jsonl') == observed
    assert draft_outcomes.run(now=sent_at + timedelta(minutes=2))['graded'] == 0


def test_offered_identifier_uses_original_normalization_without_case_rewrite(sending):
    sending[2].recipient = 'Alice <Alice@Example.com>'
    _, preview, proposal = typed(sending)
    body = request({'operation': 'send', 'review': preview['review'], 'proposal': proposal})
    assert sending[-1](body)['learning'] == 'recorded'
    assert rows(sending[0], 'events/drafts.jsonl')[0]['identifier'] == 'Alice@Example.com'


def test_original_extract_to_attended_confirmation(sending, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-host')
    import style_extract
    data = sending[0]
    text = 'Thank you for inviting me. I cannot attend this gathering.'
    result = style_extract.extract({'sent_messages': [{'channel': 'email', 'recipient': 'alice@example.com',
        'text': text, 'date': '2026-10-05', 'canonical_id': 'c_abcdef'}]}, now=datetime.now(timezone.utc))
    assert result['messages_analyzed'] == 1
    style = json.loads((data / 'style.json').read_text())
    assert style['canonical']['work_email'][0]['source'] == 'extracted'
    _, preview, proposal = typed(sending, text=text)
    body = request({'operation': 'send', 'review': preview['review'], 'proposal': proposal})
    assert sending[-1](body)['learning'] == 'recorded'
    after = json.loads((data / 'style.json').read_text())
    assert len(after['confirmed']) == 1 and after['confirmed'][0]['text'] == text
    assert after['confirmed'][0]['source'] == 'confirmed'
    assert sending[-1]({**body, 'operation': 'inspect'})['learning'] == 'recorded'
    assert json.loads((data / 'style.json').read_text()) == after


def pair(sending, first=None):
    args = {'operation':'propose_pair','message_id':'message-123',
            'accept_body':'Yes, I can join.','decline_body':'I cannot attend.'}
    if first is not None:
        args['first'] = first
    body = request(args,'preview',event='f'*64,text='Draft both choices.')
    preview = sending[-1](body)
    assert preview['status']=='preview'
    return body,preview


def accepted_pair(body,preview):
    import hashlib
    return {**body,'operation':'record_offer','accepted_delivery':{
        'accepted_dispatch_id':'accepted-pair','provider_message_id':'pair-provider-message',
        'source_revision':'c'*64,'review_hash':preview['review']['review_hash'],
        'payload_hash':hashlib.sha256(tool._pair_text(preview['review']).encode()).hexdigest()}}


def test_pair_no_offered_until_actual_attestation_read_only_grant_and_retention(sending):
    sending[3]['gmail_draft']=False
    sending[3]['gmail_send']=False
    body,preview=pair(sending,'decline')
    assert tool._pair_text(preview['review']).index('Decline:') < tool._pair_text(preview['review']).index('Accept:')
    assert rows(sending[0],'events/drafts.jsonl')==[] and not sending[2].posts
    assert sending[-1]({**body,'operation':'inspect'})==preview
    result=sending[-1](accepted_pair(body,preview))
    assert result['offer_receipt']['reply_key']==preview['review']['reply_key']
    offered=rows(sending[0],'events/drafts.jsonl')
    from keys import draft_key
    assert {draft_key(row) for row in offered}=={preview['review']['reply_key'],preview['review']['decline_key']}
    assert {row['action_type'] for row in offered}=={'reply','decline'}
    assert len(offered)==2 and not sending[2].posts
    (sending[0]/'events/drafts.jsonl').unlink()
    sending[2].get=lambda **kwargs:pytest.fail('saved pair must not refetch')
    assert sending[-1]({**body,'operation':'inspect'})==result
    assert sending[-1](accepted_pair(body,preview))==result
    assert not (sending[0]/'events/drafts.jsonl').exists()


def test_pair_partial_local_append_recovery_and_bound_selection(sending,monkeypatch):
    body,preview=pair(sending)
    import action_links
    original=action_links.record_draft
    fail=[True]
    def append(*args,**kwargs):
        result=original(*args,**kwargs)
        if args[3]=='decline' and fail[0]:
            fail[0]=False
            raise OSError('lost local append response')
        return result
    monkeypatch.setattr(action_links,'record_draft',append)
    assert sending[-1](accepted_pair(body,preview))['status']=='unknown'
    assert len(rows(sending[0],'events/drafts.jsonl'))==2
    recorded=sending[-1]({**body,'operation':'inspect'})
    assert 'offer_receipt' in recorded and len(rows(sending[0],'events/drafts.jsonl'))==2
    baseline={k:body[k] for k in ('event_id','action_id')}
    baseline.update(args_hash=tool.digest(body['args']),accepted_dispatch_id='accepted-pair',source_revision='c'*64)
    _,edited,proposal=typed(sending,baseline=baseline,event='e'*64,text='A completely different explicit decline.')
    origin=edited['review']['draft_origin']
    assert origin['baseline_action_id']==body['action_id']
    assert origin['baseline_key']==preview['review']['decline_key'] and origin['baseline_body']=='I cannot attend.'
    result=sending[-1](request({'operation':'send','review':edited['review'],'proposal':proposal}))
    assert result['status']=='committed' and result['learning']=='recorded'
    assert len(sending[2].posts)==1 and len(rows(sending[0],'events/drafts.jsonl'))==2
    assert rows(sending[0],'outcomes.jsonl')[-1]['outcome']=='edited_and_sent'


@pytest.mark.parametrize('change',['dispatch','payload','revision','review'])
def test_pair_attestation_refusal_and_source_withholding(sending,change):
    body,preview=pair(sending)
    accepted=accepted_pair(body,preview)
    key={'dispatch':'accepted_dispatch_id','payload':'payload_hash','revision':'source_revision','review':'review_hash'}[change]
    accepted['accepted_delivery'][key]='' if change=='dispatch' else 'a'*64
    assert sending[-1](accepted)['status']=='rejected'
    assert rows(sending[0],'events/drafts.jsonl')==[]
    sending[1]['_source_authority']=lambda:{'complete':False}
    assert sending[-1]({**body,'operation':'inspect'})['status']=='rejected'
    assert not sending[2].posts


def test_pair_unrecorded_lineage_and_direct_yes_cannot_select(sending):
    body,preview=pair(sending)
    baseline={k:body[k] for k in ('event_id','action_id')}
    baseline.update(args_hash=tool.digest(body['args']),accepted_dispatch_id='unaccepted',source_revision='c'*64)
    args={'operation':'propose_typed','message_id':'message-123','body':'I cannot attend.','action_type':'decline','baseline':baseline}
    assert sending[-1](request(args,'preview',event='e'*64))['status']=='rejected'
    with pytest.raises(ValueError):
        sending[-1](request({'operation':'save','review':preview['review'],'proposal':baseline}))
    assert rows(sending[0],'events/drafts.jsonl')==[] and not sending[2].posts


def test_original_matcher_can_grade_unselected_offered_accept(sending):
    from datetime import datetime, timedelta
    body,preview=pair(sending)
    assert 'offer_receipt' in sending[-1](accepted_pair(body,preview))
    offered=rows(sending[0],'events/drafts.jsonl')
    accept=next(row for row in offered if row['action_type']=='reply')
    sent_at=datetime.fromisoformat(accept['ts'].replace('Z','+00:00'))+timedelta(seconds=1)
    signal={'verdict_class':'signal','event':{'source':'email','id':'external-accept',
        'is_from_me':True,'to':'alice@example.com','text':accept['text'],'timestamp':sent_at.isoformat()}}
    (sending[0]/'events/queue.jsonl').write_text(json.dumps(signal)+'\n')
    import draft_outcomes
    assert draft_outcomes.run(now=sent_at+timedelta(minutes=1))['executed']==1
    learned=rows(sending[0],'outcomes.jsonl')
    assert len(learned)==1 and learned[0]['action_type']=='reply'
    assert learned[0]['action_id']==preview['review']['reply_key']
    assert learned[0]['source_event_id']=='email:external-accept'
    assert not sending[2].posts


def test_pair_conflicting_valid_attestation_never_changes_original_receipt(sending):
    body,preview=pair(sending)
    accepted=accepted_pair(body,preview)
    result=sending[-1](accepted)
    changed={**accepted,'accepted_delivery':{**accepted['accepted_delivery'],'provider_message_id':'other-provider-message'}}
    assert sending[-1](changed)=={'version':1,'status':'rejected','code':'event_conflict'}
    assert sending[-1]({**body,'operation':'inspect'})==result
    assert len(rows(sending[0],'events/drafts.jsonl'))==2 and not sending[2].posts
