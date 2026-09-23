"""Keep the Python Bridge receipt vocabulary aligned with statuses emitted by the Rust Bridge."""
import re
from pathlib import Path

from source_context import BRIDGE_STATUSES


ROOT = Path(__file__).resolve().parents[2]
RUST_MAIN = ROOT / "sotto-bridge/core/src/main.rs"


def _function(source: str, name: str, next_name: str) -> str:
    return source.split(f"fn {name}", 1)[1].split(f"fn {next_name}", 1)[0]


def test_python_accepts_exact_rust_bridge_status_vocabulary():
    source = RUST_MAIN.read_text()
    production = source.split("#[cfg(test)]", 1)[0]

    # Direct source-map writes in read_local, plus the two helpers that return status strings.
    direct = set()
    for statement in re.findall(r"status\.insert\([^;]+;", production, re.DOTALL):
        # The first argument is the source id; every string in the value expression is a status.
        direct.update(re.findall(r'\"([a-z_]+)\"', statement.split(",", 1)[1]))
    read_statuses = set(re.findall(
        r'\"([a-z_]+)\"', _function(production, "read_status", "mark")))
    health = _function(production, "build_health_sources", "now_iso")
    health_statuses = set(re.findall(
        r'(?:if !present|fda == "ok"|else) \{ "([a-z_]+)" \}', health))
    health_statuses.update(re.findall(
        r'json!\(if recent_files_capable \{ "([a-z_]+)" \} else \{ "([a-z_]+)" \}\)',
        health)[0])
    emitted = direct | read_statuses | health_statuses

    assert emitted == {"ok", "disabled", "unavailable", "degraded", "needs_fda"}
    # `partial` is the compatibility spelling accepted from older/non-Rust Bridge producers.
    assert BRIDGE_STATUSES == emitted | {"partial"}
