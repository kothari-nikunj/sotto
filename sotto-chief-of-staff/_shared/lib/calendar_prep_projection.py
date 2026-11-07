"""Pure day-cache rows and meeting-prep notification text; no reads or writes."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import delivery_effects  # noqa: E402


def calendar_text(value):
    return value.strip() if isinstance(value, str) else ""


def day_row(event, attendees, all_day):
    return {'summary': calendar_text(event.get('summary')), 'start': calendar_text(event.get('start')),
            'end': calendar_text(event.get('end')), 'attendees': attendees, 'all_day': all_day,
            'prep_location': (delivery_effects.prep_location(event.get('location'))
                              if event.get('_active_participation') is True else ''),
            **({'busy': False} if event.get('busy') is False else {}),
            **({'id': event['_id']} if event.get('_id') else {})}


def meeting_notice(item, now, prep_context=''):
    if item.get('location_update'):
        update = item['location_update']
        return f"{item.get('title') or 'Your meeting'} location changed to {update['location']}."
    start = delivery_effects.instant(item.get('calendar_start'))
    if start is None or start <= now.timestamp():
        return ''
    person = item.get('person')
    who = f" ({item['who']})" if item.get('who') else ''
    minutes = max(1, int((start - now.timestamp()) / 60))
    text = (f"You're meeting {person}{who} in about {minutes} minutes." if person else
            f"{item.get('title') or 'Your meeting'} starts in about {minutes} minutes.")
    if prep_context:
        text += ' ' + prep_context
    return text + (f' Want the full prep on {person}?' if person else ' Want the full prep?')
