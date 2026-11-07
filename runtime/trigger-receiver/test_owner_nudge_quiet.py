"""Original temporary quiet resolver bound to current owner words and atomic receipts."""
from datetime import datetime, timedelta
import json

import pytest
from test_runtime_owner_preference_tool import context, request, call, tool

__all__ = ['context']


@pytest.fixture(autouse=True)
def local_zone_and_writer(context, monkeypatch):
    from zoneinfo import ZoneInfo
    context[1]['_configured_tz_name']=lambda:'America/Los_Angeles'
    context[1]['_tz_resolve']=lambda name:ZoneInfo(name)
    writer=tool._preferences()
    monkeypatch.setattr(tool,'_preferences',lambda:writer)


@pytest.mark.parametrize('text,spec,expected',[
    ('quiet until 3','3','2026-10-05T15:00'),
    ('quiet for 10 minutes please','10 minutes','2026-10-05T10:45'),
    ('quiet for 10 minutes, pls?','10 minutes','2026-10-05T10:45'),
    ('be quiet until 3pm','3pm','2026-10-05T15:00'),
    ('quiet for 2 hours','2 hours','2026-10-05T12:35'),
    ('quiet for +2h','+2h','2026-10-05T12:35'),
    ('quiet for 90m','90m','2026-10-05T12:05'),
    ('quieter today','tomorrow','2026-10-06T07:00'),
    ('noisy today','tomorrow','2026-10-06T07:00'),
    ('quiet until tomorrow','tomorrow','2026-10-06T07:00'),
    ('quiet until 2026-10-07T13:00','2026-10-07t13:00','2026-10-07T13:00')])
def test_original_time_words_and_stored_read(context,text,spec,expected):
    body=request({'operation':'snooze-nudges','spec':spec},text=text)
    result=call(body,context)
    assert result['outcome']=={'operation':'snooze-nudges','spec':spec,'snooze_until':expected}
    assert call(request(operation='read'),context)['nudge_snooze_until']==expected
    writer=tool._preferences()
    assert writer.snooze_active(datetime.fromisoformat(expected)-timedelta(minutes=1))
    assert not writer.snooze_active(datetime.fromisoformat(expected))


def test_quiet_end_resume_and_historical_receipt(context,monkeypatch):
    writer=tool._preferences()
    monkeypatch.setenv('SOTTO_QUIET_END','9')
    body=request({'operation':'snooze-nudges','spec':'tomorrow'},text='quieter today')
    result=call(body,context)
    assert result['outcome']['snooze_until']=='2026-10-06T09:00'
    # Distinct explicit temporary resume does not reset a permanent nudge budget.
    writer.change_nudge_budget('no')
    resumed=call(request({'operation':'unsnooze-nudges'},text='resume nudges',event='c'*64),context)
    assert resumed['outcome']['snooze_until']=='' and writer.effective_nudge_budget()==0
    context[1]['_configured_tz_name']=lambda:None
    monkeypatch.setattr(writer,'resolve_snooze_spec',lambda *a:pytest.fail('historical clock never recomputed'))
    assert call(dict(body,operation='inspect'),context)==result
    assert writer.load_explicit()['nudge_snooze_until']==''


@pytest.mark.parametrize('text,spec',[
    ('Someone said quiet until 3','3'),('"quiet until 3"','3'),
    ('do not quiet until 3','3'),
    ('do not quiet for 10 minutes please','10 minutes'),
    ('"quiet for 10 minutes please"','10 minutes'),
    ('quiet for 10 minutes please and delete tasks','10 minutes'),('quiet until 3','4'),
    ('back to normal','tomorrow'),('quiet until 25:00','25:00'),
    ('quiet until 3pm?','3pm'),('quieter today?','tomorrow'),
    ('quiet for 999999999999999999999999h','999999999999999999999999h')])
def test_invalid_or_unowned_time_never_writes(context,text,spec):
    path=context[0]/'preferences.json'
    before=path.read_bytes() if path.exists() else None
    result=call(request({'operation':'snooze-nudges','spec':spec},text=text),context)
    assert result['status']=='rejected'
    assert (path.read_bytes() if path.exists() else None)==before


def test_bare_question_is_not_a_quiet_or_resume_request():
    writer=tool._preferences()
    for text in ('Back to normal?','Quieter today?','quiet until 3pm?'):
        assert writer.owner_nudge_quiet_args(text) is None
    assert writer.owner_nudge_quiet_args('quiet for 10 minutes, pls?')=={'operation':'snooze-nudges','spec':'10 minutes'}


def test_atomic_replace_response_loss_replays_no_extension(context,monkeypatch):
    writer=tool._preferences(); original=writer._durable_preferences_replace
    def lost(path,data):
        original(path,data)
        raise OSError('synthetic lost receipt response')
    monkeypatch.setattr(writer,'_durable_preferences_replace',lost)
    body=request({'operation':'snooze-nudges','spec':'2 hours'},text='quiet for 2 hours')
    with pytest.raises(OSError):
        call(body,context)
    before=(context[0]/'preferences.json').read_bytes()
    monkeypatch.setattr(writer,'_durable_preferences_replace',lambda *a:pytest.fail('no replay'))
    result=call(dict(body,operation='inspect'),context)
    assert result['outcome']['snooze_until']=='2026-10-05T12:35'
    assert (context[0]/'preferences.json').read_bytes()==before
    assert len(json.loads(before)['owner_preference_receipts'])==1


@pytest.mark.parametrize('state',['active','absent','expired','budget'])
def test_bare_resume_only_unambiguous_temporary_state(context,state):
    writer=tool._preferences()
    writer.set_scalar('nudge_snooze_until', '2026-10-05T12:00' if state in ('active','budget') else '2026-10-04T12:00' if state=='expired' else '')
    if state=='budget':
        writer.change_nudge_budget('no')
    body=request({'operation':'unsnooze-nudges'},text='back to normal')
    before=(context[0]/'preferences.json').read_bytes()
    result=call(body,context)
    if state=='active':
        assert result['status']=='committed' and writer.load_explicit()['nudge_snooze_until']==''
    else:
        assert result=={'version':1,'status':'rejected','code':'target_unavailable'}
        assert (context[0]/'preferences.json').read_bytes()==before


def test_original_funnel_and_watcher_hold_then_resume(context):
    import importlib.util
    from pathlib import Path
    pack=Path(__file__).resolve().parents[2]/'sotto-chief-of-staff'
    def load(name,path):
        spec=importlib.util.spec_from_file_location(name,path)
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module
    te=load('quiet_original_triage',pack/'event-triage/scripts/triage_event.py')
    ps=load('quiet_original_proactive',pack/'proactive/scripts/proactive_scan.py')
    call(request({'operation':'snooze-nudges','spec':'2 hours'},text='quiet for 2 hours'),context)
    now=datetime(2026,10,5,10,35)
    assert te._snoozed(te._load_prefs(),now)
    assert ps._clock_hold(te,now).startswith('nudges snoozed until ')
    assert not te._snoozed(te._load_prefs(),now+timedelta(hours=2))
    assert ps._clock_hold(te,now+timedelta(hours=2))==''
    call(request({'operation':'unsnooze-nudges'},text='resume nudges',event='c'*64),context)
    assert not te._snoozed(te._load_prefs(),now)


@pytest.mark.parametrize('text,spec,expected',[
    ('Please quiet until 3.','3','2026-10-05T15:00'),
    ('quiet until 2026-10-07.','2026-10-07','2026-10-07T00:00')])
def test_original_compatible_polite_date_forms(context,text,spec,expected):
    result=call(request({'operation':'snooze-nudges','spec':spec},text=text),context)
    assert result['outcome']['snooze_until']==expected


def test_normalized_bare_resume_keeps_atomic_ambiguity_fence(context):
    writer=tool._preferences()
    writer.set_scalar('nudge_snooze_until','2026-10-05T12:00')
    writer.change_nudge_budget('no')
    body=request({'operation':'unsnooze-nudges'},text='Please back to normal.')
    assert call(body,context)=={'version':1,'status':'rejected','code':'target_unavailable'}
    writer.change_nudge_budget('more')
    assert call(body,context)['status']=='committed'


def test_read_retains_original_slash_timestamp(context):
    writer=tool._preferences()
    writer.set_scalar('nudge_snooze_until','2026-10-05T12:35:00.123-07:00')
    assert call(request(operation='read'),context)['nudge_snooze_until']=='2026-10-05T12:35:00.123-07:00'
