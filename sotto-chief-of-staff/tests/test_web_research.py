"""web_research.py — THE search seam: the provider ladder (Exa / Parallel / Gemini), the honest
no-provider path, and each client's response parsing against its verified wire shape."""
import importlib.util, io, json, os, sys

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
spec = importlib.util.spec_from_file_location("wr", os.path.join(ROOT, "_shared", "scripts", "web_research.py"))
wr = importlib.util.module_from_spec(spec); spec.loader.exec_module(wr)

ALL_KEYS = ("EXA_API_KEY", "PARALLEL_API_KEY", "GOOGLE_AI_API_KEY")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test states its own connected providers — inherited keys must never decide a ladder."""
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    for k in ALL_KEYS:
        monkeypatch.delenv(k, raising=False)


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _wire(monkeypatch, routes, calls=None):
    """Serve canned JSON per URL substring; a route whose value is an Exception is raised (a dead
    provider). Records every requested URL into `calls` so a ladder's order is observable."""
    def _urlopen(req, *a, **k):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if calls is not None:
            calls.append(url)
        for frag, payload in routes.items():
            if frag in url:
                if isinstance(payload, Exception):
                    raise payload
                return _Resp(json.dumps(payload).encode())
        raise AssertionError(f"unexpected request: {url}")
    monkeypatch.setattr(wr.urllib.request, "urlopen", _urlopen)


# ── recorded wire shapes ─────────────────────────────────────────────────────────────────────────
# EXA: exa-labs/openapi-spec (Exa Search API 1.2.0) — SearchResponse / ResultWithContent, and the
# deep-search `output.content` structured mode.
EXA_SEARCH = {"requestId": "b5947044", "results": [
    {"id": "https://browserbase.com/team", "url": "https://browserbase.com/team",
     "title": "Team", "publishedDate": "2026-01-04", "author": None, "score": 0.71,
     "text": "Peyton Casper leads engineering at Browserbase."},
    {"id": "https://x.com/peytoncasper", "url": "https://x.com/peytoncasper", "title": "Peyton on X",
     "text": "Shipping browser infra."},
    {"url": "", "title": "no url — dropped"},
]}
EXA_DEEP = {"requestId": "c1", "results": [], "output": {
    "content": {"attendees": [{"email": "a@b.com", "company": "Acme", "summary": "bio"}]},
    "grounding": [{"field": "content", "confidence": "high",
                   "citations": [{"url": "https://acme.com", "title": "Acme"}]}]}}
# PARALLEL: parallel-web/parallel-sdk-python (generated from Parallel's OpenAPI spec) — TaskRun and
# TaskRunResult{run, output:{type:"json", content, basis}}.
PARALLEL_RUN = {"run_id": "trun_1", "status": "queued", "is_active": True,
                "processor": "base", "interaction_id": "int_1"}
PARALLEL_RESULT = {"run": {"run_id": "trun_1", "status": "completed", "is_active": False,
                           "processor": "base", "interaction_id": "int_1"},
                   "output": {"type": "json",
                              "content": {"attendees": [{"email": "p@q.com", "company": "Parallel Co",
                                                         "summary": "from a task run"}]},
                              "basis": [{"field": "attendees", "reasoning": "searched",
                                         "citations": [{"url": "https://q.com", "title": "Q"}]}]}}
GEMINI_SEARCH = {"candidates": [{
    "content": {"parts": [{"text": "Peyton works at Browserbase."}]},
    "groundingMetadata": {"groundingChunks": [
        {"web": {"uri": "https://browserbase.com/team", "title": "Team"}},
        {"web": {}},  # no uri → skipped
    ]}}]}
GEMINI_JSON = {"candidates": [{"content": {"parts": [
    {"text": json.dumps({"attendees": [{"email": "g@h.com", "company": "Gem", "summary": "grounded"}]})}]}}]}


# ── the resolver ─────────────────────────────────────────────────────────────────────────────────

def test_provider_chain_is_presence_only(monkeypatch):
    assert wr.provider_chain("web_search") == [] and wr.provider_chain("deep_research") == []
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    assert wr.provider_chain("web_search") == ["gemini"]
    assert wr.provider_chain("deep_research") == ["gemini"]
    monkeypatch.setenv("EXA_API_KEY", "e")
    assert wr.provider_chain("web_search") == ["exa", "gemini"]
    assert wr.provider_chain("deep_research") == ["exa", "gemini"]
    monkeypatch.setenv("PARALLEL_API_KEY", "p")
    assert wr.provider_chain("web_search") == ["exa", "gemini"]      # Parallel is a research tool
    assert wr.provider_chain("deep_research") == ["parallel", "exa", "gemini"]
    monkeypatch.setenv("EXA_API_KEY", "   ")                          # blank is not connected
    assert wr.provider_chain("deep_research") == ["parallel", "gemini"]


def test_web_search_prefers_exa_over_gemini(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "e"); monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    calls = []
    _wire(monkeypatch, {"api.exa.ai": EXA_SEARCH, "generativelanguage": GEMINI_SEARCH}, calls)
    out = wr.research("Peyton Casper")
    assert out["provider"] == "exa"
    assert all("generativelanguage" not in c for c in calls)   # Gemini was never billed


def test_web_search_falls_through_when_exa_errors(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "e"); monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    _wire(monkeypatch, {"api.exa.ai": OSError("connection reset"),
                        "generativelanguage": GEMINI_SEARCH})
    out = wr.research("Peyton Casper")
    assert out["provider"] == "gemini" and out["text"] == "Peyton works at Browserbase."


def test_web_search_falls_through_when_exa_finds_nothing(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "e"); monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    _wire(monkeypatch, {"api.exa.ai": {"results": []}, "generativelanguage": GEMINI_SEARCH})
    assert wr.research("q")["provider"] == "gemini"


def test_no_provider_is_reported_not_faked():
    out = wr.research("anything")
    # never raises, never a plausible-looking answer: empty text + a named reason to act on
    assert out["text"] == "" and out["citations"] == [] and out["provider"] is None
    assert "EXA_API_KEY" in out["error"] and "PARALLEL_API_KEY" in out["error"] \
        and "GOOGLE_AI_API_KEY" in out["error"]


def test_every_provider_dead_is_also_reported(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "e"); monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    _wire(monkeypatch, {"api.exa.ai": OSError("down"), "generativelanguage": OSError("down")})
    out = wr.research("q")
    assert out["text"] == "" and out["provider"] is None and out["error"] == "search failed"


def test_stub_path_returns_text(tmp_path, monkeypatch):
    p = tmp_path / "r.json"; p.write_text("grounded bio text")
    monkeypatch.setenv("SOTTO_LLM_STUB", str(p))
    out = wr.research("Peyton Casper Browserbase")
    assert out["text"] == "grounded bio text" and out["citations"] == []


# ── client response parsing (recorded shapes above) ──────────────────────────────────────────────

def test_exa_search_parses_text_and_citations(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "e")
    _wire(monkeypatch, {"api.exa.ai": EXA_SEARCH})
    out = wr.research("Peyton Casper")
    assert out["citations"] == [{"title": "Team", "uri": "https://browserbase.com/team"},
                                {"title": "Peyton on X", "uri": "https://x.com/peytoncasper"}]
    assert "Peyton Casper leads engineering at Browserbase." in out["text"]
    assert "no url — dropped" not in out["text"]     # a result without a URL is not a citation


def test_gemini_search_parses_grounding_citations(monkeypatch):
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    _wire(monkeypatch, {"generativelanguage": GEMINI_SEARCH})
    out = wr.research("Peyton Casper")
    assert out["text"] == "Peyton works at Browserbase."
    assert out["citations"] == [{"title": "Team", "uri": "https://browserbase.com/team"}]


def test_exa_deep_parses_structured_output(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "e")
    _wire(monkeypatch, {"api.exa.ai": EXA_DEEP})
    obj, provider = wr.deep_research("prompt", {"type": "object"}, 60, lambda *a: None)
    assert provider == "exa" and obj["attendees"][0]["company"] == "Acme"


def test_parallel_creates_a_run_then_long_polls_the_result(monkeypatch):
    monkeypatch.setenv("PARALLEL_API_KEY", "p")
    calls = []
    _wire(monkeypatch, {"/result": PARALLEL_RESULT, "/v1/tasks/runs": PARALLEL_RUN}, calls)
    obj, provider = wr.deep_research("prompt", {"type": "object"}, 90, lambda *a: None)
    assert provider == "parallel" and obj["attendees"][0]["company"] == "Parallel Co"
    # the wait is ONE blocking fetch bounded by the caller's research budget — no poll loop
    assert calls[0].endswith("/v1/tasks/runs") and "/result?timeout=" in calls[1]
    assert 1 <= int(calls[1].split("timeout=")[1]) <= 90


def test_deep_research_precedence_and_fallthrough(monkeypatch):
    for k in ALL_KEYS:
        monkeypatch.setenv(k, "x")
    seen = []
    _wire(monkeypatch, {"/result": PARALLEL_RESULT, "/v1/tasks/runs": PARALLEL_RUN,
                        "api.exa.ai": EXA_DEEP})
    obj, provider = wr.deep_research("p", {}, 60, lambda *a: seen.append("gemini") or None)
    assert provider == "parallel" and seen == []          # Parallel first when connected
    # Parallel dead → Exa; Gemini still untouched
    _wire(monkeypatch, {"/v1/tasks/runs": OSError("504"), "api.exa.ai": EXA_DEEP})
    obj, provider = wr.deep_research("p", {}, 60, lambda *a: seen.append("gemini") or None)
    assert provider == "exa" and seen == []
    # both dead → the caller's own Gemini rung, last
    _wire(monkeypatch, {"/v1/tasks/runs": OSError("504"), "api.exa.ai": OSError("429")})
    obj, provider = wr.deep_research(
        "p", {}, 60, lambda *a: seen.append("gemini") or {"attendees": [{"email": "z@z.com"}]})
    assert provider == "gemini" and seen == ["gemini"] and obj["attendees"][0]["email"] == "z@z.com"


# ── the budget is per CALL, not per provider ─────────────────────────────────────────────────────
# The ladder used to hand each rung the caller's FULL timeout, so a three-rung fall-through cost
# three times the budget research_attendees enforces per batch (measured 3.4× on a real brief).

def _fake_clock(monkeypatch):
    """A clock the test advances by hand, so a budget test costs no wall time."""
    now = [1000.0]
    monkeypatch.setattr(wr.time, "monotonic", lambda: now[0])
    return now


def _burner(now, spend, out=None, seen=None):
    """A provider that consumes `spend` seconds — but never more than the timeout it was handed,
    which is what a real HTTP timeout guarantees — and then answers `out`."""
    def attempt(*args):
        timeout = args[-1]
        if seen is not None:
            seen.append(timeout)
        now[0] += min(timeout, spend)
        return out
    return attempt


def test_deep_research_spends_one_budget_across_all_three_rungs(monkeypatch):
    for k in ALL_KEYS:
        monkeypatch.setenv(k, "x")
    now = _fake_clock(monkeypatch)
    start, seen = now[0], []
    monkeypatch.setattr(wr, "_parallel_task", _burner(now, 40, None, seen))
    monkeypatch.setattr(wr, "_exa_deep", _burner(now, 40, None, seen))
    obj, provider = wr.deep_research("p", {}, 60,
                                     _burner(now, 40, {"attendees": [{"email": "z@z.com"}]}, seen))
    assert provider == "gemini" and obj["attendees"][0]["email"] == "z@z.com"
    # Each rung got what was LEFT, never the full budget again: 60 → 20 → the 1s floor.
    assert seen == [60, 20, 1]
    # The whole fall-through fits the batch budget (the 1s floor is the only slack), where the
    # per-provider bug would have spent 3 × 60.
    assert now[0] - start <= 60 + 1


def test_web_search_spends_one_budget_across_both_rungs(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "e"); monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    now = _fake_clock(monkeypatch)
    start, seen = now[0], []
    # _WEB_SEARCH is built at import, so the dict is what a caller reaches — patch it, not the names.
    monkeypatch.setitem(wr._WEB_SEARCH, "exa", _burner(now, wr.HTTP_TIMEOUT, None, seen))
    monkeypatch.setitem(wr._WEB_SEARCH, "gemini",
                        _burner(now, 5, {"query": "q", "text": "t", "citations": []}, seen))
    out = wr.research("q")
    assert out["provider"] == "gemini" and out["text"] == "t"
    assert seen == [wr.HTTP_TIMEOUT, 1]
    assert now[0] - start <= wr.HTTP_TIMEOUT + 1


def test_deep_research_with_no_provider_answers_nothing():
    obj, provider = wr.deep_research("p", {}, 60, lambda *a: {"attendees": [{"email": "z@z.com"}]})
    assert obj is None and provider == ""     # the Gemini rung isn't even offered without its key


def test_gemini_schema_keyword_is_stripped_for_plain_json_schema_providers():
    # ONE schema constant, three providers: Gemini's OpenAPI-subset `nullable` is not JSON Schema.
    src = {"type": "object", "properties": {"title": {"type": "string", "nullable": True},
                                            "rows": {"type": "array",
                                                     "items": {"type": "string", "nullable": True}}}}
    assert wr.plain_json_schema(src) == {
        "type": "object", "properties": {"title": {"type": "string"},
                                         "rows": {"type": "array", "items": {"type": "string"}}}}
    assert "nullable" in json.dumps(src)      # the source constant is untouched
