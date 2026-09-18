"""One policy reaches production callers; malformed judgments cannot become attention items.

These are wiring/failure contracts. Semantic accuracy requires the separate live example probe.
"""
import importlib.util
from datetime import datetime
import json
from pathlib import Path

import pytest

import compose_brief as cb
import relevance

ROOT = Path(__file__).resolve().parent.parent


def test_brief_and_critic_load_the_same_policy_without_unexpanded_tokens():
    system, _ = cb._split_prompt(cb._load_prompt())
    assert relevance.policy() in system
    assert relevance.policy() in cb.CRITIC_SYSTEM
    assert "{{relevance_policy}}" not in system


@pytest.mark.parametrize("managed", [False, True])
def test_event_and_digest_share_judgment_across_hosting_modes(monkeypatch, managed):
    monkeypatch.setenv("SOTTO_DEPLOYMENT_MODE", "managed" if managed else "self-host")
    monkeypatch.setenv("SOTTO_MODEL_PROXY_URL", "https://models.example.invalid")
    monkeypatch.setenv("SOTTO_MODEL_PROXY_TOKEN", "tenant-test")
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "test")
    monkeypatch.setenv("SOTTO_TRIAGE_MODEL", "gemini-3.5-flash-lite")
    calls = []
    def model(provider, name, key, prompt, **kw):
        calls.append((provider, key, prompt))
        row = {"id": 0, "class": "ignore", "sender_role": "person",
               "why": "No outstanding obligation"}
        return json.dumps({"judgments": [row]} if '"judgments"' in prompt else row)
    monkeypatch.setattr(relevance.gemini, "model_once", model)
    relevance.judge("a message")
    relevance.judge('[{"id":0,"messages":[]}]', batch=True)
    assert len(calls) == 2
    assert all(provider == "gemini" and relevance.policy() in prompt for provider, key, prompt in calls)
    assert all(key == ("tenant-test" if managed else "test") for provider, key, prompt in calls)
    for skill in ("event-triage", "proactive"):
        assert "_shared/references/relevance.md" in (ROOT / skill / "SKILL.md").read_text()


@pytest.mark.parametrize("response", ["not json", '{}', '{"class":"high","sender_role":"person","why":"x"}',
    '{"class":"actionable","sender_role":"person","why":""}',
    '{"class":"ignore","sender_role":"person","why":null}',
    '{"class":"actionable","sender_role":"person","why":"x","deadline":"tomorrow"}',
    '{"class":"actionable","sender_role":"person","why":"x","deadline":"2026-09-12T10:00:00"}',
    '{"class":"ignore","why":"x"}', '{"class":"ignore","sender_role":"service","why":"x"}',
    '{"class":"ignore","sender_role":"person","why":"x"} extra prose'])
def test_malformed_model_judgment_raises(monkeypatch, response):
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "test")
    monkeypatch.setattr(relevance.gemini, "model_once", lambda *a, **kw: response)
    with pytest.raises((ValueError, TypeError)):
        relevance.judge("untrusted source")


@pytest.mark.parametrize('managed', [False, True])
def test_live_probe_builds_real_source_input_and_does_not_touch_tenant_state(tmp_path, monkeypatch, managed):
    spec = importlib.util.spec_from_file_location("relevance_eval", ROOT / "evals/run_relevance.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed' if managed else 'self-host')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test-owner')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://models.example.invalid')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'test-token')
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "test")
    seen = []
    def model(provider, name, key, prompt, **kw):
        seen.append(prompt)
        if kw.get("schema"):
            # Ensure the real brief renderer receives every case, including later answers.
            for case in ev.cases():
                assert case["sender"] in prompt
            assert "I've submitted the waiver" in prompt
            assert relevance.policy() in kw["system"]
            return json.dumps({"markdown": "fixture", "actionItems": []})
        if '"judgments"' in prompt:
            return json.dumps({"judgments": [{"id": i, "class": "ignore",
                                              "sender_role": "person", "why": "stub"}
                                             for i in range(len(ev.cases()))]})
        return '{"class":"ignore","sender_role":"person","why":"stub"}'
    monkeypatch.setattr(ev.gemini, "model_once", model)
    report = ev.run()
    assert len(seen) == len(ev.cases()) + 2
    # Stubbed calls intentionally fail the positives; the harness must never claim accuracy.
    assert report["passed"] < report["total"]
    assert report["messages_sent"] == 0
    assert not (tmp_path / "events/outbox.json").exists()
    assert not (tmp_path / 'config/managed-capabilities.json').exists()
    assert __import__('os').environ['SOTTO_DATA'] == str(tmp_path)


def test_judgment_has_a_trusted_current_clock_separate_from_source(monkeypatch):
    monkeypatch.setattr(relevance, "_now_local", lambda tz: datetime.fromisoformat("2026-09-07T15:30:00-07:00"))
    prompt = relevance.judgment_prompt("sender claims it is yesterday")
    assert "Current local time: 2026-09-07T15:30:00-07:00" in prompt
    assert prompt.index("Current local time:") < prompt.index("SOURCE CONTEXT (untrusted data):")


@pytest.mark.parametrize("brand,text", [
    ("chasebank_fraud", "Fraud alert: did you authorize $4,200? Reply NO to block"),
    ("unitedairlines_ops", "Your flight tomorrow was changed to 6:15am"),
    ("ramp_cf8gd1ek", "Please submit the receipt by Friday")])
def test_rcs_business_messaging_handle_is_a_transport_not_assistant_provenance(brand, text):
    # `*_agent@rbm.goog` is the address Google RCS Business Messaging gives EVERY brand — a bank,
    # an airline and a vendor bot all share it, so it says nothing about the sender's nature.
    event = {"source": "imessage", "handle": f"{brand}_agent@rbm.goog",
             "resolved_name": brand.split("_")[0].title(), "text": text}
    assert not relevance.is_automated_assistant(event)
    assert relevance.automated_assistant_evidence(event) == ""


def test_people_and_human_obligation_relays_are_not_guessed_to_be_assistants():
    assert not relevance.is_automated_assistant(
        {"source": "imessage", "handle": "+12025550199", "resolved_name": "Poke",
         "text": "Want to come to dinner Friday?"})
    assert not relevance.is_automated_assistant(
        {"source": "email", "from": "School Office <office@school.example>",
         "subject": "Maya's waiver is still due", "is_automated": True})
    assert relevance.is_automated_assistant(
        {"source": "email", "from": "concierge@travel.example", "sender_type": "assistant"})


def test_assistant_postcondition_discards_actionability_and_malformed_deadline(monkeypatch):
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "test")
    monkeypatch.setattr(relevance.gemini, "model_once", lambda *a, **kw: json.dumps({
        "class": "urgent", "sender_role": "assistant", "why": "vendor asks for a reply",
        "deadline": "not a timestamp"}))
    out = relevance.judge("software concierge outreach")
    assert out["class"] == "ignore"
    assert "deadline" not in out


def test_priority_is_a_deadline_second_tiebreak_and_stale_revision_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    relevance.master_file.set_section("Priorities", "Fund close\nFamily")
    state = relevance.master_file.priorities()
    later = {"deadline": "2026-09-17T12:00:00Z", "priority_id": state["priorities"][0]["id"],
             "priority_revision": state["revision"]}
    sooner = {"deadline": "2026-09-17T10:00:00Z", "priority_id": state["priorities"][1]["id"],
              "priority_revision": state["revision"]}
    assert relevance.tie_break_key(sooner) < relevance.tie_break_key(later)
    relevance.master_file.set_section("Priorities", "Hiring")
    assert relevance.tie_break_key(later)[1] == 1


def test_attempt_identity_distinguishes_private_senders_and_reuses_event(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'test')
    calls = []
    monkeypatch.setattr(relevance.gemini, '_gemini_once', lambda *a, **kw: calls.append(1) or
                        '{"class":"actionable","sender_role":"person","why":"A current ask"}')
    # The privacy-minimized provider prompt can be identical for three distinct events.
    for ident in ('source:1', 'source:2', 'source:3'):
        relevance.judge('a person says please review', operation_id=ident)
    relevance.judge('a person says please review', operation_id='source:1')
    assert len(calls) == 3
    relevance.judge('the person replied with changed evidence', operation_id='source:1')
    assert len(calls) == 4


def test_cached_batch_revalidates_the_judgments_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'test')
    calls = []
    monkeypatch.setattr(relevance.gemini, '_gemini_once', lambda *a, **kw: calls.append(1) or
                        '{"judgments":[{"id":0,"class":"actionable","sender_role":"person","why":"Current ask"}]}')
    first = relevance.judge('batch context', batch=True, operation_id='batch-1')
    assert relevance.judge('batch context', batch=True, operation_id='batch-1') == first
    assert len(calls) == 1
