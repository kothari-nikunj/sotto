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


def test_group_chat_guid_collapses_senders_into_one_thread():
    # Two different senders in the SAME group (same chat_guid) must collapse into ONE thread keyed by
    # the group id — not split per-sender (which is what let the LLM re-merge + re-label the group).
    lookup = cb.build_contact_lookup([
        {"name": "Alice Ng", "phones": ["+14150000001"]},
        {"name": "Bob Ray", "phones": ["+14150000002"]},
    ])
    threads = cb._group_messages_into_threads([
        {"handle": "+14150000001", "is_from_me": False, "timestamp": "2026-06-24 09:00:00",
         "text": "who's driving?", "is_group_chat": True, "chat_guid": "grp-1",
         "group_name": "Weekend Crew", "group_participants": ["+14150000001", "+14150000002"]},
        {"handle": "+14150000002", "is_from_me": False, "timestamp": "2026-06-24 09:05:00",
         "text": "I can", "is_group_chat": True, "chat_guid": "grp-1",
         "group_participants": ["+14150000002"]},
    ], "imessage", lookup)
    assert len(threads) == 1
    t = threads[0]
    assert t["is_group_chat"] is True
    assert t["chat_guid"] == "grp-1"
    # user-set group name is used VERBATIM as the display label / name
    assert t["display_label"] == "Weekend Crew"
    assert t["name"] == "Weekend Crew"
    assert set(t["group_participants"]) == {"+14150000001", "+14150000002"}
    # rendered header carries the group label, no deep link, and never a single sender
    header = cb._format_thread_as_text(t, "imessage").splitlines()[0]
    assert header == "### Weekend Crew [GROUP - no deep link]"


def test_group_unnamed_builds_participant_label():
    # No group_name → iMessage-style label from resolved participant NAMES ("Alice, Bob & N others").
    lookup = cb.build_contact_lookup([
        {"name": "Alice Ng", "phones": ["+14150000001"]},
        {"name": "Bob Ray", "phones": ["+14150000002"]},
    ])
    threads = cb._group_messages_into_threads([
        {"handle": "+14150000001", "is_from_me": False, "timestamp": "2026-06-24 09:00:00",
         "text": "hey", "is_group_chat": True, "chat_guid": "grp-2",
         "group_participants": ["+14150000001", "+14150000002", "+14159990000"]},
    ], "imessage", lookup)
    # 2 resolved names + 1 unresolved phone → "Alice Ng, Bob Ray & 1 other"
    assert threads[0]["name"] == "Alice Ng, Bob Ray & 1 other"


def test_group_backward_compat_no_new_fields():
    # A group message with NONE of the new Bridge fields still renders as a group without crashing and
    # gets the sender-derived (phone) name, never a topical invention.
    threads = cb._group_messages_into_threads([
        {"handle": "+14155558888", "is_from_me": False, "timestamp": "2026-06-24 09:00:00",
         "text": "anyone free to review the deck?", "is_group_chat": True},
    ], "imessage")
    t = threads[0]
    assert t["is_group_chat"] is True
    assert t["chat_guid"] is None
    assert t["name"] == "+1 (415) 555-8888"
    assert t["display_label"] == "+1 (415) 555-8888"


def test_whatsapp_named_group_uses_subject_verbatim():
    # A WhatsApp group's chat_guid == its group JID; the Bridge's group_name (ZPARTNERNAME subject) is
    # used VERBATIM as the header — SAME path as iMessage. All senders collapse into ONE thread.
    lookup = cb.build_contact_lookup([
        {"name": "Nadia Ops", "phones": ["+18005551000"]},
        {"name": "Yuki Tanaka", "phones": ["+818012345678"]},
    ])
    threads = cb._group_messages_into_threads([
        {"contact_jid": "120363111@g.us", "partner_name": "Weekend Hikers", "is_from_me": False,
         "timestamp": "2026-06-24 09:00:00", "text": "trailhead at 7?", "is_group_chat": True,
         "chat_guid": "120363111@g.us", "group_name": "Weekend Hikers",
         "group_participants": ["18005551000@s.whatsapp.net", "818012345678@s.whatsapp.net"]},
        {"contact_jid": "120363111@g.us", "partner_name": "Weekend Hikers", "is_from_me": False,
         "timestamp": "2026-06-24 09:05:00", "text": "works for me", "is_group_chat": True,
         "chat_guid": "120363111@g.us", "group_name": "Weekend Hikers",
         "group_participants": ["18005551000@s.whatsapp.net"]},
    ], "whatsapp", lookup)
    assert len(threads) == 1
    t = threads[0]
    assert t["is_group_chat"] is True
    assert t["chat_guid"] == "120363111@g.us"
    assert t["name"] == "Weekend Hikers"
    header = cb._format_thread_as_text(t, "whatsapp").splitlines()[0]
    assert header == "### Weekend Hikers [GROUP - no deep link]"


def test_whatsapp_unnamed_group_builds_participant_label_from_member_jids():
    # No subject → participant label built from member JIDs resolved to contact names via
    # resolve_whatsapp_name (phone_from_jid → lookup). Never the raw group JID, never a topic.
    lookup = cb.build_contact_lookup([
        {"name": "Nadia Ops", "phones": ["+18005551000"]},
        {"name": "Yuki Tanaka", "phones": ["+818012345678"]},
    ])
    threads = cb._group_messages_into_threads([
        {"contact_jid": "120363222@g.us", "partner_name": "", "is_from_me": False,
         "timestamp": "2026-06-24 09:00:00", "text": "who's bringing snacks?", "is_group_chat": True,
         "chat_guid": "120363222@g.us", "group_name": None,
         "group_participants": ["18005551000@s.whatsapp.net", "818012345678@s.whatsapp.net"]},
    ], "whatsapp", lookup)
    t = threads[0]
    assert t["name"] == "Nadia Ops & Yuki Tanaka"
    header = cb._format_thread_as_text(t, "whatsapp").splitlines()[0]
    assert header == "### Nadia Ops & Yuki Tanaka [GROUP - no deep link]"
    assert "@g.us" not in header  # raw JID never surfaces as the label


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
