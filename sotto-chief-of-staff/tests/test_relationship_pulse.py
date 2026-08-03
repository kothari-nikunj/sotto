"""relationship_pulse.py — weekly losing-touch / waiting-on-you detection from read_local history."""
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))

spec = importlib.util.spec_from_file_location(
    "relationship_pulse", os.path.join(ROOT, "relationship-pulse", "scripts", "relationship_pulse.py"))
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


def _msg(name, days_ago, from_me):
    ts = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return {"handle": "+1", "resolved_name": name, "is_from_me": from_me,
            "is_group_chat": False, "timestamp": ts, "text": "hi"}


def test_waiting_on_you():
    # They messaged 5 days ago, you never replied → waiting_on_you.
    local = {"contacts": [{"name": "Dhruv", "phones": ["+1"]}],
             "imessage": [_msg("Dhruv", 5, False)]}
    out = rp.compute(local, NOW)
    q = [x for x in out["attention_queue"] if x["queue_type"] == "waiting_on_you"]
    assert q and q[0]["display_name"] == "Dhruv" and q[0]["days_waiting"] == 5


def test_losing_touch_when_cadence_widens():
    # Used to talk every ~2 days, now silent 20 days → cadence increasing + 14d+ → losing_touch.
    msgs = []
    for d in [60, 58, 56, 54, 52, 50, 48, 46]:   # tight early cadence
        msgs.append(_msg("Sarah", d, d % 2 == 0))
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
    out = rp.compute(local, NOW)
    assert out["attention_queue"] == []
    assert "healthy" in out["pulse_markdown"].lower()


def _losing_msgs(name, handle):
    msgs = []
    for d in [60, 58, 56, 54, 52, 50, 48, 46]:
        m = _msg(name, d, d % 2 == 0)
        m["handle"] = handle
        msgs.append(m)
    last = _msg(name, 20, False)
    last["handle"] = handle
    msgs.append(last)
    return msgs


def test_graph_tracked_person_outranks_untracked(tmp_path):
    # Two people drifting identically; the one with a rich knowledge-graph file ranks first.
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
    assert names and names[0] == "Sarah"                      # graph-weighted to the top
    sarah = next(x for x in losing if x["display_name"] == "Sarah")
    assert sarah["graph_context"]["company"] == "Acme"        # grounded reconnect hook attached
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
