"""relationship_pulse.py — weekly losing-touch / waiting-on-you detection from read_local history."""
import importlib.util
import json
import os
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location(
    "relationship_pulse", os.path.join(ROOT, "relationship-pulse", "scripts", "relationship_pulse.py"))
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


def _msg(name, days_ago, from_me, text="hi"):
    ts = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return {"handle": "+1", "resolved_name": name, "is_from_me": from_me,
            "is_group_chat": False, "timestamp": ts, "text": text}


def test_waiting_on_you():
    # A visible direct request earns a conditional reply-review suggestion.
    local = {"contacts": [{"name": "Dhruv", "phones": ["+1"]}],
             "imessage": [_msg("Dhruv", 5, False, "Could you share the Cedar deck?")]}
    out = rp.compute(local, NOW)
    q = [x for x in out["attention_queue"] if x["queue_type"] == "waiting_on_you"]
    assert q and q[0]["display_name"] == "Dhruv" and q[0]["days_waiting"] == 5


def test_losing_touch_when_cadence_widens():
    # Used to talk every ~2 days, now silent 20 days → cadence increasing + 14d+ → losing_touch.
    msgs = []
    for i, d in enumerate([60, 58, 56, 54, 52, 50, 48, 46]):   # tight reciprocal cadence
        msgs.append(_msg("Sarah", d, i % 2 == 0))
    msgs.append(_msg("Sarah", 20, False))         # then a big gap, last contact 20d ago
    local = {"contacts": [{"name": "Sarah", "phones": ["+1"]}], "imessage": msgs}
    out = rp.compute(local, NOW)
    losing = [x for x in out["attention_queue"] if x["queue_type"] == "losing_touch"]
    assert any(x["display_name"] == "Sarah" for x in losing)


def test_unknown_phone_sender_excluded():
    # A raw-phone-named sender (no contact) is not a relationship — excluded.
    local = {"imessage": [_msg("+15551234567", 5, False)]}
    out = rp.compute(local, NOW)
    assert out["attention_queue"] == []


def test_healthy_when_nothing_flagged():
    local = {"contacts": [{"name": "Bob", "phones": ["+1"]}],
             "imessage": [_msg("Bob", 1, True)]}   # you spoke yesterday, you sent last
    out = rp.compute(local, NOW, absence_complete=True)
    assert out["attention_queue"] == []
    assert "No clear follow-up" in out["pulse_markdown"]
    assert "\n" not in out["pulse_markdown"]       # the weekly empty state stays ONE calm line


def test_pulse_markdown_is_chat_formatted_with_no_opener():
    """Sprint 0 §3/§4: pulse_markdown is delivered verbatim by the skill, so it must already be
    chat-ready (single-asterisk bold, no raw **markdown**) and open straight into the sections —
    no "Here's your weekly relationship pulse" meta-announcement."""
    local = {"contacts": [{"name": "Dhruv", "phones": ["+1"]}],
             "imessage": [_msg("Dhruv", 5, False, "Could you share the Cedar deck?")]}
    out = rp.compute(local, NOW)
    md = out["pulse_markdown"]
    assert "**" not in md and "## " not in md and "<!--" not in md
    assert md.startswith("*Worth a reply review*")       # sections just start
    assert "*Dhruv*" in md                         # names bolded chat-style
    assert "relationship pulse" not in md.lower()  # the meta-announcement opener is gone


def _losing_msgs(name, handle):
    msgs = []
    for i, d in enumerate([60, 58, 56, 54, 52, 50, 48, 46]):
        m = _msg(name, d, i % 2 == 0)
        m["handle"] = handle
        msgs.append(m)
    last = _msg(name, 20, False)
    last["handle"] = handle
    msgs.append(last)
    return msgs


def test_graph_research_never_changes_rank_or_leaks_talking_points(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    people = os.path.join(str(tmp_path), "knowledge", "people")
    os.makedirs(people, exist_ok=True)
    with open(os.path.join(people, "sarah.md"), "w") as f:
        f.write("---\nschema: 1\ncanonical_id: c_1\nname: Sarah\ncompany: Acme\n"
                "facts:\n  f_1:\n    text: leads platform\n    type: context\n    status: active\n"
                "    seen: 2\n    conf: 0.9\n    source: brief\n    source_ref: ''\n"
                "    first: '2026-01-01'\n    last: '2026-06-01'\n---\n"
                "\n## Talking Points\n- ask about the Series B\n")
    local = {"contacts": [{"name": "Sarah", "phones": ["+1"]}, {"name": "Tom", "phones": ["+2"]}],
             "imessage": _losing_msgs("Sarah", "+1") + _losing_msgs("Tom", "+2")}
    out = rp.compute(local, NOW)
    losing = [x for x in out["attention_queue"] if x["queue_type"] == "losing_touch"]
    names = [x["display_name"] for x in losing]
    assert set(names) == {"Sarah", "Tom"}
    sarah = next(x for x in losing if x["display_name"] == "Sarah")
    assert sarah["priority"] == next(x for x in losing if x["display_name"] == "Tom")["priority"]
    assert sarah["graph_context"] == {"company": "Acme"}
    assert "talking_point" not in sarah["graph_context"] and "fact" not in sarah["graph_context"]
    assert "Acme" in out["pulse_markdown"]
    tom = next(x for x in losing if x["display_name"] == "Tom")
    assert "graph_context" not in tom                         # untracked → no fabricated context
    del os.environ["SOTTO_DATA"]


def test_persist_writes_state(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    result = {"attention_queue": [{"display_name": "X", "queue_type": "losing_touch"}],
              "relationship_insights": [{"display_name": "X", "insight_type": "gone_silent"}]}
    rp._persist_state(result)
    import json
    state = json.load(open(os.path.join(str(tmp_path), "knowledge", "relationship_state.json")))
    assert state["attention_queue"][0]["display_name"] == "X"


def test_persist_preserves_unknown_candidate_metadata(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    rp._persist_state({"attention_queue": [], "relationship_insights": [], "history": {
        "c_alex": {"name": "Alex", "last_contact": "2026-06-24", "interactions": 2,
                   "importance_evidence": {}, "engagement": {}}}})
    path = os.path.join(str(tmp_path), "knowledge", "relationship_state.json")
    import json
    state = json.load(open(path))
    state["history"]["c_alex"]["candidate_offer"] = {"accepted_at": "2026-06-25T12:00:00Z"}
    with open(path, "w") as f:
        json.dump(state, f)
    rp._persist_state({"attention_queue": [], "relationship_insights": [], "history": {
        "c_alex": {"name": "Alex", "last_contact": "2026-06-25", "interactions": 3,
                   "importance_evidence": {}, "engagement": {}}}})
    assert json.load(open(path))["history"]["c_alex"]["candidate_offer"]["accepted_at"]


def test_persist_merges_reply_samples_from_separate_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    def engagement(start_days, finish_days, first_id):
        return rp.engagement_signals({"events": [
            {"at": NOW-timedelta(days=start_days), "from_me": True,
             "conversation_id": str(first_id), "native_id": str(first_id)},
            {"at": NOW-timedelta(days=finish_days), "from_me": False,
             "conversation_id": str(first_id), "native_id": str(first_id + 1)}]}, {}, NOW)
    base = {"name": "Alex", "last_contact": NOW.date().isoformat(), "interactions": 2,
            "importance_evidence": {}, "channels": {}}
    rp._persist_state({"attention_queue": [], "relationship_insights": [], "history": {
        "c_alex": {**base, "engagement": engagement(8, 7, 1)}}}, now=NOW)
    rp._persist_state({"attention_queue": [], "relationship_insights": [], "history": {
        "c_alex": {**base, "engagement": engagement(4, 2, 3)}}}, now=NOW)
    saved = json.load(open(tmp_path / "knowledge/relationship_state.json"))["history"]["c_alex"]
    assert len(saved["engagement"]["reply_samples"]) == 2
    assert saved["engagement"]["counterpart_reply_lag_days"] == 2


def test_only_ended_granola_attendance_counts_as_a_held_meeting():
    contact = {"name": "Alex", "emails": ["alex@example.com"]}
    meetings = [{"meeting_id": f"m{i}", "end": (NOW-timedelta(days=d)).isoformat(),
                 "attendee_emails": ["alex@example.com"]} for i, d in enumerate((1, 8, 15))]
    meetings.append({"meeting_id": "future", "end": (NOW+timedelta(days=1)).isoformat(),
                     "attendee_emails": ["alex@example.com"]})
    meetings.append({"meeting_id": "naive-past", "end": (NOW-timedelta(days=22)).replace(
        tzinfo=None).isoformat(), "attendee_emails": ["alex@example.com"]})
    meetings.append({"meeting_id": "unended", "date": (NOW-timedelta(days=29)).date().isoformat(),
                     "attendee_emails": ["alex@example.com"]})
    out = rp.compute({"contacts": [contact], "granola_meetings": meetings}, NOW)
    row = next(v for v in out["history"].values() if v["name"] == "Alex")
    assert row["importance"]["tier"] == "regular"
    assert row["importance"]["held_meetings"] == 4


def test_owner_comes_from_the_canonical_chain_not_the_env_var_alone(monkeypatch):
    """With SOTTO_USER_EMAIL unset (the normal managed case) the owner is the connected Google
    account; reading the env var alone left the owner in every 1:1 and dropped every meeting."""
    monkeypatch.delenv("SOTTO_USER_EMAIL", raising=False)
    monkeypatch.setattr(rp, "configured_user_email", lambda: "me@example.com")
    contacts = [{"name": "Alex", "emails": ["alex@example.com"]}]
    held = {"meeting_id": "one-on-one", "end": (NOW-timedelta(days=1)).isoformat(),
            "attendee_emails": ["me@example.com", "alex@example.com"]}
    out = rp._held_meetings({"contacts": contacts, "granola_meetings": [held]}, NOW)
    assert len(out) == 1
    monkeypatch.setattr(rp, "configured_user_email", lambda: "")
    assert rp._held_meetings({"contacts": contacts, "granola_meetings": [held]}, NOW) == {}


def test_group_meetings_and_owner_address_do_not_become_relationship_evidence(monkeypatch):
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    contacts = [{"name": "Alex", "emails": ["alex@example.com"]}]
    group = {"meeting_id": "standup", "end": (NOW-timedelta(days=1)).isoformat(),
             "attendee_emails": ["me@example.com", "alex@example.com", "b@example.com"]}
    owner_omitted = {"meeting_id": "standup-unknown-owner",
                     "end": (NOW-timedelta(days=2)).isoformat(),
                     "attendee_emails": ["alex@example.com", "b@example.com"]}
    owner_only = {"meeting_id": "solo", "end": (NOW-timedelta(days=1)).isoformat(),
                  "attendee_emails": ["me@example.com"]}
    assert rp._held_meetings({"contacts": contacts,
                              "granola_meetings": [group, owner_omitted, owner_only]}, NOW) == {}


def test_daily_brief_merges_persisted_relationship_state(tmp_path):
    # The pulse-written state should flow into the brief's attention-queue section.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    rp._persist_state({"attention_queue": [{"display_name": "Marcus", "queue_type": "losing_touch",
                                            "reason": "Communication declining"}],
                       "relationship_insights": []})
    import compose_brief as cb
    prompt = cb.build_prompt(cb._load_prompt(), {"type": "morning", "google": {"events": []}, "local": {}})
    assert "Marcus" in prompt
    del os.environ["SOTTO_DATA"]


def test_outgoing_call_counts_as_from_you():
    # Processed call dicts carry direction ("outgoing"/"incoming"), not is_outgoing — reading the
    # wrong key made every outgoing call inbound, flagging people the USER called as waiting_on_you.
    ts = (NOW - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    local = {"recent_calls": [{"name": "Priya", "timestamp": ts, "direction": "outgoing",
                               "call_type": "phone"}]}
    people = rp._interactions_by_contact(local)
    assert len(people["Priya"]["from_me"]) == 1 and not people["Priya"]["from_them"]
    # End-to-end: they texted 8d ago, you called them back 5d ago → NOT waiting on you.
    # (compute re-derives recent_calls from the raw `calls` via resolve_contact_names.)
    local2 = {"contacts": [{"name": "Priya", "phones": ["+14155550101"]}],
              "imessage": [{"handle": "+14155550101", "is_from_me": False, "is_group_chat": False,
                            "timestamp": (NOW - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S"),
                            "text": "call me?"}],
              "calls": [{"phone": "+14155550101", "is_outgoing": True, "is_answered": True,
                         "timestamp": ts, "call_type": "phone"}]}
    out = rp.compute(local2, NOW)
    assert not [x for x in out["attention_queue"] if x["queue_type"] == "waiting_on_you"]


def test_missed_call_counts_as_inbound():
    # Missed calls carry no direction — they are their side reaching out.
    ts = (NOW - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    local = {"missed_calls": [{"name": "Priya", "timestamp": ts}]}
    people = rp._interactions_by_contact(local)
    assert len(people["Priya"]["from_them"]) == 1 and not people["Priya"]["from_me"]


def test_unknown_sentinel_never_becomes_a_contact():
    # "Unknown" is the resolver's sentinel for unresolvable senders (e.g. @lid) — merging them
    # into one fake contact fabricated waiting/cadence signals for a person who doesn't exist.
    ts = (NOW - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    local = {"imessage": [_msg("Unknown", 5, False)],
             "recent_calls": [{"name": "Unknown", "timestamp": ts, "direction": "incoming",
                               "call_type": "phone"}]}
    assert "Unknown" not in rp._interactions_by_contact(local)


def test_lapsed_requires_outside_window_and_complete_absence_coverage():
    local = {'contacts':[{'name':'Current','phones':['+1']}], 'imessage':[_msg('Current',1,True)]}
    evidence = {'sent_days':['2026-04-01','2026-04-02','2026-04-03'],
                'received_days':['2026-04-01','2026-04-02','2026-04-03']}
    def history(day):
        return {'Historic':{'name':'Historic','last_contact':day,'importance_evidence':evidence}}
    for day in ('2026-06-18', '2026-05-14', 'invalid'):
        assert not rp.compute(local, NOW, history(day), absence_complete=True)['lapsed']
    assert rp.compute(local, NOW, history('2026-05-13'), absence_complete=True)['lapsed']
    for complete in (False,):
        result = rp.compute(local, NOW, history('2026-05-13'), absence_complete=complete)
        assert not result['lapsed'] and 'Some history may be missing' in result['pulse_markdown']
    # Ordinary waiting-on-you classification does not depend on absence inference.
    waiting = {'contacts':[{'name':'Current','phones':['+1']}], 'imessage':[_msg('Current',5,False,"Could you send the agenda?")]}
    assert rp.compute(waiting, NOW, history('2026-05-13'))['attention_queue'][0]['queue_type'] == 'waiting_on_you'


def test_healthy_weekly_pulse_stays_one_line_when_nothing_was_withheld():
    """The missing-history line is a claim that a lapse was left out; a healthy week with nothing
    withheld is the original single always-speak line, whatever the coverage."""
    local = {'contacts': [{'name': 'Bob', 'phones': ['+1']}], 'imessage': [_msg('Bob', 1, True)]}
    for coverage in (False, True, [], ['imessage']):
        out = rp.compute(local, NOW, absence_complete=coverage)
        assert '\n' not in out['pulse_markdown'] and 'No clear follow-up' in out['pulse_markdown']


def _email(days=5, **fields):
    return {"from": "Dana <dana@example.com>", "to": "me@example.com",
            "date": (NOW - timedelta(days=days)).isoformat(), "threadId": "cedar",
            "subject": "Cedar deck", "snippet": "Could you share the Cedar deck?", **fields}


def test_reply_review_has_grounded_context_and_conditional_action(monkeypatch):
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    out = rp.compute({"contacts": [{"name": "Dana", "emails": ["dana@example.com"]}],
                      "emails": [_email()]}, NOW)
    assert len(out["attention_queue"]) == 1
    text = out["pulse_markdown"]
    for expected in ("Dana", "Email", "Jun 20", "Cedar deck",
                     "No later response found in the available history", "review and reply if still relevant"):
        assert expected in text
    assert "waiting on you" not in text.lower() and "unanswered" not in text.lower()
    assert 'Could you share the Cedar deck?' not in text  # subject supplies the topic once
    assert out['attention_queue'][0]['context']['excerpt'] == 'Could you share the Cedar deck?'
    # The daily brief keeps the conditional meaning and the channel/date evidence of the persisted
    # queue, never the message wording (that stays in the attended reply).
    from render_local import _format_attention_queue
    rendered = _format_attention_queue(out)
    assert "does not establish an obligation" in rendered
    assert "Cedar" not in rendered and "2026-06-20" in rendered and '"channel": "email"' in rendered
    assert "are WAITING for your reply" not in rendered


def test_last_inbound_without_request_does_not_create_a_debt(monkeypatch):
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    for message in (_email(snippet="Thanks, received!"), _email(snippet=""),
                    _email(snippet="Thanks!\n> Could you share the Cedar deck?"),
                    _email(to="other@example.com", cc="me@example.com")):
        out = rp.compute({"contacts": [{"name": "Dana", "emails": ["dana@example.com"]}],
                          "emails": [message]}, NOW)
        assert not out["attention_queue"]
        assert "available relationship history" in out["pulse_markdown"]
    local = {"contacts": [{"name": "Dana", "phones": ["+1"]}],
             "imessage": [_msg("Dana", 5, False)]}
    assert not rp.compute(local, NOW)["attention_queue"]


def test_newsletter_and_bulk_mail_do_not_create_relationship_signals(monkeypatch):
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    for fields in ({"labelIds": ["CATEGORY_PROMOTIONS"]},
                   {"snippet": "Could you share this newsletter? Unsubscribe here."},
                   {"payload": {"headers": [{"name": "List-Unsubscribe", "value": "https://example.com"}]}}):
        local = {"contacts": [{"name": "Dana", "emails": ["dana@example.com"]}],
                 "emails": [_email(**fields)]}
        people = rp._interactions_by_contact(local)
        assert people and not next(iter(people.values()))["engaged_dates"]
        assert not rp.compute(local, NOW)["attention_queue"]


def test_bulk_labeled_owner_reply_still_clears_earlier_request(monkeypatch):
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    local = {"contacts": [{"name": "Dana", "emails": ["dana@example.com"]}], "emails": [
        _email(), _email(days=2, isSent=True, labelIds=["CATEGORY_FORUMS"],
                        snippet="Here you go. Unsubscribe from the old feed.",
                        **{"from": "me@example.com", "to": "dana@example.com"})]}
    assert not rp.compute(local, NOW)["attention_queue"]


def test_bulk_conversations_cannot_teach_reply_lag_or_attention(monkeypatch):
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    emails = [_email(days=days, id=f"bulk-{i}", labelIds=["CATEGORY_PROMOTIONS"], isSent=i % 2 == 0,
                     **({"from": "me@example.com", "to": "dana@example.com"} if i % 2 == 0 else {}))
              for i, days in enumerate((15, 13, 11, 9, 7, 5))]
    out = rp.compute({"contacts": [{"name": "Dana", "emails": ["dana@example.com"]}],
                      "emails": emails}, NOW)
    row = next(iter(out["history"].values()))
    assert not row["engagement"]
    assert not row["importance_evidence"]["active_days"]
    assert not out["attention_queue"]


def test_sustained_relationship_ranks_above_older_one_off_and_caps_suggestions(monkeypatch):
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    contacts = [{"name": "Dana", "emails": ["dana@example.com"]}]
    emails = [_email(days=d, isSent=i % 2 == 0,
                     **({"from": "me@example.com", "to": "dana@example.com"} if i % 2 == 0 else {}))
              for i, d in enumerate((38, 36, 29, 22, 15, 8, 5, 4))]
    for i in range(8):
        contacts.append({"name": f"One-off {i}", "emails": [f"person{i}@example.com"]})
        emails.append(_email(days=13, **{"from": f"person{i}@example.com"}))
    out = rp.compute({"contacts": contacts, "emails": emails}, NOW)
    assert len(out["attention_queue"]) == 5
    assert out["attention_queue"][0]["display_name"] == "Dana"
    assert "two-way activity across several weeks" in out["attention_queue"][0]["importance"]["reason"]


def test_later_owner_reply_clears_reply_suggestion(monkeypatch):
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    local = {"contacts": [{"name": "Dana", "emails": ["dana@example.com"]}],
             "emails": [_email(), _email(days=2, isSent=True, **{
                 "from": "me@example.com", "to": "dana@example.com"})]}
    assert not rp.compute(local, NOW)["attention_queue"]


def test_missed_call_suggests_conditional_callback_with_source():
    local = {"contacts": [{"name": "Priya", "phones": ["+1"]}],
             "calls": [{"phone": "+1", "timestamp": (NOW-timedelta(days=5)).isoformat(),
                        "is_outgoing": False, "is_answered": False, "call_type": "phone"}]}
    out = rp.compute(local, NOW)
    assert "Missed call" in out["pulse_markdown"]
    assert "consider calling back if still relevant" in out["pulse_markdown"]


def test_observed_request_is_reused_and_later_owner_touch_suppresses_it(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    ask = {'contacts': [{'name': 'Dana', 'phones': ['+1']}],
           'imessage': [_msg('Dana', 5, False, 'Could you send the Cedar deck?')]}
    rp.observe_history(ask, now=NOW)
    stored = rp._load_history()
    assert 'Earlier request on Jun 20' in rp.compute({}, NOW, stored)['pulse_markdown']
    assert 'Cedar' not in json.dumps(stored)
    response = {'contacts': ask['contacts'], 'imessage': [_msg('Dana', 1, True, 'Sent it') ]}
    rp.observe_history(response, now=NOW)
    stored = rp._load_history()
    assert 'Earlier request' not in rp.compute({}, NOW, stored)['pulse_markdown']
    assert all(row['last_from_you'] for row in stored.values())


def test_name_to_canonical_identity_migration_keeps_saved_ask_with_fresh_response():
    old = {'Dana': {'name': 'Dana', 'last_contact': (NOW - timedelta(days=5)).date().isoformat(),
        'channels': {'imessage': (NOW - timedelta(days=5)).date().isoformat()},
        'last_from_them': (NOW - timedelta(days=5)).isoformat(),
        'latest_inbound': {'at': (NOW - timedelta(days=5)).isoformat(), 'channel': 'imessage',
                           'text': 'Could you send the Cedar deck?'}}}
    fresh = {'contacts': [{'name': 'Dana', 'phones': ['+1']}],
             'imessage': [{**_msg('Dana', 1, True, 'Sent it'), 'canonical_id': 'c_abc123'}]}
    result = rp.compute(fresh, NOW, old)
    assert not result['attention_queue']
    assert list(result['history']) == ['c_abc123']
    assert result['history']['c_abc123']['last_from_you'] == (NOW - timedelta(days=1)).isoformat()


@pytest.mark.parametrize('graph_ids', [('c_abc123',), ('c_abc123', 'c_other12')])
def test_ambiguous_global_graph_name_never_borrows_legacy_request_or_importance(tmp_path, monkeypatch, graph_ids):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    kg = rp._knowledge()
    folder = Path(kg.people_dir())
    folder.mkdir(parents=True, exist_ok=True)
    for cid in graph_ids:
        (folder / (cid + '.md')).write_text(kg.serialize_person_file(kg.PersonFile(
            name='Dana', canonical_id=cid, identifiers=[cid + '@example.com'])))
    days = [(NOW - timedelta(days=day)).date().isoformat() for day in range(5, 20)]
    old = {'Dana': {'name': 'Dana', 'last_contact': days[0], 'channels': {'imessage': days[0]},
        'importance_evidence': {'active_days': days, 'sent_days': days, 'received_days': days},
        'last_from_them': (NOW - timedelta(days=5)).isoformat(),
        'latest_inbound': {'at': (NOW - timedelta(days=5)).isoformat(), 'channel': 'imessage',
                           'text': 'Could you send the Cedar deck?'}}}
    fresh = {'contacts': [{'name': 'Dana', 'phones': ['+1']}],
             'imessage': [{**_msg('Dana', 1, True, 'Sent it'), 'canonical_id': 'c_abc123'}]}
    result = rp.compute(fresh, NOW, old)
    assert not result['attention_queue'] and 'Cedar deck' not in result['pulse_markdown']
    assert result['history']['c_abc123']['importance']['tier'] == 'regular'


def test_commitment_with_relative_wording_without_date_is_an_earlier_quote():
    result = rp.with_commitments({'attention_queue': [], 'pulse_markdown': ''}, {'you_owe': [
        {'name': 'Dana', 'what': 'Send it tomorrow', 'channel': 'imessage'}]})
    assert 'Earlier request (date unknown): “Send it tomorrow”' in result['pulse_markdown']
    assert 'Saved wording' not in result['pulse_markdown']


@pytest.mark.parametrize('email_position', ['primary', 'secondary'])
def test_shared_contacts_email_never_allows_legacy_name_history_to_borrow_graph_identity(
        tmp_path, monkeypatch, email_position):
    _pulse_person(tmp_path, monkeypatch, 'c_abc123', 'Dana Reed', ['shared@example.com'])
    local = {'contacts': [
        {'name': 'Dana Reed', 'emails': ['a@example.com', 'shared@example.com'], 'phones': ['+15555550111']},
        {'name': 'Dana Reed', 'emails': ['b@example.com', 'shared@example.com'], 'phones': ['+15555550222']}],
        'contact_index': [{'display_name': 'Dana Reed', 'canonical_id': 'c_abc123',
                           'identifiers': ['shared@example.com']}],
        'imessage': [{**_msg('Dana Reed', 1, True, 'Sent it'), 'handle': '+15555550111',
                      'canonical_id': 'c_abc123'}]}
    if email_position == 'primary':
        for card in local['contacts']:
            card['emails'].reverse()
    days = [(NOW - timedelta(days=day)).date().isoformat() for day in range(5, 20)]
    history = {'Dana Reed': {'name': 'Dana Reed', 'last_contact': days[0],
        'channels': {'imessage': days[0]},
        'importance_evidence': {'active_days': days, 'sent_days': days, 'received_days': days},
        'last_from_them': (NOW - timedelta(days=5)).isoformat(),
        'latest_inbound': {'at': (NOW - timedelta(days=5)).isoformat(), 'channel': 'imessage',
                           'text': 'Could you send the Cedar deck?'}}}
    assert 'dana reed' in rp._ambiguous_history_names(local, rp._knowledge().build_people_index())
    result = rp.compute(local, NOW, history)
    assert not result['attention_queue']
    assert result['history']['c_abc123']['importance']['tier'] == 'regular'
    assert not result['history']['c_abc123'].get('latest_inbound')


def _pulse_person(tmp_path, monkeypatch, cid, name, identifiers):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    kg = rp._knowledge()
    folder = Path(kg.people_dir())
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (cid + '.md')).write_text(kg.serialize_person_file(kg.PersonFile(
        name=name, canonical_id=cid, identifiers=identifiers)))


def test_compact_commitments_resolve_both_channels_and_count_different_requests(tmp_path, monkeypatch):
    from copy import deepcopy
    _pulse_person(tmp_path, monkeypatch, 'c_abc123', 'Austin', ['+15555550123', 'austin@example.com'])
    rows = [dict(name='Austin', identifier='+15555550123', channel='imessage',
                 what='Connect today or tomorrow about Project Cedar', created_at='2026-10-04', deadline='Tonight'),
            dict(name='Austin', identifier='austin@example.com', channel='gmail',
                 what='Free for a call about Project Cedar following the intro', created_at='2026-10-05'),
            dict(name='Austin', identifier='austin@example.com', channel='gmail',
                 what='Connect today or tomorrow about Project Cedar', created_at='2026-10-04')]
    before = deepcopy(rows)
    result = rp.with_commitments({'attention_queue': []}, {'you_owe': rows})
    text = result['pulse_markdown']
    assert text.count('Austin') == 1 and '; 1 other open item' in text
    assert 'imessage' not in text and 'gmail' not in text and 'due Tonight' not in text
    assert 'From Oct 4: “' in text and 'today or tomorrow' in text
    assert rows == before


def test_compact_commitments_keep_unknown_ambiguous_people_and_directions_separate(tmp_path, monkeypatch):
    _pulse_person(tmp_path, monkeypatch, 'c_abc123', 'Dana', ['shared@example.com', 'dana1@example.com'])
    _pulse_person(tmp_path, monkeypatch, 'c_other12', 'Dana', ['shared@example.com', 'dana2@example.com'])
    rows = [dict(name='Dana', identifier=identifier, channel='gmail', what='Send the deck')
            for identifier in ('dana1@example.com', 'dana2@example.com', 'shared@example.com', 'unknown@example.com')]
    result = rp.with_commitments({'attention_queue': []}, {'you_owe': rows})
    assert result['pulse_markdown'].count('Dana') == 4
    result = rp.with_commitments({'attention_queue': []},
        {'you_owe': [rows[0]], 'waiting_on_them': [rows[0]]})
    assert result['pulse_markdown'].count('Dana') == 2
    assert 'Waiting for them: Send the deck' in result['pulse_markdown']
    assert 'Your follow-up' not in result['pulse_markdown']


@pytest.mark.parametrize('deadline,shown', [('Tonight', False), ('tomorrow', False),
    ('2026-02-30', False), ('2026-10-11', True)])
def test_compact_commitments_show_only_calendar_date_deadlines(deadline, shown):
    result = rp.with_commitments({'attention_queue': []}, {'you_owe': [
        {'name': 'Dana', 'what': 'Send the deck', 'deadline': deadline}]})
    assert ('due ' in result['pulse_markdown']) == shown


def test_compact_commitments_count_same_request_with_distinct_calendar_deadlines(tmp_path, monkeypatch):
    _pulse_person(tmp_path, monkeypatch, 'c_abc123', 'Austin', ['austin@example.com'])
    rows = [dict(name='Austin', identifier='austin@example.com', channel='gmail',
                 what='Send weekly report', deadline=deadline)
            for deadline in ('2026-10-12', '2026-10-19', '2026-10-12')]
    text = rp.with_commitments({'attention_queue': []}, {'you_owe': rows})['pulse_markdown']
    assert text.count('Austin') == 1 and '; 1 other open item' in text
    assert 'due Oct 12' in text


def _commitment_text(rows, queue=()):
    return rp.with_commitments({'attention_queue': list(queue)}, {'you_owe': rows})['pulse_markdown']


@pytest.mark.parametrize('graph_ids,row_id', [
    (['+15555550123', 'austin@example.com'], '15555550123@s.whatsapp.net'),   # P1: WhatsApp JID row
    (['15555550123@s.whatsapp.net', 'austin@example.com'], '+1 (555) 555-0123'),  # P2: graph keeps the JID
])
def test_compact_commitments_resolve_whatsapp_jid_and_phone_with_the_graph_normalizer(
        tmp_path, monkeypatch, graph_ids, row_id):
    _pulse_person(tmp_path, monkeypatch, 'c_abc123', 'Austin', graph_ids)
    text = _commitment_text([
        dict(name='Austin', identifier='austin@example.com', channel='gmail', what='Send deck'),
        dict(name='Austin', identifier=row_id, channel='whatsapp', what='Call re Cedar')])
    assert text.count('Austin') == 1 and '; 1 other open item' in text


def test_compact_commitments_never_trust_a_stored_id_the_identifier_does_not_prove(tmp_path, monkeypatch):
    # P5: a stored canonical ID with an identifier the graph does not hold stays a separate person.
    _pulse_person(tmp_path, monkeypatch, 'c_abc123', 'John Smith', ['+15555550123'])
    text = _commitment_text([
        dict(name='John Smith', identifier='+15555550123', channel='imessage', what='Send deck', canonical_id='c_abc123'),
        dict(name='John Smith', identifier='+14155550999', channel='imessage', what='Pay invoice', canonical_id='c_abc123'),
        dict(name='John Smith', identifier='', channel='manual', what='Book the venue', canonical_id='c_abc123')])
    assert text.count('John Smith') == 2 and 'Pay invoice' in text and '; 1 other open item' in text


def test_commitments_past_the_cap_are_counted_never_silently_dropped():
    # P4: seven distinct people, five shown, the rest counted.
    text = _commitment_text([dict(name=f'P{i}', identifier=f'p{i}@x.com', channel='gmail', what=f'Task {i}')
                             for i in range(7)])
    assert all(f'P{i}' in text for i in range(5)) and 'P5' not in text
    assert '2 other items are on your list' in text


def test_attention_suggestions_are_filtered_by_proven_identity_not_display_name(tmp_path, monkeypatch):
    _pulse_person(tmp_path, monkeypatch, 'c_abc123', 'Dana', ['dana1@example.com'])
    row = dict(name='Dana', identifier='dana1@example.com', channel='gmail', what='Send deck', canonical_id='c_abc123')
    other = {'display_name': 'Dana', 'canonical_id': 'c_other12', 'queue_type': 'waiting_on_you',
             'reason': 'Missed call 4 days ago', 'context': {'channel': 'calls', 'date': '2026-10-06', 'missed_call': True}}
    unknown = {**other, 'canonical_id': ''}
    same = {**other, 'canonical_id': 'c_abc123'}
    assert 'Missed call' in _commitment_text([row], [other])
    assert 'Missed call' in _commitment_text([row], [unknown])
    assert 'Missed call' not in _commitment_text([row], [same])


def test_pulse_commitment_dates_relative_wording_on_the_owner_calendar_day():
    # 08:00 in Tokyo is still the previous UTC day; the owner's calendar decides the label.
    owner_now = datetime(2026, 10, 10, 8, 0, tzinfo=timezone(timedelta(hours=9)))
    loops = {'you_owe': [{'name': 'Kai', 'what': 'Send the deck today', 'channel': 'imessage',
                          'created_at': '2026-10-10T07:30:00+09:00', 'age_days': 0, 'overdue': False}],
             'waiting_on_them': []}
    text = rp.with_commitments({'attention_queue': [], 'pulse_markdown': ''}, loops, now=owner_now)['pulse_markdown']
    assert 'From Oct 10: “Send the deck today”' in text


def test_readable_commitments_attribute_historical_words_and_human_deadlines_without_mutation():
    from copy import deepcopy
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    row = {'name': 'Dana', 'what': 'Check yesterday, send today, call tomorrow, confirm tonight',
           'created_at': '2024-10-04', 'deadline': '2024-10-05', 'overdue': True, 'channel': 'gmail'}
    before = deepcopy(row)
    text = rp.with_commitments({'attention_queue': []}, {'you_owe': [row]}, now)['pulse_markdown']
    # Relative words stay quoted under the recording date; an ask re-captured later keeps its
    # first created_at, so "tomorrow" is never resolved into a date the record cannot prove.
    assert 'From Oct 4, 2024: “Check yesterday, send today, call tomorrow, confirm tonight”' in text
    assert 'the next day' not in text and '(was due Oct 5, 2024)' in text
    assert all(word not in text for word in ('gmail', 'Your follow-up'))
    assert row == before


def test_compact_text_does_not_cut_a_date_or_word():
    summary = 'Review ' + 'Cedar material ' * 8 + '2026-10-12 and then confirm'
    text = rp._compact_text(summary)
    assert text.endswith('2026-10-12…') and len(text) <= rp.PULSE_ITEM_CHARS + 1


def test_readable_commitments_preserve_lapse_coverage_warning():
    result = rp.with_commitments({'attention_queue': [], 'lapse_withheld': True},
                                {'you_owe': [{'name': 'Dana', 'what': 'Send the deck.'}]})
    assert '- *Dana* - Send the deck.' in result['pulse_markdown']
    assert 'deck..' not in result['pulse_markdown']
    assert result['pulse_markdown'].endswith(rp.LAPSE_DISCLAIMER)


@pytest.mark.parametrize('ask', [None, '', 'Respond about Project Cedar'])
def test_readable_commitments_prefer_precise_ask_and_fall_back_to_summary(ask):
    from copy import deepcopy
    summary = 'After the introduction and multiple pokes, Austin is waiting for a response. ' * 3
    row = {'name': 'Austin', 'what': summary, 'created_at': '2026-10-04'}
    if ask is not None:
        row['ask'] = ask
    before = deepcopy(row)
    text = _commitment_text([row])
    if ask:
        assert 'Respond about Project Cedar' in text and 'pokes' not in text
    else:
        assert 'After the introduction' in text
    assert row == before


def test_readable_optional_birthday_uses_normalized_courtesy_instead_of_original_ask():
    text = _commitment_text([{'name': 'Maya', 'what': 'Optional birthday greeting',
                             'ask': 'Send Maya a belated birthday greeting', 'birthday_courtesy': True}])
    assert 'Optional birthday greeting (Optional, no urgency)' in text
    assert 'belated' not in text


def test_compact_commitments_count_distinct_precise_asks_even_with_the_same_summary(tmp_path, monkeypatch):
    _pulse_person(tmp_path, monkeypatch, 'c_abc123', 'Austin', ['austin@example.com'])
    text = _commitment_text([dict(name='Austin', identifier='austin@example.com', what='Intro follow-up', ask=ask)
                             for ask in ('Respond about Project Cedar', 'Send the packet', 'Send the packet')])
    assert text.count('Austin') == 1 and '; 1 other open item' in text
    assert 'Respond about Project Cedar' in text


def test_pulse_offers_one_reviewable_draft_not_a_send_or_birthday_task():
    rows = [{'name': 'Alex Stone', 'identifier': 'alex@example.test', 'ask': 'Reply about the Cedar deal',
             'what': 'Long origin summary'},
            {'name': 'Dana Reed', 'identifier': 'dana@example.test', 'ask': 'Confirm meeting time'}]
    text = _commitment_text(rows)
    assert text.count('I can draft a reply to Alex Stone.') == 1
    assert 'say “draft a reply' not in text
    rows[0]['birthday_courtesy'] = True
    rows[1]['group_id'] = 'group'
    assert 'I can draft a reply' not in _commitment_text(rows)


def test_pulse_draft_offer_does_not_target_an_ambiguous_name_hidden_beyond_display_limit():
    rows = [{'name': 'Alex Stone', 'identifier': 'alex@example.test', 'ask': 'Reply about Cedar'}]
    rows += [{'name': f'Person {i}', 'what': 'Review the request'} for i in range(rp.MAX_QUEUE)]
    rows += [{'name': 'Alex Stone', 'identifier': 'other@example.test', 'ask': 'Confirm the call'}]
    assert 'I can draft a reply' not in _commitment_text(rows)


def test_unpadded_created_at_renders_instead_of_failing_the_pulse():
    text = _commitment_text([{'name': 'Dana', 'what': 'Send the deck tomorrow', 'created_at': '2026-10-4'}])
    assert '“Send the deck tomorrow”' in text


def test_verbatim_request_excerpt_stays_quoted():
    line = rp._render([{'display_name': 'Dana', 'queue_type': 'waiting_on_you', 'reason': 'r',
                        'context': {'channel': 'email', 'date': '2026-06-20',
                                    'excerpt': 'Can you send me your login so I can fix it'}}])
    assert '“Can you send me your login so I can fix it”' in line


def test_two_tasks_sharing_a_short_ask_are_counted_and_get_no_offer(tmp_path, monkeypatch):
    _pulse_person(tmp_path, monkeypatch, 'c_abc123', 'Austin Park', ['austin@example.com'])
    text = _commitment_text([
        dict(name='Austin Park', identifier='austin@example.com', ask='Reply to Austin',
             what='Austin needs your sign-off on the Cedar term sheet'),
        dict(name='Austin Park', identifier='austin@example.com', ask='Reply to Austin',
             what='Austin is hosting the board dinner and needs a headcount')])
    assert '; 1 other open item' in text and 'I can draft a reply' not in text


def test_draft_offer_counts_every_display_name_in_a_group(tmp_path, monkeypatch):
    _pulse_person(tmp_path, monkeypatch, 'c_aaa111', 'Sam Lee', ['sam@a.com', '+15550001111'])
    text = _commitment_text([
        dict(name='Sam Lee', identifier='sam@a.com', ask='Send the deck'),
        dict(name='Sam', identifier='+15550001111', ask='Pay invoice'),
        dict(name='Sam', identifier='sam@b.com', ask='Confirm the call')])
    assert 'draft a reply to Sam”' not in text


@pytest.mark.parametrize('identifier', ['120363012345678901@g.us', 'chat123456789012345678',
                                        'iMessage;+;chat123'])
def test_draft_offer_never_targets_a_group_thread_without_group_id(identifier):
    text = _commitment_text([{'name': 'Family Chat', 'identifier': identifier, 'ask': 'Confirm Saturday dinner'}])
    assert 'I can draft a reply' not in text


def test_quoted_wording_keeps_its_own_end_punctuation():
    text = _commitment_text([{'name': 'Dana', 'what': 'Can you send it tomorrow?', 'created_at': '2026-10-04'}])
    assert '“Can you send it tomorrow?”' in text and '?”.' not in text


@pytest.mark.parametrize('identifier,allowed', [
    ('chatterjee@example.com', True), ('15551234567@s.whatsapp.net', True), ('+1 (555) 555-0123', True),
    ('chat123456789012345678', False), ('(((((((', False), ('2026-10', False)])
def test_draft_offer_address_shapes(identifier, allowed):
    assert rp._direct_address(identifier) is allowed


def test_punctuation_only_ask_falls_back_to_summary():
    assert 'Send the deck' in _commitment_text([{'name': 'K', 'ask': '.', 'what': 'Send the deck'}])
