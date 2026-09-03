"""test_model_providers.py — the provider seam (docs/plans/model-agnostic-pipeline.md).

One battery, three adapters: the wire fields each provider needs, the same-family fallback
privacy invariant, the context floor, and the back-compat guarantee that a default install
(bare model names, GOOGLE_AI_API_KEY only) behaves exactly as before the seam existed.
"""
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "gem", os.path.join(HERE, "..", "_shared", "lib", "gemini.py"))
gem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gem)


def test_parse_model_ref():
    assert gem.parse_model_ref("gemini-3.8-flash") == ("gemini", "gemini-3.8-flash")
    assert gem.parse_model_ref("openai/gpt-5.2-codex") == ("openai", "gpt-5.2-codex")
    assert gem.parse_model_ref("ANTHROPIC/claude-big") == ("anthropic", "claude-big")
    assert gem.parse_model_ref("flash-thing", default_provider="openai") == ("openai", "flash-thing")
    with pytest.raises(RuntimeError):
        gem.parse_model_ref("mistral/m")   # unknown family is a config error, not a guess


class _FakeResp:
    def __init__(self, reply):
        self._reply = reply
    def read(self):
        return json.dumps(self._reply).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _capture_http(monkeypatch, reply):
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append({"url": req.full_url,
                      "headers": {k.lower(): v for k, v in req.header_items()},
                      "body": json.loads(req.data)})
        return _FakeResp(reply)
    monkeypatch.setattr(gem.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_openai_wire_shape(monkeypatch):
    monkeypatch.delenv("SOTTO_OPENAI_BASE_URL", raising=False)
    calls = _capture_http(monkeypatch, {
        "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    out = gem._openai_once("gpt-big", "key-oai", "the prompt", system="be terse",
                           schema={"type": "object"})
    assert out == '{"ok": true}'
    c = calls[0]
    assert c["url"] == "https://api.openai.com/v1/chat/completions"
    assert c["headers"]["authorization"] == "Bearer key-oai"
    assert c["body"]["response_format"] == {"type": "json_object"}
    sys_msg = c["body"]["messages"][0]
    assert sys_msg["role"] == "system" and "be terse" in sys_msg["content"]
    assert "JSON Schema" in sys_msg["content"]           # schema travels in the system text
    assert c["body"]["messages"][1] == {"role": "user", "content": "the prompt"}


def test_openai_base_url_override_allows_keyless(monkeypatch):
    """The subscription story: a local OpenAI-compatible endpoint (LiteLLM, a gateway carrying
    Codex/Claude subscription auth) is one env var away, and needs no API key."""
    monkeypatch.setenv("SOTTO_OPENAI_BASE_URL", "http://localhost:4000/v1/")
    calls = _capture_http(monkeypatch, {
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}})
    assert gem._openai_once("local-big", "", "p") == "{}"
    assert calls[0]["url"] == "http://localhost:4000/v1/chat/completions"
    assert "authorization" not in calls[0]["headers"]


def test_anthropic_wire_shape(monkeypatch):
    calls = _capture_http(monkeypatch, {
        "content": [{"type": "tool_use", "input": {"ok": True}}],
        "usage": {"input_tokens": 10, "output_tokens": 2}})
    out = gem._anthropic_once("claude-big", "key-ant", "the prompt", system="be terse",
                              schema={"type": "object"})
    assert json.loads(out) == {"ok": True}
    c = calls[0]
    assert c["url"] == "https://api.anthropic.com/v1/messages"
    assert c["headers"]["x-api-key"] == "key-ant"
    assert "context-1m" in c["headers"]["anthropic-beta"]   # the 1M window ask
    assert c["body"]["system"] == "be terse"
    assert c["body"]["tool_choice"] == {"type": "tool", "name": "result"}
    assert c["body"]["tools"][0]["input_schema"] == {"type": "object"}
    assert c["body"]["max_tokens"] == gem.ANTHROPIC_MAX_TOKENS


def _clean_env(monkeypatch):
    for v in ("SOTTO_LLM_STUB", "SOTTO_BRIEF_MODEL", "SOTTO_GEMINI_MODEL", "SOTTO_FALLBACK_MODEL",
              "SOTTO_FALLBACK_API_KEY", "SOTTO_OPENAI_BASE_URL", "GOOGLE_AI_API_KEY",
              "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(v, raising=False)


def test_call_gemini_dispatches_on_provider(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("SOTTO_BRIEF_MODEL", "openai/gpt-big")
    monkeypatch.setenv("OPENAI_API_KEY", "key-oai")
    seen = []
    monkeypatch.setattr(gem, "_openai_once",
                        lambda model, key, prompt, label="", **kw: (seen.append((model, key)), "{}")[1])
    monkeypatch.setattr(gem, "_gemini_once",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrong provider")))
    assert gem.call_gemini("p", {}) == "{}"
    assert seen == [("gpt-big", "key-oai")]


def test_default_install_is_unchanged(monkeypatch):
    """Bare model names + GOOGLE_AI_API_KEY = exactly today's behavior — the seam is invisible."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "g-key")
    seen = []
    monkeypatch.setattr(gem, "_gemini_once",
                        lambda model, key, prompt, label="", **kw: (seen.append((model, key)), "{}")[1])
    assert gem.call_gemini("p", {}) == "{}"
    assert seen == [("gemini-3.8-flash", "g-key")]


def test_missing_key_names_the_right_env_var(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("SOTTO_BRIEF_MODEL", "anthropic/claude-big")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        gem.call_gemini("p", {})


def test_fallback_never_crosses_families(monkeypatch):
    """Privacy invariant: a quota blip must never send the user's data to a provider they didn't
    configure — a cross-family fallback is refused up front, not discovered at 6:30am."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("SOTTO_BRIEF_MODEL", "openai/gpt-big")
    monkeypatch.setenv("OPENAI_API_KEY", "key-oai")
    monkeypatch.setenv("SOTTO_FALLBACK_MODEL", "gemini/gemini-3-flash-preview")
    with pytest.raises(RuntimeError, match="crosses model families"):
        gem.call_gemini("p", {})
    # …and non-gemini providers get NO default fallback (the gemini default would cross)
    monkeypatch.delenv("SOTTO_FALLBACK_MODEL")
    seen = []
    monkeypatch.setattr(gem, "_openai_once",
                        lambda model, key, prompt, label="", **kw: (seen.append(model), "{}")[1])
    gem.call_gemini("p", {})
    assert seen == ["gpt-big"]


def test_context_floor_refuses_known_small_models(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("SOTTO_BRIEF_MODEL", "openai/tiny")
    monkeypatch.setenv("OPENAI_API_KEY", "key-oai")
    monkeypatch.setitem(gem.CONTEXT_WINDOWS, "tiny", 128_000)
    with pytest.raises(RuntimeError, match="below the brief"):
        gem.call_gemini("p", {})
    # unknown models warn and proceed — the user may know something the table doesn't
    monkeypatch.setenv("SOTTO_BRIEF_MODEL", "openai/mystery-1m")
    monkeypatch.setattr(gem, "_openai_once", lambda *a, **k: "{}")
    assert gem.call_gemini("p", {}) == "{}"
