"""Source clocks must survive Mac/server timezone differences and release-valve recovery."""
from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

import pytest

import triage_event as te
import work_queue
import delivery_effects
import personal_context


def event(**changes):
    return {"source": "imessage", "rowid": 41, "handle": "+14155550123",
            "is_from_me": False, "is_group_chat": False, "text": "Please review the plan",
            "timestamp": "2026-11-02 12:35:30", "timestamp_utc": "2026-11-02T20:35:30Z",
            **changes}


NOW = datetime(2026, 11, 2, 20, 41, 44, tzinfo=timezone.utc)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "America/Los_Angeles")
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "fixture-only")
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "last_local_snapshot.json").write_text(json.dumps({"local": {"contacts": [
        {"name": "Dana", "phones": ["+14155550123"]},
    ]}}))
    monkeypatch.setattr(te.relevance, "judge", lambda *_a, **_k: {
        "class": "actionable", "why": "unanswered review request"})
    return tmp_path


@pytest.mark.parametrize("zone", ["UTC", "America/Los_Angeles", "Asia/Tokyo"])
def test_explicit_instant_controls_age_and_order(monkeypatch, zone):
    monkeypatch.setenv("SOTTO_TIMEZONE", zone)
    row = event()
    assert te._event_age_min(row, NOW) == pytest.approx(374 / 60)
    assert te._event_instant({}, row) == datetime(2026, 11, 2, 20, 35, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("source", ["imessage", "whatsapp", "calls", "whatsapp_calls"])
def test_legacy_native_wall_time_uses_configured_zone(monkeypatch, source):
    monkeypatch.setenv("SOTTO_TIMEZONE", "America/Los_Angeles")
    row = event(source=source)
    del row["timestamp_utc"]
    assert te._event_age_min(row, NOW) == pytest.approx(374 / 60)


@pytest.mark.parametrize("zone", [None, "Invalid/Owner_Zone"])
def test_legacy_native_uses_utc_when_owner_zone_cannot_resolve(isolated, monkeypatch, zone):
    from tzchain import local_now

    monkeypatch.delenv("SOTTO_TIMEZONE")
    monkeypatch.delenv("TZ", raising=False)
    if zone is not None:
        monkeypatch.setenv("SOTTO_TIMEZONE", zone)
    assert local_now().tzinfo is timezone.utc

    ask = event(timestamp="2026-11-02 20:35:30")
    del ask["timestamp_utc"]
    reply = {**ask, "rowid": 42, "timestamp": "2026-11-02 20:36:00",
             "is_from_me": True, "text": "I have it"}
    delivery = delivery_effects.for_bundle({"events": [{"class": "urgent", "event": ask}]})
    original = datetime(2026, 11, 2, 20, 35, 30, tzinfo=timezone.utc).timestamp()
    assert delivery["valid_until"] == original + delivery_effects.FRESH_NUDGE_VALID_SECONDS
    assert te._event_age_min(ask, NOW) == pytest.approx(374 / 60)
    monkeypatch.setattr(personal_context, "conversation_snapshot", lambda: [])
    assert delivery_effects.valid(delivery["effects"], NOW.timestamp())
    monkeypatch.setattr(personal_context, "conversation_snapshot", lambda: [(reply, "", "signal")])
    assert not delivery_effects.valid(delivery["effects"], NOW.timestamp())


def test_explicit_fall_back_instants_keep_identical_wall_times_distinct(monkeypatch):
    monkeypatch.setenv("SOTTO_TIMEZONE", "America/Los_Angeles")
    early = event(timestamp="2026-11-01 01:30:00", timestamp_utc="2026-11-01T08:30:00Z")
    late = event(rowid=42, timestamp="2026-11-01 01:30:00", timestamp_utc="2026-11-01T09:30:00Z")
    now = datetime(2026, 11, 1, 9, 35, tzinfo=timezone.utc)
    assert te._event_age_min(early, now) == pytest.approx(65)
    assert te._event_age_min(late, now) == pytest.approx(5)
    assert work_queue.event_item_key(early) != work_queue.event_item_key(late)


@pytest.mark.parametrize("wall", ["2026-11-01 01:30:00", "2026-03-08 02:30:00"])
def test_ambiguous_or_nonexistent_legacy_hour_uses_queue_clock(monkeypatch, wall):
    monkeypatch.setenv("SOTTO_TIMEZONE", "America/Los_Angeles")
    row = event(timestamp=wall)
    del row["timestamp_utc"]
    assert te._event_age_min(row, NOW) is None
    assert te._event_instant({"ts": NOW.isoformat()}, row) == NOW


@pytest.mark.parametrize("explicit", ["broken", "2026-11-02 12:35:30", ""])
def test_invalid_explicit_instant_never_falls_back_to_display_time(explicit):
    row = event(timestamp_utc=explicit)
    assert te._event_age_min(row, NOW) is None


@pytest.mark.parametrize("value", ["2026-11-02T20:35:30Z", "Mon, 02 Nov 2026 12:35:30 -0800",
                                  "1793651730000"])
def test_email_date_formats_preserve_existing_clock(value):
    assert te._event_age_min({"source": "email", "date": value}, NOW) == pytest.approx(374 / 60)


def test_later_owner_send_uses_source_instants_not_display_clock():
    ask = event()
    reply = event(rowid=42, is_from_me=True, timestamp="2026-11-02 21:36:00",
                  timestamp_utc="2026-11-02T20:36:00Z")
    entry = {"event": reply, "ts": "2026-11-02T20:36:05Z", "verdict_class": "signal"}
    answered = te._answered_threads([json.dumps(entry)], NOW)
    assert te._user_spoke_since({}, ask, answered)
    later_ask = event(rowid=43, timestamp_utc="2026-11-02T20:37:00Z")
    assert not te._user_spoke_since({}, later_ask, answered)


@pytest.mark.parametrize(("reply_wall", "reply_utc", "promotes"), [
    ("01:55:00", "08:55:00", False),
    ("01:10:00", "09:10:00", True),
])
def test_release_uses_only_owner_replies_observed_by_this_tick(
        isolated, monkeypatch, reply_wall, reply_utc, promotes):
    monkeypatch.setenv("SOTTO_QUIET_START", "0")
    monkeypatch.setenv("SOTTO_QUIET_END", "0")
    now = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    local = now.astimezone(ZoneInfo("America/Los_Angeles"))
    ask = event(timestamp="2026-11-01 01:50:00", timestamp_utc="2026-11-01T08:50:00Z")
    reply = event(rowid=42, timestamp=f"2026-11-01 {reply_wall}",
                  timestamp_utc=f"2026-11-01T{reply_utc}Z", is_from_me=True, text="I have it")
    queue = isolated / "events/queue.jsonl"
    queue.parent.mkdir()
    entries = [
        {"ts": "2026-11-01T08:51:00Z", "verdict_class": "meeting_hold",
         "held_class": "actionable", "sender": "Dana", "event": ask},
        {"ts": "2026-11-01T08:59:00Z", "verdict_class": "signal", "event": reply},
    ]
    queue.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    context = personal_context.current_conversation(ask, now=now, candidates=[(reply, "", "signal")])
    assert any(message["is_from_me"] for message in context) is (not promotes)
    monkeypatch.setattr(personal_context, "conversation_snapshot", lambda: [(reply, "", "signal")])
    delivery = delivery_effects.for_bundle({"events": [{"class": "urgent", "event": ask}]})
    assert delivery_effects.valid(delivery["effects"], now.timestamp()) is promotes
    out = te.release_valve(now_local=local, now_utc=now, now_ts=now.timestamp())
    assert (out["verdict"] == "agent") is promotes


@pytest.mark.parametrize("legacy", [False, True])
def test_fresh_held_ask_promotes_once_with_source_bound_deadline(isolated, legacy):
    row = event()
    if legacy:
        del row["timestamp_utc"]
    key = work_queue.event_item_key(row)
    local = NOW.astimezone(ZoneInfo("America/Los_Angeles"))
    assert te.triage({"events": [row]}, now_local=local, now_utc=NOW)["verdict"] == "queue"
    out = te.release_valve(now_local=local, now_utc=NOW, now_ts=NOW.timestamp())
    assert out["verdict"] == "agent"
    assert out["bundle"]["events"][0]["event"]["timestamp"] == row["timestamp"]
    job = work_queue.get(isolated, out["job_id"])
    assert work_queue.event_item_key(out["bundle"]["events"][0]["event"]) == key
    assert job["kind"] == "event"
    assert job["valid_until"] == (
        datetime(2026, 11, 2, 20, 35, 30, tzinfo=timezone.utc).timestamp() + te.VALVE_MAX_AGE_MIN * 60)
    assert json.loads((isolated / "events/budget.json").read_text())["count"] == 1
    assert te.release_valve(now_local=local, now_utc=NOW, now_ts=NOW.timestamp())["verdict"] != "agent"


def test_valve_skips_events_older_than_promotion_window(isolated, monkeypatch):
    row = event(timestamp="2026-11-02 05:35:30", timestamp_utc="2026-11-02T13:35:30Z")
    local = NOW.astimezone(ZoneInfo("America/Los_Angeles"))
    te.triage({"events": [row]}, now_local=local, now_utc=NOW)
    assert te.release_valve(now_local=local, now_utc=NOW, now_ts=NOW.timestamp())["verdict"] != "agent"
    assert not (isolated / "events/budget.json").exists()
    path = isolated / "events/queue.jsonl"
    [expired] = [json.loads(line) for line in path.read_text().splitlines()]
    assert expired["valve_expired"]
    # A wider window admits an undecided control of the same age, never the terminal row.
    monkeypatch.setattr(te, "VALVE_MAX_AGE_MIN", te.VALVE_MAX_AGE_MIN * 2)
    te.triage({"events": [{**row, "rowid": 42}]}, now_local=local, now_utc=NOW)
    out = te.release_valve(now_local=local, now_utc=NOW, now_ts=NOW.timestamp())
    assert [item["event"]["rowid"] for item in out["bundle"]["events"]] == [42]
    assert [json.loads(line) for line in path.read_text().splitlines()] == [expired]


def test_clock_correction_does_not_revive_a_persisted_expiry(isolated):
    local = NOW.astimezone(ZoneInfo("America/Los_Angeles"))
    row = event()
    te.triage({"events": [row]}, now_local=local, now_utc=NOW)
    path = isolated / "events/queue.jsonl"
    [entry] = [json.loads(line) for line in path.read_text().splitlines()]
    entry["valve_expired"] = True
    path.write_text(json.dumps(entry) + "\n")
    assert te.release_valve(now_local=local, now_utc=NOW, now_ts=NOW.timestamp())["verdict"] != "agent"
    assert te._valve_candidate(entry, NOW, NOW.timestamp(), te.VALVE_MAX_AGE_MIN) is None
    assert not (isolated / "events/budget.json").exists()


def test_conversation_context_does_not_include_future_source_messages(isolated):
    future = event(rowid=42, timestamp="2026-11-02 12:42:00",
                   timestamp_utc="2026-11-02T20:42:00Z", text="Future answer", is_from_me=True)
    context = personal_context.current_conversation(
        event(), now=NOW, candidates=[(future, "", "signal")])
    assert [message["text"] for message in context] == [event()["text"]]
    assert context[0]["ts"] == "2026-11-02T20:35:30+00:00"


@pytest.mark.parametrize("reply_at,eligible", [("2026-11-02T20:34:00Z", True),
                                             ("2026-11-02T20:36:00Z", False)])
def test_delivery_freshness_and_later_reply_use_the_same_source_clock(isolated, monkeypatch, reply_at, eligible):
    reply = event(rowid=42, timestamp="2026-11-02 12:36:00", timestamp_utc=reply_at,
                  text="Owner reply", is_from_me=True)
    monkeypatch.setattr(personal_context, "conversation_snapshot", lambda: [(reply, "", "signal")])
    descriptor = delivery_effects.for_bundle({"events": [{"class": "urgent", "event": event()}]})
    descriptor = json.loads(json.dumps(descriptor))
    monkeypatch.setenv("SOTTO_TIMEZONE", "Asia/Tokyo")
    original = datetime(2026, 11, 2, 20, 35, 30, tzinfo=timezone.utc).timestamp()
    assert descriptor["valid_until"] == original + delivery_effects.FRESH_NUDGE_VALID_SECONDS
    assert delivery_effects.valid(descriptor["effects"], NOW.timestamp()) is eligible
    assert not delivery_effects.valid(descriptor["effects"], original + 31 * 60)


@pytest.mark.parametrize('zone', ['+00:00', 'America/Los_Angeles', 'Asia/Tokyo'])
@pytest.mark.parametrize('explicit', [False, True])
def test_chronological_funnel_never_sees_later_messages_or_day_end_learning(tmp_path, monkeypatch, zone, explicit):
    from test_run_golden import rg

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', zone)
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    def message(hour, text, **extra):
        clock = {'timestamp_utc': f'2026-08-08T{hour}:00:00Z'} if explicit else {}
        display = f'{int(hour) - 2:02}' if explicit else hour
        return {'handle': '+15555550123', 'timestamp': f'2026-08-08 {display}:00:00', **clock,
                'text': text, '_gid': text, **extra}
    # Deliberately reverse source order. The sent reply is context, not an interruption.
    day = {'name': 'ordered', 'replay_clock': 'day_end', 'inputs': {
        'type': 'evening', 'google': {'emails': [], 'events': []}, 'local': {'imessage': [
            message('18', 'later question'), message('14', 'sent it', is_from_me=True),
            message('09', 'morning ask')]}}}
    observed = []
    learned = tmp_path / 'learned-at-day-end'
    def compose(*args, **kwargs):
        learned.write_text('future knowledge')
        return {'brief_markdown': 'Scripted brief', 'actions': []}
    def triage(payload, **kwargs):
        item = payload['events'][0]
        assert not learned.exists(), 'earlier event saw day-end learning'
        context = personal_context.current_conversation(
            item, candidates=personal_context.conversation_snapshot())
        observed.append((item['text'], [row['text'] for row in context]))
        return {'verdict': 'queue'}
    monkeypatch.setattr(rg.cb, 'compose', compose)
    monkeypatch.setattr(te, 'triage', triage)
    result = rg.replay_day(day, datetime(2026, 8, 8, 23, 59), str(tmp_path), False)
    assert result['exception'] is None
    assert observed == [('morning ask', ['morning ask']),
                        ('later question', ['morning ask', 'sent it', 'later question'])]
    assert learned.exists()


@pytest.mark.parametrize('channel_case', [
    ('imessage', 'handle', 'iMessage'), ('whatsapp', 'contact_jid', 'WhatsApp'),
    ('missed_calls', 'phone', 'phone'),
])
@pytest.mark.parametrize(('explicit', 'included'), [
    (None, True), ('2026-11-17T09:30:00Z', True),
    ('2026-11-17T10:01:00Z', False), ('2026-11-15T09:30:00Z', False), ('invalid', False),
])
def test_contact_activity_uses_bridge_instants(monkeypatch, channel_case, explicit, included):
    import compose_brief as cb

    source, endpoint, channel = channel_case
    monkeypatch.setenv('SOTTO_TIMEZONE', 'Asia/Kolkata')
    now = datetime(2026, 11, 17, 10, tzinfo=timezone.utc)
    phone, email, name = '+12025550140', 'mira@example.test', 'Mira Calder'
    message = {endpoint: phone, 'resolved_name': name, 'name': name,
               'timestamp': '2026-11-17 15:00:00'}
    if explicit is not None:
        message['timestamp_utc'] = explicit
    local = {'contacts': [{'name': name, 'phones': [phone], 'emails': [email]}], source: [message]}
    emails = [{'from': f'{name} <{email}>', 'date': '2026-11-17T09:00:00Z'}]
    activity = cb._detect_escalation(local, emails, now)
    assert bool(activity) is included
    if included:
        assert {entry['channel'] for entry in activity[0]['channels']} == {channel, 'email'}
