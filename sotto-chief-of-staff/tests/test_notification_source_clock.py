from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from test_compose_notification import isolated, notification  # noqa: F401

pytestmark = pytest.mark.usefixtures("isolated")


@pytest.mark.parametrize('source', ['imessage', 'whatsapp'])
def test_bridge_wall_time_does_not_turn_today_into_yesterday(monkeypatch, source):
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    now = datetime(2026, 9, 25, 9, tzinfo=ZoneInfo('America/Los_Angeles'))
    monkeypatch.setattr(notification, '_write', lambda *a: [
        {'id': 'x', 'text': 'Sam asked for a call.', 'draft': '', 'decline': ''}])
    bundle = {'events': [{'decision_id': 'x', 'class': 'urgent',
        'event': {'source': source, 'timestamp': '2026-09-25 06:00:00', 'text': 'Call me'}}]}
    assert notification.compose('event', bundle, now=now).startswith('From 6:00 AM today:')


@pytest.mark.parametrize('source', ['calls', 'phonecalls', 'whatsapp_calls'])
@pytest.mark.parametrize('explicit', [False, True])
def test_recent_mac_call_is_not_eight_hours_old(monkeypatch, source, explicit):
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    now = datetime(2026, 11, 17, 15, tzinfo=ZoneInfo('America/Los_Angeles'))
    event = {'source': source, 'timestamp': '2026-11-17 14:50:00'}
    if explicit:
        event['timestamp_utc'] = '2026-11-17T22:50:00Z'
    captured = []
    body = 'Alex called today at 2:50 PM. No message was left.'
    def writer(items, llm):
        captured.extend(notification._writer_item(item) for item in items)
        return [{'id': 'call', 'text': body, 'draft': '', 'decline': ''}]
    monkeypatch.setattr(notification, '_write', writer)
    bundle = {'events': [{'decision_id': 'call', 'class': 'urgent', 'event': event}]}
    text = notification.compose('event', bundle, now=now)
    assert text == body
    assert captured[0]['source_timing']['sent_at'] == '2026-11-17T14:50:00-08:00'


@pytest.mark.parametrize('event, now, prefix', [
    ({'source': 'calls', 'timestamp': '2026-11-17 13:00:00'},
     '2026-11-17T15:00:00-08:00', 'From 1:00 PM today: '),
    ({'source': 'imessage', 'timestamp': '2026-11-17 16:30:00',
      'timestamp_utc': '2026-11-18T00:30:00Z'},
     '2026-11-18T01:00:00-08:00', 'From Nov 17: '),
    ({'source': 'whatsapp', 'timestamp': '2026-11-01 01:05:00',
      'timestamp_utc': '2026-11-01T09:05:00Z'},
     '2026-11-01T02:10:00-08:00', 'From 1:05 AM today: '),
    ({'source': 'calls', 'timestamp': '2026-11-17 14:50:00',
      'timestamp_utc': 'invalid'}, '2026-11-17T15:00:00-08:00', ''),
])
def test_notification_uses_shared_source_clock(monkeypatch, event, now, prefix):
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    captured = []
    def writer(items, llm):
        captured.extend(notification._writer_item(item) for item in items)
        return [{'id': 'event', 'text': 'Alex asked for a call.', 'draft': '', 'decline': ''}]
    monkeypatch.setattr(notification, '_write', writer)
    bundle = {'events': [{'decision_id': 'event', 'class': 'urgent', 'event': event}]}
    local_now = datetime.fromisoformat(now).astimezone(ZoneInfo('America/Los_Angeles'))
    assert notification.compose('event', bundle, now=local_now) == prefix + 'Alex asked for a call.'
    if event.get('timestamp_utc') == 'invalid':
        assert 'source_timing' not in captured[0]
    else:
        assert 'source_timing' in captured[0]
