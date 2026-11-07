"""Pure closed meeting-prep selection schema shared by admission and the worker gate."""
from datetime import datetime
from runtime_calendar_id import validate_calendar_id


def validate_selection(selection):
    if type(selection) is not dict:
        raise ValueError('skill selection invalid')
    if selection.get('mode') == 'sweep' and set(selection) == {'mode'}:
        return
    focus = selection.get('focus')
    if (set(selection) not in ({'mode', 'focus'}, {'mode', 'focus', 'meeting'}) or selection.get('mode') != 'focus'
            or type(focus) is not str or not focus or focus.strip() != focus or len(focus) > 200
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in focus)):
        raise ValueError('skill focus invalid')
    if 'meeting' in selection:
        meeting = selection['meeting']
        if (type(meeting) is not dict or set(meeting) not in ({'event_id', 'start'}, {'event_id', 'start', 'calendar_id'})
                or type(meeting['event_id']) is not str or not 1 <= len(meeting['event_id']) <= 512
                or meeting['event_id'].strip() != meeting['event_id']
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in meeting['event_id'])
                or type(meeting['start']) is not str):
            raise ValueError('skill meeting occurrence invalid')
        try:
            if datetime.fromisoformat(meeting['start'].replace('Z', '+00:00')).tzinfo is None:
                raise ValueError('missing offset')
        except ValueError as error:
            raise ValueError('skill meeting occurrence invalid') from error

        if 'calendar_id' in meeting:
            validate_calendar_id(meeting['calendar_id'])
