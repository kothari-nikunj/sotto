"""Tests for continuity_resolve, log_outcome, learn_preferences, style_extract, correlate_signals."""
import importlib.util
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "_shared", "lib"))


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "..", rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

cr = _load("morning-brief/scripts/continuity_resolve.py", "continuity_resolve")
lo = _load("_shared/scripts/log_outcome.py", "log_outcome")
lp = _load("approval-tiers/scripts/learn_preferences.py", "learn_preferences")
se = _load("_shared/scripts/style_extract.py", "style_extract")
cs = _load("_shared/scripts/correlate_signals.py", "correlate_signals")
sa = _load("_shared/scripts/style_apply.py", "style_apply")

NOW = datetime(2026, 6, 23, 18, 0, 0)


def test_continuity_anchor_key_thread_wins():
    assert cr.compute_anchor_key({"source_thread_id": "t1", "channel": "email", "action_type": "reply",
                                  "contact_name": "x"}) == "thread:t1"


def test_continuity_anchor_key_composite():
    # continuity.rs-faithful: channel:family:contact_anchor with cid:/id:/name: prefix.
    ak = cr.compute_anchor_key({"channel": "gmail", "action_type": "follow_up", "contact_name": "Sarah Chen"})
    assert ak == "email:follow_up:name:sarah chen"


def test_continuity_anchor_phone_vs_email_and_call_back_family():
    # Same person reached by +1 (415) 555-1234 vs 4155551234 must anchor-match (last-10 digits).
    a = cr.compute_anchor_key({"channel": "imessage", "action_type": "reply",
                               "contact_identifier": "+1 (415) 555-1234", "contact_name": "Jo"})
    b = cr.compute_anchor_key({"channel": "imessage", "action_type": "call_back",
                               "contact_identifier": "4155551234", "contact_name": "Jo"})
    assert a == b == "imessage:follow_up:id:4155551234"  # call_back in the follow_up family


def test_continuity_camelcase_actions_normalize():
    # The brief emits camelCase actionItems — they must not collapse to "::".
    ak = cr.compute_anchor_key(cr._normalize_action(
        {"type": "reply", "channel": "imessage", "contactName": "Sarah",
         "contactIdentifier": "+14155551234"}))
    assert ak == "imessage:follow_up:id:4155551234"
    assert ak != "::"


def test_continuity_passed_meeting():
    assert cr.meeting_passed("Tomorrow 3pm", "2026-06-21", "2026-06-23")
    assert not cr.meeting_passed("Tomorrow 3pm", "2026-06-23", "2026-06-23")
    # ISO branch: an absolute past date is passed regardless of created_at.
    assert cr.meeting_passed("2026-06-20 10:00", "2026-06-19", "2026-06-23")
    assert not cr.meeting_passed("2026-06-25 10:00", "2026-06-19", "2026-06-23")


def test_continuity_camelcase_thread_reply_resolves(tmp_path):
    # End-to-end: a camelCase action whose emailThreadId was replied to resolves.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    payload = {"today": "2026-06-23", "signals": {"replied_thread_ids": ["t-cc"]},
               "new_actions": [{"type": "reply", "channel": "gmail", "contactName": "Sarah",
                                "contactIdentifier": "sarah@acme.com", "emailThreadId": "t-cc",
                                "contextSummary": "reply to sarah"}]}
    out = cr.resolve(payload, NOW)
    assert any(r["resolution"] == "replied" for r in out["resolved"])


def test_continuity_age_expiry(tmp_path):
    # An open loop created 8+ days ago with no signal must expire (loops can't pile up forever).
    os.environ["SOTTO_DATA"] = str(tmp_path)
    cr.resolve({"today": "2026-06-10", "new_actions": [
        {"type": "reply", "channel": "imessage", "contactName": "Old", "contactIdentifier": "+14155550000",
         "contextSummary": "stale"}]}, datetime(2026, 6, 10, 9, 0, 0))
    out = cr.resolve({"today": "2026-06-23"}, NOW)
    assert any(e["resolution"] == "expired" for e in out["expired"])


def test_continuity_deadline_expiry(tmp_path):
    # Past deadline (2d grace), but created recently so it's the deadline — not age — that expires it.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    out = cr.resolve({"today": "2026-06-23", "new_actions": [
        {"type": "follow_up", "channel": "gmail", "contactName": "DL", "contactIdentifier": "dl@x.com",
         "deadlineDate": "2026-06-19", "contextSummary": "past deadline"}]}, NOW)
    assert any(e["resolution"] == "deadline_passed" for e in out["expired"])


def test_continuity_snoozed_loop_not_surfaced(tmp_path):
    # A user-snoozed loop (created today so age can't expire it) is kept on disk but NOT surfaced as
    # active until the snooze date passes. Source-of-truth for sotto-retune's snooze.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    out1 = cr.resolve({"today": "2026-06-23", "new_actions": [
        {"type": "reply", "channel": "gmail", "contactName": "Zoe", "contactIdentifier": "zoe@x.com",
         "contextSummary": "snooze me"}]}, NOW)
    it = out1["active"][0]
    # mirror retune_apply.snooze: defer + reset the aging clock so it isn't auto-expired while hidden
    it["snoozed_until"] = "2026-06-30"
    it["created_at"] = "2026-06-25"
    it["_path"] = os.path.join(str(tmp_path), "knowledge", "continuity",
                               cr._safe(it["anchor_key"]) + ".md")
    cr._persist(it)
    out2 = cr.resolve({"today": "2026-06-28"}, datetime(2026, 6, 28, 9, 0, 0))
    assert out2["active"] == [] and out2["expired"] == []   # hidden, not expired
    # after the snooze passes it surfaces again
    out3 = cr.resolve({"today": "2026-07-01"}, datetime(2026, 7, 1, 9, 0, 0))
    assert any(a["contact_name"] == "Zoe" for a in out3["active"])


def test_continuity_meeting_resolves_not_expires(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    cr.resolve({"today": "2026-06-21", "new_actions": [
        {"type": "meeting_prep", "channel": "calendar", "contactName": "Pitch",
         "contactIdentifier": "ev1", "meetingTime": "2026-06-22 10:00", "contextSummary": "prep"}]},
        datetime(2026, 6, 21, 9, 0, 0))
    out = cr.resolve({"today": "2026-06-23"}, NOW)
    assert any(r["resolution"] == "meeting_passed" and r["status"] == "resolved" for r in out["resolved"])


def test_continuity_resolve_from_handled(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    cr.resolve({"today": "2026-06-23", "new_actions": [
        {"type": "reply", "channel": "imessage", "contactName": "Han", "contactIdentifier": "+14155551111",
         "contextSummary": "owe a reply"}]}, NOW)
    out = cr.resolve({"today": "2026-06-23",
                      "signals": {"handled": [{"identifier": "4155551111", "channel": "imessage"}]}}, NOW)
    assert any(r["resolution"] == "brief_handled" for r in out["resolved"])


def test_continuity_cross_channel_reply_resolves(tmp_path):
    # Owe Dhruv a REPLY on email; you answer him on iMessage → loop closes (the moat).
    os.environ["SOTTO_DATA"] = str(tmp_path)
    cr.resolve({"today": "2026-06-23", "new_actions": [
        {"type": "reply", "channel": "gmail", "contactName": "Dhruv", "contactIdentifier": "dhruv@acme.com",
         "emailThreadId": "tA", "contextSummary": "owe the LOI reply", "created_at": "2026-06-23 08:00:00"}]}, NOW)
    out = cr.resolve({"today": "2026-06-24",
        "local": {"contacts": [{"name": "Dhruv", "emails": ["dhruv@acme.com"], "phones": ["+14155552222"]}],
                  "imessage": [{"is_from_me": True, "handle": "+14155552222",
                                "timestamp": "2026-06-23 20:00:00", "text": "sent the LOI"}]}},
        datetime(2026, 6, 24, 9, 0, 0))
    assert any(r["resolution"] == "replied" for r in out["resolved"])


def test_continuity_cross_channel_callback_resolves(tmp_path):
    # Owe Dad a call_back; an outgoing phone call closes it.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    cr.resolve({"today": "2026-06-23", "new_actions": [
        {"type": "call_back", "channel": "phone", "contactName": "Dad", "contactIdentifier": "+14155559999",
         "contextSummary": "call dad back", "created_at": "2026-06-23 08:00:00"}]}, NOW)
    out = cr.resolve({"today": "2026-06-24",
        "local": {"calls": [{"is_outgoing": True, "phone": "+14155559999", "timestamp": "2026-06-23 19:00:00"}]}},
        datetime(2026, 6, 24, 9, 0, 0))
    assert any(r["resolution"] == "called" for r in out["resolved"])


def test_continuity_cross_channel_no_false_positive(tmp_path):
    # An outgoing message to a DIFFERENT person must not resolve the loop.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    cr.resolve({"today": "2026-06-23", "new_actions": [
        {"type": "reply", "channel": "imessage", "contactName": "Dhruv", "contactIdentifier": "+14155552222",
         "contextSummary": "owe a reply", "created_at": "2026-06-23 08:00:00"}]}, NOW)
    out = cr.resolve({"today": "2026-06-24",
        "local": {"imessage": [{"is_from_me": True, "handle": "+19998887777",
                                "timestamp": "2026-06-23 20:00:00", "text": "to someone else"}]}},
        datetime(2026, 6, 24, 9, 0, 0))
    assert any(i["anchor_key"] for i in out["active"])   # still open
    assert not out["resolved"]


def test_continuity_replied_resolves(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    payload = {"today": "2026-06-23",
               "signals": {"replied_thread_ids": ["t1"]},
               "new_actions": [{"action_type": "reply", "channel": "email", "contact_name": "Sarah",
                                "source_thread_id": "t1", "summary": "reply to sarah"}]}
    out = cr.resolve(payload, NOW)
    assert any(r["resolution"] == "replied" for r in out["resolved"])


def test_continuity_dedup_bumps_times_surfaced(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    a = {"action_type": "reply", "channel": "email", "contact_name": "Sarah", "source_thread_id": "t9", "summary": "x"}
    cr.resolve({"today": "2026-06-23", "new_actions": [a]}, NOW)
    out = cr.resolve({"today": "2026-06-23", "new_actions": [a]}, NOW)
    item = next(i for i in out["active"] if i["anchor_key"] == "thread:t9")
    assert item["times_surfaced"] == 2


def test_log_outcome_and_learn(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    for _ in range(3):
        lo.log({"contact": "spammy", "action_type": "reply", "outcome": "dismissed"})
    lo.log({"contact": "sarah", "action_type": "reply", "outcome": "edited_and_sent"})
    prefs = lp.learn()
    assert "spammy|reply" in prefs["deprioritization_hints"]
    assert prefs["analytics"]["total_outcomes"] == 4


def test_log_outcome_rejects_invalid(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    try:
        lo.log({"outcome": "nonsense"})
        assert False
    except ValueError:
        pass


_SARAH = [
    {"text": "Hey Sarah, sounds great — can you send the deck by Friday?", "channel": "imessage",
     "recipient": "sarah@acme.com", "canonical_id": "c_sarah", "work": True},
    {"text": "Thanks for the update, will review tonight and circle back tomorrow.", "channel": "imessage",
     "recipient": "sarah@acme.com", "canonical_id": "c_sarah", "work": True},
]


def test_style_extract_v2_seeds_canonical_buckets(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    se.extract({"sent_messages": _SARAH + [
        {"text": "Hi team, here is the Q3 update. Numbers look strong this quarter. Best", "channel": "email", "work": True}]})
    style = json.load(open(os.path.join(str(tmp_path), "style.json")))
    assert style["schema_version"] == 2
    assert len(style["canonical"]["work_message"]) == 2     # 2 messages to Sarah
    assert len(style["canonical"]["work_email"]) == 1       # email → work_email (never personal)
    assert "work_email" in style["master_by_context"]
    assert "c_sarah" in style["per_person"]                  # >=2 samples → per-person profile


def test_style_apply_injects_verbatim_samples(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    se.extract({"sent_messages": _SARAH})
    out = sa.apply({"recipient": "sarah@acme.com", "channel": "imessage", "canonical_id": "c_sarah"})
    assert out["source"] == "per_person"
    assert "send the deck by Friday" in out["guidance"]      # a real message is quoted, not a descriptor


def test_style_apply_email_uses_work_email_bucket(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    se.extract({"sent_messages": [
        {"text": "Hi team, here is the Q3 update. Numbers look strong this quarter. Best", "channel": "email", "work": True}]})
    out = sa.apply({"recipient": "unknown@x.com", "channel": "email"})
    assert out["bucket"] == "work_email"


def test_style_back_channel_and_capitalization():
    assert not se.is_usable_sample("ok", 5, 500)             # back-channel filtered
    assert not se.is_usable_sample("thanks!", 5, 500)
    assert se.is_usable_sample("Can you send the deck by Friday?", 5, 500)
    m = se.analyze_master_style(["hey can you send that over", "yeah sounds good to me", "lmk when you're free"])
    assert m["capitalization"] == "lowercase"               # strong "sounds like me" signal


def test_correlate_granola_calendar():
    out = cs.correlate({
        "granola": [{"id": "g1", "title": "Acme platform sync", "attendees": ["sarah@acme.com"]}],
        "calendar": [{"id": "e1", "summary": "Acme platform sync", "attendees": ["sarah@acme.com"]}],
    })
    assert any(l["type"] == "granola->calendar" for l in out["links"])
    assert out["event_scores"]["e1"] >= 1


def test_correlate_excludes_hosting_domains_and_finds_file_links():
    # Converged with compose_brief: shared exclusion (google.com/gmail.com don't false-match) and the
    # previously-missing file->email / file->granola matchings now fire.
    out = cs.correlate({
        "chrome": [{"url": "https://acme.com/pricing"}, {"url": "https://google.com"}],
        "emails": [{"id": "e1", "from": "Jane <jane@acme.com>"}],
        "granola": [{"id": "g1", "title": "Acme pricing review", "attendees": ["jane@acme.com"]}],
        "files": [{"name": "Acme pricing deck.pptx", "source_url": "https://acme.com/d"}],
    })
    types = {l["type"] for l in out["links"]}
    assert {"domain->email", "file->email", "file->granola", "granola->email"} <= types
    assert not any(l.get("domain") == "google.com" for l in out["links"])  # hosting domain excluded
