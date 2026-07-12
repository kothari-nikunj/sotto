"""compose_brief.py — offline (stubbed Gemini) contract test."""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))

spec = importlib.util.spec_from_file_location("compose_brief", os.path.join(ROOT, "_shared", "scripts", "compose_brief.py"))
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)


def test_compose_with_injected_llm():
    # inject a fake model: returns a minimal valid extraction
    def fake_llm(prompt, inputs):
        assert "Brief Extraction Prompt" in prompt          # the real prompt was loaded
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
        if len(calls) == 1:  # primary 429s
            raise urllib.error.HTTPError("u", 429, "RESOURCE_EXHAUSTED", {}, None)
        return '{"markdown": "ok"}'

    monkeypatch.setattr(cb, "_gemini_once", fake_once)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "primary")
    monkeypatch.setenv("SOTTO_FALLBACK_MODEL", "gemini-2.5-pro")
    monkeypatch.setenv("SOTTO_FALLBACK_API_KEY", "backup")
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    out = cb.call_gemini("p", {})
    assert out == '{"markdown": "ok"}'
    assert calls[0][0] == cb.os.environ.get("SOTTO_GEMINI_MODEL", "gemini-3-flash-preview")
    assert calls[1][:2] == ("gemini-2.5-pro", "backup") and calls[1][2] == " [fallback]"  # used backup


def test_gemini_no_fallback_configured_reraises(monkeypatch):
    import urllib.error

    def fake_once(model, key, prompt, label=""):
        raise urllib.error.HTTPError("u", 429, "RESOURCE_EXHAUSTED", {}, None)

    monkeypatch.setattr(cb, "_gemini_once", fake_once)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "primary")
    monkeypatch.delenv("SOTTO_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("SOTTO_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    import pytest
    with pytest.raises(urllib.error.HTTPError):
        cb.call_gemini("p", {})


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
    assert cb._env_tz() == "America/Los_Angeles"
    assert cb._user_tz_offset([]) == "America/Los_Angeles"   # env fallback when no events carry an offset
    import datetime
    from zoneinfo import ZoneInfo
    expected = datetime.datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    assert cb._user_local_date(cb._env_tz()) == expected


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
    assert cb._unwrap_local(ld) is ld
    # {result: {...}} wrapper (what the hermes-results file holds)
    assert cb._unwrap_local({"result": ld}) == ld
    # MCP tool result: structuredContent
    assert cb._unwrap_local({"structuredContent": ld, "isError": False}) == ld
    # MCP tool result: content[0].text JSON string
    import json as _j
    assert cb._unwrap_local({"content": [{"type": "text", "text": _j.dumps(ld)}]}) == ld
    # nested: result → structuredContent
    assert cb._unwrap_local({"result": {"structuredContent": ld}}) == ld
    # garbage → {}
    assert cb._unwrap_local("nope") == {} and cb._unwrap_local({"junk": 1}) == {"junk": 1}
