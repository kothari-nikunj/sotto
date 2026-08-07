"""triage_event.py — the Tier 0 verdict matrix, the stubbed Tier 1, and the agent cooldown."""
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location(
    "te", os.path.join(ROOT, "event-triage", "scripts", "triage_event.py"))
te = importlib.util.module_from_spec(spec)
spec.loader.exec_module(te)

DAY = datetime(2026, 8, 6, 11, 0)      # 11:00 local — outside the default 21..7 quiet window
NIGHT = datetime(2026, 8, 6, 23, 0)    # 23:00 local — inside quiet hours
NOW_UTC = datetime(2026, 8, 6, 10, 5, tzinfo=timezone.utc)  # 5 min after fixture timestamps


def _seed_snapshot(tmp_path, contacts=None, person_knowledge=None):
    """Write the cached local snapshot the funnel resolves senders against (the exact file
    compose_brief._save_local_snapshot writes)."""
    kn = tmp_path / "knowledge"
    kn.mkdir(parents=True, exist_ok=True)
    local = {"contacts": contacts if contacts is not None else
             [{"name": "Sarah Chen", "phones": ["+14155551234"], "emails": ["sarah@acme.com"]}]}
    if person_knowledge:
        local["person_knowledge"] = person_knowledge
    (kn / "last_local_snapshot.json").write_text(
        json.dumps({"captured_at": "2026-08-06 10:00:00", "local": local}))


def _seed_rel_state(tmp_path, queue):
    kn = tmp_path / "knowledge"
    kn.mkdir(parents=True, exist_ok=True)
    (kn / "relationship_state.json").write_text(json.dumps({"attention_queue": queue}))


def _im(text="hey, can you review the deck today?", handle="+14155551234", **kw):
    e = {"source": "imessage", "rowid": kw.pop("rowid", 1), "handle": handle, "is_from_me": False,
         "timestamp": "2026-08-06T10:00:00Z", "text": text, "is_group_chat": False,
         "chat_guid": None, "group_name": None, "group_participants": []}
    e.update(kw)
    return e


def _call(phone="+14155551234", **kw):
    e = {"source": "calls", "rowid": kw.pop("rowid", 7), "phone": phone, "is_outgoing": False,
         "is_answered": False, "call_type": "phone", "timestamp": "2026-08-06T10:00:00Z"}
    e.update(kw)
    return e


def _queue_entries(tmp_path):
    p = tmp_path / "events" / "queue.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _surfaced_entries(tmp_path):
    p = tmp_path / "events" / "surfaced.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _no_llm(monkeypatch):
    """Tier 1 must NOT be reached — any call is a test failure."""
    def boom(*a, **k):
        raise AssertionError("Tier 1 was called for a Tier-0-decided event")
    monkeypatch.setattr(te._gemini, "_gemini_once", boom)


def _stub_llm(monkeypatch, reply):
    calls = []

    def stub(model, key, prompt, label=""):
        calls.append({"model": model, "prompt": prompt})
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(te._gemini, "_gemini_once", stub)
    return calls


# ── Tier 0 verdict matrix ─────────────────────────────────────────────────────────────────────────

def test_is_from_me_queues_as_silent_signal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    out = te.triage({"events": [_im("on it, sending now", is_from_me=True)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue" and out["bundle"] == {}
    entries = _queue_entries(tmp_path)
    assert len(entries) == 1 and entries[0]["verdict_class"] == "signal"
    assert entries[0]["event"]["is_from_me"] is True


def test_automated_email_sender_drops_silently(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    ev = {"source": "email", "rowid": "abc", "from": "Stripe <no-reply@stripe.com>",
          "subject": "Your receipt", "body": "Receipt #123", "threadId": "t1", "date": "2026-08-06"}
    out = te.triage({"events": [ev]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "drop"
    assert _queue_entries(tmp_path) == []                     # dropped = nothing anywhere


def test_otp_and_shortcode_and_system_messages_drop(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    otp = _im("Your verification code is 482910", handle="692639", rowid=2)
    system = _im("Alice added Bob to the group", handle="+14155551234", rowid=3,
                 is_group_chat=True, chat_guid="g1")
    out = te.triage({"events": [otp, system]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "drop"
    assert _queue_entries(tmp_path) == []


def test_muted_sender_and_muted_person_drop(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    (tmp_path / "preferences.json").write_text(json.dumps(
        {"explicit": {"mute_senders": ["@newsletter.com"], "mute_people": ["Sarah Chen"]}}))
    _no_llm(monkeypatch)
    email = {"source": "email", "rowid": "m1", "from": "News <digest@newsletter.com>",
             "subject": "Weekly digest", "body": "lots of words", "threadId": "t2", "date": ""}
    out = te.triage({"events": [email, _im(rowid=4)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "drop"                           # both muted → nothing survives
    assert _queue_entries(tmp_path) == []


def test_missed_call_from_known_person_goes_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    out = te.triage({"events": [_call()]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "agent"
    assert out["bundle"]["events"][0]["sender"] == "Sarah Chen"
    assert out["bundle"]["events"][0]["class"] == "missed_call"
    # the agent verdict stamped the per-thread cooldown
    cd = json.loads((tmp_path / "events" / "cooldowns.json").read_text())
    assert any(k.startswith("calls:") for k in cd)


def test_missed_call_from_unknown_number_queues(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    out = te.triage({"events": [_call(phone="+19998887777")]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue"
    assert _queue_entries(tmp_path)[0]["verdict_class"] == "missed_call"


def test_answered_or_outgoing_call_drops(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    out = te.triage({"events": [_call(is_answered=True), _call(is_outgoing=True, rowid=8)]},
                    now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "drop"


def test_unknown_non_vip_one_to_one_queues_never_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)                                       # unknown 1:1 must not reach Tier 1
    out = te.triage({"events": [_im("URGENT wire $10k now!!", handle="+19998887777")]},
                    now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue"
    assert _queue_entries(tmp_path)[0]["verdict_class"] == "unknown"


def test_group_without_name_mention_queues_with_mention_reaches_tier1(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_NAME", "Nikunj Kothari")
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    group = dict(is_group_chat=True, chat_guid="g42", group_name="Ski trip",
                 group_participants=["+14155551234", "+15550001111"])
    # no mention → queue, Tier 1 untouched
    _no_llm(monkeypatch)
    out = te.triage({"events": [_im("who's driving saturday?", **group)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue"
    assert _queue_entries(tmp_path)[-1]["verdict_class"] == "group"
    # name-mention → survives to Tier 1 (stub says urgent → agent)
    calls = _stub_llm(monkeypatch, '{"class":"urgent","why":"direct ask"}')
    out2 = te.triage({"events": [_im("Nikunj can you book the cabin?", rowid=9, **group)]},
                     now_local=DAY, now_utc=NOW_UTC)
    assert out2["verdict"] == "agent" and len(calls) == 1
    assert out2["bundle"]["events"][0]["class"] == "urgent"


def test_quiet_hours_queue_everything_except_vip_missed_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    # known 1:1 message at 23:00 → queue (class "quiet"), never Tier 1
    out = te.triage({"events": [_im()]}, now_local=NIGHT, now_utc=NOW_UTC)
    assert out["verdict"] == "queue"
    assert _queue_entries(tmp_path)[-1]["verdict_class"] == "quiet"
    # known but non-VIP missed call in quiet hours → queue
    out2 = te.triage({"events": [_call(rowid=10)]}, now_local=NIGHT, now_utc=NOW_UTC)
    assert out2["verdict"] == "queue"
    # VIP (top-of-queue attention_queue priority) missed call → agent even at 23:00
    _seed_rel_state(tmp_path, [{"display_name": "Sarah Chen", "queue_type": "waiting_on_you",
                                "reason": "asked about the deck", "priority": 14.0}])
    out3 = te.triage({"events": [_call(rowid=11)]}, now_local=NIGHT, now_utc=NOW_UTC)
    assert out3["verdict"] == "agent"
    assert out3["bundle"]["events"][0]["sender"] == "Sarah Chen"


def test_low_priority_attention_queue_is_not_vip(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _seed_rel_state(tmp_path, [{"display_name": "Sarah Chen", "queue_type": "losing_touch",
                                "reason": "quiet lately", "priority": 2.0}])
    _no_llm(monkeypatch)
    out = te.triage({"events": [_call(rowid=12)]}, now_local=NIGHT, now_utc=NOW_UTC)
    assert out["verdict"] == "queue"                          # priority below the VIP bar


# ── Tier 1 (stubbed) ──────────────────────────────────────────────────────────────────────────────

def test_tier1_urgent_and_actionable_go_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path, contacts=[
        {"name": "Sarah Chen", "phones": ["+14155551234"], "emails": []},
        {"name": "Dhruv Patel", "phones": ["+14155559999"], "emails": []}])
    _stub_llm(monkeypatch, '{"class":"urgent","why":"deadline"}')
    out = te.triage({"events": [_im(rowid=30)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "agent"
    assert out["bundle"]["events"][0]["class"] == "urgent"
    assert out["bundle"]["events"][0]["why"] == "deadline"
    # actionable also clears the bar (different sender = different cooldown thread)
    _stub_llm(monkeypatch, '{"class":"actionable","why":"an ask"}')
    out2 = te.triage({"events": [_im(rowid=31, handle="+14155559999",
                                     text="mind sending the notes?")]}, now_local=DAY, now_utc=NOW_UTC)
    assert out2["verdict"] == "agent"
    assert out2["bundle"]["events"][0]["class"] == "actionable"


def test_tier1_ambient_queues_and_ignore_drops(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    _stub_llm(monkeypatch, '{"class":"ambient","why":"fyi"}')
    out = te.triage({"events": [_im("fyi the game moved to 7", rowid=40)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue"
    assert _queue_entries(tmp_path)[-1]["verdict_class"] == "ambient"
    _stub_llm(monkeypatch, '{"class":"ignore","why":"noise"}')
    out2 = te.triage({"events": [_im("ok", rowid=41)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out2["verdict"] == "drop"


def test_tier1_prompt_carries_event_text_and_sender(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.setenv("SOTTO_TRIAGE_MODEL", "gemini-3.5-flash-lite")
    _seed_snapshot(tmp_path, person_knowledge={
        "sarah-chen": "Sarah Chen (c_ab) | Founder @ Acme | sarah@acme.com\n= facts here"})
    calls = _stub_llm(monkeypatch, '{"class":"ambient","why":"x"}')
    te.triage({"events": [_im("can you review the deck today?", rowid=42)]}, now_local=DAY, now_utc=NOW_UTC)
    assert len(calls) == 1
    assert calls[0]["model"] == "gemini-3.5-flash-lite"
    assert "can you review the deck today?" in calls[0]["prompt"]
    assert "Sarah Chen" in calls[0]["prompt"]                 # sender one-liner present
    assert "Founder @ Acme" in calls[0]["prompt"]             # graph head line woven in
    assert len(calls[0]["prompt"]) < 8000                     # ≈ ≤ 2k tokens


def test_tier1_any_error_fails_toward_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    # (a) exception from the model call
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _stub_llm(monkeypatch, RuntimeError("503 backend"))
    out = te.triage({"events": [_im(rowid=50)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue" and out["bundle"] == {}
    assert _queue_entries(tmp_path)[-1]["verdict_class"] == "ambient"
    # (b) garbage / non-JSON reply
    _stub_llm(monkeypatch, "sure! here's my analysis, no json though")
    out2 = te.triage({"events": [_im(rowid=51)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out2["verdict"] == "queue"
    # (c) unexpected class value
    _stub_llm(monkeypatch, '{"class":"panic","why":"??"}')
    out3 = te.triage({"events": [_im(rowid=52)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out3["verdict"] == "queue"
    # (d) missing API key never even calls the stub
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)
    calls = _stub_llm(monkeypatch, '{"class":"urgent","why":"x"}')
    out4 = te.triage({"events": [_im(rowid=53)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out4["verdict"] == "queue" and calls == []


# ── Cooldown ──────────────────────────────────────────────────────────────────────────────────────

def test_cooldown_suppresses_second_agent_on_same_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    out1 = te.triage({"events": [_call(rowid=60)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out1["verdict"] == "agent"
    out2 = te.triage({"events": [_call(rowid=61)]}, now_local=DAY, now_utc=NOW_UTC)   # same phone = same thread key
    assert out2["verdict"] == "queue"
    assert _queue_entries(tmp_path)[-1]["verdict_class"] == "cooldown"
    # an expired stamp (older than SOTTO_EVENT_COOLDOWN_MIN) lets the next one through again
    cd_path = tmp_path / "events" / "cooldowns.json"
    cd = json.loads(cd_path.read_text())
    cd_path.write_text(json.dumps({k: v - 21 * 60 for k, v in cd.items()}))
    out3 = te.triage({"events": [_call(rowid=62)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out3["verdict"] == "agent"


def test_cooldown_is_per_thread_not_global(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path, contacts=[
        {"name": "Sarah Chen", "phones": ["+14155551234"], "emails": []},
        {"name": "Dhruv Patel", "phones": ["+14155559999"], "emails": []}])
    _no_llm(monkeypatch)
    assert te.triage({"events": [_call(rowid=70)]}, now_local=DAY, now_utc=NOW_UTC)["verdict"] == "agent"
    out = te.triage({"events": [_call(phone="+14155559999", rowid=71)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "agent"                          # a different person is not throttled


# ── Batch aggregation + robustness ────────────────────────────────────────────────────────────────

def test_batch_verdict_is_highest_severity(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    otp = _im("Your verification code is 111222", handle="55555", rowid=80)
    unknown = _im("hi", handle="+19998887777", rowid=81)
    out = te.triage({"events": [otp, unknown]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue"                          # queue beats drop
    out2 = te.triage({"events": [otp, unknown, _call(rowid=82)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out2["verdict"] == "agent"                         # agent beats queue
    assert len(out2["bundle"]["events"]) == 1                 # only the agent-worthy event bundled


def test_empty_and_malformed_input_are_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _no_llm(monkeypatch)
    out = te.triage({"events": []}, now_local=DAY, now_utc=NOW_UTC)
    assert out == {"verdict": "drop", "reason": "no events", "bundle": {}}
    out2 = te.triage({"events": [None, "junk", 42]}, now_local=DAY, now_utc=NOW_UTC)
    assert out2["verdict"] == "drop"


def test_queue_entries_carry_ts_class_sender_event(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    te.triage({"events": [_im(is_from_me=True, rowid=90)]}, now_local=DAY, now_utc=NOW_UTC)
    entry = _queue_entries(tmp_path)[0]
    assert set(entry) == {"ts", "verdict_class", "sender", "event"}   # held_class only on demotions
    assert entry["sender"] == "Sarah Chen"
    assert entry["event"]["rowid"] == 90


# ── Reconnect grace (catchup batches + stale events never barrage) ────────────────────────────────

def test_catchup_batch_demotes_messages_to_queue_but_not_missed_calls(tmp_path, monkeypatch):
    # Bridge was off for hours → backlog arrives flagged catchup. Even a Tier-1 "urgent" message
    # must NOT nudge (it was probably handled on the phone already) — it queues for digest/brief.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    _stub_llm(monkeypatch, '{"class":"urgent","why":"deadline"}')
    out = te.triage({"events": [_im(rowid=80)], "catchup": True},
                    now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue"
    assert _queue_entries(tmp_path)[-1]["verdict_class"] == "stale"
    # A missed call from a known person is still worth surfacing hours later → agent even in catchup.
    out2 = te.triage({"events": [_call(rowid=81)], "catchup": True},
                     now_local=DAY, now_utc=NOW_UTC)
    assert out2["verdict"] == "agent"


def test_stale_event_queues_even_without_catchup_flag(tmp_path, monkeypatch):
    # A small backlog (≤10 events) carries no catchup flag — the age gate still catches it.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    _stub_llm(monkeypatch, '{"class":"urgent","why":"deadline"}')
    old = _im(rowid=82, timestamp="2026-08-06T07:00:00Z")   # 3h05m before NOW_UTC
    out = te.triage({"events": [old]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue"
    assert _queue_entries(tmp_path)[-1]["verdict_class"] == "stale"
    # Fresh event (5 min old) from the same known sender still goes agent — real-time stays
    # real-time, and the demoted stale event must NOT have stamped the thread's cooldown.
    out2 = te.triage({"events": [_im(rowid=83)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out2["verdict"] == "agent"


def test_unparseable_timestamp_never_gates(tmp_path, monkeypatch):
    # Age gate must fail open (no demotion) on a missing/garbled timestamp — never gate on a guess.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    _stub_llm(monkeypatch, '{"class":"urgent","why":"deadline"}')
    out = te.triage({"events": [_im(rowid=84, timestamp="not-a-date")]},
                    now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "agent"


# ── Surfaced ledger (one line per verdict — the "why didn't I get nudged?" substrate) ─────────────

def test_surfaced_ledger_records_every_verdict(tmp_path, monkeypatch):
    """agent/queue/drop all land in surfaced.jsonl with the Record-renderable shape:
    {ts, sender, channel, verdict, reason, class} (ts ISO-Z, per the dashboard's parser)."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    otp = _im("Your verification code is 482910", handle="692639", rowid=200)   # drop
    unknown = _im("hi", handle="+19998887777", rowid=201)                       # queue
    te.triage({"events": [otp, unknown, _call(rowid=202)]}, now_local=DAY, now_utc=NOW_UTC)
    rows = _surfaced_entries(tmp_path)
    assert len(rows) == 3
    for r in rows:
        assert set(r) == {"ts", "sender", "channel", "verdict", "reason", "class"}
        assert r["ts"].endswith("Z")
    by_verdict = {r["verdict"]: r for r in rows}
    assert by_verdict["drop"]["class"] == "automated"
    assert by_verdict["queue"]["class"] == "unknown"
    assert by_verdict["queue"]["channel"] == "imessage"
    assert by_verdict["agent"]["sender"] == "Sarah Chen"
    assert by_verdict["agent"]["class"] == "missed_call"
    assert by_verdict["agent"]["channel"] == "calls"


def test_surfaced_ledger_records_demotions_with_demoted_class(tmp_path, monkeypatch):
    """A cooldown-demoted event surfaces as verdict queue / class cooldown — the exact trail that
    makes 'why didn't I get nudged?' answerable."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    te.triage({"events": [_call(rowid=210)]}, now_local=DAY, now_utc=NOW_UTC)     # agent + stamp
    te.triage({"events": [_call(rowid=211)]}, now_local=DAY, now_utc=NOW_UTC)     # cooldown demote
    rows = _surfaced_entries(tmp_path)
    assert [r["verdict"] for r in rows] == ["agent", "queue"]
    assert rows[1]["class"] == "cooldown"
    assert "cooldown" in rows[1]["reason"]


# ── scheduling_ask (Tier 1 vocabulary + demotion carry-through) ───────────────────────────────────

def test_scheduling_ask_from_known_sender_goes_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    calls = _stub_llm(monkeypatch, '{"class":"scheduling_ask","why":"wants coffee Thursday"}')
    out = te.triage({"events": [_im("can we do coffee thursday?", rowid=220)]},
                    now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "agent"
    assert out["bundle"]["events"][0]["class"] == "scheduling_ask"
    assert out["bundle"]["events"][0]["why"] == "wants coffee Thursday"
    assert "scheduling_ask" in calls[0]["prompt"]              # the vocabulary reached the model
    assert _surfaced_entries(tmp_path)[-1]["class"] == "scheduling_ask"


def test_demoted_scheduling_ask_keeps_held_class_in_queue(tmp_path, monkeypatch):
    """Catchup demotion preserves the pre-demotion class as held_class, so the valve (and digest)
    still know this was a scheduling ask — and the digest gate counts the row (class 'stale')."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    _stub_llm(monkeypatch, '{"class":"scheduling_ask","why":"30 min next week"}')
    out = te.triage({"events": [_im("got 30 min next week?", rowid=221)], "catchup": True},
                    now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue"
    entry = _queue_entries(tmp_path)[-1]
    assert entry["verdict_class"] == "stale"                   # counted by digest_check
    assert entry["held_class"] == "scheduling_ask"             # preserved for the valve/agent


# ── Release valve (the deferred queue's way back to a nudge) ──────────────────────────────────────

VALVE_NOW_UTC = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)   # fixtures at 10:00Z = 120m old
VALVE_NOW_TS = 1_000_000.0


def _q(cls, sender="Sarah Chen", rowid=1, handle="+14155551234", held=None,
       ev_ts="2026-08-06T10:00:00Z", q_ts="2026-08-06T10:00:05Z"):
    """One raw queue.jsonl entry (the exact shape triage writes)."""
    e = _im(rowid=rowid, handle=handle, timestamp=ev_ts)
    entry = {"ts": q_ts, "verdict_class": cls, "sender": sender, "event": e}
    if held:
        entry["held_class"] = held
    return entry


def _seed_queue(tmp_path, entries):
    d = tmp_path / "events"
    d.mkdir(parents=True, exist_ok=True)
    (d / "queue.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_valve_promotes_known_sender_catchup_demoted_event(tmp_path, monkeypatch):
    """End-to-end through the real demotion: a catchup-demoted urgent event promotes once the hold
    lifts — same bundle shape as a fresh agent verdict, queue entry removed, surfaced 'promoted',
    cooldown stamped."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    _stub_llm(monkeypatch, '{"class":"urgent","why":"deadline"}')
    te.triage({"events": [_im(rowid=300)], "catchup": True}, now_local=DAY, now_utc=NOW_UTC)
    assert _queue_entries(tmp_path)[-1]["verdict_class"] == "stale"
    out = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out["verdict"] == "agent"
    assert out["bundle"]["promoted"] is True
    ev = out["bundle"]["events"][0]
    assert ev["sender"] == "Sarah Chen"
    assert ev["class"] == "urgent"                             # held_class carried through
    assert "promoted from the deferred queue" in ev["why"]
    assert ev["event"]["rowid"] == 300
    assert _queue_entries(tmp_path) == []                      # promoted entries leave the queue
    assert _surfaced_entries(tmp_path)[-1]["verdict"] == "promoted"
    cd = json.loads((tmp_path / "events" / "cooldowns.json").read_text())
    assert any(k.startswith("imessage:") for k in cd)          # cooldown stamped at promotion


def test_valve_cap_per_tick_and_hourly_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_VALVE_MAX_PER_HOUR", "5")        # budget above the per-tick cap
    _seed_queue(tmp_path, [
        _q("cooldown", sender="Sarah Chen", rowid=1, handle="+14155551234"),
        _q("quiet", sender="Dhruv Patel", rowid=2, handle="+14155559999"),
        _q("stale", sender="Alice Wong", rowid=3, handle="+14155550000"),
    ])
    out = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out["verdict"] == "agent"
    assert len(out["bundle"]["events"]) == 2                   # ≤ 2 per tick, oldest first
    assert [e["sender"] for e in out["bundle"]["events"]] == ["Sarah Chen", "Dhruv Patel"]
    assert len(_queue_entries(tmp_path)) == 1                  # the third stays queued
    # Hourly budget: default 2/h — after 2 promotions the next tick promotes nothing.
    monkeypatch.setenv("SOTTO_VALVE_MAX_PER_HOUR", "2")
    out2 = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS + 900)
    assert out2["verdict"] == "drop" and "budget" in out2["reason"]
    assert len(_queue_entries(tmp_path)) == 1                  # untouched
    # An hour later the budget refills.
    out3 = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS + 3700)
    assert out3["verdict"] == "agent"
    assert [e["sender"] for e in out3["bundle"]["events"]] == ["Alice Wong"]


def test_valve_skips_events_older_than_promotion_window(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_queue(tmp_path, [
        _q("cooldown", rowid=1, ev_ts="2026-08-06T05:00:00Z",  # 7h old at 12:00 — beyond 4h
           q_ts="2026-08-06T05:00:05Z"),
    ])
    out = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out["verdict"] == "drop" and "nothing promotable" in out["reason"]
    assert len(_queue_entries(tmp_path)) == 1                  # old entries stay for the brief
    # A widened window (env knob) lets the same entry through.
    monkeypatch.setenv("SOTTO_VALVE_MAX_AGE_MIN", "480")
    out2 = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out2["verdict"] == "agent"


def test_meeting_held_entries_are_exempt_from_the_promotion_window(tmp_path, monkeypatch):
    """An ask Sotto itself held is never too old to deliver: a 3h meeting outlasts the 4h window
    for an ask that arrived before it started, and expiring it silently is the failure the valve
    exists to fix. Every OTHER held class still expires on the same clock."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_queue(tmp_path, [
        _q("cooldown", rowid=1, ev_ts="2026-08-06T05:00:00Z",      # 7h old — past the 4h window
           q_ts="2026-08-06T05:00:05Z"),
        _q("meeting_hold", sender="Dhruv Patel", rowid=2, handle="+14155559999",
           held="urgent", ev_ts="2026-08-06T05:00:00Z", q_ts="2026-08-06T05:00:05Z"),
    ])
    out = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out["verdict"] == "agent"
    ev = out["bundle"]["events"]
    assert [e["sender"] for e in ev] == ["Dhruv Patel"]             # the cooldown entry stayed
    assert ev[0]["class"] == "urgent" and "420m old" in ev[0]["why"]
    assert [e["verdict_class"] for e in _queue_entries(tmp_path)] == ["cooldown"]


def test_valve_disabled_by_knob(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_VALVE", "0")
    _seed_queue(tmp_path, [_q("cooldown", rowid=1)])
    out = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out["verdict"] == "drop" and "disabled" in out["reason"]
    assert len(_queue_entries(tmp_path)) == 1
    assert _surfaced_entries(tmp_path) == []


def test_valve_never_promotes_drop_class_or_unknown_senders(tmp_path, monkeypatch):
    """Only demoted-agent classes from KNOWN senders promote — born-ambient, group chatter,
    unknown 1:1, the user's own signals, and phone-shaped 'names' never do."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_queue(tmp_path, [
        _q("ambient", rowid=1),                                # born-ambient: not a demotion
        _q("group", rowid=2),
        _q("unknown", sender="", rowid=3),
        _q("signal", rowid=4),
        _q("missed_call", sender="", rowid=5),
        _q("cooldown", sender="+19998887777", rowid=6),        # demoted but sender unresolved
    ])
    out = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out["verdict"] == "drop" and "nothing promotable" in out["reason"]
    assert len(_queue_entries(tmp_path)) == 6


def test_valve_holds_during_quiet_hours(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_queue(tmp_path, [_q("quiet", rowid=1)])
    out = te.release_valve(now_local=NIGHT, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out["verdict"] == "drop" and "quiet hours" in out["reason"]
    assert len(_queue_entries(tmp_path)) == 1                  # still there for when quiet lifts


def test_valve_respects_thread_cooldown_at_promotion_time(tmp_path, monkeypatch):
    """A live cooldown on the thread blocks promotion NOW (the entry stays queued for a later
    tick) — and two queued entries from one thread can't both promote in a tick (the first
    promotion stamps the cooldown the second then fails)."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_queue(tmp_path, [
        _q("cooldown", rowid=1),
        _q("cooldown", rowid=2),                               # same handle = same thread
        _q("quiet", sender="Dhruv Patel", rowid=3, handle="+14155559999"),
    ])
    te._stamp_cooldown("imessage:+14155551234", VALVE_NOW_TS - 60)   # active (default 20 min)
    out = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out["verdict"] == "agent"
    assert [e["sender"] for e in out["bundle"]["events"]] == ["Dhruv Patel"]
    assert len(_queue_entries(tmp_path)) == 2                  # Sarah's entries wait their turn
    # Once the cooldown expires, the same-thread pair promotes exactly ONE (self-stamping).
    later = VALVE_NOW_TS + 21 * 60
    out2 = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=later)
    assert out2["verdict"] == "agent"
    assert [e["event"]["rowid"] for e in out2["bundle"]["events"]] == [1]
    assert len(_queue_entries(tmp_path)) == 1                  # rowid 2 still queued


# ── Cross-thread daily interrupt budget (Editor Step 2 §1) ────────────────────────────────────────

NEXT_DAY = datetime(2026, 8, 7, 11, 0)      # same wall-clock hour, one local day later

THREE_CONTACTS = [
    {"name": "Sarah Chen", "phones": ["+14155551234"], "emails": ["sarah@acme.com"]},
    {"name": "Dhruv Patel", "phones": ["+14155559999"]},
    {"name": "Alice Wong", "phones": ["+14155550000"]},
]
HANDLES = ["+14155551234", "+14155559999", "+14155550000"]


def _budget_file(tmp_path):
    p = tmp_path / "events" / "budget.json"
    return json.loads(p.read_text()) if p.exists() else None


def _urgent_from_each(monkeypatch, tmp_path, n=3, base_rowid=400, now_local=DAY):
    """Triage one urgent message from n DIFFERENT known senders (n different threads, so the
    per-thread cooldown can never be the thing doing the suppressing)."""
    _stub_llm(monkeypatch, '{"class":"urgent","why":"needs you now"}')
    outs = []
    for i in range(n):
        outs.append(te.triage({"events": [_im(rowid=base_rowid + i, handle=HANDLES[i % 3])]},
                              now_local=now_local, now_utc=NOW_UTC))
    return outs


def test_daily_budget_demotes_agent_verdicts_beyond_the_cap(tmp_path, monkeypatch):
    """The volume control per-thread cooldowns can't provide: three DIFFERENT senders, cap 2 —
    the third is held with class 'budget' and its pre-demotion class preserved."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "2")
    _seed_snapshot(tmp_path, contacts=THREE_CONTACTS)
    outs = _urgent_from_each(monkeypatch, tmp_path)
    assert [o["verdict"] for o in outs] == ["agent", "agent", "queue"]
    assert _budget_file(tmp_path) == {"date": "2026-08-06", "count": 2}
    held = _queue_entries(tmp_path)[-1]
    assert held["verdict_class"] == "budget"
    assert held["held_class"] == "urgent"                     # digest/valve still see the real class
    assert held["sender"] == "Alice Wong"
    row = _surfaced_entries(tmp_path)[-1]
    assert row["verdict"] == "queue" and row["class"] == "budget"
    assert "budget" in row["reason"] and "Alice Wong" in row["reason"]


def test_budget_zero_holds_everything_but_the_exempt_classes(tmp_path, monkeypatch):
    """Missed calls are exempt BY CLASS (the same list item-5's 'escalation' will join): they
    nudge with the budget at zero, and they never spend it."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "0")
    _seed_snapshot(tmp_path, contacts=THREE_CONTACTS)
    _stub_llm(monkeypatch, '{"class":"urgent","why":"needs you now"}')
    msg = te.triage({"events": [_im(rowid=420)]}, now_local=DAY, now_utc=NOW_UTC)
    assert msg["verdict"] == "queue" and _queue_entries(tmp_path)[-1]["verdict_class"] == "budget"
    call = te.triage({"events": [_call(rowid=421, phone="+14155559999")]},
                     now_local=DAY, now_utc=NOW_UTC)
    assert call["verdict"] == "agent"                          # Tier-0 missed call, budget or not
    assert call["bundle"]["events"][0]["class"] == "missed_call"
    assert _budget_file(tmp_path) is None                      # …and it spent nothing
    assert "missed_call" in te.BUDGET_EXEMPT_CLASSES and "escalation" in te.BUDGET_EXEMPT_CLASSES


def test_budget_resets_on_the_local_day_rollover(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "1")
    _seed_snapshot(tmp_path, contacts=THREE_CONTACTS)
    outs = _urgent_from_each(monkeypatch, tmp_path, n=2)
    assert [o["verdict"] for o in outs] == ["agent", "queue"]
    assert _budget_file(tmp_path)["date"] == "2026-08-06"
    # Same clock hour, next LOCAL day → the counter is keyed by date, so it reads as spent-0.
    tomorrow = te.triage({"events": [_im(rowid=430, handle=HANDLES[2],
                                         timestamp="2026-08-07T10:00:00Z")]},
                         now_local=NEXT_DAY, now_utc=datetime(2026, 8, 7, 10, 5, tzinfo=timezone.utc))
    assert tomorrow["verdict"] == "agent"
    assert _budget_file(tmp_path) == {"date": "2026-08-07", "count": 1}


def test_budget_demotion_does_not_burn_the_thread_cooldown(tmp_path, monkeypatch):
    """A budget-held event must not stamp its thread's cooldown — otherwise tomorrow's valve tick
    would find it blocked by a cooldown it never earned a nudge for."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "0")
    _seed_snapshot(tmp_path, contacts=THREE_CONTACTS)
    _stub_llm(monkeypatch, '{"class":"urgent","why":"needs you now"}')
    te.triage({"events": [_im(rowid=440)]}, now_local=DAY, now_utc=NOW_UTC)
    assert not (tmp_path / "events" / "cooldowns.json").exists()


# ── The valve and the budget share one allowance ──────────────────────────────────────────────────

def test_valve_promotion_spends_the_daily_budget(tmp_path, monkeypatch):
    """A promotion IS a nudge: it spends the day's allowance, and the valve stops at the cap even
    with per-tick and per-hour room left."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "1")
    monkeypatch.setenv("SOTTO_VALVE_MAX_PER_HOUR", "5")
    _seed_queue(tmp_path, [
        _q("cooldown", sender="Sarah Chen", rowid=1, handle="+14155551234", held="urgent"),
        _q("quiet", sender="Dhruv Patel", rowid=2, handle="+14155559999", held="actionable"),
    ])
    out = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert [e["sender"] for e in out["bundle"]["events"]] == ["Sarah Chen"]   # cap 1, not 2
    assert _budget_file(tmp_path) == {"date": "2026-08-06", "count": 1}
    # Budget now spent — the next tick promotes nothing and says why.
    out2 = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS + 60)
    assert out2["verdict"] == "drop" and "budget" in out2["reason"]
    assert len(_queue_entries(tmp_path)) == 1                  # Dhruv waits for the digest/tomorrow


def test_valve_promotes_exempt_held_classes_without_spending_budget(tmp_path, monkeypatch):
    """A cooldown-demoted MISSED CALL keeps its exemption through the valve — promoted with the
    budget at zero, and it doesn't consume it."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "0")
    _seed_queue(tmp_path, [
        _q("cooldown", sender="Dhruv Patel", rowid=1, handle="+14155559999", held="urgent"),
        _q("cooldown", sender="Sarah Chen", rowid=2, handle="+14155551234", held="missed_call"),
    ])
    out = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out["verdict"] == "agent"
    assert [e["class"] for e in out["bundle"]["events"]] == ["missed_call"]
    assert _budget_file(tmp_path) is None
    assert len(_queue_entries(tmp_path)) == 1                  # the non-exempt one stayed put


def test_budget_held_entries_are_promotable_once_the_day_rolls(tmp_path, monkeypatch):
    """'budget' is in PROMOTABLE_CLASSES — a real ask held only by the cap comes back through the
    valve once there is allowance again (in practice: the next local day)."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_queue(tmp_path, [_q("budget", sender="Sarah Chen", rowid=1, held="scheduling_ask")])
    (tmp_path / "events" / "budget.json").write_text(json.dumps({"date": "2026-08-06", "count": 9}))
    assert te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC,
                            now_ts=VALVE_NOW_TS)["verdict"] == "drop"     # today: spent
    out = te.release_valve(now_local=NEXT_DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out["verdict"] == "agent"
    assert out["bundle"]["events"][0]["class"] == "scheduling_ask"        # held class carried
    assert _budget_file(tmp_path) == {"date": "2026-08-07", "count": 1}


# ── Cadence: the nudge snooze (preferences.explicit.nudge_snooze_until) ───────────────────────────

def _snooze(tmp_path, until):
    (tmp_path / "preferences.json").write_text(json.dumps({"explicit": {"nudge_snooze_until": until}}))


def test_snooze_holds_every_agent_verdict_including_missed_calls(tmp_path, monkeypatch):
    """An explicit snooze outranks even the VIP/missed-call carve-outs — and it lands BEFORE
    Tier 1, so a snoozed hour costs nothing in LLM calls."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path, contacts=THREE_CONTACTS)
    _snooze(tmp_path, "2026-08-06T15:00")
    _no_llm(monkeypatch)
    out = te.triage({"events": [_im(rowid=500), _call(rowid=501, phone="+14155559999")]},
                    now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue" and out["bundle"] == {}
    assert [e["verdict_class"] for e in _queue_entries(tmp_path)] == ["snoozed", "snoozed"]
    rows = _surfaced_entries(tmp_path)
    assert [r["verdict"] for r in rows] == ["queue", "queue"]
    for r in rows:
        assert set(r) == {"ts", "sender", "channel", "verdict", "reason", "class"}
        assert r["class"] == "snoozed" and "snoozed until 2026-08-06T15:00" in r["reason"]
    assert _budget_file(tmp_path) is None                      # a held event spends no budget


def test_expired_or_broken_snooze_is_inert(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path, contacts=THREE_CONTACTS)
    _stub_llm(monkeypatch, '{"class":"urgent","why":"needs you now"}')
    _snooze(tmp_path, "2026-08-06T09:00")                      # already lifted at 11:00
    assert te.triage({"events": [_im(rowid=510)]}, now_local=DAY, now_utc=NOW_UTC)["verdict"] == "agent"
    _snooze(tmp_path, "not-a-timestamp")                       # never silence forever on a typo
    assert te.triage({"events": [_im(rowid=511, handle=HANDLES[1])]},
                     now_local=DAY, now_utc=NOW_UTC)["verdict"] == "agent"


def test_valve_holds_while_snoozed_and_never_promotes_snoozed_entries(tmp_path, monkeypatch):
    """Two halves of one decision: the valve is held outright during a snooze, and 'snoozed' is not
    a promotable class — so when the snooze lifts the held events do NOT arrive as a late burst;
    they ride the digest (digest_check counts them) or the next brief."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_queue(tmp_path, [
        _q("snoozed", sender="Sarah Chen", rowid=1, held="urgent"),
        _q("quiet", sender="Dhruv Patel", rowid=2, handle="+14155559999"),
    ])
    _snooze(tmp_path, "2026-08-06T15:00")
    out = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out["verdict"] == "drop" and "snoozed until 2026-08-06T15:00" in out["reason"]
    assert len(_queue_entries(tmp_path)) == 2
    # Snooze lifts → the quiet-held entry promotes; the snoozed one stays for the digest.
    _snooze(tmp_path, "")
    out2 = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert [e["sender"] for e in out2["bundle"]["events"]] == ["Dhruv Patel"]
    assert [e["verdict_class"] for e in _queue_entries(tmp_path)] == ["snoozed"]
    assert "snoozed" not in te.PROMOTABLE_CLASSES


# ── In-meeting hold (Editor Step 2 item 2: quiet hours, but for rooms) ────────────────────────────
# The receiver's shared calendar cache (calcache.py) writes $SOTTO_DATA/cache/calendar_today.json.
# Triage only READS it — cheaply, tolerantly, and never on a stale belief.

def _cal_event(start="2026-08-06T09:30:00+00:00", end="2026-08-06T10:35:00+00:00",
               attendees=2, all_day=False, summary="Board sync"):
    return {"summary": summary, "start": start, "end": end,
            "attendees": attendees, "all_day": all_day}


def _seed_calendar(tmp_path, events, generated_at="2026-08-06T10:00:00Z", refresh_secs=900,
                   date="2026-08-06"):
    d = tmp_path / "cache"
    d.mkdir(parents=True, exist_ok=True)
    (d / "calendar_today.json").write_text(json.dumps({
        "generated_at": generated_at, "date": date,
        "refresh_secs": refresh_secs, "events": events}))


def _urgent_batch(tmp_path, monkeypatch, rowid=900, ev=None, reset=True):
    """A known sender with a Tier-1 'urgent' verdict — an event that WOULD nudge. `reset` clears
    the OTHER volume controls (thread cooldown, daily budget) so a test with several sub-cases
    measures the meeting hold alone and not the residue of the sub-case before it."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    if reset:
        for name in ("cooldowns.json", "budget.json"):
            p = tmp_path / "events" / name
            if p.exists():
                p.unlink()
    _seed_snapshot(tmp_path)
    _stub_llm(monkeypatch, '{"class":"urgent","why":"deadline today"}')
    return te.triage({"events": [ev if ev is not None else _im(rowid=rowid)]},
                     now_local=DAY, now_utc=NOW_UTC)


def test_in_meeting_holds_a_would_be_nudge_without_spending_budget_or_cooldown(tmp_path, monkeypatch):
    """NOW (10:05Z) inside a 09:30–10:35Z meeting with another human → the urgent ask is HELD, not
    nudged. It burns neither the thread cooldown nor a unit of the day's interrupt budget: both are
    spent later, by the valve, if and when the hold lifts."""
    _seed_calendar(tmp_path, [_cal_event()])
    out = _urgent_batch(tmp_path, monkeypatch)
    assert out["verdict"] == "queue"
    entry = _queue_entries(tmp_path)[-1]
    assert entry["verdict_class"] == "meeting_hold"
    assert entry["held_class"] == "urgent"               # the valve restores the right treatment
    assert not (tmp_path / "events" / "budget.json").exists()
    assert not (tmp_path / "events" / "cooldowns.json").exists()


def test_meeting_hold_reason_reads_well_in_the_record(tmp_path, monkeypatch):
    """The Record's sentence composer builds "Held — <reason>" and drops the name when the reason
    already carries it (the same shape the budget/snooze reasons use). Target sentence:
    "Held — in a meeting until 10:35 AM — Sarah Chen"."""
    _seed_calendar(tmp_path, [_cal_event()])
    out = _urgent_batch(tmp_path, monkeypatch)
    assert out["reason"] == "in a meeting until 10:35 AM — Sarah Chen"
    row = _surfaced_entries(tmp_path)[-1]
    assert row["verdict"] == "queue" and row["class"] == "meeting_hold"
    assert row["reason"] == "in a meeting until 10:35 AM — Sarah Chen"
    assert row["sender"] == "Sarah Chen" and row["channel"] == "imessage"
    assert row["ts"].endswith("Z")
    assert row["sender"].lower() in row["reason"].lower()   # composer renders the reason alone


def test_solo_block_and_all_day_event_never_hold(tmp_path, monkeypatch):
    """A focus block is not a room and a company holiday is not a place you are — the docket
    already learned this distinction, and the count in the cache carries it."""
    _seed_calendar(tmp_path, [_cal_event(attendees=0, summary="Focus block")])
    assert _urgent_batch(tmp_path, monkeypatch, rowid=901)["verdict"] == "agent"
    _seed_calendar(tmp_path, [_cal_event(start="2026-08-06", end="2026-08-07",
                                         attendees=5, all_day=True, summary="Company offsite")])
    assert _urgent_batch(tmp_path, monkeypatch, rowid=902)["verdict"] == "agent"
    # …and an event that simply isn't happening now
    _seed_calendar(tmp_path, [_cal_event(start="2026-08-06T13:00:00+00:00",
                                         end="2026-08-06T14:00:00+00:00")])
    assert _urgent_batch(tmp_path, monkeypatch, rowid=903)["verdict"] == "agent"


def test_missed_call_is_exempt_from_the_meeting_hold(tmp_path, monkeypatch):
    """A missed call from someone you know is exactly what should reach you mid-meeting. The hold
    exempts LESS than the budget does: a post-meeting tap is budget-free but still hold-able."""
    assert te.MEETING_HOLD_EXEMPT_CLASSES == {"missed_call", "escalation"}
    assert "post_meeting" in te.BUDGET_EXEMPT_CLASSES
    assert "post_meeting" not in te.MEETING_HOLD_EXEMPT_CLASSES
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    _seed_calendar(tmp_path, [_cal_event()])
    out = te.triage({"events": [_call()]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "agent" and "missed call from Sarah Chen" in out["reason"]


def test_stale_or_missing_calendar_cache_never_holds(tmp_path, monkeypatch):
    """Never hold on a stale belief about where the user is. Older than 2 refresh intervals, no
    file at all, an undateable stamp, or garbage on disk → the hold simply does not engage."""
    # 40 min old against a 900s cadence = past 2 intervals
    _seed_calendar(tmp_path, [_cal_event()], generated_at="2026-08-06T09:25:00Z")
    assert _urgent_batch(tmp_path, monkeypatch, rowid=910)["verdict"] == "agent"
    # exactly inside the bound (25 min old) still holds — the disengage is about staleness, not
    # about the cache being ignorable
    _seed_calendar(tmp_path, [_cal_event()], generated_at="2026-08-06T09:40:00Z")
    assert _urgent_batch(tmp_path, monkeypatch, rowid=911)["verdict"] == "queue"
    # a shorter declared cadence tightens the bound (2 × 300s = 10 min)
    _seed_calendar(tmp_path, [_cal_event()], generated_at="2026-08-06T09:40:00Z", refresh_secs=300)
    assert _urgent_batch(tmp_path, monkeypatch, rowid=912)["verdict"] == "agent"
    # undateable belief
    _seed_calendar(tmp_path, [_cal_event()], generated_at="")
    assert _urgent_batch(tmp_path, monkeypatch, rowid=913)["verdict"] == "agent"
    # garbage / missing
    (tmp_path / "cache" / "calendar_today.json").write_text("{not json")
    assert _urgent_batch(tmp_path, monkeypatch, rowid=914)["verdict"] == "agent"
    (tmp_path / "cache" / "calendar_today.json").unlink()
    assert _urgent_batch(tmp_path, monkeypatch, rowid=915)["verdict"] == "agent"


def test_calendar_cache_for_another_day_never_holds(tmp_path, monkeypatch):
    """calcache stamps the day it gathered; triage checks it. A file for any other date is
    yesterday's belief about where you are, however recently it was written — never hold on
    yesterday's calendar. A missing date reads the same way (an unknown is not a hold)."""
    _seed_calendar(tmp_path, [_cal_event()], date="2026-08-05")      # yesterday's gather
    assert _urgent_batch(tmp_path, monkeypatch, rowid=916)["verdict"] == "agent"
    _seed_calendar(tmp_path, [_cal_event()], date="")                # no date at all
    assert _urgent_batch(tmp_path, monkeypatch, rowid=917)["verdict"] == "agent"
    # …and today's file still holds, unchanged
    _seed_calendar(tmp_path, [_cal_event()])
    assert _urgent_batch(tmp_path, monkeypatch, rowid=918)["verdict"] == "queue"
    # the valve reads the same hold, so a wrong-dated cache doesn't shut it either
    _seed_calendar(tmp_path, [_cal_event()], date="2026-08-05")
    assert te.release_valve(now_local=DAY, now_utc=NOW_UTC,
                            now_ts=VALVE_NOW_TS)["verdict"] == "agent"


def test_wall_clock_label_is_the_event_s_own_clock(tmp_path):
    """No tz math (the calendar's times are already the user's), so no DST drift — and the label
    reads the way a person says it."""
    assert te._wall_clock("2026-08-06T14:30:00-07:00") == "2:30 PM"
    assert te._wall_clock("2026-08-06T00:05:00Z") == "12:05 AM"
    assert te._wall_clock("2026-08-06T12:00:00Z") == "12:00 PM"
    assert te._wall_clock("2026-08-06") == ""            # all-day: no clock at all
    assert te._wall_clock("") == ""


def test_meeting_held_ask_rides_the_valve_out_when_the_meeting_ends(tmp_path, monkeypatch):
    """The release path, end to end and with NO new machinery: while the meeting runs the valve is
    shut (it must not push OTHER queued asks into the room either); its next tick after the meeting
    ends promotes the held ask, spending the day's interrupt budget and the hourly valve budget
    exactly as any other promotion does."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert "meeting_hold" in te.PROMOTABLE_CLASSES
    _seed_calendar(tmp_path, [_cal_event()])
    assert _urgent_batch(tmp_path, monkeypatch, rowid=920)["verdict"] == "queue"
    # 10:05Z — still in the room: nothing moves, and the entry keeps its place
    shut = te.release_valve(now_local=DAY, now_utc=NOW_UTC, now_ts=VALVE_NOW_TS)
    assert shut["verdict"] == "drop" and "in a meeting until 10:35 AM" in shut["reason"]
    assert len(_queue_entries(tmp_path)) == 1
    # 12:00Z — the meeting ended at 10:35; the heartbeat's next tick is the release
    out = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out["verdict"] == "agent"
    ev = out["bundle"]["events"][0]
    assert ev["sender"] == "Sarah Chen" and ev["class"] == "urgent"   # held_class restored
    assert _queue_entries(tmp_path) == []
    assert _surfaced_entries(tmp_path)[-1]["verdict"] == "promoted"
    # the promotion spends both budgets — it IS a nudge
    assert json.loads((tmp_path / "events" / "budget.json").read_text())["count"] == 1
    assert len(json.loads((tmp_path / "events" / "valve_state.json").read_text())["promotions"]) == 1


def test_valve_hold_covers_every_queued_ask_not_just_the_meeting_held_one(tmp_path, monkeypatch):
    """A cooldown-held ask must not be promoted INTO a meeting either — the hold is about the room,
    not about how the entry got queued."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_calendar(tmp_path, [_cal_event()])
    _seed_queue(tmp_path, [_q("cooldown", rowid=1)])
    out = te.release_valve(now_local=DAY, now_utc=NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out["verdict"] == "drop" and "in a meeting" in out["reason"]
    assert len(_queue_entries(tmp_path)) == 1
    # stale calendar → the valve is not held by a belief it can't trust
    _seed_calendar(tmp_path, [_cal_event()], generated_at="2026-08-06T08:00:00Z")
    out2 = te.release_valve(now_local=DAY, now_utc=NOW_UTC, now_ts=VALVE_NOW_TS)
    assert out2["verdict"] == "agent"


# ── The post-meeting tap (Editor Step 2 item 3) ──────────────────────────────────────────────────
# The receiver's calendar refresh thread (calcache.py) injects a synthetic `meeting_end` event when
# a meeting it saw on the calendar has just ended. These tests are about ONE claim: that injecting
# it as an event means every existing gate applies to the tap with no second implementation.

def _tap(summary="Product sync", start="2026-08-06T09:00:00+00:00",
         end="2026-08-06T10:00:00+00:00", attendees=None, key=None):
    """The exact shape calcache.tap_event() emits (see runtime/trigger-receiver/calcache.py)."""
    att = [{"name": "Sarah Chen", "email": "sarah@acme.com"}] if attendees is None else attendees
    return {"source": "meeting_end", "rowid": key or f"{start}|{end}|{summary}",
            "timestamp": end, "summary": summary, "start": start, "end": end,
            "attendees": att, "meeting_link": "", "location": "", "is_from_me": False, "text": ""}


def _tap_now(minutes_after_end=6):
    """UTC 'now' a few minutes after the fixture meeting's 10:00Z end."""
    from datetime import timedelta
    return datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes_after_end)


def test_post_meeting_tap_goes_agent_with_the_wrapped_reason(tmp_path, monkeypatch):
    """The happy path: a just-ended peopled meeting is an agent verdict of class post_meeting, and
    the reason is the sentence the Record composes ("Nudged — your 9:00 AM with Sarah Chen
    wrapped"). No Tier-1 call — the calendar already carried the whole judgment."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    out = te.triage({"events": [_tap()]}, now_local=DAY, now_utc=_tap_now())
    assert out["verdict"] == "agent"
    ev = out["bundle"]["events"][0]
    assert ev["class"] == "post_meeting" and ev["sender"] == "Sarah Chen"
    assert ev["why"] == "your 9:00 AM with Sarah Chen wrapped"
    assert ev["event"]["summary"] == "Product sync"           # the skill composes from the event
    assert [a["email"] for a in ev["event"]["attendees"]] == ["sarah@acme.com"]


def test_post_meeting_reason_names_the_room_not_just_one_person(tmp_path, monkeypatch):
    """Two attendees are named; three or more collapse to "and N others" — the nudge's first line
    has to read like a person said it."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    two = [{"name": "Sarah Chen", "email": "s@acme.com"}, {"name": "Dhruv Patel", "email": "d@acme.com"}]
    out = te.triage({"events": [_tap(attendees=two)]}, now_local=DAY, now_utc=_tap_now())
    assert out["reason"] == "your 9:00 AM with Sarah Chen and Dhruv Patel wrapped"
    four = two + [{"name": "Ben Butler", "email": "b@x.com"}, {"name": "Ana Ruiz", "email": "a@x.com"}]
    out2 = te.triage({"events": [_tap(attendees=four, key="k2")]}, now_local=DAY, now_utc=_tap_now())
    assert out2["reason"] == "your 9:00 AM with Sarah Chen and 3 others wrapped"
    # an attendee with no display name still gets named by address, never anonymously
    out3 = te.triage({"events": [_tap(attendees=[{"name": "", "email": "zoe@acme.com"}], key="k3")]},
                     now_local=DAY, now_utc=_tap_now())
    assert out3["reason"] == "your 9:00 AM with zoe@acme.com wrapped"


def test_post_meeting_tap_records_a_surfaced_row_the_composer_can_render(tmp_path, monkeypatch):
    """Record visibility: one surfaced.jsonl row per tap, verdict agent, class post_meeting, and a
    reason that carries the name (so the composer renders the reason alone)."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    te.triage({"events": [_tap()]}, now_local=DAY, now_utc=_tap_now())
    row = _surfaced_entries(tmp_path)[-1]
    assert row["verdict"] == "agent" and row["class"] == "post_meeting"
    assert row["reason"] == "your 9:00 AM with Sarah Chen wrapped"
    assert row["sender"] == "Sarah Chen" and row["channel"] == "meeting_end"
    assert row["ts"].endswith("Z")
    assert row["sender"].lower() in row["reason"].lower()


def test_post_meeting_taps_do_not_spend_the_daily_interrupt_budget(tmp_path, monkeypatch):
    """Two caps, not one: taps have their own (SOTTO_TAP_MAX_PER_DAY, enforced at dispatch in
    calcache), so they are budget-exempt here. Three taps must not starve the urgent 4th event —
    that starvation is exactly what the shared pot caused."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "1")
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    assert "post_meeting" in te.BUDGET_EXEMPT_CLASSES
    others = [{"name": "Dhruv Patel", "email": "dhruv@acme.com"}]   # not the urgent sender below
    for i in range(3):
        out = te.triage({"events": [_tap(summary=f"Sync {i}", key=f"k{i}", attendees=others)]},
                        now_local=DAY, now_utc=_tap_now())
        assert out["verdict"] == "agent"
    assert not (tmp_path / "events" / "budget.json").exists()   # nothing spent
    # the day's single interrupt is still available to a genuine ask
    assert _urgent_batch(tmp_path, monkeypatch, rowid=990, reset=False)["verdict"] == "agent"
    assert json.loads((tmp_path / "events" / "budget.json").read_text())["count"] == 1


def test_budget_file_keeps_its_shape(tmp_path, monkeypatch):
    """No new counters, no new keys: an existing {date, count} budget.json is read and incremented
    exactly as before (a tap adds nothing to it)."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "4")
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    (tmp_path / "events").mkdir(parents=True, exist_ok=True)
    (tmp_path / "events" / "budget.json").write_text(
        json.dumps({"date": te._local_day(DAY), "count": 2}))
    te.triage({"events": [_tap(attendees=[{"name": "Dhruv Patel", "email": "dhruv@acme.com"}])]},
              now_local=DAY, now_utc=_tap_now())
    assert json.loads((tmp_path / "events" / "budget.json").read_text()) == {
        "date": te._local_day(DAY), "count": 2}
    _urgent_batch(tmp_path, monkeypatch, rowid=991, reset=False)
    assert json.loads((tmp_path / "events" / "budget.json").read_text()) == {
        "date": te._local_day(DAY), "count": 3}


def test_post_meeting_tap_holds_during_the_next_meeting_and_rides_the_valve_out(tmp_path, monkeypatch):
    """Back-to-back meetings, the whole point of routing the tap through triage: the tap for A is
    held while B runs (a tap is budget-free but NOT hold-free), and the release valve delivers it —
    with its post_meeting treatment intact — on the first tick after B ends, still spending no
    interrupt budget."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    # B runs 10:00–10:35Z; NOW is 10:06Z, six minutes into it, just after A ended at 10:00Z.
    _seed_calendar(tmp_path, [_cal_event(start="2026-08-06T10:00:00+00:00",
                                         end="2026-08-06T10:35:00+00:00", summary="Board sync")])
    out = te.triage({"events": [_tap()]}, now_local=DAY, now_utc=_tap_now())
    assert out["verdict"] == "queue"
    entry = _queue_entries(tmp_path)[-1]
    assert entry["verdict_class"] == "meeting_hold" and entry["held_class"] == "post_meeting"
    assert not (tmp_path / "events" / "budget.json").exists()
    assert not (tmp_path / "events" / "cooldowns.json").exists()
    # 12:00Z — B is over; the heartbeat's next tick promotes the tap intact
    promoted = te.release_valve(now_local=DAY, now_utc=VALVE_NOW_UTC, now_ts=VALVE_NOW_TS)
    assert promoted["verdict"] == "agent"
    ev = promoted["bundle"]["events"][0]
    assert ev["class"] == "post_meeting" and ev["event"]["source"] == "meeting_end"
    assert not (tmp_path / "events" / "budget.json").exists()   # exempt on promotion too


def test_post_meeting_tap_respects_quiet_hours_and_the_snooze(tmp_path, monkeypatch):
    """Both cadence levers apply, and both keep the tap's own reason so the Record can explain it."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    out = te.triage({"events": [_tap()]}, now_local=NIGHT, now_utc=_tap_now())
    assert out["verdict"] == "queue"
    assert _queue_entries(tmp_path)[-1]["verdict_class"] == "quiet"
    assert out["reason"] == "quiet hours — your 9:00 AM with Sarah Chen wrapped"
    monkeypatch.setattr(te._prefs, "snooze_active", lambda now_local=None, explicit=None: True)
    out2 = te.triage({"events": [_tap(key="k2")]}, now_local=DAY, now_utc=_tap_now())
    assert out2["verdict"] == "queue"
    assert _queue_entries(tmp_path)[-1]["verdict_class"] == "snoozed"
    assert "wrapped" in out2["reason"]
    assert not (tmp_path / "events" / "budget.json").exists()   # neither spent a nudge


def test_post_meeting_tap_with_no_other_attendees_is_dropped_silently(tmp_path, monkeypatch):
    """calcache never emits one (a solo block is not a meeting), but a hand-fed event must not
    produce a nudge about nobody."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    out = te.triage({"events": [_tap(attendees=[])]}, now_local=DAY, now_utc=_tap_now())
    assert out["verdict"] == "drop"
    assert _queue_entries(tmp_path) == []
    assert _surfaced_entries(tmp_path)[-1]["class"] == "post_meeting"


def test_post_meeting_tap_cools_down_per_meeting_and_goes_stale_like_any_event(tmp_path, monkeypatch):
    """The thread key is the meeting, so a re-injected end can't double-nudge — and a tap the
    receiver only noticed an hour late is demoted by the ordinary staleness gate."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    assert te._thread_key(_tap()) == "meeting_end:2026-08-06T09:00:00+00:00|2026-08-06T10:00:00+00:00|Product sync"
    assert te.triage({"events": [_tap()]}, now_local=DAY, now_utc=_tap_now())["verdict"] == "agent"
    again = te.triage({"events": [_tap()]}, now_local=DAY, now_utc=_tap_now(7))
    assert again["verdict"] == "queue" and _queue_entries(tmp_path)[-1]["verdict_class"] == "cooldown"
    stale = te.triage({"events": [_tap(summary="Design review", key="k2")]},
                      now_local=DAY, now_utc=_tap_now(75))
    assert stale["verdict"] == "queue"
    entry = _queue_entries(tmp_path)[-1]
    assert entry["verdict_class"] == "stale" and entry["held_class"] == "post_meeting"


# ── Real-time escalation join (Editor Step 2 item 4) ──────────────────────────────────────────────
# Same person, 2+ DISTINCT channels, inside the window → one agent verdict of class "escalation"
# that bypasses the per-thread cooldown, the daily budget and the in-meeting hold. The join reads
# the two retained ledgers (queue.jsonl carries queued events; surfaced.jsonl is the ONLY place an
# agent verdict such as a live missed call is retained), so the tests seed them at controlled times.

def _seed_surfaced(tmp_path, rows):
    d = tmp_path / "events"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "surfaced.jsonl", "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _seed_queue(tmp_path, entries):
    d = tmp_path / "events"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "queue.jsonl", "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _prior_call(ts="2026-08-06T09:45:00Z", sender="Sarah Chen", cls="missed_call"):
    """The surfaced row a live missed call leaves behind (20 min before NOW_UTC by default)."""
    return {"ts": ts, "sender": sender, "channel": "calls", "verdict": "agent",
            "reason": f"missed call from {sender}", "class": cls}


def _email(rowid="e1", **kw):
    e = {"source": "email", "rowid": rowid, "from": "Sarah Chen <sarah@acme.com>",
         "subject": "the deck", "body": "can you send the revised deck before 3?",
         "threadId": "t1", "timestamp": "2026-08-06T10:00:00Z"}
    e.update(kw)
    return e


def test_escalation_joins_two_channels_and_names_the_fact(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    _seed_surfaced(tmp_path, [_prior_call()])
    _stub_llm(monkeypatch, '{"class":"actionable","why":"asks for the deck"}')
    out = te.triage({"events": [_email()]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "agent"
    ev = out["bundle"]["events"][0]
    assert ev["class"] == "escalation"
    # The reason is name-carrying and chronological — The Record and the nudge both lead with it.
    assert ev["why"] == "Sarah Chen called AND emailed within 20 min"
    assert _surfaced_entries(tmp_path)[-1]["class"] == "escalation"


def test_escalation_bypasses_cooldown_budget_and_the_meeting_hold(tmp_path, monkeypatch):
    """The three gates that would each silence it, all switched on at once."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "0")          # the day's budget is spent
    _seed_snapshot(tmp_path)
    _seed_surfaced(tmp_path, [_prior_call()])
    _seed_calendar(tmp_path, [_cal_event()])               # …and the user is in a meeting
    (tmp_path / "events").mkdir(parents=True, exist_ok=True)
    (tmp_path / "events" / "cooldowns.json").write_text(
        json.dumps({"email:t1": time.time()}))             # …and the thread just nudged
    _stub_llm(monkeypatch, '{"class":"urgent","why":"needs it before 3"}')
    out = te.triage({"events": [_email()]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "agent" and out["bundle"]["events"][0]["class"] == "escalation"
    assert not (tmp_path / "events" / "budget.json").exists()   # exempt: it spends nothing
    assert _queue_entries(tmp_path) == []


def test_escalation_matches_on_identifier_when_the_ledger_has_no_name(tmp_path, monkeypatch):
    """Identity match is exact-name OR exact normalized identifier — never fuzzy."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    _seed_queue(tmp_path, [{"ts": "2026-08-06T09:55:00Z", "verdict_class": "actionable",
                            "sender": "", "event": _im(rowid=60)}])
    out = te.triage({"events": [_call(rowid=61)]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "agent"
    ev = out["bundle"]["events"][0]
    assert ev["class"] == "escalation"
    assert ev["why"] == "Sarah Chen texted AND called within 5 min"


def test_escalation_matches_a_surfaced_row_that_fell_back_to_the_address(tmp_path, monkeypatch):
    """A surfaced row carries whatever triage resolved — often a bare address. It still has to
    match the person, and it must match on the identifier, not on the address-as-a-name."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path, contacts=[])                  # Contacts knows nobody → name fallbacks
    _seed_surfaced(tmp_path, [{"ts": "2026-08-06T09:50:00Z", "sender": "sarah@acme.com",
                               "channel": "email", "verdict": "agent", "reason": "asked for the deck",
                               "class": "actionable"}])
    _stub_llm(monkeypatch, '{"class":"actionable","why":"asks again"}')
    ev = {"source": "whatsapp", "rowid": "w1", "contact_jid": "15551239999@s.whatsapp.net",
          "sender_jid": "15551239999@s.whatsapp.net", "partner_name": "Sarah Chen",
          "text": "sent you a mail too — can you look?", "timestamp": "2026-08-06T10:00:00Z"}
    out = te.triage({"events": [ev]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["bundle"]["events"][0]["class"] == "actionable"   # different identifiers → no join
    _seed_surfaced(tmp_path, [{"ts": "2026-08-06T09:50:00Z", "sender": "15551239999",
                               "channel": "calls", "verdict": "agent", "reason": "missed call",
                               "class": "missed_call"}])
    ev2 = dict(ev, rowid="w2", text="did you get my message?")
    out2 = te.triage({"events": [ev2]}, now_local=DAY, now_utc=NOW_UTC)
    esc = out2["bundle"]["events"][0]
    assert esc["class"] == "escalation"
    assert esc["why"] == "Sarah Chen called AND messaged on WhatsApp within 15 min"


def test_a_meeting_ending_is_never_escalation_evidence(tmp_path, monkeypatch):
    """The post-meeting tap is Sotto's own synthetic event. An attendee texting minutes after the
    meeting is the NORMAL follow-up, not the person reaching you twice — without the meeting_end
    skip in _ledger_rows, every such text would fire the loudest (budget- and cooldown-exempt)
    nudge Sotto can send."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    _seed_surfaced(tmp_path, [{"ts": "2026-08-06T09:55:00Z", "sender": "Sarah Chen",
                               "channel": "meeting_end", "verdict": "agent",
                               "reason": "meeting ended: Roadmap sync", "class": "post_meeting"}])
    _seed_queue(tmp_path, [{"ts": "2026-08-06T09:55:00Z", "verdict_class": "meeting_hold",
                            "held_class": "post_meeting", "sender": "Sarah Chen",
                            "event": {"source": "meeting_end", "summary": "Roadmap sync",
                                      "timestamp": "2026-08-06T09:55:00Z"}}])
    _stub_llm(monkeypatch, '{"class":"actionable","why":"sends the follow-up"}')
    out = te.triage({"events": [_im()]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "agent"
    assert out["bundle"]["events"][0]["class"] == "actionable"   # no join on the tap


def test_one_channel_alone_never_escalates(tmp_path, monkeypatch):
    """A second message on the SAME channel is a repeat, not an escalation."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    _seed_queue(tmp_path, [{"ts": "2026-08-06T09:55:00Z", "verdict_class": "ambient",
                            "sender": "Sarah Chen", "event": _email(rowid="e0", threadId="t0")}])
    _stub_llm(monkeypatch, '{"class":"actionable","why":"asks for the deck"}')
    out = te.triage({"events": [_email()]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "agent" and out["bundle"]["events"][0]["class"] == "actionable"


def test_escalation_window_expires(tmp_path, monkeypatch):
    """The call was 95 minutes ago — outside the 45-minute default. Two channels, no escalation."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    _seed_surfaced(tmp_path, [_prior_call(ts="2026-08-06T08:30:00Z")])
    _stub_llm(monkeypatch, '{"class":"actionable","why":"asks for the deck"}')
    out = te.triage({"events": [_email()]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["bundle"]["events"][0]["class"] == "actionable"
    # …and widening the window brings the same pair back into range.
    monkeypatch.setenv("SOTTO_ESCALATION_WINDOW_MIN", "120")
    out2 = te.triage({"events": [_email(rowid="e2", threadId="t2")]}, now_local=DAY, now_utc=NOW_UTC)
    assert out2["bundle"]["events"][0]["class"] == "escalation"


def test_unknown_sender_never_escalates(tmp_path, monkeypatch):
    """An unresolved number can't be 'the same person' — it is every unknown number at once."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    _seed_surfaced(tmp_path, [_prior_call(sender="+19998887777")])
    out = te.triage({"events": [_im("wire the money now", handle="+19998887777", rowid=62)]},
                    now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue"
    assert _queue_entries(tmp_path)[-1]["verdict_class"] == "unknown"


def test_two_ambient_channels_without_a_call_or_an_ask_do_not_escalate(tmp_path, monkeypatch):
    """The join needs real evidence: a call on either side, or an ask-like Tier-1 class."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    _seed_queue(tmp_path, [{"ts": "2026-08-06T09:55:00Z", "verdict_class": "ambient",
                            "sender": "Sarah Chen", "event": _im(rowid=63)}])
    _stub_llm(monkeypatch, '{"class":"ambient","why":"just chatter"}')
    out = te.triage({"events": [_email()]}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue"
    assert _queue_entries(tmp_path)[-1]["verdict_class"] == "ambient"


def test_escalation_fires_once_per_window_not_once_per_message(tmp_path, monkeypatch):
    """The exemptions that make the first one land are exactly what would make the rest a storm."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    _seed_surfaced(tmp_path, [_prior_call()])
    _stub_llm(monkeypatch, '{"class":"actionable","why":"asks for the deck"}')
    assert te.triage({"events": [_email()]}, now_local=DAY,
                     now_utc=NOW_UTC)["bundle"]["events"][0]["class"] == "escalation"
    # A third channel minutes later still nudges on its own merits — but as its ordinary Tier-1
    # class, spending the budget and honoring the cooldown like everything else. Never a second
    # exempt escalation about the same push.
    again = te.triage({"events": [_im("did you see my email?", rowid=64)]},
                      now_local=DAY, now_utc=NOW_UTC)
    assert again["bundle"]["events"][0]["class"] == "actionable"


def test_backlog_never_escalates(tmp_path, monkeypatch):
    """A catchup batch is history replayed, not someone reaching you now — the loudest nudge Sotto
    sends must never fire on it."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    _seed_snapshot(tmp_path)
    _seed_surfaced(tmp_path, [_prior_call()])
    _stub_llm(monkeypatch, '{"class":"actionable","why":"asks for the deck"}')
    out = te.triage({"events": [_email()], "catchup": True}, now_local=DAY, now_utc=NOW_UTC)
    assert out["verdict"] == "queue"
    entry = _queue_entries(tmp_path)[-1]
    assert entry["verdict_class"] == "stale" and entry["held_class"] == "actionable"


def test_quiet_hours_and_snooze_outrank_the_join(tmp_path, monkeypatch):
    """Cadence the user set by the clock wins: an escalation is loud, not exempt from silence."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _seed_snapshot(tmp_path)
    _no_llm(monkeypatch)
    _seed_surfaced(tmp_path, [_prior_call()])
    out = te.triage({"events": [_email()]}, now_local=NIGHT, now_utc=NOW_UTC)
    assert out["verdict"] == "queue" and _queue_entries(tmp_path)[-1]["verdict_class"] == "quiet"
    monkeypatch.setattr(te._prefs, "snooze_active", lambda now_local=None, explicit=None: True)
    out2 = te.triage({"events": [_email(rowid="e3", threadId="t3")]}, now_local=DAY, now_utc=NOW_UTC)
    assert out2["verdict"] == "queue" and _queue_entries(tmp_path)[-1]["verdict_class"] == "snoozed"


def test_held_escalation_is_promoted_by_the_valve_without_cooldown_or_budget(tmp_path, monkeypatch):
    """COOLDOWN_EXEMPT_CLASSES applies in the valve too — a held escalation is not cooled away."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "0")
    _seed_snapshot(tmp_path)
    now_ts = time.time()
    _seed_queue(tmp_path, [{"ts": "2026-08-06T10:00:00Z", "verdict_class": "stale",
                            "held_class": "escalation", "sender": "Sarah Chen",
                            "event": _email()}])
    (tmp_path / "events" / "cooldowns.json").write_text(json.dumps({"email:t1": now_ts}))
    out = te.release_valve(now_local=DAY, now_utc=NOW_UTC, now_ts=now_ts)
    assert out["verdict"] == "agent"
    assert out["bundle"]["events"][0]["class"] == "escalation"
    assert not (tmp_path / "events" / "budget.json").exists()


# ── Advisory lock around the read-modify-write state (budget.json, queue.jsonl) ───────────────────

def test_locked_is_reentrant_across_sequential_acquisitions(tmp_path, monkeypatch):
    """Up to four producers run this script at once, so the budget spend and the valve's queue
    rewrite take an flock first. The helper must be plain: take it, release it, take it again."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    path = os.path.join(str(tmp_path), "events", "budget.json")
    for _ in range(2):
        with te._locked(path):
            pass
    assert os.path.exists(path + ".lock")          # the lock file lives beside its data file
    # an unwritable location must not stop the work — best-effort, never a silenced nudge
    with te._locked("/proc/definitely-unwritable/budget.json"):
        pass


def test_budget_spend_holds_the_lock_and_keeps_the_file_shape(tmp_path, monkeypatch):
    """The spend is read-modify-write UNDER the lock; the write itself is still tmp + os.replace."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    seen = []
    real = te._locked
    monkeypatch.setattr(te, "_locked", lambda p: (seen.append(p), real(p))[1])
    te._budget_spend("2026-08-06", 1)
    te._budget_spend("2026-08-06", 2)
    assert seen == [te._budget_path(), te._budget_path()]
    assert json.loads((tmp_path / "events" / "budget.json").read_text()) == {
        "date": "2026-08-06", "count": 3}
