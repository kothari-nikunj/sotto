"""compose_brief.py — input-wiring tests: top-level SKILL inputs must reach the renderers.

Guards the integration gap the end-to-end review found: granola + the knowledge graph were
passed at the top level but the renderers read everything from `local`, so they were dropped.
"""
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


def test_top_level_granola_reaches_prompt():
    inputs = {"type": "morning", "google": {"events": []}, "local": {},
              "granola": {"meetings": [
                  {"title": "Sync with Acme", "date": "2026-06-23",
                   "ai_summary": "Discussed the pilot rollout and pricing.",
                   "attendee_emails": ["sarah@acme.com"]}]}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "Sync with Acme" in prompt
    assert "pilot rollout" in prompt


def test_top_level_granola_as_bare_list():
    inputs = {"type": "morning", "google": {"events": []}, "local": {},
              "granola": [{"title": "1:1 with Devon", "date": "2026-06-23", "your_notes": "Roadmap chat."}]}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "1:1 with Devon" in prompt


def test_prior_knowledge_named_keys_reach_prompt():
    inputs = {"type": "morning", "google": {"events": []}, "local": {},
              "prior_knowledge": {
                  "person_knowledge": {"sarah-chen": "Sarah Chen (sarah-chen) | CTO @ Acme\n= raised Series B"},
                  "company_knowledge": {"acme": "Acme — dev-tools, 50 people"}}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "Sarah Chen" in prompt
    assert "Acme — dev-tools" in prompt


def test_prior_knowledge_bare_slug_map_treated_as_people():
    # knowledge_query.py emits a bare {slug: packed} map — must still land as person knowledge.
    inputs = {"type": "morning", "google": {"events": []}, "local": {},
              "prior_knowledge": {"devon-park": "Devon Park (devon-park) | Investor @ Northstar\n= leads seed deals"}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "Devon Park" in prompt


def test_source_status_becomes_source_availability_warning():
    inputs = {"type": "morning", "google": {"events": []},
              "local": {"source_status": {"whatsapp": "needs_fda", "imessage": "ok", "reminders": "unavailable"}}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "Data Source Availability" in prompt
    assert "Unavailable on this device" in prompt
    assert "WhatsApp" in prompt          # needs_fda → unavailable, with the friendly label
    assert "Apple Reminders" in prompt   # unavailable → unavailable


def test_explicit_local_values_are_not_clobbered():
    inputs = {"type": "morning", "google": {"events": []},
              "granola": {"meetings": [{"title": "FROM TOP LEVEL"}]},
              "local": {"granola_meetings": [{"title": "ALREADY IN LOCAL", "your_notes": "x"}]}}
    local = cb._normalize_local(inputs)
    assert local["granola_meetings"][0]["title"] == "ALREADY IN LOCAL"


def test_no_extra_inputs_is_noop():
    local = cb._normalize_local({"type": "morning", "local": {"imessage": []}})
    assert "granola_meetings" not in local
    assert "_source_availability" not in local


def test_cli_separate_files_assembles_inputs(tmp_path):
    """The one-command CLI: a file per source, no hand-assembled JSON. Local data must reach the brief."""
    import subprocess, sys as _sys
    (tmp_path / "local.json").write_text(json.dumps(
        {"imessage": [{"handle": "+1555", "is_from_me": False, "timestamp": "2026-06-24 09:00:00",
                       "text": "ping from imessage", "is_group_chat": False}], "source_status": {"imessage": "ok"}}))
    (tmp_path / "cal.json").write_text(json.dumps([{"summary": "Pitch", "start": "2026-06-24T22:00:00+00:00"}]))
    stub = tmp_path / "stub.json"
    stub.write_text(json.dumps({"brief_markdown": "# Real", "actions": []}))
    script = os.path.join(ROOT, "_shared", "scripts", "compose_brief.py")
    out = subprocess.run(
        [_sys.executable, script, "--type", "evening",
         "--local", str(tmp_path / "local.json"), "--calendar", str(tmp_path / "cal.json")],
        capture_output=True, text=True, env={**os.environ, "SOTTO_LLM_STUB": str(stub)})
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["brief_markdown"] == "# Real"


def test_cli_local_reaches_prompt():
    """With --local, the read_local data is rendered into the FLEX prompt (not dropped)."""
    inputs = {"type": "evening", "google": {"events": []},
              "local": {"contacts": [{"name": "Sarah Chen", "phones": ["+15551234567"]}],
                        "imessage": [{"handle": "+15551234567", "is_from_me": False,
                                      "timestamp": "2026-06-24 09:00:00", "text": "unique-marker-xyz",
                                      "is_group_chat": False}]}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "unique-marker-xyz" in prompt


# ---------------------------------------------------------------------------
# Contact filter — port of the Mac pipeline's is_known_contact gate
# (thread-processing.ts). Unknown senders (raw phone / shortcode / OTP spam)
# are dropped before reaching the FLEX prompt.
# ---------------------------------------------------------------------------

def test_looks_like_phone_number():
    assert cb._looks_like_phone_number("")           # empty
    assert cb._looks_like_phone_number("+15551234567")  # starts with +
    assert cb._looks_like_phone_number("(555) 123-4567")  # starts with (
    assert cb._looks_like_phone_number("262966")     # shortcode, all digits
    assert not cb._looks_like_phone_number("Sarah Chen")
    assert not cb._looks_like_phone_number("Devon")


def test_unknown_sender_thread_is_dropped():
    """A thread that only ever resolved to a phone number (no contact, not on the calendar)
    must not reach the prompt — matches the Mac's is_known_contact filter."""
    inputs = {"type": "evening", "google": {"events": []},
              "local": {"imessage": [{"handle": "+15550000000", "is_from_me": False,
                                      "timestamp": "2026-06-24 09:00:00", "text": "OTP-spam-marker",
                                      "is_group_chat": False}]}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "OTP-spam-marker" not in prompt


def test_known_contact_thread_is_kept():
    """A thread whose Contacts resolved a real name is kept even if the handle is a phone."""
    inputs = {"type": "evening", "google": {"events": []},
              "local": {"contacts": [{"name": "Dhruv Patel", "phones": ["+15550000000"]}],
                        "imessage": [{"handle": "+15550000000", "is_from_me": False,
                                      "timestamp": "2026-06-24 09:00:00", "text": "known-friend-marker",
                                      "is_group_chat": False}]}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "known-friend-marker" in prompt


def test_calendar_attendee_rescues_unresolved_thread():
    """An unresolved (phone-named) WhatsApp thread is rescued if the JID/identifier matches a
    calendar attendee email — the isKnownPerson rescue path."""
    inputs = {"type": "morning",
              "google": {"events": [{"summary": "Pitch", "start": "2026-06-25T22:00:00+00:00",
                                     "attendees": [{"email": "guest@startup.com", "displayName": "Guest VC"}]}]},
              "local": {"whatsapp": [{"contact_jid": "guest@startup.com", "is_from_me": False,
                                      "timestamp": "2026-06-24 09:00:00", "text": "rescued-marker",
                                      "is_group_chat": False}]}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "rescued-marker" in prompt


def test_group_chat_thread_is_always_kept():
    """Group chats are rooms, not unknown individuals — never dropped by the contact filter."""
    inputs = {"type": "evening", "google": {"events": []},
              "local": {"imessage": [{"handle": "+15550000000", "is_from_me": False,
                                      "timestamp": "2026-06-24 09:00:00", "text": "group-marker",
                                      "is_group_chat": True}]}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "group-marker" in prompt


# ---------------------------------------------------------------------------
# Contact-matching decision layer — ports of name-matching.ts / thread-processing.ts
# ---------------------------------------------------------------------------

def test_strict_names_match():
    # Exact, last-name, last-initial, 3-char prefix → match.
    assert cb._names_match("Jake Helberg", "Jake Helberg")
    assert cb._names_match("Jake H", "Jake Helberg")
    assert cb._names_match("Jake Hel", "Jake Helberg")
    # First-name-only must NOT match (the loose substring bug this replaced).
    assert not cb._names_match("Marcus", "Marcus Wallace")
    assert not cb._names_match("Sam", "Samantha Lee")
    # Diacritics normalized.
    assert cb._names_match("Tomás García", "Tomas Garcia")


def test_is_likely_automated():
    assert cb._is_likely_automated("no-reply@stripe.com")
    assert cb._is_likely_automated("notifications@github.com")
    assert cb._is_likely_automated("billing@aws.amazon.com")
    assert not cb._is_likely_automated("sarah@acme.com")


def test_system_messages_stripped_from_thread():
    inputs = {"type": "evening", "google": {"events": []},
              "local": {"contacts": [{"name": "Dhruv Patel", "phones": ["+15550000000"]}],
                        "imessage": [
                            {"handle": "+15550000000", "is_from_me": False, "is_group_chat": False,
                             "timestamp": "2026-06-24 09:00:00", "text": "Missed voice call"},
                            {"handle": "+15550000000", "is_from_me": False, "is_group_chat": False,
                             "timestamp": "2026-06-24 09:05:00", "text": "real-question-marker?"}]}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "real-question-marker?" in prompt
    assert "Missed voice call" not in prompt


def test_short_ack_keeps_thread_in_needs_response():
    # The other side asked; user only sent a short "ok" → still needs a real response.
    threads = cb._group_messages_into_threads([
        {"handle": "+1", "is_from_me": False, "timestamp": "2026-06-24 09:00:00",
         "text": "Can you send me the deck please?"},
        {"handle": "+1", "is_from_me": True, "timestamp": "2026-06-24 09:01:00", "text": "ok"},
    ], "imessage")
    assert threads[0]["last_unreplied_ask"] is True
    assert cb._thread_needs_response(threads[0]) is True
    # A substantive reply clears it.
    threads2 = cb._group_messages_into_threads([
        {"handle": "+1", "is_from_me": False, "timestamp": "2026-06-24 09:00:00",
         "text": "Can you send me the deck please?"},
        {"handle": "+1", "is_from_me": True, "timestamp": "2026-06-24 09:01:00",
         "text": "Sure, attaching it now — let me know if the formatting works."},
    ], "imessage")
    assert threads2[0]["last_unreplied_ask"] is False


def test_group_chat_detected_when_any_message_flagged():
    # Group flag on a later message must still classify the whole thread as a group.
    threads = cb._group_messages_into_threads([
        {"handle": "+1", "is_from_me": False, "timestamp": "2026-06-24 09:00:00",
         "text": "hi", "is_group_chat": False},
        {"handle": "+1", "is_from_me": False, "timestamp": "2026-06-24 09:05:00",
         "text": "yo", "is_group_chat": True},
    ], "imessage")
    assert threads[0]["is_group_chat"] is True


def test_escalation_detection_cross_channel():
    from datetime import datetime, timezone
    now = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)
    local = {"imessage": [{"handle": "+1", "resolved_name": "Sarah Chen", "is_from_me": False,
                           "is_group_chat": False, "timestamp": "2026-06-24 09:00:00"}],
             "whatsapp": [{"contact_jid": "x", "resolved_name": "Sarah Chen", "is_from_me": False,
                           "is_group_chat": False, "timestamp": "2026-06-24 15:00:00"}],
             "missed_calls": []}
    emails = [{"from": "Sarah Chen <sarah@acme.com>", "date": "2026-06-25 08:00:00"}]
    r = cb._detect_escalation(local, emails, now)
    assert r and r[0]["name"] == "sarah chen"
    assert r[0]["escalation_level"] == 3
    assert "iMessage" in r[0]["narrative"] and "email" in r[0]["narrative"]


def test_escalation_ignores_single_channel_and_old():
    from datetime import datetime, timezone
    now = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Single channel → no escalation; and a 2nd channel 3 days old is outside the 48h window.
    local = {"imessage": [{"handle": "+1", "resolved_name": "Bob", "is_from_me": False,
                           "is_group_chat": False, "timestamp": "2026-06-24 09:00:00"}],
             "whatsapp": [{"contact_jid": "x", "resolved_name": "Bob", "is_from_me": False,
                           "is_group_chat": False, "timestamp": "2026-06-21 09:00:00"}]}
    assert cb._detect_escalation(local, [], now) == []


def test_canonical_id_rescues_phone_named_thread():
    # An unresolved (phone-named) thread is kept when its canonical_id is a known graph person.
    inputs = {"type": "evening", "google": {"events": []},
              "local": {"action_ledger": [{"canonical_id": "c_abc", "status": "open",
                                           "action_type": "reply", "contact_name": "X",
                                           "channel": "imessage", "summary": "s"}],
                        "imessage": [{"handle": "+15557778888", "is_from_me": False,
                                      "is_group_chat": False, "canonical_id": "c_abc",
                                      "timestamp": "2026-06-24 09:00:00", "text": "ledger-rescue-marker"}]}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert "ledger-rescue-marker" in prompt
