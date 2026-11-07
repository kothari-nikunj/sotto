"""web_research.py — THE search seam: the provider ladder (Exa / Parallel / Gemini), the honest
no-provider path, and each client's response parsing against its verified wire shape."""
import concurrent.futures
import importlib.util, io, json, os
import threading
from urllib.error import HTTPError

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


def test_web_search_falls_through_when_exa_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("EXA_API_KEY", "e"); monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    _wire(monkeypatch, {"api.exa.ai": OSError("connection reset"),
                        "generativelanguage": GEMINI_SEARCH})
    out = wr.research("Peyton Casper")
    assert out["provider"] == "gemini" and out["text"] == "Peyton works at Browserbase."


def test_web_search_falls_through_when_exa_finds_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
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


def test_gemini_search_parses_grounding_citations(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    _wire(monkeypatch, {"generativelanguage": GEMINI_SEARCH})
    out = wr.research("Peyton Casper")
    assert out["text"] == "Peyton works at Browserbase."
    assert out["citations"] == [{"title": "Team", "uri": "https://browserbase.com/team"}]


def test_gemini_search_rejects_truncated_output_inside_attempt(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    truncated = {"candidates": [{"finishReason": "MAX_TOKENS",
                                 "content": {"parts": [{"text": "partial answer"}]}}]}
    _wire(monkeypatch, {"generativelanguage": truncated})
    out = wr.research("Peyton Casper")
    assert out["provider"] is None and out["text"] == ""
    import sqlite3
    with sqlite3.connect(tmp_path / "events/model-work.sqlite3") as db:
        assert db.execute("SELECT status FROM attempts").fetchone()[0] == "ValueError"


def test_web_client_repair_releases_parked_request(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_DELIVERY_RUN_ID", "a" * 32)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise HTTPError("https://provider", 400, "bad request", {}, io.BytesIO())
        return GEMINI_SEARCH

    monkeypatch.setattr(wr, "_post", post)
    with pytest.raises(HTTPError):
        wr._gemini_search("same query", 10)
    import model_work
    with pytest.raises(model_work.ModelWorkHeldError):
        wr._gemini_search("same query", 10)
    assert len(calls) == 1
    monkeypatch.setattr(wr, "_client_revision", lambda: "repaired-client")
    assert wr._gemini_search("same query", 10)["text"] == "Peyton works at Browserbase."
    assert len(calls) == 2


def test_identical_grounded_search_in_one_run_reuses_complete_result(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_DELIVERY_RUN_ID", "c" * 32)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    calls = []
    _wire(monkeypatch, {"generativelanguage": GEMINI_SEARCH}, calls)
    first = wr._gemini_search("same query", 10)
    second = wr._gemini_search("same query", 10)
    assert first == second and len(calls) == 1
    first["text"] = "mutated by caller"
    assert wr._gemini_search("same query", 10)["text"] == "Peyton works at Browserbase."


def test_identical_url_read_in_one_run_reuses_complete_result(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_DELIVERY_RUN_ID", "d" * 32)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    calls = []
    response = {"candidates": [{"urlContextMetadata": {"urlMetadata": [{"retrievedUrl": "https://example.com", "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_SUCCESS"}]}, "content": {"parts": [{"text": "Title\nBody"}]}}]}
    _wire(monkeypatch, {"generativelanguage": response}, calls)
    assert wr._gemini_fetch_url("https://example.com", 10) == wr._gemini_fetch_url(
        "https://example.com", 10)
    assert len(calls) == 1


def test_concurrent_identical_searches_single_flight_one_paid_call(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_DELIVERY_RUN_ID", "e" * 32)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    with wr._RUN_CACHE_LOCK:
        wr._RUN_CACHE.clear()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return GEMINI_SEARCH

    monkeypatch.setattr(wr, "_post", post)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(wr._gemini_search, "same concurrent query", 10)
        assert entered.wait(1)
        second = pool.submit(wr._gemini_search, "same concurrent query", 10)
        release.set()
        assert first.result() == second.result()
    assert len(calls) == 1


def test_same_run_and_query_never_reuses_across_data_roots(tmp_path, monkeypatch):
    first_root, second_root = tmp_path / "one", tmp_path / "two"
    monkeypatch.setenv("SOTTO_DELIVERY_RUN_ID", "f" * 32)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    with wr._RUN_CACHE_LOCK:
        wr._RUN_CACHE.clear()
    calls = []

    def post(*args, **kwargs):
        calls.append(os.environ["SOTTO_DATA"])
        text = "first root" if os.environ["SOTTO_DATA"] == str(first_root) else "second root"
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}

    monkeypatch.setattr(wr, "_post", post)
    monkeypatch.setenv("SOTTO_DATA", str(first_root))
    assert wr._gemini_search("same cross-root query", 10)["text"] == "first root"
    monkeypatch.setenv("SOTTO_DATA", str(second_root))
    assert wr._gemini_search("same cross-root query", 10)["text"] == "second root"
    assert calls == [str(first_root), str(second_root)]


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


def test_deep_research_reports_unbilled_admission_when_no_provider_answers(monkeypatch):
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture')
    busy = HTTPError('https://proxy.example/native/v1beta/models/fixture:generateContent',
                     429, 'busy', {'X-Sotto-Admission': 'busy'}, None)
    monkeypatch.setattr(wr.gemini_transport, 'proxy_admission_busy', lambda error: error is busy)
    with pytest.raises(wr.ResearchAdmissionBusyError):
        wr.deep_research('prompt', {}, 60, lambda *args: (_ for _ in ()).throw(busy))

    monkeypatch.setenv('EXA_API_KEY', 'fixture')
    monkeypatch.setattr(wr, '_exa_deep', lambda *args: {'attendees': [{'email': 'a@example.com'}]})
    rows, provider = wr.deep_research('prompt', {}, 60, lambda *args: (_ for _ in ()).throw(busy))
    assert provider == 'exa' and rows['attendees'][0]['email'] == 'a@example.com'


def test_gemini_schema_keyword_is_stripped_for_plain_json_schema_providers():
    # ONE schema constant, three providers: Gemini's OpenAPI-subset `nullable` is not JSON Schema.
    src = {"type": "object", "properties": {"title": {"type": "string", "nullable": True},
                                            "rows": {"type": "array",
                                                     "items": {"type": "string", "nullable": True}}}}
    assert wr.plain_json_schema(src) == {
        "type": "object", "properties": {"title": {"type": "string"},
                                         "rows": {"type": "array", "items": {"type": "string"}}}}
    assert "nullable" in json.dumps(src)      # the source constant is untouched


@pytest.mark.parametrize(('metadata', 'category'), [
    (None, 'metadata_absent_or_empty'),
    ({}, 'metadata_absent_or_empty'),
    ({'urlMetadata': []}, 'metadata_absent_or_empty'),
    ({'urlMetadata': 'PRIVATE'}, 'metadata_absent_or_empty'),
    ({'urlMetadata': ['PRIVATE']}, 'retrieval_status_failed'),
    ({'urlMetadata': [{'retrievedUrl': 'https://example.com', 'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_ERROR'}]}, 'retrieval_status_failed'),
    ({'urlMetadata': [{'retrievedUrl': 'https://example.com', 'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_PAYWALL'}]}, 'retrieval_status_failed'),
    ({'urlMetadata': [{'retrievedUrl': 'https://PRIVATE.example', 'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]}, 'retrieved_url_mismatch'),
    ({'urlMetadata': [{'retrievedUrl': 'https://example.com/', 'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]}, 'retrieved_url_mismatch'),
    ({'urlMetadata': [{'retrievedUrl': 'https://example.com?', 'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]}, 'retrieved_url_mismatch'),
    ({'urlMetadata': [{'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]}, 'retrieved_url_mismatch'),
    ({'urlMetadata': [{'retrievedUrl': 'https://example.com', 'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'},
                     {'retrievedUrl': 'https://PRIVATE.example', 'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]}, 'retrieved_url_mismatch'),
])
def test_fluent_url_answer_without_exact_successful_retrieval_is_unreadable(monkeypatch, tmp_path, metadata, category):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture')
    candidate = {'finishReason': 'STOP', 'content': {'parts': [{'text': 'PRIVATE invented page text'}]}}
    if metadata is not None:
        candidate['urlContextMetadata'] = metadata
    calls, diagnostics = [], []
    monkeypatch.setattr(wr, '_diag', diagnostics.append)
    _wire(monkeypatch, {'generativelanguage': {'candidates': [candidate]}}, calls)
    result = wr.fetch_url('https://example.com')
    assert result['text'] == '' and result['provider'] is None and result['error']
    assert len(calls) == 1  # no additional model attempt to manufacture evidence
    assert diagnostics == [f'[web_research] fetch_url via gemini failed: ValueError: Gemini URL {category}']
    assert 'PRIVATE' not in str(diagnostics)


def test_url_answer_accepts_exact_successful_provider_retrieval(monkeypatch, tmp_path):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture')
    _wire(monkeypatch, {'generativelanguage': {'candidates': [{
        'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': 'https://example.com',
            'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]},
        'content': {'parts': [{'text': 'Title\nRetrieved content'}]}}]}})
    result = wr.fetch_url('https://example.com')
    assert result['provider'] == 'gemini' and result['text'] == 'Source-derived summary (not verbatim page text):\nTitle\nRetrieved content'


@pytest.mark.parametrize('returned_url', [None, 'https://other.example', 'https://example.com'])
def test_exa_contents_requires_requested_url_and_requests_fresh_crawl(monkeypatch, returned_url):
    calls = []
    def post(url, body, headers, timeout):
        calls.append(body)
        return {'results': [{'url': returned_url, 'text': 'Page text', 'title': 'Title'}]}
    monkeypatch.setattr(wr, '_post', post)
    result = wr._exa_contents('https://example.com', 10)
    assert calls[0]['maxAgeHours'] == 0 and calls[0]['urls'] == ['https://example.com']
    assert bool(result) is (returned_url == 'https://example.com')


def test_gemini_url_requests_own_words_notes_and_labels_summary(monkeypatch, tmp_path):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture')
    requests = []
    def post(api, body, headers, timeout):
        requests.append(body)
        return {'candidates': [{'finishReason': 'STOP',
            'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': 'https://example.com',
                'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]},
            'content': {'parts': [{'text': 'Example\nA short factual note.'}]}}]}
    monkeypatch.setattr(wr, '_post', post)
    result = wr.fetch_url('https://example.com')
    prompt = requests[0]['contents'][0]['parts'][0]['text']
    assert 'title on the first line' in prompt and 'in your own words' in prompt
    assert 'do not reproduce the page or quote its text' in prompt
    assert 'Use only facts supported by the retrieved page' in prompt
    assert requests[0]['tools'] == [{'url_context': {}}]
    assert len(requests) == 1 and result['title'] == 'Example'
    assert result['text'].startswith('Source-derived summary (not verbatim page text):\n')


@pytest.mark.parametrize(('finish_reason', 'parts', 'diagnostic'), [
    ('RECITATION', [], 'Gemini URL recitation_blocked'),
    ('RECITATION', [{'text': 'Unusable partial text'}], 'Gemini URL recitation_blocked'),
    ('STOP', [], 'Gemini URL read returned no text'),
])
def test_gemini_url_blocked_or_empty_content_stays_failure(monkeypatch, tmp_path, finish_reason, parts, diagnostic):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture')
    calls, diagnostics = [], []
    _wire(monkeypatch, {'generativelanguage': {'candidates': [{'finishReason': finish_reason,
        'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': 'https://example.com',
            'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]},
        'content': {'parts': parts}}]}}, calls)
    monkeypatch.setattr(wr, '_diag', diagnostics.append)
    result = wr.fetch_url('https://example.com')
    assert result['text'] == '' and result['provider'] is None and result['error']
    assert len(calls) == 1
    assert diagnostics == [f'[web_research] fetch_url via gemini failed: ValueError: {diagnostic}']


@pytest.mark.parametrize(('requested', 'retrieved'), [
    ('https://sixsense.ai/', 'https://www.sixsense.ai/'),
    ('https://www.example.co.uk/', 'https://example.co.uk/'),
    ('https://example.com/', 'https://example.com/'),
])
def test_company_homepage_accepts_only_root_alias_and_reports_actual_url(monkeypatch, tmp_path, requested, retrieved):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture')
    calls = []
    _wire(monkeypatch, {'generativelanguage': {'candidates': [{
        'finishReason': 'STOP',
        'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': retrieved,
            'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]},
        'content': {'parts': [{'text': 'Example\nFacts from the homepage.'}]}}]}}, calls)
    result = wr.fetch_url(requested, company_homepage=True)
    assert result['url'] == retrieved and result['provider'] == 'gemini'
    assert 'Facts from the homepage.' in result['text']
    assert len(calls) == 1  # metadata interpretation, never a second fetch or model attempt


@pytest.mark.parametrize('retrieved', [
    'https://example.com/about', 'https://example.com/?a=1', 'https://example.com/?',
    'https://example.com/#section', 'https://example.com/#', 'https://example.com:443/',
    'https://owner@example.com/', 'http://example.com/', 'https://api.example.com/',
    'https://www.www.example.com/', 'https://example.com.evil.test/', 'https://other.test/',
    'https://www.example.com/about', 'https://www.example.com/?a=1', None,
])
def test_company_homepage_does_not_widen_other_url_boundaries(monkeypatch, tmp_path, retrieved):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture')
    calls = []
    _wire(monkeypatch, {'generativelanguage': {'candidates': [{
        'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': retrieved,
            'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]},
        'content': {'parts': [{'text': 'Unproven page facts'}]}}]}}, calls)
    result = wr.fetch_url('https://example.com/', company_homepage=True)
    assert result['text'] == '' and result['error'] == 'could not read the page'
    assert len(calls) == 1


@pytest.mark.parametrize('requested', [
    'http://example.com/', 'https://example.com/page', 'https://example.com/?',
    'https://example.com/#', 'https://example.com:443/', 'https://owner@example.com/',
    'https://www.www.example.com/',
])
def test_company_homepage_policy_requires_plain_https_root(requested):
    assert wr._company_homepage_urls(requested) == ()


@pytest.mark.parametrize('company_homepage', [False, True])
@pytest.mark.parametrize('status', [None, 'URL_RETRIEVAL_STATUS_ERROR', 'URL_RETRIEVAL_STATUS_PAYWALL'])
def test_company_homepage_still_requires_successful_retrieval(monkeypatch, tmp_path, company_homepage, status):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture')
    _wire(monkeypatch, {'generativelanguage': {'candidates': [{
        'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': 'https://www.example.com/',
            'urlRetrievalStatus': status}]},
        'content': {'parts': [{'text': 'Unproven page facts'}]}}]}})
    assert wr.fetch_url('https://example.com/', company_homepage=company_homepage)['text'] == ''


def test_literal_url_read_still_rejects_company_homepage_alias(monkeypatch, tmp_path):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture')
    _wire(monkeypatch, {'generativelanguage': {'candidates': [{
        'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': 'https://www.example.com/',
            'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]},
        'content': {'parts': [{'text': 'The homepage'}]}}]}})
    result = wr.fetch_url('https://example.com/')
    assert result['text'] == '' and result['url'] == 'https://example.com/'


def test_company_homepage_result_cannot_satisfy_a_literal_url_read(monkeypatch, tmp_path):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'same-operation-fixture')
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture')
    calls = []
    _wire(monkeypatch, {'generativelanguage': {'candidates': [{
        'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': 'https://www.example.com/',
            'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]},
        'content': {'parts': [{'text': 'The company homepage'}]}}]}}, calls)
    assert wr.fetch_url('https://example.com/', company_homepage=True)['text']
    assert wr.fetch_url('https://example.com/')['text'] == ''
    assert len(calls) == 2  # different proof policy cannot reuse the cached broader result


def test_literal_fetch_preserves_existing_operation_identity_material(monkeypatch, tmp_path):
    import model_work
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture')
    monkeypatch.setattr(wr, '_client_revision', lambda: 'current-client-revision')
    captured = []
    original = model_work.scope
    def scope(task, evidence, **kwargs):
        captured.append((task, evidence, kwargs))
        return original(task, evidence, **kwargs)
    monkeypatch.setattr(model_work, 'scope', scope)
    _wire(monkeypatch, {'generativelanguage': {'candidates': [{
        'urlContextMetadata': {'urlMetadata': [{'retrievedUrl': 'https://example.com/',
            'urlRetrievalStatus': 'URL_RETRIEVAL_STATUS_SUCCESS'}]},
        'content': {'parts': [{'text': 'The exact homepage'}]}}]}})
    assert wr.fetch_url('https://example.com/')['text']
    assert captured == [('web_fetch', ['https://example.com/', wr.MODEL, 'current-client-revision'], {'occurrence': True})]
