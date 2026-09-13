"""Calendar participation rules shared by notification producers."""

from datetime import datetime
import re


def human_attendees(event, self_email='', *, include_self=False):
    """Normalize people, excluding room resources and (normally) the account owner."""
    owner = self_email.strip().lower()
    result = []
    seen = set()
    values = event.get('attendees') or []
    for value in values if isinstance(values, list) else []:
        row = {'email': value} if isinstance(value, str) else value
        if not isinstance(row, dict):
            continue
        email = str(row.get('email') or row.get('address') or '').strip().lower()
        name = str(row.get('displayName') or row.get('name') or '').strip()
        if row.get('resource') or 'resource.calendar.google' in email:
            continue
        if not include_self and (row.get('self') or (owner and email == owner)):
            continue
        key = email or name.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append({**row, 'email': email, 'name': name, 'displayName': name,
                       'status': str(row.get('responseStatus') or '').lower()})
    return result


def user_participates(event, self_email=''):
    """Require explicit participation; merely appearing on a shared calendar proves nothing."""
    if str(event.get('my_response') or '').lower() == 'declined':
        return False
    owner = self_email.strip().lower()
    for person in human_attendees(event, owner, include_self=True):
        if person.get('self') or (owner and person['email'] == owner):
            return person['status'] != 'declined'
    organizer = event.get('organizer') or {}
    return isinstance(organizer, dict) and bool(organizer.get('self') or
        (owner and str(organizer.get('email') or '').strip().lower() == owner))


_CONTEXT_TITLE = re.compile(r'^(?:meeting\s+)?(?:context|prep notes|briefing notes|meeting notes)\s*:\s*(.+)$', re.I)


def _interval(event):
    try:
        times = [datetime.fromisoformat(str(event[key]).replace('Z', '+00:00')) for key in ('start', 'end')]
        return tuple(t.timestamp() for t in times) if all(t.tzinfo for t in times) and times[1] > times[0] else None
    except (KeyError, ValueError, TypeError):
        return None


def meeting_events(events):
    """Attach explicitly labelled context to its uniquely matching timed meeting, preserving notes.

    Match the entire context subject or a parenthesized subject (often the company), as a whole
    phrase in the meeting title. Exact start/end and a unique match are required; ambiguous,
    unrelated and separately scheduled prep entries retain their ordinary calendar meaning.
    """
    rows = [dict(e) for e in events if isinstance(e, dict)]
    titles = [str(e.get('summary') or e.get('title') or '') for e in rows]
    contextual = {i: match.group(1) for i, title in enumerate(titles) if (match := _CONTEXT_TITLE.match(title))}
    attached = set()
    for index, subject in contextual.items():
        span = _interval(rows[index])
        if span is None or rows[index].get('status') == 'cancelled':
            continue
        phrases = [subject, *re.findall(r'\(([^()]+)\)', subject)]
        phrases = [' '.join(re.findall(r'\w+', p.casefold())) for p in phrases]
        matches = []
        for other, title in enumerate(titles):
            if other in contextual or rows[other].get('status') == 'cancelled' or _interval(rows[other]) != span:
                continue
            normalized = ' ' + ' '.join(re.findall(r'\w+', title.casefold())) + ' '
            if any(len(p) >= 3 and (' ' + p + ' ') in normalized for p in phrases):
                matches.append(other)
        if len(matches) != 1:
            continue
        target = rows[matches[0]]
        target['supporting_context'] = [*(target.get('supporting_context') or []), rows[index]]
        detail = str(rows[index].get('description') or '').strip()
        note = titles[index] + (('\n' + detail) if detail else '')
        target['description'] = (str(target.get('description') or '').rstrip() + '\n\n' + note).strip()
        attached.add(index)
    return [row for i, row in enumerate(rows) if i not in attached]
