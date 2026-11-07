"""Reader-visible phase-two contracts: honest health, exact surfacing, one review question."""
from datetime import datetime, timezone
import json

import pytest

import compose_brief as cb
import source_context
import review_candidates
import retune_scan
import ledger_io
import master_file

NOW = datetime(2026, 9, 18, 18, tzinfo=timezone.utc)


@pytest.fixture
def volume(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "UTC")
    monkeypatch.setenv("SOTTO_DEPLOYMENT_MODE", "managed")
    monkeypatch.setattr(source_context, "allowed", lambda source: source == "gmail")
    monkeypatch.setattr(cb, "explicit_prefs", lambda: {"mute_people": [], "mute_senders": [], "mute_sections": []})
    (tmp_path / "knowledge").mkdir()
    return tmp_path


def health(volume, state, inputs=None):
    (volume / "knowledge/history-state.json").write_text(json.dumps(state))
    return cb._append_history_health({"brief_markdown": "Good morning\n\nNeeds Attention Now\nAlex: reply."},
                                     inputs or {}, NOW)["brief_markdown"]


def test_history_health_is_honest_and_disappears_on_recovery(volume):
    stalled = {"sources": {"gmail": {"error_code": "provider_request_rejected"},
                           "imessage": {"error": "ConnectionError"}}}
    text = health(volume, stalled)
    assert "Learning from older Gmail messages is paused" in text
    assert "iMessage" not in text and "provider_request" not in text
    assert "past context" in text and "Alex: reply" in text
    assert "past context" not in health(volume, {"sources": {"gmail": {"last_success": NOW.isoformat()}}})


def test_history_hold_is_not_a_current_source_outage(volume):
    state = {"receipt": {"dreamer": {"reason": "background_budget_not_configured"}}}
    assert "budget or your explicit unlimited choice" in health(volume, state)
    assert "unavailable" not in health(volume, state)


def test_stopped_worker_is_reported_and_disabled_source_is_silent(volume, monkeypatch):
    state = {"sources": {"gmail": {"last_success": "2026-09-10T12:00:00Z"}}}
    assert "past context" in health(volume, state)
    monkeypatch.setattr(source_context, "allowed", lambda source: False)
    assert "past context" not in health(volume, state)


def test_never_successful_history_does_not_repeat_a_pending_warning(volume):
    text = health(volume, {"sources": {"gmail": {"pages": 0}}})
    assert "hasn't finished loading" not in text
    assert text.startswith("Good morning")


def test_a_source_whose_live_feed_is_already_reported_down_gets_no_second_line(volume, monkeypatch):
    """A Mac asleep all weekend: the brief already says today's iMessage feed is unavailable, and
    its history stopped for the same reason. One cause, one line."""
    monkeypatch.setattr(source_context, "allowed", lambda source: source in ("gmail", "imessage"))
    state = {"sources": {"imessage": {"last_success": "2026-09-10T12:00:00Z"},
                         "gmail": {"last_success": NOW.isoformat()}}}
    down = {"local": {"source_status": {"imessage": "unavailable"}}}
    assert "past context" not in health(volume, state, down)
    # …but a feed that is up today and a history that stopped is still a real disclosure
    up = {"local": {"source_status": {"imessage": "ok"}}}
    assert "Learning from older iMessage messages is paused" in health(volume, state, up)
    # a stale Bridge snapshot saying "ok" never outranks what this brief derived: unavailable now
    stale = {"local": {"source_status": {"imessage": "ok"}, "_source_availability": {"imessage": "unavailable"}}}
    assert "past context" not in health(volume, state, stale)


def test_real_source_failure_wins_over_background_hold(volume):
    text = health(volume, {"sources": {"gmail": {"error_code": "provider_request_rejected"}},
                           "receipt": {"dreamer": {"reason": "background_budget_exhausted"}}})
    assert "Learning from older Gmail messages is paused" in text
    assert "spending limit" not in text and "provider_request_rejected" not in text


def row(key, summary):
    return {"anchor_key": key, "status": "open", "contact_name": "Alex Chen",
            "contact_identifier": "alex@example.com", "summary": summary}


def test_surface_attribution_does_not_count_other_asks_or_hidden_markers():
    first, second = row("one", "Send the signed agreement"), row("two", "Confirm the lunch invitation")
    text = "Alex Chen: Send the signed agreement. <!--id:alex@example.com--> <!--loop:one-->\n<!--loop:two-->"
    assert cb._represented_loop_rows([first, second], text) == [first]
    assert cb._represented_loop_rows([first, second], "Alex Chen sent a birthday greeting. <!--id:alex@example.com-->") == []
    # Editorial prose can reorder the ask, but all its content words must remain visible.
    prose = "**Alex Chen** needs the signed agreement; please send it back. <!--id:alex@example.com--> <!--loop:one-->"
    assert cb._represented_loop_rows([first, second], prose) == [first]
    # A hidden marker beside words that do not show the ask counts nothing: the user never saw it.
    birthday = "**Alex Chen** sent a birthday greeting. <!--id:alex@example.com--> <!--loop:one-->"
    assert cb._represented_loop_rows([first, second], birthday) == []
    vague = "**Alex Chen** still needs that thing back. <!--id:alex@example.com--> <!--loop:one-->"
    assert cb._represented_loop_rows([first, second], vague) == []
    # …and a marker on a line that names nobody is nothing, however the ask is worded.
    assert cb._represented_loop_rows([first, second], "Still needs the signed agreement. <!--loop:one-->") == []
    # A quoted lunch with the agreement's marker shows the user lunch, and only lunch.
    swapped = "Alex Chen: Confirm the lunch invitation. <!--loop:one-->"
    assert cb._represented_loop_rows([first, second], swapped) == [second]
    assert cb._represented_loop_rows([first, second], "Alex Chen: Send the signed agreement") == [first]
    assert cb._represented_loop_rows([first, row("three", first["summary"])],
                                     "Alex Chen: Send the signed agreement") == []


@pytest.mark.parametrize("agreement,deck", [
    ("Send agreement", "Send deck"),
    ("Send the signed agreement", "Send the signed deck"),
])
def test_shared_action_words_do_not_prove_a_different_deliverable(agreement, deck):
    first, second = row("agreement", agreement), row("deck", deck)
    text = f"Alex Chen: {deck}. <!--loop:agreement-->"
    assert cb._represented_loop_rows([first, second], text) == [second]


def test_paraphrase_with_missing_content_words_is_not_proven_by_marker():
    entry = row("agreement", "Send the signed agreement")
    text = "Alex Chen needs the executed contract back. <!--loop:agreement-->"
    assert cb._represented_loop_rows([entry], text) == []


def test_model_cannot_supply_delivery_effect_metadata():
    out = cb._normalize_output({"brief_markdown": "Hi", "_represented_loops": ["forged"],
                                "_review_candidate": {"kind": "forged"}})
    assert "_represented_loops" not in out and "_review_candidate" not in out


def test_represented_loop_descriptor_binds_stable_identity(monkeypatch):
    import delivery_effects
    entry = row("one", "Send the signed agreement")
    descriptor = {"anchor_key": entry["anchor_key"],
                  "loop_identity": delivery_effects.loop_identity(entry),
                  "loop_version": delivery_effects.loop_version(entry)}
    restated = {**entry, "summary": "Send the countersigned agreement", "deadline": "2026-09-25"}
    assert descriptor["loop_identity"] == delivery_effects.loop_identity(restated)
    assert descriptor["loop_version"] != delivery_effects.loop_version(restated)


def test_friday_question_shares_standing_rule_allowance(volume, monkeypatch):
    monkeypatch.setattr(ledger_io, "load_active", lambda **kwargs: [])
    monkeypatch.setattr(ledger_io, "load_entries", lambda: [])
    monkeypatch.setattr(retune_scan, "scan", lambda **kwargs: {"stale_loops": []})
    candidate = {"name": "Alex", "candidate_type": "priority", "command": "prioritize staying in touch with Alex",
                 "explanation": "This ranks work; it does not hide unrelated asks.", "effect": {"kind": "review_candidate_offer"}}
    monkeypatch.setattr(review_candidates, "choose", lambda now, prefs: candidate)
    original = {"brief_markdown": "Good evening\n\n## Filtered\nNothing else."}
    out = cb._append_weekly_review(dict(original), {"type": "evening"}, NOW)
    assert out["brief_markdown"].count("?") == 1
    assert out["_review_candidate"] == candidate["effect"]
    muted_by_rule = cb._append_weekly_review(dict(original), {"type": "evening", "_procedure_offer": "No Friday meetings"}, NOW)
    assert "_review_candidate" not in muted_by_rule and "Would you like" not in muted_by_rule["brief_markdown"]
    assert cb._append_weekly_review(dict(original), {"type": "morning"}, NOW) == original


@pytest.mark.parametrize('repeated', [False, True])
def test_weekly_review_adds_choice_without_repeating_visible_task(volume, monkeypatch, repeated):
    import pending_offer
    entry = row('agreement', 'Send the signed agreement')
    scanned = {'anchor_key': 'agreement', 'name': 'Alex Chen', 'what': entry['summary'],
               'times_surfaced': retune_scan.STALE_SURFACED if repeated else 0,
               'surface_count_provenance': 'accepted_delivery'}
    monkeypatch.setattr(ledger_io, 'load_active', lambda **kwargs: [entry])
    monkeypatch.setattr(ledger_io, 'load_entries', lambda: [])
    monkeypatch.setattr(retune_scan, 'scan', lambda **kwargs: {'stale_loops': [scanned]})
    monkeypatch.setattr(review_candidates, 'choose', lambda *args: None)
    offers = []
    monkeypatch.setattr(pending_offer, 'set_offer', lambda *args, **kwargs: offers.append(kwargs))
    out = {'brief_markdown': '## Needs Attention Now\nAlex Chen: Send the signed agreement. '
                             '<!--id:alex@example.com--> <!--loop:agreement-->\n'}
    cb._append_weekly_review(out, {'type': 'evening'}, NOW)
    assert out['brief_markdown'].count('Send the signed agreement') == 1
    assert '## Weekly review' in out['brief_markdown']
    assert cb._represented_loop_rows([entry], out['brief_markdown']) == [entry]
    if repeated:
        assert 'Keep it, snooze it, or let it go?' in out['brief_markdown']
        assert out['_review_question'] == 'weekly_repeated'
        assert offers == [{'person': 'Alex Chen', 'detail': entry['summary'], 'anchor_key': 'agreement'}]
    else:
        assert 'Keep, snooze or dismiss any of the unfinished tasks above' in out['brief_markdown']
        assert not offers and '_review_question' not in out


def test_weekly_review_respects_source_bound_display_approval(volume, monkeypatch):
    import delivery_effects
    entry = row('agreement', 'Send the signed agreement')
    scanned = {'anchor_key': 'agreement', 'name': 'Alex Chen', 'what': entry['summary'],
               'times_surfaced': 0, 'surface_count_provenance': 'accepted_delivery'}
    monkeypatch.setattr(ledger_io, 'load_active', lambda **kwargs: [entry])
    monkeypatch.setattr(ledger_io, 'load_entries', lambda: [])
    monkeypatch.setattr(retune_scan, 'scan', lambda **kwargs: {'stale_loops': [scanned]})
    monkeypatch.setattr(review_candidates, 'choose', lambda *args: None)
    line = 'Alex Chen<!--id:alex@example.com-->: Return the countersigned contract. <!--loop:agreement-->'
    out = {'brief_markdown': '## Needs Attention Now\n' + line + '\n',
           '_reviewed_mentions': {'agreement': (delivery_effects.loop_version(entry), line)}}
    # The same approval that keeps Still open from repeating this task binds every later appendix.
    assert cb._append_still_open(dict(out), [entry], NOW, '2026-09-18')['brief_markdown'] == out['brief_markdown']
    cb._append_weekly_review(out, {'type': 'evening'}, NOW)
    assert 'Send the signed agreement' not in out['brief_markdown']
    assert 'Keep, snooze or dismiss any of the unfinished tasks above' in out['brief_markdown']
    stale = {**out, 'brief_markdown': '## Needs Attention Now\n' + line + '\n',
             '_reviewed_mentions': {'agreement': ('older-version', line)}}
    cb._append_weekly_review(stale, {'type': 'evening'}, NOW)
    assert 'Send the signed agreement <!--loop:agreement-->' in stale['brief_markdown']


def test_weekly_hidden_marker_cannot_suppress_the_reviewed_task(volume, monkeypatch):
    import pending_offer
    entry = row('agreement', 'Send the signed agreement')
    scanned = {'anchor_key': 'agreement', 'name': 'Alex Chen', 'what': entry['summary'],
               'times_surfaced': retune_scan.STALE_SURFACED,
               'surface_count_provenance': 'accepted_delivery'}
    monkeypatch.setattr(ledger_io, 'load_active', lambda **kwargs: [entry])
    monkeypatch.setattr(ledger_io, 'load_entries', lambda: [])
    monkeypatch.setattr(retune_scan, 'scan', lambda **kwargs: {'stale_loops': [scanned]})
    offers = []
    monkeypatch.setattr(pending_offer, 'set_offer', lambda *args, **kwargs: offers.append(kwargs))
    out = {'brief_markdown': '## Needs Attention Now\nAlex Chen: Confirm lunch. <!--loop:agreement-->\n'}
    assert cb._represented_loop_rows([entry], out['brief_markdown']) == []
    cb._append_weekly_review(out, {'type': 'evening'}, NOW)
    weekly = out['brief_markdown'].split('## Weekly review', 1)[1]
    assert 'Send the signed agreement <!--loop:agreement-->' in weekly
    assert cb._represented_loop_rows([entry], out['brief_markdown']) == [entry]
    assert offers[0]['anchor_key'] == 'agreement' and offers[0]['detail'] in weekly


def test_quoted_question_does_not_consume_appended_friday_allowance(volume, monkeypatch):
    monkeypatch.setattr(ledger_io, "load_active", lambda **kwargs: [])
    monkeypatch.setattr(ledger_io, "load_entries", lambda: [])
    monkeypatch.setattr(retune_scan, "scan", lambda **kwargs: {"stale_loops": []})
    candidate = {"name": "Alex", "candidate_type": "priority",
                 "command": "prioritize staying in touch with Alex", "explanation": "Ranks work.",
                 "effect": {"kind": "review_candidate_offer"}}
    monkeypatch.setattr(review_candidates, "choose", lambda now, prefs: candidate)
    original = {"brief_markdown": "Can you confirm Friday?\n\n## Filtered\nNothing else."}
    out = cb._append_weekly_review(dict(original), {"type": "evening"}, NOW)
    assert out["brief_markdown"].count("?") == 2
    assert out["_review_question"] == "weekly_candidate"
    assert out["_review_candidate"] == candidate["effect"]


def test_standing_rule_ignores_quoted_question_but_respects_appended_question(volume):
    free = cb._append_procedure_offer(
        {"brief_markdown": "Dana asked, ‘Can you send the deck?’"},
        {"_procedure_offer": "Never schedule Friday calls"})
    assert free["brief_markdown"].count("?") == 2
    assert free["_review_question"] == "procedure"
    occupied = cb._append_procedure_offer(
        {"brief_markdown": "Good evening", "_review_question": "weekly_repeated"},
        {"_procedure_offer": "Never schedule Friday calls"})
    assert occupied["brief_markdown"] == "Good evening"


def test_legacy_capture_count_never_drives_repeated_question(volume, monkeypatch):
    monkeypatch.setattr(ledger_io, "load_active", lambda **kwargs: [])
    monkeypatch.setattr(ledger_io, "load_entries", lambda: [])
    monkeypatch.setattr(retune_scan, "scan", lambda **kwargs: {"stale_loops": [
        {"anchor_key": "one", "name": "Alex", "what": "Sign the agreement", "times_surfaced": 99,
         "surface_count_provenance": "unknown_legacy"}]})
    monkeypatch.setattr(review_candidates, "choose", lambda now, prefs: None)
    out = cb._append_weekly_review({"brief_markdown": "## Filtered\nNothing else."}, {"type": "evening"}, NOW)
    assert "brief deliveries" not in out["brief_markdown"]


def test_confirmed_priority_preserves_existing_authority_and_refuses_full_set(volume):
    master_file.set_section("Priorities", "Family\nFundraising")
    master_file.add_priority("Staying in touch with Alex")
    master_file.add_priority("staying in touch with Alex")
    assert [p["text"] for p in master_file.priorities()["priorities"]] == [
        "Family", "Fundraising", "Staying in touch with Alex"]
    before = master_file.read()
    with pytest.raises(ValueError, match="full"):
        master_file.add_priority("New inferred focus")
    assert master_file.read() == before


def test_priority_confirmation_cli_writes_once_and_returns_json(volume, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["master_file.py", "prioritize", "--text", "Stay in touch with Alex"])
    assert master_file.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"]
    assert [p["text"] for p in master_file.priorities()["priorities"]] == ["Stay in touch with Alex"]


def test_missing_history_row_is_silent_and_selfhost_requires_real_configuration(volume, monkeypatch):
    assert "history" not in health(volume, {})
    assert "history" not in health(volume, {"sources": {"imessage": {}}})
    monkeypatch.setenv("SOTTO_DEPLOYMENT_MODE", "selfhost")
    assert "history" not in health(volume, {})
    (volume / "knowledge/history-state.json").write_text("{}")
    out = cb._append_history_health({"brief_markdown": "Good morning"},
                                    {"local": {"whatsapp": [], "source_status": {"whatsapp": ""}}}, NOW)
    assert out["brief_markdown"] == "Good morning"


def test_selfhost_stale_failure_is_silent_when_current_source_is_not_configured(
        volume, monkeypatch):
    monkeypatch.setenv("SOTTO_DEPLOYMENT_MODE", "selfhost")
    monkeypatch.setattr(source_context, "allowed", lambda source: source == "whatsapp")
    state = {"sources": {"whatsapp": {"error": "failed every page"}}}
    inputs = {"local": {"whatsapp": [], "source_status": {"whatsapp": "not_configured"}}}
    (volume / "knowledge/history-state.json").write_text(json.dumps(state))
    out = cb._append_history_health({"brief_markdown": "Good morning"}, inputs, NOW)
    assert out["brief_markdown"] == "Good morning"


def test_selfhost_nested_disabled_status_overrides_old_success(volume, monkeypatch):
    monkeypatch.setenv("SOTTO_DEPLOYMENT_MODE", "selfhost")
    monkeypatch.setattr(source_context, "allowed", lambda source: source == "whatsapp")
    state = {"sources": {"whatsapp": {"last_success": "2026-09-01T00:00:00Z"}}}
    inputs = {"local": {"source_status": {"whatsapp": {"enabled": False}}}}
    (volume / "knowledge/history-state.json").write_text(json.dumps(state))
    out = cb._append_history_health({"brief_markdown": "Good morning"}, inputs, NOW)
    assert out["brief_markdown"] == "Good morning"


def test_named_loop_suppression_requires_the_specific_ask(volume):
    entry = row("one", "Send the signed agreement")
    prose = "Alex Chen still needs an answer. <!--id:alex@example.com-->"
    assert cb._represented_loop_rows([entry], prose) == []
    cb._record_named_loops([entry], prose, "2026-09-18", "evening")
    saved = json.loads((volume / "briefs/2026-09-18.evening.named.json").read_text())
    assert saved == {"anchor_keys": []}
    cb._record_named_loops([entry], "Alex Chen: Send the signed agreement.",
                           "2026-09-18", "evening")
    saved = json.loads((volume / "briefs/2026-09-18.evening.named.json").read_text())
    assert saved == {"anchor_keys": ["one"]}


def test_open_loop_markers_follow_deduplicated_asks():
    entries = [row("one", "Send the agreement"), row("two", "Send the agreement"),
               row("three", "Confirm lunch"), row("four", "Return the book"),
               row("five", "Schedule the call")]
    line = cb._open_loop_line(entries, NOW)
    assert "<!--loop:one-->" in line and "<!--loop:two-->" in line
    assert "<!--loop:three-->" in line and "<!--loop:four-->" in line
    assert "<!--loop:five-->" not in line


def test_exact_delivery_counts_all_active_states_but_not_closed():
    for status in ledger_io.ACTIVE | {"resolved", "dismissed", "parked"}:
        entry = {**row("one", "Send the signed agreement"), "status": status}
        got = cb._represented_loop_rows([entry], "Alex Chen: Send the signed agreement")
        assert got == ([entry] if status in ledger_io.ACTIVE else [])


def test_history_warning_distinguishes_recent_access_and_excluded_pages(volume, monkeypatch):
    monkeypatch.setattr(source_context, 'allowed', lambda source: source == 'imessage')
    inputs = {'local': {'source_status': {'imessage': 'ok'}}}
    state = {'sources': {'imessage': {'error': 'ModelResponseError'}}}
    text = health(volume, state, inputs)
    assert 'iMessage: recent messages are available.' in text
    assert 'Learning from older iMessage messages is paused' in text
    state['sources']['imessage'] = {'last_success': NOW.isoformat(), 'excluded_pages': {'opaque': {'rows': 1}}}
    text = health(volume, state, inputs)
    assert "Some older iMessage messages couldn't be learned" in text
    assert 'past context has gaps' in text and 'is paused' not in text
    assert 'recent messages are available' not in health(volume, {'sources': {'imessage': {'error': 'error'}}})


def test_partial_current_messages_do_not_hide_stalled_history(volume, monkeypatch):
    monkeypatch.setattr(source_context, "allowed", lambda source: source == "imessage")
    state = {"sources": {"imessage": {"last_success": "2026-09-10T12:00:00Z"}}}
    inputs = {"local": {"source_status": {"imessage": "partial"}}}
    text = health(volume, state, inputs)
    assert "Learning from older iMessage messages is paused" in text
    assert "recent messages are available" in text
