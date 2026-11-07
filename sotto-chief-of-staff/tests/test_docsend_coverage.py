"""DocSend page coverage never mistakes an interrupted read for the end of the deck."""
import importlib.util
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "_shared/scripts/docsend_fetch.py"
spec = importlib.util.spec_from_file_location("docsend_coverage_reader", SCRIPT)
deck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deck)
URL = "https://docsend.com/view/coverage"


class Pages:
    def __init__(self, count=2, end=b"{}"):
        self.count = count
        self.end = end
        self.contacts = []

    def get(self, url, accept="text/html"):
        self.contacts.append(url)
        if accept == "text/html":
            return b"<title>Deck</title>"
        if accept == "image/*":
            return b"page image"
        if int(url.rsplit("/", 1)[1]) <= self.count:
            return b'{"imageUrl":"https://assets.example/page.png"}'
        if isinstance(self.end, BaseException):
            raise self.end
        return self.end


def vision(images, title):
    return f"Read {len(images)} images from {title}."


@pytest.mark.parametrize("end,reason", [
    (b"{}", "page_end_unverified"),
    (HTTPError(URL, 404, "not found", {}, None), "page_end_unverified"),
    (HTTPError(URL, 503, "unavailable", {}, None), "page_fetch_failed"),
    (TimeoutError("private error details"), "page_fetch_failed"),
    (b"not json", "page_fetch_failed"),
    (b"[]", "page_fetch_failed"),
    (b'{"imageUrl":123}', "page_fetch_failed"),
])
def test_short_deck_does_not_infer_authoritative_end(end, reason):
    http = Pages(end=end)
    result = deck.fetch_deck(URL, "owner@example.com", http=http, vision=vision)
    assert result["status"] == "ok"
    assert result["pages"] == result["pagesRead"] == 2
    assert result["expectedPages"] is None and result["complete"] is None
    assert result["stopReason"] == reason
    assert "Full-deck completeness is unknown" in result["text"]
    assert "Read 2 images" in result["text"]
    assert "private error details" not in result["text"]
    assert len(http.contacts) == 6  # open + 3 metadata + 2 images, no retry/probe


def test_page_limit_is_reported_without_probing_page_31():
    http = Pages(count=100)
    result = deck.fetch_deck(URL, "owner@example.com", http=http, vision=vision)
    assert result["pagesRead"] == deck.DOCSEND_MAX_PAGES == 30
    assert result["stopReason"] == "page_limit"
    assert result["complete"] is None and result["expectedPages"] is None
    assert "page limit was reached" in result["text"]
    assert len(http.contacts) == 61
    assert not any(url.endswith("/31") for url in http.contacts)


def test_first_page_failure_remains_error_without_vision():
    result = deck.fetch_deck(URL, "owner@example.com", http=Pages(count=0),
                            vision=lambda *a: pytest.fail("no images to read"))
    assert result["status"] == "error" and result["step"] == "pages"


def test_image_failure_still_fails_instead_of_claiming_read_pages():
    class ImageFailure(Pages):
        def get(self, url, accept="text/html"):
            if accept == "image/*":
                raise TimeoutError("failed image")
            return super().get(url, accept)
    result = deck.fetch_deck(URL, "owner@example.com", http=ImageFailure(),
                            vision=lambda *a: pytest.fail("image download failed"))
    assert result["status"] == "error" and result["step"] == "pages"


def test_coverage_survives_cache_without_contact_or_duplicate_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    http = Pages(count=100)
    first = deck.read_deck(URL, "owner@example.com", http=http, vision=vision, exact_cache=True)
    saved = json.loads(Path(deck.deck_paths("coverage")[1]).read_text())
    assert saved["stopReason"] == "page_limit" and saved["pagesRead"] == 30
    contacts = list(http.contacts)
    for _ in range(2):
        cached = deck.read_deck(URL, "owner@example.com", http=http,
                                vision=lambda *a: pytest.fail("cache cannot call model"), exact_cache=True)
        assert cached["cached"] is True
        assert cached["text"] == first["text"]
        assert cached["complete"] is None and cached["stopReason"] == "page_limit"
    assert http.contacts == contacts


def test_legacy_cache_is_unknown_without_rewriting_or_refetching(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    cache = Path(deck.deck_paths("coverage")[1])
    cache.parent.mkdir()
    original = json.dumps({"url": URL, "pages": 3, "title": "Legacy", "text": "Saved evidence"})
    cache.write_text(original)
    monkeypatch.setattr(deck, "_Http", lambda: pytest.fail("cache cannot open network"))
    result = deck.read_deck(URL, "owner@example.com", exact_cache=True)
    assert result["cached"] is True and result["pagesRead"] == 3
    assert result["complete"] is None and result["expectedPages"] is None
    assert result["stopReason"] == "legacy_cache"
    assert "cached read did not record" in result["text"]
    assert result["text"].endswith("Saved evidence")
    assert cache.read_text() == original


def test_held_contact_does_not_publish_partial_cache_or_call_vision(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    http = Pages(count=1, end=deck.DocSendHeldError("source revoked"))
    with pytest.raises(deck.DocSendHeldError, match="source revoked"):
        deck.read_deck(URL, "owner@example.com", http=http,
                       vision=lambda *a: pytest.fail("held contact cannot call model"), exact_cache=True)
    assert not Path(deck.deck_paths("coverage")[1]).exists()
    assert len(http.contacts) == 4
