"""Stable relationship importance from dated, reciprocal activity; no urgency score."""
from datetime import datetime, timedelta, timezone

WINDOW_DAYS = 42
VIP_ACTIVE_DAYS, VIP_ACTIVE_WEEKS, VIP_EACH_DIRECTION_DAYS = 6, 3, 2
VVIP_ACTIVE_DAYS, VVIP_ACTIVE_WEEKS, VVIP_EACH_DIRECTION_DAYS = 12, 4, 4


def _days(values, now):
    today = now.astimezone(timezone.utc).date()
    first = today - timedelta(days=WINDOW_DAYS - 1)
    result = set()
    for value in values or []:
        try:
            day = (value.astimezone(timezone.utc).date() if isinstance(value, datetime)
                   else datetime.strptime(str(value), '%Y-%m-%d').date())
            if first <= day <= today:
                result.add(day)
        except (TypeError, ValueError, AttributeError):
            continue
    return result


def activity_evidence(person, previous, now):
    """Union dated observations, never sum overlapping windows or retain peak volume."""
    previous = previous if isinstance(previous, dict) else {}
    return {key: sorted(day.isoformat() for day in _days(
        list(previous.get(key) or []) + list(person.get(source) or []), now))
        for key, source in (('active_days', 'dates'), ('sent_days', 'from_me'), ('received_days', 'from_them'))}


def classify(evidence, now, explicit_vip=False):
    evidence = evidence if isinstance(evidence, dict) else {}
    active = _days(evidence.get('active_days'), now)
    sent = _days(evidence.get('sent_days'), now) & active
    received = _days(evidence.get('received_days'), now) & active
    weeks = {day.isocalendar()[:2] for day in active}
    tier, reason = 'regular', 'insufficient sustained two-way activity'
    if explicit_vip:
        tier, reason = 'vip', 'you marked this person VIP'
    elif len(active) >= VVIP_ACTIVE_DAYS and len(weeks) >= VVIP_ACTIVE_WEEKS and min(len(sent), len(received)) >= VVIP_EACH_DIRECTION_DAYS:
        tier, reason = 'vvip', 'frequent two-way activity across several weeks'
    elif len(active) >= VIP_ACTIVE_DAYS and len(weeks) >= VIP_ACTIVE_WEEKS and min(len(sent), len(received)) >= VIP_EACH_DIRECTION_DAYS:
        tier, reason = 'vip', 'regular two-way activity across several weeks'
    return {'tier': tier, 'reason': reason, 'active_days': len(active), 'active_weeks': len(weeks),
            'sent_days': len(sent), 'received_days': len(received), 'window_days': WINDOW_DAYS}


def for_contact(contact, history, vip_people, now, aliases=None):
    """Prefer canonical identity; ambiguous display names never borrow someone's rank."""
    name = str(contact.get('name') or '').strip().casefold()
    cid = str(contact.get('canonical_id') or '')
    history = history if isinstance(history, dict) else {}
    if cid:
        record = history.get(cid, {})
    else:
        matches = [record for key, record in history.items() if isinstance(record, dict)
                   and str(record.get('name') or key).strip().casefold() == name]
        if len(matches) > 1:
            return classify({}, now)
        record = matches[0] if matches else {}
    contact_names = {name} | {str(v).strip().casefold() for v in aliases or [] if str(v).strip()}
    explicit = bool(contact_names & {str(v).strip().casefold() for v in vip_people or []})
    return classify(record.get('importance_evidence', {}), now, explicit_vip=explicit)


def main():
    import argparse
    import json
    import os
    from pathlib import Path
    parser = argparse.ArgumentParser(description='Explain relationship importance from saved activity')
    parser.add_argument('--person')
    args = parser.parse_args()
    root = Path(os.environ.get('SOTTO_DATA', '/data'))
    def read(path):
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    history = read(root / 'knowledge/relationship_state.json').get('history', {})
    vips = read(root / 'preferences.json').get('explicit', {}).get('vip_people', [])
    now = datetime.now(timezone.utc)
    names = [args.person] if args.person else sorted({str(h.get('name') or key) for key, h in history.items()} | set(vips))
    people = [{'name': name, **for_contact({'name': name}, history, vips, now)} for name in names]
    print(json.dumps({'people': people if args.person else [p for p in people if p['tier'] in {'vip', 'vvip'}]}))


if __name__ == '__main__':
    main()
