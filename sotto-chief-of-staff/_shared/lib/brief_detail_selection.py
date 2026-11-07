"""Original gallery discovery retention and owner-local acceptance dates."""
from datetime import datetime, timezone

RETENTION_DAYS = 7
MAX_MATCHES = 20


def accepted_day(stamp, clock, zone):
    if type(stamp) not in (int, float) or not 0 <= clock - stamp <= RETENTION_DAYS * 86400:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).astimezone(zone).date().isoformat()
