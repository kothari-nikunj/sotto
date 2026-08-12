"""The public-repo guards, enforced at TEST time — a guard that only fires during ship.sh
lands in the OWNER's terminal mid-release (it has, twice). Same regexes as
tools/prepare-public-repo.sh; distributable paths only."""
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HERMES = os.path.dirname(ROOT)                    # sotto-hermes/ — where the generator lives
GENERATOR = os.path.join(HERMES, "tools", "prepare-public-repo.sh")

# The generator is a PRIVATE tool — `tools/` is excluded from the published tree — but these tests
# ship with it. A contributor cloning the public repo therefore cannot run them, and until Aug 2026
# they didn't skip, they FAILED: five red tests on a fresh clone with no way to reach a green
# baseline. An external reviewer hit exactly that. Skipping is the honest outcome — the tests still
# document what the guards promise, and they still RUN in the monorepo, where the generator lives
# and where the guarantee actually has to hold.
pytestmark = pytest.mark.skipif(
    not os.path.isfile(GENERATOR),
    reason="tools/prepare-public-repo.sh is private and not part of the published tree — "
           "these guard tests run in the source monorepo")

# Assembled from parts so this file's own source never matches the pattern it enforces — including
# the realistic fakes the tests below plant, which are built the same way.
_PARTS = ["AI" + "za", r"\bsk" + "-", "PRIVATE" + " KEY", "@gm" + "ail", "@fpv" + "ventures",
          # the full modern GitHub family (classic ghp_/gho_/ghu_/ghs_/ghr_ + fine-grained), Slack,
          # and AWS access key ids at their real shape: AKIA + exactly 16 uppercase alphanumerics.
          "gh" + r"[pousr]_[A-Za-z0-9]{16,}", "git" + r"hub_pat_[A-Za-z0-9_]{20,}",
          "xo" + r"x[baprs]-[A-Za-z0-9-]{10,}", "AK" + r"IA[0-9A-Z]{16}\b"]
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


# ── IGNORED GUARD — `cp -a` is gitignore-blind, so an ignored file inside a shipped directory ships
# The one that made this real: a local `adapters/hermes/.env` (gitignored at the monorepo root) sat
# inside a distributable directory. All six older guards passed it unless it happened to carry a
# vendor-prefixed key shape, and ship.sh's untracked check could not see it either — `git status
# --porcelain` never lists ignored files.

def _run_generator(target):
    return subprocess.run(["bash", GENERATOR, target, "kothari-nikunj/sotto"],
                          capture_output=True, text=True)


def test_a_gitignored_env_file_in_a_shipped_dir_fails_the_publish():
    planted = os.path.join(HERMES, "adapters", "hermes", ".env")
    assert not os.path.exists(planted), "a real .env is in the way — remove it first"
    target = tempfile.mkdtemp(prefix="sotto-dist-")
    os.rmdir(target)                                    # the generator refuses a non-empty target
    try:
        with open(planted, "w", encoding="utf-8") as f:
            f.write("BRIDGE" + "_TOKEN=" + "z" * 48 + "\n")
        # It IS gitignored in the source (that is exactly why nothing else caught it)…
        assert subprocess.run(["git", "check-ignore", "-q", planted], cwd=HERMES).returncode == 0
        proc = _run_generator(target)
        assert proc.returncode != 0, "a gitignored .env published:\n" + proc.stdout
        assert "IGNORED GUARD: FAIL" in proc.stdout
        assert "adapters/hermes/.env" in proc.stdout
    finally:
        if os.path.exists(planted):
            os.remove(planted)
        shutil.rmtree(target, ignore_errors=True)


def test_the_template_env_file_still_ships():
    """The guard must not swallow `.env.template` — it is a real, committed, shipped file."""
    target = tempfile.mkdtemp(prefix="sotto-dist-")
    os.rmdir(target)
    try:
        proc = _run_generator(target)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "IGNORED GUARD: PASS" in proc.stdout
        assert os.path.exists(os.path.join(target, "adapters", "hermes", ".env.template"))
    finally:
        shutil.rmtree(target, ignore_errors=True)


def test_the_secrets_guard_catches_an_opaque_token_assignment():
    """The vendor prefixes recognize a key by its brand. The keys Sotto documents now
    — EXA_API_KEY, PARALLEL_API_KEY, BRIDGE_TOKEN, SOTTO_TRIGGER_TOKEN, SOTTO_SETUP_CODE — are
    opaque strings, so the shape that catches them is the ASSIGNMENT. Planted as a NON-ignored file
    so this exercises the SECRETS guard rather than the IGNORED one."""
    planted = os.path.join(HERMES, "adapters", "hermes", "scratch-notes.md")
    assert not os.path.exists(planted)
    target = tempfile.mkdtemp(prefix="sotto-dist-")
    os.rmdir(target)
    try:
        with open(planted, "w", encoding="utf-8") as f:
            f.write("EXA" + "_API_KEY=" + "a1b2c3d4e5f6a7b8c9d0" + "\n")
        proc = _run_generator(target)
        assert proc.returncode != 0, "an opaque key assignment published:\n" + proc.stdout
        assert "SECRETS GUARD: FAIL" in proc.stdout
    finally:
        if os.path.exists(planted):
            os.remove(planted)
        shutil.rmtree(target, ignore_errors=True)


# ── SECRETS GUARD, the vendor prefixes ────────────────────────────────────────────────────────────
# A third external review found the prefix list stale: it knew `ghp_` (and nothing else GitHub has
# issued since), it did not know Slack at all, and its bare `AKIA` fired on prose. Every fixture
# below is assembled from string parts for the same reason the module-level pattern is — this file
# ships, and the guard greps the whole shipped tree including its own tests.

def _publish_with_planted_note(body: str):
    """Run the real generator with `body` sitting in a shipped (non-ignored) file, so what is being
    exercised is the SECRETS guard and not the IGNORED one. Returns the completed process."""
    # A unique name per call: the fixture must sit in a shipped, NON-ignored directory, and a fixed
    # one would collide with the older opaque-assignment test (and with a second checkout running the
    # suite at the same time).
    fd, planted = tempfile.mkstemp(prefix="scratch-fixture-", suffix=".md",
                                   dir=os.path.join(HERMES, "adapters", "hermes"))
    os.close(fd)
    target = tempfile.mkdtemp(prefix="sotto-dist-")
    os.rmdir(target)                                    # the generator refuses a non-empty target
    try:
        with open(planted, "w", encoding="utf-8") as f:
            f.write(body + "\n")
        return _run_generator(target)
    finally:
        if os.path.exists(planted):
            os.remove(planted)
        shutil.rmtree(target, ignore_errors=True)


@pytest.mark.parametrize("prefix", ["gh" + "p_", "gh" + "o_", "gh" + "u_", "gh" + "s_", "gh" + "r_"])
def test_the_secrets_guard_catches_every_github_token_prefix(prefix):
    """`ghp_` was never the whole family. An OAuth, user-to-server, server-to-server or refresh
    token leaks exactly as much, and the guard knew none of them."""
    proc = _publish_with_planted_note(prefix + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
    assert proc.returncode != 0, "a GitHub token published:\n" + proc.stdout
    assert "SECRETS GUARD: FAIL" in proc.stdout


def test_the_secrets_guard_catches_a_fine_grained_github_token():
    """The kind GitHub hands out by default now — and the kind the publish lane's own PAT is."""
    proc = _publish_with_planted_note(
        "git" + "hub_pat_" + "11ABCDEFG0aBcDeFgHiJkL" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6A7b8C9")
    assert proc.returncode != 0, "a fine-grained GitHub token published:\n" + proc.stdout
    assert "SECRETS GUARD: FAIL" in proc.stdout


@pytest.mark.parametrize("prefix", ["xo" + "xb-", "xo" + "xa-", "xo" + "xp-", "xo" + "xr-", "xo" + "xs-"])
def test_the_secrets_guard_catches_a_slack_token(prefix):
    """Sotto has no Slack integration, which is exactly why a Slack token here would be someone's
    own workspace credential pasted into a note — the accident this guard exists for."""
    proc = _publish_with_planted_note(prefix + "2109876543-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx")
    assert proc.returncode != 0, "a Slack token published:\n" + proc.stdout
    assert "SECRETS GUARD: FAIL" in proc.stdout


def test_the_secrets_guard_catches_an_aws_access_key_id():
    proc = _publish_with_planted_note("AK" + "IA" + "IOSFODNN7EXAMPLE" + " (an access key id)")
    assert proc.returncode != 0, "an AWS access key id published:\n" + proc.stdout
    assert "SECRETS GUARD: FAIL" in proc.stdout


@pytest.mark.parametrize("placeholder", [
    "gh" + "p_<your-token>",                     # the docs' own placeholder shape
    "git" + "hub_pat_xxx",
    "xo" + "xb-<workspace-token>",
    "AK" + "IA is the prefix every AWS access key id starts with",   # prose, not a key
    "GITHUB" + "_TOKEN=${" + "GITHUB_TOKEN}",    # a variable reference, not a value
])
def test_the_new_prefixes_stay_quiet_on_placeholders(placeholder):
    """The tightening must not cost the placeholder-friendly design: a guard that fires on the
    documentation it ships is a guard someone deletes."""
    proc = _publish_with_planted_note(placeholder)
    assert proc.returncode == 0, "a placeholder tripped the guard:\n" + proc.stdout
    assert "SECRETS GUARD: PASS" in proc.stdout


def test_the_secrets_guard_leaves_documented_placeholders_alone():
    """…and the docs' own examples (`<your-gemini-key>`, `${VAR}`, `xxx`, `…`) must never trip it —
    a guard that cries wolf on the template it ships is a guard someone disables."""
    target = tempfile.mkdtemp(prefix="sotto-dist-")
    os.rmdir(target)
    try:
        proc = _run_generator(target)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "SECRETS GUARD: PASS" in proc.stdout
    finally:
        shutil.rmtree(target, ignore_errors=True)


# ── WORKFLOW GUARD — the tree may contain exactly ONE workflow, the CI the generator writes ───────
# The guard used to fail on ANY `.github/` path, which also meant the published repo shipped with no
# CI at all: an external reviewer cloned it, found five red guard tests (now skipped, above) and no
# workflow to tell him whether main worked. The generator therefore WRITES `.github/workflows/ci.yml`
# and the guard's rule became "that file, and nothing else under any .github, and it names no
# credential" — the private mint/publish workflows still cannot ship.

def test_the_published_tree_ships_exactly_one_workflow():
    target = tempfile.mkdtemp(prefix="sotto-dist-")
    os.rmdir(target)                                    # the generator refuses a non-empty target
    try:
        proc = _run_generator(target)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "WORKFLOW GUARD: PASS" in proc.stdout
        ci = os.path.join(target, ".github", "workflows", "ci.yml")
        assert os.path.isfile(ci), "the published repo would have no CI"
        found = []
        for dirpath, _dirnames, filenames in os.walk(os.path.join(target, ".github")):
            found += [os.path.relpath(os.path.join(dirpath, fn), target) for fn in filenames]
        assert found == [os.path.join(".github", "workflows", "ci.yml")], found
    finally:
        shutil.rmtree(target, ignore_errors=True)


def test_the_shipped_ci_workflow_names_no_credential():
    """It runs tests and nothing else — a CI file in a public repo that reads a secret is either a
    mistake or an exfiltration, and neither belongs in a tree this script publishes unattended."""
    target = tempfile.mkdtemp(prefix="sotto-dist-")
    os.rmdir(target)
    try:
        proc = _run_generator(target)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        with open(os.path.join(target, ".github", "workflows", "ci.yml"), encoding="utf-8") as f:
            ci = f.read()
        for forbidden in ("secrets.", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"):
            assert forbidden not in ci, f"the shipped ci.yml mentions {forbidden!r}"
        # …and it is the green baseline: the two suites plus the validator, on the image's Python.
        assert "pytest tests" in ci and "validate_skills.py" in ci
        assert "pytest runtime/trigger-receiver" in ci
        assert "requirements-dev.txt" in ci
        assert os.path.isfile(os.path.join(target, "requirements-dev.txt")), \
            "ci.yml installs from a file the generator never copies"
    finally:
        shutil.rmtree(target, ignore_errors=True)


def test_a_second_workflow_anywhere_in_the_tree_fails_the_publish():
    """The guard is no longer 'no .github', so prove the narrower rule still bites: a workflow that
    rides along inside a shipped directory must stop the publish."""
    planted_dir = os.path.join(HERMES, "adapters", "hermes", ".github", "workflows")
    assert not os.path.exists(os.path.join(HERMES, "adapters", "hermes", ".github"))
    target = tempfile.mkdtemp(prefix="sotto-dist-")
    os.rmdir(target)
    try:
        os.makedirs(planted_dir)
        with open(os.path.join(planted_dir, "rides-along.yml"), "w", encoding="utf-8") as f:
            f.write("name: rides along\non: push\n")
        proc = _run_generator(target)
        assert proc.returncode != 0, "a second workflow published:\n" + proc.stdout
        assert "WORKFLOW GUARD: FAIL" in proc.stdout
        assert "rides-along.yml" in proc.stdout
    finally:
        shutil.rmtree(os.path.join(HERMES, "adapters", "hermes", ".github"), ignore_errors=True)
        shutil.rmtree(target, ignore_errors=True)


def test_the_shipped_gitignore_ignores_a_deployers_env_file():
    """The .gitignore that ships IS sotto-hermes/.gitignore. Until Aug 2026 it ignored build junk but
    not `adapters/hermes/.env` — which the monorepo root ignores and the public repo therefore did
    not — so a deployer who followed LOCAL-SETUP.md saw their own credentials file sitting untracked
    in `git status`. Asserted through git itself, not a substring match."""
    target = tempfile.mkdtemp(prefix="sotto-dist-")
    os.rmdir(target)
    try:
        proc = _run_generator(target)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        subprocess.run(["git", "init", "-q"], cwd=target, check=True)
        ignored = subprocess.run(["git", "check-ignore", "-q", "adapters/hermes/.env"], cwd=target)
        assert ignored.returncode == 0, "the shipped .gitignore does not ignore adapters/hermes/.env"
        # …and it must not swallow the template beside it, which is a real shipped file.
        kept = subprocess.run(["git", "check-ignore", "-q", "adapters/hermes/.env.template"], cwd=target)
        assert kept.returncode != 0, "the shipped .gitignore ignores .env.template"
    finally:
        shutil.rmtree(target, ignore_errors=True)


# ── DOC TRANSFORMS — a dead `sed` is drift no guard can see ───────────────────────────────────────
# The generator rewrites the shipped docs with seds anchored to the source doc's exact wording. The
# seven guards check what must NOT be in the tree; none of them can tell that a rewrite silently
# stopped matching. In Aug 2026 LOCAL-SETUP.md was restructured into "option A: download / option B:
# build from source" and the block went stale in three ways at once: a title rewrite that matched
# nothing, a download blurb substituted INTO option B's `cargo build` fence, and the same `--doctor`
# command emitted twice. Every one of those shipped, green. This asserts the SHAPE of the produced
# file instead, so the next restructure fails here rather than in a deployer's terminal.

def test_the_public_local_setup_offers_exactly_one_way_to_get_the_bridge():
    target = tempfile.mkdtemp(prefix="sotto-dist-")
    os.rmdir(target)                                    # the generator refuses a non-empty target
    try:
        proc = _run_generator(target)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        with open(os.path.join(target, "LOCAL-SETUP.md"), encoding="utf-8") as f:
            doc = f.read()

        # This repo ships no Bridge source, so nothing may tell a reader to build one.
        for forbidden in ("cargo build", "Option B", "option B", "build from source",
                          "rustup.rs", "core/target/release"):
            assert forbidden not in doc, f"the shipped LOCAL-SETUP still mentions {forbidden!r}"

        # …and the one way that IS offered has to be whole: the app, its bundled engine, and a single
        # verify command pointed at it.
        bridged = "/Applications/Sotto Bridge.app/Contents/Resources/sotto-bridged"
        assert bridged in doc, "option A (download the signed app) did not survive the rewrite"
        doctor = [ln.strip() for ln in doc.splitlines() if "--doctor" in ln]
        assert len(doctor) == 1, f"expected exactly one --doctor invocation, got {doctor}"
        assert bridged in doctor[0], f"--doctor points somewhere that does not exist here: {doctor[0]}"
    finally:
        shutil.rmtree(target, ignore_errors=True)


def test_every_doc_the_readme_links_actually_ships():
    """The generator's copy list is an ALLOWLIST, so adding a doc to the repo does not publish it —
    a LICENSE added and never copied would leave the public repo unlicensed while every guard went
    green. This walks the README's own relative links and asserts each one exists in the tree."""
    target = tempfile.mkdtemp(prefix="sotto-dist-")
    os.rmdir(target)                                    # the generator refuses a non-empty target
    try:
        proc = subprocess.run(["bash", GENERATOR, target, "kothari-nikunj/sotto"],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        readme = open(os.path.join(target, "README.md"), encoding="utf-8").read()
        links = set(re.findall(r"\]\((?!https?:|#|mailto:)([^)]+)\)", readme))
        missing = sorted(l for l in links
                         if not os.path.exists(os.path.join(target, l.split("#")[0])))
        assert not missing, (
            f"README links these, but the generator never copies them: {missing}\n"
            "Add each to tools/prepare-public-repo.sh's copy list.")
    finally:
        shutil.rmtree(target, ignore_errors=True)
