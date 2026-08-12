import importlib.util
import os

import pytest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "action_links", os.path.join(HERE, "..", "_shared", "scripts", "action_links.py"))
al = importlib.util.module_from_spec(spec)
spec.loader.exec_module(al)


def test_imessage_link():
    assert al.link_for("imessage", "+1 (555) 123-4567", "On my way!") == \
        "imessage://+15551234567?body=On%20my%20way%21"


def test_whatsapp_https_form():
    assert al.link_for("whatsapp", "+15551234567", "hi there") == \
        "https://wa.me/15551234567?text=hi%20there"


def test_mailto_subject_and_body():
    url = al.link_for("email", "sarah@acme.com", "See attached.", subject="Re: Contract")
    assert url.startswith("mailto:sarah@acme.com?")
    assert "subject=Re%3A%20Contract" in url and "body=See%20attached." in url


def test_tel_strips_formatting():
    assert al.link_for("phone", "+1 (555) 123-4567") == "tel:+15551234567"


def test_sms_routes_messages():
    assert al.link_for("sms", "5551234567", "yo") == "sms:5551234567&body=yo"


def test_calendar_and_gmail_thread_channels():
    """Folded in from compose_brief's second builder: a meeting link is already a URL, and an email
    action with a thread but no address opens Gmail on the web."""
    assert al.link_for("calendar", " https://meet.google.com/abc ") == "https://meet.google.com/abc"
    assert al.link_for("calendar", "") == ""
    assert al.link_for("gmail_thread", "thr_88") == "https://mail.google.com/mail/u/0/#inbox/thr_88"


def test_unknown_channel_raises():
    try:
        al.link_for("carrier-pigeon", "x")
        assert False
    except ValueError:
        pass


def test_encoding_is_safe():
    # special chars must be percent-encoded so the link doesn't break
    url = al.link_for("imessage", "+15551234567", "a&b=c?d #e")
    assert "&" not in url.split("?body=")[1]  # the & in the body is encoded
    assert "%26" in url


# ── The variant: a decline is never pre-linked ────────────────────────────────────────────────────

def test_a_decline_is_never_linked(tmp_path):
    """The never-pre-link-a-decline rule, moved out of prose and into code: the user gets no
    tappable no, whatever the channel — a decline is presented as text and approved every time."""
    assert al.link_for("imessage", "+15551234567", "Can't make it — thanks for thinking of me.",
                       action_type="decline") == ""
    assert al.link_for("email", "sarah@acme.com", "Can't make this one — next month?",
                       subject="Re: coffee", action_type="decline") == ""
    # …and the channel is still validated, so a typo can never quietly pass as a withheld link
    with pytest.raises(ValueError):
        al.link_for("carrier-pigeon", "x", "no thanks", action_type="decline")


def test_any_other_variant_still_links(tmp_path):
    assert al.link_for("imessage", "+15551234567", "on my way", action_type="reply") \
        == "imessage://+15551234567?body=on%20my%20way"


# ── The offer: email asks, every other channel links ──────────────────────────────────────────────
#
# The rule in one sentence: for an email action Sotto shows the draft text and ASKS ("want this in
# your Gmail drafts?" → `google_action.py gmail-draft`); every other channel, and any host where
# Google isn't connected, keeps the deep link. The offer itself is prose the agent speaks, so it is
# guarded where it is written — the SKILLs — and the fallback is guarded here, in code.

_SKILL_ROOT = os.path.join(HERE, "..")
# Every surface that used to hand the user a mailto for an email draft.
_OFFER_SKILLS = ("draft-reply", "proactive", "event-triage", "followup")


def _skill_text(name: str) -> str:
    with open(os.path.join(_SKILL_ROOT, name, "SKILL.md"), encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("skill", _OFFER_SKILLS)
def test_the_email_offer_is_an_ask_not_a_mailto(skill):
    """An email draft is offered into Gmail, not pasted as a 300-character percent-encoded URL."""
    text = _skill_text(skill)
    assert "gmail-draft" in text, f"{skill}/SKILL.md never names the draft verb"
    assert "Gmail drafts" in text, f"{skill}/SKILL.md never makes the ask"


@pytest.mark.parametrize("skill", _OFFER_SKILLS)
def test_every_offer_surface_keeps_an_honest_fallback(skill):
    """Do not remove the only path that works when Google is absent: each surface names the
    `fallback:deep_link` branch (or, for followup, defers to draft-reply which does) and still
    offers the link for a non-email channel."""
    text = _skill_text(skill)
    assert "deep_link" in text or "sotto-draft-reply" in text
    assert any(s in text for s in ("wa.me", "action_links.py", "one-tap link"))


def test_the_brief_stops_pasting_mailto_urls_too():
    """The brief is the same rule at a different altitude: it links wa.me/sms/tel actions and lets
    the offer line carry email ("say *draft Dhruv*")."""
    text = _skill_text("morning-brief")
    assert "email asks, every other channel links" in text
    assert "mailto:dhruv@acme.com" not in text     # the old worked example, deleted


def test_the_deep_link_fallback_is_untouched():
    """The mailto builder stays — it is what a host without Google, and every non-email channel,
    still runs on. Removing it would leave those cases with nothing."""
    assert al.link_for("email", "pegah@example.com", "Hey Pegah", subject="Railway SAFE") \
        .startswith("mailto:pegah@example.com?")
    assert al.link_for("whatsapp", "+15551234567", "hi") == "https://wa.me/15551234567?text=hi"
    assert al.link_for("imessage", "+15551234567", "hi") == "imessage://+15551234567?body=hi"
