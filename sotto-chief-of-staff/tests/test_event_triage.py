"""triage_event.py — the Tier 0 verdict matrix, the stubbed Tier 1, and the agent cooldown."""
import importlib.util
import json
import os
import sys
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
