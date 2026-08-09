"""The public-repo guards, enforced at TEST time — a guard that only fires during ship.sh
lands in the OWNER's terminal mid-release (it has, twice). Same regexes as
tools/prepare-public-repo.sh; distributable paths only."""
import json
import os
import re
import shutil
import subprocess
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HERMES = os.path.dirname(ROOT)                    # sotto-hermes/ — where the generator lives
GENERATOR = os.path.join(HERMES, "tools", "prepare-public-repo.sh")

# Assembled from parts so this file's own source never matches the pattern it enforces.
_PARTS = ["AI" + "za", r"\bsk" + "-", "gh" + "p_", "PRIVATE" + " KEY", "AK" + "IA",
          "@gm" + "ail", "@fpv" + "ventures"]
SECRET_RE = re.compile("|".join(_PARTS))


def test_no_secret_patterns_in_distributable_tree():
    """Mirrors prepare-public-repo.sh's SECRETS GUARD. Use a fake freemail like
    yahoo.com in fixtures — the guard greps the whole shipped tree, tests included."""
    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".pytest_cache")]
        for fn in filenames:
            if not fn.endswith((".py", ".md", ".json", ".yaml", ".yml", ".sh")):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if SECRET_RE.search(line):
                            hits.append(f"{os.path.relpath(path, ROOT)}:{i}: {line.strip()[:80]}")
            except (OSError, UnicodeDecodeError):
                continue
    assert not hits, (
        "secrets-guard patterns in the distributable tree (these WILL fail ship.sh):\n  "
        + "\n  ".join(hits)
    )


# ── CORPUS GUARD — the Golden Corpus is real user data and must never ship ────────────────────────
# "Scrubbed" never means "shareable" (docs/plans/golden-corpus.md). The corpus is gitignored, so a
# clean checkout has none — but the OWNER's checkout does, and `cp -a` is gitignore-blind. These
# tests stand in for the one thing nobody can retry: a published inbox.

# Mirrors the find expression in prepare-public-repo.sh's CORPUS GUARD.
CORPUS_FIND = ["(", "-path", "*/evals/corpus/*", "-o", "-name", "corpus-v*",
               "-o", "-name", "*.map.json", "-o", "-name", "labels.yaml",
               "-o", "-name", "labels.draft.yaml", ")"]


def _seed_corpus(root: str) -> str:
    """A corpus exactly where a real one lives, plus a stray identity map."""
    d = os.path.join(root, "evals", "corpus", "corpus-v1", "days")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "day-00.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "day-00", "inputs": {}}, f)
    with open(os.path.join(root, "evals", "corpus", "corpus-v1", "labels.yaml"), "w", encoding="utf-8") as f:
        f.write("reviewed: true\n")
    with open(os.path.join(root, "evals", "corpus-v1.map.json"), "w", encoding="utf-8") as f:
        json.dump({"key": "00"}, f)
    return os.path.join(root, "evals", "corpus")


def test_the_corpus_guard_is_not_vacuous():
    """A guard nobody has seen fail is a guard nobody knows works: the same find expression, run
    against a tree that DOES contain a corpus, must find it."""
    tmp = tempfile.mkdtemp(prefix="corpus-guard-")
    try:
        _seed_corpus(tmp)
        hits = subprocess.run(["find", tmp] + CORPUS_FIND + ["-print"],
                              capture_output=True, text=True, check=True).stdout
        assert "corpus-v1" in hits and "labels.yaml" in hits and ".map.json" in hits
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_corpus_in_the_working_tree_never_reaches_the_distribution():
    """The real generator, run against a source tree that HAS a corpus: the corpus must be gone and
    the CORPUS GUARD must say so. This is the exact situation on the owner's machine."""
    seeded = _seed_corpus(ROOT)
    target = tempfile.mkdtemp(prefix="sotto-dist-")
    os.rmdir(target)                                    # the generator refuses a non-empty target
    try:
        proc = subprocess.run(["bash", GENERATOR, target, "kothari-nikunj/sotto"],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "CORPUS GUARD: PASS" in proc.stdout
        leaked = subprocess.run(["find", target] + CORPUS_FIND + ["-print"],
                                capture_output=True, text=True, check=True).stdout.strip()
        assert not leaked, f"corpus artifacts shipped:\n{leaked}"
        # positive control: the HARNESS ships (it is code, not data) — the guard is path-shaped.
        assert os.path.exists(os.path.join(target, "sotto-chief-of-staff", "evals", "run_golden.py"))
        assert os.path.exists(os.path.join(target, "sotto-chief-of-staff", "tools",
                                           "build_golden_corpus.py"))
    finally:
        shutil.rmtree(seeded, ignore_errors=True)
        shutil.rmtree(target, ignore_errors=True)
        if os.path.exists(os.path.join(ROOT, "evals", "corpus-v1.map.json")):
            os.remove(os.path.join(ROOT, "evals", "corpus-v1.map.json"))


def test_corpus_paths_are_gitignored():
    """Belt to the generator's braces: `git add -A` on the owner's machine must not stage a corpus."""
    for rel in ("sotto-chief-of-staff/evals/corpus/corpus-v1/days/day-00.json",
                "sotto-chief-of-staff/evals/corpus-v1.map.json"):
        proc = subprocess.run(["git", "check-ignore", "-q", rel], cwd=HERMES)
        assert proc.returncode == 0, f"{rel} is NOT gitignored — a corpus could be committed"
