"""The corpus builder's module loader must not load a second copy of a pack module."""
import importlib.util
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _builder():
    loaded = sys.modules.get("build_golden_corpus")
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(
        "build_golden_corpus", os.path.join(ROOT, "tools", "build_golden_corpus.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_loader_reuses_a_module_imported_through_an_unnormalised_path(monkeypatch):
    """Compose scripts put `scripts/../../_shared/knowledge` on sys.path, so the loaded path has `..`."""
    indirect = os.path.join(ROOT, "morning-brief", "scripts", "..", "..", "_shared", "knowledge", "knowledge.py")
    spec = importlib.util.spec_from_file_location("knowledge", indirect)
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "knowledge", loaded)
    spec.loader.exec_module(loaded)
    assert _builder()._load_module("_shared/knowledge/knowledge.py", "knowledge") is loaded
