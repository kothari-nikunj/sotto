"""web_research.py — Gemini-grounded search: stub path, no-key, citation parsing."""
import importlib.util, io, json, os, sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
spec = importlib.util.spec_from_file_location("wr", os.path.join(ROOT, "_shared", "scripts", "web_research.py"))
wr = importlib.util.module_from_spec(spec); spec.loader.exec_module(wr)


def test_stub_path_returns_text(tmp_path, monkeypatch):
    p = tmp_path / "r.json"; p.write_text("grounded bio text")
    monkeypatch.setenv("SOTTO_LLM_STUB", str(p))
    out = wr.research("Peyton Casper Browserbase")
    assert out["text"] == "grounded bio text" and out["citations"] == []


def test_no_key_degrades_gracefully(monkeypatch):
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)
    out = wr.research("anything")
    assert out["text"] == "" and "error" in out   # never raises; empty so the brief omits, never invents


def test_parses_grounding_citations(monkeypatch):
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    fake = {"candidates": [{
        "content": {"parts": [{"text": "Peyton works at Browserbase."}]},
        "groundingMetadata": {"groundingChunks": [
            {"web": {"uri": "https://browserbase.com/team", "title": "Team"}},
            {"web": {}},  # no uri → skipped
        ]},
    }]}

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(wr.urllib.request, "urlopen", lambda *a, **k: _Resp(json.dumps(fake).encode()))
    out = wr.research("Peyton Casper")
    assert out["text"] == "Peyton works at Browserbase."
    assert out["citations"] == [{"title": "Team", "uri": "https://browserbase.com/team"}]
