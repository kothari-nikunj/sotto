"""Bridge source IDs and payload projections shared by readers and consent gates."""
from datetime import datetime
import re

# Source IDs and their payload projections; source consent also governs derived fields.
SOURCE_FIELDS = {
    'imessage': ('imessage', 'deferred_unread_imessage'),
    'whatsapp': ('whatsapp', 'deferred_unread_whatsapp'),
    'calls': ('calls',), 'whatsapp_calls': ('whatsapp_calls',),
    'contacts': ('contacts', 'contacts_total'), 'reminders': ('reminders',),
    'apple_notes': ('apple_notes',), 'chrome': ('chrome_history', 'search_queries'),
    'safari': ('safari_history', 'safari_search_queries'), 'recent_files': ('recent_files',),
    'screen_time': ('screen_time',),
}

LOCAL_SNAPSHOT_TTL_HOURS = 24

SOURCE_LABELS = {
    'imessage': 'iMessage', 'whatsapp': 'WhatsApp', 'calls': 'Phone Calls',
    'whatsapp_calls': 'WhatsApp Calls', 'contacts': 'Apple Contacts',
    'reminders': 'Apple Reminders', 'apple_notes': 'Apple Notes',
    'chrome': 'Chrome History', 'safari': 'Safari History',
    'recent_files': 'Recent Files', 'screen_time': 'Screen Time',
    'gmail': 'Gmail', 'calendar': 'Google Calendar',
    'granola': 'Meeting Notes (Granola)',
    'attendee_research': 'Attendee Research (web search)',
    'x': 'X (attendee context)',
}


# Every Bridge read failure category, with what the owner or a client can do about it.
FAILURE_HINTS = {
    'permission_denied': 'macOS blocked the read: grant Sotto Bridge Full Disk Access in System Settings > Privacy & Security.',
    'missing_source': "This source's data was not found on the Mac; the app may be missing or never used.",
    'locked': "Another app held this source's database locked. Retry shortly.",
    'schema': "The app's database format is not one this Bridge reads. Update Sotto Bridge.",
    'query': 'The read failed in the Bridge. Retry; if it repeats, run sotto-bridged --doctor on the Mac.',
    'decode': 'Some stored values could not be decoded; other rows may still be usable.',
    'timeout': 'The read hit its time limit. Request fewer sources or a shorter window.',
    'busy': 'The Bridge was busy with another read. Retry shortly.',
    'worker_failed': 'The Bridge reader stopped unexpectedly. Retry; if it repeats, restart Sotto Bridge.',
}
_COVERAGE_FAILURES = frozenset(FAILURE_HINTS)
SEARCH_HINTS = {
    'not_configured': 'No document folders are approved for search. Choose them in Sotto Bridge settings.',
    'disabled': 'This source is turned off for Sotto. Turn it on in Sotto Bridge to search it.',
}
_COVERAGE_MEASURES = frozenset({'requested_window', 'effective_window', 'last_successful_read_at',
                              'rows_returned', 'query_row_caps', 'omitted_rows', 'failure_category'})
_COVERAGE_BASES = frozenset({'sent', 'modified', 'created', 'due_or_created', 'inventory', 'local_day', 'snapshot'})
_COVERAGE_COMPONENTS = frozenset({'deferred_unread', 'search_queries', 'first_active'})


def _coverage_timestamp(value):
    if (not isinstance(value, str) or len(value) > 64 or re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)', value) is None):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def valid_source_coverage(value, *, component=False):
    """Accept bounded typed read metadata only; nested source content is never a receipt."""
    fields = _COVERAGE_MEASURES | {'status'}
    statuses = {'ok', 'partial', 'failed', 'unavailable'}
    if not component:
        fields |= {'observed_at', 'rows_scanned', 'scan_observed', 'limit_reached', 'content_partial',
                   'bounded', 'components', 'attachments'}
        statuses |= {'unread', 'empty', 'disabled', 'degraded', 'needs_fda'}
    if not isinstance(value, dict) or set(value) - fields:
        return False
    if component and not {'status', 'failure_category'} <= set(value):
        return False
    if 'status' in value and (not isinstance(value['status'], str) or value['status'] not in statuses):
        return False
    for key in ('scan_observed', 'limit_reached', 'content_partial', 'bounded'):
        if key in value and type(value[key]) is not bool:
            return False
    def count(number):
        return type(number) is int and 0 <= number <= 2 ** 53 - 1
    for key in ('rows_scanned', 'rows_returned', 'omitted_rows'):
        if key in value and not (key == 'omitted_rows' and value[key] is None) and not count(value[key]):
            return False
    for key in ('observed_at', 'last_successful_read_at'):
        if key in value and not (key == 'last_successful_read_at' and value[key] is None) and _coverage_timestamp(value[key]) is None:
            return False
    if 'failure_category' in value and value['failure_category'] is not None and (
            not isinstance(value['failure_category'], str) or value['failure_category'] not in _COVERAGE_FAILURES):
        return False
    for key in ('requested_window', 'effective_window'):
        if key not in value:
            continue
        window = value[key]
        effective = key == 'effective_window'
        if not isinstance(window, dict) or set(window) != ({'since', 'until', 'basis'} if effective else {'since', 'until'}):
            return False
        if effective and (not isinstance(window['basis'], str) or window['basis'] not in _COVERAGE_BASES):
            return False
        bounds = []
        for bound in ('since', 'until'):
            parsed = _coverage_timestamp(window[bound])
            if parsed is None and not (effective and window[bound] is None):
                return False
            bounds.append(parsed)
        if all(bound is not None for bound in bounds) and bounds[0] > bounds[1]:
            return False
    if 'query_row_caps' in value:
        caps = value['query_row_caps']
        if not isinstance(caps, list) or len(caps) > 32 or any(not count(cap) for cap in caps) or len(set(caps)) != len(caps):
            return False
    if 'components' in value:
        components = value['components']
        if (not isinstance(components, dict) or len(components) > 8 or set(components) - _COVERAGE_COMPONENTS
                or any(not valid_source_coverage(detail, component=True) for detail in components.values())):
            return False
    if 'attachments' in value:
        attachments = value['attachments']
        if (not isinstance(attachments, dict) or set(attachments) != {'metadata', 'body'}
                or attachments['body'] != 'not_read' or not isinstance(attachments['metadata'], str)
                or attachments['metadata'] not in {'available', 'partial', 'unknown', 'not_read'}):
            return False
    return True
