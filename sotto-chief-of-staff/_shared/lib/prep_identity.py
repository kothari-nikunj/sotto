"""Exact-address display evidence from existing consented context; no new source grant."""
import json
import os
import sys
from pathlib import Path
from datetime import timedelta
from functools import cached_property
from email.utils import getaddresses

from textutil import _s
from research_contract import CACHE_KEEP_DAYS

MAX_DISPLAY_NAME_CHARS = 120


def _human_name(value, email):
    value = _s(value).strip()
    return value if (value and len(value) <= MAX_DISPLAY_NAME_CHARS and '\n' not in value
                     and '@' not in value and value != email.partition('@')[0]) else ''


class PrepIdentityResolver:
    """One request's optional exact-identifier indexes and already-gathered message snapshot.

    Callers retain their current source guards; resolving a name never changes identity or
    replaces a contact/calendar name with published research.
    """

    def __init__(self, local, snapshot=None):
        self.local = local
        self.snapshot = snapshot

    @cached_property
    def contact_resolver(self):
        from render_local import build_identity_resolver
        from source_context import project_local
        return build_identity_resolver(project_local(self.local or {}))

    @cached_property
    def people_index(self):
        return self.knowledge.build_people_index()

    @cached_property
    def knowledge(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'knowledge'))
        import knowledge
        return knowledge

    def _contact_name(self, attendee, email):
        try:
            identity = self.contact_resolver(email) or {}
        except Exception:  # noqa: BLE001 - optional enrichment must not withhold a reminder
            identity = {}
        contact = _human_name(identity.get('name'), email)
        supplied = _human_name(attendee.get('displayName'), email)
        return (contact if contact and identity.get('confidence') == 'high'
                else supplied or contact)

    def _graph_name(self, email):
        try:
            path = self.knowledge.find_person_file(name='', identifier=email,
                                                  index=self.people_index) if email else None
            return _human_name(self.knowledge.parse_person_file(Path(path).read_text()).name, email) if path else ''
        except Exception:  # noqa: BLE001 - malformed optional graph degrades to the meeting title
            return ''

    def _gmail_name(self, email):
        from personal_context import conversation_snapshot
        from source_context import allowed
        import preferences
        if not email or not allowed('gmail'):
            return ''
        names = set()
        explicit = preferences.load_explicit()
        rows = self.snapshot() if callable(self.snapshot) else conversation_snapshot()
        for event, _, _ in rows:
            if event.get('source') not in ('email', 'gmail'):
                continue
            participants = getaddresses([_s(event[key]) for key in ('from', 'to', 'cc') if event.get(key)])
            for display_name, address in participants:
                if address.casefold() != email:
                    continue
                name = _human_name(display_name, email)
                if name and not preferences.proactively_muted(name, {**event, 'email': email}, explicit):
                    names.add(name)
        return names.pop() if len(names) == 1 and allowed('gmail') else ''

    def _cached_name(self, email, now):
        # Existing published names need no new network read or research call.
        for offset in range(CACHE_KEEP_DAYS + 1):
            day = (now - timedelta(days=offset)).strftime('%Y-%m-%d')
            path = Path(os.environ.get('SOTTO_DATA', '/data')) / 'cache' / f'research_{day}.json'
            try:
                data = json.loads(path.read_text())
                names = {_human_name(row.get('full_name'), email) for row in data.get('attendees', [])
                         if isinstance(row, dict) and _s(row.get('email')).strip().lower() == email}
            except (OSError, ValueError, AttributeError, TypeError):
                continue
            names.discard('')
            if names:
                return next(iter(names)) if len(names) == 1 else ''
        return ''

    def resolve(self, attendee, now):
        """Return (display name, additional source used); never mutate the attendee."""
        email = _s(attendee.get('email')).strip().lower()
        name = self._contact_name(attendee, email) or self._graph_name(email)
        if name:
            return name, ''
        name = self._gmail_name(email)
        return (name, 'gmail') if name else (self._cached_name(email, now), '')
