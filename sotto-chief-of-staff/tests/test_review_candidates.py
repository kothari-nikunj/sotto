import json
from datetime import datetime, timedelta, timezone

import review_candidates as rc

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
CID = "c_aaa111bbb222"


def _state(tmp_path, **record):
    path = tmp_path / "knowledge/relationship_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"keep_top": {"x": 1}, "history": {CID: {
        "name": "Alex Chen", **record}}}))
    return path


def _reciprocal_days():
    days = [(NOW - timedelta(days=d)).date().isoformat() for d in (2, 5, 9, 12)]
    return {"active_days": days, "sent_days": days[::2], "received_days": days[1::2]}


def _engaged(reciprocal=True):
    record = {"engagement": {"owner_reply_at": NOW.isoformat(), "attention_boost": 1,
                             "attention_boost_expires": (NOW + timedelta(days=20)).isoformat()}}
    if reciprocal:
        record["importance_evidence"] = _reciprocal_days()
    return record


def test_fast_replies_alone_propose_nothing(tmp_path, monkeypatch):
    """Two quick answers to a vendor are attention, not a relationship: no priority offer without
    contact on distinct days in both directions."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path)); _state(tmp_path, **_engaged(reciprocal=False))
    assert rc.choose(NOW, {}) is None


def test_confirmed_family_outranks_an_attention_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    path = tmp_path / "knowledge/relationship_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"history": {
        CID: {"name": "Alex Chen", **_engaged()},
        "c_bbb222ccc333": {"name": "Sam Chen"}}}))
    people = tmp_path / "knowledge/people"; people.mkdir()
    (people / "sam-chen.md").write_text("---\ncanonical_id: c_bbb222ccc333\nname: Sam Chen\n---\n")
    monkeypatch.setattr(rc, "_confirmed_user_family", lambda person: bool(person) and "Sam" in str(person))
    candidate = rc.choose(NOW, {})
    assert candidate["candidate_type"] == "vip" and candidate["name"] == "Sam Chen"


def test_actual_reply_engagement_offers_exact_priority_command(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path)); _state(tmp_path, **_engaged())
    candidate = rc.choose(NOW, {})
    assert candidate["command"] == "prioritize staying in touch with Alex Chen"
    assert candidate["candidate_type"] == "priority"
    assert candidate["effect"] == {"kind": "review_candidate_offer", "canonical_id": CID,
                                   "candidate_type": "priority",
                                   "payload_hash": candidate["effect"]["payload_hash"]}
    assert "same real urgency band" in candidate["explanation"]


def test_volume_or_biography_never_becomes_a_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _state(tmp_path, importance={"tier": "vvip"}, interactions=999,
           graph_context={"company": "Family Office AI"})
    assert rc.choose(NOW, {}) is None


def test_owner_reply_speed_only_ranks_attention_and_cannot_offer_vip(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    engagement = _engaged()["engagement"]
    engagement["owner_reply_lag_days"] = 0.5
    _state(tmp_path, engagement=engagement, importance_evidence=_reciprocal_days())
    candidate = rc.choose(NOW, {})
    assert candidate["candidate_type"] == "priority"
    assert candidate["command"] == "prioritize staying in touch with Alex Chen"


def test_sustained_reciprocal_relationship_can_offer_vip(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    days = [(NOW - timedelta(days=d)).date().isoformat() for d in (0, 1, 8, 9, 16, 17)]
    _state(tmp_path, importance_evidence={"active_days": days,
        "sent_days": days[::2], "received_days": days[1::2]})
    candidate = rc.choose(NOW, {})
    assert candidate["candidate_type"] == "vip"
    assert candidate["command"] == "make Alex Chen VIP"


def test_single_reply_without_persisted_speed_is_only_priority_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path)); _state(tmp_path, **_engaged())
    assert rc.choose(NOW, {})["candidate_type"] == "priority"


def test_confirmed_relation_must_bind_person_directly_to_user(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path)); _state(tmp_path)
    people = tmp_path / "knowledge/people"; people.mkdir()
    template = ("---\ncanonical_id: " + CID + "\nname: Alex Chen\nrelations:\n"
                "- type: family_of\n  slug: {slug}\n  name: {name}\n  source: user_edit\n"
                "  confidence: 1.0\n---\n")
    path = people / f"{CID}.md"
    path.write_text(template.format(slug="c_someone_else", name="Pat"))
    assert rc.choose(NOW, {}) is None
    path.write_text(template.format(slug="c_user", name="You"))
    candidate = rc.choose(NOW, {})
    assert candidate["command"] == "make Alex Chen VIP"
    assert "missed calls" in candidate["explanation"]


def test_full_priorities_mutes_and_existing_vip_block_stale_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path)); _state(tmp_path, **_engaged())
    master = tmp_path / "knowledge/master.md"
    master.write_text("## Priorities\n- One\n- Two\n- Three\n")
    assert rc.choose(NOW, {}) is None
    master.write_text("")
    candidate = rc.choose(NOW, {})
    assert candidate is not None
    assert rc.choose(NOW, {"mute_people": ["Alex Chen"]}) is None
    master.write_text("## Priorities\n- Stay close to Alex Chen\n")
    assert rc.choose(NOW, {}) is None
    master.write_text("")
    prefs = tmp_path / "preferences.json"
    prefs.write_text(json.dumps({"explicit": {"mute_people": ["Alex Chen"]}}))
    assert rc.candidate_valid(candidate["effect"], NOW) is False


def test_only_accepted_delivery_starts_global_cooldown_and_preserves_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    engagement = _engaged()
    engagement["engagement"]["attention_boost_expires"] = (NOW + timedelta(days=60)).isoformat()
    path = _state(tmp_path, **engagement)
    candidate = rc.choose(NOW, {})
    assert rc.mark_offered(candidate["effect"], {"accepted_at": NOW.isoformat()}) is False
    receipt = {"accepted_at": NOW.isoformat(), "message_id": "provider-1"}
    assert rc.mark_offered(candidate["effect"], receipt) is True
    assert rc.mark_offered(candidate["effect"], receipt) is True
    saved = json.loads(path.read_text())
    assert saved["keep_top"] == {"x": 1}
    assert len(saved[rc.OFFER_KEY]) == 1
    assert rc.choose(NOW + timedelta(days=29), {}) is None
    assert rc.choose(NOW + timedelta(days=31), {}) is not None


def test_numeric_outbox_time_and_delivery_id_finalize_without_provider_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path)); path = _state(tmp_path, **_engaged())
    effect = {**rc.choose(NOW, {})["effect"], "delivery_id": "0123456789abcdef"}
    assert rc.candidate_valid(effect, NOW.timestamp())
    receipt = {"accepted_at": NOW.timestamp()}
    assert rc.mark_offered(effect, receipt)
    assert rc.mark_offered(effect, receipt)  # outbox retry is idempotent without a provider id
    offer = json.loads(path.read_text())[rc.OFFER_KEY][0]
    assert offer["delivery_id"] == "0123456789abcdef" and "message_id" not in offer


def test_accepted_send_stamps_even_if_authority_changes_before_finalize(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path)); path = _state(tmp_path, **_engaged())
    effect = {**rc.choose(NOW, {})["effect"], "delivery_id": "fedcba9876543210"}
    (tmp_path / "preferences.json").write_text(json.dumps({"explicit": {
        "mute_people": ["Alex Chen"]}}))
    assert not rc.candidate_valid(effect, NOW.timestamp())
    assert rc.mark_offered(effect, {"accepted_at": NOW.timestamp()})
    assert json.loads(path.read_text())[rc.OFFER_KEY][0]["delivery_id"] == "fedcba9876543210"


def test_accepted_send_finishes_when_another_offer_started_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path)); path = _state(tmp_path, **_engaged())
    effect = {**rc.choose(NOW, {})["effect"], "delivery_id": "aaaaaaaaaaaaaaaa"}
    state = json.loads(path.read_text())
    state[rc.OFFER_KEY] = [{"kind": rc.EFFECT_KIND, "candidate_type": "vip",
        "canonical_id": "c_bbb111bbb222", "payload_hash": "b" * 64,
        "accepted_at": NOW.isoformat(), "delivery_id": "bbbbbbbbbbbbbbbb"}]
    path.write_text(json.dumps(state))
    assert rc.mark_offered(effect, {"accepted_at": NOW.timestamp()})
    assert len(json.loads(path.read_text())[rc.OFFER_KEY]) == 2


def test_mute_senders_matches_canonical_identifiers_and_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _state(tmp_path, aliases=["A. Chen"], **_engaged())
    people = tmp_path / "knowledge/people"; people.mkdir()
    (people / f"{CID}.md").write_text(
        f"---\ncanonical_id: {CID}\nname: Alex Chen\nidentifiers:\n- alex@example.com\n---\n")
    assert rc.choose(NOW, {"mute_people": ["A. Chen"]}) is None
    assert rc.choose(NOW, {"mute_senders": ["alex@example.com"]}) is None
    assert rc.choose(NOW, {"mute_senders": [CID]}) is None


def test_renamed_identity_does_not_offer_existing_vip_or_duplicate_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _state(tmp_path, aliases=["Alex Chen"], **_engaged())
    people = tmp_path / "knowledge/people"; people.mkdir()
    (people / f"{CID}.md").write_text(
        f"---\ncanonical_id: {CID}\nname: Alexander Chen\nidentifiers:\n- alex@example.com\n"
        "relations:\n- type: family_of\n  slug: c_user\n  name: You\n  source: user_edit\n---\n")
    assert rc.choose(NOW, {"vip_people": ["Alexander Chen"]})["candidate_type"] == "priority"
    assert rc.choose(NOW, {"vip_people": ["alex@example.com"]})["candidate_type"] == "priority"
    master = tmp_path / "knowledge/master.md"
    master.write_text("## Priorities\n- Staying in touch with Alexander Chen\n")
    assert rc.choose(NOW, {"vip_people": ["Alexander Chen"]}) is None


def test_family_lookup_builds_one_index_and_parses_only_history_people(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    history = {f"c_{i:012x}": {"name": f"Person {i}"} for i in range(30)}
    path = tmp_path / "knowledge/relationship_state.json"; path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"history": history}))
    calls = {"build": 0, "parse": 0}
    monkeypatch.setattr(rc.kg, "build_people_index", lambda: calls.__setitem__("build", calls["build"] + 1) or
                        {"by_cid": {}, "by_identifier": {}, "by_name": {}})
    original = rc.kg.parse_person_file
    monkeypatch.setattr(rc.kg, "parse_person_file", lambda text: calls.__setitem__("parse", calls["parse"] + 1)
                        or original(text))
    assert rc.choose(NOW, {}) is None
    assert calls == {"build": 1, "parse": 0}


def test_candidate_effect_rejects_payload_tampering(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path)); _state(tmp_path, **_engaged())
    effect = rc.choose(NOW, {})["effect"]
    assert rc.candidate_valid(effect, NOW)
    assert not rc.candidate_valid({**effect, "payload_hash": "wrong"}, NOW)
