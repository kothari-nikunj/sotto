"""compose_brief.py — offline (stubbed Gemini) contract test."""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location("compose_brief", os.path.join(ROOT, "_shared", "scripts", "compose_brief.py"))
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)

import gemini      # noqa: E402  — owns call_gemini / _gemini_once (patched below)
import textutil    # noqa: E402
import timeutil    # noqa: E402
import render_local  # noqa: E402


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
    assert out["brief_markdown"].startswith("# Brief")
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
    assert out["brief_markdown"] == "# Legacy"


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
    assert body["systemInstruction"] == {"parts": [{"text": "policy"}]}
    assert body["contents"] == [{"parts": [{"text": "user data"}]}]
    assert body["generationConfig"]["responseSchema"] == {"type": "object"}
    # legacy call shape unchanged when the kwargs are omitted
    gemini._gemini_once("m", "k", "plain")
    assert "systemInstruction" not in captured["body"]
    assert "responseSchema" not in captured["body"]["generationConfig"]


def test_brief_response_schema_shape():
    s = cb.BRIEF_RESPONSE_SCHEMA
    assert s["required"] == ["markdown", "actionItems", "extractedKnowledge"]
    item = s["properties"]["actionItems"]["items"]["properties"]
    for f in ("contextSummary", "contextAsk", "prose", "channel", "contactIdentifier",
              "threadSnippet", "emailThreadId", "meetingLink"):
        assert f in item, f
    ek = s["properties"]["extractedKnowledge"]["properties"]
    assert "person_updates" in ek and "company_updates" in ek


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
    two = cb._extract_prompt_sections("## Triage Discipline\na\n\n## Priority Levels\nb\n\n## Next\nc",
                                      cb._CRITIC_POLICY_SECTIONS)
    assert two == "## Triage Discipline\na\n\n## Priority Levels\nb"


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
    assert json.loads(out.stdout)["brief_markdown"] == "# B"
    # the envelope was accepted: 1 email reached the pipeline (the inputs diag names it)
    assert "1 emails" in out.stderr


def test_stub_env_path(tmp_path, monkeypatch):
    p = tmp_path / "resp.json"
    p.write_text(json.dumps({"brief_markdown": "stubbed"}))
    monkeypatch.setenv("SOTTO_LLM_STUB", str(p))
    out = cb.compose({"type": "evening", "window_hours": 24, "google": {}, "granola": {}, "local": {}})
    assert out["brief_markdown"] == "stubbed"


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
    # Freeze "now" near the meetings so the 72h horizon is deterministic.
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 24, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(cb, "datetime", _FixedDateTime)
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
    out = cb.compose({"type": "morning", "google": {}, "local": {}}, llm=fake_llm)
    assert out["actions"][0]["tap_link"] == "https://wa.me/15551234567"


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


def test_expired_snapshot_is_dropped(tmp_path):
    # A snapshot older than the TTL is NOT replayed — better Google-only than day(s)-old "needs reply".
    os.environ["SOTTO_DATA"] = str(tmp_path)
    old = _recent_stamp(cb.LOCAL_SNAPSHOT_TTL_HOURS + 5)
    cb._save_local_snapshot({"imessage": [{"text": "stale", "is_from_me": False, "timestamp": old}],
                             "generated_at": old, "source_status": {"imessage": "ok"}})
    assert cb._local_fallback({}) == {}          # expired → dropped
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
    assert out["brief_markdown"] == "# Revised\n- Added Sarah"   # revise pass applied
    assert out["_critic"]["actionable"] == 1
    assert calls["n"] == 3


def test_critic_passes_clean_brief_unchanged(monkeypatch):
    monkeypatch.setenv("SOTTO_CRITIC", "always")

    def fake_llm(prompt, inputs):
        if inputs.get("_critic"):
            return json.dumps({"patches": [], "score": 95, "summary": "clean"})
        return json.dumps({"brief_markdown": "# Clean", "actions": []})

    out = cb.compose({"type": "morning", "google": {}, "local": {}}, llm=fake_llm, critic=True)
    assert out["brief_markdown"] == "# Clean"        # no actionable patches → no revise
    assert out["_critic"]["actionable"] == 0


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
    assert cb._critic_decision("auto", 0, cb.CRITIC_AUTO_MIN_ACTIONS + 1)[0] is True         # many actions
    # unknown mode string falls back to auto
    import os as _os
    _os.environ["SOTTO_CRITIC"] = "banana"
    try:
        assert cb._critic_mode() == "auto"
    finally:
        del _os.environ["SOTTO_CRITIC"]


def test_critic_auto_skips_small_brief(monkeypatch):
    monkeypatch.delenv("SOTTO_CRITIC", raising=False)          # default = auto
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
    assert out["brief_markdown"] == "# Draft survives"


def test_research_quality_gate_rereseaches_thin_graph_profile(monkeypatch):
    # A graph person with only a thin profile must be re-researched (not skipped as "known").
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 24, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(cb, "datetime", _FixedDateTime)
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

    monkeypatch.setattr(cb, "datetime", _FixedDateTime)
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
         "conversation_hooks": ["His agent-memory piece last week fits your roadmap."]},
        {"email": "nelson@cobalt-research.com", "title": None, "company": "Cobalt Research",
         "relevance": [], "summary": "No public profile found.",
         "company_summary": "Cobalt Research does battery-materials analytics."},
    ]})
    # Compact, one-per-line, dates + urls preserved (snapshot of the block shape).
    assert "  Recent: Published an agent-memory piece. (late July 2026) — https://t.substack.com/p/mem" in formatted
    assert "skipme" not in formatted                                   # what-less item not rendered
    assert "  Personal: Ran the SF Marathon (https://x.com/t/1)" in formatted
    assert "  Hook: His agent-memory piece last week fits your roadmap." in formatted
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
    primary = cb.os.environ.get("SOTTO_GEMINI_MODEL", "gemini-3.6-flash")
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
    assert calls == ["gemini-3.6-flash"]


# --- evening followup merge (Sprint 0 #4) --------------------------------------

def _iso_hours_ago(h):
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _followup_capable_llm(seen, calls):
    def fake_llm(prompt, inputs, system=None, schema=None):
        if inputs.get("_followup"):
            calls["followup"] += 1
            return json.dumps({
                "followup_markdown": "**Acme Sync** — you owe Dana the deck.",
                "commitments": [{"meeting": "Acme Sync", "owner": "you",
                                 "what": "send the deck", "to_email": "dana@acme.com"}],
                "drafts": [{"to_name": "Dana", "to_email": "dana@acme.com", "channel": "email",
                            "subject": "Deck", "body": "Here it is."}]})
        seen["prompt"] = prompt
        return json.dumps({"brief_markdown": "# Evening", "actions": []})
    return fake_llm


def test_evening_brief_merges_followup_context(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    seen, calls = {}, {"followup": 0}
    inputs = {"type": "evening", "google": {"events": [], "userEmail": "me@x.com"},
              "granola": [{"title": "Acme Sync", "date": _iso_hours_ago(2),
                           "transcript": "you: I'll send the deck",
                           "attendee_emails": ["dana@acme.com"]}],
              "local": {}}
    out = cb.compose(inputs, llm=_followup_capable_llm(seen, calls))
    assert calls["followup"] == 1                      # exactly one followup extraction ran
    # The model's narrative is delivered as written; the still-open backstop then appends the
    # commitment this very run wrote to the ledger, which the stub brief never named (open-items
    # contract — see test_still_open_backstop_appends_dropped_ledger_items).
    assert out["brief_markdown"].startswith("# Evening")
    assert "## Still open" in out["brief_markdown"] and "send the deck" in out["brief_markdown"]
    p = seen["prompt"]
    # rendered followup context reached the evening prompt (via the reconciliation/evening path,
    # since the template has no {{followup_context}} placeholder yet)
    assert "Today's Meeting Follow-Ups" in p
    assert "send the deck" in p
    assert "Ready-to-send drafts prepared: 1" in p
    # commitments were deterministically written to the continuity ledger via the existing apply path
    cdir = tmp_path / "knowledge" / "continuity"
    files = list(cdir.glob("*.md")) if cdir.exists() else []
    assert files and any("send the deck" in f.read_text() for f in files)


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
    assert out["brief_markdown"] == "# Evening survives"


def test_evening_no_ended_meetings_adds_no_block(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    seen, calls = {}, {"followup": 0}
    out = cb.compose({"type": "evening", "google": {"events": []}, "granola": [], "local": {}},
                     llm=_followup_capable_llm(seen, calls))
    assert calls["followup"] == 0                      # short-circuits before any LLM call
    assert "Today's Meeting Follow-Ups" not in seen["prompt"]
    assert out["brief_markdown"] == "# Evening"


# ── Already Nudged Today: the surfaced-ledger reader (root of the double-tell) ──

def _surfaced(tmp_path, rows):
    d = tmp_path / "events"
    d.mkdir(parents=True, exist_ok=True)
    (d / "surfaced.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


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
    te._record_surfaced("agent", "urgent", "Sarah is waiting on the deck", "Sarah Chen", event)
    te._record_surfaced("queue", "quiet", "quiet hours", "Ben", event)
    rows = cb._load_surfaced_nudges(cb._user_local_date("+00:00"), cb._resolve_tz("+00:00"))
    assert [(r["sender"], r["cls"]) for r in rows] == [("Sarah Chen", "urgent")]
    assert "waiting on the deck" in rows[0]["reason"]


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

    out = cb.compose({"type": "morning", "google": {}, "local": {}}, llm=fake_llm, critic=True)
    # violations were appended to the critic's user content …
    assert "AUTOMATED VALIDATOR VIOLATIONS" in seen["critic_prompt"]
    assert "banned-phrase: 'reached out'" in seen["critic_prompt"]
    assert "missing-marker" in seen["critic_prompt"]
    # … and force the revise pass even though the critic itself found nothing
    assert out["brief_markdown"] == fixed_brief
    assert out["_critic"]["actionable"] >= 2


def test_validator_never_blocks_delivery(monkeypatch):
    # validator violations with the critic OFF: logged only, brief delivered unchanged
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    bad_brief = "## Needs Attention Now\n**Sarah Chen** reached out about the deal."

    def fake_llm(prompt, inputs, system=None, schema=None):
        assert not inputs.get("_critic") and not inputs.get("_revise")
        return json.dumps({"brief_markdown": bad_brief, "actions": []})

    out = cb.compose({"type": "morning", "google": {}, "local": {}}, llm=fake_llm, critic=True)
    assert out["brief_markdown"] == bad_brief


# --- the open-items contract: critic retry, then the deterministic backstop ----
# Every open ledger item appears in the brief exactly once — as an ask with its age, or in Already
# Handled when today's data shows it resolved. The bug this closes: a brief that showed NOTHING to do
# while the ledger held open loops older than the 1-day gather window.

def _open_ledger_inputs(hours=30):
    return {"type": "morning", "google": {}, "local": {"action_ledger": [
        {"contact_name": "Priya Raman", "status": "open", "action_type": "follow_up",
         "channel": "email", "summary": "Send the diligence memo",
         "created_at": _iso_hours_ago(hours)}]}}


def test_still_open_backstop_appends_dropped_ledger_items(monkeypatch):
    monkeypatch.setenv("SOTTO_CRITIC", "off")            # no retry available → backstop is the guarantee
    dropped = ("## Needs Attention Now\n**Dana Wu**<!--id:dana@x.com|ch:email--> wants the LOI.\n\n"
               "## Filtered\n\n12 promotional emails\n")

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": dropped, "actions": []})

    out = cb.compose(_open_ledger_inputs(), llm=fake_llm, critic=True)
    body = out["brief_markdown"]
    assert "## Still open\n- **Priya Raman** — Send the diligence memo (1d)" in body
    assert body.index("Still open") < body.index("## Filtered")   # before the last section, not after
    assert out["brief_text"].count("Priya Raman") == 1            # and it survives into the chat text


def test_backstop_stays_silent_when_the_brief_named_the_loop(monkeypatch):
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    handled = "## ✅ Already Handled\n**Priya Raman**<!--id:p@x.com|ch:email--> has the memo now.\n"

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": handled, "actions": []})

    assert "Still open" not in cb.compose(_open_ledger_inputs(), llm=fake_llm, critic=True)["brief_markdown"]


def test_dropped_loop_goes_to_the_critic_first(monkeypatch):
    """The backstop is the LAST resort: the violation rides the normal critic/revise path first, and a
    revision that surfaces the loop leaves nothing for the backstop to append."""
    monkeypatch.setenv("SOTTO_CRITIC", "always")
    seen = {}
    fixed = ("## Should Handle Today\n**Priya Raman**<!--id:p@x.com|ch:email--> is still waiting on "
             "the diligence memo, open two days.\n")

    def fake_llm(prompt, inputs, system=None, schema=None):
        if inputs.get("_critic"):
            seen["critic_prompt"] = prompt
            return json.dumps({"patches": [], "score": 90, "summary": "ok"})
        if inputs.get("_revise"):
            return json.dumps({"brief_markdown": fixed, "actions": []})
        return json.dumps({"brief_markdown": "## Filtered\n\n12 promotional emails\n", "actions": []})

    out = cb.compose(_open_ledger_inputs(), llm=fake_llm, critic=True)
    assert "dropped-open-loop" in seen["critic_prompt"] and "Priya Raman" in seen["critic_prompt"]
    assert out["brief_markdown"] == fixed
    assert "Still open" not in out["brief_markdown"]


# --- action_ledger populated from the continuity ledger -----------------------

def test_continuity_ledger_populates_action_ledger(tmp_path, monkeypatch):
    # Nothing else fills local["action_ledger"], so Open Commitments / Evening Accountability /
    # TRACKED OPEN LOOPS rendered empty every day until _normalize_local merged the ledger in.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True)
    (d / "a.md").write_text(
        "---\nanchor_key: k1\nstatus: open\naction_type: reply\ncontact_name: Sarah Chen\n"
        "channel: email\ncreated_at: '2026-06-20'\nsummary: Send the deck\n"
        "source_brief_at: '2026-06-23T07:00:00Z'\n---\n")
    (d / "b.md").write_text(
        "---\nanchor_key: k2\nstatus: resolved\ncontact_name: Done Person\n---\n")
    local = cb._normalize_local({"local": {}})
    assert [a["contact_name"] for a in local["action_ledger"]] == ["Sarah Chen"]   # active only
    # ...and it reaches the sections that were inert before
    assert "Sarah Chen" in cb._format_action_ledger(local)
    assert "Sarah Chen" in cb._format_reconciliation(local, "evening")
    # an explicitly-supplied action_ledger still wins
    local2 = cb._normalize_local({"local": {"action_ledger": [{"status": "open", "contact_name": "X"}]}})
    assert [a["contact_name"] for a in local2["action_ledger"]] == ["X"]


def test_action_ledger_excludes_meeting_entries(tmp_path, monkeypatch):
    """Meeting-prep/meeting-info loops are calendar shadows — the docket is their surface. The
    brief drops them exactly like /api/loops does, legacy hyphen spelling included."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True)
    for i, atype in enumerate(("meeting_prep", "meeting-prep", "Meeting_Info", "reply")):
        (d / f"{i}.md").write_text(
            f"---\nanchor_key: k{i}\nstatus: open\naction_type: {atype}\n"
            f"contact_name: Person {i}\nchannel: email\ncreated_at: '2026-06-20'\n"
            f"summary: item {i}\nsource_brief_at: '2026-06-23T07:00:00Z'\n---\n")
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
    assert json.loads(out.stdout)["brief_markdown"] == "# Real"


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

def test_freemail_user_domain_does_not_skip_freemail_attendees(tmp_path, monkeypatch):
    # A gmail.com user shares a "domain" with every other gmail.com human — the colleague-domain
    # skip must not blanket-exclude them from research (corporate domains keep the skip).
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 24, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(cb, "datetime", _FixedDateTime)
    inputs = {"google": {"userEmail": "me@yahoo.com", "events": [
        {"id": "e", "summary": "Coffee", "start": "2026-06-24T22:00:00+00:00", "attendees": [
            {"email": "me@yahoo.com", "displayName": "Me"},
            {"email": "jordan.vale@yahoo.com", "displayName": "Jordan Vale"}]}]}, "local": {}}
    assert [p["email"] for p in cb.select_attendees_for_research(inputs)] == ["jordan.vale@yahoo.com"]
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

def _dedup_data(tmp_path, companies=(), suggestions=None):
    os.environ["SOTTO_DATA"] = str(tmp_path)
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


def test_known_company_names_ride_into_the_extraction_prompt(tmp_path):
    """The model free-forms "YC" one day and "Y Combinator" the next; each spelling mints a file.
    Feeding back the names the graph already uses is the cheap half of the fix."""
    inputs = _dedup_data(tmp_path, companies=[("y-combinator", "Y Combinator"),
                                              ("signal-house", "Signal House")])
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "Companies Already in the Knowledge Graph" in prompt
    assert "- Y Combinator" in prompt and "- Signal House" in prompt


def test_no_company_files_means_no_company_block(tmp_path):
    """Silence gate: an empty graph adds nothing to the prompt."""
    inputs = _dedup_data(tmp_path)
    assert "Companies Already in the Knowledge Graph" not in cb.build_prompt(cb._load_prompt(), inputs)


def test_merge_suggestions_surface_as_one_optional_brief_line(tmp_path):
    inputs = _dedup_data(tmp_path, suggestions=[
        {"from": "c_aaa", "into": "c_bbb", "from_name": "Ben", "into_name": "Ben Butler",
         "reason": "name containment: ben ⊂ ben-butler", "first_seen": "2026-08-06"}])
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "Memory Maintenance" in prompt
    assert "- Ben and Ben Butler (name containment: ben ⊂ ben-butler)" in prompt
    assert "AT MOST ONE short closing line" in prompt


def test_no_suggestions_means_no_maintenance_block(tmp_path):
    inputs = _dedup_data(tmp_path, suggestions=[])
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
    assert "[chased ×2, last Tue]" in block
    assert "Vendor via imessage: a quote" in block and "chased" not in block.split("Vendor")[1]
    assert "never propose a first follow-up" in block


def test_ledger_ages_follow_an_injected_clock():
    """A replayed day is six weeks old; wall-clock ages would inflate every number in its prompt."""
    from datetime import datetime, timedelta, timezone
    local = {"action_ledger": [{"status": "open", "action_type": "reply", "contact_name": "Sarah",
                                "channel": "email", "summary": "the deck",
                                "created_at": "2026-06-20 09:00:00"}]}
    now = datetime(2026, 6, 22, 9, 0, 0, tzinfo=timezone.utc)
    assert "(2d)" in cb._format_action_ledger(local, now)
    assert cb.build_prompt("{{action_ledger}}", {"local": local, "now": "2026-06-22 09:00:00"}).count("(2d)") == 1


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
            "- Gave Maya a nudge about the contract (second ask).\n"
            "- Closed 2 loops — Ron delivered the deck; the RSVP resolved.\n"
            "- Held 3 interruptions until a better moment.\n"
            "- Prepped 4 people for tomorrow.\n"
            "- Offered follow-ups after 2 meetings.\n") in body
    # placement: after the Still open backstop's seam, before the Filtered section (the last one)
    assert body.index("## Still open") < body.index("## What moved today") < body.index("## Filtered")
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
    assert cb._receipt_lines(_evening(events=tomorrow), today, "+00:00") == [
        "Prepped 1 person for tomorrow."]


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
    assert out["brief_markdown"] == _RECEIPT_BRIEF
    assert cb._receipt_lines(_evening(), today, "+00:00") == []


def test_chase_ordinal_and_nameless_loops_read_as_plain_english():
    assert cb._chase_clause({"contact_name": "Maya", "summary": "the contract.",
                             "chased_count": 1}) == "Gave Maya a nudge about the contract (first ask)."
    assert cb._chase_clause({"contact_name": "Maya"}) == "Gave Maya a nudge."
    assert cb._chase_clause({"summary": "the contract"}) == "Sent a nudge about the contract."
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
    assert out["brief_markdown"] == _RECEIPT_BRIEF
    assert _marker(tmp_path) is None                               # nothing said, nothing recorded


def test_an_unreadable_update_cache_costs_nothing(monkeypatch, tmp_path):
    """Same bar as its neighbouring appendices: any failure renders nothing, never an error."""
    _receipt_env(monkeypatch, tmp_path)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "update_check.json").write_text("{not json")
    out = cb.compose({"type": "morning", "google": {"events": []}, "local": {}}, llm=_receipt_llm())
    assert out["brief_markdown"] == _RECEIPT_BRIEF
