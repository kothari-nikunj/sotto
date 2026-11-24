import pytest

from relationship_pulse import _interactions_by_contact
from render_local import resolve_contact_names

PHONE = '+14155550109'
MISSED = {'phone': PHONE, 'timestamp_utc': '2026-06-24T07:00:00Z',
          'is_outgoing': False, 'is_answered': False, 'call_type': 'phone', 'record_id': '1'}


def _readback(*calls):
    return resolve_contact_names({'contacts': [{'name': 'Dana', 'phones': [PHONE]}],
                                  'calls': list(calls)})


@pytest.mark.parametrize('duration', [{}, {'duration_seconds': 0}])
def test_outgoing_attempt_does_not_hide_earlier_missed_call(duration):
    attempt = {**MISSED, **duration, 'timestamp_utc': '2026-06-24T08:00:00Z',
               'is_outgoing': True, 'record_id': '2'}
    result = _readback(MISSED, attempt)
    assert [call['record_id'] for call in result['missed_calls']] == ['1']
    assert result['recent_calls'] == []


def test_answered_incoming_call_without_duration_counts_in_relationship_history():
    incoming = {**MISSED, 'timestamp': '2026-06-24 07:00:00', 'is_answered': True}
    result = _readback(incoming)
    assert result['recent_calls'][0]['direction'] == 'incoming'
    assert _interactions_by_contact(result)['Dana']['engaged_from_them']
