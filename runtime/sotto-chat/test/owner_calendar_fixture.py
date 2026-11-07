"""Synthetic Calendar resource; the installed reader/review/writer/receipt remain real."""

import copy
import importlib.util
import os
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote


def configure(receiver):
    data = Path(receiver.DATA)
    path = data / "calendar-event.json"
    calendar_id = os.environ.get("SOTTO_TEST_CALENDAR_ID", "primary")
    rsvp = os.environ.get("SOTTO_TEST_CALENDAR_RSVP")
    create = os.environ.get("SOTTO_TEST_CALENDAR_CREATE")
    update = os.environ.get("SOTTO_TEST_CALENDAR_UPDATE")
    combined = os.environ.get("SOTTO_TEST_CALENDAR_COMBINED")
    if rsvp:
        home=Path(os.environ['SOTTO_HOME'])
        home.mkdir(parents=True,exist_ok=True)
        credential=home/'google_token.json'
        credential.write_text(json.dumps({'token':'synthetic-calendar-token','sotto_source_generation':'a'*64}))
        credential.chmod(0o600)
    if not path.exists():
        path.write_text(
            json.dumps(
                {
                    "id": "cedar_occurrence",
                    "etag": '"version-1"',
                    "summary": "Cedar *review*",
                    "htmlLink": "https://calendar.google.com/calendar/event?eid=cedar",
                    "start": {"dateTime": "2026-10-10T10:00:00-07:00"},
                    "end": {"dateTime": "2026-10-10T11:00:00-07:00"},
                    "recurringEventId": "cedar_series",
                    "originalStartTime": {"dateTime": "2026-10-10T10:00:00-07:00"},
                    "attendees": [{"email": "dana@example.com"}],
                    "location": "Cedar office",
                    "conferenceData": {"conferenceId": "untouched"},
                }
            )
        )
        if rsvp:
            event = json.loads(path.read_text())
            event['attendees'].append({'email':'owner@example.com','self':True,'responseStatus':'needsAction'})
            if rsvp in ('all_day','series_all_day'):
                event['start']={'date':'2026-10-10'}
                event['end']={'date':'2026-10-11'}
                event['originalStartTime']={'date':'2026-10-10'}
            if rsvp == 'single':
                event.pop('recurringEventId')
                event.pop('originalStartTime')
            if rsvp in ('series','series_all_day'):
                event['id']='cedar_series'
                event.pop('recurringEventId')
                event.pop('originalStartTime')
                event['recurrence']=['RRULE:FREQ=WEEKLY']
            path.write_text(json.dumps(event))
        if update:
            event=json.loads(path.read_text())
            event.update(start={"date":"2026-10-10"},end={"date":"2026-10-11"},description="Old agenda\nKeep exact second line")
            event.pop("recurringEventId",None)
            event.pop("originalStartTime",None)
            path.write_text(json.dumps(event))
        if combined:
            event=json.loads(path.read_text())
            event['description']='Old agenda\nKeep exact second line'
            if combined=='single':
                event.pop('recurringEventId',None)
                event.pop('originalStartTime',None)
            path.write_text(json.dumps(event))
        path.chmod(0o600)

    if calendar_id != "primary" and not (data / "calendar-primary.json").exists():
        primary = json.loads(path.read_text())
        primary["summary"] = "Untouched primary event with same ID"
        (data / "calendar-primary.json").write_text(json.dumps(primary))

    class Calendar:
        def events(self):
            return self

        def get(self, **kwargs):
            assert kwargs['calendarId']==calendar_id
            assert kwargs['eventId'] in ('cedar_occurrence','cedar_series') or kwargs['eventId']==json.loads((data/'calendar-created.json').read_text())['id']
            if kwargs['eventId'] not in ('cedar_occurrence','cedar_series'):
                return SimpleNamespace(execute=lambda: json.loads((data/'calendar-created.json').read_text()))
            return SimpleNamespace(execute=lambda: json.loads(path.read_text()))

        def patch(self, **kwargs):
            assert kwargs["calendarId"] == calendar_id and kwargs["eventId"] == json.loads(path.read_text())["id"]
            assert kwargs["sendUpdates"] == "all"
            if combined:
                assert set(kwargs['body'])=={'start','end','summary','location','description'}
                assert set(kwargs['body']['start'])=={'dateTime'} and set(kwargs['body']['end'])=={'dateTime'}
            elif update:
                assert kwargs["body"] and set(kwargs["body"]) <= {"summary","location","description"}
                assert all(isinstance(value,str) for value in kwargs["body"].values())
            else:
                assert set(kwargs["body"]) == ({"attendees"} if rsvp else {"start", "end"})
            request = SimpleNamespace(headers={})

            def execute():
                old = json.loads(path.read_text())
                assert request.headers.get("If-Match") == old["etag"]
                with (data / "calendar-provider.jsonl").open("a") as stream:
                    stream.write(
                        json.dumps({"operation": "patch", "args": kwargs, "headers": request.headers}) + "\n"
                    )
                old.update(copy.deepcopy(kwargs["body"]))
                old["etag"] = '"version-2"'
                path.write_text(json.dumps(old))
                if (data / "calendar-provider-unknown").exists():
                    raise TimeoutError("synthetic post-contact failure")
                return old

            request.execute = execute
            return request

    tool = receiver.RUNTIME_OWNER_CALENDAR_TOOL
    google = tool._google()
    google._google_service = lambda service, version: Calendar()
    receiver._google_api_py = lambda: str(Path(__file__).resolve())
    tool._google = lambda: google
    if create:
        configure_create_transport(receiver)
    google.capabilities = lambda: {
        "calendar_read": True,
        "calendar_write": not (data / "calendar-revoked").exists(),
    }
    receiver._runtime_source_authority = lambda: {
        "version": 2,
        "complete": True,
        "revision": "a" * 64,
        "bridge_sources": [],
        "google_sources": ["calendar"],
    }


def read_owner(operation, arguments):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "real_calendar_reader", root / "adapters/sotto/google_api.py"
    )
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    data = Path(os.environ["SOTTO_DATA"])

    class Response:
        status_code = 200
        headers = {}

        def __init__(self, value):
            self.value = value

        def iter_content(self, chunk_size):
            yield json.dumps(self.value).encode()

        def close(self):
            pass

    class Session:
        def get(self, url, **kwargs):
            calendar_id = os.environ.get("SOTTO_TEST_CALENDAR_ID", "primary")
            base = "https://www.googleapis.com/calendar/v3/calendars/" + quote(calendar_id, safe="") + "/events"
            if url.endswith('/users/me/calendarList'):
                return Response({'items': [{'id': calendar_id, 'accessRole': 'owner'}]})
            if url == 'https://www.googleapis.com/calendar/v3/users/me/calendarList/' + quote(calendar_id, safe=''):
                return Response({'id': calendar_id, 'accessRole': 'owner', 'primary': calendar_id == 'primary'})
            assert url in (base, base + "/cedar_occurrence", base + "/cedar_series")
            event = json.loads((data / "calendar-event.json").read_text())
            with (data / "calendar-reads.jsonl").open("a") as stream:
                stream.write(json.dumps({"operation": operation, "calendar_id": calendar_id, "url": url}) + "\n")
            if url == base and os.environ.get('SOTTO_TEST_CALENDAR_RSVP') in ('series','series_all_day'):
                occurrence=copy.deepcopy(event)
                occurrence['id']='cedar_occurrence'
                occurrence.pop('recurrence')
                occurrence['recurringEventId']='cedar_series'
                occurrence['originalStartTime']=copy.deepcopy(occurrence['start'])
                return Response({'items':[occurrence]})
            return Response({"items": [event]} if url == base else event)

        def close(self):
            pass

    return api.read_owner(operation, arguments, session_factory=Session)


def configure_create_transport(receiver):
    """Only the subprocess HTTP boundary is synthetic; original CLI/parser/insert stay real."""
    data = Path(receiver.DATA)
    home = Path(os.environ['SOTTO_HOME'])
    home.mkdir(parents=True, exist_ok=True)
    token = home / 'google_token.json'
    token.write_text(json.dumps({'token':'fixture-token','refresh_token':'fixture-refresh',
        'client_id':'fixture-client','client_secret':'fixture-secret','token_uri':'https://oauth2.googleapis.com/token',
        'expiry':'2099-01-01T00:00:00Z','scopes':['https://www.googleapis.com/auth/calendar'],
        'sotto_source_generation':'a'*64}))
    token.chmod(0o600)
    os.environ['SOTTO_USER_EMAIL']='owner@example.com'
    injection=data/'calendar-python-boundary'
    injection.mkdir(parents=True,exist_ok=True,mode=0o700)
    fixture=str(Path(__file__).resolve())
    (injection/'sitecustomize.py').write_text(
        f"import importlib.util;s=importlib.util.spec_from_file_location('calendar_http',{fixture!r});"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.configure_create_http()")
    os.environ['PYTHONPATH']=str(injection)
    google=receiver.RUNTIME_OWNER_CALENDAR_TOOL._google()
    google._find_google_api=lambda: str(Path(__file__).resolve().parents[3]/'adapters/sotto/google_api.py')


def configure_create_http():
    import httplib2  # noqa: PLC0415
    from urllib.parse import urlparse, parse_qs  # noqa: PLC0415
    data=Path(os.environ['SOTTO_DATA'])
    def request(_self,uri,method='GET',body=None,headers=None,**kwargs):
        parsed=urlparse(uri)
        assert parsed.hostname=='www.googleapis.com'
        assert parsed.path=='/calendar/v3/calendars/primary/events'
        assert method=='POST' and parse_qs(parsed.query)['sendUpdates']==['all']
        event=json.loads(body)
        assert len(event['id'])==64 and set(event)<={'id','summary','start','end','attendees','location','description'}
        path=data/'calendar-created.json'
        if path.exists():
            return httplib2.Response({'status':'409','content-type':'application/json'}),json.dumps({'error':{'code':409,'message':'duplicate'}}).encode()
        event['htmlLink']='https://calendar.google.com/calendar/event?eid=created'
        path.write_text(json.dumps(event))
        with (data/'calendar-provider.jsonl').open('a') as log:
            log.write(json.dumps({'operation':'insert','args':{'body':event,'sendUpdates':'all'}})+'\n')
        if (data/'calendar-provider-unknown').exists():
            raise TimeoutError('synthetic post-contact failure')
        return httplib2.Response({'status':'200','content-type':'application/json'}),json.dumps(event).encode()
    httplib2.Http.request=request
