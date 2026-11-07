"""Bounded Google calendar reads and selection in the existing settings file."""
import json
import os
from pathlib import Path
from datetime import datetime, timezone

MAX_CALENDARS = 50
MAX_PAGES = 20


class CalendarSelectionExpiredError(ValueError):
    pass


class CalendarPaginationError(RuntimeError):
    pass


def validate_selection(value):
    if (type(value) is not dict or set(value) != {'mode', 'calendar_ids'}
            or value['mode'] not in ('all', 'selected') or type(value['calendar_ids']) is not list
            or len(value['calendar_ids']) > MAX_CALENDARS
            or any(type(x) is not str or not x or len(x) > 1024
                   or any(c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in x)
                   for x in value['calendar_ids'])
            or len(set(value['calendar_ids'])) != len(value['calendar_ids'])
            or value['mode'] == 'all' and value['calendar_ids']):
        raise ValueError('Calendar selection is invalid')
    return {'mode': value['mode'], 'calendar_ids': sorted(value['calendar_ids'])}


def selection(data_root=None, *, generation=None):
    root = data_root or os.environ.get('SOTTO_DATA')
    if not root:
        return {'mode': 'all', 'calendar_ids': []}
    path = Path(root) / 'config/settings.json'
    try:
        if path.stat().st_size > 65536:
            raise ValueError('Settings exceed bound')
        settings = json.loads(path.read_text())
    except FileNotFoundError:
        settings = {}
    if type(settings) is not dict:
        raise ValueError('Settings are invalid')
    # Product default explicitly requested by the owner; no calendar is silently selected
    # from a model tool argument. Narrowing stays in the established settings store.
    value = settings.get('google_calendar_selection', {'mode': 'all', 'calendar_ids': []})
    if isinstance(value, dict) and 'generation' in value:
        if generation is None:
            import gmail_read
            path = gmail_read.token_path()
            if path and Path(path).stat().st_size > 65536:
                raise ValueError('Google consent metadata exceeds bound')
            token = json.loads(Path(path).read_text()) if path else {}
            generation = token.get('sotto_source_generation')
        if not generation or value['generation'] != generation:
            raise CalendarSelectionExpiredError('Calendar selection requires current Google consent')
        value = {key: val for key, val in value.items() if key != 'generation'}
    return validate_selection(value)


def authority_selection(data_root, *, generation):
    """Expired calendar selection withholds only calendar, not independent source grants."""
    try:
        return selection(data_root, generation=generation)
    except CalendarSelectionExpiredError:
        return {'mode': 'selected', 'calendar_ids': [], 'needs_reselection': True}


class CalendarRows(list):
    def __init__(self):
        super().__init__()
        self.complete = True
        self.calendars = []


def read(fetch_inventory, fetch_events, *, selected=None, max_items=5000):
    """Callbacks preserve each transport's deadline and credentials; no retries here."""
    selected = validate_selection(selected) if selected is not None else selection()
    rows = CalendarRows()
    primary_calendars = {'primary'}
    ids = selected['calendar_ids']
    if selected['mode'] == 'all':
        ids, tokens, page = [], set(), None
        try:
            for _ in range(MAX_PAGES):
                result = fetch_inventory(page)
                items = result.get('items', [])
                if type(items) is not list:
                    raise ValueError('Calendar inventory is invalid')
                for item in items:
                    if not isinstance(item, dict):
                        raise ValueError('Calendar inventory is invalid')
                    if item.get('deleted') or item.get('hidden') or item.get('accessRole') not in ('reader', 'writer', 'writerWithoutPrivateAccess', 'owner'):
                        continue
                    calendar_id = item.get('id')
                    if item.get('primary') is True:
                        primary_calendars.add(calendar_id)
                    validate_selection({'mode': 'selected', 'calendar_ids': [calendar_id]})
                    if calendar_id not in ids:
                        if len(ids) == MAX_CALENDARS:
                            rows.complete = False
                            continue
                        ids.append(calendar_id)
                page = result.get('nextPageToken')
                if not page:
                    break
                if not isinstance(page, str) or len(page) > 2048 or page in tokens:
                    raise CalendarPaginationError('Calendar pagination repeated a page token or is invalid')
                tokens.add(page)
            else:
                rows.complete = False
        except (ValueError, CalendarPaginationError):
            raise
        except Exception:  # provider failure is explicit partial coverage, never empty success
            rows.complete = False
            rows.calendars.append({'calendar_id': '*', 'status': 'unavailable'})
            # Existing event-only grants can read primary without calendar discovery.
            # Keep that useful read, while explicitly withholding complete coverage.
            # Explicit owner selections never enter this branch.
            if not ids:
                ids = ['primary']
    seen = set()
    for calendar_id in ids:
        status, tokens, page = 'ok', set(), None
        count = 0
        try:
            for _ in range(MAX_PAGES):
                if count >= max_items or len(rows) >= 5000:
                    status = 'partial'
                    break
                result = fetch_events(calendar_id, page)
                items = result.get('items', [])
                if type(items) is not list or len(items) > 250 or any(type(item) is not dict for item in items):
                    raise ValueError('Calendar events exceed bound')
                for item in items:
                    if item.get('status') == 'cancelled':
                        continue
                    key = (calendar_id, item.get('id'), json.dumps(item.get('originalStartTime'), sort_keys=True))
                    if not isinstance(item.get('id'), str) or not item['id']:
                        raise ValueError('Calendar event identity unavailable')
                    if key in seen:
                        continue
                    if count >= max_items or len(rows) >= 5000:
                        status = 'partial'
                        break
                    seen.add(key)
                    rows.append({**item, 'calendar_id': calendar_id, '_calendar_primary': calendar_id in primary_calendars})
                    count += 1
                page = result.get('nextPageToken')
                if not page or status == 'partial':
                    break
                if not isinstance(page, str) or len(page) > 2048 or page in tokens:
                    raise CalendarPaginationError('Calendar pagination repeated a page token or is invalid')
                tokens.add(page)
            else:
                status = 'partial'
        except (ValueError, CalendarPaginationError):
            raise
        except Exception:
            status = 'unavailable'
        rows.calendars.append({'calendar_id': calendar_id, 'status': status})
        rows.complete = rows.complete and status == 'ok'
    def order(item):
        raw = (item.get('start') or {}).get('dateTime') or (item.get('start') or {}).get('date') or ''
        try:
            parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            stamp = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        except (ValueError, TypeError):
            stamp = float('inf')
        return (stamp, str(item.get('calendar_id')), str(item.get('id')))
    rows.sort(key=order)
    return rows


def read_service(service, start, end, *, selected=None):
    def inventory(page):
        params = {'maxResults': 250, 'minAccessRole': 'reader', 'showHidden': False}
        if page:
            params['pageToken'] = page
        return service.calendarList().list(**params).execute()

    def events(calendar_id, page):
        params = {'calendarId': calendar_id, 'timeMin': start, 'timeMax': end,
                  'singleEvents': True, 'orderBy': 'startTime', 'maxResults': 250}
        if page:
            params['pageToken'] = page
        return service.events().list(**params).execute()
    return read(inventory, events, selected=selected)
