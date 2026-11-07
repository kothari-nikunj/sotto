"""compose_brief.py — offline (stubbed Gemini) contract test."""
import importlib.util
import json
import os
import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location("compose_brief", os.path.join(ROOT, "_shared", "scripts", "compose_brief.py"))
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)

import gemini      # noqa: E402  — owns call_gemini / _gemini_once (patched below)
import textutil    # noqa: E402
import timeutil    # noqa: E402
import render_local  # noqa: E402


@pytest.mark.parametrize('error_code,expected', [('sotto_budget_exhausted', 77), ('upstream_billing', None)])
def test_cli_distinguishes_proxy_budget_denial(monkeypatch, capsys, error_code, expected):
    import io
    import urllib.error
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    error = urllib.error.HTTPError(
        'https://proxy.example/native/v1beta/models/gemini-3.8-flash:generateContent',
        402, 'fixture', {}, io.BytesIO(json.dumps({'error': {'code': error_code, 'message': 'private fixture'}}).encode()))
    def fail():
        raise error
    monkeypatch.setattr(cb, 'main', fail)
    if expected:
        assert cb.cli() == expected
        captured = capsys.readouterr()
        assert captured.out == '' and 'private fixture' not in captured.err
        assert 'Model allowance reached' in captured.err
    else:
        with pytest.raises(urllib.error.HTTPError) as caught:
            cb.cli()
        assert caught.value is error


def test_cli_marks_only_typed_proxy_admission_as_uncharged(monkeypatch, capsys):
    import io
    import urllib.error
    from work_queue import MODEL_ADMISSION_BUSY_EXIT
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    url = 'https://proxy.example/native/v1beta/models/gemini-3.8-flash:generateContent'
    for headers, expected in (({'X-Sotto-Admission': 'busy'}, MODEL_ADMISSION_BUSY_EXIT),
                              ({}, None)):
        error = urllib.error.HTTPError(url, 429, 'private provider reason', headers,
                                       io.BytesIO(b'private body'))
        def fail():
            raise error
        monkeypatch.setattr(cb, 'main', fail)
        if expected is None:
            with pytest.raises(urllib.error.HTTPError) as caught:
                cb.cli()
            assert caught.value is error
        else:
            assert cb.cli() == expected
            captured = capsys.readouterr()
            assert 'Model admission busy' in captured.err
            assert 'private' not in captured.err


def test_compose_with_injected_llm():
    # inject a fake model (new-style: accepts system/schema): returns a minimal valid extraction
    def fake_llm(prompt, inputs, system=None, schema=None):
        assert "Brief type: morning" in prompt              # the rendered USER turn was passed
        assert "## SYSTEM INSTRUCTION" not in prompt        # policy is NOT duplicated into the user turn
        assert system and "Triage Discipline" in system     # static policy rides systemInstruction
        assert schema is cb.BRIEF_RESPONSE_SCHEMA           # three-field responseSchema attached
        assert inputs["type"] == "morning"
        return json.dumps({
            "brief_markdown": "# Brief\n## Needs attention\n- Reply to Sarah",
            "actions": [{"id": "a1", "section": "needs_attention", "channel": "email",
                         "action_type": "reply", "contact_name": "Sarah"}],
            "extracted_knowledge": {"person_updates": [
                {"person_name": "Sarah", "facts": [{"fact": "CTO at Acme", "memory_type": "milestone", "confidence": 0.9}]}]},
        })
    out = cb.compose({"type": "morning", "window_hours": 24, "google": {}, "granola": {}, "local": {}}, llm=fake_llm)
    assert out["brief_markdown"].startswith("# Good ") and "\n\n# Brief" in out["brief_markdown"]
    assert out["actions"][0]["contact_name"] == "Sarah"
    # contract keys always present even if the model omitted them
    assert "company_updates" in out["extracted_knowledge"]
    assert out["meetings_needing_prep"] == []


def test_compose_with_legacy_two_arg_llm_stub():
    # an OLD-style stub (prompt, inputs) still works — _invoke_llm falls back to the 2-arg call
    def fake_llm(prompt, inputs):
        assert "Brief type: morning" in prompt
        return json.dumps({"brief_markdown": "# Legacy", "actions": []})
    out = cb.compose({"type": "morning", "google": {}, "local": {}}, llm=fake_llm)
    assert _body(out["brief_markdown"]) == "# Legacy"


# --- system/user split + maintainer-comment stripping (Sprint 1 #2) ------------

def test_load_prompt_strips_maintainer_comments_keeps_markers():
    t = cb._load_prompt()
    assert "MAINTAINER" not in t                     # maintainer prose never reaches the model
    assert "PORT SOURCE" not in t
    assert "<!--id:" in t                            # tap-marker FORMAT examples survive
    assert "<!--meeting:" in t
    assert cb._SPLIT_SEAM in t                       # the seam token survives for _split_prompt


def test_split_prompt_at_documented_seam():
    system, user = cb._split_prompt(cb._load_prompt())
    assert "Triage Discipline" in system             # policy is system-side …
    assert "Triage Discipline" not in user           # … and not duplicated into the user turn
    assert "{{brief_type}}" in user and "{{gmail}}" in user
    assert "{{" not in system                        # nothing dynamic in the cacheable half
    assert cb._SPLIT_SEAM not in system and cb._SPLIT_SEAM not in user
    # byte-stable across loads — the property implicit prefix caching depends on
    assert cb._split_prompt(cb._load_prompt()) == (system, user)


def test_split_prompt_without_seam_degrades_to_single_prompt():
    assert cb._split_prompt("no seam here {{gmail}}") == ("", "no seam here {{gmail}}")
    # legacy heading seam still splits
    sys_part, user_part = cb._split_prompt(
        "intro\n## SYSTEM INSTRUCTION\npolicy text\n## DATA PROMPT\n{{gmail}}")
    assert sys_part == "policy text" and "{{gmail}}" in user_part


def test_strip_maintainer_comments_handles_quoted_close_tokens():
    # The template's maintainer block QUOTES '-->' mid-prose; the strip must reach the standalone
    # closing line, not the quoted token.
    text = ('<!-- MAINTAINER:\nthe "<!-- SYSTEM/USER SPLIT -->" seam divides things\nmore notes\n-->\n'
            "# Kept\nbody **A**<!--id:a@b.com|ch:email--> tail\n<!-- MAINTAINER: inline note -->\nend\n")
    out = cb._strip_maintainer_comments(text)
    assert "seam divides" not in out and "more notes" not in out and "inline note" not in out
    assert out.startswith("# Kept")
    assert "<!--id:a@b.com|ch:email-->" in out


def test_loaded_prompt_keeps_exact_loop_marker_instruction_but_strips_maintainer_prose():
    loaded = cb._load_prompt()
    assert "<!--loop:EXACT_LOOP_ID-->" in loaded
    assert "only for the specific ask described" in loaded
    assert "<!-- MAINTAINER:" not in loaded


def test_gemini_once_sends_system_instruction_and_schema(tmp_path, monkeypatch):
    import urllib.request
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp({"candidates": [{"content": {"parts": [{"text": "{}"}]}}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    gemini._gemini_once("m", "k", "user data", system="policy", schema={"type": "object"})
    body = captured["body"]
    assert body["systemInstruction"] == {"parts": [{"text": gemini.gemini_transport.writing_system("policy")}]}
    assert body["contents"] == [{"parts": [{"text": "user data"}]}]
    assert body["generationConfig"]["responseSchema"] == {"type": "object"}
    # legacy call shape unchanged when the kwargs are omitted
    gemini._gemini_once("m", "k", "plain")
    assert captured["body"]["systemInstruction"] == {"parts": [{"text": gemini.gemini_transport.WRITING_STYLE}]}
    assert "responseSchema" not in captured["body"]["generationConfig"]


def test_brief_response_schema_shape():
    s = cb.BRIEF_RESPONSE_SCHEMA
    assert s["required"] == ["markdown", "actionItems", "loopUpdates", "extractedKnowledge"]
    assert s["properties"]["loopUpdates"]["type"] == "array"
    assert cb._normalize_output({"markdown": "Quiet day."})["loop_updates"] == []
    # Metadata copies the visible clause, so the provider must generate that clause first.
    assert s["propertyOrdering"][:2] == ["markdown", "actionItems"]
    assert cb.BRIEF_REVISION_SCHEMA["propertyOrdering"][:2] == ["brief_markdown", "actions"]
    assert "contextAsk" in s["properties"]["actionItems"]["items"]["required"]
    for action_schema in (s['properties']['actionItems']['items'],
                          cb.BRIEF_REVISION_SCHEMA['properties']['actions']['items']):
        assert {'contextDeadline', 'deadlineDate'} <= set(action_schema['required'])
    item = s["properties"]["actionItems"]["items"]["properties"]
    for f in ("contextSummary", "contextAsk", "prose", "channel", "contactIdentifier",
              "threadSnippet", "emailThreadId", "meetingLink"):
        assert f in item, f
    ek = s["properties"]["extractedKnowledge"]["properties"]
    assert "person_updates" in ek and "company_updates" in ek
    # An extracted fact can name where it came from; a company is an entity, not a bag of news.
    fact = ek["person_updates"]["items"]["properties"]["facts"]["items"]["properties"]
    assert "source_ref" in fact
    comp = ek["company_updates"]["items"]["properties"]
    assert "about" in comp and "domain" in comp
    assert "url" in comp["news"]["items"]["properties"]


# --- critic mandate fix (Sprint 1 #1) ------------------------------------------

def test_critic_system_carries_triage_policy_and_anti_inflation():
    assert "Triage Discipline" in cb.CRITIC_SYSTEM
    assert "Needs Attention Now: stakes are real AND timing matters" in cb.CRITIC_SYSTEM
    # MISSED THREADS is redefined against the Needs-Attention bar, not completeness
    assert "meet the Needs Attention Now bar" in cb.CRITIC_SYSTEM
    assert "A brief that omits a low-stakes thread is CORRECT, not incomplete." in cb.CRITIC_SYSTEM
    assert "Never patch for coverage." in cb.CRITIC_SYSTEM
    # the same anti-inflation clause guards the revise pass
    assert "A brief that omits a low-stakes thread is CORRECT, not incomplete." in cb.REVISE_SYSTEM


def test_critic_policy_is_extracted_from_the_prompt_not_a_copy():
    """One file owns the triage bar: the critic quotes extraction-prompt.md's own sections. The
    hand-copied constant drifted (it lost the 5-item cap), which is exactly what this prevents."""
    template = cb._load_prompt()
    for heading in cb._CRITIC_POLICY_SECTIONS:
        assert heading in cb._CRITIC_TRIAGE_POLICY
    assert "At most 5 items — fewer is better." in cb.CRITIC_SYSTEM      # the drifted-away line
    # every extracted line is a line of the prompt itself — verbatim, not paraphrased
    prompt_lines = set(template.splitlines())
    body = cb._CRITIC_TRIAGE_POLICY[len(cb._CRITIC_POLICY_HEADER):]
    assert all(ln in prompt_lines for ln in body.splitlines() if ln.strip())


def test_critic_policy_falls_back_when_the_headings_move():
    """A template the parser can't read falls back WHOLE (never a half-policy) to the constant."""
    assert cb._extract_prompt_sections("## Triage Discipline\nbody\n", cb._CRITIC_POLICY_SECTIONS) == ""
    assert cb._extract_prompt_sections("## Triage Discipline\n\n## Priority Levels\nx",
                                       cb._CRITIC_POLICY_SECTIONS) == ""   # empty section
    sections = "\n\n".join(f"{heading}\nbody-{i}" for i, heading in enumerate(cb._CRITIC_POLICY_SECTIONS))
    assert cb._extract_prompt_sections(sections + "\n\n## Next\nd", cb._CRITIC_POLICY_SECTIONS) == sections


def test_extraction_critic_and_revision_share_canonical_open_task_wording(monkeypatch):
    template = cb._load_prompt()
    system, _ = cb._split_prompt(template)
    policy = cb._extract_prompt_sections(template, cb._CANONICAL_TASK_POLICY_SECTION)
    assert policy and "## Canonical Open-Task Wording" in system
    assert "same visible next step" in system
    assert "same visible next step" in policy
    assert "one complete action clause" in policy
    assert "do not make two dated task sentences" in policy
    assert "From Oct 5" in policy
    assert "Do not restate that task as a later update" in policy
    assert "Coming Up ends with its schedule preview" in policy
    monkeypatch.setattr(cb, 'build_data_manifest', lambda inputs: {})
    seen = []
    def llm(prompt, inputs, **kwargs):
        if inputs.get('_critic'):
            seen.append(('critic', prompt))
            return json.dumps({'patches': [{'type': 'remove_item', 'severity': 'critical',
                                            'detail': 'Remove a duplicate'}], 'score': 50})
        seen.append(('revise', prompt))
        return json.dumps({'brief_markdown': '## Needs Attention Now\nKept task.',
                           'actions': [], 'loop_updates': []})
    cb.critique_and_revise({'brief_markdown': '## Needs Attention Now\nDuplicate.',
                            'actions': [], 'loop_updates': []}, {}, llm=llm)
    assert [stage for stage, _ in seen] == ['critic', 'revise']
    for _, prompt in seen:
        assert policy in prompt
        assert prompt.count('## Canonical Open-Task Wording') == 1
        assert 'A complete saved summary also suffices.' in prompt
        assert 'do not repeat the same obligation as a summary followed by an instruction' in prompt
    assert 'When restoring a missing marked task, render its complete ask once' in seen[-1][1]
    assert 'Keep each different ask, its marker, priority and recipient.' in seen[-1][1]


def test_help_prompt_does_not_offer_drafting_for_owner_only_work():
    system, _ = cb._split_prompt(cb._load_prompt())
    assert 'coding expenses' in system
    assert 'not made draftable by a possible status reply' in system
    assert 'Fewer than three supported items is an honest partial brief' in ' '.join(system.split())


def test_saved_task_violations_reach_existing_revision_without_new_stage(monkeypatch):
    import brief_validate
    row = {'anchor_key': 'group-deck', 'contact_name': 'Project Group', 'status': 'open',
           'created_at': '2026-08-10 09:00:00', 'deadline': '2026-08-10',
           'summary': 'Dana shared the clinical trial deck and flagged a data room review tomorrow',
           'ask': 'Review the deck and coordinate with Dana on data room next steps'}
    draft = {'brief_markdown': '## Needs Attention Now\n**Project Group**<!--loop:group-deck-->: '
             'Dana flagged a data room review tomorrow.', 'actions': [], 'loop_updates': []}
    violations = brief_validate.validate(draft['brief_markdown'], [], '',
                                         action_ledger=[row], today='2026-08-10')
    assert any(v.startswith('marked-loop-wording:') for v in violations)
    monkeypatch.setattr(cb, 'build_data_manifest', lambda inputs: {})
    calls = []
    def llm(prompt, options, **kwargs):
        calls.append((options, prompt))
        if options.get('_critic'):
            return json.dumps({'patches': [{'type': 'add_item', 'severity': 'moderate', 'detail': '\n'.join(violations)}], 'score': 80, 'summary': 'Task needs correction'})
        return json.dumps({**draft, 'brief_markdown':
                           '## Needs Attention Now\n**Project Group**<!--loop:group-deck-->: '
                           + row['summary'] + '. ' + row['ask'] + '.'})
    result = cb.critique_and_revise(draft, {'local': {'action_ledger': [row]}}, llm=llm,
                                    violations=violations, source_prompt='full source fixture')
    assert len(calls) == 2  # the existing critic and revision only
    assert row['summary'] in calls[0][1] and row['ask'] in calls[1][1]
    assert result['_critic']['revision_reasons']['validator']['categories']['marked-loop-wording'] == 1
    assert not [v for v in brief_validate.validate(result['brief_markdown'], [], '',
                                                    action_ledger=[row], today='2026-08-10')
                if v.startswith('marked-loop-wording:')]


def test_saved_task_detail_is_not_written_to_diagnostics(monkeypatch):
    secret = 'Project Lavender private data room clause'
    row = {'anchor_key': 'private-loop', 'contact_name': 'Dana Reed', 'status': 'open',
           'deadline': '2026-08-09', 'summary': secret, 'ask': 'Reply to Dana Reed'}
    logs = []
    monkeypatch.setattr(cb, '_diag', logs.append)
    monkeypatch.delenv('SOTTO_DATA', raising=False)
    cb.compose({'type': 'morning', 'now': '2026-08-10T16:00:00Z',
                'google': {'userTimezone': 'America/Los_Angeles'},
                'local': {'action_ledger': [row]}},
               llm=lambda prompt, inputs, **kwargs: json.dumps({
                   'brief_markdown': '## Needs Attention Now\nNo task selected.', 'actions': []}))
    assert any('dropped-open-loop' in line for line in logs)
    assert all(secret not in line and row['ask'] not in line for line in logs)


# --- email-window honesty (Sprint 0 #5) ----------------------------------------

def test_email_truncation_note_reaches_prompt_and_coverage():
    inputs = {"type": "morning", "first_run": False,
              "google": {"events": [], "emails": [{"from": "a <a@x.com>", "subject": "Hi"}],
                         "emailsTruncatedAt": 40},
              "local": {}}
    p = cb.build_prompt(cb._load_prompt(), inputs)
    assert "(inbox window truncated at 40 — more arrived)" in p
    # coverage line variant (first-run surface)
    note = cb._email_truncation_note({"emailsTruncatedAt": 40})
    line = cb._coverage_line({}, {}, [], [{"id": "m1"}], note)
    assert "(inbox window truncated at 40 — more arrived)" in line
    # no truncation → no note
    assert cb._email_truncation_note({}) == ""
    p2 = cb.build_prompt(cb._load_prompt(), {"type": "morning", "first_run": False,
                                             "google": {"events": []}, "local": {}})
    assert "inbox window truncated" not in p2


def test_date_slice_truncation_uses_fixed_coverage_not_total_cap_wording():
    google = {"emails": [{"id": "m1"}], "events": [], "emailsTruncatedAt": 50,
              "emailsTruncationScope": "date_slices", "truncationNote": "provider supplied text"}
    note = cb._email_truncation_note(google)
    assert note == "(email history is partial — some date ranges reached the read limit)"
    prompt = cb.build_prompt(cb._load_prompt(), {"type": "welcome", "first_run": True,
                                                 "google": google, "local": {}})
    assert note in prompt
    assert "truncated at 50" not in prompt and "provider supplied text" not in prompt
    assert note in cb._coverage_line({}, {}, [], google['emails'], note)
    assert cb._email_truncation_note({"emailsTruncationScope": "date_slices"}) == ""


def test_cli_gmail_envelope_carries_truncation(tmp_path):
    import subprocess, sys as _sys
    (tmp_path / "gmail.json").write_text(json.dumps(
        {"emails": [{"from": "a <a@x.com>", "subject": "Enveloped subject", "snippet": "s"}],
         "truncated_at": 40}))
    stub = tmp_path / "stub.json"
    stub.write_text(json.dumps({"brief_markdown": "# B", "actions": []}))
    script = os.path.join(ROOT, "_shared", "scripts", "compose_brief.py")
    out = subprocess.run(
        [_sys.executable, script, "--type", "morning", "--gmail", str(tmp_path / "gmail.json")],
        capture_output=True, text=True,
        env={**os.environ, "SOTTO_LLM_STUB": str(stub), "SOTTO_DATA": str(tmp_path)})
    assert out.returncode == 0, out.stderr
    assert _body(json.loads(out.stdout)["brief_markdown"]) == "# B"
    # the envelope was accepted: 1 email reached the pipeline (the inputs diag names it)
    assert "1 emails" in out.stderr


def test_cli_carries_only_known_date_slice_scope_into_google_inputs(tmp_path, monkeypatch, capsys):
    import sys as _sys
    gmail = tmp_path / 'gmail.json'
    gmail.write_text(json.dumps({'emails': [{'id': 'm1', 'snippet': 's'}],
                                 'truncated_at': 50, 'truncation_scope': 'date_slices',
                                 'truncation_note': 'untrusted provider wording'}))
    captured = {}
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(cb, 'compose', lambda inputs, **kwargs:
                        captured.update(inputs=inputs) or {'brief_markdown': '# Brief', 'actions': []})
    monkeypatch.setattr(cb, '_archive_brief', lambda *a, **k: None)
    monkeypatch.setattr(_sys, 'argv', ['compose_brief.py', '--type', 'morning', '--gmail', str(gmail)])
    cb.main()
    google = captured['inputs']['google']
    assert google['emailsTruncatedAt'] == 50
    assert google['emailsTruncationScope'] == 'date_slices'
    assert 'truncationNote' not in google
    assert cb._email_truncation_note(google) == (
        '(email history is partial — some date ranges reached the read limit)')
    assert capsys.readouterr().out


def test_stub_env_path(tmp_path, monkeypatch):
    p = tmp_path / "resp.json"
    p.write_text(json.dumps({"brief_markdown": "stubbed"}))
    monkeypatch.setenv("SOTTO_LLM_STUB", str(p))
    out = cb.compose({"type": "evening", "window_hours": 24, "google": {}, "granola": {}, "local": {}})
    assert _body(out["brief_markdown"]) == "stubbed"


# --- attendee research (Phase 3) ---------------------------------------------

def _research_inputs():
    return {
        "type": "morning",
        "google": {
            "userEmail": "me@mycorp.com",
            "events": [
                {"id": "ev1", "summary": "Pitch", "start": "2026-06-24T22:00:00+00:00",
                 "attendees": [
                     {"email": "me@mycorp.com", "displayName": "Me"},
                     {"email": "colleague@mycorp.com", "displayName": "Colleague"},
                     {"email": "taylor@startup.com", "displayName": "Taylor Reed"},
                     {"email": "known@acme.com", "displayName": "Known Person"},
                 ]},
                {"id": "ev2", "summary": "Far", "start": "2026-12-30T10:00:00+00:00",
                 "attendees": [{"email": "future@far.com", "displayName": "Future"}]},
            ],
        },
        "local": {"contacts": [{"name": "Known Person", "emails": ["known@acme.com"], "phones": []}]},
    }


def test_select_attendees_filters_user_domain_known_and_horizon(monkeypatch):
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 24, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(__import__("prep_selection"), "datetime", _FixedDateTime)
    picked = cb.select_attendees_for_research(_research_inputs())
    emails = [p["email"] for p in picked]
    assert emails == ["taylor@startup.com"]  # user, same-domain, known, and far-horizon all excluded


def test_action_tap_links_are_chat_tappable():
    # Each channel maps to a web/universal scheme that renders tappable in chat.
    assert cb._action_tap_link({"channel": "whatsapp", "contactIdentifier": "+1 (555) 123-4567"}) == "https://wa.me/15551234567"
    assert cb._action_tap_link({"channel": "phone", "type": "call_back", "contactIdentifier": "+15551234567"}) == "tel:+15551234567"
    assert cb._action_tap_link({"channel": "imessage", "type": "reply", "contactIdentifier": "+15551234567"}) == "sms:+15551234567"
    m = cb._action_tap_link({"channel": "email", "type": "reply", "emailReplyTo": "dhruv@acme.com", "emailSubject": "LOI"})
    assert m == "mailto:dhruv@acme.com?subject=Re%3A%20LOI"
    assert cb._action_tap_link({"channel": "calendar", "type": "meeting_prep", "meetingLink": "https://meet.google.com/x"}) == "https://meet.google.com/x"


def test_tap_link_delegates_to_the_one_builder(monkeypatch):
    """D3: action_links.link_for is the ONLY link builder — _action_tap_link picks the
    channel/identifier and delegates, so no URL scheme lives twice."""
    calls = []

    def spy(channel, identifier, message="", subject=""):
        calls.append((channel, identifier, message, subject))
        return "spied"

    monkeypatch.setattr(cb, "link_for", spy)
    assert cb._action_tap_link({"channel": "email", "emailReplyTo": "d@acme.com",
                                "emailSubject": "LOI"}) == "spied"
    assert cb._action_tap_link({"channel": "calendar", "meetingLink": "https://meet.google.com/x"}) == "spied"
    assert calls == [("email", "d@acme.com", "", "Re: LOI"),
                     ("calendar", "https://meet.google.com/x", "", "")]


def test_calendar_action_resolves_link_from_event_id():
    # A calendar action carries only the event id (in contactIdentifier) — resolve it to the gathered
    # event's link so meeting actions become one-tap (the "no link for calendar:..." gap).
    inputs = {"google": {"events": [
        {"id": "evt123", "summary": "Pitch", "meetingLink": "https://meet.google.com/abc"},
        {"id": "evt999", "summary": "Sync", "htmlLink": "https://calendar.google.com/event?eid=z"}]}}
    elinks = cb._event_link_map(inputs)
    # meetingLink preferred
    assert cb._action_tap_link({"channel": "calendar", "contactIdentifier": "evt123"}, elinks) == "https://meet.google.com/abc"
    # falls back to htmlLink when there's no meeting link
    assert cb._action_tap_link({"channel": "calendar", "eventId": "evt999"}, elinks) == "https://calendar.google.com/event?eid=z"
    # unknown event id → no link (no fake deep link)
    assert cb._action_tap_link({"channel": "calendar", "contactIdentifier": "nope"}, elinks) == ""


def test_calendar_eid_fallback_when_event_has_no_link():
    # google_api.py sometimes returns events without htmlLink/meetingLink — build the canonical eid URL
    # from the event id + the user's calendar email (from userEmail, or the self-attendee).
    import base64
    inputs = {"google": {"userEmail": "casey@example.com", "events": [
        {"id": "9i2gt18pag0i8ch4h1qadvtjtc", "summary": "Alive"}]}}
    link = cb._event_link_map(inputs)["9i2gt18pag0i8ch4h1qadvtjtc"]
    expect = base64.b64encode(b"9i2gt18pag0i8ch4h1qadvtjtc casey@example.com").decode().rstrip("=")
    assert link == f"https://www.google.com/calendar/event?eid={expect}"
    # zero-config: no userEmail, derive the calendar id from the self-attendee
    inputs2 = {"google": {"events": [
        {"id": "evt2", "summary": "X", "attendees": [{"email": "me@x.com", "self": True}]}]}}
    assert "eid=" in cb._event_link_map(inputs2)["evt2"]


def test_compose_attaches_tap_links():
    def fake_llm(prompt, inputs):
        return json.dumps({"brief_markdown": "# B", "actions": [
            {"id": "a1", "type": "reply", "channel": "whatsapp", "contactName": "Dhruv",
             "contactIdentifier": "+15551234567"}]})
    # The recipient must be one the SOURCE carried — a link is minted for a real WhatsApp
    # counterpart, never for a number that appears only in the model's answer (see
    # tests/test_tap_link_allowlist.py for the fabricated-recipient half of this rule).
    local = {"whatsapp": [{"contact_jid": "15551234567@s.whatsapp.net", "partner_name": "Dhruv",
                           "is_from_me": False, "text": "hi"}]}
    out = cb.compose({"type": "morning", "google": {}, "local": local}, llm=fake_llm)
    assert out["actions"][0]["tap_link"] == "https://wa.me/15551234567"
    # …and with no source data at all, the same answer gets no link and no identifier.
    bare = cb.compose({"type": "morning", "google": {}, "local": {}}, llm=fake_llm)
    assert not bare["actions"][0].get("tap_link")
    assert not bare["actions"][0].get("contactIdentifier")


def _recent_stamp(hours_ago=1):
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


def test_no_bridge_fallback_uses_cached_snapshot(tmp_path):
    # A fresh run caches local; a later run with no Bridge data falls back to the snapshot + stale note.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    stamp = _recent_stamp(1)   # within TTL
    fresh = {"contacts": [{"name": "Sarah", "phones": ["+15551234567"]}],
             "imessage": [{"handle": "+15551234567", "is_from_me": False, "is_group_chat": False,
                           "timestamp": stamp, "text": "cached-bridge-marker"}],
             "generated_at": stamp, "source_status": {"imessage": "ok"}}
    cb._save_local_snapshot(fresh)
    assert not cb._local_has_data({})           # bridge down
    fb = cb._local_fallback({})
    assert cb._arr(fb, "imessage") and fb["_local_stale_since"] == stamp
    prompt = cb.build_prompt(cb._load_prompt(), {"type": "morning", "google": {"events": []}, "local": fb})
    assert "Earlier Snapshot" in prompt          # honest staleness framing
    assert "cached-bridge-marker" in prompt       # yesterday's local data, not Google-only
    del os.environ["SOTTO_DATA"]


def test_no_bridge_fallback_noop_without_snapshot(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    assert cb._local_fallback({}) == {}          # nothing cached → unchanged (Google-only, as before)
    del os.environ["SOTTO_DATA"]


def test_expired_snapshot_is_dropped_and_the_brief_says_so(tmp_path):
    """A snapshot older than the TTL is NOT replayed — better no local than day(s)-old "needs reply".
    But dropping it used to hand back a bare {}, and a brief composed from that read as a complete,
    honest day with iMessage merely quiet (a Mac asleep 37h, Sep 2026 review). A source that breaks
    must SAY so: the sources the Mac was reporting are marked unavailable and the prompt warns."""
    os.environ["SOTTO_DATA"] = str(tmp_path)
    old = _recent_stamp(cb.LOCAL_SNAPSHOT_TTL_HOURS + 5)
    cb._save_local_snapshot({"imessage": [{"text": "expired-snapshot-marker", "is_from_me": False,
                                           "timestamp": old}],
                             "generated_at": old, "source_status": {"imessage": "ok", "calls": "ok"}})
    fb = cb._local_fallback({})
    assert not cb._arr(fb, "imessage")                       # expired → not replayed
    assert fb["_source_availability"] == {"imessage": "unavailable", "calls": "unavailable"}
    assert fb["_local_unavailable_since"]
    prompt = cb.build_prompt(cb._load_prompt(), {"type": "morning", "google": {"events": []}, "local": fb})
    assert "Local Context Is Unavailable" in prompt and "expired-snapshot-marker" not in prompt
    assert "Unavailable on this device: iMessage, Phone Calls" in prompt
    # …while a Bridge that was NEVER here is unconfigured, and an unconfigured source reports nothing
    os.unlink(cb._snapshot_path())
    assert cb._local_fallback({}) == {}
    del os.environ["SOTTO_DATA"]


def test_contacts_carry_forward_on_thin_pull(tmp_path):
    # A pull with contacts caches them; a later contacts-LESS pull must not wipe them from the snapshot
    # (the "raw phone numbers in the brief" symptom). Messages update; contacts persist.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    cb._save_local_snapshot({"contacts": [{"name": "Sarah", "phones": ["+15551234567"]}],
                             "imessage": [{"text": "hi", "is_from_me": False, "timestamp": _recent_stamp(2)}],
                             "generated_at": _recent_stamp(2)})
    # Next pull: fresh messages, but contacts came back empty (partial read).
    cb._save_local_snapshot({"imessage": [{"text": "new msg", "is_from_me": False, "timestamp": _recent_stamp(1)}],
                             "generated_at": _recent_stamp(1)})
    import json
    snap = json.load(open(cb._snapshot_path()))
    assert any(c.get("name") == "Sarah" for c in snap["local"]["contacts"])   # contacts preserved
    assert snap["local"]["imessage"][0]["text"] == "new msg"                  # messages updated
    del os.environ["SOTTO_DATA"]


def test_carry_forward_reaches_the_live_run_not_just_the_file(tmp_path):
    # The symptom is LIVE: the brief being composed right now must see the carried contacts, or a
    # thin pull still renders "+1 (973) 985-7448" group labels while the snapshot quietly heals —
    # exactly the recurring report. _save_local_snapshot returns the healed copy; callers compose
    # from it.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    cb._save_local_snapshot({"contacts": [{"name": "Sarah", "phones": ["+15551234567"]}],
                             "generated_at": _recent_stamp(2)})
    healed = cb._save_local_snapshot({"imessage": [{"text": "new msg", "is_from_me": False,
                                                    "timestamp": _recent_stamp(1)}],
                                      "generated_at": _recent_stamp(1)})
    assert any(c.get("name") == "Sarah" for c in healed["contacts"])          # live copy healed
    # The disabled CHOICE is honored in the live copy too — no contacts revive in today's brief.
    dropped = cb._save_local_snapshot({"imessage": [{"text": "x", "is_from_me": False,
                                                     "timestamp": _recent_stamp(1)}],
                                       "generated_at": _recent_stamp(1),
                                       "source_status": {"contacts": "disabled"}})
    assert not dropped.get("contacts")
    del os.environ["SOTTO_DATA"]


def test_contacts_are_not_carried_forward_when_the_source_is_disabled(tmp_path):
    # A thin pull and a SWITCHED-OFF source look identical in the payload (no contacts) and must not
    # be treated alike: `source_status.contacts == "disabled"` is the user's choice, and carrying
    # yesterday's address book past it keeps a disabled source alive in every later brief.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    cb._save_local_snapshot({"contacts": [{"name": "Sarah", "phones": ["+15551234567"]}],
                             "imessage": [{"text": "hi", "is_from_me": False, "timestamp": _recent_stamp(2)}],
                             "generated_at": _recent_stamp(2), "source_status": {"contacts": "ok"}})
    cb._save_local_snapshot({"imessage": [{"text": "new msg", "is_from_me": False, "timestamp": _recent_stamp(1)}],
                             "generated_at": _recent_stamp(1),
                             "source_status": {"imessage": "ok", "contacts": "disabled"}})
    import json
    snap = json.load(open(cb._snapshot_path()))
    assert not snap["local"].get("contacts")                                 # dropped, not remembered
    assert snap["local"]["imessage"][0]["text"] == "new msg"                 # everything else unchanged
    del os.environ["SOTTO_DATA"]


def test_a_broken_contacts_source_still_carries_forward(tmp_path):
    # Only "disabled" is a choice. needs_fda / unavailable / degraded are a source that BROKE, and a
    # broken source's last good data is still the best name resolution available.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    cb._save_local_snapshot({"contacts": [{"name": "Sarah", "phones": ["+15551234567"]}],
                             "generated_at": _recent_stamp(2), "source_status": {"contacts": "ok"}})
    cb._save_local_snapshot({"imessage": [{"text": "new msg", "is_from_me": False, "timestamp": _recent_stamp(1)}],
                             "generated_at": _recent_stamp(1),
                             "source_status": {"contacts": "needs_fda"}})
    import json
    snap = json.load(open(cb._snapshot_path()))
    assert any(c.get("name") == "Sarah" for c in snap["local"]["contacts"])
    del os.environ["SOTTO_DATA"]


def test_browser_only_payload_is_a_live_local_observation():
    assert cb._live_local_observation({"search_queries": ["quarterly planning"]})
    assert cb._live_local_observation({"screen_time": {"top_apps": [{"app_name": "Notes"}]}})


def test_partial_browser_read_keeps_explicit_live_fields_without_cached_sibling(tmp_path):
    previous = {
        "captured_at": _recent_stamp(2),
        "local": {
            "chrome_history": [{"domain": "old.example", "visit_count": 2}],
            "search_queries": ["older query"],
            "source_status": {"chrome": "ok"},
            "_source_observed_at": {"chrome": _recent_stamp(2)},
        },
    }
    live_stamp = _recent_stamp(1)
    merged = cb._merge_local_snapshot({
        "generated_at": live_stamp,
        "chrome_history": [{"domain": "fresh.example", "visit_count": 3}],
        "search_queries": [],
        "source_status": {"chrome": "partial"},
    }, previous)
    assert merged["chrome_history"][0]["domain"] == "fresh.example"
    assert merged["search_queries"] == []
    assert merged["source_status"]["chrome"] == "partial"
    assert merged["_source_availability"]["chrome"] == "partial"
    assert merged["_source_observed_at"]["chrome"] == live_stamp
    assert "_local_stale_since" not in merged

    # A later partial response that omits search_queries cannot resurrect the old cached query.
    again = cb._merge_local_snapshot({
        "generated_at": _recent_stamp(0),
        "chrome_history": [{"domain": "newest.example", "visit_count": 1}],
        "source_status": {"chrome": "degraded"},
    }, {"captured_at": live_stamp, "local": merged})
    assert "search_queries" not in again


def test_source_availability_uses_readable_local_source_labels():
    text = cb._format_source_availability({
        "safari": "unavailable", "recent_files": "unavailable",
        "screen_time": "disabled", "apple_notes": "disabled",
    })
    assert "Safari History" in text and "Recent Files" in text
    assert "Screen Time" in text and "Apple Notes" in text


def test_build_data_manifest_shape():
    inputs = {"google": {"emails": [{"headers": {"subject": "Deal", "from": "a@x.com"}, "threadId": "t1"},
                                    {"headers": {"subject": "Deal", "from": "a@x.com"}, "threadId": "t1"}],
                         "events": [{"summary": "Pitch", "attendees": [{"email": "x@y.com"}]}]},
              "local": {"contacts": [{"name": "Sarah", "phones": ["+15551234567"]}],
                        "imessage": [{"handle": "+15551234567", "is_from_me": False, "is_group_chat": False}],
                        "action_ledger": [{"status": "open"}, {"status": "resolved"}]}}
    m = cb.build_data_manifest(inputs)
    assert m["email_count"] == 2 and len(m["email_threads"]) == 1   # deduped by threadId
    assert m["imessage_contacts"] == ["Sarah"]
    assert m["calendar_event_count"] == 1 and m["action_ledger_open"] == 1


def test_critic_receives_the_same_task_context_as_the_writer(monkeypatch):
    rows = [
        {"anchor_key": "lease-a", "status": "open", "contact_name": "Alex Stone",
         "contact_identifier": "alex-a@example.com", "channel": "gmail", "action_type": "reply",
         "created_at": "2026-10-13T08:00:00Z", "summary": "Lease decision",
         "ask": "Send the revised lease tomorrow, after checking the guarantor clause"},
        {"anchor_key": "deck-b", "status": "waiting", "contact_name": "Alex Stone",
         "contact_identifier": "alex-b@example.com", "channel": "gmail", "action_type": "waiting_on",
         "created_at": "2026-10-12T08:00:00Z", "summary": "Waiting for deck",
         "ask": "Alex owes the revised deck; do not chase before Friday"},
    ]
    inputs = {"now": "2026-10-14T08:00:00Z", "local": {"action_ledger": rows}}
    manifest = cb.build_data_manifest(inputs)
    assert manifest['open_commitments'] == cb._format_action_ledger(
        cb._normalize_local(inputs), cb._brief_now(inputs))
    # Same-name tasks and their qualifications must reach the actual critic request,
    # not just the extraction prompt or a count of open rows.
    prompts = []
    def critic(prompt, _inputs):
        prompts.append(prompt)
        return json.dumps({"patches": [], "score": 90})
    cb.run_critic('Brief', [], manifest, llm=critic)
    for row in rows:
        assert row['ask'] in prompts[0]
        assert row['contact_identifier'] in prompts[0]
        assert row['created_at'] in prompts[0]


def test_critic_knows_email_excerpt_is_partial_and_which_direction_it_came_from():
    manifest = cb.build_data_manifest({'google': {'emails': [{
        'id': 'm-actual', 'threadId': 't-actual', 'from': 'owner@example.com',
        'date': '2026-10-14T08:00:00Z', 'isSent': True,
        'body': 'Context ' * 30 + 'I cannot commit to Friday yet.',
    }]}})
    thread = manifest['email_threads'][0]
    assert thread['message_id'] == 'm-actual'
    assert thread['sent_by_owner'] is True
    assert thread['date'] == '2026-10-14T08:00:00Z'
    assert thread['snippet_truncated'] is True


def test_critic_receives_action_identity_and_complete_ask_instead_of_summary_alone():
    action = {'id': 'a', 'contactName': 'Alex Stone', 'contactIdentifier': 'alex@example.com',
              'loopId': 'lease-a', 'contextSummary': 'Context ' * 30,
              'contextAsk': 'Send the lease only after legal approval', 'prose': ''}
    prompts = []
    cb.run_critic('Brief', [action], {}, llm=lambda prompt, _inputs:
                  prompts.append(prompt) or '{"patches": [], "score": 90}')
    for key in ('contactIdentifier', 'loopId', 'contextSummary', 'contextAsk'):
        assert action[key] in prompts[0]


@pytest.mark.parametrize('deadline_text,deadline_date', [
    ('tomorrow', '2026-10-19'), ('when legal approves', ''), ('', ''),
])
def test_critic_receives_deadline_fields_and_original_source_date(monkeypatch, deadline_text, deadline_date):
    monkeypatch.setattr('source_context.allowed', lambda source: True)
    quote = 'I will send the pricing document tomorrow.'
    action = {'type': 'follow_up', 'channel': 'gmail', 'contactName': 'Maya Journey',
              'contactIdentifier': 'maya.journey@example.test', 'contextAsk': quote,
              'contextDeadline': deadline_text, 'deadlineDate': deadline_date,
              'evidence': [{'sourceType': 'email', 'sourceId': 'promise-mail', 'snippet': quote}]}
    mail = {'id': 'promise-mail', 'from': 'owner@example.test', 'to': 'maya.journey@example.test',
            'isSent': True, 'date': '2026-10-18T08:00:00Z', 'body': quote}
    prompts = []
    cb.run_critic('Brief', [action], {}, llm=lambda prompt, _inputs:
                  prompts.append(prompt) or '{"patches": [], "score": 90}',
                  source_index={'email': [mail]})
    reviewed = json.loads(prompts[0].split('## ACTION ITEMS (1 shown of 1 total)\n')[1]
                          .split('\n\nAnalyze the brief')[0])[0]
    assert reviewed['contextDeadline'] == deadline_text
    assert reviewed['deadlineDate'] == deadline_date
    observed = reviewed['evidence'][0]['observed_source']
    assert observed['date'] == '2026-10-18T08:00:00Z'
    assert observed['original_context'] == quote


def test_critic_and_revise_fixes_brief(monkeypatch):
    # First llm call = extraction; second = critic (returns a moderate patch); third = revise.
    monkeypatch.setenv("SOTTO_CRITIC", "always")   # tiny test inputs would otherwise auto-skip
    calls = {"n": 0}

    def fake_llm(prompt, inputs):
        calls["n"] += 1
        if inputs.get("_critic"):
            return json.dumps({"patches": [{"type": "add_item", "detail": "Missed Sarah's thread",
                                            "severity": "moderate"}], "score": 70, "summary": "missed one"})
        if inputs.get("_revise"):
            return json.dumps({"brief_markdown": "# Revised\n- Added Sarah", "actions": []})
        return json.dumps({"brief_markdown": "# Draft", "actions": [],
                           "extracted_knowledge": {"person_updates": [], "company_updates": []}})

    out = cb.compose({"type": "morning", "google": {}, "local": {}}, llm=fake_llm, critic=True)
    assert _body(out["brief_markdown"]) == "# Revised\n- Added Sarah"   # revise pass applied
    assert out["_critic"]["actionable"] == 1
    assert calls["n"] == 3


def test_critic_passes_clean_brief_unchanged(monkeypatch):
    monkeypatch.setenv("SOTTO_CRITIC", "always")

    def fake_llm(prompt, inputs):
        if inputs.get("_critic"):
            return json.dumps({"patches": [], "score": 95, "summary": "clean"})
        return json.dumps({"brief_markdown": "# Clean", "actions": []})

    out = cb.compose({"type": "morning", "google": {}, "local": {}}, llm=fake_llm, critic=True)
    assert _body(out["brief_markdown"]) == "# Clean"        # no actionable patches → no revise
    assert out["_critic"]["actionable"] == 0


def test_critic_receives_task_and_original_clause_for_loop_proposal(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    import loops_query
    monkeypatch.setattr(loops_query, 'permitted_rows', lambda _root, _sources: inputs['local']['action_ledger'])
    seen = []
    def critic_stub(prompt, _options):
        seen.append(prompt)
        return '{"patches": [], "score": 90, "summary": "reviewed"}'

    inputs = {'type': 'morning', 'local': {'action_ledger': [{
        'anchor_key': 'deck-loop', 'status': 'open', 'ask': 'send Dana the deck',
        'summary': 'Send Dana the deck'}]},
        'google': {'emails': [{'id': 'mail-1', 'isSent': True,
            'date': '2026-09-19T10:00:00Z', 'from': 'me@example.com',
            'to': 'dana@example.com', 'body': "I haven't sent the deck yet."}],
                   'events': []}}
    out = {'brief_markdown': 'Brief.', 'actions': [], 'loop_updates': [{
        'loopId': 'deck-loop', 'status': 'resolved',
        'reason': 'The deck was sent.', 'evidence': [{
            'sourceType': 'email', 'sourceId': 'mail-1', 'snippet': 'sent the deck'}]}]}
    cb.critique_and_revise(out, inputs, llm=critic_stub)
    assert len(seen) == 1
    assert 'send Dana the deck' in seen[0]
    assert "I haven't sent the deck yet." in seen[0]
    assert 'sent the deck' in seen[0]


# --- conditional critic (SOTTO_CRITIC auto/always/off) -------------------------

def _one_call_llm(brief='{"brief_markdown": "# B", "actions": []}'):
    """An llm stub that FAILS if the critic/revise pass calls it — proves the pass was skipped."""
    calls = {"n": 0}

    def fake_llm(prompt, inputs):
        calls["n"] += 1
        assert not inputs.get("_critic") and not inputs.get("_revise"), "critic ran but should be skipped"
        return brief
    return fake_llm, calls


def _bulky_inputs(chars=cb.CRITIC_AUTO_MIN_PAYLOAD_CHARS + 8000):
    """Inputs whose rendered source payload exceeds the auto threshold."""
    body = "meeting notes and decisions " * (chars // 28)
    return {"type": "morning", "google": {"events": [], "emails": [
        {"from": "Jane <jane@acme.com>", "subject": "Big thread", "snippet": "long", "body": body}]},
        "local": {}}


def test_critic_decision_matrix():
    assert cb._critic_decision("off", 10**6, 50) == (False, "SOTTO_CRITIC=off")
    assert cb._critic_decision("always", 0, 0) == (True, "SOTTO_CRITIC=always")
    run, reason = cb._critic_decision("auto", cb.CRITIC_AUTO_MIN_PAYLOAD_CHARS - 1, cb.CRITIC_AUTO_MIN_ACTIONS)
    assert run is False and "small brief" in reason
    # either side of the AND flips it back to running
    assert cb._critic_decision("auto", cb.CRITIC_AUTO_MIN_PAYLOAD_CHARS, 0)[0] is True       # big payload
    # the first brief is always reviewed — its thin shape is exactly what "auto" would skip
    assert cb._critic_decision("auto", 0, 0, first_run=True) == (True, "first brief — always reviewed")
    assert cb._critic_decision("off", 0, 0, first_run=True)[0] is False
    assert cb._critic_decision("auto", 0, cb.CRITIC_AUTO_MIN_ACTIONS + 1)[0] is True         # many actions
    # a measured violation is the one thing the revise pass exists for — it outranks the quiet skip
    assert cb._critic_decision("auto", 0, 0, n_violations=1) == (True, "1 validator violation(s) to fix")
    assert cb._critic_decision("off", 0, 0, n_violations=3)[0] is False                      # off wins
    # only a reviewed closure is applied, so a proposed closure outranks the quiet skip too
    assert cb._critic_decision("auto", 0, 0, n_closures=1) == (True, "1 proposed task closure(s) to review")
    assert cb._critic_decision("off", 0, 0, n_closures=1)[0] is False
    # unknown mode string falls back to auto
    import os as _os
    _os.environ["SOTTO_CRITIC"] = "banana"
    try:
        assert cb._critic_mode() == "auto"
    finally:
        del _os.environ["SOTTO_CRITIC"]


def test_critic_auto_skips_small_brief(monkeypatch, tmp_path):
    monkeypatch.delenv("SOTTO_CRITIC", raising=False)          # default = auto
    # …on an ordinary day: the FIRST brief is always reviewed, so plant a delivered marker
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    (tmp_path / "briefs").mkdir()
    (tmp_path / "briefs" / "2026-06-01.morning.delivered").write_text("")
    fake_llm, calls = _one_call_llm()
    out = cb.compose({"type": "morning", "google": {}, "local": {}}, llm=fake_llm, critic=True)
    assert calls["n"] == 1                                     # extraction only — no critic/revise calls
    assert out["_critic"]["skipped"] is True and "small brief" in out["_critic"]["reason"]


def test_critic_auto_runs_on_large_payload(monkeypatch):
    monkeypatch.delenv("SOTTO_CRITIC", raising=False)
    seen = {"critic": False}

    def fake_llm(prompt, inputs):
        if inputs.get("_critic"):
            seen["critic"] = True
            return json.dumps({"patches": [], "score": 90, "summary": "ok"})
        return json.dumps({"brief_markdown": "# Big", "actions": []})

    out = cb.compose(_bulky_inputs(), llm=fake_llm, critic=True)
    assert seen["critic"] is True and out["_critic"]["actionable"] == 0


def test_critic_auto_runs_when_many_actions(monkeypatch):
    monkeypatch.delenv("SOTTO_CRITIC", raising=False)
    seen = {"critic": False}
    actions = [{"id": f"a{i}", "type": "reply", "channel": "email", "contactName": f"P{i}"}
               for i in range(cb.CRITIC_AUTO_MIN_ACTIONS + 1)]

    def fake_llm(prompt, inputs):
        if inputs.get("_critic"):
            seen["critic"] = True
            return json.dumps({"patches": [], "score": 90, "summary": "ok"})
        return json.dumps({"brief_markdown": "# B", "actions": actions})

    cb.compose({"type": "morning", "google": {}, "local": {}}, llm=fake_llm, critic=True)
    assert seen["critic"] is True                              # small payload, but 6 actions → run


def test_a_validator_violation_runs_the_critic_on_an_otherwise_quiet_brief(monkeypatch, tmp_path):
    """The auto skip saves two calls on a brief with nothing to catch. A violation IS something to
    catch, and the revise pass is the only thing that fixes it — so on the quiet brief where a
    dropped open loop or a never-tell-twice breach is most visible, the skip was logging the defect
    with `_diag` and shipping it. `SOTTO_CRITIC=off` still wins, violation or not."""
    monkeypatch.delenv("SOTTO_CRITIC", raising=False)          # default = auto
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))            # …and not the always-reviewed first brief
    (tmp_path / "briefs").mkdir()
    (tmp_path / "briefs" / "2026-06-01.morning.delivered").write_text("")
    monkeypatch.setattr(cb.brief_validate, "validate", lambda *a, **k: ["dropped-open-loop: Jane"])
    seen = {"violation_reached_the_critic": False}

    def fake_llm(prompt, inputs):
        if inputs.get("_critic"):
            seen["violation_reached_the_critic"] = "dropped-open-loop: Jane" in prompt
            return json.dumps({"patches": [], "score": 90, "summary": "ok"})
        return json.dumps({"brief_markdown": "# B", "actions": []})

    quiet = {"type": "morning", "google": {}, "local": {}}
    out = cb.compose(quiet, llm=fake_llm, critic=True)
    assert seen["violation_reached_the_critic"] is True
    # An advisory coverage hint reaches review but does not force a paid rewrite.
    assert "skipped" not in out["_critic"] and out["_critic"]["actionable"] == 0
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    fake, calls = _one_call_llm()
    out = cb.compose(quiet, llm=fake, critic=True)
    assert calls["n"] == 1 and out["_critic"] == {"skipped": True, "reason": "SOTTO_CRITIC=off"}


def test_critic_off_env_never_runs(monkeypatch):
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    fake_llm, calls = _one_call_llm()
    out = cb.compose(_bulky_inputs(), llm=fake_llm, critic=True)   # large brief, still skipped
    assert calls["n"] == 1
    assert out["_critic"] == {"skipped": True, "reason": "SOTTO_CRITIC=off"}


def test_critic_failure_never_blocks_delivery(monkeypatch):
    # The critic call blowing up (network, junk JSON) must deliver the draft brief unchanged.
    monkeypatch.setenv("SOTTO_CRITIC", "always")

    def fake_llm(prompt, inputs):
        if inputs.get("_critic"):
            raise RuntimeError("gemini 500")
        return json.dumps({"brief_markdown": "# Draft survives", "actions": []})

    out = cb.compose({"type": "morning", "google": {}, "local": {}}, llm=fake_llm, critic=True)
    assert _body(out["brief_markdown"]) == "# Draft survives"


def test_research_quality_gate_rereseaches_thin_graph_profile(monkeypatch):
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 24, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(__import__("prep_selection"), "datetime", _FixedDateTime)
    inputs = {"google": {"userEmail": "me@mycorp.com", "events": [
        {"id": "ev1", "summary": "Pitch", "start": "2026-06-24T22:00:00+00:00", "attendees": [
            {"email": "thin@startup.com", "displayName": "Thin Person"},
            {"email": "rich@acme.com", "displayName": "Rich Person"}]}]},
        "local": {"person_knowledge": {
            "thin-person": "Thin Person (thin-person) | thin@startup.com\n= team member",
            "rich-person": "Rich Person (rich-person) | CEO @ Acme | rich@acme.com\n"
                           "= raised a Series B last quarter; previously VP Eng at BigCo; based in NYC"}}}
    picked = [p["email"] for p in cb.select_attendees_for_research(inputs)]
    assert "thin@startup.com" in picked     # thin profile → re-research
    assert "rich@acme.com" not in picked     # rich profile → already known, skip


def test_select_attendees_caps_at_max(monkeypatch):
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 24, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(__import__("prep_selection"), "datetime", _FixedDateTime)
    attendees = [{"email": f"p{i}@ext.com", "displayName": f"P{i}"} for i in range(40)]
    inputs = {"google": {"userEmail": "me@mycorp.com",
                         "events": [{"id": "e", "summary": "Big", "start": "2026-06-24T22:00:00+00:00",
                                     "attendees": attendees}]}, "local": {}}
    assert len(cb.select_attendees_for_research(inputs)) == cb.MAX_ATTENDEES_TO_RESEARCH


def test_attendee_research_renders_into_prompt():
    inputs = _research_inputs()
    inputs["attendee_research"] = [
        {"email": "taylor@startup.com", "title": "CEO", "company": "Startup Inc.",
         "relevance": ["Raising a Series A"], "summary": "CEO of Startup Inc., a dev-tools company."},
        {"email": "nobody@void.com", "title": None, "company": "",
         "relevance": [], "summary": "No public profile found."},
    ]
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "Attendee Research (PRE-COMPUTED" in prompt
    assert "taylor@startup.com — CEO at Startup Inc." in prompt
    assert "Raising a Series A" in prompt
    # "No public profile found." summary is suppressed (not echoed as a bio line)
    assert "nobody@void.com" in prompt
    assert prompt.count("No public profile found.") == 0


def test_attendee_research_renders_recency_fields_and_company_fallback():
    formatted = cb._format_attendee_research({"attendee_research": [
        {"email": "taylor@startup.com", "title": "CEO", "company": "Startup Inc.",
         "relevance": ["Raising a Series A"], "summary": "CEO of Startup Inc.",
         "company_summary": "Startup Inc. builds CI/CD tooling.",
         "recent_activity": [{"when": "late July 2026", "what": "Published an agent-memory piece.",
                              "source_url": "https://t.substack.com/p/mem"},
                             {"when": "", "what": "", "source_url": "https://x.com/skipme"}],
         "personal": ["Ran the SF Marathon (https://x.com/t/1)"],
         # A stale cache row may still carry a hook; the renderer must not resurrect it.
         "conversation_hooks": ["His agent-memory piece last week fits your roadmap."]},
        {"email": "nelson@cobalt-research.com", "title": None, "company": "Cobalt Research",
         "relevance": [], "summary": "No public profile found.",
         "company_summary": "Cobalt Research does battery-materials analytics."},
    ]})
    # Compact, one-per-line, dates + urls preserved (snapshot of the block shape).
    assert "  Recent: Published an agent-memory piece. (late July 2026) — https://t.substack.com/p/mem" in formatted
    assert "skipme" not in formatted                                   # what-less item not rendered
    assert "  Personal: Ran the SF Marathon (https://x.com/t/1)" in formatted
    assert "Hook:" not in formatted and "fits your roadmap" not in formatted
    # Person → company fallback: sentinel bio suppressed, company line shown instead.
    assert formatted.count("No public profile found.") == 0
    assert "  Company: Cobalt Research does battery-materials analytics." in formatted
    # The consumption note tells the model what the new lines are and bans filler.
    assert "never pad with generic process points" in formatted


def test_attendee_research_absent_leaves_no_placeholder():
    prompt = cb.build_prompt(cb._load_prompt(), {"type": "morning", "google": {"events": []}, "local": {}})
    assert "{{attendee_research}}" not in prompt
    assert "Attendee Research (PRE-COMPUTED" not in prompt


def test_tap_link_drops_nonroutable_imessage_identifiers():
    # The bug: name slugs / group ids leaked as fake sms: deep links (sms:arnav_sahu, sms:group_jake_ts).
    assert cb._action_tap_link({"channel": "imessage", "contactIdentifier": "arnav_sahu"}) == ""
    assert cb._action_tap_link({"channel": "sms", "contactIdentifier": "group_jake_ts"}) == ""
    # A real phone still routes.
    assert cb._action_tap_link({"channel": "imessage", "contactIdentifier": "+1 (206) 999-4970"}) == "sms:+12069994970"


def test_tap_link_drops_letters_stripped_into_a_fake_phone():
    # The bug: naive \D-stripping turned "contact_14155551234" into "+14155551234" — a real-looking
    # phone number built out of a contact id's digits. message_targets rejects the identifier
    # outright (it contains letters), so no link is ever minted for it.
    assert cb._action_tap_link({"channel": "imessage", "contactIdentifier": "contact_14155551234"}) == ""
    assert cb._action_tap_link({"channel": "sms", "contactIdentifier": "contact_14155551234"}) == ""
    assert cb._action_tap_link({"channel": "whatsapp", "contactIdentifier": "contact_14155551234"}) == ""
    assert cb._action_tap_link({"channel": "phone", "type": "call_back",
                                "contactIdentifier": "contact_14155551234"}) == ""


def test_tap_link_whatsapp_and_email_unaffected():
    assert cb._action_tap_link({"channel": "whatsapp", "contactIdentifier": "15551234567"}) == "https://wa.me/15551234567"
    assert cb._action_tap_link({"channel": "email", "emailReplyTo": "a@b.com"}).startswith("mailto:a@b.com")


def test_imessage_names_resolve_from_contacts():
    # The bug: _group_messages_into_threads passed an empty lookup, so iMessage handles never resolved
    # to contact names. With the contacts array threaded through, the raw phone becomes the contact name.
    lookup = cb.build_contact_lookup([{"name": "Jake Rosen", "phones": ["+1 206 999 4970"]}])
    threads = cb._group_messages_into_threads(
        [{"handle": "+12069994970", "text": "coffee?", "is_from_me": False, "timestamp": "2026-06-25"}],
        "imessage", lookup)
    assert threads and threads[0]["name"] == "Jake Rosen"
    # Without the lookup it must NOT invent a name — it stays the raw handle.
    bare = cb._group_messages_into_threads(
        [{"handle": "+12069994970", "text": "coffee?", "is_from_me": False, "timestamp": "2026-06-25"}],
        "imessage")
    assert bare[0]["name"] != "Jake Rosen"


def test_brief_excludes_proven_assistant_rows_but_keeps_human_invitation():
    inputs = {"type": "morning", "first_run": False, "google": {"events": [], "emails": []},
              "local": {"contacts": [
                  {"name": "Poke", "phones": ["+12025550123"]},
                  {"name": "Ramp", "emails": ["ramp_cf8gd1ek_agent@rbm.goog"]},
                  {"name": "Chase", "emails": ["chasebank_fraud_agent@rbm.goog"]}],
                  "imessage": [
                      {"handle": "ramp_cf8gd1ek_agent@rbm.goog", "text": "ASSISTANT_MARKER submit receipt",
                       "sender_type": "assistant",
                       "is_from_me": False, "timestamp": "2026-09-14T18:00:00Z"},
                      {"handle": "+12025550123", "text": "HUMAN_MARKER want to join dinner Friday?",
                       "is_from_me": False, "timestamp": "2026-09-14T18:01:00Z"},
                      # Same RCS business-messaging transport as Ramp, but nothing marks the sender
                      # as an assistant — the brief's own relevance judgment gets to see it.
                      {"handle": "chasebank_fraud_agent@rbm.goog",
                       "text": "BANK_MARKER fraud alert: did you authorize $4,200? Reply NO to block",
                       "is_from_me": False, "timestamp": "2026-09-14T18:02:00Z"}]}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "ASSISTANT_MARKER" not in prompt
    assert "HUMAN_MARKER" in prompt
    assert "BANK_MARKER" in prompt


def test_whatsapp_jid_never_becomes_mailto():
    # The bug: a WhatsApp JID (…@s.whatsapp.net) has an '@', so it was routed to mailto:. Channel is
    # authoritative now, and the JID is stripped to its phone for wa.me.
    assert cb._action_tap_link({"channel": "whatsapp", "type": "reply",
                                "contactIdentifier": "4525171275@s.whatsapp.net"}) == "https://wa.me/4525171275"
    # A name-only JID has no phone → no link (not a broken mailto).
    assert cb._action_tap_link({"channel": "whatsapp", "type": "reply",
                                "contactIdentifier": "alberto_taiuti@s.whatsapp.net"}) == ""
    # A real email still routes to mailto.
    assert cb._action_tap_link({"channel": "email", "type": "reply",
                                "contactIdentifier": "sarah@acme.com"}).startswith("mailto:sarah@acme.com")
    # A reply whose id is a JID but channel missing must NOT mailto — infer sms from the phone.
    assert cb._action_tap_link({"type": "reply",
                                "contactIdentifier": "4525171275@s.whatsapp.net"}) == "sms:+4525171275"


def test_birthdays_surfaces_next_7_days_only():
    import datetime
    today = datetime.date.today()
    soon = today + datetime.timedelta(days=3)
    far = today + datetime.timedelta(days=40)
    local = {"contacts": [
        {"name": "Jake Rosen", "birthday": today.strftime("%m-%d")},
        {"name": "Mira Patel", "birthday": soon.strftime("%m-%d")},
        {"name": "Old Friend", "birthday": far.strftime("%m-%d")},
        {"name": "No BDay", "birthday": ""},
    ]}
    out = cb._format_birthdays(local)
    assert "Jake Rosen" in out and "TODAY" in out      # today's birthday flagged
    assert "Mira Patel" in out and "in 3 days" in out   # within the week
    assert "Old Friend" not in out                      # 40 days out → excluded
    # soonest first
    assert out.index("Jake Rosen") < out.index("Mira Patel")


def test_gemini_fallback_on_429_when_backup_configured(monkeypatch):
    import urllib.error
    calls = []

    def fake_once(model, key, prompt, label=""):
        calls.append((model, key, label))
        if len(calls) <= 2:  # primary 429s, and so does its one retry
            raise urllib.error.HTTPError("u", 429, "RESOURCE_EXHAUSTED", {}, None)
        return '{"markdown": "ok"}'

    monkeypatch.setattr(gemini, "_gemini_once", fake_once)
    monkeypatch.setattr(gemini, "_PRIMARY_RETRY_BACKOFF_S", 0)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "primary")
    monkeypatch.setenv("SOTTO_FALLBACK_MODEL", "gemini-2.5-pro")
    monkeypatch.setenv("SOTTO_FALLBACK_API_KEY", "backup")
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    out = gemini.call_gemini("p", {})
    assert out == '{"markdown": "ok"}'
    primary = cb.os.environ.get("SOTTO_GEMINI_MODEL", "gemini-3.8-flash")
    assert calls[0][0] == primary
    assert calls[1][0] == primary and calls[1][2] == " [retry]"    # bounded primary retry first
    assert calls[2][:2] == ("gemini-2.5-pro", "backup") and calls[2][2] == " [fallback]"  # then backup


def test_gemini_primary_retry_succeeds_without_fallback(monkeypatch):
    # A single 429 blip: the bounded primary retry recovers, the fallback model is never touched.
    import urllib.error
    calls = []

    def fake_once(model, key, prompt, label=""):
        calls.append((model, label))
        if len(calls) == 1:
            raise urllib.error.HTTPError("u", 503, "UNAVAILABLE", {}, None)
        return '{"markdown": "ok"}'

    monkeypatch.setattr(gemini, "_gemini_once", fake_once)
    monkeypatch.setattr(gemini, "_PRIMARY_RETRY_BACKOFF_S", 0)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "primary")
    monkeypatch.delenv("SOTTO_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("SOTTO_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    assert gemini.call_gemini("p", {}) == '{"markdown": "ok"}'
    assert len(calls) == 2 and calls[1][1] == " [retry]"


def test_gemini_fallback_defaults_to_flash_preview(monkeypatch):
    # SOTTO_FALLBACK_MODEL unset → the fallback defaults to gemini-3-flash-preview (the model
    # metrics.py prices as "the brief's automatic fallback").
    import urllib.error
    calls = []

    def fake_once(model, key, prompt, label=""):
        calls.append((model, label))
        if len(calls) <= 2:
            raise urllib.error.HTTPError("u", 429, "RESOURCE_EXHAUSTED", {}, None)
        return '{"markdown": "ok"}'

    monkeypatch.setattr(gemini, "_gemini_once", fake_once)
    monkeypatch.setattr(gemini, "_PRIMARY_RETRY_BACKOFF_S", 0)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "primary")
    monkeypatch.delenv("SOTTO_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("SOTTO_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    assert gemini.call_gemini("p", {}) == '{"markdown": "ok"}'
    assert calls[2] == ("gemini-3-flash-preview", " [fallback]")


def test_gemini_empty_fallback_env_disables_fallback_reraises(monkeypatch):
    # SOTTO_FALLBACK_MODEL="" is the explicit off switch: primary + one retry, then re-raise.
    import urllib.error
    calls = []

    def fake_once(model, key, prompt, label=""):
        calls.append(model)
        raise urllib.error.HTTPError("u", 429, "RESOURCE_EXHAUSTED", {}, None)

    monkeypatch.setattr(gemini, "_gemini_once", fake_once)
    monkeypatch.setattr(gemini, "_PRIMARY_RETRY_BACKOFF_S", 0)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "primary")
    monkeypatch.setenv("SOTTO_FALLBACK_MODEL", "")
    monkeypatch.delenv("SOTTO_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    import pytest
    with pytest.raises(urllib.error.HTTPError):
        gemini.call_gemini("p", {})
    assert len(calls) == 2  # no fallback attempt


def test_user_local_date_resolves_iana_zone_not_utc(monkeypatch):
    # The off-by-one date bug: a fixed-offset parser returned UTC for IANA zone names, so an evening
    # brief in a behind-UTC zone showed the next day. An IANA name must resolve to the local day.
    import datetime
    utc_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    la_date = cb._user_local_date("America/Los_Angeles")   # always behind UTC (PST/PDT)
    # LA's date is the same or one earlier than UTC's — never the same string blindly returned for UTC.
    assert la_date <= utc_date
    # And it must equal the actual LA wall-clock date (DST-aware via zoneinfo), not a 0-offset fallback.
    from zoneinfo import ZoneInfo
    expected = datetime.datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    assert la_date == expected


def test_tz_falls_back_to_env_timezone(monkeypatch):
    # With no userTimezone and no event offsets, the brief must use SOTTO_TIMEZONE, not UTC.
    monkeypatch.setenv("SOTTO_TIMEZONE", "America/Los_Angeles")
    assert timeutil._env_tz() == "America/Los_Angeles"
    assert cb._user_tz_offset([]) == "America/Los_Angeles"   # env fallback when no events carry an offset
    import datetime
    from zoneinfo import ZoneInfo
    expected = datetime.datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    assert cb._user_local_date(timeutil._env_tz()) == expected


def _write_prefs(tmp_path, explicit):
    cfg = {"explicit": explicit}
    (tmp_path / "preferences.json").write_text(json.dumps(cfg))


def test_muted_sender_dropped_from_brief(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _write_prefs(tmp_path, {"mute_senders": ["@news.example.com"], "mute_people": [],
                            "mute_sections": [], "tone_notes": []})
    inputs = {"type": "morning", "first_run": False, "google": {"events": [], "emails": [
        {"from": "Daily Digest <digest@news.example.com>", "subject": "Your newsletter", "snippet": "buy now"},
        {"from": "Jane Real <jane@acme.com>", "subject": "Re: the deal", "snippet": "can you confirm?"},
    ]}, "local": {}}
    p = cb.build_prompt(cb._load_prompt(), inputs)
    assert "the deal" in p                      # the real email survives
    assert "Your newsletter" not in p           # the muted sender is gone


def test_muted_person_removed_from_attention_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _write_prefs(tmp_path, {"mute_senders": [], "mute_people": ["Bob"],
                            "mute_sections": [], "tone_notes": ["keep it terse"]})
    # seed a relationship_state the brief would otherwise surface
    kn = tmp_path / "knowledge"; kn.mkdir()
    (kn / "relationship_state.json").write_text(json.dumps({"attention_queue": [
        {"display_name": "Bob", "queue_type": "losing_touch", "reason": "going quiet"},
        {"display_name": "Maria", "queue_type": "waiting_on_you", "reason": "waiting 4 days"}],
        "relationship_insights": []}))
    p = cb.build_prompt(cb._load_prompt(), {"type": "morning", "first_run": False,
                                            "google": {"events": []}, "local": {}})
    # Bob is dropped from the attention queue (his reason is gone); Maria's stays. Bob's NAME still
    # appears once — in the "do not surface" instruction we restate so the model can't re-add him.
    assert "waiting 4 days" in p and "going quiet" not in p
    assert "Do NOT surface or flag these people anywhere in the brief: Bob" in p
    assert "keep it terse" in p                 # tone note surfaced to the model


def test_a_muted_persons_thread_never_reaches_the_model(tmp_path, monkeypatch):
    """"Mute Bob" was machinery for email and prose for iMessage: his texts still reached the model,
    which was merely asked not to mention him. A muted person is removed from the data. Groups he
    is in stay — a mute is a person, not a room."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _write_prefs(tmp_path, {"mute_senders": [], "mute_people": ["Bob Lee"],
                            "mute_sections": [], "tone_notes": []})
    local = {"contacts": [{"name": "Bob Lee", "phones": ["+14155550001"]},
                          {"name": "Maria Ruiz", "phones": ["+14155550002"]}],
             "imessage": [
                 {"handle": "+14155550001", "is_from_me": False, "timestamp": "2026-06-24 08:00:00",
                  "text": "can you look at the bob-only thread today?", "is_group_chat": False},
                 {"handle": "+14155550002", "is_from_me": False, "timestamp": "2026-06-24 08:05:00",
                  "text": "maria here — the venue moved to thursday", "is_group_chat": False},
                 {"handle": "+14155550001", "is_from_me": False, "timestamp": "2026-06-24 08:10:00",
                  "text": "group note from bob about the offsite", "is_group_chat": True,
                  "chat_guid": "g-offsite", "group_name": "Offsite crew"}]}
    p = cb.build_prompt(cb._load_prompt(), {"type": "morning", "first_run": False,
                                            "google": {"events": []}, "local": local})
    assert "bob-only thread" not in p and "venue moved to thursday" in p
    assert "group note from bob" in p                    # the group thread is not his to mute


# ── debts code can see are minted by code ────────────────────────────────────────────────────────

def _stale(tid="t-victor", to="Victor Yeung <victor@acme.com>", email="victor@acme.com",
           subject="Re: allocation", days=4, snippet="sending the revised deck — thoughts on the allocation?"):
    return {"threadId": tid, "to": to, "toEmail": email, "subject": subject, "daysSinceSent": days,
            "sentDate": f"2026-08-{31 - days:02d}T09:00:00Z", "snippet": snippet}


def test_stale_sent_threads_are_candidates_not_automatic_debts(tmp_path, monkeypatch):
    """Silence identifies threads worth considering; it cannot establish who owes what."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    local = {"contacts": [{"name": "Victor Yeung", "emails": ["victor@acme.com"], "phones": []},
                          {"name": "Dana Roe", "emails": ["dana@acme.com"], "phones": []},
                          {"name": "Ops", "emails": ["noreply@acme.com"], "phones": []}]}
    inputs = {"type": "morning", "local": local, "google": {"events": [], "staleThreads": [
        _stale(),
        _stale(tid="t-stranger", to="cold@pitch.io", email="cold@pitch.io"),
        _stale(tid="t-noreply", to="noreply@acme.com", email="noreply@acme.com"),
        _stale(tid="t-intro", to="Dana Roe <dana@acme.com>", email="dana@acme.com",
               subject="Intro: Dana <> Priya"),
        _stale(tid="t-thanks", to="Dana Roe <dana@acme.com>", email="dana@acme.com", snippet="thanks!"),
        # a calendar RSVP your Gmail sent to the organizer is not a letter anybody answers
        _stale(tid="t-rsvp", to="Team Acme <team@acme.com>", email="team@acme.com",
               subject="Accepted: Morning Team Video Sync @ Tue Sep 8, 2026 9:30am - 10am (PDT) (Team Acme)",
               snippet="Dana Roe has accepted this invitation. Morning Team Video Sync"),
        _stale(tid="t-invite", to="Dana Roe <dana@acme.com>", email="dana@acme.com",
               subject="Updated invitation: Board prep @ Wed Sep 9 (dana@acme.com)"),
        # …but a chase the user WROTE with a calendar-looking word is still a letter
        _stale(tid="t-reminder", to="Dana Roe <dana@acme.com>", email="dana@acme.com",
               subject="Reminder: allocation numbers by Friday?"),
    ]}}
    local["contacts"].append({"name": "Team Acme", "emails": ["team@acme.com"], "phones": []})
    norm = cb._normalize_local(inputs)
    assert [t["threadId"] for t in norm["stale_threads"]] == [
        "t-victor", "t-intro", "t-thanks", "t-reminder"]
    assert not hasattr(cb, "_stale_debt_actions")
    # The prompt gives the model source context and explicitly leaves the debt decision open.
    p = cb.build_prompt(cb._load_prompt(), inputs)
    assert "Emails you sent that nobody answered" in p
    assert "Silence alone does not establish a debt" in p
    # A thread the ledger already tracks is not presented as a second opening candidate.
    tracked = dict(norm, action_ledger=[{"status": "waiting", "action_type": "waiting_on",
                                         "contact_name": "Victor Yeung", "source_thread_id": "t-victor",
                                         "created_at": "2026-08-27", "summary": "the allocation"}])
    assert "Thread ID: t-victor" not in cb._format_stale_threads(tracked)


def test_minted_actions_never_displace_the_models_row_for_the_same_debt():
    model = [{"type": "reply", "channel": "gmail", "contactName": "Victor", "emailThreadId": "t-victor",
              "contactIdentifier": "victor@acme.com"}]
    minted = [{"type": "waiting_on", "emailThreadId": "t-victor", "contactIdentifier": "victor@acme.com"},
              {"type": "waiting_on", "emailThreadId": "t-dana", "contactIdentifier": "dana@acme.com"},
              {"type": "rsvp", "contactIdentifier": "ev9", "emailThreadId": ""}]
    out = cb._merge_deterministic_actions(model, minted)
    assert [a.get("emailThreadId") or a["contactIdentifier"] for a in out] == ["t-victor", "t-dana", "ev9"]
    assert out[0]["type"] == "reply"


def test_a_meeting_you_declined_is_not_on_your_day_and_an_unanswered_invite_is_an_ask(monkeypatch):
    """Declined: gone from the calendar the model sees, from research, and from the prep nudge.
    Unanswered within 48h with somebody else on it: an `rsvp` ask, minted by code."""
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    soon = (now + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    later = (now + timedelta(hours=80)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    me = {"email": "me@example.com", "self": True}
    events = [
        {"id": "ev-declined", "summary": "Declined pitch", "start": soon, "my_response": "declined",
         "attendees": [dict(me, responseStatus="declined"), {"email": "vc@fund.com", "displayName": "VC"}]},
        {"id": "ev-unanswered", "summary": "Coffee with Priya", "start": soon, "my_response": "needsaction",
         "attendees": [dict(me, responseStatus="needsAction"), {"email": "priya@acme.com"}]},
        {"id": "ev-far", "summary": "Far invite", "start": later, "my_response": "needsaction",
         "attendees": [dict(me, responseStatus="needsAction"), {"email": "x@acme.com"}]},
        {"id": "ev-solo", "summary": "Focus block", "start": soon, "my_response": "needsaction",
         "attendees": [dict(me, responseStatus="needsAction")]},
        {"id": "ev-ok", "summary": "Accepted sync", "start": soon, "my_response": "accepted",
         "attendees": [dict(me, responseStatus="accepted"), {"email": "y@acme.com"}]},
    ]
    inputs = {"type": "morning", "local": {}, "google": {"events": events, "userEmail": "me@example.com"}}
    p = cb.build_prompt(cb._load_prompt(), inputs)
    assert "Declined pitch" not in p and "Coffee with Priya" in p
    assert "you haven't answered this invite yet" in p
    rsvp = cb._rsvp_actions(inputs, now)
    assert [a["contactIdentifier"] for a in rsvp] == ["ev-unanswered"]
    assert rsvp[0]["type"] == "rsvp" and rsvp[0]["channel"] == "calendar"
    assert "haven't answered the invite for Coffee with Priya" in rsvp[0]["contextSummary"]

    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 4, 9, 0, tzinfo=tz)
    monkeypatch.setattr(cb, "datetime", _Fixed)
    assert "vc@fund.com" not in [x["email"] for x in cb.select_attendees_for_research(inputs)]







def test_first_run_note_only_on_first_brief(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    base = {"type": "morning", "google": {"events": []}, "local": {}}
    # No delivered marker yet → first run → the one-time welcome/capability framing is injected.
    p1 = cb.build_prompt(cb._load_prompt(), base)
    assert "FIRST BRIEF" in p1 and "what they can ask next" in p1
    # Once a brief has been delivered, the framing disappears (never repeats).
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-06-01.morning.delivered").write_text("")
    p2 = cb.build_prompt(cb._load_prompt(), base)
    assert "FIRST BRIEF" not in p2
    # An explicit first_run flag overrides auto-detection either way.
    assert "FIRST BRIEF" in cb.build_prompt(cb._load_prompt(), {**base, "first_run": True})


def test_first_run_coverage_line_names_missing_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    inputs = {"type": "morning", "first_run": True,
              "google": {"events": [], "emails": []},
              "local": {"imessage": [], "source_status": {"whatsapp": "needs_fda"}}}
    p = cb.build_prompt(cb._load_prompt(), inputs)
    assert "WhatsApp" in p and "full picture" in p
    assert "Granola" in p                                     # absent → named as optional-to-link
    # The "seeing" clause names only what actually had data — not "email and calendar" when one is empty.
    email_only = cb._coverage_line({"granola_meetings": [{"title": "Acme sync"}]}, {}, [], [{"id": "m1"}])
    assert "your email" in email_only and "calendar" not in email_only
    assert "Granola meeting notes" in email_only              # present → counted as seen
    cal_only = cb._coverage_line({}, {}, [{"id": "ev1"}], [])
    assert "your calendar" in cal_only and "your email" not in cal_only
    both = cb._coverage_line({}, {}, [{"id": "ev1"}], [{"id": "m1"}])
    assert "your email and calendar" in both
    # A Bridge that was NEVER connected reports no source_status at all — the common day-0 case,
    # and the one this line exists to name. It used to fall through and never mention the Mac.
    assert "iMessage" in both and "WhatsApp" in both and "full picture" in both


def test_configured_tz_reads_volume_settings_when_env_unset(tmp_path, monkeypatch):
    # The setup wizard writes the browser-detected zone to the volume; with no Railway var, the brief
    # must still compute the user's local day from it (so SOTTO_TIMEZONE becomes OPTIONAL).
    monkeypatch.delenv("SOTTO_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert cb.configured_tz() == ""                              # nothing set anywhere yet
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "settings.json").write_text('{"timezone": "America/Los_Angeles"}')
    assert cb.configured_tz() == "America/Los_Angeles"
    # but an explicit env var always wins over the volume file
    monkeypatch.setenv("SOTTO_TIMEZONE", "Europe/Paris")
    assert cb.configured_tz() == "Europe/Paris"


def test_configured_user_email_prefers_env_then_the_derived_google_account(tmp_path, monkeypatch):
    """Sotto learns your address from the account you connected — the Google connect derives it into
    settings.json and SOTTO_USER_EMAIL is only an override. ONE helper, so every fallback site in the
    brief (attendee research, the calendar-link default, the followup handoff) resolves it the same."""
    monkeypatch.delenv("SOTTO_USER_EMAIL", raising=False)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert cb.configured_user_email() == ""                      # nothing set anywhere yet
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "settings.json").write_text('{"google_account_email": "Me@Acme.com"}')
    assert cb.configured_user_email() == "me@acme.com"           # derived at connect, lowercased
    monkeypatch.setenv("SOTTO_USER_EMAIL", "Override@Acme.com")
    assert cb.configured_user_email() == "override@acme.com"     # the env var still wins


def test_user_local_date_accepts_fixed_offset():
    # The legacy '+HH:MM' path still works (events that carry an explicit offset).
    import datetime
    expected = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)).strftime("%Y-%m-%d")
    assert cb._user_local_date("+09:00") == expected


def test_correlate_connects_signals_to_email_senders():
    local = {
        "chrome_history": [{"domain": "acme.com", "visit_count": 4, "top_titles": ["Acme pricing"]},
                           {"domain": "google.com", "visit_count": 9}],   # hosting → excluded
        "safari_history": [{"domain": "oneoff.com", "visit_count": 1}],    # <2 visits → excluded
        "recent_files": [{"filename": "Q1 Deck.pptx", "source_url": "https://acme.com/d", "status": "unopened"}],
        "granola_meetings": [{"title": "Acme sync", "date": "2026-06-20",
                              "attendee_emails": ["jane@acme.com"], "ai_summary": "discussed pricing"}],
    }
    emails = [{"from": "Jane Doe <jane@acme.com>", "subject": "pricing follow-up"},
              {"from": "promo@google.com", "subject": "ad"}]            # consumer/hosting sender excluded
    c = cb._correlate_signals(local, emails, local["granola_meetings"])
    # researched their company → boost the sender
    assert any(b["email"] == "jane@acme.com" and b["domain"] == "acme.com" for b in c["signal_boosts"])
    # downloaded from their domain → file match (high confidence)
    assert c["file_matches"] and c["file_matches"][0]["event"] == "Jane Doe" and c["file_matches"][0]["confidence"] == "high"
    # met them recently → granola context
    assert c["granola_context"] and c["granola_context"][0]["person"] == "Jane Doe"
    # google.com (hosting) was NOT treated as research
    assert all(b["domain"] != "google.com" for b in c["signal_boosts"])


def test_correlate_rejects_ambiguous_file_domain():
    # two senders share corp.com → a file from corp.com is NOT attributed (one-to-one only)
    local = {"recent_files": [{"filename": "x.pdf", "source_url": "https://corp.com/x", "status": "opened"}]}
    emails = [{"from": "a <a@corp.com>"}, {"from": "b <b@corp.com>"}]
    assert cb._correlate_signals(local, emails, [])["file_matches"] == []


def test_unwrap_local_handles_mcp_result_wrappers():
    ld = {"imessage": [{"handle": "+1", "text": "hi"}], "contacts": []}
    # clean LocalData passes through
    assert textutil.unwrap_tool_result(ld) is ld
    # {result: {...}} wrapper (what the hermes-results file holds)
    assert textutil.unwrap_tool_result({"result": ld}) == ld
    # MCP tool result: structuredContent
    assert textutil.unwrap_tool_result({"structuredContent": ld, "isError": False}) == ld
    # MCP tool result: content[0].text JSON string
    import json as _j
    assert textutil.unwrap_tool_result({"content": [{"type": "text", "text": _j.dumps(ld)}]}) == ld
    # nested: result → structuredContent
    assert textutil.unwrap_tool_result({"result": {"structuredContent": ld}}) == ld
    # garbage → {}
    assert textutil.unwrap_tool_result("nope") == {} and textutil.unwrap_tool_result({"junk": 1}) == {"junk": 1}


# --- Gemini response guarding (blocked / truncated 200s) ----------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_gemini_blocked_response_raises_diagnosable_error(tmp_path, monkeypatch):
    # A safety-blocked prompt is a 200 with promptFeedback and NO candidates — the raw chained
    # index died with an opaque KeyError after the call was billed.
    import urllib.request
    import pytest
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))   # keep _diag's log file out of /data
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=0: _FakeResp({"promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(RuntimeError) as ei:
        gemini._gemini_once("test-model", "k", "p")
    assert "SAFETY" in str(ei.value)


def test_gemini_max_tokens_truncation_raises_with_finish_reason(tmp_path, monkeypatch):
    # MAX_TOKENS truncation returns a candidate whose content has no parts.
    import urllib.request
    import pytest
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=0: _FakeResp(
                            {"candidates": [{"content": {"role": "model"}, "finishReason": "MAX_TOKENS"}]}))
    with pytest.raises(RuntimeError) as ei:
        gemini._gemini_once("test-model", "k", "p")
    assert "MAX_TOKENS" in str(ei.value)


def test_gemini_content_block_does_not_burn_fallback(monkeypatch):
    # RuntimeError is not in _is_retryable's transient set: a content block would fail on the
    # backup model too, so call_gemini must re-raise instead of retrying.
    import pytest
    calls = []

    def fake_once(model, key, prompt, label=""):
        calls.append(model)
        raise RuntimeError("Gemini test-model returned no candidates (blockReason: SAFETY)")

    monkeypatch.setattr(gemini, "_gemini_once", fake_once)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "primary")
    monkeypatch.setenv("SOTTO_FALLBACK_MODEL", "gemini-2.5-pro")
    monkeypatch.setenv("SOTTO_FALLBACK_API_KEY", "backup")
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    with pytest.raises(RuntimeError):
        gemini.call_gemini("p", {})
    assert len(calls) == 1


# --- default model ------------------------------------------------------------

def test_default_model_is_gemini_36_flash(monkeypatch):
    calls = []

    def fake_once(model, key, prompt, label=""):
        calls.append(model)
        return '{"markdown": "ok"}'

    monkeypatch.setattr(gemini, "_gemini_once", fake_once)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    for var in ("SOTTO_GEMINI_MODEL", "SOTTO_LLM_STUB", "SOTTO_FALLBACK_MODEL", "SOTTO_FALLBACK_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    gemini.call_gemini("p", {})
    assert calls == ["gemini-3.8-flash"]


# --- evening followup merge (Sprint 0 #4) --------------------------------------

def _iso_hours_ago(h):
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _body(markdown: str) -> str:
    """The brief WITHOUT its code-owned greeting line ("# Good morning — Saturday, September 6"):
    what the model wrote, plus the code-appended blocks — which is what these tests pin."""
    md = markdown or ""
    return md.split("\n\n", 1)[1] if md.startswith("# Good ") and "\n\n" in md else md


def _followup_capable_llm(seen, calls):
    def fake_llm(prompt, inputs, system=None, schema=None):
        if inputs.get("_followup"):
            calls["followup"] += 1
            return json.dumps({
                "followup_markdown": "**Acme Sync** — you owe Dana the deck.",
                "commitments": [{"meeting": "Acme Sync", "meeting_id": "m-acme",
                                 "owner": "you", "owner_is_user": True,
                                 "what": "send the deck", "to_email": "dana@acme.com",
                                 "source_snippet": "you: I'll send the deck"}],
                "drafts": [{"to_name": "Dana", "to_email": "dana@acme.com", "channel": "email",
                            "subject": "Deck", "body": "Here it is."}]})
        seen["prompt"] = prompt
        return json.dumps({"brief_markdown": "# Evening", "actions": []})
    return fake_llm


def test_evening_brief_reuses_shared_persisted_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    seen, calls = {}, {"followup": 0}
    inputs = {"type": "evening", "google": {"events": [], "userEmail": "me@x.com"},
              "granola": [{"meeting_id": "m-acme", "title": "Acme Sync",
                           "date": _iso_hours_ago(2),
                           "transcript": "you: I'll send the deck",
                           "attendee_emails": ["dana@acme.com"]}],
              "local": {}}
    out = cb.compose(inputs, llm=_followup_capable_llm(seen, calls))
    assert calls["followup"] == 1
    assert out["brief_markdown"].startswith("# Good ") and "\n\n# Evening" in out["brief_markdown"]
    p = seen["prompt"]
    assert "Today's Meeting Follow-Ups" in p
    assert "Commitments confirmed in the action ledger: 1" in p
    assert (tmp_path / "knowledge/continuity").exists()


def test_evening_shared_capture_preserves_standing_rule_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    seen = {}
    def llm(prompt, inputs, system=None, schema=None):
        if inputs.get('_followup'):
            return json.dumps({'followup_markdown': '', 'commitments': [], 'drafts': [],
                               'procedural_candidates': ['Always send a recap after investor calls.']})
        seen['prompt'] = prompt
        return json.dumps({'brief_markdown': '# Evening', 'actions': []})
    inputs = {'type': 'evening', 'google': {'events': [], 'userEmail': 'me@x.com'},
              'granola': [{'meeting_id': 'm-rule', 'title': 'Investor call',
                           'date': _iso_hours_ago(2),
                           'transcript': 'Me: Always send a recap after investor calls.',
                           'attendee_emails': ['investor@example.com']}], 'local': {}}
    out = cb.compose(inputs, llm=llm)
    assert 'Always send a recap after investor calls.' in out['brief_markdown']


def test_morning_brief_never_runs_followup(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    seen, calls = {}, {"followup": 0}
    inputs = {"type": "morning", "google": {"events": []},
              "granola": [{"title": "Acme Sync", "date": _iso_hours_ago(2), "transcript": "t"}],
              "local": {}}
    cb.compose(inputs, llm=_followup_capable_llm(seen, calls))
    assert calls["followup"] == 0
    assert "Today's Meeting Follow-Ups" not in seen["prompt"]


def test_evening_followup_failure_never_blocks_brief(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    def fake_llm(prompt, inputs, system=None, schema=None):
        if inputs.get("_followup"):
            raise RuntimeError("gemini down")
        return json.dumps({"brief_markdown": "# Evening survives", "actions": []})
    inputs = {"type": "evening", "google": {"events": []},
              "granola": [{"title": "Sync", "date": _iso_hours_ago(1), "transcript": "t"}],
              "local": {}}
    out = cb.compose(inputs, llm=fake_llm)
    assert _body(out["brief_markdown"]) == "# Evening survives"


def test_evening_no_ended_meetings_adds_no_block(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    seen, calls = {}, {"followup": 0}
    out = cb.compose({"type": "evening", "google": {"events": []}, "granola": [], "local": {}},
                     llm=_followup_capable_llm(seen, calls))
    assert calls["followup"] == 0                      # short-circuits before any LLM call
    assert "Today's Meeting Follow-Ups" not in seen["prompt"]
    assert _body(out["brief_markdown"]) == "# Evening"


# ── Already Nudged Today: the surfaced-ledger reader (root of the double-tell) ──

def _surfaced(tmp_path, rows):
    d = tmp_path / "events"
    d.mkdir(parents=True, exist_ok=True)
    for i, row in enumerate(rows):
        row.setdefault("decision_id", f"decision-{i}")
    (d / "surfaced.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    delivered = [{"ts": r["ts"], "status": "delivered", "decision_ids": [r["decision_id"]]}
                 for r in rows if r.get("verdict") in ("agent", "promoted")]
    (d / "delivery.jsonl").write_text("".join(json.dumps(r) + "\n" for r in delivered))


def _row(ts, sender="Sarah Chen", verdict="agent", cls="urgent", reason="Sarah's ask about the deck"):
    return {"ts": ts, "sender": sender, "channel": "imessage", "verdict": verdict,
            "reason": reason, "class": cls}


def _today_at(hhmm):
    """A UTC stamp on the user's local today (tests pin the zone to +00:00, so the dates agree)."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT") + hhmm + ":00Z"


def test_evening_renders_todays_delivered_nudges(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    _surfaced(tmp_path, [_row(_today_at("15:05")),
                         _row(_today_at("16:20"), sender="Ben", cls="scheduling_ask",
                              reason="wants Thursday"),
                         _row(_today_at("14:00"), sender="Noise", verdict="queue"),  # not delivered
                         _row("2020-01-01T09:00:00Z", sender="Ancient")])            # not today
    seen = {}

    def fake_llm(prompt, inputs, system=None, schema=None):
        seen["prompt"] = prompt
        return json.dumps({"brief_markdown": "# Evening", "actions": []})

    cb.compose({"type": "evening", "google": {"events": []}, "granola": [], "local": {}}, llm=fake_llm)
    p = seen["prompt"]
    assert "## Already Nudged Today" in p
    assert "Sarah Chen [urgent] — Sarah's ask about the deck" in p
    assert "Ben [scheduling_ask]" in p
    assert "Noise" not in p and "Ancient" not in p       # queued verdicts and other days stay out


def test_a_morning_brief_reads_the_surfaced_ledger_whenever_it_has_rows(monkeypatch, tmp_path):
    # The 06:30 cron brief finds nothing; a wake-path brief at 9am finds the 7:15 chase and must
    # not re-tell it as news. So the block is gated on rows existing, never on the brief type.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    seen = {}

    def fake_llm(prompt, inputs, system=None, schema=None):
        seen["prompt"] = prompt
        return json.dumps({"brief_markdown": "# Morning", "actions": []})

    cb.compose({"type": "morning", "google": {"events": []}, "local": {}}, llm=fake_llm)
    assert "## Already Nudged Today" not in seen["prompt"]      # no nudges yet today
    _surfaced(tmp_path, [_row(_today_at("07:15"))])
    cb.compose({"type": "morning", "google": {"events": []}, "local": {}}, llm=fake_llm)
    assert "## Already Nudged Today" in seen["prompt"]
    assert "Sarah Chen [urgent] — Sarah's ask about the deck" in seen["prompt"]


def test_already_nudged_missing_or_corrupt_ledger_renders_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert cb._already_nudged_block("+00:00") == ""          # no file at all
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "surfaced.jsonl").write_text("{not json\n" + json.dumps(
        {"verdict": "agent", "ts": "nonsense", "sender": "X"}) + "\n")
    assert cb._already_nudged_block("+00:00") == ""          # unparseable rows skipped


def test_already_nudged_is_bounded_and_time_ordered(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    import datetime as _dt
    base = _dt.datetime.now(_dt.timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    _surfaced(tmp_path, [_row((base + _dt.timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              sender=f"P{i:02d}") for i in range(25)])
    rows = cb._load_surfaced_nudges(base.strftime("%Y-%m-%d"), _dt.timezone.utc)
    assert len(rows) == cb.SURFACED_NUDGE_MAX                     # bounded — never a second brief
    assert [r["sender"] for r in rows] == sorted(r["sender"] for r in rows)   # oldest first
    assert rows[0]["time"].endswith(("am", "pm"))


def test_already_nudged_reads_the_writers_real_rows(monkeypatch, tmp_path):
    """The reader is pinned to the WRITER, not to a hand-typed row: triage_event._record_surfaced
    produces the line, compose_brief parses it. A field rename on either side fails here."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    import triage_event as te                       # conftest puts event-triage/scripts on sys.path
    event = {"source": "imessage", "handle": "+15551234567", "text": "can you send the deck?"}
    te._record_surfaced("agent", "urgent", "Sarah is waiting on the deck", "Sarah Chen", event,
                        decision_id="delivered-sarah")
    te._record_surfaced("queue", "quiet", "quiet hours", "Ben", event,
                        decision_id="queued-ben")
    (tmp_path / "events" / "delivery.jsonl").write_text(json.dumps({
        "ts": _today_at("12:00"), "status": "delivered",
        "decision_ids": ["delivered-sarah"]}) + "\n")
    rows = cb._load_surfaced_nudges(cb._user_local_date("+00:00"), cb._resolve_tz("+00:00"))
    assert [(r["sender"], r["cls"]) for r in rows] == [("Sarah Chen", "urgent")]
    assert "waiting on the deck" in rows[0]["reason"]
    # a proactive nudge's sender is its prose line; the row carries the PERSON it was about, and
    # that is the name the validator's never-tell-you-twice rule measures
    chase = {"source": "proactive", "kind": "chase", "person": "Maya Chen",
             "text": "still no word from Maya Chen on the contract"}
    te._record_surfaced("agent", "chase", "still no word from Maya Chen on the contract",
                        "still no word from Maya Chen on the contract", chase, decision_id="chase-maya")
    with open(tmp_path / "events" / "delivery.jsonl", "a") as f:
        f.write(json.dumps({"ts": _today_at("12:10"), "status": "delivered",
                            "decision_ids": ["chase-maya"]}) + "\n")
    assert cb._already_nudged_senders({"type": "evening", "google": {"userTimezone": "+00:00"},
                                       "local": {}}) == ["Sarah Chen", "Maya Chen"]


def test_failed_delivery_is_never_reported_as_already_nudged(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ts = _today_at("12:00")
    _surfaced(tmp_path, [_row(ts)])
    (tmp_path / "events" / "delivery.jsonl").write_text(json.dumps({
        "ts": ts, "status": "failed", "decision_ids": ["decision-0"]}) + "\n")
    assert cb._already_nudged_block("+00:00") == ""


def test_already_nudged_placeholder_contract(monkeypatch, tmp_path):
    """Renders at {{already_nudged}} when the template has it, exactly once — and rides the
    reconciliation slot on an older template, still exactly once."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    _surfaced(tmp_path, [_row(_today_at("15:05"))])
    inputs = {"type": "evening", "google": {"events": []}, "local": {}}
    with_ph = cb.build_prompt("{{brief_type}}\n{{already_nudged}}\n{{reconciliation}}END", inputs)
    assert with_ph.count("Already Nudged Today") == 1
    without = cb.build_prompt("{{brief_type}}\n{{reconciliation}}END", inputs)
    assert without.count("Already Nudged Today") == 1


def test_followup_context_placeholder_contract():
    """The {{followup_context}} placeholder contract: when the template carries the placeholder, the
    block renders THERE and is NOT double-injected into the reconciliation path."""
    template = "## Context\nBrief type: {{brief_type}}\n{{followup_context}}\n{{reconciliation}}END"
    inputs = {"type": "evening", "google": {"events": []}, "local": {},
              "_followup_context": "## Today's Meeting Follow-Ups (PRE-COMPUTED)\n- deck to Dana"}
    p = cb.build_prompt(template, inputs)
    assert p.count("deck to Dana") == 1
    # and without the placeholder, it rides the reconciliation slot — still exactly once
    template2 = "## Context\nBrief type: {{brief_type}}\n{{reconciliation}}END"
    p2 = cb.build_prompt(template2, inputs)
    assert p2.count("deck to Dana") == 1
    # morning briefs never render it, placeholder or not
    p3 = cb.build_prompt(template, {**inputs, "type": "morning"})
    assert "deck to Dana" not in p3


# --- brief validator wiring (Sprint 1 #6) --------------------------------------

def test_validator_violations_feed_critic_and_force_revise(monkeypatch):
    monkeypatch.setenv("SOTTO_CRITIC", "always")
    seen = {}
    bad_brief = "## Needs Attention Now\n**Sarah Chen** reached out about the deal."
    fixed_brief = ("## Needs Attention Now\n**Sarah Chen**<!--id:sarah@x.com|ch:email--> "
                   "wants the deal decision.")

    def fake_llm(prompt, inputs, system=None, schema=None):
        if inputs.get("_critic"):
            seen["critic_prompt"] = prompt
            return json.dumps({"patches": [], "score": 90, "summary": "ok"})
        if inputs.get("_revise"):
            seen["revise_prompt"] = prompt
            return json.dumps({"brief_markdown": fixed_brief, "actions": []})
        return json.dumps({"brief_markdown": bad_brief, "actions": []})

    # Sarah is a real correspondent, so the revised brief's marker survives (a marker for someone
    # the data never contained is refused — tests/test_tap_link_allowlist.py).
    google = {"emails": [{"threadId": "t1", "from": "Sarah Chen <sarah@x.com>", "subject": "deal"}]}
    out = cb.compose({"type": "morning", "google": google, "local": {}}, llm=fake_llm, critic=True)
    # violations were appended to the critic's user content …
    assert "AUTOMATED VALIDATOR VIOLATIONS" in seen["critic_prompt"]
    assert "banned-phrase: 'reached out'" in seen["critic_prompt"]
    assert "missing-marker" in seen["critic_prompt"]
    # … and force the revise pass even though the critic itself found nothing
    assert _body(out["brief_markdown"]) == fixed_brief
    assert out["_critic"]["actionable"] >= 2
    assert out["_critic"]["revision_reasons"] == {
        "critic": {"count": 0, "categories": {}},
        "validator": {"count": 2,
                      "categories": {"banned-phrase": 1, "missing-marker": 1}},
    }


def test_revision_reason_diagnostics_are_content_free_and_bounded(monkeypatch):
    monkeypatch.setenv("SOTTO_CRITIC", "always")

    def fake_llm(prompt, inputs, system=None, schema=None):
        if inputs.get("_critic"):
            return json.dumps({"patches": [
                {"type": "fix_attribution", "detail": "private detail", "severity": "critical"},
                {"type": "invented-freeform-type", "detail": "another detail", "severity": "moderate"},
            ], "score": 50, "summary": "repair"})
        return json.dumps({"brief_markdown": "## Needs Attention Now\nFixed.", "actions": []})

    out = cb.critique_and_revise(
        {"brief_markdown": "draft", "actions": []}, {}, llm=fake_llm,
        violations=["bad category with spaces: secret source text",
                    "dropped-open-loop: private obligation"], source_prompt="sources")
    assert out["_critic"]["revision_reasons"] == {
        "critic": {"count": 2, "categories": {"fix_attribution": 1, "other": 1}},
        "validator": {"count": 2, "categories": {"dropped-open-loop": 1, "other": 1}},
    }
    assert "private detail" not in json.dumps(out["_critic"]["revision_reasons"])


def test_validator_never_blocks_delivery(monkeypatch):
    # validator violations with the critic OFF: logged only, brief delivered unchanged
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    bad_brief = "## Needs Attention Now\n**Sarah Chen** reached out about the deal."

    def fake_llm(prompt, inputs, system=None, schema=None):
        assert not inputs.get("_critic") and not inputs.get("_revise")
        return json.dumps({"brief_markdown": bad_brief, "actions": []})

    out = cb.compose({"type": "morning", "google": {}, "local": {}}, llm=fake_llm, critic=True)
    assert _body(out["brief_markdown"]) == bad_brief


# --- the open-items contract: critic retry, then the deterministic backstop ----
# An open loop earns its own line only when it is overdue, due within 24h, or already chased without
# an answer. Everything else is ONE quiet count line and belongs to the proactive lane. The bug this
# closes: the owner's evening brief carried 14 "Still open" rows for ~7 real debts, several of them
# repeating what the narrative had already said.

def _open_ledger_inputs(hours=30, extra=None, **fields):
    urgent = {"contact_name": "Priya Raman", "status": "open", "action_type": "follow_up",
              "channel": "email", "summary": "Send the diligence memo",
              "chased_count": 1,                      # asked once, no answer → urgent
              "created_at": _iso_hours_ago(hours)}
    urgent.update(fields)
    return {"type": "morning", "google": {}, "local": {"action_ledger": [urgent] + (extra or [])}}


def test_still_open_backstop_appends_dropped_urgent_items(monkeypatch):
    monkeypatch.setenv("SOTTO_CRITIC", "off")            # no retry available → backstop is the guarantee
    dropped = ("## Needs Attention Now\n**Dana Wu**<!--id:dana@x.com|ch:email--> wants the LOI.\n\n"
               "## Filtered\n\n12 promotional emails\n")

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": dropped, "actions": []})

    out = cb.compose(_open_ledger_inputs(), llm=fake_llm, critic=True)
    body = out["brief_markdown"]
    assert "## Still open\n- **Priya Raman**: Send the diligence memo (open since " in body
    assert body.index("Still open") < body.index("## Filtered")   # before the last section, not after
    assert out["brief_text"].count("Priya Raman") == 1            # and it survives into the chat text


def test_a_quiet_pile_is_one_line_with_a_count_not_a_wall(monkeypatch):
    """Tonight's brief, in one test: 14 open loops of which 2 are urgent renders 2 lines and ONE
    count line — never fourteen rows."""
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    quiet = [{"contact_name": f"Person {i}", "status": "open", "action_type": "follow_up",
              "channel": "email", "summary": f"loop number {i}", "created_at": _iso_hours_ago(50)}
             for i in range(12)]
    urgent2 = {"contact_name": "Amy Wu", "status": "waiting", "action_type": "waiting_on",
               "channel": "email", "summary": "the signed side letter", "chased_count": 2,
               "created_at": _iso_hours_ago(96)}

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": "## Needs Attention Now\nA quiet morning.\n",
                           "actions": []})

    body = cb.compose(_open_ledger_inputs(extra=[urgent2] + quiet),
                      llm=fake_llm, critic=True)["brief_markdown"]
    lines = [l for l in body.split("## Still open\n", 1)[1].splitlines() if l.strip()]
    assert len(lines) == 3                                   # 2 urgent + 1 count line, nothing else
    assert lines[-1] == "- 12 other open loops. Ask me what's still open."
    assert "**Priya Raman**" in lines[0] and "**Amy Wu**" in lines[1]
    for i in range(12):
        assert f"loop number {i}" not in body               # the wall is gone


def test_one_line_per_person_however_many_debts(monkeypatch):
    """Spencer owed three things across three email threads and got three rows. One person, one
    line — naming him once, carrying all three asks, aged by the OLDEST."""
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    debts = [{"contact_name": "Spencer Schneier", "status": "open", "action_type": "follow_up",
              "channel": "email", "canonical_id": "c_abc123", "chased_count": 1,
              "summary": s, "created_at": _iso_hours_ago(h)}
             for s, h in (("send the deck", 72), ("the Onshore intro", 48), ("coffee next week", 60))]

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": "## Needs Attention Now\nQuiet.\n", "actions": []})

    body = cb.compose({"type": "morning", "google": {}, "local": {"action_ledger": debts}},
                      llm=fake_llm, critic=True)["brief_markdown"]
    lines = [l for l in body.split("## Still open\n", 1)[1].splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("- **Spencer Schneier**: send the deck; the Onshore intro; "
                               "coffee next week (open since ")
    assert body.count("Spencer Schneier") == 1


def test_a_nameless_row_renders_without_a_placeholder_noun(monkeypatch):
    """"**Open loop** —" was a name the brief invented for a row that had none. No name, no prefix."""
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    nameless = {"status": "open", "action_type": "follow_up", "channel": "email",
                "summary": "Return the Redwood term sheet", "chased_count": 1,
                "created_at": _iso_hours_ago(30)}

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": "## Needs Attention Now\nQuiet.\n", "actions": []})

    body = cb.compose({"type": "morning", "google": {}, "local": {"action_ledger": [nameless]}},
                      llm=fake_llm, critic=True)["brief_markdown"]
    assert "## Still open\n- Return the Redwood term sheet (open since " in body
    assert "Open loop" not in body


def test_backstop_never_repeats_what_the_brief_already_said(monkeypatch):
    """Tonight's FPV/Piston row: already in Should Handle Today, repeated verbatim below. A group
    label the brief printed is naming it — for the urgent line AND for the quiet count."""
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    group = {"contact_name": "FPV / Piston", "status": "waiting", "action_type": "waiting_on",
             "channel": "imessage", "chased_count": 1, "created_at": _iso_hours_ago(30),
             "summary": "asked for an intro to Insight Partners"}
    said = ("## Should Handle Today\n**FPV / Piston** wants the Insight Partners intro.\n\n"
            "**Priya Raman**<!--id:p@x.com|ch:email--> is waiting on the diligence memo."
            " <!--loop:priyaa-->\n")

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": said, "actions": []})

    body = cb.compose(_open_ledger_inputs(extra=[group]), llm=fake_llm, critic=True)["brief_markdown"]
    assert "Still open" not in body
    assert body.count("FPV / Piston") == 1 and body.count("Priya Raman") == 1


def test_backstop_stays_silent_when_the_brief_named_the_loop(monkeypatch):
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    handled = "## ✅ Already Handled\n**Priya Raman**<!--id:p@x.com|ch:email--> has the memo now.\n"

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": handled, "actions": []})

    # A handled claim without verified completion cannot hide the still-open ledger task.
    assert "Still open" in cb.compose(_open_ledger_inputs(), llm=fake_llm, critic=True)["brief_markdown"]


def test_dropped_loop_goes_to_the_critic_first(monkeypatch):
    """The backstop is the LAST resort: the violation rides the normal critic/revise path first, and a
    revision that surfaces the loop leaves nothing for the backstop to append."""
    monkeypatch.setenv("SOTTO_CRITIC", "always")
    seen = {}
    fixed = ("## Should Handle Today\n**Priya Raman**<!--id:p@x.com|ch:email--> is still waiting on "
             "the diligence memo, open two days. <!--loop:priyaa-->\n")

    def fake_llm(prompt, inputs, system=None, schema=None):
        if inputs.get("_critic"):
            seen["critic_prompt"] = prompt
            return json.dumps({"patches": [{"type": "add_item", "severity": "moderate", "detail": "Priya still needs the diligence memo; show that specific ask."}], "score": 80, "summary": "Unfulfilled task"})
        if inputs.get("_revise"):
            return json.dumps({"brief_markdown": fixed, "actions": []})
        return json.dumps({"brief_markdown": "## Filtered\n\n12 promotional emails\n", "actions": []})

    # The ledger row carries Priya's identifier — which is what lets the revision's tap marker for
    # her stand (an identifier no source carried is refused, allowlist rule).
    out = cb.compose(_open_ledger_inputs(contact_identifier="p@x.com"), llm=fake_llm, critic=True)
    assert "dropped-open-loop" in seen["critic_prompt"] and "Priya Raman" in seen["critic_prompt"]
    assert _body(out["brief_markdown"]) == fixed
    assert "Still open" not in out["brief_markdown"]


def test_a_non_urgent_loop_never_reaches_the_critic(monkeypatch):
    """The contract change on the model's side: a quiet loop the brief didn't name is not a defect,
    so it is not handed to the critic as one."""
    monkeypatch.setenv("SOTTO_CRITIC", "always")
    seen = {}

    def fake_llm(prompt, inputs, system=None, schema=None):
        if inputs.get("_critic"):
            seen["critic_prompt"] = prompt
            return json.dumps({"patches": [], "score": 90, "summary": "ok"})
        return json.dumps({"brief_markdown": "## Needs Attention Now\nQuiet.\n", "actions": []})

    quiet = {"contact_name": "Priya Raman", "status": "open", "action_type": "follow_up",
             "channel": "email", "summary": "Send the diligence memo",
             "created_at": _iso_hours_ago(30)}
    cb.compose({"type": "morning", "google": {}, "local": {"action_ledger": [quiet]}},
               llm=fake_llm, critic=True)
    assert "dropped-open-loop" not in seen["critic_prompt"]


# --- action_ledger populated from the continuity ledger -----------------------

def test_continuity_ledger_populates_action_ledger(tmp_path, monkeypatch):
    # Nothing else fills local["action_ledger"], so Open Commitments / Evening Accountability /
    # TRACKED OPEN LOOPS rendered empty every day until _normalize_local merged the ledger in.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda source: source == 'calls')
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True)
    (d / "a.md").write_text(
        "---\nanchor_key: k1\nstatus: open\naction_type: reply\ncontact_name: Sarah Chen\n"
        "channel: email\ncreated_at: '2026-06-20'\nsummary: Send the deck\n"
        "source_brief_at: '2026-06-23T07:00:00Z'\n"
        "source_refs:\n  - sourceType: calls\n    sourceId: call-1\n---\n")
    (d / "b.md").write_text(
        "---\nanchor_key: k2\nstatus: resolved\ncontact_name: Done Person\n---\n")
    local = cb._normalize_local({"local": {}})
    assert [a["contact_name"] for a in local["action_ledger"]] == ["Sarah Chen"]   # active only
    # ...and it reaches the sections that were inert before
    assert "Sarah Chen" in cb._format_action_ledger(local)
    assert "Sarah Chen" in cb._format_reconciliation(local, "evening")
    # the evening block is THIS morning's actions: a `today` narrows it to rows stamped that day
    assert "Sarah Chen" in cb._format_reconciliation(local, "evening", "2026-06-23")
    assert cb._format_reconciliation(local, "evening", "2026-06-24") == ""
    # A carried ledger cannot replace the source-authorized canonical row.
    local2 = cb._normalize_local({"local": {"action_ledger": [{"status": "open", "contact_name": "X"}]}})
    assert [a["contact_name"] for a in local2["action_ledger"]] == ["Sarah Chen"]


def test_brief_ledger_uses_protected_source_projection(tmp_path, monkeypatch):
    """A raw legacy row cannot escape the owner-facing protected loop view via the brief."""
    import yaml
    import ledger_provenance
    import source_context

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(source_context, 'allowed', lambda source: source == 'calls')
    directory = tmp_path / 'knowledge' / 'continuity'
    directory.mkdir(parents=True)

    def write(name, row):
        (directory / f'{name}.md').write_text('---\n' + yaml.safe_dump(row, sort_keys=False) + '---\n')

    def row(anchor, **extra):
        return {'anchor_key': anchor, 'status': 'open', 'action_type': 'reply',
                'channel': 'phone', 'contact_name': anchor, 'created_at': '2026-10-10',
                'summary': f'Call {anchor}', **extra}

    allowed = row('permitted', source_refs=[{'sourceType': 'calls', 'sourceId': 'call-1'}])
    denied = row('denied', source_refs=[{'sourceType': 'imessage', 'sourceId': 'message-1'}])
    union = row('union', source_refs=[{'sourceType': 'calls', 'sourceId': 'call-2'},
                                    {'sourceType': 'imessage', 'sourceId': 'message-2'}])
    unknown = row('unknown')
    owner = row('owner', channel='manual', source='user_added',
                source_thread_id='manual:owner-1')
    owner['content_provenance'] = {
        'version': 1, 'kind': 'owner', 'sources': [],
        'digest': ledger_provenance.digest_frontmatter(owner),
    }
    for item in (allowed, denied, union, unknown, owner):
        write(item['anchor_key'], item)

    injected = row('injected')
    local = cb._normalize_local({'local': {'action_ledger': [injected]}})
    assert {item['anchor_key'] for item in local['action_ledger']} == {'permitted', 'owner'}
    seen = []
    def model(prompt, *_args, **_kwargs):
        seen.append(prompt)
        return json.dumps({'brief_markdown': '## Filtered\nNo noise.', 'actions': []})
    out = cb.compose({'type': 'morning', 'google': {},
                      'local': {'action_ledger': [injected]}}, llm=model, critic=False)
    assert len(seen) == 1
    assert 'Call permitted' in seen[0] and 'Call owner' in seen[0]
    assert all(f'Call {name}' not in seen[0] for name in ('denied', 'union', 'unknown'))
    assert 'Call injected' not in seen[0]
    assert out['_loop_source_permissions'] == ['calls']


def test_brief_protected_ledger_failure_never_uses_carried_rows(tmp_path, monkeypatch):
    import loops_query
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    def unavailable(*_args):
        raise RuntimeError('protected read unavailable')
    monkeypatch.setattr(loops_query, 'permitted_rows', unavailable)
    with pytest.raises(RuntimeError, match='protected read unavailable'):
        cb._normalize_local({'local': {'action_ledger': [{'status': 'open',
                                                          'summary': 'Unsafe carried row'}]}})


def test_brief_one_protected_ledger_snapshot_binds_prompt_to_delivery_sources(tmp_path, monkeypatch):
    import loops_query
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    row = {'anchor_key': 'call-one', 'status': 'open', 'action_type': 'reply',
           'contact_name': 'Alex Stone', 'channel': 'phone',
           'summary': 'Return the missed call', 'created_at': '2026-10-10',
           'source_refs': [{'sourceType': 'calls', 'sourceId': 'call-1'}]}
    reads = []
    def changing_rows(_root, _sources):
        reads.append(1)
        return [row] if len(reads) == 1 else []
    monkeypatch.setattr(loops_query, 'permitted_rows', changing_rows)
    seen = []
    def model(prompt, *_args, **_kwargs):
        seen.append(prompt)
        return json.dumps({'brief_markdown': '## Filtered\nNo noise.', 'actions': []})
    out = cb.compose({'type': 'morning', 'google': {}, 'local': {}}, llm=model, critic=False)
    assert len(reads) == 1
    assert 'Return the missed call' in seen[0]
    assert out['_loop_source_permissions'] == ['calls']


def test_action_ledger_excludes_meeting_entries(tmp_path, monkeypatch):
    """Meeting-prep/meeting-info loops are calendar shadows — the docket is their surface. The
    brief drops them exactly like /api/loops does, legacy hyphen spelling included."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda source: source == 'calls')
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True)
    for i, atype in enumerate(("meeting_prep", "meeting-prep", "Meeting_Info", "reply")):
        (d / f"{i}.md").write_text(
            f"---\nanchor_key: k{i}\nstatus: open\naction_type: {atype}\n"
            f"contact_name: Person {i}\nchannel: email\ncreated_at: '2026-06-20'\n"
            f"summary: item {i}\nsource_brief_at: '2026-06-23T07:00:00Z'\n"
            f"source_refs:\n  - sourceType: calls\n    sourceId: call-{i}\n---\n")
    local = cb._normalize_local({"local": {}})
    assert [a["action_type"] for a in local["action_ledger"]] == ["reply"]


def test_archive_brief_writes_dated_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    cb._archive_brief({"brief_text": "hello"}, "morning")
    files = list((tmp_path / "briefs").glob("*_morning.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["brief_text"] == "hello"
    # same-day re-run overwrites — the archive holds the brief the user actually got last
    cb._archive_brief({"brief_text": "revised"}, "morning")
    assert json.loads(files[0].read_text())["brief_text"] == "revised"


def test_archive_brief_never_raises(monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", "/proc/definitely-unwritable")
    cb._archive_brief({"brief_text": "x"}, "evening")   # must not raise


# ── attendee research: {"attendees":[…]} envelope (the literal --out file shape) ─

def test_normalize_attendee_research_accepts_both_shapes():
    rows = [{"email": "t@s.com"}]
    assert cb._normalize_attendee_research(rows) == rows                 # documented bare list
    assert cb._normalize_attendee_research({"attendees": rows}) == rows  # research --out envelope
    assert cb._normalize_attendee_research({"junk": 1}) == []
    assert cb._normalize_attendee_research(None) == []


def test_attendee_research_out_envelope_reaches_prompt_via_compose():
    # The skill passes research_attendees.py's --out file straight through as --attendee-research;
    # its {"attendees":[…]} envelope must not silently erase the research block from the prompt.
    seen = {}

    def fake_llm(prompt, inputs):
        seen["prompt"] = prompt
        return json.dumps({"brief_markdown": "# B", "actions": []})

    inputs = {"type": "morning", "google": {"events": []}, "local": {},
              "attendee_research": {"attendees": [
                  {"email": "taylor@startup.com", "title": "CEO", "company": "Startup Inc.",
                   "relevance": ["Raising a Series A"], "summary": "CEO of Startup Inc."}]}}
    cb.compose(inputs, llm=fake_llm)
    assert "Attendee Research (PRE-COMPUTED" in seen["prompt"]
    assert "taylor@startup.com — CEO at Startup Inc." in seen["prompt"]


def test_research_unavailable_envelope_reaches_the_prompt_via_compose():
    """The other half of the same envelope: `warnings` (no search provider) must survive the flatten
    and reach the brief as an unavailable source. Before this, the fact died on stderr and a brief
    with no researched attendees read as a quiet day."""
    seen = {}

    def fake_llm(prompt, inputs):
        seen["prompt"] = prompt
        return json.dumps({"brief_markdown": "# B", "actions": []})

    inputs = {"type": "morning", "google": {"events": []}, "local": {},
              "attendee_research": {"attendees": [], "warnings": ["no search provider connected"]}}
    cb.compose(inputs, llm=fake_llm)
    assert "Data Source Availability" in seen["prompt"]
    assert "Attendee Research (web search)" in seen["prompt"]


def test_research_capacity_pending_is_partial_without_claiming_calendar_is_broken():
    seen = {}
    def fake_llm(prompt, inputs):
        seen['prompt'] = prompt
        return json.dumps({'brief_markdown': '# B', 'actions': []})
    cb.compose({'type': 'morning', 'google': {'events': []}, 'local': {},
                'attendee_research': {'attendees': [], 'status': 'capacity_pending'}},
               llm=fake_llm)
    assert 'Partial coverage: Attendee Research (web search)' in seen['prompt']
    assert 'shared model capacity was busy' in seen['prompt']
    assert 'Unavailable on this device: Attendee Research' not in seen['prompt']


def test_cli_attendee_research_envelope_accepted(tmp_path):
    # End-to-end through main(): --attendee-research pointed at the literal --out file.
    import subprocess, sys as _sys
    (tmp_path / "research.json").write_text(json.dumps({"attendees": [
        {"email": "taylor@startup.com", "title": "CEO", "company": "Startup Inc.",
         "relevance": [], "summary": "CEO of Startup Inc."}]}))
    (tmp_path / "local.json").write_text(json.dumps({"source_status": {"imessage": "ok"}}))
    stub = tmp_path / "stub.json"
    stub.write_text(json.dumps({"brief_markdown": "# Real", "actions": []}))
    script = os.path.join(ROOT, "_shared", "scripts", "compose_brief.py")
    out = subprocess.run(
        [_sys.executable, script, "--local", str(tmp_path / "local.json"),
         "--attendee-research", str(tmp_path / "research.json")],
        capture_output=True, text=True,
        env={**os.environ, "SOTTO_LLM_STUB": str(stub), "SOTTO_DATA": str(tmp_path)})
    assert out.returncode == 0, out.stderr
    assert _body(json.loads(out.stdout)["brief_markdown"]) == "# Real"


# ── --seed-snapshot: setup writes the sender snapshot the event funnel reads ────

def _run_seed(tmp_path, seed_obj):
    import subprocess, sys as _sys
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(seed_obj))
    script = os.path.join(ROOT, "_shared", "scripts", "compose_brief.py")
    out = subprocess.run([_sys.executable, script, "--seed-snapshot", str(seed)],
                         capture_output=True, text=True,
                         env={**os.environ, "SOTTO_DATA": str(tmp_path)})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_seed_snapshot_writes_the_funnel_snapshot(tmp_path):
    """Until this file exists the event funnel can't name a single sender (it resolves every one
    from last_local_snapshot.json), and only a delivered brief wrote it. Setup writes it too."""
    res = _run_seed(tmp_path, {"imessage": [{"handle": "+15551234567", "text": "hi"}],
                               "contacts": [{"name": "Sarah Chen", "phone": "+15551234567"}]})
    assert res["seeded"] is True and res["contacts"] == 1
    assert res["counts"]["imessage"] == 1
    snap = json.loads((tmp_path / "knowledge" / "last_local_snapshot.json").read_text())
    assert snap["local"]["contacts"][0]["name"] == "Sarah Chen"   # the name resolution the funnel needs


def test_seed_snapshot_tolerates_a_raw_tool_wrapper_and_refuses_an_empty_seed(tmp_path):
    wrapped = {"content": [{"type": "text", "text": json.dumps({"whatsapp": [{"text": "yo"}]})}]}
    assert _run_seed(tmp_path, wrapped)["counts"] == {"whatsapp": 1}
    assert _run_seed(tmp_path, {"contacts": []})["seeded"] is False   # nothing to seed → say so


# ── freemail user domain: same-domain attendees still get researched ────────────

def test_automatic_research_excludes_personal_addresses_for_every_owner(tmp_path, monkeypatch):
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 24, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(__import__("prep_selection"), "datetime", _FixedDateTime)
    inputs = {"google": {"userEmail": "me@yahoo.com", "events": [
        {"id": "e", "summary": "Coffee", "start": "2026-06-24T22:00:00+00:00", "attendees": [
            {"email": "me@yahoo.com", "displayName": "Me"},
            {"email": "jordan.vale@yahoo.com", "displayName": "Jordan Vale"}]}]}, "local": {}}
    assert cb.select_attendees_for_research(inputs) == []
    inputs['google']['events'][0]['attendees'].append({'email': 'jordan@acme.example', 'displayName': 'Jordan Vale'})
    assert [p['email'] for p in cb.select_attendees_for_research(inputs)] == ['jordan@acme.example']
    # Corporate stays corporate: same-domain colleague still skipped.
    inputs["google"]["userEmail"] = "me@mycorp.com"
    inputs["google"]["events"][0]["attendees"] = [
        {"email": "me@mycorp.com", "displayName": "Me"},
        {"email": "colleague@mycorp.com", "displayName": "Colleague"}]
    assert cb.select_attendees_for_research(inputs) == []
    # Headless briefs never pass --user-email, so the env-or-settings chain is the only source of
    # "who am I". Without it the user is researched as an attendee of their own meeting and the
    # freemail rule above can never fire in production.
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@mycorp.com")
    del inputs["google"]["userEmail"]
    assert cb.select_attendees_for_research(inputs) == []
    # …and with no env var at all, the address the Google CONNECT derived answers it — that is why
    # SOTTO_USER_EMAIL is an override rather than a requirement.
    monkeypatch.delenv("SOTTO_USER_EMAIL")
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.json").write_text('{"google_account_email": "me@mycorp.com"}')
    assert cb.select_attendees_for_research(inputs) == []


# ── Entity-dedup lite, the brief-side halves (Editor Step 2 item 6) ──────────────────────────────
# Both ride {{knowledge_section}} as prompt CONTEXT: prevention (the company names the graph already
# uses) and the unconfirmed-duplicate line. Context can't break a brief — that's why it lives here.

def _dedup_data(tmp_path, monkeypatch, companies=(), suggestions=None):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    kn = tmp_path / "knowledge"
    (kn / "companies").mkdir(parents=True, exist_ok=True)
    for slug, alias in companies:
        (kn / "companies" / f"{slug}.md").write_text(
            f"---\nschema: 1\nnormalized: {slug}\naliases:\n- {alias}\n"
            "updated_at: '2026-08-06T07:00:00Z'\nupdated_by: brief_extraction\n---\n")
    if suggestions is not None:
        (kn / "merge_suggestions.json").write_text(
            json.dumps({"updated_at": "2026-08-06T07:00:00Z", "suggestions": suggestions}))
    return {"type": "morning", "google": {"events": []}, "local": {}}


def test_known_company_names_ride_into_the_extraction_prompt(tmp_path, monkeypatch):
    """The model free-forms "YC" one day and "Y Combinator" the next; each spelling mints a file.
    Feeding back the names the graph already uses is the cheap half of the fix."""
    inputs = _dedup_data(tmp_path, monkeypatch, companies=[("y-combinator", "Y Combinator"),
                                              ("signal-house", "Signal House")])
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "Companies Already in the Knowledge Graph" in prompt
    assert "- Y Combinator" in prompt and "- Signal House" in prompt


def test_no_company_files_means_no_company_block(tmp_path, monkeypatch):
    """Silence gate: an empty graph adds nothing to the prompt."""
    inputs = _dedup_data(tmp_path, monkeypatch)
    assert "Companies Already in the Knowledge Graph" not in cb.build_prompt(cb._load_prompt(), inputs)


def test_merge_suggestions_surface_as_one_optional_brief_line(tmp_path, monkeypatch):
    inputs = _dedup_data(tmp_path, monkeypatch, suggestions=[
        {"from": "c_aaa", "into": "c_bbb", "from_name": "Ben", "into_name": "Ben Butler",
         "reason": "name containment: ben ⊂ ben-butler", "first_seen": "2026-08-06"}])
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "Memory Maintenance" in prompt
    assert "- Ben and Ben Butler (name containment: ben ⊂ ben-butler)" in prompt
    assert "AT MOST ONE short closing line" in prompt


def test_no_suggestions_means_no_maintenance_block(tmp_path, monkeypatch):
    inputs = _dedup_data(tmp_path, monkeypatch, suggestions=[])
    assert "Memory Maintenance" not in cb.build_prompt(cb._load_prompt(), inputs)
    # …and an unreadable file is silence too, never a crash.
    (tmp_path / "knowledge" / "merge_suggestions.json").write_text("{not json")
    assert "Memory Maintenance" not in cb.build_prompt(cb._load_prompt(), inputs)


def test_the_ledger_block_names_the_chases_that_already_happened():
    """The brief is the daily surface, and it was the one place that never said Sotto had already
    nudged — so it proposed a first follow-up on day 4, day 8 and day 20."""
    local = {"action_ledger": [
        {"status": "open", "action_type": "waiting_on", "contact_name": "Acme", "channel": "imessage",
         "summary": "the signed contract", "created_at": "2026-06-20",
         "chased_count": 2, "last_chased_at": "2026-06-23"},
        {"status": "open", "action_type": "waiting_on", "contact_name": "Vendor", "channel": "imessage",
         "summary": "a quote", "created_at": "2026-06-20"}]}
    block = cb._format_action_ledger(local)
    assert "[reminded you ×2, last Tue]" in block
    assert "Vendor via imessage: a quote" in block and "chased" not in block.split("Vendor")[1]
    assert "Sotto reminded YOU" in block


def test_ledger_block_exposes_recorded_relay_identifier_without_claiming_named_person_owns_it():
    row = {'anchor_key': 'follow_up:cid:relay', 'status': 'open', 'action_type': 'reply',
           'contact_name': 'Alex Stone', 'contact_identifier': '+15550001000',
           'channel': 'imessage', 'summary': 'A relay mentioned a call',
           'ask': 'Reply about the call', 'created_at': '2026-08-05'}
    block = cb._format_action_ledger({'action_ledger': [row]})
    assert '[recorded identifier: +15550001000]' in block
    assert 'may belong to a relay' in block


def test_ledger_ages_follow_an_injected_clock():
    """A replayed day is six weeks old; wall-clock ages would inflate every number in its prompt."""
    from datetime import datetime, timezone
    local = {"action_ledger": [{"status": "open", "action_type": "reply", "contact_name": "Sarah",
                                "channel": "email", "summary": "the deck",
                                "created_at": "2026-06-20 09:00:00"}]}
    now = datetime(2026, 6, 22, 9, 0, 0, tzinfo=timezone.utc)
    assert "(2d)" in cb._format_action_ledger(local, now)
    assert cb.build_prompt("{{action_ledger}}", {"local": local, "now": "2026-06-22 09:00:00"}).count("(2d)") == 1


def test_a_host_data_directory_never_replaces_an_injected_ledger(monkeypatch):
    """Only a named volume switches to the protected ledger; a stray /data on the host does not."""
    real_isdir = os.path.isdir
    monkeypatch.setattr(os.path, "isdir", lambda p: str(p) == "/data" or real_isdir(p))
    monkeypatch.delenv("SOTTO_DATA", raising=False)
    local = {"action_ledger": [{"status": "open", "action_type": "reply", "contact_name": "Sarah",
                                "channel": "email", "summary": "the deck",
                                "created_at": "2026-06-20 09:00:00"}]}
    assert cb.build_prompt("{{action_ledger}}", {"local": local, "now": "2026-06-22 09:00:00"}).count("(2d)") == 1


def test_evening_prompt_receives_the_morning_brief_as_delta_context(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-08-23_morning.json").write_text(json.dumps({
        "brief_markdown": "## Needs Attention Now\n\n- Sarah — review the deck"}))
    block = cb._prior_brief_context("evening", datetime(2026, 8, 23, 17, tzinfo=timezone.utc))
    assert "PRIOR DELIVERED BRIEF" in block and "Sarah — review the deck" in block
    assert "Do not repeat" in block


def test_morning_never_reads_a_same_day_archive(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-08-23_morning.json").write_text(json.dumps({"brief_markdown": "current rerun"}))
    assert cb._prior_brief_context("morning", datetime(2026, 8, 23, 7, tzinfo=timezone.utc)) == ""


def test_prior_brief_uses_the_user_day_not_the_utc_day(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-08-22_morning.json").write_text(json.dumps({"brief_markdown": "local morning"}))
    # 00:30 UTC on Aug 23 is still Aug 22 in Los Angeles.
    block = cb._prior_brief_context(
        "evening", datetime(2026, 8, 23, 0, 30, tzinfo=timezone.utc), "America/Los_Angeles")
    assert "local morning" in block


# ── "What moved today": the evening brief's outcome receipts ──────────────────
# The owner's bar: outcomes, never throughput. These lock the line grammar, the sources each line
# reads, the evening-only gate, the nothing-moved silence, and the fail-quiet discipline.

def _receipt_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    return cb._user_local_date("+00:00")


def _ledger_entry(tmp_path, name, **fm):
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{k}: {json.dumps(v)}" for k, v in fm.items())
    (d / f"{name}.md").write_text(f"---\n{body}\n---\n")


def _cache_file(tmp_path, name, obj):
    d = tmp_path / "cache"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(obj))


def _next_day(day):
    import datetime
    return (datetime.datetime.strptime(day, "%Y-%m-%d")
            + datetime.timedelta(days=1)).strftime("%Y-%m-%d")


_RECEIPT_BRIEF = "## ✅ Already Handled\nQuiet day.\n\n## Filtered\n\n12 promotional emails\n"


def _receipt_llm(markdown=_RECEIPT_BRIEF):
    def fake_llm(prompt, inputs, system=None, schema=None):
        if inputs.get("_followup"):
            raise AssertionError("no ended meetings — followup must not run")
        return json.dumps({"brief_markdown": markdown, "actions": []})
    return fake_llm


def test_the_brief_opens_with_the_time_of_day_and_the_date_in_your_zone():
    """"Needs Attention Now" as the first words read cold (owner, Sep 6, 2026). The greeting is
    code, a heading, keyed to the local HOUR: a morning brief the wake path composes after noon
    says good afternoon, and the evening brief says good evening. The chat text carries it bold."""
    from datetime import datetime, timezone
    pin = lambda hour: {"type": "morning", "google": {"events": [], "userTimezone": "America/Los_Angeles"},
                        "now": datetime(2026, 9, 6, hour, 40, tzinfo=timezone.utc).isoformat()}
    assert cb._greeting_line(pin(14)) == "# Good morning — Sunday, September 6"      # 07:40 PDT
    assert cb._greeting_line(pin(19)) == "# Good afternoon — Sunday, September 6"    # 12:40 PDT
    assert cb._greeting_line(pin(1)) == "# Good evening — Saturday, September 5"     # 18:40 PDT, the day before
    out = cb.compose(pin(14), llm=_receipt_llm())
    assert out["brief_markdown"].startswith("# Good morning — Sunday, September 6\n\n")
    assert out["brief_text"].startswith("*Good morning - Sunday, September 6*\n\n")
    # idempotent: a second pass never stacks a second greeting
    assert cb._prepend_greeting(out, pin(14))["brief_markdown"].count("# Good ") == 1


def _evening(events=None, **extra):
    return {"type": "evening", "google": {"events": events or []}, "granola": [], "local": {}, **extra}


def test_evening_receipts_report_what_moved(monkeypatch, tmp_path):
    """Every line, in its exact grammar, from its own source — and not one count of throughput."""
    today = _receipt_env(monkeypatch, tmp_path)
    _ledger_entry(tmp_path, "chase", status="open", action_type="waiting_on", contact_name="Maya",
                  channel="email", summary="the contract", created_at=today,
                  chased_count=2, last_chased_at=today)
    _ledger_entry(tmp_path, "ron", status="resolved", action_type="waiting_on", contact_name="Ron",
                  summary="the deck", resolution="delivered", resolved_at=today)
    _ledger_entry(tmp_path, "rsvp", status="resolved", action_type="reply", summary="the RSVP",
                  resolved_at=today)
    import ledger_io
    import loops_query
    import ledger_provenance
    monkeypatch.setattr(loops_query, 'permitted_rows', lambda _root, _sources: ledger_io.load_entries())
    monkeypatch.setattr(ledger_provenance, 'source_loop_sources', lambda _row: frozenset({'calls'}))
    _surfaced(tmp_path, [
        _row(_today_at("09:10"), verdict="queue", cls="meeting_hold"),
        _row(_today_at("11:00"), verdict="queue", cls="cooldown"),
        _row(_today_at("14:30"), verdict="queue", cls="budget"),
        _row(_today_at("15:00"), verdict="agent"),        # delivered, not held
        _row(_today_at("15:30"), verdict="drop", cls="automated"),
        _row(_today_at("16:00"), verdict="promoted", cls="meeting_hold")])
    _cache_file(tmp_path, f"research_{today}.json", {"attendees": [{"name": n} for n in "abcd"]})
    _cache_file(tmp_path, "meeting_taps.json", {"date": today, "fired": ["k1", "k2"]})

    out = cb.compose(_evening(events=[{"start": _next_day(today) + "T10:00:00+00:00"}]),
                     llm=_receipt_llm())
    body = out["brief_markdown"]
    assert ("## What moved today\n"
            "- Reminded you to chase Maya about the contract (second reminder).\n"
            "- Closed 2 loops — Ron delivered the deck; the RSVP resolved.\n") in body
    assert out['_activity_receipts'] == {'held_interruptions': 3, 'prepped_people': 4,
                                          'followup_taps': 2}
    # Maya's loop was reminded today: it is reported ONCE, here — never again under Still open
    # (the third mention of one debt in one brief, Sep 5, 2026)
    assert "## Still open" not in body
    # …but a MORNING brief prints no receipts, so it skips nothing: the same reminded loop still
    # earns its Still open line there (a 06:15 chase, a 06:30 brief that forgot her)
    morning = cb.compose({"type": "morning", "google": {"events": []}, "granola": [], "local": {}},
                         llm=_receipt_llm())["brief_markdown"]
    assert "## Still open" in morning and "Maya" in morning.split("## Still open", 1)[1]
    # placement: at the Still open backstop's seam, before the Filtered section (the last one)
    assert body.index("## What moved today") < body.index("## Filtered")
    assert "What moved today" in out["brief_text"]          # and it survives into the chat text
    # the refusal, stated as a test: no throughput anywhere in the block
    block = body.split("## What moved today")[1].split("## Filtered")[0]
    assert "processed" not in block and "emails" not in block and "nudged you" not in block


def test_a_day_where_nothing_moved_gets_no_block(monkeypatch, tmp_path):
    """An empty receipts block would be activity theater about inactivity."""
    _receipt_env(monkeypatch, tmp_path)
    out = cb.compose(_evening(), llm=_receipt_llm())
    assert "What moved today" not in out["brief_markdown"]


def test_receipts_are_evening_only(monkeypatch, tmp_path):
    today = _receipt_env(monkeypatch, tmp_path)
    _ledger_entry(tmp_path, "ron", status="resolved", contact_name="Ron", summary="the deck",
                  resolution="delivered", resolved_at=today)
    out = cb.compose({"type": "morning", "google": {"events": []}, "local": {}}, llm=_receipt_llm())
    assert "What moved today" not in out["brief_markdown"]


def test_a_loop_that_merely_aged_out_is_not_a_closure(monkeypatch, tmp_path):
    """Expired/dismissed loops left the board without anything moving — nothing to report."""
    today = _receipt_env(monkeypatch, tmp_path)
    _ledger_entry(tmp_path, "gone", status="expired", contact_name="Old", summary="a quote",
                  resolution="expired", resolved_at=today)
    _ledger_entry(tmp_path, "drop", status="dismissed", contact_name="Other", summary="a ping",
                  resolution="user_dismissed", resolved_at=today)
    assert cb._receipt_lines(_evening(), today, "+00:00") == []


def test_closures_are_named_up_to_three_then_grouped_by_what_happened(monkeypatch, tmp_path):
    today = _receipt_env(monkeypatch, tmp_path)
    for i, (name, res) in enumerate([("Ron", "delivered"), ("Ada", "delivered"),
                                     ("Ben", "replied"), ("Cy", "meeting_passed")]):
        _ledger_entry(tmp_path, f"c{i}", status="resolved", contact_name=name, summary="a thing",
                      resolution=res, resolved_at=today)
    assert cb._receipt_lines(_evening(), today, "+00:00") == [
        "Closed 4 loops — 2 they delivered, 1 closed out, 1 you answered."]


def test_grouped_reminders_never_claim_counterpart_contact(monkeypatch, tmp_path):
    today = _receipt_env(monkeypatch, tmp_path)
    for index in range(cb.RECEIPT_NAMES_MAX + 1):
        _ledger_entry(tmp_path, f'reminder-{index}', status='open', action_type='waiting_on',
                      contact_name='Maya', summary=f'outstanding request {index}',
                      created_at=today, chased_count=1, last_chased_at=today)
    assert cb._receipt_lines(_evening(), today, '+00:00') == [
        f'Reminded you about {cb.RECEIPT_NAMES_MAX + 1} outstanding requests.']


def test_yesterdays_record_is_not_todays_receipt(monkeypatch, tmp_path):
    today = _receipt_env(monkeypatch, tmp_path)
    _ledger_entry(tmp_path, "old", status="resolved", contact_name="Ron", summary="the deck",
                  resolution="delivered", resolved_at="2020-01-01")
    _cache_file(tmp_path, "meeting_taps.json", {"date": "2020-01-01", "fired": ["k1"]})
    assert cb._receipt_lines(_evening(), today, "+00:00") == []


def test_receipts_follow_an_injected_clock(monkeypatch, tmp_path):
    """A replayed day reports the record of THAT day — the injected-now discipline, as in _action_age."""
    today = _receipt_env(monkeypatch, tmp_path)
    _ledger_entry(tmp_path, "then", status="resolved", contact_name="Ron", summary="the deck",
                  resolution="delivered", resolved_at="2026-06-22")
    _ledger_entry(tmp_path, "now", status="resolved", contact_name="Nia", summary="the memo",
                  resolution="delivered", resolved_at=today)
    out = cb.compose(_evening(now="2026-06-22 09:00:00"), llm=_receipt_llm())
    assert "Closed 1 loop — Ron delivered the deck." in out["brief_markdown"]
    assert "Nia" not in out["brief_markdown"]


def test_prep_is_only_a_receipt_when_tomorrow_has_meetings(monkeypatch, tmp_path):
    today = _receipt_env(monkeypatch, tmp_path)
    _cache_file(tmp_path, f"research_{today}.json", {"attendees": [{"name": "Dana"}]})
    assert cb._receipt_lines(_evening(), today, "+00:00") == []       # nothing on the books tomorrow
    tomorrow = [{"start": _next_day(today) + "T10:00:00+00:00"}]
    assert cb._receipt_lines(_evening(events=tomorrow), today, "+00:00") == []
    assert cb._activity_counts(_evening(events=tomorrow), today, "+00:00")['prepped_people'] == 1


def test_a_broken_record_costs_no_lines_and_no_brief(monkeypatch, tmp_path):
    """Fail toward silence: unreadable ledger/cache/surfaced files render nothing, never an error."""
    today = _receipt_env(monkeypatch, tmp_path)
    (tmp_path / "knowledge" / "continuity").mkdir(parents=True)
    (tmp_path / "knowledge" / "continuity" / "bad.md").write_text("---\nnot: [yaml\n")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "meeting_taps.json").write_text("{not json")
    (tmp_path / "cache" / f"research_{today}.json").write_text("{not json")
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "surfaced.jsonl").write_text("{not json\n")
    out = cb.compose(_evening(), llm=_receipt_llm())
    assert _body(out["brief_markdown"]) == _RECEIPT_BRIEF
    assert cb._receipt_lines(_evening(), today, "+00:00") == []


def test_chase_ordinal_and_nameless_loops_read_as_plain_english():
    # a delivered chase is a reminder to the user — it never says Sotto wrote to Maya
    assert cb._chase_clause({"contact_name": "Maya", "summary": "the contract.",
                             "chased_count": 1}) == "Reminded you to chase Maya about the contract (first reminder)."
    assert cb._chase_clause({"contact_name": "Maya"}) == "Reminded you to chase Maya."
    assert cb._chase_clause({"summary": "the contract"}) == "Reminded you about the contract."
    assert cb._chase_clause({}) == ""                       # nothing to name → no line
    assert cb._closed_clause({"summary": "the RSVP"}) == "the RSVP resolved"
    assert cb._closed_clause({"contact_name": "Ron", "resolution": "replied"}) == "you got back to Ron"
    assert cb._closed_clause({}) == ""


# ── "A newer Sotto is published": one quiet line in the next brief, once per version ──────────────

_UPDATE_LINE = ("Sotto 2026-08-07.bbbbbbb is ready — merge the Railway update PR "
                "(or Sync fork on GitHub); the redeploy is the update.")


def _update_cache(tmp_path, latest="2026-08-07.bbbbbbb", current="2026-08-01.aaaaaaa", **extra):
    """The receiver's daily check, as it lands on the shared volume."""
    _cache_file(tmp_path, "update_check.json",
                {"latest": latest, "fetched_at": 1.0, "current": current, **extra})


def _marker(tmp_path):
    p = tmp_path / "cache" / "update_notice.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_a_new_version_gets_one_line_in_the_next_brief(monkeypatch, tmp_path):
    """Housekeeping: one line at the shared appendix seam, and the marker records the version."""
    _receipt_env(monkeypatch, tmp_path)
    _update_cache(tmp_path)
    body = cb.compose({"type": "morning", "google": {"events": []}, "local": {}},
                      llm=_receipt_llm())["brief_markdown"]
    assert body.count(_UPDATE_LINE) == 1
    assert body.index(_UPDATE_LINE) < body.index("## Filtered")   # the same seam, last in line
    assert _marker(tmp_path) == {"noticed_version": "2026-08-07.bbbbbbb"}


def test_the_update_line_never_repeats_and_never_interrupts(monkeypatch, tmp_path):
    """Two briefs, one line: the marker is what stops the second — and neither brief is a push."""
    _receipt_env(monkeypatch, tmp_path)
    _update_cache(tmp_path)
    first = cb.compose({"type": "morning", "google": {"events": []}, "local": {}},
                       llm=_receipt_llm())["brief_markdown"]
    second = cb.compose(_evening(), llm=_receipt_llm())["brief_markdown"]
    assert first.count(_UPDATE_LINE) == 1
    assert "is ready — merge" not in second
    # …until the NEXT version is published, which is a different version to notice
    _update_cache(tmp_path, latest="2026-09-01.ccccccc")
    third = cb.compose({"type": "morning", "google": {"events": []}, "local": {}},
                       llm=_receipt_llm())["brief_markdown"]
    assert "Sotto 2026-09-01.ccccccc is ready" in third
    assert _marker(tmp_path) == {"noticed_version": "2026-09-01.ccccccc"}


def test_an_up_to_date_or_unchecked_server_says_nothing(monkeypatch, tmp_path):
    """No cache (SOTTO_UPDATE_CHECK=0, no volume, dev build) and same-stamp both render nothing."""
    _receipt_env(monkeypatch, tmp_path)
    assert cb._update_notice_version() == ""                       # nothing on the volume at all
    _update_cache(tmp_path, latest="2026-08-01.aaaaaaa")           # already on the latest
    assert cb._update_notice_version() == ""
    _update_cache(tmp_path, current="")                            # dev build: no stamp of its own
    assert cb._update_notice_version() == ""
    _cache_file(tmp_path, "update_check.json", {"latest": "2026-08-07.bbbbbbb"})   # pre-upgrade file
    assert cb._update_notice_version() == ""
    out = cb.compose({"type": "morning", "google": {"events": []}, "local": {}}, llm=_receipt_llm())
    assert _body(out["brief_markdown"]) == _RECEIPT_BRIEF
    assert _marker(tmp_path) is None                               # nothing said, nothing recorded


def test_an_unreadable_update_cache_costs_nothing(monkeypatch, tmp_path):
    """Same bar as its neighbouring appendices: any failure renders nothing, never an error."""
    _receipt_env(monkeypatch, tmp_path)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "update_check.json").write_text("{not json")
    out = cb.compose({"type": "morning", "google": {"events": []}, "local": {}}, llm=_receipt_llm())
    assert _body(out["brief_markdown"]) == _RECEIPT_BRIEF


# --- the brief's three read-view fixes: snooze, the fold's receipts, the "+N more" cap ------------

def test_a_snoozed_loop_reaches_neither_the_brief_nor_the_count(monkeypatch, tmp_path):
    """Three read views, ONE rule. The brief read the ledger without `snoozed_until`, so a loop the
    user had explicitly parked was named as urgent AND counted in the line that points at
    /app#loops — where sotto-loops showed neither. The filter lives in ledger_io.load_active now."""
    import loops_query as lq
    today = _receipt_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    _ledger_entry(tmp_path, "maya", anchor_key="maya", status="open", action_type="waiting_on",
                  channel="email", contact_name="Maya Chen", contact_identifier="maya@x.com",
                  summary="the signed contract", created_at="2026-01-02 09:00:00",
                  chased_count=1, snoozed_until="2099-01-01")
    _ledger_entry(tmp_path, "ron", anchor_key="ron", status="open", action_type="reply",
                  channel="email", contact_name="Ron Diaz", contact_identifier="ron@x.com",
                  summary="his intro request", created_at="2026-01-02 09:00:00",
                  snoozed_until="2099-01-01")
    assert lq.query()["counts"] == {"you_owe": 0, "waiting_on_them": 0}
    assert cb._open_ledger_entries({}) == []

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": "## Needs Attention Now\nQuiet.\n", "actions": []})

    body = cb.compose({"type": "morning", "google": {}, "local": {}},
                      llm=fake_llm, critic=True)["brief_markdown"]
    assert "Still open" not in body and "Maya Chen" not in body
    assert today                                          # the fixture's day, unused but asserted live


def test_a_folded_rows_chase_is_reported_once(monkeypatch, tmp_path):
    """When two rows fold, the retired one KEEPS its `last_chased_at` and the survivor adopts it —
    so one delivered nudge printed "Gave Maya Chen a nudge…" twice. A dedupe is bookkeeping: the
    survivor carries the chase, the merged_duplicate reports nothing. (It is correctly never
    reported as a closure either — that half was already right.)"""
    today = _receipt_env(monkeypatch, tmp_path)
    _ledger_entry(tmp_path, "keeper", status="open", action_type="waiting_on",
                  contact_name="Maya Chen", channel="email", summary="the signed contract",
                  created_at=today, chased_count=1, last_chased_at=today)
    _ledger_entry(tmp_path, "loser", status="dismissed", resolution="merged_duplicate",
                  merged_into="keeper", action_type="waiting_on", contact_name="Maya Chen",
                  channel="email", summary="the signed contract", created_at=today,
                  chased_count=1, last_chased_at=today, resolved_at=today)
    chased, closed = cb._ledger_receipts(today)
    assert [cb._chase_clause(e) for e in chased] == [
        "Reminded you to chase Maya Chen about the signed contract (first reminder)."]
    assert closed == []


def test_a_fourth_ask_from_one_person_is_named_not_sliced_off(monkeypatch):
    """The cap keeps one person's line from becoming the wall again, but an ask silently sliced off
    the end was shown nowhere and counted nowhere. Past the cap the line says how many are left."""
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    debts = [{"contact_name": "Maya Chen", "status": "open", "action_type": "waiting_on",
              "channel": "email", "contact_identifier": "maya@x.com", "chased_count": 1,
              "summary": s, "created_at": _iso_hours_ago(30)}
             for s in ("the signed contract", "the vendor list", "the Q3 numbers", "the board memo")]

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": "## Needs Attention Now\nQuiet.\n", "actions": []})

    body = cb.compose({"type": "morning", "google": {}, "local": {"action_ledger": debts}},
                      llm=fake_llm, critic=True)["brief_markdown"]
    lines = [l for l in body.split("## Still open\n", 1)[1].splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("- **Maya Chen**: the signed contract; the vendor list; the Q3 numbers; "
                               "+1 more (open since ")


# --- what the brief NAMED, recorded for the chase lane -------------------------------------------

def test_the_brief_records_which_loops_it_named(monkeypatch, tmp_path):
    """The chase lane's collision check needs to know what THIS brief said, not merely that a brief
    happened: the printed and narrated loops are written down, the ones behind the count line are
    not (a count is not a telling)."""
    today = _receipt_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    named = {"anchor_key": "loop:maya", "contact_name": "Maya Chen", "status": "open",
             "action_type": "waiting_on", "channel": "email", "contact_identifier": "maya@x.com",
             "chased_count": 1, "summary": "the signed contract",
             "created_at": _iso_hours_ago(30)}
    quiet = {"anchor_key": "loop:ron", "contact_name": "Ron Diaz", "status": "open",
             "action_type": "reply", "channel": "email", "contact_identifier": "ron@x.com",
             "summary": "his intro request", "created_at": _iso_hours_ago(30)}
    # Keep this count-vs-named contract meaningful now that short text briefs can show quiet
    # tasks: a complete long ask cannot fit the existing readable expansion budget.
    import visual_brief
    quiet['summary'] += ' with supporting context' * visual_brief.MAX_WORDS
    import loops_query
    import ledger_provenance
    monkeypatch.setattr(loops_query, 'permitted_rows', lambda _root, _sources: [named, quiet])
    monkeypatch.setattr(ledger_provenance, 'source_loop_sources', lambda _row: frozenset({'calls'}))

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": "## Needs Attention Now\nQuiet.\n", "actions": []})

    body = cb.compose({"type": "morning", "google": {}, "local": {"action_ledger": [named, quiet]}},
                      llm=fake_llm, critic=True)["brief_markdown"]
    assert "**Maya Chen**" in body and "- 1 other open loop. Ask me what's still open." in body
    with open(cb._named_loops_path(today, "morning"), encoding="utf-8") as f:
        assert json.load(f) == {"anchor_keys": ["loop:maya"]}


# --- Apple Notes / Reminders / Granola actually reach the model ----------------------------------
#
# The owner's report (Aug 2026): "granola is connected but I have never seen anything come from that.
# same with apple notes, reminders … I took a note from Brexton in Apple Notes on companies, that
# should be flagged?" These pin the rendered prompt so a regression that silently drops one of the
# three surfaces is a failing test rather than weeks of quietly thinner briefs.

def _sources_inputs(local):
    return {"type": "morning", "first_run": False, "google": {"emails": [], "events": []},
            "local": local}


def test_apple_note_with_an_ask_about_companies_reaches_the_model():
    note = {"title": "Brexton — companies to look at", "folder": "Notes",
            "modified_date": "2026-08-09 18:32:00",
            "snippet": "From coffee w/ Brexton: 1) Helix Bio - protein design, raising seed. "
                       "2) Northwind Robotics - warehouse arms. He'll send the Helix deck this week."}
    prompt = cb.build_prompt(cb._load_prompt(), _sources_inputs({"apple_notes": [note]}))
    assert "### Recent Apple Notes" in prompt
    assert "**Brexton — companies to look at** (Notes, modified 2026-08-09 18:32:00)" in prompt
    assert "Helix Bio - protein design, raising seed" in prompt
    # …and the model is told to connect it to the people in today's brief, not just shown it.
    assert "Cross-reference with people and meetings" in prompt


def test_no_apple_notes_leaves_no_empty_section_or_placeholder():
    prompt = cb.build_prompt(cb._load_prompt(), _sources_inputs({"apple_notes": []}))
    assert "{{apple_notes}}" not in prompt and "### Recent Apple Notes" not in prompt


def test_reminders_reach_the_model_and_a_disabled_source_says_so():
    prompt = cb.build_prompt(cb._load_prompt(), {
        **_sources_inputs({"reminders": [{"title": "Send Brexton the Arcadia memo",
                                          "due_date": "2026-08-10"}]}),
        "now": "2026-08-10 07:00:00"})
    assert "- Send Brexton the Arcadia memo (due today)" in prompt
    off = cb.build_prompt(cb._load_prompt(), _sources_inputs(
        {"reminders": [], "_source_availability": {"reminders": "disabled"}}))
    assert "(disabled by user)" in off


def test_an_overdue_reminder_reads_as_overdue_not_as_a_date():
    """The Bridge sees the last 7 days and the next 3 — a reminder counts if it's due in that span,
    or if you wrote it in the last 7 days. So overdue and undated reminders now reach the prompt,
    and each of the four shapes has to say what it is without the model doing date arithmetic."""
    inputs = {**_sources_inputs({"reminders": [
        {"title": "Send Brexton the Arcadia memo", "due_date": "2026-08-07"},   # 3 days ago
        {"title": "Call the dentist", "due_date": "2026-08-09 09:00:00"},       # yesterday
        {"title": "Confirm the Helix intro", "due_date": "2026-08-10 15:00:00"},
        {"title": "Pay the Q3 invoice", "due_date": "2026-08-12"},
        {"title": "Look at Northwind Robotics", "created_date": "2026-08-05 21:04:00"},
    ]}), "now": "2026-08-10 07:00:00"}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "- Send Brexton the Arcadia memo (due 3 days ago)" in prompt
    assert "- Call the dentist (due 1 day ago)" in prompt      # singular, not "1 days ago"
    assert "- Confirm the Helix intro (due today)" in prompt
    assert "- Pay the Q3 invoice (due 2026-08-12)" in prompt   # ahead of you: a plain date
    assert "- Look at Northwind Robotics (no date, written Wednesday)" in prompt


def test_reminder_relative_dates_follow_the_injected_clock_not_the_wall_clock():
    """One clock. A corpus replay of a day six weeks back must not read every reminder as ancient."""
    from datetime import datetime as _dt, timezone as _tz
    rem = [{"title": "Send the memo", "due_date": "2026-08-07"}]
    assert "(due today)" in render_local._format_reminders(rem, None, _dt(2026, 8, 7, 9, tzinfo=_tz.utc))
    assert "(due 2026-08-07)" in render_local._format_reminders(rem, None, _dt(2026, 8, 4, 9, tzinfo=_tz.utc))
    assert "(due 30 days ago)" in render_local._format_reminders(rem, None, _dt(2026, 9, 6, 9, tzinfo=_tz.utc))
    # The undated half moves with the same clock, and a weekday name is only used inside the 7-day
    # window that produced it — at exactly 7 days back "Monday" would mean today, so it dates out.
    jotted = [{"title": "Look at Northwind", "created_date": "2026-08-03 21:04:00"}]
    assert "(no date, written Monday)" in render_local._format_reminders(jotted, None, _dt(2026, 8, 5))
    assert "(no date, written yesterday)" in render_local._format_reminders(jotted, None, _dt(2026, 8, 4))
    assert "(no date, written 2026-08-03)" in render_local._format_reminders(jotted, None, _dt(2026, 8, 10))


def test_an_undated_reminder_from_an_older_bridge_build_still_renders():
    """`created_date` is additive — a payload from a Bridge that predates it must not blow up or
    print "None"; it degrades to a bare "(no date)"."""
    from datetime import datetime as _dt
    assert render_local._format_reminders([{"title": "Look at Northwind"}], None,
                                          _dt(2026, 8, 10)) == "- Look at Northwind (no date)"


def test_granola_notes_reach_the_model_and_a_broken_gather_says_so():
    meeting = {"title": "Arcadia <> Sotto", "date": "2026-08-09",
               "attendee_emails": ["brexton@arcadia.vc"],
               "ai_summary": "Action items: Nik sends the memo; Brexton forwards the Helix deck."}
    prompt = cb.build_prompt(cb._load_prompt(), {
        **_sources_inputs({}), "granola": {"meetings": [meeting]}})
    assert "**Arcadia <> Sotto** (2026-08-09)" in prompt
    assert "Brexton forwards the Helix deck" in prompt
    # A gather that failed embeds warnings — the brief must SAY Granola is unavailable rather than
    # render "(none)" and let a broken connector read as a quiet week.
    broken = cb.build_prompt(cb._load_prompt(), {
        **_sources_inputs({}),
        "granola": {"meetings": [], "warnings": ["no usable detail for 2 meeting(s)"]}})
    assert "Unavailable on this device: Meeting Notes (Granola)" in broken


def test_calendar_formatter_flags_meetings_with_nowhere_to_go():
    """A meeting with other humans, no location, and no video link gets the logistics flag the
    brief's Coming Up line carries — and the calendar's own htmlLink (which gather folds into
    meetingLink) is NOT a way to join."""
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "rl_flag", os.path.join(os.path.dirname(__file__), "..", "_shared", "lib", "render_local.py"))
    rl = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(rl)
    others = [{"email": "ron@example.com", "displayName": "Ron"}]
    base = {"id": "e1", "summary": "Coffee", "start": "2026-08-26T11:30:00-07:00",
            "attendees": others, "location": "", "description": "", "meetingLink": ""}
    FLAG = "no video link and no address"
    assert FLAG in rl._format_calendar([base])
    assert FLAG not in rl._format_calendar([dict(base, location="Mendocino Farms, 300 Mission St")])
    assert FLAG not in rl._format_calendar([dict(base, meetingLink="https://meet.google.com/abc")])
    assert FLAG not in rl._format_calendar(
        [dict(base, description="join: https://us02web.zoom.us/j/123")])
    # the calendar EVENT page is not a place to meet — the pollution case the regex must not accept
    assert FLAG in rl._format_calendar(
        [dict(base, meetingLink="https://www.google.com/calendar/event?eid=abc")])
    # solo blocks and all-day events are not meetings
    assert FLAG not in rl._format_calendar([dict(base, attendees=[])])
    assert FLAG not in rl._format_calendar([dict(base, start="2026-08-26")])


def test_snapshot_archive_accumulates_dated_copies_and_prunes(tmp_path, monkeypatch):
    """The live snapshot answers "now"; the dated archive is the Golden Corpus's message history.
    One file per day (last write wins), pruned past SNAPSHOT_ARCHIVE_DAYS."""
    from datetime import datetime, timezone
    import local_snapshot

    class ArchiveClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 26, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(local_snapshot, 'datetime', ArchiveClock)
    os.environ["SOTTO_DATA"] = str(tmp_path)
    try:
        arch = tmp_path / "knowledge" / "snapshots"
        arch.mkdir(parents=True)
        (arch / "1999-01-01.json").write_text("{}")            # ancient — must be pruned
        cb._save_local_snapshot({"imessage": [{"text": "hi", "timestamp": "2026-08-25 09:00:00"}],
                                 "generated_at": "2026-08-25 09:00:00",
                                 "source_status": {"imessage": "ok"}})
        cb._save_local_snapshot({"imessage": [{"text": "later", "timestamp": "2026-08-25 18:00:00"}],
                                 "generated_at": "2026-08-25 18:00:00",
                                 "source_status": {"imessage": "ok"}})
        names = sorted(p.name for p in arch.glob("*.json"))
        assert names == ["2026-08-25.json"]                    # dated, deduped by day, pruned
        day = json.load(open(arch / "2026-08-25.json"))
        assert day["local"]["imessage"][0]["text"] == "later"  # last write of the day wins
    finally:
        del os.environ["SOTTO_DATA"]


@pytest.mark.parametrize('bad', ['{}', '{"markdown":""}', '{"markdown":"  "}',
                                 '{"markdown":42}', '[]', 'not JSON'])
def test_empty_extraction_retries_full_sources_before_critic(tmp_path, monkeypatch, bad):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    calls = []
    def llm(prompt, inputs, system=None, schema=None):
        calls.append((prompt, system, schema))
        assert not inputs.get('_critic') and not inputs.get('_revise')
        return bad if len(calls) == 1 else json.dumps({'markdown': 'Nothing needs your attention today.'})
    out = cb.compose({'type': 'morning', 'google': {}, 'local': {}}, llm=llm)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert 'Nothing needs your attention today.' in out['brief_markdown']
    assert set(calls[0][2]['required']) == {'markdown', 'actionItems', 'loopUpdates', 'extractedKnowledge'}


def test_repeated_empty_extraction_fails_without_critic_or_memory_writes(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_CRITIC', 'always')
    calls = []
    def llm(prompt, inputs, **kwargs):
        assert not inputs.get('_critic') and not inputs.get('_revise')
        calls.append(prompt)
        return '{"markdown":""}'
    with pytest.raises(RuntimeError, match='extraction failed twice'):
        cb.compose({'type': 'morning', 'google': {}, 'local': {}}, llm=llm, critic=True)
    assert len(calls) == 2
    assert not (tmp_path / 'briefs').exists()


def test_revision_receives_original_calendar_and_conversation_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_CRITIC', 'always')
    source = 'VERIFIED SOURCE: Tuesday lunch at Noon Cafe. The later reply resolved the missed call.'
    monkeypatch.setattr(cb, 'build_prompt', lambda *a: source)
    def llm(prompt, inputs):
        if inputs.get('_critic'):
            return json.dumps({'score': 60, 'summary': 'missing meeting', 'patches': [
                {'type': 'missing', 'detail': 'Restore the calendar', 'severity': 'moderate'}]})
        if inputs.get('_revise'):
            assert source in prompt
            return json.dumps({'brief_markdown': 'Coming Up: Tuesday lunch at Noon Cafe.', 'actions': []})
        return json.dumps({'brief_markdown': 'A valid but incomplete draft.', 'actions': []})
    out = cb.compose({'type': 'morning', 'google': {}, 'local': {}}, llm=llm, critic=True)
    assert 'Tuesday lunch at Noon Cafe.' in out['brief_markdown']


def test_calendar_preview_keeps_last_meeting_and_quiet_line_is_time_neutral():
    body = ('Nothing needs you this morning; inbox is clear and no loops are open.\n\n'
            '**Coming Up**\n' + '\n'.join(f'- **{n}:00 PM** — Meeting {n}' for n in range(1, 6))
            + '\n\n## ✅ Already Handled\nDone')
    out = cb._finish_brief_presentation({'brief_markdown': body})
    assert 'this morning' not in out['brief_markdown']
    assert '5:00 PM' in out['brief_markdown']
    assert 'Calendar preview' not in out['brief_markdown']
    assert not cb.brief_validate._check_coming_up_length(out['brief_markdown'])
    assert cb._finish_brief_presentation(out) == out


def test_brief_facts_require_a_reference_present_in_source_snapshot():
    person = {'person_name': 'Maya', 'facts': [
        {'fact': 'Runs legal'},
        {'fact': 'Runs legal', 'source_ref': ''},
        {'fact': 'Runs legal', 'source_ref': 'invented'},
        {'fact': 'Runs legal', 'source_ref': 'email-123'},
    ]}
    out = {'extracted_knowledge': {'person_updates': [person]}}
    cb._bind_brief_fact_evidence(out, {'google': {'emails': [
        {'id': 'email-123', 'date': '2026-09-10T08:00:00Z'}]}})
    assert person['facts'] == [
        {'fact': 'Runs legal', 'source_ref': 'email-123', 'observed_date': '2026-09-10'}]
    assert out['learning_evidence'] == {'accepted': 1, 'rejected': 3}


@pytest.mark.parametrize(("source_date", "expected"), [
    ("Thu, 10 Sep 2026 08:00:00 +0000", "2026-09-10"),
    ("2026-09-10T08:00:00Z", "2026-09-10"),
    ("unavailable", None),
])
def test_fact_observed_date_comes_from_source_not_model(source_date, expected):
    fact = {"fact": "Runs legal", "source_ref": "email-123", "observed_date": "2099-01-01"}
    out = {"extracted_knowledge": {"person_updates": [{"person_name": "Maya", "facts": [fact]}]}}
    cb._bind_brief_fact_evidence(out, {"google": {"emails": [{"id": "email-123", "date": source_date}]}})
    assert fact.get("observed_date") == expected


@pytest.mark.parametrize('note', [
    'Calendar preview - open Calendar for the full schedule.',
    'Calendar preview (open Calendar for the full schedule):',
    'Calendar preview — open Calendar for the full schedule.',
    'Calendar preview: open Calendar for the full schedule.',
])
def test_legacy_calendar_notes_are_removed_without_costing_a_meeting(note):
    body = ('## Coming Up\n' + note + '\nCalendar preview: open Calendar for the full schedule.'
            + '\n' + '\n'.join(f'- **{n}:00 PM**: Meeting {n}' for n in range(1, 6)))
    out = cb._finish_brief_presentation({'brief_markdown': body})
    assert 'Calendar preview' not in out['brief_markdown']
    assert '5:00 PM' in out['brief_markdown']
    assert not cb.brief_validate._check_coming_up_length(out['brief_markdown'])
    assert cb._finish_brief_presentation(out) == out


def test_model_work_hold_delivers_the_draft_and_never_reports_a_passing_critic(monkeypatch):
    """A quality stage whose attempts are spent may not cost the user the brief (one per day,
    always). The draft ships, and the record says the gate was held — never that it passed."""
    import model_work
    def held(*args, **kwargs):
        raise model_work.ModelWorkHeldError('stage exhausted')
    critic = cb.run_critic('draft', [], {}, llm=held)
    assert critic['held'] is True and critic['score'] == -1 and critic['patches'] == []
    out = cb.critique_and_revise({'brief_markdown': 'draft'}, {}, llm=held)
    assert out['brief_markdown'] == 'draft' and out['_critic']['held'] is True
    monkeypatch.setattr(cb, 'run_critic', lambda *a, **k: {
        'patches': [{'severity': 'critical', 'type': 'fix', 'detail': 'unsupported claim'}],
        'score': 0, 'summary': 'repair required'})
    out = cb.critique_and_revise({'brief_markdown': 'draft'}, {}, llm=held, source_prompt='evidence')
    assert out['brief_markdown'] == 'draft' and out['_critic']['held'] is True



def test_gallery_space_names_active_loops_before_delivery_attribution(tmp_path, monkeypatch):
    from test_visual_brief import BRIEF
    import visual_brief
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    rows = [{"anchor_key": f"quiet-{i}", "contact_name": name, "status": "open",
             "canonical_id": f"person-{i}", "contact_identifier": f"person{i}@example.com",
             "type": "waiting_on", "created_at": "2026-09-19T10:00:00+00:00",
             "summary": f"Send the {subject} notes for next month."}
            for i, (name, subject) in enumerate([
                ("Robin Green", "planning"), ("Taylor Chen", "research"),
                ("Casey Brooks", "workshop"), ("Morgan White", "strategy")])]
    excluded = [{**rows[0], "anchor_key": "closed", "contact_name": "Closed Person", "status": "resolved"},
                {**rows[0], "anchor_key": "parked", "contact_name": "Parked Person", "status": "parked"}]
    import loops_query
    import ledger_provenance
    monkeypatch.setattr(loops_query, 'permitted_rows', lambda _root, _sources: rows + excluded)
    monkeypatch.setattr(ledger_provenance, 'source_loop_sources', lambda _row: frozenset({'calls'}))
    inputs = {"type": "morning", "google": {}, "local": {"action_ledger": rows + excluded}}
    def model(*args, **kwargs):
        return json.dumps({"brief_markdown": BRIEF.split('\n', 1)[1], "actions": []})
    out = cb.compose(inputs, llm=model, critic=False)
    assert "other open loop" not in out['brief_text']
    for row in rows:
        assert row['contact_name'] in out['brief_text']
        assert row['summary'] in out['brief_text']
    assert 'Closed Person' not in out['brief_text'] and 'Parked Person' not in out['brief_text']
    assert {r['anchor_key'] for r in out['_represented_loops']} == {r['anchor_key'] for r in rows}
    named = list((tmp_path / 'briefs').glob('*.named.json'))
    assert len(named) == 1
    assert set(json.loads(named[0].read_text())['anchor_keys']) == {r['anchor_key'] for r in rows}
    manifest = visual_brief.render(visual_brief.build(out['brief_text']), tmp_path / 'gallery')
    assert len(manifest['images']) == 4
    for row in rows:
        assert row['summary'] in str(manifest['cards'])


def test_optional_loop_details_stop_at_readable_space_and_leave_count():
    from test_visual_brief import BRIEF
    import visual_brief
    rows = [{"anchor_key": f"quiet-{i}", "contact_name": f"Robin Person{chr(65 + i)}", "status": "open",
             "summary": "Review the planning notes and decide whether to attend the workshop next month.",
             "created_at": "2026-09-19T10:00:00+00:00"} for i in range(20)]
    out = cb._append_still_open({'brief_markdown': BRIEF}, rows, today='2026-09-20')
    expanded = cb._expand_quiet_loops(out, rows, today='2026-09-20')
    shown = cb._represented_loop_rows(rows, expanded['brief_markdown'])
    assert 0 < len(shown) < len(rows)
    assert f"{len(rows) - len(shown)} other open loops" in expanded['brief_markdown']
    assert visual_brief.fits(visual_brief.build(expanded['brief_markdown']), allow_tall=False)
    assert all(row['summary'] in expanded['brief_markdown'] for row in shown)


def test_optional_loop_details_never_repeat_an_evening_receipt_or_break_fallback(monkeypatch):
    from test_visual_brief import BRIEF
    import visual_brief
    row = {'anchor_key': 'already-reported', 'contact_name': 'Robin Green', 'status': 'open',
           'summary': 'Send the planning notes next month.'}
    out = cb._append_still_open({'brief_markdown': BRIEF}, [row], today='2026-09-20')
    original = out['brief_markdown']
    cb._expand_quiet_loops(out, [row], today='2026-09-20', reported={'already-reported'})
    assert 'Robin Green' not in out['brief_markdown']
    assert 'other open loop' not in out['brief_markdown'], 'a reported task is not part of the remaining count'
    assert '## Still open' not in out['brief_markdown'], 'remove the now-empty section too'
    out['brief_markdown'] = original
    monkeypatch.setattr(visual_brief, 'fits', lambda deck, **kwargs: False)
    cb._expand_quiet_loops(out, [row], today='2026-09-20')
    assert out['brief_markdown'] == original



def test_spare_space_does_not_repeat_a_handoff_question_already_delivered():
    from test_visual_brief import BRIEF
    row = {'anchor_key': 'waiting', 'contact_name': 'Robin Green', 'status': 'waiting',
           'summary': 'Send the planning notes next month.', 'chased_count': 2,
           'handoff_asked_at': '2026-09-19T10:00:00+00:00'}
    out = cb._append_still_open({'brief_markdown': BRIEF}, [row], today='2026-09-20')
    cb._expand_quiet_loops(out, [row], today='2026-09-20')
    assert '1 other open loop' in out['brief_markdown']
    assert 'Robin Green' not in out['brief_markdown']


def test_revision_requires_task_update_contract_and_preserves_draft(monkeypatch):
    monkeypatch.setattr(cb, 'build_data_manifest', lambda inputs: {})
    seen = []
    def llm(prompt, inputs, system=None, schema=None):
        if inputs.get('_critic'):
            return json.dumps({'patches': [{'type': 'mark_handled', 'detail': 'synthetic',
                                             'severity': 'critical'}], 'score': 55, 'summary': 'fix'})
        seen.append(schema)
        return json.dumps({'brief_markdown': 'revised', 'actions': [],
                           'loop_updates': [{'loopId': 'one', 'evidence': {'sourceId': 'm1'}}]})
    draft = {'brief_markdown': 'draft', 'actions': [], 'loop_updates': []}
    out = cb.critique_and_revise(draft, {}, llm=llm, source_prompt='fixture')
    assert out['brief_markdown'] == 'draft'
    assert out['loop_updates'] == []
    assert seen == [cb.BRIEF_REVISION_SCHEMA]


def test_critic_manifest_includes_contact_birthday_evidence():
    manifest = cb.build_data_manifest({'type': 'morning', 'now': '2026-09-21T13:30:00Z',
        'google': {'userTimezone': 'America/Los_Angeles'},
        'local': {'contacts': [{'name': 'Robin Green', 'birthday': '09-21'}]}})
    assert any('Robin Green' in line for line in manifest['birthday_evidence'])


def test_fact_reference_cannot_bind_to_disabled_local_source():
    out = {'extracted_knowledge': {'person_updates': [{'person_name': 'Robin Green',
        'facts': [{'fact': 'synthetic fact', 'source_ref': 'imessage:private-row'}]}]}}
    cb._bind_brief_fact_evidence(out, {'local': {
        'source_status': {'imessage': 'disabled'},
        'imessage': [{'id': 'private-row', 'timestamp': '2026-09-21 09:00:00'}]}})
    assert out['extracted_knowledge']['person_updates'][0]['facts'] == []
    assert out['learning_evidence']['rejected'] == 1


def test_loop_projection_removes_only_verified_task_and_keeps_distinct_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    from delivery_effects import loop_version
    from render_local import message_evidence_id
    row = {'anchor_key': 'task-one', 'status': 'open', 'action_type': 'reply',
           'contact_name': 'Robin Green', 'contact_identifier': '+14155552222',
           'channel': 'imessage', 'created_at': '2026-09-20 08:00:00',
           'summary': 'Send the pricing document'}
    sibling = {**row, 'anchor_key': 'task-two', 'summary': 'Review the draft agreement'}
    msg = {'rowid': 19, 'is_from_me': True, 'handle': '+14155552222',
           'timestamp': '2026-09-21 09:00:00', 'text': 'The pricing document is sent.'}
    update = {'loopId': row['anchor_key'], 'loopVersion': loop_version(row),
              'status': 'resolved', 'evidence': [{'sourceType': 'imessage',
                  'sourceId': message_evidence_id(msg, 'imessage'),
                  'snippet': 'pricing document is sent'}]}
    update['criticReceipt'] = cb._continuity().record_critic_approval(update)
    inputs = {'type': 'morning', 'local': {'imessage': [msg]}, 'google': {}}
    out = {'brief_markdown': '## ✅ Already Handled\n- **Robin Green**: Send the pricing document. <!--loop:task-one-->\n'
                             '## Still Pending\n- **Robin Green**: Review the draft agreement.\n',
           'loop_updates': [update]}
    projected = cb._project_loop_updates(out, inputs, [row, sibling])
    assert projected == [sibling]
    cb._reconcile_task_sections(out, [row, sibling], projected)
    assert 'Send the pricing document' in out['brief_markdown']
    assert 'Review the draft agreement' in out['brief_markdown']
    assert out['_loop_projection']['resolved'] == ['task-one']


def _closure_fixture(tmp_path, monkeypatch, n=1):
    """A captured `send Dana ...` task per item, plus a sent email saying it has NOT been sent."""
    import yaml
    from delivery_effects import loop_version
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    monkeypatch.delenv('SOTTO_CRITIC', raising=False)
    (tmp_path / 'briefs').mkdir()
    (tmp_path / 'briefs/2026-09-18.morning.delivered').write_text('x')  # not the first brief
    ledger = tmp_path / 'knowledge/continuity'
    ledger.mkdir(parents=True)
    email = {'id': 'm-neg', 'isSent': True, 'labelIds': ['SENT'], 'from': 'me@x.com',
             'to': 'Dana <dana@acme.com>', 'date': '2026-09-19T10:00:00Z', 'subject': 'deck',
             'body': "I haven't sent the deck yet, will do next week."}
    rows, updates = [], []
    for i in range(n):
        row = {'anchor_key': f'deck-{i}', 'status': 'open', 'action_type': 'follow_up',
               'channel': 'gmail', 'contact_name': 'Dana', 'contact_identifier': 'dana@acme.com',
               'created_at': '2026-09-18 08:00:00', 'summary': 'You committed to: send Dana the deck',
               'ask': 'send Dana the deck', 'resolution_mode': 'source_grounded', 'source': 'granola'}
        (ledger / f'deck-{i}.md').write_text('---\n' + yaml.safe_dump(row, sort_keys=False) + '---\n')
        rows.append(row)
        updates.append({'loopId': row['anchor_key'], 'loopVersion': loop_version(row),
                        'status': 'resolved', 'evidence': [{'sourceType': 'email',
                        'sourceId': 'm-neg', 'snippet': 'sent the deck'}]})
    inputs = {'type': 'morning', 'now': '2026-09-19T12:00:00Z', 'google': {'emails': [email]},
              'local': {'action_ledger': rows}}
    return inputs, updates


def test_auto_critic_reviews_any_proposed_closure_on_a_quiet_brief(tmp_path, monkeypatch):
    inputs, updates = _closure_fixture(tmp_path, monkeypatch)
    critic_calls = []

    def llm(prompt, inputs_, system=None, schema=None):
        if inputs_.get('_critic'):
            critic_calls.append(prompt)
            return json.dumps({'patches': [], 'score': 90, 'summary': 'ok',
                'loop_verdicts': [{'proposal_id': cb._continuity().critic_review_receipt(updates[0]),
                                   'verdict': 'approve'}]})
        return json.dumps({'markdown': '# Brief\n\nQuiet day.', 'actionItems': [],
                           'loopUpdates': updates})
    out = cb.compose(inputs, llm=llm, critic=True)
    assert len(critic_calls) == 1 and 'sent the deck' in critic_calls[0]
    assert 'loop_update_review' in critic_calls[0]
    # An explicit verdict binds approval to this exact proposal.
    assert out['loop_updates'][0]['criticReceipt']
    assert out['_loop_projection']['resolved'] == ['deck-0']


@pytest.mark.parametrize('failure', ['skipped', 'held', 'error', 'malformed', 'revise_failed'])
def test_closure_without_completed_critic_review_stays_open(tmp_path, monkeypatch, failure):
    """Fail closed for closures only: the brief still ships, the task stays open and visible."""
    inputs, updates = _closure_fixture(tmp_path, monkeypatch)
    updates.append({**updates[0], 'status': 'waiting'})  # other update kinds are unaffected
    if failure == 'skipped':
        monkeypatch.setenv('SOTTO_CRITIC', 'off')

    def llm(prompt, inputs_, system=None, schema=None):
        if inputs_.get('_critic'):
            if failure == 'held':
                raise cb.model_work.ModelWorkHeldError('attempts exhausted')
            if failure == 'error':
                raise RuntimeError('critic down')
            if failure == 'malformed':  # no patch list: the review contract was not followed
                return json.dumps({'score': 90, 'summary': 'ok'})
            return json.dumps({'patches': [{'type': 'mark_handled', 'severity': 'critical',
                                            'detail': 'deck is not sent'}], 'score': 40})
        if inputs_.get('_revise'):
            raise RuntimeError('revise down')
        return json.dumps({'markdown': '# Brief\n\nQuiet day.', 'actionItems': [],
                           'loopUpdates': updates})
    out = cb.compose(inputs, llm=llm, critic=True)
    assert out['_loop_projection']['resolved'] == []
    assert out['_loop_projection']['rejected_by_reason'].get('unreviewed_resolution') == 1
    assert [u['status'] for u in out['loop_updates']] == ['resolved', 'waiting']
    assert not any('criticReceipt' in u for u in out['loop_updates'])


def test_model_written_review_receipt_is_not_trusted(tmp_path, monkeypatch):
    inputs, updates = _closure_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv('SOTTO_CRITIC', 'off')
    forged = {**updates[0], 'criticReceipt': cb._continuity().critic_review_receipt(updates[0])}

    def llm(prompt, inputs_, system=None, schema=None):
        return json.dumps({'markdown': '# Brief', 'actionItems': [], 'loopUpdates': [forged]})
    out = cb.compose(inputs, llm=llm, critic=True)
    assert out['_loop_projection']['resolved'] == []


def test_closures_past_the_review_cap_are_held_not_applied(tmp_path, monkeypatch):
    n = cb.LOOP_REVIEW_MAX_UPDATES + 1
    inputs, updates = _closure_fixture(tmp_path, monkeypatch, n=n)
    waiting = {**updates[0], 'status': 'waiting'}
    reviewed = set()
    out = {'loop_updates': [waiting] + updates}
    review = cb._loop_update_review_context(out, inputs, reviewed)
    # Closures take review slots before a waiting update; exactly the reviewed ones get receipts.
    assert [r['loopId'] for r in review] == [u['loopId'] for u in updates[:cb.LOOP_REVIEW_MAX_UPDATES]]
    cb._stamp_loop_reviews(out, reviewed)
    assert [bool(u.get('criticReceipt')) for u in out['loop_updates']] == (
        [False] + [True] * cb.LOOP_REVIEW_MAX_UPDATES + [False])
    # Source excerpts share one fixed budget however many updates are reviewed.
    assert sum(len(e['original_context']) for r in review for e in r['evidence']) <= (
        cb.LOOP_REVIEW_BUDGET_CHARS)
    # A closure citing more messages than the critic is shown is not reviewed either.
    extra = {**updates[0], 'evidence': updates[0]['evidence'] * (cb.LOOP_REVIEW_MAX_EVIDENCE + 1)}
    reviewed = set()
    cb._loop_update_review_context({'loop_updates': [extra]}, inputs, reviewed)
    assert reviewed == set()


def test_unverified_handled_claim_is_removed_for_exact_open_task_only():
    row = {'anchor_key': 'task-one', 'status': 'open', 'contact_name': 'Robin Green',
           'summary': 'Send the pricing document'}
    sibling = {**row, 'anchor_key': 'task-two', 'summary': 'Review the draft agreement'}
    out = {'brief_markdown': '## ✅ Already Handled\n- **Robin Green**: Send the pricing document. <!--loop:task-one-->\n'
                             '## Needs Attention Now\n- **Robin Green**: Review the draft agreement.\n'}
    cb._reconcile_task_sections(out, [row, sibling], [row])
    assert 'Send the pricing document' not in out['brief_markdown']
    assert 'Review the draft agreement' in out['brief_markdown']


def test_unverified_confirmed_meeting_paraphrase_does_not_hide_separate_task():
    meeting = {'anchor_key': 'meeting-one', 'contact_name': 'Robin Green', 'status': 'open',
               'summary': 'Schedule the 9:30 meeting with Robin'}
    deck = {'anchor_key': 'deck-two', 'contact_name': 'Robin Green', 'status': 'open',
            'summary': 'Send the strategy deck to Robin'}
    out = {'brief_markdown': '## ✅ Already Handled\n**Robin Green**: The 9:30 meeting is confirmed. <!--loop:meeting-one-->\n'
                             '## Needs Attention Now\n**Robin Green**: Send the strategy deck.\n'}
    cb._reconcile_task_sections(out, [meeting, deck], [meeting, deck])
    assert '9:30 meeting is confirmed' not in out['brief_markdown']
    assert 'Send the strategy deck' in out['brief_markdown']


def test_pending_repeat_of_needs_you_is_deduped_by_task_not_person():
    first = {'anchor_key': 'contract', 'contact_name': 'Robin Green', 'status': 'open',
             'summary': 'Sign the partnership contract'}
    second = {'anchor_key': 'deck', 'contact_name': 'Robin Green', 'status': 'open',
              'summary': 'Send the strategy deck'}
    out = {'brief_markdown': '## Needs Attention Now\n- **Robin Green**: Sign the partnership contract. <!--loop:contract-->\n'
                             '**Still Pending**\n- **Robin Green**: Sign the partnership contract. <!--loop:contract-->\n'
                             '- **Robin Green**: Send the strategy deck. <!--loop:deck-->\n'}
    cb._reconcile_task_sections(out, [first, second], [first, second])
    assert out['brief_markdown'].count('Sign the partnership contract') == 1
    assert 'Send the strategy deck' in out['brief_markdown']


def test_unmarked_same_person_similar_task_is_preserved():
    open_task = {'anchor_key': 'new-pricing', 'contact_name': 'Robin Green', 'status': 'open',
                 'summary': 'Send the pricing document'}
    out = {'brief_markdown': '## ✅ Already Handled\n- **Robin Green**: Sent the pricing document.\n'}
    cb._reconcile_task_sections(out, [open_task], [open_task])
    assert 'Sent the pricing document' in out['brief_markdown']


def test_one_persons_visible_task_cannot_hide_a_different_urgent_open_task():
    first = {'anchor_key': 'walk', 'contact_name': 'Robin Green', 'status': 'open',
             'summary': 'Confirm the afternoon walk', 'chased_count': 1,
             'created_at': '2026-09-20 08:00:00'}
    second = {**first, 'anchor_key': 'deck', 'summary': 'Send the strategy deck'}
    out = {'brief_markdown': '## Should Handle Today\n- **Robin Green**: Confirm the afternoon walk. '
                             '<!--loop:walk-->\n## ✅ Already Handled\n- **Robin Green**: The deck was sent.'}
    cb._append_still_open(out, [first, second], today='2026-09-21')
    assert out['brief_markdown'].count('Confirm the afternoon walk') == 1
    assert '## Still open\n- **Robin Green**: Send the strategy deck' in out['brief_markdown']


def test_waiting_projection_rejects_replayed_promise():
    from delivery_effects import loop_version
    from render_local import message_evidence_id
    row = {'anchor_key': 'waiver', 'status': 'waiting', 'action_type': 'waiting_on',
           'contact_name': 'Robin Green', 'contact_identifier': '+14155552222',
           'channel': 'imessage', 'created_at': '2026-09-20 08:00:00',
           'last_heard_at': '2026-09-21 09:00:00', 'summary': 'Robin owes the signed waiver'}
    msg = {'rowid': 20, 'is_from_me': False, 'handle': '+14155552222',
           'timestamp': '2026-09-21 09:00:00', 'text': 'I will send the waiver Friday.'}
    update = {'loopId': 'waiver', 'loopVersion': loop_version(row), 'status': 'waiting',
              'evidence': [{'sourceType': 'imessage',
                            'sourceId': message_evidence_id(msg, 'imessage'),
                            'snippet': 'I will send the waiver Friday.'}]}
    out = {'loop_updates': [update]}
    assert cb._project_loop_updates(out, {'local': {'imessage': [msg]}, 'google': {}}, [row]) == [row]
    assert out['_loop_projection']['rejected_by_reason'] == {'replayed_promise': 1}


def test_notices_and_solo_holds_owe_no_rsvp_and_every_event_stays_scheduled():
    from datetime import datetime, timezone
    inputs = _calendar_inputs([
        {'id': 'personal', 'summary': 'Conference reception', 'start': '2026-09-21',
         'my_response': 'needsAction', 'attendees': [{'email': 'host@example.com'}]},
        {'id': 'context', 'summary': 'CONTEXT: building evacuation drill', 'start': '2026-09-21',
         'my_response': 'needsAction', 'attendees': [{'email': 'me@example.com', 'self': True}]},
        {'id': 'board', 'summary': 'HOLD: Acme board meeting', 'start': '2026-09-21',
         'my_response': 'needsAction',
         'attendees': [{'email': 'ea@acme.com'}, {'email': 'me@example.com', 'self': True}]},
    ])
    personal, context, board = inputs['google']['events']
    assert not cb._owes_no_rsvp(personal)
    assert cb._owes_no_rsvp(context)
    # A labelled hold with another attendee, or sent by another organizer, is a real invite.
    assert not cb._owes_no_rsvp(board)
    # A notice or drill owes no answer even when someone else sent it to everyone.
    drill = {**context, 'id': 'drill', 'organizer': {'email': 'facilities@example.com'},
             'attendees': [{'email': 'all@example.com'}, {'email': 'me@example.com', 'self': True}]}
    assert cb._owes_no_rsvp(drill)
    inputs['google']['events'].append(drill)
    actions = cb._rsvp_actions(inputs, datetime(2026, 9, 20, 18, tzinfo=timezone.utc))
    assert [action['contactIdentifier'] for action in actions] == ['personal', 'board']
    schedule = cb._schedule_lines(inputs, {})
    for title in ('Conference reception', 'evacuation drill', 'Acme board meeting'):
        assert any(title in line for line in schedule)


def test_accepted_prep_block_stays_in_coming_up():
    schedule = cb._schedule_lines(_calendar_inputs([
        _meeting(11, id='prep', summary='Prep: Sequoia pitch', my_response='accepted')]), {})
    assert any('Sequoia pitch' in line for line in schedule)


@pytest.mark.parametrize('critic', [False, True])
def test_compose_keeps_model_rsvp_only_for_visible_unanswered_real_events(tmp_path, monkeypatch, critic):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_CRITIC', 'always')
    inputs = _calendar_inputs([
        _meeting(10, id='context-event', summary='CONTEXT: building evacuation drill',
                 my_response='needsAction'),
        _meeting(11, id='personal-event', summary='Conference reception',
                 my_response='needsAction', attendees=[{'email': 'host@example.com'}]),
        _meeting(12, id='other-event', summary='Lunch with Robin',
                 my_response='accepted', attendees=[{'email': 'robin@example.com'}]),
        _meeting(13, id='declined-event', summary='Declined pitch',
                 my_response='declined', attendees=[{'email': 'vc@example.com'}]),
        _meeting(14, id='hold-invite', summary='HOLD: Acme board meeting',
                 my_response='needsAction', attendees=[{'email': 'ea@acme.com'}]),
    ])
    model_actions = [
        {'type': 'rsvp', 'channel': 'calendar', 'contactIdentifier': 'context-event',
         'contactName': 'Building drill', 'contextAsk': 'accept the invite'},
        {'type': 'rsvp', 'channel': 'calendar', 'contactIdentifier': 'hold-invite',
         'contactName': 'Acme board meeting', 'contextAsk': 'accept the invite'},
        {'type': 'rsvp', 'channel': 'calendar', 'contactIdentifier': 'personal-event',
         'contactName': 'Conference reception', 'contextAsk': 'accept the invite'},
        {'type': 'rsvp', 'channel': 'calendar', 'contactIdentifier': 'other-event',
         'contactName': 'Lunch with Robin', 'contextAsk': 'accept the invite'},
        {'type': 'rsvp', 'channel': 'calendar', 'contactIdentifier': 'declined-event',
         'contactName': 'Declined pitch', 'contextAsk': 'accept the invite'},
        {'type': 'reply', 'channel': 'calendar', 'contactIdentifier': 'other-event',
         'contactName': 'Robin', 'contextAsk': 'send agenda'},
    ]

    def llm(prompt, source_inputs, **kwargs):
        if source_inputs.get('_critic'):
            return json.dumps({'score': 40, 'summary': 'Tighten the brief', 'patches': [
                {'type': 'clarity', 'severity': 'minor', 'detail': 'Shorten an action'}]})
        return json.dumps({'brief_markdown': '## Needs Attention Now\nReply to Robin.',
                           'actions': model_actions})

    out = cb.compose(inputs, llm=llm, critic=critic)
    identifiers = {action.get('contactIdentifier') for action in out['actions']}
    assert not {'context-event', 'declined-event'} & identifiers
    assert not any(action.get('type') == 'rsvp' and action.get('contactIdentifier') == 'other-event'
                   for action in out['actions'])
    assert {'personal-event', 'other-event', 'hold-invite'} <= identifiers


def test_quiet_copy_only_scoped_when_another_section_has_action():
    busy = {'brief_markdown': '## Needs Attention Now\n- **Robin Green**: Reply.\n'
                              '## Should Handle Today\nNothing needs your attention right now.'}
    quiet = {'brief_markdown': 'Nothing needs your attention right now.\n## ✅ Already Handled\nQuiet.'}
    assert 'Nothing else needs your attention in this section.' in cb._finish_brief_presentation(busy)['brief_markdown']
    assert 'Nothing needs your attention right now.' in cb._finish_brief_presentation(quiet)['brief_markdown']



def test_quiet_copy_ignores_handled_items_and_reads_unbulleted_entries():
    handled = {'brief_markdown': 'Nothing needs your attention right now.\n\n'
                                 '## ✅ Already Handled\n- **Robin Green**: Sent the signed lease.\n'}
    assert cb._finish_brief_presentation(handled) == handled
    busy = {'brief_markdown': '## Needs Attention Now\n**Robin Green**<!--id:r@x.com|ch:email--> - Reply.\n'
                              '## Should Handle Today\nNothing needs your attention right now.\n'}
    assert ('## Should Handle Today\nNothing else needs your attention in this section.'
            in cb._finish_brief_presentation(busy)['brief_markdown'])


def test_task_prose_saying_still_pending_or_handled_is_not_a_section_heading():
    lease = {'anchor_key': 'lease', 'status': 'open', 'contact_name': 'Robin Green',
             'summary': 'Sign the lease'}
    deck = {'anchor_key': 'deck', 'status': 'open', 'contact_name': 'Maya Chen',
            'summary': 'Send the strategy deck'}
    md = ('## Needs Attention Now\n'
          '**Robin Green**<!--id:r@x.com|ch:email--> - The lease is still pending your signature. '
          '<!--loop:lease-->\n'
          '**Maya Chen**<!--id:m@x.com|ch:email--> - Deposit already handled; send the strategy deck. '
          '<!--loop:deck-->\n'
          '## Coming Up\n- Mon Sep 21 9 AM: Standup\n')
    out = cb._reconcile_task_sections({'brief_markdown': md}, [lease, deck], [lease, deck])
    assert out['brief_markdown'] == md
    assert '_task_section_reconciliation' not in out


def test_compose_keeps_canonical_needs_attention_entries_that_say_still_pending(monkeypatch):
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    rows = [{"anchor_key": "memo", "contact_name": "Priya Raman", "status": "open",
             "action_type": "follow_up", "channel": "email", "summary": "Send the diligence memo",
             "chased_count": 1, "created_at": _iso_hours_ago(30)},
            {"anchor_key": "loi", "contact_name": "Dana Wu", "status": "open", "action_type": "reply",
             "channel": "email", "summary": "Return the signed LOI", "created_at": _iso_hours_ago(30)}]
    md = ("## Needs Attention Now\n"
          "**Priya Raman**<!--id:p@x.com|ch:email--> - The diligence memo is still pending; she "
          "needs it before Thursday's IC. Send the diligence memo. <!--loop:memo-->\n\n"
          "**Dana Wu**<!--id:dana@x.com|ch:email--> - Return the signed LOI today so the close "
          "stays on track. <!--loop:loi-->\n\n## Filtered\n\n12 promotional emails\n")
    out = cb.compose({"type": "morning", "google": {}, "local": {"action_ledger": rows}},
                     llm=lambda *a, **k: json.dumps({"brief_markdown": md, "actions": []}),
                     critic=True)
    assert "before Thursday's IC" in out["brief_markdown"]
    assert "signed LOI today" in out["brief_markdown"]
    assert "_task_section_reconciliation" not in out


def test_paraphrased_anchored_task_without_marker_is_not_repeated_and_is_recorded(
        tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    row = {"anchor_key": "memo", "contact_name": "Priya Raman", "status": "open",
           "action_type": "follow_up", "channel": "email", "summary": "Send the diligence memo",
           "chased_count": 1, "created_at": _iso_hours_ago(30)}
    import loops_query
    import ledger_provenance
    monkeypatch.setattr(loops_query, 'permitted_rows', lambda _root, _sources: [row])
    monkeypatch.setattr(ledger_provenance, 'source_loop_sources', lambda _row: frozenset({'calls'}))
    md = ("## Should Handle Today\n**Priya Raman**<!--id:p@x.com|ch:email--> is still waiting on "
          "the diligence memo you promised; send it today.\n")
    out = cb.compose({"type": "morning", "google": {}, "local": {"action_ledger": [row]}},
                     llm=lambda *a, **k: json.dumps({"brief_markdown": md, "actions": []}),
                     critic=True)
    assert out["brief_markdown"].count("Priya Raman") == 1
    assert cb._visible_open_task_keys(md, [row]) == {"memo"}
    named = list((tmp_path / "briefs").glob("*.named.json"))
    assert [json.loads(p.read_text())["anchor_keys"] for p in named] == [["memo"]]
    # A marker for this task elsewhere (here: a handled claim) means only the marked line counts.
    marked = md + "## ✅ Already Handled\n- **Priya Raman**: memo sent. <!--loop:memo-->\n"
    assert cb._visible_open_task_keys(marked, [row]) == set()


@pytest.mark.parametrize('bad_actions', [["reply to Robin"], {"type": "reply"}, [{"type": "reply"}, 7]])
def test_malformed_revision_actions_keep_the_validated_draft(monkeypatch, tmp_path, bad_actions):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_CRITIC', 'always')

    def llm(prompt, inputs, system=None, schema=None):
        if inputs.get('_critic'):
            return json.dumps({'score': 40, 'summary': 'x', 'patches': [
                {'type': 'add_item', 'severity': 'critical', 'detail': 'add Robin'}]})
        if inputs.get('_revise'):
            return json.dumps({'brief_markdown': '## Needs Attention Now\n- **Robin**: revised.',
                               'actions': bad_actions, 'loop_updates': []})
        return json.dumps({'brief_markdown': '## Needs Attention Now\nReply to Robin.', 'actions': []})

    out = cb.compose({'type': 'morning', 'google': {}, 'local': {}}, llm=llm, critic=True)
    assert 'Reply to Robin.' in out['brief_markdown'] and 'revised.' not in out['brief_markdown']
    assert all(isinstance(action, dict) for action in out['actions'])


def test_rsvp_filter_keeps_only_action_objects():
    keep = {'type': 'reply', 'contactIdentifier': 'x'}
    assert cb._drop_informational_rsvp_actions(['reply', keep, None], {}) == [keep]
    assert cb._drop_informational_rsvp_actions({'type': 'reply'}, {}) == []


def test_legacy_ask_only_loop_is_counted_only_when_its_actual_ask_is_shown():
    from test_visual_brief import BRIEF
    row = {'anchor_key': 'legacy-ask', 'contact_name': 'Robin Green', 'status': 'open',
           'ask': 'Send the planning notes next month.'}
    sibling = {**row, 'anchor_key': 'other-ask', 'ask': 'Review the revised contract.'}
    out = cb._append_still_open({'brief_markdown': BRIEF}, [row], today='2026-09-20')
    cb._expand_quiet_loops(out, [row], today='2026-09-20')
    assert row['ask'] in out['brief_markdown']
    assert cb._represented_loop_rows([row, sibling], out['brief_markdown']) == [row]
    # A name and hidden marker beside unrelated prose do not count as showing the obligation.
    unrelated = f"Robin Green: Happy birthday. <!--loop:{row['anchor_key']}-->"
    assert not cb._represented_loop_rows([row], unrelated)


# The source-backed schedule survives both an incomplete extraction and a lossy revision.
def _calendar_inputs(events, **extra):
    return {'type': 'morning', 'first_run': False, 'now': '2026-09-21T13:30:00Z',
            'google': {'userTimezone': 'America/Los_Angeles', 'events': events},
            'local': {}, **extra}


def _meeting(hour=9, **extra):
    return {'id': f'event-{hour}', 'summary': f'Planning {hour}',
            'start': f'2026-09-21T{hour:02}:00:00-07:00',
            'end': f'2026-09-21T{hour:02}:45:00-07:00', **extra}


@pytest.mark.parametrize('critic', [False, True])
def test_schedule_survives_missing_extraction_and_lossy_revision(tmp_path, monkeypatch, critic):
    import visual_brief
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_CRITIC', 'always')
    calls = []
    def llm(prompt, inputs, **kwargs):
        calls.append('critic' if inputs.get('_critic') else 'revise' if inputs.get('_revise') else 'extract')
        if inputs.get('_critic'):
            return json.dumps({'score': 45, 'summary': 'Calendar omitted', 'patches': [
                {'type': 'missing', 'severity': 'critical', 'detail': 'Restore schedule'}]})
        return json.dumps({'brief_markdown': '## Needs Attention Now\nReview the signed offer.\n\n'
                           '## Still Open\nJordan: Send the requested notes.', 'actions': []})
    inputs = _calendar_inputs([_meeting(h) for h in reversed(range(8, 16))])
    out = cb.compose(inputs, llm=llm, critic=critic)
    assert calls == (['extract', 'critic', 'revise'] if critic else ['extract'])
    for h in range(8, 13):
        assert f'Planning {h}' in out['brief_text']
    assert 'Planning 13' not in out['brief_text']
    assert not cb.brief_validate._check_coming_up_length(out['brief_markdown'])
    deck = visual_brief.build(out['brief_text'])
    assert deck and len(deck['cards']) == 4
    assert sum(len(c['blocks']) for c in deck['cards'] if c['title'] == 'Your day') == 5


def test_schedule_replaces_partial_wrong_and_duplicate_sections_without_losing_other_sections(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    inputs = _calendar_inputs([_meeting(location='14 Main St')])
    original = {'brief_markdown': '## Needs Attention Now\nReply to Alex.\n\n'
                '**Coming Up**\n- 2:00 PM: Invented meeting\n\n'
                '## Already Handled\nAlex: Replied.\n\n'
                '## Coming Up\n- 3:00 PM: Duplicate\n\n'
                '## Filtered\n2 newsletters'}
    result = cb._ensure_schedule(original, inputs)
    assert result['brief_markdown'].count('Coming Up') == 1
    assert '9:00 AM: Planning 9 | Location: 14 Main St' in result['brief_markdown']
    assert 'Invented' not in result['brief_markdown'] and 'Duplicate' not in result['brief_markdown']
    for text in ('Reply to Alex.', 'Alex: Replied.', '2 newsletters'):
        assert text in result['brief_markdown']
    assert cb._ensure_schedule(result, inputs) == result


def test_schedule_excludes_past_declined_cancelled_invalid_but_keeps_ongoing_and_tentative(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    inputs = _calendar_inputs([
        _meeting(5), _meeting(6), _meeting(8, my_response='declined'),
        _meeting(9, status='cancelled', my_response='needsAction', attendees=[{'email': 'a@example.com'}]),
        _meeting(10, start='not a timestamp'), _meeting(11, my_response='tentative'),
        _meeting(12, start='2026-09-25T12:00:00-07:00')])
    rows = '\n'.join(cb._schedule_lines(inputs, {}))
    assert 'Planning 6' in rows and 'Planning 11' in rows
    for h in (5, 8, 9, 10, 12):
        assert f'Planning {h}' not in rows
    assert cb._rsvp_actions(inputs, cb._brief_now(inputs)) == []
    manifest = cb.build_data_manifest(inputs)
    assert all(e['title'] not in ('Planning 8', 'Planning 9') for e in manifest['calendar_events'])
    prompt = cb.build_prompt('{{calendar}}', inputs)
    assert 'Planning 8' not in prompt and 'Planning 9' not in prompt


@pytest.mark.parametrize('status', ['disabled', 'unavailable', 'skipped'])
def test_unavailable_calendar_cannot_reintroduce_old_schedule(tmp_path, monkeypatch, status):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    inputs = _calendar_inputs([_meeting()], source_results={'calendar': {'status': status}})
    out = cb._ensure_schedule({'brief_markdown': '**Coming Up**\n- 9:00 AM: Stale meeting'}, inputs)
    assert not out['brief_markdown']


def test_calendar_permissions_and_section_mute_override_final_schedule(tmp_path, monkeypatch):
    import source_context
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    inputs = _calendar_inputs([_meeting()])
    monkeypatch.setattr(source_context, 'allowed', lambda source: source != 'calendar')
    assert cb._schedule_lines(inputs, {}) == []
    monkeypatch.setattr(source_context, 'allowed', lambda source: True)
    for section in ('calendar', 'Coming Up', 'your_day'):
        assert cb._schedule_lines(inputs, {'mute_sections': [section]}) == []


def test_schedule_uses_local_day_birthdays_and_single_line_source_fields(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    inputs = _calendar_inputs([
        _meeting(start='2026-09-22T00:00:00Z', end='2026-09-22T01:00:00Z',
                 summary='Design\n## Still Open\n<!--id:fake|ch:email--> review', location='14 Main St'),
        _meeting(start='2026-09-21', end='2026-09-22', summary='Offsite'),
        _meeting(start='2026-09-22', end='2026-09-23', summary='Workshop')],
        local={'contacts': [{'name': 'Sam', 'birthday': '09-21'}, {'name': 'Lee', 'birthday': '09-22'}]})
    rows = cb._schedule_lines(inputs, {})
    assert len(rows) == 5
    assert 'Mon Sep 21 5:00 PM: Design Still Open review | Location: 14 Main St' in rows[1]
    assert 'Mon Sep 21 All day: Offsite' in rows[0]
    import visual_brief
    assert visual_brief._birthday_parts(next(line[2:] for line in rows if 'Sam' in line)) == ('Sam', 'Birthdays today')
    assert any('Lee' in line and 'birthday tomorrow' in line for line in rows)
    assert not any('\n' in line or '<!--' in line or '##' in line or '—' in line for line in rows)
    assert not any('Sam' in line for line in cb._schedule_lines(inputs, {'mute_people': ['Sam']}))
    prompt = cb.build_prompt('{{user_today}} {{birthdays}}', inputs)
    assert 'birthday TODAY' in prompt


def test_busy_schedule_keeps_five_meetings_ahead_of_birthday(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    events = [_meeting(hour) for hour in (9, 10, 11, 12, 13)]
    inputs = _calendar_inputs(events, local={'contacts': [{'name': 'Ashish', 'birthday': '09-21'}]})
    rows = cb._schedule_lines(inputs, {})
    assert len(rows) == 5 and all('Planning' in row for row in rows)
    assert 'Ashish' in '\n'.join(cb._schedule_lines(_calendar_inputs(events[:2], local={
        'contacts': [{'name': 'Ashish', 'birthday': '09-21'}]}), {}))


def test_schedule_keeps_first_brief_closing_offer_and_welcome_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    original = {'brief_markdown': '**Coming Up**\n- 9:00 AM: Old meeting\n\n'
                'Ask me to draft 3 replies today.\n- You can ask about tomorrow too.'}
    out = cb._ensure_schedule(original, _calendar_inputs([_meeting()], first_run=True))
    assert 'Ask me to draft 3 replies today.\n- You can ask about tomorrow too.' in out['brief_markdown']
    assert cb._ensure_schedule(original, _calendar_inputs([_meeting()], type='welcome')) == original


def test_schedule_preserves_unheaded_tasks_after_calendar_block(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    original = {'brief_markdown': ('## Coming Up\n'
        '- Ashish · birthday today\n\n'
        '- Charley: Send the signed contract. <!--loop:contract-->\n'
        '## ✅ Already Handled\n- A sourced item.')}
    out = cb._ensure_schedule(original, _calendar_inputs([_meeting()], local={
        'contacts': [{'name': 'Ashish', 'birthday': '09-21'}]}))
    assert 'Ashish' in out['brief_markdown']
    assert 'Planning 9' in out['brief_markdown']
    assert 'Charley: Send the signed contract.' in out['brief_markdown']
    assert 'A sourced item.' in out['brief_markdown']


def test_injected_ledger_hides_only_unsourced_brief_birthday_courtesy():
    courtesy = {'anchor_key': 'patrick', 'status': 'open', 'contact_name': 'Patrick',
                'summary': 'Send Patrick a belated birthday greeting',
                'source_brief_at': '2026-10-04 08:00:00'}
    owner = {**courtesy, 'anchor_key': 'owner', 'source': 'user_added'}
    requested = {**courtesy, 'anchor_key': 'requested',
                 'source_refs': [{'sourceType': 'imessage', 'sourceId': '7'}]}
    inputs = {'type': 'morning', 'local': {'action_ledger': [courtesy, owner, requested]}}
    rows = cb._normalize_local(inputs)['action_ledger']
    assert [row['anchor_key'] for row in rows] == ['owner', 'requested']
    assert 'patrick' not in cb._format_action_ledger({'action_ledger': [courtesy]}, cb._brief_now(inputs))


def test_same_person_and_topic_do_not_hide_a_distinct_future_call():
    row = {'anchor_key': 'november-call', 'status': 'open', 'contact_name': 'Dana',
           'contact_identifier': 'dana@example.com',
           'summary': 'Schedule November planning call regarding Project Cedar',
           'created_at': '2026-10-05', 'deadline': '2026-10-06'}
    md = ('## Should Handle Today\n- Dana: Call now regarding Project Cedar. '
          '<!--id:dana@example.com-->\n')
    assert cb._visible_open_task_keys(md, [row]) == set()


def test_screenshot_paraphrases_appear_once_before_canonical_followups_are_appended():
    rows = [
        {'anchor_key': 'nika', 'contact_name': 'Austin Park', 'canonical_id': 'person-austin',
         'contact_identifier': 'austin@example.com', 'status': 'open', 'chased_count': 1,
         'summary': 'Reply to Austin about the new startup Project Cedar and share your thoughts',
         'created_at': '2026-10-01'},
        {'anchor_key': 'telecom', 'contact_name': 'Jordan Lake', 'canonical_id': 'person-jackson',
         'contact_identifier': 'jackson@example.com', 'status': 'open', 'chased_count': 1,
         'summary': 'Jordan Lake asked if your team has US telecom contacts for LineForge '
                    'after sending the Orbit CIO details.', 'created_at': '2026-10-01'},
        {'anchor_key': 'contract', 'contact_name': 'Austin Park', 'canonical_id': 'person-austin',
         'contact_identifier': 'austin@example.com', 'status': 'open', 'chased_count': 1,
         'summary': 'Sign the services contract', 'created_at': '2026-10-02'}]
    md = ('## Needs you\n'
          '- **Austin Park**: Your thoughts on Project Cedar are still owed. <!--loop:nika-->\n'
          '- **Jordan Lake**: Nudged you at 7:00 AM and 9:00 AM about Jordan’s request for '
          'US telecom contacts; still open unless you handled it. <!--loop:telecom-->\n'
          '## Follow-ups\n'
          '- **Austin Park**: Reply about Project Cedar. <!--loop:nika-->\n'
          '- **Jordan Lake**: Jordan Lake’s request for US telecom contacts remains open '
          'after this morning’s nudges. <!--loop:telecom-->\n'
          '- **Austin Park**: Reply to Austin about the new startup Project Cedar and share your '
          'thoughts. <!--loop:nika-->\n'
          '- **Jordan Lake**: Jordan Lake asked if your team has US telecom contacts for '
          'LineForge after sending the Orbit CIO details. <!--loop:telecom-->\n'
          '## Filtered\nPromotions\n')
    original = json.loads(json.dumps(rows))
    out = cb._append_still_open({'brief_markdown': md}, rows, today='2026-10-10')
    text = out['brief_markdown']
    # Earlier partial mentions keep their section and place; lines that add anything (here,
    # when it was nudged) stay. Only bare repeats after a full appearance go.
    assert text.count('Reply to Austin about the new startup Project Cedar') == 1
    assert text.count('US telecom contacts for LineForge') == 1
    assert 'Sign the services contract' in text
    assert cb._visible_open_task_keys(text, rows) == {'nika', 'telecom', 'contract'}
    assert rows == original  # Presentation never resolves or edits a canonical obligation.


def test_duplicate_followup_is_removed_without_removing_separate_ask_for_same_person():
    first = {'anchor_key': 'deck', 'contact_name': 'Robin Green', 'canonical_id': 'person-robin',
             'status': 'open', 'summary': 'Send the strategy deck'}
    second = {**first, 'anchor_key': 'contract', 'summary': 'Sign the partnership contract'}
    md = ('## Follow-ups\n- **Robin Green**: The strategy deck is still owed.\n'
          '- **Robin Green**: Send the strategy deck.\n'
          '- **Robin Green**: Sign the partnership contract.\n')
    text = cb._dedupe_open_task_lines(md, [first, second])
    assert text.count('strategy deck') == 1
    assert 'Sign the partnership contract' in text


def test_ambiguous_same_person_topics_and_same_name_identities_are_not_deduped():
    first = {'anchor_key': 'deck', 'contact_name': 'Robin Green', 'canonical_id': 'person-robin',
             'contact_identifier': 'robin@example.com', 'status': 'open',
             'summary': 'Send the Project Cedar strategy deck'}
    second = {**first, 'anchor_key': 'contract', 'summary': 'Sign the Project Cedar contract'}
    md = ('## Needs you\n- **Robin Green**: Project Cedar needs a decision.\n'
          '## Follow-ups\n- **Robin Green**: Project Cedar needs a decision.\n')
    assert cb._dedupe_open_task_lines(md, [first, second]) == md
    assert cb._visible_open_task_keys(md, [first, second]) == set()
    collision = {**first, 'anchor_key': 'another-deck', 'canonical_id': 'another-robin',
                 'contact_identifier': 'other-robin@example.com'}
    same_name = md.replace('Project Cedar needs a decision', 'The Project Cedar strategy deck is owed')
    assert cb._dedupe_open_task_lines(same_name, [first, collision]) == same_name
    assert cb._visible_open_task_keys(same_name, [first, collision]) == set()


def test_exact_contact_marker_disambiguates_names_but_multitask_lines_stay_intact():
    first = {'anchor_key': 'deck', 'contact_name': 'Robin Green', 'canonical_id': 'person-robin',
             'contact_identifier': 'robin@example.com', 'status': 'open',
             'summary': 'Send the strategy deck'}
    other = {**first, 'anchor_key': 'other-deck', 'canonical_id': 'other-robin',
             'contact_identifier': 'other@example.com'}
    line = '- **Robin Green**<!--id:robin@example.com-->: The strategy deck is still owed.\n'
    md = '## Needs you\n' + line + '## Follow-ups\n' + line
    assert cb._dedupe_open_task_lines(md, [first, other]).count('strategy deck') == 1
    contract = {**first, 'anchor_key': 'contract', 'summary': 'Sign the partnership contract'}
    combined = '- **Robin Green**: Send the strategy deck and sign the partnership contract.\n'
    mixed = ('## Needs you\n- **Robin Green**: Send the strategy deck. <!--loop:deck-->\n'
             '## Follow-ups\n' + combined)
    assert combined in cb._dedupe_open_task_lines(mixed, [first, contract])


def test_shared_contract_words_cannot_remove_a_different_unmarked_obligation():
    row = {'anchor_key': 'acquisition', 'contact_name': 'Dana Wu', 'canonical_id': 'person-dana',
           'contact_identifier': 'dana@example.com', 'status': 'open',
           'summary': 'Send the signed acquisition contract'}
    first = '- **Dana Wu**<!--id:dana@example.com-->: Send the signed acquisition contract.\n'
    rental = '- **Dana Wu**<!--id:dana@example.com-->: Send the signed rental contract.\n'
    md = '## Needs you\n' + first + '## Follow-ups\n' + rental
    assert cb._dedupe_open_task_lines(md, [row]) == md
    assert cb._line_open_task_keys(rental, [row]) == set()


def test_unreviewed_precise_ask_cannot_hide_broader_saved_summary():
    import copy
    row = {'anchor_key': 'market-map', 'contact_name': 'Robin Green', 'canonical_id': 'c_abc123',
           'contact_identifier': 'robin@example.test', 'status': 'open', 'deadline': '2026-09-20',
           'summary': 'Robin discussed the acquisition pipeline on August 6, 2026 and requested materials',
           'ask': 'Send the updated market map'}
    original = copy.deepcopy(row)
    first = 'Robin Green<!--id:robin@example.test-->: Send the updated market map. <!--loop:market-map-->\n'
    main = '## Needs Attention Now\n' + first
    duplicate = '## Still open\nRobin Green: ' + row['summary'] + '. <!--loop:market-map-->\n'
    assert cb._line_open_task_keys(first, [row]) == set()
    assert cb._dedupe_open_task_lines(main + duplicate, [row]) == main + duplicate
    # A precise ask alone cannot prove whether the saved summary's extra scope is stale.
    out = {'brief_markdown': main}
    cb._append_still_open(out, [row], today='2026-09-21')
    assert row['summary'] in out['brief_markdown'] and '## Still open' in out['brief_markdown']
    assert row == original


@pytest.mark.parametrize('boundary', ['unmarked', 'wrong_topic', 'wrong_date', 'ambiguous_identity'])
def test_precise_ask_fallback_requires_exact_task_and_identity(boundary):
    row = {'anchor_key': 'market-map', 'contact_name': 'Robin Green', 'canonical_id': 'c_abc123',
           'contact_identifier': 'robin@example.test', 'status': 'open',
           'summary': 'Robin discussed the acquisition pipeline on August 6, 2026 and requested materials',
           'ask': 'Send the updated market map'}
    rows = [row]
    line = 'Robin Green<!--id:robin@example.test-->: Send the updated market map. <!--loop:market-map-->'
    if boundary == 'unmarked':
        line = line.replace('<!--loop:market-map-->', '')
    elif boundary == 'wrong_topic':
        line = line.replace('updated market map', 'signed rental contract')
    elif boundary == 'wrong_date':
        row['ask'] += ' for October 12, 2026'
        line = line.replace('market map.', 'market map for October 13, 2026.')
    else:
        rows.append({**row, 'anchor_key': 'other-market-map', 'canonical_id': 'c_def456'})
    assert cb._line_open_task_keys(line, rows) == set()


@pytest.mark.parametrize('heading', ['## Still Pending', '**Still Pending**',
                                     '## Follow-ups', '**Follow-ups**', '## Still open'])
def test_only_duplicate_pruned_empty_followup_headers_are_removed(heading):
    row = {'anchor_key': 'deck', 'contact_name': 'Robin Green', 'status': 'open',
           'summary': 'Send the strategy deck'}
    line = '- **Robin Green**: Send the strategy deck. <!--loop:deck-->\n'
    main = '## Needs you\n' + line + '\n'
    suffix = '## Notes\n\n## Already Handled\n\n'
    md = main + heading + '\n\n' + line + '\n' + suffix
    assert cb._dedupe_open_task_lines(md, [row]) == main + suffix
    # Empty input sections and sections with any remaining content keep their heading.
    empty = main + heading + '\n\n' + suffix
    assert cb._dedupe_open_task_lines(empty, [row]) == empty
    retained = main + heading + '\n' + line + 'A separate unbound request remains.\n'
    assert heading in cb._dedupe_open_task_lines(retained, [row])
    assert cb._dedupe_open_task_lines(main + heading + '\n' + line, [row]) == main


@pytest.mark.parametrize('verdict', [None, 'reject', 'duplicate', 'unknown'])
def test_rejected_or_unbound_critic_verdict_never_closes_repeated_revision(tmp_path, monkeypatch, verdict):
    inputs, updates = _closure_fixture(tmp_path, monkeypatch)
    ident = cb._continuity().critic_review_receipt(updates[0])
    def llm(prompt, data, **kwargs):
        if data.get('_critic'):
            votes = [] if verdict is None else [{'proposal_id': ident, 'verdict': verdict}]
            if verdict == 'duplicate':
                votes = [{'proposal_id': ident, 'verdict': v} for v in ('approve', 'reject')]
            return json.dumps({'patches': [{'type': 'mark_handled', 'severity': 'critical',
                'detail': 'Reject closure: email says it has NOT been sent.'}],
                'score': 20, 'loop_verdicts': votes})
        return json.dumps({'markdown': '# Brief\n\nQuiet day.', 'actionItems': [], 'loopUpdates': updates})
    result = cb.compose(inputs, llm=llm, critic=True)
    assert result['_loop_projection']['resolved'] == []
    assert not result['loop_updates'][0].get('criticReceipt')


def test_critic_verdict_is_bound_to_exact_proposal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    first = {'loopId': 'one', 'loopVersion': 'v1', 'status': 'resolved', 'evidence': []}
    ident = cb._continuity().critic_review_receipt(first)
    approved = cb._approved_loop_reviews({'loop_verdicts': [
        {'proposal_id': ident, 'verdict': 'approve'},
        {'proposal_id': 'not-presented', 'verdict': 'approve'}]}, {ident})
    out = {'loop_updates': [first, {**first, 'loopVersion': 'v2'}]}
    cb._stamp_loop_reviews(out, approved)
    assert out['loop_updates'][0].get('criticReceipt') == ident
    assert not out['loop_updates'][1].get('criticReceipt')


@pytest.mark.parametrize('original_decision', ['none', 'approve', 'reject'])
@pytest.mark.parametrize('decision', ['approve', 'reject', 'missing', 'old_proof', 'unbound', 'error', 'held', 'source_failure'])
def test_revision_closure_gets_one_exact_source_bound_review(
        tmp_path, monkeypatch, original_decision, decision):
    """A prose mark_handled repair cannot approve its newly structured closure."""
    from copy import deepcopy
    inputs, updates = _closure_fixture(tmp_path, monkeypatch, n=2)
    inputs['google']['emails'][0]['body'] = 'I sent the deck. Delivery is complete.'
    original = deepcopy(updates[0])
    revised = deepcopy(original)
    revised['evidence'][0]['snippet'] = 'I sent the deck.'
    if decision == 'unbound':
        revised['evidence'][0]['sourceId'] = 'missing-source'
    old_id = cb._continuity().critic_review_receipt(original)
    revised_id = cb._continuity().critic_review_receipt(revised)
    original_proposal = original_decision != 'none'
    prior_summary = 'Previous proof rejected as incomplete.' if original_decision == 'reject' else 'First source review.'
    calls = []

    def llm(prompt, data, **kwargs):
        if data.get('_critic'):
            calls.append('critic')
            manifest = json.loads(prompt.split('## DATA MANIFEST\n', 1)[1].split(
                '\n\n## GENERATED BRIEF', 1)[0])
            if calls.count('critic') == 1:
                return json.dumps({'patches': [{'type': 'mark_handled', 'severity': 'moderate',
                    'detail': 'The deck was sent. Move it to Already Handled.'}], 'score': 60,
                    'summary': prior_summary,
                    'loop_verdicts': [{'proposal_id': old_id, 'verdict': original_decision}]
                        if original_proposal else []})
            assert manifest['loop_update_review'][0]['proposal_id'] == revised_id
            assert manifest['loop_update_review'][0]['evidence'][0]['original_context'] == inputs['google']['emails'][0]['body']
            prior = manifest['prior_loop_update_review']
            assert prior['summary'] == prior_summary
            assert prior['verdicts'] == ([{'proposal_id': old_id, 'verdict': original_decision}]
                                        if original_proposal else [])
            assert [r['proposal_id'] for r in prior['proposals']] == ([old_id] if original_proposal else [])
            if decision == 'error':
                raise RuntimeError('offline reviewer unavailable')
            if decision == 'held':
                raise cb.model_work.ModelWorkHeldError('attempts exhausted')
            # An actionable response must not trigger another revision/review cycle.
            return json.dumps({'patches': [{'type': 'mark_handled', 'severity': 'moderate',
                'detail': 'No recursive repair.'}], 'score': 90,
                'loop_verdicts': [] if decision == 'missing' else [{
                    'proposal_id': old_id if decision == 'old_proof' else revised_id,
                    'verdict': 'reject' if decision == 'reject' else 'approve'}]})
        if data.get('_revise'):
            calls.append('revise')
            if decision == 'source_failure':
                def unavailable(_):
                    raise RuntimeError('offline source index unavailable')
                monkeypatch.setattr(cb, '_review_message_sources', unavailable)
            return json.dumps({'markdown': '## Already Handled\n- Dana: Deck sent. <!--loop:deck-0-->',
                              'actionItems': [], 'loopUpdates': [revised]})
        calls.append('extract')
        return json.dumps({'markdown': '## Should Handle Today\n- Dana: Send the deck. <!--loop:deck-0-->',
                          'actionItems': [], 'loopUpdates': [original] if original_proposal else []})

    out = cb.compose(inputs, llm=llm, critic=True)
    assert calls == ['extract', 'critic', 'revise'] + ([] if decision in ('unbound', 'source_failure') else ['critic'])
    approved = decision == 'approve'
    assert out['_loop_projection']['resolved'] == (['deck-0'] if approved else [])
    assert bool(out['loop_updates'][0].get('criticReceipt')) is approved
    assert out['_critic']['revision_closure_review']['approved'] == int(approved)
    if not approved:
        assert 'Deck sent' not in out['brief_markdown']
        assert 'deck-0' in out['_held_loop_ids']
    # The projection consumes a receipt only for this proof and leaves its sibling open.
    if approved:
        changed = deepcopy(out['loop_updates'][0])
        changed['evidence'][0]['snippet'] = 'Delivery is complete.'
        projected = cb._project_loop_updates({'loop_updates': [changed]}, inputs, inputs['local']['action_ledger'])
        assert {row['anchor_key'] for row in projected} == {'deck-0', 'deck-1'}


def test_supplemental_email_completion_is_shared_by_critic_and_resolver(tmp_path, monkeypatch):
    import loop_context
    import source_context
    inputs, updates = _closure_fixture(tmp_path, monkeypatch)
    email = inputs['google']['emails'][0]
    email['body'] = 'Hello Dana.'
    full = 'Hello Dana. I sent the deck as promised.'
    refreshed = {**email, 'body': full, '_loop_context': True,
                 '_loop_context_sources': ['gmail', 'imessage']}
    inputs['google']['emails'] = loop_context.merge_context([email], [refreshed])
    update = updates[0]
    update['evidence'][0]['snippet'] = 'I sent the deck as promised.'
    monkeypatch.setattr(source_context, 'allowed', lambda _: True)
    reviews = cb._loop_update_review_context({'loop_updates': updates}, inputs)
    assert reviews[0]['evidence'][0]['original_context'] == full
    update['criticReceipt'] = cb._continuity().record_critic_approval(update)
    local = {**inputs['local'], 'emails': inputs['google']['emails']}
    proof, reason = cb._continuity()._completion_evidence_result(inputs['local']['action_ledger'][0], update, local, {})
    assert proof is not None, reason
    monkeypatch.setattr(source_context, 'allowed', lambda source: source != 'imessage')
    reviews = cb._loop_update_review_context({'loop_updates': updates}, inputs)
    assert reviews[0]['evidence'][0]['original_context'] == 'Hello Dana.'
    proof, reason = cb._continuity()._completion_evidence_result(inputs['local']['action_ledger'][0], update, local, {})
    assert proof is None and reason == 'quote_mismatch'


@pytest.mark.parametrize('sent', [False, True])
def test_loop_selected_inbox_or_sent_email_keeps_its_late_lines_in_the_source_prompt(
        tmp_path, monkeypatch, sent):
    """The loop lane re-reading an inbox/sent message must not shrink it to the 2,000-char excerpt:
    a new ask (or a completion line) late in the reply has to reach the writer (and revise) prompt."""
    import loop_context
    import source_context
    inputs, _ = _closure_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(source_context, 'allowed', lambda _: True)
    tail = 'NEW ASK: please also send the signed SOW by Friday.'
    prior = {'id': 'm-long', 'threadId': 't-long', 'subject': 'Deck', 'date': '2026-09-19T09:00:00Z',
             'from': 'Me <me@x.com>' if sent else 'Dana <dana@acme.com>',
             'to': 'Dana <dana@acme.com>' if sent else 'me@x.com',
             'labelIds': ['SENT'] if sent else ['INBOX', 'UNREAD'], 'isSent': sent,
             'body': 'Context paragraph. ' * 200 + tail}
    fresh = {**prior, 'body': prior['body'][:2000], '_loop_context': True,
             '_loop_context_sources': ['gmail']}
    inputs['google']['emails'] = loop_context.merge_context([prior], [fresh])
    prompts = []

    def llm(prompt, data, **kwargs):
        prompts.append((bool(data.get('_critic')), prompt))
        if data.get('_critic'):
            return json.dumps({'patches': [], 'score': 90, 'loop_verdicts': []})
        return json.dumps({'markdown': '# Brief\n\nQuiet day.', 'actionItems': [], 'loopUpdates': []})

    cb.compose(inputs, llm=llm, critic=True)
    writer = [p for critic, p in prompts if not critic]
    assert writer and tail in writer[0]
    trimmed = cb._trim_email(inputs['google']['emails'][0])
    assert tail in trimmed['body'] and len(trimmed['_loop_context_body']) <= 2000


@pytest.mark.parametrize('revision', ['none', 'repeats', 'drops_update', 'invalid'])
def test_rejected_closure_cannot_survive_as_unmarked_handled_prose(tmp_path, monkeypatch, revision):
    inputs, updates = _closure_fixture(tmp_path, monkeypatch)
    draft = {'brief_markdown': '## Already Handled\n- Dana: The deck was sent.\n\n## Coming Up\nQuiet.',
             'actions': [], 'loop_updates': updates}
    ident = cb._continuity().critic_review_receipt(updates[0])
    def llm(prompt, data, **kwargs):
        if data.get('_critic'):
            return json.dumps({'patches': [] if revision == 'none' else [
                {'type': 'remove_item', 'severity': 'critical', 'detail': 'Remove unsupported completion.'}],
                'score': 30, 'loop_verdicts': [{'proposal_id': ident, 'verdict': 'reject'}]})
        if revision == 'invalid':
            return '{}'
        return json.dumps({**draft, 'loop_updates': [] if revision == 'drops_update' else updates})
    result = cb.critique_and_revise(draft, inputs, llm=llm)
    cb._stamp_loop_reviews(result, result.pop('_approved_loop_receipts', set()))
    ledger = inputs['local']['action_ledger']
    projected = cb._project_loop_updates(result, inputs, ledger)
    cb._reconcile_task_sections(result, ledger, projected)
    assert projected == ledger
    assert 'The deck was sent' not in result['brief_markdown']
    assert 'Coming Up' in result['brief_markdown']


@pytest.mark.parametrize("failure", ["disk", "corrupt"])
def test_review_record_failure_keeps_delivery_available_and_task_open(tmp_path, monkeypatch, failure):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    update = {'loopId': 'one', 'loopVersion': 'v1', 'status': 'resolved', 'evidence': []}
    ident = cb._continuity().critic_review_receipt(update)
    def unavailable(_):
        if failure == 'corrupt':
            raise cb._continuity().jsonstore.Unreadable('corrupt review record')
        raise OSError('disk unavailable')
    monkeypatch.setattr(cb._continuity(), 'record_critic_approval', unavailable)
    out = {'brief_markdown': 'The rest of the brief.', 'loop_updates': [update], '_critic': {}}
    cb._stamp_loop_reviews(out, {ident})
    assert not out['loop_updates'][0].get('criticReceipt')
    assert out['_critic']['unreviewed_closures'] == 1
    assert out['brief_markdown'] == 'The rest of the brief.'


def test_held_completion_omits_unbound_continuations_but_keeps_verified_task():
    ledger = [{'anchor_key': 'open'}, {'anchor_key': 'closed'}]
    out = {'_held_loop_ids': ['open'], 'brief_markdown':
           '## Already Handled\nThe deck was sent.\n  Everyone has it now.\n'
           '- Different verified task. <!--loop:closed-->\n\n## Coming Up\nKeep this.'}
    cb._reconcile_task_sections(out, ledger, ledger[:1])
    assert 'deck' not in out['brief_markdown'] and 'Everyone' not in out['brief_markdown']
    assert 'Different verified task' in out['brief_markdown'] and 'Keep this' in out['brief_markdown']


def test_retained_relative_loop_is_attributed_on_october_six_without_rewriting_october_four():
    from datetime import datetime, timezone
    from copy import deepcopy
    now = datetime(2026, 10, 6, 20, tzinfo=timezone.utc)
    row = {'anchor_key': 'birthday-old', 'status': 'open', 'contact_name': 'Alex Chen',
           'summary': 'Wish Alex a happy birthday yesterday on October 4',
           'created_at': '2026-10-05', 'action_type': 'followup'}
    before = deepcopy(row)
    line = cb._open_loop_line([row], now)
    assert 'From Oct 5: “Wish Alex a happy birthday yesterday on October 4”' in line
    assert 'Saved wording' not in line
    assert row == before
    prompt = cb.build_prompt(cb._load_prompt(), {'type': 'evening', 'now': now.isoformat(),
                                               'local': {'action_ledger': [row]}})
    assert "User's today: 2026-10-06" in prompt
    assert 'birthday on October 4' in prompt
    assert 'creation date is not proof of its event date' in prompt
    assert row['summary'] in prompt


def test_late_brief_prompt_has_local_clock_for_passed_same_day_times():
    from datetime import datetime, timezone

    prompt = cb.build_prompt(cb._load_prompt(), {
        'type': 'evening',
        'now': datetime(2026, 10, 14, 5, 48, tzinfo=timezone.utc).isoformat(),
        'google': {'userTimezone': 'America/Los_Angeles'},
        'local': {},
    })
    assert "User's today: 2026-10-13" in prompt
    assert "User's local time: 10:48 PM" in prompt
    assert 'do not present that time as an upcoming' in prompt
    assert 'do not shorten a marked ledger task into a second, partial version' in prompt


def test_undated_quoted_and_ambiguous_loop_dates_are_not_rebased():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 6, 20, tzinfo=timezone.utc)
    for created in ['', 'October 4', '2026-99-04']:
        text = 'Alex said “send it today”; date 04/10 is ambiguous'
        row = {'status': 'open', 'contact_name': 'Alex Chen', 'summary': text,
               'created_at': created, 'action_type': 'reply'}
        line = cb._open_loop_line([row], now)
        assert 'Earlier request (date unknown)' in line
        assert text in line
        assert row['summary'] == text
        assert '2026-10-04' not in line
    current = {'summary': 'Send it today', 'created_at': '2026-10-06'}
    assert 'From Oct 6: “Send it today”' in cb._open_loop_line([current], now)
    for created in ['2026-10-06', '2026-10-07']:
        stale = {'summary': 'Birthday yesterday on October 4', 'created_at': created}
        line = cb._open_loop_line([stale], now)
        assert stale['summary'] in line and ('From Oct 6' in line or 'Earlier request (date unknown)' in line)
        if created > '2026-10-06':
            assert 'date unknown' in line and created not in line.split('“')[0]


def test_history_discovery_purpose_overrides_first_run_without_inventing_obligation():
    discovery = {'observations': [{'person': 'Invented Alex', 'fact': 'Developing a community garden',
                 'source_refs': ['gmail:invented'], 'importance': {'tier': 'vip',
                 'reason': 'regular two-way activity across several weeks'}}], 'sources': ['gmail']}
    note = cb._first_run_note({'type': 'welcome', 'first_run': False,
                              'history_discovery': discovery}, {}, {}, [], [])
    assert 'HISTORY DISCOVERY HANDOFF' in note
    assert 'community garden' in note and 'gmail:invented' in note
    assert 'frequency alone is not importance' in note
    assert 'do not invent urgency' in note.lower()
    assert 'bounded initial history review' in note
    assert 'FIRST USEFUL LOOK' not in note


@pytest.mark.parametrize('persistent', [False, True])
def test_truncated_extraction_reuses_full_context_without_queue_wait(tmp_path, monkeypatch, persistent):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    calls = []
    def llm(prompt, inputs, system=None, schema=None):
        calls.append((prompt, system, schema))
        assert not inputs.get('_critic') and not inputs.get('_revise')
        if len(calls) == 1 or persistent:
            raise cb.ModelResponseError('model_output_limit', 'MAX_TOKENS')
        return json.dumps({'markdown': 'Nothing needs your attention today.'})
    if persistent:
        with pytest.raises(cb.ModelResponseError):
            cb.compose({'type': 'morning', 'google': {}, 'local': {}}, llm=llm)
        assert not (tmp_path / 'briefs').exists()
    else:
        out = cb.compose({'type': 'morning', 'google': {}, 'local': {}}, llm=llm)
        assert 'Nothing needs your attention today.' in out['brief_markdown']
    assert len(calls) == 2 and calls[0] == calls[1]


def test_blocked_extraction_is_not_retried_as_truncation(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    calls = []
    def llm(*args, **kwargs):
        calls.append(1)
        raise cb.ModelResponseError('model_no_candidates', 'SAFETY')
    with pytest.raises(cb.ModelResponseError):
        cb.compose({'type': 'morning', 'google': {}, 'local': {}}, llm=llm)
    assert len(calls) == 1


def test_real_gemini_path_recovers_truncation_in_same_compose(tmp_path, monkeypatch):
    import io
    import sqlite3
    import gemini
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_GEMINI_MODEL', 'gemini-3.8-flash')
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'test')
    monkeypatch.setenv('SOTTO_FALLBACK_MODEL', '')
    monkeypatch.setenv('SOTTO_CRITIC', 'off')
    monkeypatch.delenv('SOTTO_BRIEF_MODEL', raising=False)
    monkeypatch.delenv('SOTTO_LLM_STUB', raising=False)
    requests = []
    def respond(request, **kwargs):
        requests.append(json.loads(request.data))
        result = {'candidates': [{'finishReason': 'MAX_TOKENS',
                                 'content': {'parts': [{'text': '{"markdown": "unfinished'}]}}]}
        if len(requests) == 2:
            result = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
                {'text': json.dumps({'markdown': 'Nothing needs your attention today.'})}]}}]}
        return io.BytesIO(json.dumps(result).encode())
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', respond)
    out = cb.compose({'type': 'morning', 'google': {}, 'local': {}}, llm=gemini.call_gemini)
    assert len(requests) == 2 and requests[0] == requests[1]
    assert requests[0]['generationConfig']['maxOutputTokens'] == 65536
    assert 'Nothing needs your attention today.' in out['brief_markdown']
    with sqlite3.connect(tmp_path / 'events/model-work.sqlite3') as db:
        assert [r[0] for r in db.execute('SELECT status FROM attempts ORDER BY created')] == [
            'ModelResponseError', 'succeeded']

def test_dedupe_never_removes_a_line_carrying_another_task_or_person():
    deck = {'anchor_key': 'deck', 'contact_name': 'Robin Green', 'contact_identifier': 'robin@example.com',
            'status': 'open', 'summary': 'Send the strategy deck'}
    contract = {**deck, 'anchor_key': 'contract', 'summary': 'Sign the partnership contract'}
    head = ('## Needs Attention Now\n- **Robin Green**<!--id:robin@example.com-->: Send the strategy '
            'deck. <!--loop:deck-->\n## Should Handle Today\n')
    for later in ('- **Robin Green**<!--id:robin@example.com-->: She asked you to confirm Thursday '
                  'dinner with her investors; the strategy deck is still owed too. <!--loop:deck-->\n',
                  '- **Robin Green**: Send the strategy deck. <!--loop:deck--> And **Pat Lee**'
                  '<!--id:pat@example.com--> asked you to approve the Q4 budget.\n',
                  '- **Robin Green**: Send the strategy deck and sign the partnership contract. '
                  '<!--loop:deck-->\n'):
        assert cb._dedupe_open_task_lines(head + later, [deck, contract]) == head + later
    repeat = head + '- **Robin Green**: The strategy deck is still owed. <!--loop:deck-->\n'
    # The emptied section goes with its last repeat.
    assert cb._dedupe_open_task_lines(repeat, [deck, contract]) == head.removesuffix('## Should Handle Today\n')


def test_dedupe_never_demotes_an_earlier_partial_mention():
    deck = {'anchor_key': 'deck', 'contact_name': 'Robin Green', 'contact_identifier': 'robin@example.com',
            'status': 'open', 'chased_count': 2, 'summary': 'Send the updated strategy deck'}
    md = ('## Needs Attention Now\n- **Robin Green**<!--id:robin@example.com|ch:email-->: Strategy deck '
          'still owed. <!--loop:deck-->\n## Still Pending\n- **Robin Green**: Send the updated strategy '
          'deck. <!--loop:deck-->\n')
    assert cb._dedupe_open_task_lines(md, [deck]) == md


def test_partial_mention_is_neither_shown_nor_lets_the_full_ask_be_removed():
    lease = {'anchor_key': 'lease', 'contact_name': 'Alex Chen', 'status': 'open', 'chased_count': 2,
             'summary': 'Wire the deposit and countersign the lease agreement'}
    md = ('## Needs Attention Now\n- **Alex Chen**: Lease agreement thread has gone quiet. <!--loop:lease-->\n'
          '## Still Pending\n- **Alex Chen**: Wire the deposit and countersign the lease agreement. '
          '<!--loop:lease-->\n')
    assert cb._dedupe_open_task_lines(md, [lease]) == md
    call = {'anchor_key': 'call', 'contact_name': 'Dana Wu', 'status': 'open', 'chased_count': 2,
            'summary': 'Call Dana Wu back about the Cedar lease', 'created_at': '2026-10-01'}
    signed = '## Needs Attention Now\n- **Dana Wu**: Sign the Cedar lease today.\n'
    assert cb._visible_open_task_keys(signed, [call]) == set()
    out = cb._append_still_open({'brief_markdown': signed}, [call], today='2026-10-10')
    assert 'Call Dana Wu back about the Cedar lease' in out['brief_markdown']


def test_final_validation_reads_the_backstop_as_coverage_not_model_inventory(monkeypatch):
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    second = {"contact_name": "Dana Wu", "status": "open", "action_type": "follow_up",
              "channel": "email", "summary": "Return the signed lease", "chased_count": 1,
              "created_at": _iso_hours_ago(30)}

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": "## Needs Attention Now\nQuiet.\n", "actions": []})

    out = cb.compose(_open_ledger_inputs(extra=[second]), llm=fake_llm, critic=True)
    assert "## Still open" in out["brief_markdown"]
    final = out["_final_validation"]
    assert "unavailable" not in final
    assert not {"open-loop-inventory", "dropped-open-loop"} & set(final["categories"])


def test_final_validation_never_exempts_model_inventory_after_filtered(monkeypatch):
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    model = ("## Needs Attention Now\nQuiet.\n## Filtered\nNothing else.\n"
             "## Still open\n- **Alex Stone**: Review the budget.\n"
             "- **Dana Wu**: Return the contract.\n")

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": model, "actions": []})

    out = cb.compose(_open_ledger_inputs(anchor_key="memo"), llm=fake_llm, critic=True)
    body = out["brief_markdown"]
    assert body.index("<!--loop:memo-->") < body.index("## Filtered")
    assert body.rindex("## Still open") > body.index("## Filtered")
    assert "open-loop-inventory" in out["_final_validation"]["categories"]
    assert "_deterministic_sections" not in out


@pytest.mark.parametrize("section", ["still open", "weekly review"])
@pytest.mark.parametrize("model_tail", ["", "## Filtered\nNothing else.\n"])
def test_owned_section_append_exempts_only_the_inserted_block(section, model_tail):
    # A same-name model heading before the insertion is still model inventory.
    model = f"## {section.title()}\n- **Alex Stone**: Budget.\n- **Dana Wu**: Contract.\n"
    out = {"brief_markdown": model + model_tail}
    block = f"## {section.title()}\n- **Priya Raman**: Send the memo. <!--loop:memo-->\n\n"
    cb._insert_deterministic_section(out, section, block)
    owned = out["_deterministic_sections"]
    assert owned == [section]
    lines = cb.brief_validate._deterministic_lines(out["brief_markdown"], owned)
    assert not any(mine for line, _, mine in lines if "Alex Stone" in line)
    assert any(mine for line, _, mine in lines if "<!--loop:memo-->" in line)


@pytest.mark.parametrize("section", ["still open", "weekly review"])
def test_owned_section_withholds_exemption_for_a_later_model_heading(section):
    out = {"brief_markdown": f"## Filtered\nNothing else.\n## {section.title()}\n"
                            "- **Alex Stone**: Budget.\n- **Dana Wu**: Contract.\n"}
    cb._insert_deterministic_section(out, section,
        f"## {section.title()}\n- **Priya Raman**: Send the memo. <!--loop:memo-->\n\n")
    assert out["_deterministic_sections"] == []
    assert not any(mine for _, _, mine in cb.brief_validate._deterministic_lines(
        out["brief_markdown"], out["_deterministic_sections"]))


def test_thread_sender_proof_skips_owner_sent_aliases(monkeypatch):
    monkeypatch.setattr(cb, 'configured_user_email', lambda: 'owner@example.com')
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda source: True)
    sent = {'threadId': 't1', 'from': 'Owner Alias <alias@example.com>', 'labelIds': ['SENT']}
    received = {'threadId': 't2', 'from': 'Dana Reed <dana@example.com>'}
    assert cb._source_thread_senders({'google': {'emails': [sent, received]}}) == {
        'thread:t2': {'dana reed': 'dana@example.com'}}


def test_word_matching_hint_does_not_override_review_or_force_paid_revision(monkeypatch):
    monkeypatch.setattr(cb, 'build_data_manifest', lambda inputs: {})
    calls = []
    def llm(prompt, options, **kwargs):
        calls.append(prompt)
        if options.get('_critic'):
            return json.dumps({'patches': [], 'score': 80, 'summary': 'reviewed'})
        return json.dumps({'brief_markdown': '## Needs Attention Now\nFixed.', 'actions': [], 'loop_updates': []})
    violation = ('marked-loop-wording: loop deck (missing ask words board). Saved summary: Share the '
                 'board deck. Saved ask: Share the board deck. Recorded Aug 9')
    result = cb.critique_and_revise({'brief_markdown': '## Needs Attention Now\nDeck.', 'actions': [],
                                     'loop_updates': []}, {}, llm=llm, violations=[violation],
                                    source_prompt='full source fixture')
    assert len(calls) == 1
    assert 'revision_source_scope' not in result['_critic']
    assert result['brief_markdown'].endswith('Deck.')
    assert violation in calls[0]


def test_separately_dated_summary_and_ask_use_existing_revision_and_remain_invalid_if_unchanged(monkeypatch):
    import brief_validate
    row = {'anchor_key': 'group-deck', 'contact_name': 'Research Group', 'status': 'open',
           'created_at': '2026-08-05', 'summary':
           'From Aug 5: Dana shared a trial-project deck and raised a review tomorrow.',
           'ask': 'Review the deck and ask Dana about the data room tomorrow.'}
    doubled = ('## Needs Attention Now\n**Research Group**<!--loop:group-deck--> - '
               'From Aug 5: “Dana shared a trial-project deck and raised a review tomorrow.” '
               'From Aug 5: “Review the deck and ask Dana about the data room tomorrow.”')
    violations = brief_validate.validate(doubled, [], '', action_ledger=[row], today='2026-08-10')
    assert any('one complete canonical ask' in v for v in violations)
    monkeypatch.setattr(cb, 'build_data_manifest', lambda inputs: {})
    calls = []
    def llm(prompt, options, **kwargs):
        calls.append(prompt)
        if options.get('_critic'):
            return json.dumps({'patches': [{'type': 'add_item', 'severity': 'moderate', 'detail': '\n'.join(violations)}], 'score': 80, 'summary': 'Task needs correction'})
        return json.dumps({'brief_markdown': doubled, 'actions': [], 'loop_updates': []})
    result = cb.critique_and_revise({'brief_markdown': doubled, 'actions': [], 'loop_updates': []},
                                    {'local': {'action_ledger': [row]}}, llm=llm,
                                    violations=violations, source_prompt='full source fixture')
    assert len(calls) == 2
    assert 'one complete canonical ask' in calls[-1]
    assert result['_critic']['revision_reasons']['validator']['categories']['marked-loop-wording'] == 1
    assert any('one complete canonical ask' in v for v in brief_validate.validate(
        result['brief_markdown'], [], '', action_ledger=[row], today='2026-08-10'))


def test_dropped_historical_group_task_gets_one_ask_rule_before_its_only_revision(monkeypatch):
    import brief_validate
    row = {'anchor_key': 'group-deck', 'contact_name': 'Research Group', 'status': 'open',
           'created_at': '2026-08-05', 'deadline': '2026-08-09',
           'summary': 'From Aug 5: Dana shared a trial-project deck and raised a review tomorrow.',
           'ask': 'Review the deck and ask Dana about the data room tomorrow.'}
    initial = '## Needs Attention Now\nNo task selected.'
    corrected = ('## Needs Attention Now\n**Research Group**<!--loop:group-deck--> - '
                 'From Aug 5: “Review the trial-project deck Dana shared, following the review '
                 'she raised; ask Dana about the data room tomorrow.”')
    violations = brief_validate.validate(initial, [], '', action_ledger=[row], today='2026-08-10')
    assert any(v.startswith('dropped-open-loop:') for v in violations)
    monkeypatch.setattr(cb, 'build_data_manifest', lambda inputs: {})
    calls = []
    def llm(prompt, options, **kwargs):
        calls.append(prompt)
        if options.get('_critic'):
            return json.dumps({'patches': [{'type': 'add_item', 'severity': 'moderate', 'detail': '\n'.join(violations)}], 'score': 80, 'summary': 'Task needs correction'})
        assert 'one complete canonical ask' in prompt
        assert 'not two separately dated task sentences' in prompt
        return json.dumps({'brief_markdown': corrected, 'actions': [], 'loop_updates': []})
    result = cb.critique_and_revise({'brief_markdown': initial, 'actions': [], 'loop_updates': []},
                                    {'local': {'action_ledger': [row]}}, llm=llm,
                                    violations=violations, source_prompt='full source fixture')
    assert len(calls) == 2, 'the existing critic and revision are the only model calls'
    assert not [v for v in brief_validate.validate(result['brief_markdown'], [], '',
                 action_ledger=[row], today='2026-08-10')
                if v.startswith(('dropped-open-loop:', 'marked-loop-wording:'))]


def test_model_output_cannot_claim_composer_sections():
    assert '_deterministic_sections' not in cb._normalize_output(
        {'brief_markdown': 'x', '_deterministic_sections': ['needs attention now']})


def test_historical_date_repair_does_not_splice_provenance_into_narrative():
    from datetime import datetime, timezone
    import copy
    row = {'anchor_key': 'task:one', 'summary': 'A partner shared a deck yesterday',
           'ask': 'Review the data room tomorrow.', 'created_at': '2026-10-10',
           'deadline': '2026-10-12', 'status': 'open'}
    original = copy.deepcopy(row)
    out = {'brief_markdown': '**Partner**<!--loop:task:one--> - From Oct 10: A partner shared a deck yesterday. Review the data room tomorrow.',
           'actions': [{'loopId': 'task:one', 'deadlineDate': '2026-10-12'}]}
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    fixed = cb._date_marked_historical_requests(copy.deepcopy(out), [row], now)
    assert fixed['brief_markdown'] == out['brief_markdown']
    assert fixed['actions'] == out['actions'] and row == original
    assert cb._date_marked_historical_requests(copy.deepcopy(fixed), [row], now) == fixed
    unchanged = '<!--loop:task:one--> A different appointment tomorrow.'
    assert cb._date_marked_historical_requests({'brief_markdown': unchanged}, [row], now)['brief_markdown'] == unchanged
    current = dict(row, created_at='2026-10-12')
    assert cb._date_marked_historical_requests(copy.deepcopy(out), [current], now) == out
    for line in ('Review the data room tomorrow.',
                 '<!--loop:task:one--><!--loop:task:two--> Review the data room tomorrow.'):
        assert cb._date_marked_historical_requests({'brief_markdown': line}, [row], now)['brief_markdown'] == line


def test_historical_repair_overlapping_saved_phrases_is_single_pass_and_quote_safe():
    from datetime import datetime, timezone
    import copy
    row = {'anchor_key': 'task:overlap', 'summary': 'Please review the deck tomorrow.',
           'ask': 'review the deck tomorrow.', 'created_at': '2026-10-10'}
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    out = {'brief_markdown': '<!--loop:task:overlap--> Please review the deck tomorrow.'}
    fixed = cb._date_marked_historical_requests(copy.deepcopy(out), [row], now)
    assert fixed['brief_markdown'] == '<!--loop:task:overlap--> From Oct 10: “Please review the deck tomorrow.”'
    assert cb._date_marked_historical_requests(copy.deepcopy(fixed), [row], now) == fixed
    quoted = {'brief_markdown': '<!--loop:task:overlap--> From Oct 10: “Please review the deck tomorrow.”'}
    assert cb._date_marked_historical_requests(copy.deepcopy(quoted), [row], now) == quoted


def test_historical_repair_does_not_nest_an_existing_saved_date():
    from datetime import datetime, timezone
    import copy
    row = {'anchor_key': 'task:deck', 'status': 'open', 'created_at': '2026-10-10',
           'summary': 'From Oct 10: A partner shared a deck for review tomorrow.',
           'ask': 'Review the deck tomorrow.'}
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    out = {'brief_markdown': '<!--loop:task:deck--> From Oct 10: A partner shared a deck for review tomorrow. '
                             'Review the deck tomorrow.'}
    fixed = cb._date_marked_historical_requests(copy.deepcopy(out), [row], now)
    assert fixed['brief_markdown'] == out['brief_markdown']
    assert '“From Oct 10:' not in fixed['brief_markdown']
    assert cb._date_marked_historical_requests(copy.deepcopy(fixed), [row], now) == fixed


def test_schedule_prioritizes_owner_timed_meetings_over_old_all_day_context(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    old_context = [{'id': f'ooo-{i}', 'calendar_id': 'team', 'summary': f'Team OOO {i}',
                    'start': '2026-09-01', 'end': '2026-10-01'} for i in range(6)]
    timed_context = _meeting(7, calendar_id='team', summary='Other person meeting')
    owner = [_meeting(h, calendar_id='primary') for h in range(8, 13)]
    inputs = _calendar_inputs([*old_context, timed_context, *owner])
    lines = cb._schedule_lines(inputs, {})
    assert len(lines) == 5
    assert all(f'Planning {h}' in lines[h-8] for h in range(8, 13))
    assert not any('OOO' in line or 'Other person' in line for line in lines)
    # Context remains visible when space permits, explicitly labeled; ongoing dates are today.
    spare = cb._schedule_lines(_calendar_inputs([old_context[0], timed_context, owner[0]]), {})
    assert 'Planning 8' in spare[0]
    assert any('Other person meeting (shared context)' in line for line in spare)
    assert any('Mon Sep 21 All day: Team OOO 0 (shared context)' in line for line in spare)


def test_schedule_all_day_dedupe_requires_exact_provider_copy_proof(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    base = {'id': 'original', 'calendar_id': 'primary', 'iCalUID': 'same-invite',
            'summary': 'Team in office', 'start': '2026-09-21', 'end': '2026-09-22'}
    # Two copies coalesce. A same-title separate event or next-day occurrence must survive.
    copy = {**base, 'id': 'copy', 'calendar_id': 'team'}
    unrelated = {**base, 'id': 'other', 'iCalUID': 'different-invite'}
    following = {**base, 'id': 'tomorrow', 'start': '2026-09-22', 'end': '2026-09-23'}
    lines = cb._schedule_lines(_calendar_inputs([base, copy, unrelated, following]), {})
    assert len(lines) == 3
    assert sum('Mon Sep 21' in line for line in lines) == 2
    assert sum('Tue Sep 22' in line for line in lines) == 1


def test_cli_uncertain_brief_has_fixed_terminal_signal(monkeypatch, capsys):
    from gemini_transport import BriefModelOutcomeUncertainError
    from work_queue import MODEL_OUTCOME_UNCERTAIN_EXIT
    def fail():
        raise BriefModelOutcomeUncertainError('private provider details')
    monkeypatch.setattr(cb, 'main', fail)
    assert cb.cli() == MODEL_OUTCOME_UNCERTAIN_EXIT
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'no automatic retry' in captured.err
    assert 'private' not in captured.err


def test_internal_loop_marker_and_mixed_context_are_left_for_semantic_review():
    from datetime import datetime, timezone
    import copy
    row = {'anchor_key': 'task:deck', 'status': 'open', 'created_at': '2026-10-10',
           'contact_name': 'Dana Reed', 'contact_identifier': 'dana@example.test',
           'summary': 'Dana shared a deck for review tomorrow.',
           'ask': 'Review the deck tomorrow.'}
    marker = '<!--loop:task:deck-->'
    original = ('## Needs Attention Now\n**Dana Reed**<!--id:dana@example.test|ch:email-->: '
                'From Oct 10: Dana shared a deck for review tomorrow' + marker + '. '
                'The current meeting is confirmed for Oct 14. I can draft a reply.\n')
    out = {'brief_markdown': original, 'actions': [{'loopId': 'task:deck'}]}
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    fixed = cb._date_marked_historical_requests(copy.deepcopy(out), [row], now)
    assert fixed['brief_markdown'] == original
    assert fixed['brief_markdown'].count(marker) == 1
    assert '<!--id:dana@example.test|ch:email-->' in fixed['brief_markdown']
    assert 'The current meeting is confirmed for Oct 14. I can draft a reply.' in fixed['brief_markdown']
    assert fixed['actions'] == out['actions']
    assert cb.brief_validate._check_marked_loop_wording(fixed['brief_markdown'], [row], '2026-10-14')
    assert cb._date_marked_historical_requests(copy.deepcopy(fixed), [row], now) == fixed
    # Never choose one task when a paragraph binds multiple obligations.
    ambiguous = dict(out, brief_markdown=original.replace(marker, marker + '<!--loop:other-->'))
    assert cb._date_marked_historical_requests(copy.deepcopy(ambiguous), [row], now) == ambiguous


def test_one_complete_marked_ask_needs_no_repeated_summary_sentence():
    from datetime import datetime, timezone
    row = {'anchor_key': 'task:call', 'status': 'open', 'created_at': '2026-10-10',
           'contact_name': 'Dana Reed', 'contact_identifier': 'dana@example.test',
           'summary': 'Dana offered a choice of two times to discuss the lease tomorrow.',
           'ask': 'Confirm whether 9 PM tomorrow works or propose another time.'}
    markdown = ('## Needs Attention Now\n**Dana Reed**<!--id:dana@example.test|ch:email--> '
                '<!--loop:task:call-->: From Oct 10: “Confirm whether 9 PM tomorrow works '
                'or propose another time.” The meeting is now confirmed for Oct 14.\n')
    assert cb.brief_validate._check_marked_loop_wording(markdown, [row], '2026-10-14') == []
    # Wording can be complete without proving the broader summary's lease context stale.
    # That distinction requires the existing source-bound semantic review.
    assert cb._line_open_task_keys(markdown.splitlines()[1], [row], '2026-10-14') == set()
    out = {'brief_markdown': markdown}
    assert cb._date_marked_historical_requests(dict(out), [row], datetime(2026, 10, 14, tzinfo=timezone.utc)) == out


def test_calendar_model_prompt_and_critic_manifest_label_nonparticipating_context(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    shared = _meeting(9, calendar_id='colleague', summary='Colleague only',
                      attendees=[{'email': 'colleague@example.com', 'self': True}],
                      my_response='needsAction')
    own = _meeting(10, calendar_id='primary', summary='Owner meeting')
    inputs = _calendar_inputs([shared, own])
    prompt = cb.build_prompt('{{calendar}}', inputs)
    assert 'Colleague only** [shared calendar context; owner is not participating, not their meeting or RSVP]' in prompt
    assert 'Owner meeting** [shared calendar' not in prompt
    assert "you haven't answered this invite" not in prompt
    manifest = cb.build_data_manifest(inputs)
    assert {event['title']: event['owner_participates'] for event in manifest['calendar_events']} == {
        'Colleague only': False, 'Owner meeting': True}
    assert '_owner_participates' not in shared  # source rows stay immutable
    assert cb._rsvp_actions(inputs, now=cb.datetime.fromisoformat(inputs['now'])) == []


def test_schedule_keeps_current_owner_all_day_block_before_five_timed_events(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    block = {'id': 'travel', 'calendar_id': 'primary', 'summary': 'Owner travel',
             'start': '2026-09-20', 'end': '2026-09-22'}
    lines = cb._schedule_lines(_calendar_inputs([*[_meeting(h, calendar_id='primary') for h in range(8, 13)], block]), {})
    assert len(lines) == 5
    assert 'Mon Sep 21 All day: Owner travel' in lines[0]
    assert all(f'Planning {hour}' in lines[hour-7] for hour in range(8, 12))


@pytest.mark.parametrize('proof', ['exact', 'missing', 'wrong_thread', 'wrong_address', 'different_ask'])
def test_legacy_thread_row_display_uses_exact_source_sender_without_mutation(proof):
    import copy
    row = {'anchor_key': 'thread:invite', 'status': 'open', 'channel': 'gmail',
           'contact_name': '', 'contact_identifier': '', 'created_at': '2026-10-14',
           'summary': 'Dana Reed invited you to meet Thursday at 11 AM at the office',
           'ask': 'Accept or decline the office invitation', 'deadline': '2026-10-15'}
    before = copy.deepcopy(row)
    line = ('**Dana Reed**<!--id:dana@example.com|ch:email--> - '
            'Dana Reed invited you to meet Thursday at 11 AM at the office'
            '<!--loop:thread:invite-->. Accept or decline the office invitation.')
    source = {'thread:invite': {'dana reed': 'dana@example.com'}}
    if proof == 'missing':
        source = {}
    elif proof == 'wrong_thread':
        source = {'thread:other': source['thread:invite']}
    elif proof == 'wrong_address':
        source = {'thread:invite': {'dana reed': 'other@example.com'}}
    elif proof == 'different_ask':
        line = '**Dana Reed**<!--id:dana@example.com|ch:email--> - Send the budget.<!--loop:thread:invite-->'
    out = {'brief_markdown': '## Should Handle Today\n' + line + '\n'}
    cb._append_still_open(out, [row], today='2026-10-14', source_thread_senders=source)
    assert ('## Still open' not in out['brief_markdown']) is (proof == 'exact')
    assert row == before


def test_source_schedule_replaces_model_timezone_rows_without_false_paid_revision(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    inputs = _calendar_inputs([_meeting(h) for h in range(8, 13)])
    original = {'brief_markdown': '**Coming Up**\n- Thu Oct 15 8:30 AM EDT: Old timezone row\n\n'
                '<!--meeting:event_id:source-->\n## Still open\nReply to Dana about the lease.'}
    result = cb._ensure_schedule(original, inputs)
    assert 'Old timezone row' not in result['brief_markdown']
    assert 'Reply to Dana' in result['brief_markdown']
    assert '<!--meeting:event_id:source-->' in result['brief_markdown']
    assert not cb.brief_validate._check_coming_up_length(result['brief_markdown'])
    assert cb._ensure_schedule(result, inputs) == result


def test_extraction_and_revision_require_the_existing_recipient_contract():
    for schema in (cb.BRIEF_RESPONSE_SCHEMA['properties']['actionItems']['items'],
                   cb.BRIEF_REVISION_SCHEMA['properties']['actions']['items']):
        assert {'type', 'contactName', 'contactIdentifier', 'helpKind', 'evidence', 'newObligation'} <= set(schema['required'])
        assert set(schema['properties']['evidence']['items']['required']) == {'sourceType', 'sourceId', 'snippet'}
        assert 'none' in schema['properties']['helpKind']['enum']
    assert 'Preserve type, contactName, contactIdentifier and helpKind' in cb.REVISE_SYSTEM


def test_revision_receives_same_help_voice_and_marker_policy_as_writer(monkeypatch):
    monkeypatch.setattr(cb, 'build_data_manifest', lambda inputs: {})
    monkeypatch.setattr(cb, 'run_critic', lambda *a, **kw: {
        'patches': [{'type': 'add_item', 'detail': 'Include the supported urgent decision.', 'severity': 'moderate'}],
        'score': 60, 'summary': 'missing decision'})
    seen = []
    def revise(prompt, inputs, **kw):
        seen.append(prompt)
        return json.dumps({'brief_markdown': '## Needs Attention Now\nDecision.', 'actions': [], 'loop_updates': []})
    cb.critique_and_revise({'brief_markdown': 'Original', 'actions': [], 'loop_updates': []}, {}, llm=revise, source_prompt='source')
    assert len(seen) == 1
    for heading in ('## Useful help in every brief', '## Writing the Brief',
                    '## Deep Link Markers (CRITICAL — every entry must have one)'):
        expected = cb._extract_prompt_sections(cb._load_prompt(), (heading,))
        assert expected and expected in seen[0]


def test_model_cannot_forge_archived_model_timings(monkeypatch):
    def llm(prompt, inputs, **kwargs):
        cb.metrics.record('extraction', 1.25, 12, 8, 'fixture')
        return json.dumps({'brief_markdown': '## Filtered\nA quiet day.', 'actions': [],
                           '_model_metrics': {'total_wall_s': 999, 'private': 'forged'}})
    out = cb.compose({'type': 'morning', 'google': {}, 'local': {}}, llm=llm)
    assert out['_model_metrics']['calls'] == 1
    assert out['_model_metrics']['total_wall_s'] == 1.2
    assert out['_model_metrics']['phases']['extraction']['calls'] == 1
    assert 'private' not in out['_model_metrics']


def test_completion_typography_is_source_exact_before_critic_receipt(tmp_path, monkeypatch):
    inputs, updates = _closure_fixture(tmp_path, monkeypatch)
    message = inputs['google']['emails'][0]
    message['body'] = 'I’ve sent the deck; it’s ready for review.'
    updates[0]['evidence'][0]['snippet'] = "I've sent the deck; it's ready for review."
    out = {'loop_updates': updates}
    before = cb._continuity().critic_review_receipt(updates[0])
    reviewed = set()
    context = cb._loop_update_review_context(out, inputs, reviewed)
    after = cb._continuity().critic_review_receipt(updates[0])
    assert before != after
    assert context[0]['proposal_id'] == after
    assert context[0]['evidence'][0]['proposed_quote'] == message['body']
    assert reviewed == {after}
    cb._stamp_loop_reviews(out, reviewed)
    assert cb._continuity()._has_critic_approval(out['loop_updates'][0])
    assert cb._project_loop_updates(out, inputs, inputs['local']['action_ledger']) == []
    # A later material rewrite cannot borrow that approval, even when it is in the source.
    out['loop_updates'][0]['evidence'][0]['snippet'] = 'it’s ready for review.'
    assert not cb._continuity()._has_critic_approval(out['loop_updates'][0])


def test_completion_quote_recovery_does_not_remove_negation(tmp_path, monkeypatch):
    inputs, updates = _closure_fixture(tmp_path, monkeypatch)
    inputs['google']['emails'][0]['body'] = 'I haven’t sent the deck.'
    updates[0]['evidence'][0]['snippet'] = "I have sent the deck."
    cb._loop_update_review_context({'loop_updates': updates}, inputs)
    assert updates[0]['evidence'][0]['snippet'] == 'I have sent the deck.'
    assert not cb._continuity()._has_critic_approval(updates[0])


@pytest.mark.parametrize('same_title', [False, True])
def test_source_schedule_replaces_natural_revision_at_rows_once(tmp_path, monkeypatch, same_title):
    """Replay the October 28 natural early-prep revision's mixed schedule formats."""
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    first = 'Pilot pricing review' if same_title else 'Weekly team sync'
    events = [
        {'id': 'routine-team-sync', 'summary': first, 'start': '2026-10-27T09:00:00Z'},
        {'id': 'pricing-decision-meeting', 'summary': 'Pilot pricing review',
         'start': '2026-10-29T14:00:00Z'}]
    inputs = _calendar_inputs(events, now='2026-10-27T08:00:00Z')
    inputs['google']['userTimezone'] = 'UTC'
    original = {'brief_markdown': (
        '## Should Handle Today\n**Maya Chen**<!--id:pricing-decision-meeting|ch:calendar--> '
        'Compare annual and monthly pricing against your team budget before Thursday.\n\n'
        f'**Coming Up**\n- Tue Oct 27 9:00 AM: {first}\n'
        '- Thu Oct 29 2:00 PM: Pilot pricing review\n\n'
        f'- **Today at 9:00 AM** — {first} (Sam Patel) — ⚠ no link or address yet\n'
        '- **Thu Oct 29 at 2:00 PM** — Pilot pricing review (Maya Chen) — ⚠ no link or address yet\n\n'
        '<!--meeting:event_id:routine-team-sync-->\n'
        '<!--meeting:event_id:pricing-decision-meeting-->\n'
        'Unheaded source context remains.\n## Filtered\n0 promotional emails')}
    result = cb._ensure_schedule(original, inputs)
    markdown = result['brief_markdown']
    assert markdown.count('- Tue Oct 27 9:00 AM: ' + first) == 1
    assert markdown.count('- Thu Oct 29 2:00 PM: Pilot pricing review') == 1
    assert 'Today at' not in markdown and 'Oct 29 at' not in markdown
    assert '(Sam Patel)' not in markdown and '(Maya Chen)' not in markdown
    for text in ('Compare annual and monthly pricing', '<!--meeting:event_id:routine-team-sync-->',
                 '<!--meeting:event_id:pricing-decision-meeting-->', 'Unheaded source context remains.',
                 '0 promotional emails'):
        assert text in markdown
    assert cb._ensure_schedule(result, inputs) == result


@pytest.mark.parametrize('heading', ['## Already Handled', '## ✅ Already Handled'])
@pytest.mark.parametrize('subheading', ['', '### Dana Reed\n'])
def test_reconciliation_keeps_completed_section_with_unbulleted_person(heading, subheading):
    reply = {'anchor_key': 'reply', 'status': 'open', 'summary': 'Reply with feedback'}
    deck = {'anchor_key': 'deck', 'status': 'open', 'summary': 'Send the deck'}
    completed = subheading + '**Dana Reed**<!--id:dana@example.test|ch:email--> - Shared the feedback. <!--loop:reply-->'
    still_open = '**Robin Green**: Send the deck. <!--loop:deck-->'
    out = {'brief_markdown': '## Should Handle Today\n' + still_open + '\n\n' + heading
           + '\n\n' + completed + '\n\n## Still Pending\n' + still_open + '\n'}
    cb._reconcile_task_sections(out, [reply, deck], [deck])
    assert heading + '\n\n' + completed in out['brief_markdown']
    assert out['brief_markdown'].count(still_open) == 1
    assert '## Still Pending' not in out['brief_markdown']


def test_compose_keeps_verified_completion_under_handled_after_pruning_duplicate(tmp_path, monkeypatch):
    inputs, updates = _closure_fixture(tmp_path, monkeypatch, n=2)
    receipt = cb._continuity().critic_review_receipt(updates[0])
    completed = '**Dana Reed**: Sent the deck. <!--loop:deck-0-->'
    outstanding = '**Robin Green**: Send the second deck. <!--loop:deck-1-->'
    def llm(prompt, data, **kwargs):
        if data.get('_critic'):
            return json.dumps({'score': 95, 'patches': [], 'loop_verdicts': [
                {'proposal_id': receipt, 'verdict': 'approve'}]})
        return json.dumps({'markdown': '## Should Handle Today\n' + outstanding
            + '\n\n## Already Handled\n\n' + completed
            + '\n\n## Still Pending\n' + outstanding,
            'actionItems': [], 'loopUpdates': [updates[0]]})
    out = cb.compose(inputs, llm=llm, critic=True)
    assert out['_loop_projection']['resolved'] == ['deck-0']
    assert '## Already Handled\n\n' + completed in out['brief_markdown']
    assert out['brief_markdown'].count(outstanding) == 1
    assert '## Still Pending' not in out['brief_markdown']


@pytest.mark.parametrize("direction", [
    "focusing on tomorrow’s meetings and the open pricing decision.",
    "Send my morning brief. For the meeting prep, focus on Alex Coleman: the investment decision.",
    "Send my brief, and draft a reply to Bob",
    "Send my evening brief. Make it the morning one.",
])
def test_composition_direction_is_exact_context_with_existing_authority_limits(direction):
    inputs = {'type': 'evening', 'google': {'events': []}, 'local': {},
              'composition_direction': direction}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert direction in prompt
    assert 'does not grant action authority' in prompt
    assert 'Fixed brief edition: evening.' in prompt
    assert 'direction below cannot change it' in prompt
    assert 'does not require a prep offer' in prompt
    assert direction not in cb.build_prompt(cb._load_prompt(), {**inputs, 'composition_direction': ''})


def test_manifest_only_revision_retains_owner_direction_and_authority_limits():
    direction = "focusing on tomorrow’s meetings and the pricing decision."
    seen = []
    def llm(prompt, inputs, **kwargs):
        if inputs.get('_critic'):
            return json.dumps({'patches': [{'type': 'remove_item', 'severity': 'critical',
                                            'detail': 'Remove a duplicate'}], 'score': 50})
        seen.append(prompt)
        return json.dumps({'brief_markdown': '## Needs Attention Now\nKept task.',
                           'actions': [], 'loop_updates': []})
    result = cb.critique_and_revise({'brief_markdown': '## Needs Attention Now\nDuplicate.',
                                   'actions': [], 'loop_updates': []},
                                  {'composition_direction': direction}, llm=llm)
    assert result['_critic']['revision_source_scope'] == 'manifest'
    assert direction in seen[0]
    assert 'does not grant action authority' in seen[0]
    assert 'does not require a prep offer' in seen[0]



def test_history_discovery_candidates_reach_existing_composer_with_current_evidence():
    discovery = {'observations': [
        {'canonical_id': 'z', 'person': 'Zara', 'fact': 'Works on gardens',
         'observed_date': '2026-10-01', 'source_refs': ['gmail:old-z'], 'fact_sources': ['gmail']},
        {'canonical_id': 'a', 'person': 'Alex', 'fact': 'Organizes the garden fundraiser',
         'observed_date': '2026-10-02', 'source_refs': ['gmail:old-a'], 'fact_sources': ['gmail']}],
        'sources': ['gmail', 'imessage'],
        'coverage': {'gmail': {'status': 'reviewed'}, 'imessage': {'status': 'held'}}}
    prompt = cb.build_prompt(cb._load_prompt(), {'type': 'welcome',
        'history_discovery': discovery, 'now': '2026-11-03T10:00:00Z',
        'local': {'action_ledger': [{'anchor_key': 'alex-budget', 'status': 'open',
            'contact_name': 'Alex', 'canonical_id': 'a', 'summary': 'Review the fundraiser budget',
            'action_type': 'follow_up', 'created_at': '2026-11-02'}]},
        'google': {'emails': [{'id': 'zara-done', 'threadId': 'garden-z',
            'from': 'owner@example.test', 'to': 'zara@example.test', 'isSent': True,
            'date': '2026-11-02T10:00:00Z', 'body': 'The garden plan is complete and attached.'}]}})
    assert 'HISTORY DISCOVERY HANDOFF' in prompt
    assert 'gmail:old-z' in prompt and 'gmail:old-a' in prompt
    assert 'Review the fundraiser budget' in prompt
    assert 'The garden plan is complete and attached.' in prompt
    assert 'current action ledger' in prompt and 'later replies' in prompt
    assert 'held' in prompt


def test_history_discovery_reads_protected_ledger_and_rejects_carried_override(tmp_path, monkeypatch):
    import yaml
    import source_context
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(source_context, 'allowed', lambda source: source == 'gmail')
    directory = tmp_path / 'knowledge/continuity'
    directory.mkdir(parents=True)
    row = {'anchor_key': 'garden-budget', 'status': 'open', 'action_type': 'reply',
           'channel': 'email', 'contact_name': 'Alex', 'canonical_id': 'garden-alex',
           'created_at': '2026-11-03', 'summary': 'PROTECTED_GARDEN_BUDGET',
           'source_refs': [{'sourceType': 'gmail', 'sourceId': 'budget-email'}]}
    (directory / 'garden-budget.md').write_text('---\n' + yaml.safe_dump(row) + '---\n')
    inputs = {'type': 'welcome', 'now': '2026-11-03T10:00:00Z',
              'history_discovery': {'observations': [
                  {'canonical_id': 'garden-alex', 'person': 'Alex', 'fact': 'Organizes gardens',
                   'source_refs': ['gmail:old-garden'], 'fact_sources': ['gmail'],
                   'observed_date': '2026-11-01'}],
                  'sources': ['gmail'], 'coverage': {'gmail': {'status': 'reviewed'}}},
              'local': {'action_ledger': [{**row, 'summary': 'UNTRUSTED_CARRIED_OVERRIDE'}]},
              'google': {'emails': [{'id': 'done', 'from': 'owner@example.test', 'isSent': True,
                  'date': '2026-11-03T09:00:00Z', 'body': 'LATER_REPLY_PLAN_COMPLETE'}]}}
    seen = []
    def model(prompt, *_args, **_kwargs):
        seen.append(prompt)
        return json.dumps({'brief_markdown': '## Needs Attention Now\nReview the garden budget.',
                           'actions': [], 'loop_updates': []})
    cb.compose(inputs, llm=model, critic=False)
    assert len(seen) == 1
    assert all(value in seen[0] for value in (
        'HISTORY DISCOVERY HANDOFF', 'gmail:old-garden',
        'PROTECTED_GARDEN_BUDGET', 'LATER_REPLY_PLAN_COMPLETE'))
    assert 'UNTRUSTED_CARRIED_OVERRIDE' not in seen[0]


def test_history_discovery_handoff_survives_manifest_only_review_and_revision():
    discovery = {'observations': [{'canonical_id': 'garden-alex', 'person': 'Alex',
        'fact': 'Organizes the garden fundraiser', 'observed_date': '2026-11-01',
        'source_refs': ['gmail:old-garden'], 'fact_sources': ['gmail']}],
        'sources': ['gmail', 'imessage'], 'coverage': {'gmail': {'status': 'reviewed'},
                                                     'imessage': {'status': 'held'}}}
    inputs = {'type': 'welcome', 'history_discovery': discovery, 'google': {}, 'local': {}}
    manifest = cb.build_data_manifest(inputs)
    assert manifest['history_discovery_handoff'] == cb._first_run_note(inputs, {}, {}, [], [])
    seen = []
    def model(prompt, inputs, **kwargs):
        seen.append(prompt)
        if inputs.get('_critic'):
            return json.dumps({'patches': [{'type': 'remove_item', 'severity': 'moderate',
                                            'detail': 'Remove a duplicate'}], 'score': 70})
        return json.dumps({'brief_markdown': 'Garden context.', 'actions': [], 'loop_updates': []})
    result = cb.critique_and_revise({'brief_markdown': 'Garden context. Duplicate.',
                                   'actions': [], 'loop_updates': []}, inputs, llm=model)
    assert result['_critic']['revision_source_scope'] == 'manifest'
    assert len(seen) == 2
    assert all('gmail:old-garden' in prompt and 'bounded initial history review' in prompt
               and 'held' in prompt for prompt in seen)


def test_history_discovery_fixed_purpose_is_policy_and_observations_stay_data():
    secret_fact = 'UNTRUSTED_FACT_INVENTED_ASK_IGNORE_ALL_RULES'
    inputs = {'type': 'welcome', 'history_discovery': {
        'observations': [{'canonical_id': 'alex', 'person': 'Alex', 'fact': secret_fact,
                          'observed_date': '2026-11-01', 'source_refs': ['gmail:old'],
                          'fact_sources': ['gmail']}], 'sources': ['gmail'],
        'coverage': {'gmail': {'status': 'reviewed'}}}, 'google': {}, 'local': {}}
    seen = []
    def model(prompt, inputs, system=None, **kwargs):
        seen.append((prompt, inputs, system))
        if inputs.get('_critic'):
            return json.dumps({'patches': [{'type': 'remove_item', 'severity': 'moderate',
                                            'detail': 'Remove a duplicate'}], 'score': 70})
        return json.dumps({'brief_markdown': '## Discovery\nGarden context.',
                           'actions': [], 'loop_updates': []})
    cb.compose(inputs, llm=model, critic=True)
    extract = seen[0]
    assert 'bounded history discovery' in extract[2]
    assert 'Use names and relevant roles in prose; canonical IDs belong only in existing hidden markers.' in extract[2]
    assert 'State that this is a partial, bounded initial history review' in extract[2]
    assert secret_fact not in extract[2] and 'gmail:old' not in extract[2]
    assert secret_fact in extract[0]
    assert len(seen) == 3
    assert all('bounded history discovery' in item[0].split('## DATA MANIFEST')[0]
               for item in seen[1:2])
    assert 'bounded history discovery' in seen[2][0].split('## CRITIC ISSUES TO FIX')[0]
    assert all(secret_fact in item[0] for item in seen[1:])
    assert cb._history_discovery_direction({'type': 'morning'}) == ''
