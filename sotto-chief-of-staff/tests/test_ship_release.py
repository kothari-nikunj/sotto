"""Exercise release ordering with real local Git remotes and stubbed build/signing tools."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


HERMES = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not (HERMES / "tools/ship.sh").exists(), reason="Private release wrapper is not exported."
)


def _git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _script(path, body):
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
    path.chmod(0o755)


def _fixture(tmp_path):
    root = tmp_path / "source"
    tools = root / "sotto-hermes/tools"
    app = root / "sotto-hermes/sotto-bridge/app"
    tools.mkdir(parents=True)
    app.mkdir(parents=True)
    for name in ("ship.sh", "verify-public-image.sh"):
        shutil.copy2(HERMES / "tools" / name, tools / name)
    (app / "VERSION").write_text("1.2.19\n")
    _script(app / "build-app.sh", 'echo build >> "$CALLS"\n')
    _script(app / "release.sh", 'echo "release $1" >> "$CALLS"\n')
    _script(tools / "publish-public.sh", '''
# The version must already be pushed before the long public-image build starts.
test "$(git -C "$ORIGIN" show main:sotto-hermes/sotto-bridge/app/VERSION)" = 1.2.20
echo publish >> "$CALLS"
# Reproduce another contributor landing work during that build.
git -C "$PEER" pull -q --ff-only origin main
echo newer > "$PEER/newer.txt"
git -C "$PEER" add newer.txt
git -C "$PEER" commit -qm newer
git -C "$PEER" push -q origin main
echo status=pushed > "$SOTTO_PUBLISH_STATUS_FILE"
''')
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "fixture")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "origin", "main")
    peer = tmp_path / "peer"
    subprocess.run(["git", "clone", "-q", str(origin), str(peer)], check=True)
    _git(peer, "config", "user.email", "test@example.invalid")
    _git(peer, "config", "user.name", "Test")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _script(bin_dir / "uname", 'echo Darwin\n')
    _script(bin_dir / "gh", 'echo v1.2.19\n')
    _script(bin_dir / "docker", 'exit 0\n')
    calls = tmp_path / "calls"
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
           "NOTARYTOOL_PROFILE": "test-profile", "CALLS": str(calls),
           "ORIGIN": str(origin), "PEER": str(peer)}
    return root, origin, calls, env


def _ship(root, env):
    return subprocess.run(
        ["bash", str(root / "sotto-hermes/tools/ship.sh"), "example/sotto",
         "--bridge", "v1.2.20", "--no-install", "--skip-tests"],
        cwd=root, env=env, capture_output=True, text=True,
    )


def test_main_advancing_during_public_build_does_not_strand_bridge(tmp_path):
    root, origin, calls, env = _fixture(tmp_path)
    proc = _ship(root, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls.read_text().splitlines() == ["publish", "build", "release v1.2.20"]
    assert _git(origin, "show", "main:newer.txt") == "newer"
    assert not (root / "newer.txt").exists(), "Never pull unverified source into a release"
    marker = root / "sotto-hermes/sotto-bridge/.last-ship"
    assert marker.read_text().strip() == _git(root, "rev-parse", "HEAD")


def test_rejected_version_push_stops_before_build_or_publication(tmp_path):
    root, origin, calls, env = _fixture(tmp_path)
    _script(origin / "hooks/pre-receive", "exit 1\n")
    proc = _ship(root, env)
    assert proc.returncode != 0
    assert "Nothing has been built or published" in proc.stderr
    assert "--bridge v1.2.20" in proc.stderr
    assert not calls.exists()
    assert _git(root, "show", "HEAD:sotto-hermes/sotto-bridge/app/VERSION") == "1.2.20"
    assert _git(origin, "show", "main:sotto-hermes/sotto-bridge/app/VERSION") == "1.2.19"


def test_missing_buildx_stops_before_version_commit(tmp_path):
    root, origin, calls, env = _fixture(tmp_path)
    _script(tmp_path / "bin/docker", '[ "$1" = info ]\n')
    before = _git(root, "rev-parse", "HEAD")
    proc = _ship(root, env)
    assert proc.returncode != 0
    assert "Docker Buildx is required" in proc.stderr
    assert _git(root, "rev-parse", "HEAD") == before
    assert _git(origin, "rev-parse", "main") == before
    assert not calls.exists()
