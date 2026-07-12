"""compose_brief.py compat surface — locks the `import compose_brief as cb` contract.

compose_brief.py was split from a 2,400-line monolith into focused modules under _shared/lib/
(textutil, timeutil, gemini, render_local). ~10 sibling scripts still do `import compose_brief as cb`
and reach helpers as `cb.<name>`. compose_brief.py re-imports every moved name at its old location.
This test fails loudly if a future edit drops one of those aliases (or changes compose's signature),
so the mechanical-move guarantee can't silently rot.
"""
import importlib.util
import inspect
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))

spec = importlib.util.spec_from_file_location("compose_brief", os.path.join(ROOT, "_shared", "scripts", "compose_brief.py"))
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)


# Names sibling scripts actually reach via `cb.<name>` today (grep of `cb.` across the pack).
# Dropping any of these breaks a live import path.
EXTERNAL_CONTRACT = [
    "CRITIC_AUTO_MIN_ACTIONS", "CRITIC_AUTO_MIN_PAYLOAD_CHARS", "LOCAL_SNAPSHOT_TTL_HOURS",
    "MAX_ATTENDEES_TO_RESEARCH", "RESEARCH_HORIZON_HOURS", "_action_tap_link", "_arr",
    "_base_domain", "_correlate_signals", "_coverage_line", "_critic_decision", "_critic_mode",
    "_detect_escalation", "_env_tz", "_event_link_map", "_format_birthdays",
    "_group_messages_into_threads", "_is_excluded_domain", "_is_likely_automated", "_is_sent_email",
    "_load_prompt", "_local_fallback", "_local_has_data", "_looks_like_phone_number", "_names_match",
    "_normalize_local", "_now_local", "_obj", "_parse_ts", "_resolve_tz", "_s",
    "_save_local_snapshot", "_sender_addr", "_snapshot_path", "_thread_needs_response",
    "_unwrap_local", "_user_local_date", "_user_tz_offset", "build_contact_lookup",
    "build_data_manifest", "build_prompt", "call_gemini", "compose", "configured_tz", "os",
    "resolve_contact_names", "run_critic", "select_attendees_for_research",
]

# The full set of helpers moved into _shared/lib/ and re-exported for the compat surface. Locking the
# whole surface (not just today's external callers) keeps a future sibling script's `cb.<helper>` safe.
MOVED_FROM_TEXTUTIL = [
    "_arr", "_obj", "_s", "_digits", "normalize_phone_for_comparison", "_format_phone_for_display",
    "_normalize_identifier", "_normalize_name_key", "_names_match", "_looks_like_phone_number",
    "_is_likely_automated", "_extract_sender_name", "_HOSTING_DOMAINS", "_CONSUMER_DOMAINS",
    "_is_excluded_domain", "_base_domain", "_sender_addr",
]
MOVED_FROM_TIMEUTIL = [
    "_parse_ts", "_date_only", "_tz_offset_minutes", "_env_tz", "_settings_path", "load_settings",
    "configured_tz", "_resolve_tz", "_now_local", "_user_tz_offset", "_user_local_date", "_time_frame",
]
MOVED_FROM_GEMINI = ["_diag", "_gemini_once", "_is_retryable"]
MOVED_FROM_RENDER = [
    "DEFERRED_UNREAD_PROMPT_CAP", "EMAIL_BODY_MAX", "build_contact_lookup", "resolve_imessage_name",
    "resolve_whatsapp_name", "resolve_call_name", "build_canonical_resolver", "_build_connected_dict",
    "_process_missed_calls", "_process_wa_missed_calls", "_process_recent_calls",
    "resolve_contact_names", "_norm_escalation_tone", "_action_age", "_SYSTEM_MESSAGE_PATTERNS",
    "_ASK_OR_COMMITMENT_PATTERN", "_SHORT_ACK_MAX_CHARS", "_is_system_message",
    "_compute_last_unreplied_ask", "_group_messages_into_threads", "_thread_last_is_user",
    "_thread_needs_response", "_thread_is_known_person", "_format_thread_as_text",
    "_format_threads_as_text", "_is_sent_email", "_trim_email", "_format_emails", "_format_calendar",
    "MAX_ATTENDEES_TO_RESEARCH", "RESEARCH_HORIZON_HOURS", "_LOW_QUALITY_PROFILE_PHRASES",
    "_is_high_quality_profile", "_known_identities", "_format_attendee_research", "_format_reminders",
    "_format_birthdays", "_format_missed_calls", "_format_recent_calls", "_stale_local_note",
    "_format_source_availability", "_format_deferred_unread", "_format_stale_threads",
    "_format_past_commitments", "_format_action_ledger", "_format_attention_queue",
    "_format_relationship_insights", "_format_knowledge_section", "_format_contact_notes",
    "_format_apple_notes", "_format_granola_meetings", "_format_top_browsing_domains",
    "_format_search_queries", "_format_screen_time", "_format_recent_files", "_format_meeting_archive",
    "_format_reconciliation", "_format_signal_scores", "_format_granola_context",
    "_format_file_matches", "_format_domain_research", "_format_escalation_signals",
]

FULL_COMPAT_SURFACE = sorted(set(
    EXTERNAL_CONTRACT + MOVED_FROM_TEXTUTIL + MOVED_FROM_TIMEUTIL + MOVED_FROM_GEMINI + MOVED_FROM_RENDER
))


def test_every_compat_name_is_reachable_on_cb():
    missing = [n for n in FULL_COMPAT_SURFACE if not hasattr(cb, n)]
    assert not missing, f"compat surface regressed — missing from compose_brief: {missing}"


def test_external_contract_names_are_reachable():
    # The exact names sibling scripts reach via `cb.<name>` today.
    missing = [n for n in EXTERNAL_CONTRACT if not hasattr(cb, n)]
    assert not missing, f"external `import compose_brief as cb` contract broke: {missing}"


def test_moved_helpers_resolve_to_lib_modules():
    # The re-exported helper is the SAME object the lib module defines (a real re-export, not a shadow).
    import textutil, timeutil, gemini, render_local
    for name in MOVED_FROM_TEXTUTIL:
        assert getattr(cb, name) is getattr(textutil, name), name
    for name in MOVED_FROM_TIMEUTIL:
        assert getattr(cb, name) is getattr(timeutil, name), name
    for name in MOVED_FROM_GEMINI:
        assert getattr(cb, name) is getattr(gemini, name), name
    for name in MOVED_FROM_RENDER:
        assert getattr(cb, name) is getattr(render_local, name), name


def test_compose_signature_unchanged():
    sig = inspect.signature(cb.compose)
    assert list(sig.parameters) == ["inputs", "llm", "critic"]
    assert sig.parameters["inputs"].default is inspect.Parameter.empty
    assert sig.parameters["llm"].default is cb.call_gemini
    assert sig.parameters["critic"].default is False


def test_call_gemini_honors_cb_level_gemini_once_patch(monkeypatch):
    # The reason call_gemini stays defined in compose_brief: sibling/test code monkeypatches
    # `cb._gemini_once` and expects call_gemini to pick it up. Lock that contract.
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setattr(cb, "_gemini_once", lambda model, key, prompt, label="": '{"ok": 1}')
    assert cb.call_gemini("p", {}) == '{"ok": 1}'
