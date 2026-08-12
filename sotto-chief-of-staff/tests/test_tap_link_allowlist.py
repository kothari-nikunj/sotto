"""test_tap_link_allowlist.py — a tap link may only point at someone the SOURCE DATA contained.

THE BUG (Aug 2026, external security reviewer, reproduced below verbatim): the model chooses each
action's `contactIdentifier` and deterministic code turned it into a tappable link without ever
asking whether the data contained that recipient. Source recipient `+15551234567`, model-produced
recipient `+19999999999` → validator violations `[]`, returned link `sms:+19999999999`. The
validator checked identifiers embedded in the MARKDOWN, never the action objects the link builder
consumes, and its verdict was logged rather than enforced.

The fix is one sentence: a tap link is minted only for an identifier the gathered sources actually
carried — anything else keeps its prose, loses its identifier, and is recorded as a refusal.
"""
import importlib.util
import json
import os

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location(
    "compose_brief", os.path.join(ROOT, "_shared", "scripts", "compose_brief.py"))
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)

import brief_validate  # noqa: E402


REAL_PHONE = "+15551234567"
FABRICATED_PHONE = "+19999999999"


def _inputs():
    """A payload whose ONLY message counterpart is REAL_PHONE (Sarah)."""
    return {
        "type": "morning",
        "window_hours": 24,
        "google": {"emails": [{"threadId": "t-1", "from": "Dana Wells <dana@acme.com>",
                               "subject": "Pilot", "body": "ping"}],
                   "events": []},
        "local": {"contacts": [{"name": "Sarah Chen", "phones": [REAL_PHONE],
                                "emails": ["sarah@acme.com"]}],
                  "imessage": [{"handle": REAL_PHONE, "is_from_me": False, "is_group_chat": False,
                                "timestamp": "2026-08-12 09:00:00", "text": "still on for Thursday?"}]},
    }


def _llm_returning(actions, markdown="# Brief\n\n## Needs Attention Now\n\n**Sarah Chen** - reply."):
    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": markdown, "actions": actions})
    return fake_llm


# ── The reviewer's reproduction ───────────────────────────────────────────────────────────────────

def test_reviewer_reproduction_fabricated_recipient_is_never_linked(tmp_path, monkeypatch):
    """Source `+15551234567`, model action `+19999999999`: no link, no identifier, one recorded
    violation — and the brief still composes."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    out = cb.compose(_inputs(), llm=_llm_returning([
        {"id": "a1", "type": "reply", "channel": "imessage", "contactName": "Sarah Chen",
         "contextSummary": "Sarah asked about Thursday", "contactIdentifier": FABRICATED_PHONE}]))
    a = out["actions"][0]
    assert not a.get("tap_link"), f"a fabricated recipient was linked: {a.get('tap_link')}"
    assert not a.get("contactIdentifier"), "the fabricated identifier survived on the action"
    refusals = out.get("_refused_tap_targets") or []
    assert any(FABRICATED_PHONE in r for r in refusals), f"no refusal recorded: {refusals}"
    # Fail toward silence for the ACTION, never for the brief.
    assert out["brief_markdown"].startswith("# Brief")
    assert a.get("contextSummary") == "Sarah asked about Thursday"   # the TEXT is untouched


def test_validator_sees_the_action_objects_not_just_the_markdown():
    """The check the reviewer found missing: validate() reads ACTION OBJECTS against the allowlist."""
    allow = {brief_validate._normalize_identifier(REAL_PHONE)}
    violations = brief_validate.validate(
        "# Brief", [{"id": "a1", "channel": "imessage", "contactIdentifier": FABRICATED_PHONE}],
        "identifier: +15551234567 | channel: imessage", allowed_identifiers=allow)
    assert any("fabricated-identifier" in v and FABRICATED_PHONE in v for v in violations), violations
    # …and a real one is clean.
    assert not brief_validate.validate(
        "# Brief", [{"id": "a1", "channel": "imessage", "contactIdentifier": REAL_PHONE}],
        "identifier: +15551234567 | channel: imessage", allowed_identifiers=allow)


# ── The legitimate cases the refusal must NOT touch ───────────────────────────────────────────────

def test_legitimate_identifier_still_links(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    out = cb.compose(_inputs(), llm=_llm_returning([
        {"id": "a1", "type": "reply", "channel": "imessage", "contactName": "Sarah Chen",
         "contactIdentifier": REAL_PHONE}]))
    assert out["actions"][0]["tap_link"] == "sms:+15551234567"
    assert not (out.get("_refused_tap_targets") or [])


def test_formatting_variant_of_a_real_identifier_still_links(tmp_path, monkeypatch):
    """Normalization, not string equality: '+1 (555) 123-4567' is the source's '15551234567'."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    payload = _inputs()
    payload["local"]["imessage"][0]["handle"] = "15551234567"      # source form: bare digits
    payload["local"]["contacts"][0]["phones"] = ["15551234567"]
    out = cb.compose(payload, llm=_llm_returning([
        {"id": "a1", "type": "reply", "channel": "imessage", "contactName": "Sarah Chen",
         "contactIdentifier": "+1 (555) 123-4567"}]))                # model's form: formatted
    assert out["actions"][0]["tap_link"] == "sms:+15551234567"


def test_email_variant_is_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    out = cb.compose(_inputs(), llm=_llm_returning([
        {"id": "a1", "type": "reply", "channel": "email", "contactName": "Dana Wells",
         "emailReplyTo": "Dana@Acme.com"}]))
    assert out["actions"][0]["tap_link"].startswith("mailto:Dana@Acme.com")


def test_whatsapp_jid_counts_as_its_phone(tmp_path, monkeypatch):
    """A WhatsApp JID in the source allows the phone the action carries (one normalizer, both ends)."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    payload = _inputs()
    payload["local"]["whatsapp"] = [{"contact_jid": "15558887777@s.whatsapp.net",
                                     "partner_name": "Ravi", "is_from_me": False,
                                     "timestamp": "2026-08-12 09:00:00", "text": "date?"}]
    out = cb.compose(payload, llm=_llm_returning([
        {"id": "a1", "type": "reply", "channel": "whatsapp", "contactName": "Ravi",
         "contactIdentifier": "+15558887777"}]))
    assert out["actions"][0]["tap_link"] == "https://wa.me/15558887777"


def test_group_actions_keep_their_group_id_and_stay_unlinked(tmp_path, monkeypatch):
    """Group deep links stay disabled — and the refusal must not strip a group's stable id, which is
    how a group loop keeps its identity across days."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    payload = _inputs()
    payload["local"]["imessage"].append(
        {"handle": REAL_PHONE, "is_from_me": False, "is_group_chat": True, "chat_guid": "grp-ops",
         "group_name": "Ops War Room", "timestamp": "2026-08-12 09:05:00", "text": "who's on call?"})
    out = cb.compose(payload, llm=_llm_returning([
        {"id": "a1", "type": "reply", "channel": "imessage", "contactName": "Ops War Room",
         "contactIdentifier": "grp-ops"}]))
    a = out["actions"][0]
    assert not a.get("tap_link")                    # no group deep link, as before
    assert a.get("contactIdentifier") == "grp-ops"  # …but the group id survives


def test_a_fabricated_meeting_link_is_refused(tmp_path, monkeypatch):
    """Every place an identifier becomes a link answers to the allowlist — including a calendar
    action's link and an email action's thread id, which are tap targets the data must vouch for."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    payload = _inputs()
    payload["google"]["events"] = [{"id": "evt-real", "summary": "Pitch",
                                    "meetingLink": "https://meet.google.com/real-abc"}]
    out = cb.compose(payload, llm=_llm_returning([
        {"id": "a1", "type": "meeting_prep", "channel": "calendar", "contactName": "Pitch",
         "meetingLink": "https://meet.google.com/invented-xyz"},
        {"id": "a2", "type": "meeting_prep", "channel": "calendar", "contactName": "Pitch",
         "eventId": "evt-real"},
        {"id": "a3", "type": "reply", "channel": "email", "contactName": "Dana",
         "emailThreadId": "t-invented"}]))
    by_id = {a["id"]: a for a in out["actions"]}
    assert not by_id["a1"].get("tap_link") and not by_id["a1"].get("meetingLink")
    assert by_id["a2"]["tap_link"] == "https://meet.google.com/real-abc"      # the real event links
    assert not by_id["a3"].get("tap_link") and not by_id["a3"].get("emailThreadId")
    assert by_id["a3"].get("contactName") == "Dana"                            # text kept, id dropped


def test_the_critics_rewrite_is_held_to_the_same_allowlist(tmp_path, monkeypatch):
    """Enforcement runs LAST, after critic/revise — a fabricated recipient introduced by the revise
    pass is refused exactly like one in the first draft."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_CRITIC", "always")

    def fake_llm(prompt, inputs, system=None, schema=None):
        if inputs.get("_critic"):
            return json.dumps({"patches": [{"type": "accuracy", "detail": "thin",
                                            "severity": "moderate"}],
                               "score": 40, "summary": "needs work"})
        if inputs.get("_revise"):
            return json.dumps({"brief_markdown": "# Brief\n\nrevised.", "actions": [
                {"id": "a1", "type": "reply", "channel": "imessage", "contactName": "Ghost",
                 "contactIdentifier": FABRICATED_PHONE}]})
        return json.dumps({"brief_markdown": "# Brief\n\ndraft.", "actions": []})

    out = cb.compose(_inputs(), llm=fake_llm, critic=True)
    assert not out["actions"][0].get("tap_link")
    assert not out["actions"][0].get("contactIdentifier")
    assert any(FABRICATED_PHONE in r for r in out.get("_refused_tap_targets") or [])


# ── The markdown half: a tap MARKER is a tap target too ───────────────────────────────────────────

def test_fabricated_tap_marker_is_stripped_and_the_name_stays(tmp_path, monkeypatch):
    """The app deep-links from `<!--id:…-->` exactly as it does from an action identifier, so the
    same rule applies: the marker goes, the bold name stays, the brief is delivered."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    markdown = ("## Needs Attention Now\n\n"
                "**Sarah Chen**<!--id:+15551234567|ch:imessage--> - real.\n\n"
                "**Nobody Atall**<!--id:+19999999999|ch:imessage--> - invented.\n")
    out = cb.compose(_inputs(), llm=_llm_returning([], markdown))
    assert "<!--id:+15551234567|ch:imessage-->" in out["brief_markdown"]   # the real one survives
    assert "+19999999999" not in out["brief_markdown"]                    # the invented one is gone
    assert "**Nobody Atall**" in out["brief_markdown"]                    # …but the line remains
    assert any("+19999999999" in r for r in out.get("_refused_tap_targets") or [])


def test_fabricated_meeting_marker_is_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    payload = _inputs()
    payload["google"]["events"] = [{"id": "evt-real", "summary": "Pitch",
                                    "meetingLink": "https://meet.google.com/abc"}]
    markdown = ("## Coming Up\n\n<!--meeting:event_id:evt-real|title:Pitch-->\n"
                "<!--meeting:event_id:evt-invented|title:Ghost-->\n")
    out = cb.compose(payload, llm=_llm_returning([], markdown))
    assert "evt-real" in out["brief_markdown"]
    assert "evt-invented" not in out["brief_markdown"]


# ── The allowlist itself ──────────────────────────────────────────────────────────────────────────

def test_allowlist_covers_every_source_channel():
    norm = brief_validate._normalize_identifier
    inputs = {
        "google": {
            "emails": [{"threadId": "t-board", "from": "Ivy Park <ivy@lumen.vc>",
                        "to": "you@sotto.com, Leo <leo@acme.com>"}],
            "events": [{"id": "evt-acme", "meetingLink": "https://meet.google.com/rich-acme-abc",
                        "attendees": [{"email": "dana@acme.com"}],
                        "organizer": {"email": "you@sotto.com"}}],
        },
        "local": {
            "contacts": [{"name": "Sarah", "phones": ["+1 (555) 123-4567"], "emails": ["S@Acme.com"]}],
            "imessage": [{"handle": "+12069994970"}],
            "whatsapp": [{"contact_jid": "15558887777@s.whatsapp.net", "sender_jid": "447700900000@lid",
                          "chat_guid": "120363111@g.us"}],
            "calls": [{"phone": "+14155550123"}],
            "whatsapp_calls": [{"jid": "13035551188@s.whatsapp.net"}],
            "contact_index": [{"canonical_id": "c_abc123", "identifiers": ["graph@person.com"]}],
            "action_ledger": [{"status": "open", "contact_identifier": "+19995550000"}],
            "stale_threads": [{"threadId": "t-stale", "to": "tomas@vendorco.com"}],
        },
    }
    allow = cb.build_tap_target_allowlist(inputs, cb._event_link_map(inputs))
    for value in ("15551234567", "s@acme.com", "+12069994970", "15558887777@s.whatsapp.net",
                  "447700900000@lid", "120363111@g.us", "+14155550123", "13035551188",
                  "graph@person.com", "+19995550000", "t-stale", "tomas@vendorco.com",
                  "ivy@lumen.vc", "leo@acme.com", "t-board", "evt-acme", "dana@acme.com",
                  "https://meet.google.com/rich-acme-abc"):
        assert norm(value) in allow, f"source identifier missing from the allowlist: {value}"
    assert norm("+19999999999") not in allow


def test_a_broken_payload_refuses_links_and_still_composes(tmp_path, monkeypatch):
    """Fail toward silence: a payload the allowlist builder chokes on costs the run its LINKS, never
    its brief — the failure direction can only ever refuse more, not allow more."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))

    def boom(*_a, **_k):
        raise ValueError("malformed source payload")

    monkeypatch.setattr(cb, "_collect_tap_targets", boom)
    assert cb.build_tap_target_allowlist(_inputs()) == set()        # not None — nothing is vouched for
    out = cb.compose(_inputs(), llm=_llm_returning([
        {"id": "a1", "type": "reply", "channel": "imessage", "contactName": "Sarah Chen",
         "contactIdentifier": REAL_PHONE}]))
    assert out["brief_markdown"].startswith("# Brief")             # the brief is still delivered
    assert not out["actions"][0].get("tap_link")


def test_allowlist_never_reads_model_output():
    """It is built from the INPUTS only — an identifier that exists only in the brief is not in it."""
    allow = cb.build_tap_target_allowlist({"google": {}, "local": {}})
    assert allow == set() or all(isinstance(v, str) for v in allow)
    assert brief_validate._normalize_identifier(FABRICATED_PHONE) not in allow
