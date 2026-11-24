"""A failed attendee Gmail search must read as "email history unavailable" in meeting prep, never
as "no email with this person"; a search that genuinely found nothing still reads as empty."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import compose_meeting_prep as mp
import gather_google as gg
import meeting_prep_pipeline as pipeline

UNAVAILABLE = "email history unavailable"


def _inputs(comms, availability=None):
    start = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    return {"attendee_comms": comms,
            "google": {"userEmail": "me@myco.com", "_source_availability": dict(availability or {}),
                       "events": [{"id": "e1", "summary": "Coffee with Taylor", "start": start,
                                   "attendees": [{"email": "taylor@startup.com", "displayName": "Taylor Reed"}]}]}}


def test_whole_run_failure_records_every_attendee_with_the_source_error_code(tmp_path, monkeypatch, capsys):
    attendees = tmp_path / "att.json"
    attendees.write_text(json.dumps([{"email": "Taylor@Startup.com"}, {"email": "dana@acme.com"}]))
    out = tmp_path / "comms.json"

    def refresh():
        raise gg.GoogleCLIError("HttpError 429 when requesting token")

    monkeypatch.setattr(gg, "_find_google_api", lambda: "/fake/api.py")
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: True)
    monkeypatch.setattr(gg, "_refresh_google_token", refresh)
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--attendee-comms", str(attendees), "--comms-out", str(out)])
    gg.main()
    assert json.loads(out.read_text()) == {
        "_search_failed": {"taylor@startup.com": "rate_limited", "dana@acme.com": "rate_limited"}}
    assert "WARNING" in capsys.readouterr().out


def test_failed_search_marks_gmail_partial_and_not_consulted():
    inputs, sources = _inputs({}, {"gmail": "available"}), set()
    comms = {"_search_failed": {"taylor@startup.com": "TimeoutError"}}
    pipeline.record_attendee_search(inputs, comms, {"taylor@startup.com": {}}, sources)
    assert inputs["google"]["_source_availability"]["gmail"] == "partial" and "gmail" not in sources
    pipeline.record_attendee_search(inputs, {}, {"taylor@startup.com": {}}, sources)
    assert "gmail" in sources  # a search that ran and found nothing was consulted


def test_composer_says_email_history_unavailable_only_for_a_failed_search(monkeypatch):
    monkeypatch.setattr(mp, "_active_loops", lambda: [])
    failed, _ = mp.build_context(_inputs({"_search_failed": {"Taylor@Startup.com": "rate_limited"}},
                                         {"gmail": "partial"}))
    assert UNAVAILABLE in failed and "Partial coverage: Gmail" in failed
    empty, _ = mp.build_context(_inputs({}, {"gmail": "available"}))
    assert UNAVAILABLE not in empty and "Partial coverage" not in empty and "thread:" not in empty


@pytest.mark.parametrize('succeed', [False, True])
def test_capped_attendee_search_reports_omissions_without_claiming_unread_gmail(tmp_path, monkeypatch, succeed):
    emails = [f'p{i}@example.com' for i in range(gg.ATTENDEE_COMMS_CAP)] + ['taylor@startup.com']
    attendees = tmp_path / 'attendees.json'
    attendees.write_text(json.dumps(emails))
    calls = []
    def search(api, args, timeout):
        calls.append(args)
        if not succeed:
            raise TimeoutError()
        return []
    monkeypatch.setattr(gg, '_run', search)
    comms = gg.gather_attendee_comms('/fake/api.py', str(attendees))
    inputs, sources = _inputs(comms, {'gmail': 'available'}), set()
    pipeline.record_attendee_search(inputs, comms, emails, sources)
    assert len(calls) == gg.ATTENDEE_COMMS_CAP
    assert ('gmail' in sources) == succeed
    assert inputs['google']['_source_availability']['gmail'] == 'partial'
    assert comms['_search_failed']['taylor@startup.com'] == 'not_searched'
    monkeypatch.setattr(mp, '_active_loops', lambda: [])
    prompt, _ = mp.build_context(inputs)
    assert 'attendee search limit' in prompt


def test_no_attendees_does_not_record_a_gmail_read():
    sources = set()
    pipeline.record_attendee_search(_inputs({}), {}, [], sources)
    assert not sources
