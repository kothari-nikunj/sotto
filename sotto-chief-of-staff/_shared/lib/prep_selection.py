"""Deterministic research audiences and exact upcoming owner-meeting selection."""
import re
from datetime import datetime, timezone, timedelta

from textutil import _arr, _obj, _s, _names_match, FREEMAIL_DOMAINS
from timeutil import _parse_ts, _resolve_tz, configured_user_email
from research_contract import MAX_ATTENDEES_TO_RESEARCH, RESEARCH_HORIZON_HOURS, matches_focus
from calendar_context import meeting_events, work_attendees

MIN_PROFILE_FACT_CHARS = 50
HOURS_PER_HALF_DAY = 12
MAX_CLOCK_HOUR = 23

_LOW_QUALITY_PROFILE_PHRASES = ("no public profile", "no additional info", "no additional background",
                                "email signature", "team member", "works at")

def is_high_quality_profile(packed: str) -> bool:
    """True if a packed person-knowledge string carries real substance (a facts line ≥ ~50 chars,
    no low-quality placeholder phrases). Mirrors isHighQualityResearch over the knowledge graph,
    which is the cloud's attendee cache."""
    text = _s(packed)
    low = text.lower()
    if any(p in low for p in _LOW_QUALITY_PROFILE_PHRASES):
        return False
    # Primary facts may occupy several lines. Related people's facts cannot justify skipping
    # research on this person, and a short first fact must not hide the rest of their profile.
    facts = " ".join(line[1:].strip() for line in text.split("\n") if line.startswith("="))
    return len(facts) >= MIN_PROFILE_FACT_CHARS


def known_identities(local: dict, research_quality: bool = False) -> tuple[set, list]:
    """Known emails + display names (Apple Contacts + the person knowledge graph). With
    research_quality=True, a graph person counts as known ONLY if their profile is high-quality —
    so thin/stale graph entries are re-researched instead of being skipped forever (the Mac's
    re-research-on-low-quality behavior, since the cloud graph is the attendee cache)."""
    emails, names = set(), []
    for c in _arr(local, "contacts"):
        nm = _s(c.get("name")).strip()
        if nm:
            names.append(nm)
        for e in _arr(c, "emails"):
            if e:
                emails.add(e.lower().strip())
    # person_knowledge values are packed strings "Name (id) | role @ company | email"
    for packed in _obj(local, "person_knowledge").values():
        if research_quality and not is_high_quality_profile(packed):
            continue  # thin profile → re-research this attendee
        head = _s(packed).split("\n", 1)[0]
        nm = head.split("(")[0].strip()
        if nm:
            names.append(nm)
        m = re.search(r"\|\s*([^\s|]+@[^\s|]+)", head)
        if m:
            emails.add(m.group(1).lower().strip())
    return emails, names


def external_attendees(event: dict, user_email: str, user_domain: str) -> list:
    """All EXTERNAL attendees of a meeting (port of the meeting-prep set: every attendee except the
    user and same-domain colleagues). Unlike select_attendees_for_research this keeps KNOWN people
    too — prep wants them, attached to their knowledge-graph context. Deduped by email."""
    out, seen = [], set()
    for a in _arr(event, "attendees"):
        email = _s(a.get("email")).lower().strip()
        if not email or email in seen:
            continue
        # Google `self` is the queried calendar's holder: the owner on their own calendar, and
        # someone else (a spouse, an assistant) on a shared one. Neither is an outside attendee.
        if email == user_email or a.get("self"):
            continue
        if user_domain and email.endswith("@" + user_domain):
            continue
        seen.add(email)
        out.append({"name": _s(a.get("displayName")) or email.split("@")[0], "email": email})
    return out


def owner_meetings(google: dict) -> list:
    """The one participation rule for every prep selection path (sweep, name focus, clock focus):
    exact cross-calendar copies are deduplicated and owner participation counts, including free events.
    Cancelled, declined or merely shared calendar rows are not meetings to prepare for."""
    from calendar_context import prep_meeting_events
    owner = _s(google.get("userEmail"))
    # Preserve the deduper's conservative owner-decline evidence across copies.
    return prep_meeting_events(_arr(google, "events"), owner)


def upcoming(inputs: dict) -> list:
    """Every owner meeting in the window (next 72h) that has external attendees, soonest first, as
    (start_dt, event, title, start, attendees) tuples. Pure — build_context renders it and
    focus_mode() interrogates it."""
    google = _obj(inputs, "google")
    user_email = _s(google.get("userEmail")).lower()
    user_domain = user_email.split("@")[1] if "@" in user_email else ""
    if user_domain in FREEMAIL_DOMAINS:
        user_domain = ""
    now = datetime.now(timezone.utc)
    upcoming = []
    for e in owner_meetings(google):
        if _s(e.get("my_response")).lower() == "declined":
            continue                  # a meeting you declined is not one you prep for
        start = _s(e.get("start"))
        st = _parse_ts(start)
        if st is not None:
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            hours_away = (st - now).total_seconds() / 3600.0
            if hours_away < -1 or hours_away > RESEARCH_HORIZON_HOURS:
                continue
        attendees = external_attendees(e, user_email, user_domain)
        if not attendees:
            continue
        upcoming.append((st or now, e, _s(e.get("summary")), start, attendees))
    upcoming.sort(key=lambda x: x[0])
    return upcoming


def time_focus_hits(inputs: dict) -> list | None:
    """Resolve a literal owner clock selection on today's (or explicitly tomorrow's) calendar.

    None means this is not a clock selector; [] means it cannot safely identify a meeting.
    Use the configured owner zone, never the server day or the nearest meeting by guess.
    """
    focus = _s(inputs.get('focus')).strip().lower().replace('’', "'")
    match = re.fullmatch(
        r"(?:(today|tomorrow)(?:'s)?\s+)?(?:(?:my|the)\s+)?"
        r"(?:(?:meeting|call)\s+)?(?:at\s+)?"
        r"([0-2]?\d)(?::([0-5]\d))?\s*(am|pm)?"
        r"(?:\s+(?:meeting|call))?(?:\s+(today|tomorrow))?", focus)
    if not match:
        return None
    before, hour, minute, period, after = match.groups()
    hour, minute = int(hour), int(minute or 0)
    if (before and after and before != after) or hour > MAX_CLOCK_HOUR or (period and not 1 <= hour <= HOURS_PER_HALF_DAY):
        return []
    google = _obj(inputs, 'google')
    zone = _resolve_tz(_s(google.get('userTimezone')))
    if zone is None:
        return []
    day = datetime.now(timezone.utc).astimezone(zone).date()
    if (before or after) == 'tomorrow':
        day += timedelta(days=1)
    hours = {hour % HOURS_PER_HALF_DAY + (HOURS_PER_HALF_DAY if period == 'pm' else 0)} if period else (
        {hour % HOURS_PER_HALF_DAY, hour % HOURS_PER_HALF_DAY + HOURS_PER_HALF_DAY} if 1 <= hour <= HOURS_PER_HALF_DAY else {hour})
    # upcoming applies the shared participation rule; sharing a clock time never merges events.
    hits = []
    for row in upcoming(inputs):
        start = _parse_ts(row[3])
        if (start is None or start.tzinfo is None or row[1].get('status') == 'cancelled'
                or not isinstance(row[1].get('id'), str) or not row[1]['id']):
            continue
        local = start.astimezone(zone)
        if local.date() == day and local.hour in hours and local.minute == minute:
            hits.append(row)
    return hits


def title_focus_hits(inputs: dict) -> list | None:
    """Match a calendar title supplied by the agent, without fuzzy person/event inference.

    Duplicate occurrences remain ambiguous. The same owner-participation, declined and
    upcoming-window rules as every other prep path apply before selection.
    """
    def title(value):
        return " ".join(_s(value).casefold().split())
    focus = title(inputs.get('focus'))
    if not focus:
        return None
    hits = [row for row in upcoming(inputs) if title(row[2]) == focus
            and row[1].get('status') != 'cancelled'
            and isinstance(row[1].get('id'), str) and row[1]['id']
            and _parse_ts(row[3]) is not None]
    return hits or None


def focus_hits(inputs: dict, meetings: list | None = None) -> list:
    """The upcoming meetings whose external attendees match the `focus` argument (--focus), soonest
    first. Empty when no focus was asked for or it matches nobody on the calendar ahead — the
    caller must not run the ordinary sweep."""
    focus = _s(inputs.get("focus")).strip()
    if not focus:
        return []
    up = upcoming(inputs) if meetings is None else meetings
    return [u for u in up
            if any(matches_focus(focus, a["name"], a["email"]) for a in u[4])]


def focus_mode(inputs: dict) -> bool:
    """True when the user named a person AND that person is on the calendar ahead: the prep is a
    deep dive on one meeting, and compose() picks the FOCUSED PREP prompt variant. A --focus that
    matches nobody is not focus mode; compose() rejects it."""
    return bool(focus_hits(inputs))


def require_exact_meeting(inputs):
    """An explicitly offered occurrence can never fall through to a calendar sweep."""
    import delivery_effects
    exact = inputs.get('exact_meeting')
    if exact is not None:
        if (not isinstance(exact, dict) or set(exact) != {'event_id', 'start'}
                or len(_arr(_obj(inputs, 'google'), 'events')) != 1):
            raise ValueError('focused meeting occurrence invalid')
        meetings = upcoming(inputs)
        if (len(meetings) != 1 or meetings[0][1].get('id') != exact['event_id']
                or delivery_effects.instant(meetings[0][3]) != delivery_effects.instant(exact['start'])
                or (_s(inputs.get('focus')).strip() and not focus_hits(inputs, meetings))):
            raise ValueError('focused meeting occurrence unavailable')
        return meetings[0][4]
    return None


def select_attendees_for_research(inputs: dict) -> list:
    """Deterministically pick the external attendees of upcoming meetings who warrant research.
    Mirrors the Mac backend: within RESEARCH_HORIZON_HOURS, an attendee needs research unless they
    have personal addresses, are internal colleagues, or already have a researched profile.
    Returns [{name, email, meeting_title, meeting_start}], deduped by email, capped at the max."""
    google = _obj(inputs, "google")
    from render_local import resolve_contact_names
    local = resolve_contact_names(_obj(inputs, "local"))
    events = meeting_events(_arr(google, "events"))
    user_email = (_s(google.get("userEmail")) or configured_user_email()).lower()
    # Research-quality gate: a thin/stale graph profile doesn't count as "known" here, so the
    # attendee gets re-researched (port of the Mac's re-research-on-low-quality cache behavior).
    known_emails, known_names = known_identities(local, research_quality=True)
    now = datetime.now(timezone.utc)

    picked, seen = [], set()
    for e in events:
        if _s(e.get("my_response")).lower() == "declined":
            continue                  # a meeting you declined never spends a research call
        start = _s(e.get("start"))
        st = _parse_ts(start)
        if st is not None:
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            hours_away = (st - now).total_seconds() / 3600.0
            if hours_away < -1 or hours_away > RESEARCH_HORIZON_HOURS:
                continue  # past meeting or beyond the research horizon
        for a in work_attendees(e, user_email):
            email = _s(a.get("email")).lower().strip()
            if not email or email in seen:
                continue
            name = _s(a.get("displayName")) or (email.split("@")[0])
            if email in known_emails or any(_names_match(name, kn) for kn in known_names):
                continue
            seen.add(email)
            picked.append({"name": name, "email": email,
                           "meeting_title": _s(e.get("summary")), "meeting_start": start})
            if len(picked) >= MAX_ATTENDEES_TO_RESEARCH:
                return picked
    return picked

