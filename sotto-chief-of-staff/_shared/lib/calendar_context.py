"""Calendar participation rules shared by notification producers."""

from datetime import datetime
import re
import os


SCHEDULE_DAY = (r'(?:Today|Tomorrow|(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|'
        r'Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)(?:,?\s+[A-Za-z]{3,9}\s+\d{1,2})?'
        r'|[A-Za-z]{3,9}\s+\d{1,2})')


def _self_describes_owner(event):
    # Google `self` is relative to the queried calendar, not the authenticated account.
    # Legacy rows were primary-only; new scoped rows require primary discovery proof.
    return event.get('calendar_id') in (None, '', 'primary') or event.get('_calendar_primary') is True


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
        if not include_self and ((row.get('self') and _self_describes_owner(event)) or (owner and email == owner)):
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
        if (person.get('self') and _self_describes_owner(event)) or (owner and person['email'] == owner):
            return person['status'] != 'declined'
    if (event.get('_calendar_primary') is True or event.get('calendar_id') == 'primary'
            or (owner and event.get('calendar_id') == owner)):
        return True  # a personal primary slot, including a private/solo block
    organizer = event.get('organizer') or {}
    return isinstance(organizer, dict) and bool((organizer.get('self') and _self_describes_owner(event)) or
        (owner and str(organizer.get('email') or '').strip().lower() == owner))


def active_participation(event, self_email=''):
    """An active event belonging to the owner can receive attendance follow-through.

    Unscoped legacy rows came from the primary calendar; scoped shared rows require evidence
    of participation. Availability is a separate question handled by blocks_time.
    """
    if (event.get('_active_participation') is False or event.get('_owner_participates') is False
            or str(event.get('status') or '').lower() in ('cancelled', 'canceled')
            or str(event.get('my_response') or '').lower() == 'declined'
            or event.get('eventType') == 'workingLocation'):
        return False
    owner = (self_email or os.environ.get('SOTTO_USER_EMAIL', '')).strip().lower()
    if event.get('busy') is False or str(event.get('transparency') or '').lower() == 'transparent':
        # Free calendar entries often carry FYIs or holds. Only an explicit accepted/tentative
        # invitation proves attendance; organizing a free entry does not.
        if event.get('_active_participation') is True:
            return True  # normalized cache retained the raw attendance decision
        organizer = event.get('organizer') or {}
        if isinstance(organizer, dict) and ((organizer.get('self') and _self_describes_owner(event))
                or (owner and str(organizer.get('email') or '').strip().lower() == owner)):
            return False
        return any(person['status'] in ('accepted', 'tentative') and not person.get('organizer')
                   and ((person.get('self') and _self_describes_owner(event))
                        or (owner and person['email'] == owner))
                   for person in human_attendees(event, owner, include_self=True))
    if event.get('calendar_id'):
        return user_participates(event, owner)
    return not any(person['status'] == 'declined' and
                   (person.get('self') or owner and person['email'] == owner)
                   for person in human_attendees(event, owner, include_self=True))


def blocks_time(event, self_email=''):
    """Availability excludes free events; follow-through requires attendance evidence."""
    return (event.get('busy') is not False
            and str(event.get('transparency') or '').lower() != 'transparent'
            and active_participation(event, self_email))


def work_attendees(event, self_email=''):
    """Unsolicited research/prep is for external work addresses, never personal mailboxes.

    This is eligibility for automatic prep, not an identity claim or a restriction on a user's
    explicit prep request. A consumer mailbox belonging to the owner is not a company domain.
    """
    if event.get('calendar_id') and not user_participates(event, self_email):
        return []
    # Only skill-side callers need this policy. The receiver also copies this module alone.
    from textutil import FREEMAIL_DOMAINS
    owner_domain = self_email.strip().lower().partition('@')[2]
    if owner_domain in FREEMAIL_DOMAINS:
        owner_domain = ''
    return [a for a in human_attendees(event, self_email)
            if re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', a['email'])
            and a['status'] != 'declined'
            and a['email'].partition('@')[2] not in FREEMAIL_DOMAINS
            and a['email'].partition('@')[2] != owner_domain]


def prep_meeting_events(events, self_email=''):
    """Explicit prep retains free owner meetings, excluding cancelled or declined participation."""
    owner = self_email.strip().lower()
    return [event for event in meeting_events(events, owner)
            if str(event.get('status') or '').lower() not in ('cancelled', 'canceled')
            and str(event.get('my_response') or '').lower() != 'declined'
            and event.get('_owner_participates') is not False
            and (not event.get('calendar_id') or user_participates(event, owner))
            and not any(person['status'] == 'declined' and
                        ((person.get('self') and _self_describes_owner(event)) or
                         (owner and person['email'] == owner))
                        for person in human_attendees(event, owner, include_self=True))]


def accepted_or_organized(event, self_email=''):
    """Unsolicited private context requires owner acceptance or native organizer proof."""
    owner = self_email.strip().lower()
    organizer = event.get('organizer') or {}
    if isinstance(organizer, dict) and ((organizer.get('self') and _self_describes_owner(event))
            or (owner and str(organizer.get('email') or '').strip().lower() == owner)):
        return True
    return any(person['status'] == 'accepted' and
               ((person.get('self') and _self_describes_owner(event)) or (owner and person['email'] == owner))
               for person in human_attendees(event, owner, include_self=True))


_CONTEXT_TITLE = re.compile(r'^(?:meeting\s+)?(?:context|prep notes|briefing notes|meeting notes)\s*:\s*(.+)$', re.I)


def is_context_event(event):
    """Explicitly labelled prep notes do not assert that another meeting exists."""
    return bool(_CONTEXT_TITLE.match(str(event.get('summary') or event.get('title') or '')))


def _event_time(event, key):
    value = event.get(key)
    return (value.get('dateTime') or value.get('date') or '') if isinstance(value, dict) else value


def _interval(event):
    try:
        times = [datetime.fromisoformat(str(_event_time(event, key)).replace('Z', '+00:00')) for key in ('start', 'end')]
        return tuple(t.timestamp() for t in times) if all(t.tzinfo for t in times) and times[1] > times[0] else None
    except (KeyError, ValueError, TypeError):
        return None


def meeting_events(events, self_email=''):
    """Attach explicitly labelled context to its uniquely matching timed meeting, preserving notes.

    Match the entire context subject or a parenthesized subject (often the company), as a whole
    phrase in the meeting title. Exact start/end and a unique match are required; ambiguous,
    unrelated and separately scheduled prep entries retain their ordinary calendar meaning.
    """
    rows = [dict(e) for e in events if isinstance(e, dict)]
    # One invitation may appear on several readable calendars. Provider UID plus exact timed or date-only
    # occurrence proves a copy; title/name matches never do. Keep all opaque IDs for old receipts.
    copies, separate = {}, []
    owner = (self_email or os.environ.get('SOTTO_USER_EMAIL', '')).strip().lower()
    for row in rows:
        span = _interval(row)
        if span is None and all(re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(_event_time(row, key) or '')) for key in ('start', 'end')):
            try:
                dates = tuple(datetime.fromisoformat(_event_time(row, key)).date() for key in ('start', 'end'))
                span = dates if dates[1] > dates[0] else None
            except ValueError:
                pass
        uid = row.get('iCalUID')
        if not uid or span is None or not row.get('calendar_id'):
            separate.append(row)
            continue
        original = _event_time(row, 'originalStartTime')
        if row.get('recurringEventId') and not original:
            separate.append(row)  # a series UID and current slot cannot identify an instance
            continue
        if original:
            try:
                instant = datetime.fromisoformat(str(original).replace('Z', '+00:00'))
                original = instant.timestamp() if instant.tzinfo else str(original)
            except (ValueError, TypeError):
                separate.append(row)
                continue
        copies.setdefault((uid, span, original or ''), []).append(row)
    for group in copies.values():
        def priority(row):
            own_copy = row.get('_calendar_primary') is True or row.get('calendar_id') == 'primary' or (owner and row.get('calendar_id') == owner)
            declined = str(row.get('my_response') or '').lower() == 'declined' and own_copy
            declined = declined or any(owner and p['email'] == owner and p['status'] == 'declined'
                                      for p in human_attendees(row, owner, include_self=True))
            return (not declined, not own_copy, str(row.get('calendar_id')), str(row.get('id')))
        group.sort(key=priority)
        kept = group[0]
        if len(group) > 1:
            kept['_calendar_copy_ids'] = sorted({str(i) for row in group
                for i in [row.get('id'), *(row.get('_calendar_copy_ids') or [])] if i})
        separate.append(kept)
    rows = separate
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
