"""One synthetic Calendar/public-profile boundary for the existing prep conversation eval.

No chat choices or final prep output are supplied here. Production readers, selection,
worker and composition remain active. Unknown source operations fail the eval.
"""
import copy
from datetime import datetime
import importlib.util
import json
import re
from pathlib import Path
import sys

AGENT = Path(__file__).resolve().parents[4]
CALENDAR_ID = 'owner@inside.test'


def seed_calendar_connection(root):
    """Synthetic local credential status; the real Google --check stays active.

    Only the eval transport can consume this unusable token. No live credential,
    refresh or OAuth authorization is involved.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / 'google_token.json'
    with path.open('x') as stream:
        json.dump({'type': 'authorized_user', 'token': 'synthetic-eval-calendar-token',
                   'refresh_token': 'synthetic-eval-refresh-token',
                   'client_id': 'synthetic-eval-client', 'client_secret': 'synthetic-eval-secret',
                   'token_uri': 'https://eval-source.invalid/token',
                   'expiry': '2099-01-01T00:00:00Z',
                   'scopes': ['https://www.googleapis.com/auth/calendar']}, stream)
    path.chmod(0o600)


def calendar_page(world, start, end):
    since = datetime.fromisoformat(start.replace('Z', '+00:00'))
    until = datetime.fromisoformat(end.replace('Z', '+00:00'))
    rows = [copy.deepcopy(row) for row in world['calendarEvents']
            if since <= datetime.fromisoformat(row['start']['dateTime'].replace('Z', '+00:00')) < until]
    if not rows:
        raise ValueError('Prep Calendar window has no declared fixture occurrence')
    return rows


def install_calendar(world, trace, denied):
    """Freeze only the Google service transport; keep real CLI and Calendar projection."""
    sys.path.insert(0, str(AGENT / 'sotto-chief-of-staff/_shared/lib'))
    import gmail_read  # noqa: PLC0415 - explicit immutable skill library selected above
    class Reply:
        def __init__(self, value): self.value = value
        def execute(self): return copy.deepcopy(self.value)
    class Calendar:
        def calendarList(self):  # noqa: N802 - Google client protocol

            return self
        def events(self):
            return Events()
        def list(self, **kwargs):
            trace({'source': 'calendar_fixture', 'operation': 'calendar_inventory'})
            return Reply({'items': [{'id': CALENDAR_ID, 'primary': True, 'accessRole': 'owner'}]})
        def close(self): pass
    class Events:
        def list(self, **kwargs):
            if kwargs.get('calendarId') != CALENDAR_ID or kwargs.get('pageToken'):
                denied('Unexpected prep Calendar selection')
            try:
                rows = calendar_page(world, kwargs['timeMin'], kwargs['timeMax'])
                trace({'source': 'calendar_fixture', 'operation': 'calendar_events', 'calendar_id': CALENDAR_ID, 'ids': [r['id'] for r in rows]})
                return Reply({'items': rows})
            except Exception:  # noqa: BLE001 - every unexpected fixture failure must leave sticky denial
                denied('Prep Calendar request missed the declared fixture window')
    def service(api, version, **kwargs):
        if (api, version) != ('calendar', 'v3'):
            denied('Prep world has no other native Google source')
        return Calendar()
    gmail_read.google_service = service


def research_response(body, trace, denied):
    try:
        return _research_response(body, trace, denied)
    except PermissionError:
        raise
    except Exception:  # noqa: BLE001 - every unexpected fixture failure must leave sticky denial
        denied('Malformed or unexpected prep public profile request')


def _research_response(body, trace, denied):
    """Reuse the existing owner-prep public profile fixture, never its scripted chat/prose."""
    if body.get('tools') != [{'google_search': {}}]:
        denied('Prep fixture only supplies public research data')
    prompt = '\n'.join(part.get('text', '') for content in body.get('contents', []) for part in content.get('parts', []))
    selectors = re.findall(r'<([^<>\s]+@[^<>\s]+)>', prompt.split('\n\n', 1)[0])
    if selectors != ['ada@example.invalid']:
        denied('Prep public profile fixture requires the exact sole Ada selector')
    location = AGENT / 'smoke/fresh-managed-photon'
    sys.path.insert(0, str(location))
    spec = importlib.util.spec_from_file_location('eval_owner_prep_data', location / 'owner_prep_fixture.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result, stage = module.native_response(body)
    if stage not in ('profile', 'recency', 'focus'):
        denied('Prep fixture cannot compose final output')
    trace({'source': 'prep_public_profile', 'stage': stage})
    return result
