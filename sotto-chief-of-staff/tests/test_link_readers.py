"""fetch_url (the search seam's third capability) + docsend_fetch (the deck reader):
ladder order, never-raise contracts, the DocSend gate flow on fixture HTML, and the
unattended refusal that keeps deck views chat-only."""
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location(
    "wr", os.path.join(ROOT, "_shared", "scripts", "web_research.py"))
wr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wr)

spec2 = importlib.util.spec_from_file_location(
    "df", os.path.join(ROOT, "_shared", "scripts", "docsend_fetch.py"))
df = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(df)


# ── fetch_url ─────────────────────────────────────────────────────────────────────────────────────

def test_fetch_url_ladder_is_exa_then_gemini(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "x")
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    assert wr.provider_chain("fetch_url") == ["exa", "gemini"]
    calls = []
    monkeypatch.setitem(wr._FETCH_URL, "exa",
                        lambda url, t: (calls.append("exa"), None)[1])       # Exa comes back empty
    monkeypatch.setitem(wr._FETCH_URL, "gemini",
                        lambda url, t: (calls.append("gemini"),
                                        {"url": url, "title": "T", "text": "body"})[1])
    out = wr.fetch_url("https://example.com/memo")
    assert calls == ["exa", "gemini"]                          # fell through in order
    assert out["provider"] == "gemini" and out["text"] == "body"


def test_fetch_url_never_raises_and_names_the_missing_provider(monkeypatch):
    for k in ("EXA_API_KEY", "PARALLEL_API_KEY", "GOOGLE_AI_API_KEY", "SOTTO_LLM_STUB"):
        monkeypatch.delenv(k, raising=False)
    out = wr.fetch_url("https://example.com")
    assert out["text"] == "" and "no search provider" in out["error"]


def test_fetch_url_exa_contents_parses_the_wire_shape(monkeypatch):
    monkeypatch.setattr(wr, "_post", lambda url, body, headers, timeout: {
        "results": [{"url": body["urls"][0], "title": "The Memo", "text": "  the content  "}]})
    monkeypatch.setenv("EXA_API_KEY", "x")
    out = wr._exa_contents("https://example.com/memo", 10)
    assert out == {"url": "https://example.com/memo", "title": "The Memo", "text": "the content"}


def test_browseruse_is_the_last_rung_when_keyed(monkeypatch):
    # Exa and Gemini url_context are crawler-class and cheap; the hosted browser (real credits,
    # a 10-60s session) is the escalation for pages NO crawler can read — so it comes LAST.
    monkeypatch.setenv("EXA_API_KEY", "x")
    monkeypatch.setenv("BROWSER_USE_API_KEY", "b")
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "g")
    assert wr.provider_chain("fetch_url") == ["exa", "gemini", "browseruse"]
    monkeypatch.delenv("BROWSER_USE_API_KEY")
    assert wr.provider_chain("fetch_url") == ["exa", "gemini"]   # keyless deploys: unchanged


def test_browseruse_fetch_creates_polls_and_stops_the_session(monkeypatch):
    """The v2 wire shape (their CLOUD.md): POST /tasks {task} → {id, sessionId}; GET /tasks/<id>
    until finished; PATCH /sessions/<id> {action: stop} ALWAYS — an open session bills."""
    monkeypatch.setenv("BROWSER_USE_API_KEY", "b")
    monkeypatch.setattr(wr, "BROWSERUSE_POLL_SECS", 0)
    calls = []

    def fake_post(url, body, headers, timeout, method="POST"):
        calls.append((method, url.split("/api/v2")[1], body))
        assert headers.get("X-Browser-Use-API-Key") == "b"
        return {"id": "t1", "sessionId": "s1"}

    def fake_get(url, headers, timeout):
        calls.append(("GET", url.split("/api/v2")[1], None))
        return {"status": "finished", "output": "The Memo\nBody text of the page."}

    monkeypatch.setattr(wr, "_post", fake_post)
    monkeypatch.setattr(wr, "_get", fake_get)
    out = wr._browseruse_fetch("https://example.com/spa", 30)
    assert out["title"] == "The Memo" and "Body text" in out["text"]
    assert calls[0][0] == "POST" and calls[0][1] == "/tasks"
    assert "do not log in" in calls[0][2]["task"]                # the read-only fence rides the task
    assert calls[-1] == ("PATCH", "/sessions/s1", {"action": "stop"})   # session never left open


# ── docsend_fetch ────────────────────────────────────────────────────────────────────────────────

GATED_PAGE = ('<html><head><title>Acme Seed Deck | DocSend</title>'
              '<meta name="csrf-token" content="meta-tok"></head><body>'
              '<form><input name="authenticity_token" value="form-tok">'
              '<input name="link_auth_form[email]"></form></body></html>')
OPEN_PAGE = "<html><head><title>Open Deck</title></head><body>viewer</body></html>"


class FakeHttp:
    """Scripted DocSend: a gated view page, a gate POST that unlocks it, two pages of images."""

    def __init__(self, gated=True, unlock=True):
        self.gated, self.unlocked = gated, not gated
        self.unlock = unlock
        self.posts = []

    def get(self, url, accept="text/html"):
        if "/page_data/" in url:
            n = int(url.rsplit("/", 1)[1])
            if n <= 2:
                return json.dumps({"imageUrl": f"https://img/{n}.png"}).encode()
            raise RuntimeError("404")
        if url.startswith("https://img/"):
            return b"PNGBYTES" + url[-5].encode()
        if self.gated and not self.unlocked:
            return GATED_PAGE.encode()
        return OPEN_PAGE.encode()

    def post_form(self, url, fields, referer):
        self.posts.append(fields)
        if self.unlock:
            self.unlocked = True
        return b"{}"


def test_docsend_gate_flow_posts_the_users_email_and_reads_pages():
    http = FakeHttp()
    seen = {}

    def vision(images, title):
        seen["n"], seen["title"] = len(images), title
        return "Page 1: intro\nSUMMARY: Acme builds X."

    out = df.fetch_deck("https://docsend.com/view/abc123", "nikunj@fpv.com", http=http,
                        vision=vision)
    assert out["status"] == "ok" and out["pages"] == 2
    assert "Acme builds X." in out["text"]
    assert http.posts[0]["link_auth_form[email]"] == "nikunj@fpv.com"
    assert http.posts[0]["authenticity_token"] == "form-tok"   # form token preferred over meta
    assert seen["n"] == 2 and "Acme Seed Deck" in out["title"]


def test_docsend_open_deck_skips_the_gate():
    http = FakeHttp(gated=False)
    out = df.fetch_deck("https://docsend.com/view/xyz", "n@x.com", http=http,
                        vision=lambda i, t: "SUMMARY: open deck")
    assert out["status"] == "ok" and http.posts == []


def test_docsend_verification_wall_fails_loud_with_the_step():
    http = FakeHttp(unlock=False)                              # gate never opens
    out = df.fetch_deck("https://docsend.com/view/abc", "n@x.com", http=http,
                        vision=lambda i, t: "x")
    assert out["status"] == "error" and out["step"] == "gate"
    assert "VERIFICATION" in out["error"] or "passcode" in out["error"]


def test_docsend_rejects_non_docsend_urls():
    out = df.fetch_deck("https://example.com/deck.pdf", "n@x.com", http=FakeHttp())
    assert out["status"] == "error" and out["step"] == "url"
    assert df.is_docsend("https://docsend.com/view/aB3_x") is True
    assert df.is_docsend("https://example.com") is False


def test_docsend_cli_refuses_unattended_runs(tmp_path):
    # The outward-facing gate: a cron/event run must never log a view-receipt on a founder's deck.
    env = {**os.environ, "SOTTO_UNATTENDED": "1", "SOTTO_DATA": str(tmp_path)}
    r = subprocess.run([sys.executable,
                        os.path.join(ROOT, "_shared", "scripts", "docsend_fetch.py"),
                        "--url", "https://docsend.com/view/abc"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 2
    out = json.loads(r.stdout)
    assert out["step"] == "unattended" and "refused" in out["error"]


# A handmade minimal JPEG: SOI + SOF0 (precision 8, 100×200, 3 components) + EOI. Not renderable —
# the builder only parses the header and embeds the bytes verbatim (/DCTDecode is packaging).
FAKE_JPEG = (b"\xff\xd8" + b"\xff\xc0\x00\x11\x08" + (100).to_bytes(2, "big")
             + (200).to_bytes(2, "big") + b"\x03" + b"\x00" * 9 + b"\xff\xd9")


def test_pdf_builder_embeds_jpegs_and_declines_anything_else():
    assert df._jpeg_size(FAKE_JPEG) == (200, 100, 3)
    pdf = df.build_pdf([FAKE_JPEG, FAKE_JPEG])
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    assert pdf.count(b"/DCTDecode") == 2 and b"/Count 2" in pdf
    assert FAKE_JPEG in pdf                                   # the image bytes ride verbatim
    assert df.build_pdf([b"\x89PNG not a jpeg"]) is None      # non-JPEG → no PDF, never a crash
    assert df.build_pdf([]) is None


def test_read_deck_saves_pdf_plus_cache_and_a_reask_never_reviews(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    http = FakeHttp()
    http_gets = http.get

    def counting_get(url, accept="text/html"):
        if url.startswith("https://img/"):
            return FAKE_JPEG                                   # embeddable pages
        return http_gets(url, accept)
    http.get = counting_get
    monkeypatch.setattr(df, "_Http", lambda: http)
    monkeypatch.setattr(df, "_gemini_read", lambda images, title: "SUMMARY: Acme builds X.")
    out = df.read_deck("https://docsend.com/view/abc123", "n@x.com")
    assert out["status"] == "ok" and out["cached"] is False
    assert out["pdf"].endswith("abc123.pdf") and os.path.exists(out["pdf"])
    with open(out["pdf"], "rb") as f:
        assert f.read(8).startswith(b"%PDF")
    # Second ask: answered from disk — NO network, NO new view logged.
    monkeypatch.setattr(df, "_Http", lambda: (_ for _ in ()).throw(AssertionError("network!")))
    again = df.read_deck("https://docsend.com/view/abc123", "n@x.com")
    assert again["cached"] is True and "Acme builds X." in again["text"]


def test_unattended_serves_the_cache_but_never_fetches(tmp_path):
    env = {**os.environ, "SOTTO_UNATTENDED": "1", "SOTTO_DATA": str(tmp_path)}
    script = os.path.join(ROOT, "_shared", "scripts", "docsend_fetch.py")
    # cache MISS → refused (a fetch would log a view under the user's name)
    r = subprocess.run([sys.executable, script, "--url", "https://docsend.com/view/zzz"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 2 and json.loads(r.stdout)["step"] == "unattended"
    # cache HIT → served (reading a file already on the volume is not a view)
    os.makedirs(os.path.join(str(tmp_path), "decks"), exist_ok=True)
    with open(os.path.join(str(tmp_path), "decks", "zzz.json"), "w") as f:
        json.dump({"url": "https://docsend.com/view/zzz", "title": "T", "pages": 3,
                   "text": "SUMMARY: cached deck", "pdf": ""}, f)
    r = subprocess.run([sys.executable, script, "--url", "https://docsend.com/view/zzz"],
                       capture_output=True, text=True, env=env)
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["cached"] is True and "cached deck" in out["text"]


def test_skills_document_both_readers():
    with open(os.path.join(ROOT, "ask", "SKILL.md"), encoding="utf-8") as f:
        ask = f.read()
    assert "--url" in ask and "docsend_fetch.py" in ask
    assert "refuses in unattended runs" in ask                 # the view-receipt warning is stated
    with open(os.path.join(ROOT, "event-triage", "SKILL.md"), encoding="utf-8") as f:
        ev = f.read()
    assert "web_research.py\" --url" in ev
    assert "Never `docsend_fetch.py`" in ev                    # chat-only, stated where agents read
