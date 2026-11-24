"""The personal-data publish guard: the public tree carries no phone number or email domain a real
person could own. Planted values are assembled from parts so this shipped file stays clean itself."""
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

AGENT_ROOT = Path(__file__).resolve().parents[2]
GUARD = AGENT_ROOT / "tools" / "personal_data_guard.py"
GENERATOR = AGENT_ROOT / "tools" / "prepare-public-repo.sh"
pytestmark = pytest.mark.skipif(not GUARD.is_file(), reason="the publish guard is a private source-repo tool")

REAL_PHONE = "+1 (206) " + "834-" + "2291"
REAL_DOMAIN = "realfirm" + ".io"
REAL_DIGITS = "206834" + "2291"


def _guard():
    spec = importlib.util.spec_from_file_location("personal_data_guard", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Shapes a real number or address takes in fixtures; each must be a hit. Built from parts.
MISSES = ["+44 20 " + "7123 4567", "+91 9876" + "5 43210", "chat_+1" + REAL_DIGITS, "id-" + REAL_DIGITS,
          "206/834/" + "2291", "415-555-" + "9876", "+1 206 834 " + "22 91", REAL_DIGITS + "@s.whatsapp.net",
          "jane.realperson@" + "yahoo.com", "ceo@" + "stripe.com", "dana@" + REAL_DOMAIN]
FICTIONAL = ["+1 202-555-0143", "+14155550100", "+19998887777", "+44 20 7946 0958", "+44 7700 900123",
             "dana@example.com", "lee@cedar.example", "kim@corp.test", "x@bad.invalid", "0123456789abcdef" * 4, "57203355-53c7-4e68-a3af-7e2652085210"]


def _hits(tmp_path, lines, allowed=frozenset()):
    (tmp_path / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return _guard().scan(str(tmp_path), set(allowed))


def test_every_real_looking_shape_is_a_hit(tmp_path):
    hits = _hits(tmp_path, MISSES)
    assert {int(hit.split(":")[1]) for hit in hits} == set(range(1, len(MISSES) + 1)), hits


def test_reserved_and_documented_fictional_values_pass(tmp_path):
    assert _hits(tmp_path, FICTIONAL) == []


# Review of PR #984: typographic separators and the "+44 (0)20" trunk-zero form hid real numbers.
TYPOGRAPHIC_MISSES = ["206\u2013834\u2013" + "2291", "206  834  " + "2291", "(206)\u00a0834-" + "2291",
                      "+1\u202f206\u2011834\u2011" + "2291", "+44 (0)20 " + "7123 4567", "+44\u00a0(0) 20  " + "7123 4567"]
TYPOGRAPHIC_FICTIONAL = ["202\u2013555\u20130143", "(202)\u00a0555-0143", "202  555  0143", "+44 (0)20 7946 0958",
                         "+44\u00a0(0)7700 900123"]


def test_typographic_separators_and_a_trunk_zero_do_not_hide_a_number(tmp_path):
    hits = _hits(tmp_path, TYPOGRAPHIC_MISSES)
    assert {int(hit.split(":")[1]) for hit in hits} == set(range(1, len(TYPOGRAPHIC_MISSES) + 1)), hits
    assert _hits(tmp_path, TYPOGRAPHIC_FICTIONAL) == []


def test_a_file_that_is_not_utf8_is_still_scanned(tmp_path):
    line = "Zo\u00eb,dana@" + REAL_DOMAIN + "," + REAL_PHONE + "\n"
    (tmp_path / "export.csv").write_bytes(line.encode("latin-1"))
    assert _guard().scan(str(tmp_path), set()) == [
        "export.csv:1: phone " + REAL_PHONE, "export.csv:1: email dana@" + REAL_DOMAIN]
    (tmp_path / "export.csv").write_bytes("Zo\u00eb,dana@example.com,+1 202-555-0143\n".encode("latin-1"))
    assert _guard().scan(str(tmp_path), set()) == []
    (tmp_path / "export.csv").write_bytes(b"\xff\x00" + REAL_DIGITS.encode())   # a binary, as before
    assert _guard().scan(str(tmp_path), set()) == []


def test_allowlist_entries_are_exact_numbers_exact_addresses_or_placeholder_domains(tmp_path):
    lines = [f"a@{REAL_DOMAIN} b@mail.{REAL_DOMAIN} {REAL_DIGITS}", "ceo@" + "stripe.com", "no-reply@" + "stripe.com"]
    hits = _hits(tmp_path, lines, {"@" + REAL_DOMAIN, REAL_DIGITS, "no-reply@" + "stripe.com"})
    assert hits == ["fixture.txt:2: email ceo@" + "stripe.com"]


def test_every_allowlist_entry_has_a_known_form():
    for entry in _guard().load_allowlist():
        assert (entry.isdigit() and len(entry) >= len(REAL_DIGITS)) or (
            "." in entry and entry.count("@") == 1 and " " not in entry), entry


@pytest.mark.skipif(not GENERATOR.is_file(), reason="needs the private generator")
def test_the_publish_fails_on_a_planted_fixture_and_passes_on_the_real_tree(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(AGENT_ROOT, source, symlinks=True, ignore=shutil.ignore_patterns(
        "node_modules", "dist", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "target"))
    (tmp_path / "bin").mkdir()
    shutil.copy2(AGENT_ROOT.parent / "bin" / "check", tmp_path / "bin" / "check")
    clean = subprocess.run(["bash", str(source / "tools/prepare-public-repo.sh"), str(tmp_path / "clean"),
                            "example/sotto"], capture_output=True, text=True)
    assert "PERSONAL-DATA GUARD: PASS" in clean.stdout, clean.stdout[-3000:]
    planted = source / "sotto-chief-of-staff" / "tests" / "planted_fixture.py"
    planted.write_text(f'PHONE = "{REAL_PHONE}"\nEMAIL = "dana@{REAL_DOMAIN}"\n', encoding="utf-8")
    proc = subprocess.run(["bash", str(source / "tools/prepare-public-repo.sh"), str(tmp_path / "out"),
                           "example/sotto"], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "PERSONAL-DATA GUARD: FAIL" in proc.stdout
    rel = os.path.join("sotto-chief-of-staff", "tests", "planted_fixture.py")
    assert f"{rel}:1: phone {REAL_PHONE}" in proc.stdout
    assert f"{rel}:2: email dana@{REAL_DOMAIN}" in proc.stdout
