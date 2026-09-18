"""Safety contracts for the stateful public-repository publisher."""
from pathlib import Path
import os
import shutil
import subprocess
import pytest


HERMES = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not (HERMES / "tools" / "publish-public.sh").is_file(),
    reason="The private publisher is not part of the public distribution.",
)


def _fixture(tmp_path: Path, docker_exit: int = 0):
    root = tmp_path / "source"
    tools = root / "sotto-hermes" / "tools"
    tools.mkdir(parents=True)
    shutil.copy2(HERMES / "tools" / "publish-public.sh", tools / "publish-public.sh")
    shutil.copy2(HERMES / "tools" / "verify-public-image.sh", tools / "verify-public-image.sh")
    generator = tools / "prepare-public-repo.sh"
    generator.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "mkdir -p \"$1\"\n"
        "printf 'FROM scratch\\n' > \"$1/Dockerfile\"\n"
        "printf '2026-09-16.deadbee\\n' > \"$1/VERSION\"\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" = info ]; then exit 0; fi\n"
        f"printf '%s\\n' \"$*\" >> \"${{DOCKER_CALLS}}\"\nexit {docker_exit}\n",
        encoding="utf-8",
    )
    for script in (tools / "publish-public.sh", tools / "verify-public-image.sh", generator, docker):
        script.chmod(0o755)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    calls = tmp_path / "docker-calls"
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
           "DOCKER_CALLS": str(calls)}
    return root, tools / "publish-public.sh", calls, env


def test_existing_work_parent_is_preserved(tmp_path):
    root, publisher, calls, env = _fixture(tmp_path)
    work_parent = tmp_path / "valuable-existing-directory"
    work_parent.mkdir()
    marker = work_parent / "keep-me"
    marker.write_text("valuable", encoding="utf-8")

    proc = subprocess.run(
        [publisher, "example/sotto", "--dry-run", "--fresh-history", "--work-dir", work_parent],
        cwd=root, env=env, text=True, capture_output=True,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert marker.read_text(encoding="utf-8") == "valuable"
    assert calls.exists(), "the dry run should still build the generated artifact"
    assert any(path.name.startswith("sotto-publish.") for path in work_parent.iterdir())


def test_work_parent_inside_source_is_rejected_without_creation(tmp_path):
    root, publisher, calls, env = _fixture(tmp_path)
    work_parent = root / "scratch" / "publish"

    proc = subprocess.run(
        [publisher, "example/sotto", "--dry-run", "--work-dir", work_parent],
        cwd=root, env=env, text=True, capture_output=True,
    )

    assert proc.returncode != 0
    assert "must be outside the source checkout" in proc.stderr
    assert not work_parent.exists()
    assert not calls.exists()


@pytest.mark.parametrize(("args", "message"), [
    (["--work-dir"], "--work-dir requires a directory value"),
    (["--work-dir", "--dry-run"], "--work-dir requires a directory value"),
    (["--template-url"], "--template-url requires a URL value"),
    (["--template-url", "--dry-run"], "--template-url requires a URL value"),
])
def test_value_taking_flags_fail_before_any_side_effect(tmp_path, args, message):
    root, publisher, calls, env = _fixture(tmp_path)

    proc = subprocess.run(
        [publisher, "example/sotto", *args], cwd=root, env=env, text=True, capture_output=True,
    )

    assert proc.returncode == 2
    assert message in proc.stderr
    assert not calls.exists(), "malformed arguments reached Docker"
    assert not (root / "--dry-run").exists(), "a consumed flag became a work directory"


def test_a_second_destination_is_refused_before_any_side_effect(tmp_path):
    """The destination decides where the whole distribution lands. A second positional used to win
    silently, so `publish-public.sh good/repo evil/repo` published the monorepo's distributable half
    onto evil/repo — a paste or a wrapper appending an argument was all it took."""
    root, publisher, calls, env = _fixture(tmp_path)
    work_parent = tmp_path / "work"

    proc = subprocess.run(
        [publisher, "good/repo", "evil/repo", "--dry-run", "--work-dir", work_parent],
        cwd=root, env=env, text=True, capture_output=True,
    )

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "only one <github-owner/repo> is accepted" in proc.stderr
    assert "evil/repo" not in proc.stdout, "the second destination reached the run"
    assert not work_parent.exists()
    assert not calls.exists(), "a second destination reached Docker"


def test_a_forged_ci_environment_cannot_publish_from_a_feature_branch(tmp_path):
    """The branch check used to be waived by GITHUB_ACTIONS=true + GITHUB_REF_NAME=main — two
    variables any shell can export, which is an assertion, not an attestation. The workflow earns
    the exemption the ordinary way, by checking out main."""
    root, publisher, calls, env = _fixture(tmp_path)
    subprocess.run(["git", "checkout", "-qb", "feature/review"], cwd=root, check=True)
    work_parent = tmp_path / "work"
    env.update({"GITHUB_ACTIONS": "true", "GITHUB_REF_NAME": "main"})

    proc = subprocess.run(
        [publisher, "example/sotto", "--work-dir", work_parent],
        cwd=root, env=env, text=True, capture_output=True,
    )

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "current branch: feature/review" in proc.stderr
    assert not work_parent.exists()
    assert not calls.exists()


def _add_origin(tmp_path: Path, root: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=root, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=root, check=True)
    return origin


def test_main_that_is_not_origin_main_refuses_a_production_publish(tmp_path):
    """Being ON main is half the promise. A local main with an unpushed commit stamps VERSION with a
    commit nobody can fetch, and the old check could not see it."""
    root, publisher, calls, env = _fixture(tmp_path)
    _add_origin(tmp_path, root)
    (root / "unpushed.txt").write_text("local only\n", encoding="utf-8")
    subprocess.run(["git", "add", "unpushed.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "unpushed"], cwd=root, check=True)
    work_parent = tmp_path / "work"

    proc = subprocess.run(
        [publisher, "example/sotto", "--work-dir", work_parent],
        cwd=root, env=env, text=True, capture_output=True,
    )

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "main is not origin/main" in proc.stderr
    assert "1 commit(s) not pushed" in proc.stderr
    assert not work_parent.exists()
    assert not calls.exists()


def test_main_in_sync_with_origin_is_publishable(tmp_path):
    """…and the check must not cost the ordinary case: a main that equals origin/main is exactly
    what a release is made from, so it goes through."""
    root, publisher, calls, env = _fixture(tmp_path)
    _add_origin(tmp_path, root)

    proc = subprocess.run(
        [publisher, "example/sotto", "--dry-run", "--work-dir", tmp_path / "work"],
        cwd=root, env=env, text=True, capture_output=True,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PREVIEW source" not in proc.stdout
    assert calls.exists()


def test_failed_preview_never_prunes_other_prefix_matching_workspaces(tmp_path):
    """A run owns only its unique workspace. Names and mtimes cannot prove that another matching
    sibling is stale: it may be a concurrent publish, retained failure, or unrelated operator data."""
    root, publisher, _calls, env = _fixture(tmp_path, docker_exit=1)
    work_parent = tmp_path / "work"
    work_parent.mkdir()
    preserved = []
    for index in range(4):
        sibling = work_parent / f"sotto-publish.unrelated-{index}"
        sibling.mkdir()
        marker = sibling / "owner-data"
        marker.write_text(f"unrelated-{index}", encoding="utf-8")
        preserved.append((marker, f"unrelated-{index}"))
    prior = work_parent / "sotto-publish.PRIOR1" / "tree"
    prior.mkdir(parents=True)
    (prior / "VERSION").write_text("failed-preview\n", encoding="utf-8")
    preserved.append((prior / "VERSION", "failed-preview\n"))
    live = work_parent / "sotto-publish.LIVE01" / "repo" / ".git"
    live.mkdir(parents=True)
    (live / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    preserved.append((live / "HEAD", "ref: refs/heads/main\n"))

    proc = subprocess.run(
        [publisher, "example/sotto", "--dry-run", "--work-dir", work_parent],
        cwd=root, env=env, text=True, capture_output=True,
    )

    assert proc.returncode != 0, proc.stdout + proc.stderr
    for marker, value in preserved:
        assert marker.read_text(encoding="utf-8") == value
    workspace_line = next(line for line in proc.stdout.splitlines()
                          if line.startswith("==> publish workspace: "))
    failed_workspace = Path(workspace_line.removeprefix("==> publish workspace: "))
    assert failed_workspace.is_dir(), "the failed preview workspace should remain inspectable"


def _seed_published_repo(tmp_path: Path, env: dict):
    """A published repo whose tree differs from the generated one ONLY in VERSION, with the
    publisher's https remote pointed at it."""
    remote = tmp_path / "public.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", seed], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=seed, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=seed, check=True)
    (seed / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (seed / "VERSION").write_text("2026-09-15.aaaaaaa\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-qm", "published"], cwd=seed, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)
    env.update({
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.file://{remote}.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/example/sotto.git",
        "SOTTO_PUBLISH_STATUS_FILE": str(tmp_path / "publish-status"),
    })
    return remote


def test_a_preview_stays_a_preview_when_only_version_changed(tmp_path):
    """The VERSION-only path adopts the PUBLISHED stamp, which erased the `.preview` marking a dirty
    or non-main source earned — so the status file a wrapper reads announced a production version
    for a tree that was never publishable."""
    root, publisher, _calls, env = _fixture(tmp_path)
    _seed_published_repo(tmp_path, env)
    generator = root / "sotto-hermes" / "tools" / "prepare-public-repo.sh"
    # Dirty the source: this run can only ever be a preview.
    generator.write_text(generator.read_text(encoding="utf-8") + "# dirty\n", encoding="utf-8")

    proc = subprocess.run(
        [publisher, "example/sotto", "--dry-run", "--work-dir", tmp_path / "work"],
        cwd=root, env=env, text=True, capture_output=True,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "only the generated VERSION changed" in proc.stdout
    status = (tmp_path / "publish-status").read_text(encoding="utf-8")
    assert "version=2026-09-15.aaaaaaa.preview" in status, status


def test_version_only_change_preserves_published_stamp_and_skips_push(tmp_path):
    root, publisher, calls, env = _fixture(tmp_path)
    remote = _seed_published_repo(tmp_path, env)
    before = subprocess.check_output(["git", "rev-parse", "refs/heads/main"], cwd=remote, text=True).strip()

    proc = subprocess.run(
        [publisher, "example/sotto", "--work-dir", tmp_path / "work"],
        cwd=root, env=env, text=True, capture_output=True,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "only the generated VERSION changed" in proc.stdout
    assert "already up to date" in proc.stdout
    status = (tmp_path / "publish-status").read_text(encoding="utf-8")
    assert "status=unchanged" in status and "version=2026-09-15.aaaaaaa" in status
    after = subprocess.check_output(["git", "rev-parse", "refs/heads/main"], cwd=remote, text=True).strip()
    assert after == before


def test_dirty_production_publish_refuses_before_workspace_or_docker(tmp_path):
    root, publisher, calls, env = _fixture(tmp_path)
    (root / "sotto-hermes" / "tools" / "prepare-public-repo.sh").write_text("dirty\n", encoding="utf-8")
    work_parent = tmp_path / "work"

    proc = subprocess.run(
        [publisher, "example/sotto", "--fresh-history", "--work-dir", work_parent],
        cwd=root, env=env, text=True, capture_output=True,
    )

    assert proc.returncode != 0
    assert "production publishing requires committed, clean main" in proc.stderr
    assert not work_parent.exists()
    assert not calls.exists()


def test_non_main_production_publish_refuses_before_workspace_or_docker(tmp_path):
    root, publisher, calls, env = _fixture(tmp_path)
    subprocess.run(["git", "checkout", "-qb", "feature/review"], cwd=root, check=True)
    work_parent = tmp_path / "work"

    proc = subprocess.run(
        [publisher, "example/sotto", "--fresh-history", "--work-dir", work_parent],
        cwd=root, env=env, text=True, capture_output=True,
    )

    assert proc.returncode != 0
    assert "current branch: feature/review" in proc.stderr
    assert not work_parent.exists()
    assert not calls.exists()


def test_failed_image_build_stops_before_destination_git_or_push(tmp_path):
    root, publisher, calls, env = _fixture(tmp_path, docker_exit=9)
    work_parent = tmp_path / "work"

    proc = subprocess.run(
        [publisher, "example/sotto", "--fresh-history", "--work-dir", work_parent],
        cwd=root, env=env, text=True, capture_output=True,
    )

    assert proc.returncode == 9, proc.stdout + proc.stderr
    assert calls.exists()
    run_dirs = list(work_parent.glob("sotto-publish.*"))
    assert len(run_dirs) == 1
    assert not (run_dirs[0] / "repo").exists(), "destination git repo was initialized before build passed"
    assert "pushing to" not in proc.stdout
