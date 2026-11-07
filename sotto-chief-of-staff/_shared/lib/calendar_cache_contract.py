"""Calendar cache protocol shared by its receiver producer and triage consumer."""
import os

REFRESH_SECS_DEFAULT = 900
CACHE_DIRNAME = 'cache'
CACHE_FILENAME = 'calendar_today.json'
MEETING_END_SOURCE = 'meeting_end'
CALENDAR_CHANGE_SOURCE = 'calendar_change'


def calendar_cache_path(data_root):
    return os.path.join(data_root, CACHE_DIRNAME, CACHE_FILENAME)
