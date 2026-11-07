"""Actual brief composition in the existing isolated Calendar-only prep world."""
from datetime import datetime, timedelta, timezone
import json
import os
import sys


def compose_offer(world, pack, data, expected_event, owner_text=None):
    if not world.get('meetingPrep') or world.get('savedSourceGrants') != ['calendar']:
        raise ValueError('Brief prep acceptance requires the isolated Calendar-only world')
    sys.path[:0] = [str(pack / '_shared' / folder) for folder in ('scripts', 'lib', 'knowledge')]
    import compose_brief  # noqa: PLC0415 - explicit selected pack
    from calendar_context import meeting_events  # noqa: PLC0415 - explicit selected pack
    from source_context import source_result  # noqa: PLC0415 - the real gather receipt
    now = datetime.now(timezone.utc)
    events = [{**row, 'start': row['start']['dateTime'], 'end': row['end']['dateTime']}
              for row in world['calendarEvents']]
    inputs = {'type': 'morning', 'first_run': False, 'now': now.isoformat(), 'window_hours': 24,
              'source_results': {'calendar': source_result('ok', complete=True,
                  since=now.isoformat(), until=(now + timedelta(hours=72)).isoformat())},
              'google': {'userEmail': 'owner@inside.test', 'userTimezone': '+00:00',
                         'events': events, 'emails': []},
              'local': {'contacts': [], 'imessage': [], 'whatsapp': [], 'reminders': []},
              'granola': {'meetings': []}}
    # No supplied draft, helpKind, selected person, or expected event reaches the model.
    if owner_text:
        from runtime_owner_brief_request import parse_owner_brief_request  # noqa: PLC0415 - isolated receiver imports
        request = parse_owner_brief_request(owner_text)
        if not request or not request.get('composition_direction'):
            raise ValueError('Focused brief fixture requires an admitted composition direction')
        inputs['composition_direction'] = request['composition_direction']
    artifact = compose_brief.compose(inputs, critic=True)
    offer = artifact.get('_prep_offer')
    if (not isinstance(offer, dict) or offer.get('ambiguous') is not False
            or offer.get('meeting', {}).get('event_id') != expected_event
            or not offer.get('question') or artifact['brief_text'].count(offer['question']) != 1):
        rejected = data.parent / 'brief-prep-rejected.json'
        rejected.write_text(json.dumps(artifact))
        os.chmod(rejected, 0o600)
        raise ValueError('Actual brief failed to select and render the expected single prep offer')
    artifact['_source_permissions'] = ['calendar']
    artifact['_calendar_eligibility'] = [
        {'kind': 'eligibility', 'source': 'calendar', 'calendar_event_id': event['id'],
         'calendar_start': event['start'], 'calendar_observed_at': now.isoformat()}
        for event in meeting_events(events, 'owner@inside.test') if event.get('id') and event.get('start')]
    path = data.parent / 'brief-prep-artifact.json'
    path.write_text(json.dumps(artifact, indent=2))
    os.chmod(path, 0o600)
    return artifact
