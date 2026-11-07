"""Path contract test for run.sh; replaces Docker and needs no Git checkout."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest


HERE = Path(__file__).resolve().parent


class RunScriptPathTests(unittest.TestCase):
    def test_paths_are_absolute_from_unrelated_exported_tree_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exported = root / "export"
            # Reproduce only the path-bearing files; never copy node_modules, a
            # local venv, build products or other working-tree data into a unit test.
            context = exported / "sotto-agent"
            fixture_copy = context / "runtime/sotto-chat/test/fixtures/composed-runtime"
            fixture_copy.mkdir(parents=True)
            for name in ("run.sh", "Dockerfile"):
                shutil.copy2(HERE / name, fixture_copy / name)
            shutil.copy2(HERE.parents[2] / "Dockerfile.runtime",
                         context / "runtime/sotto-chat/Dockerfile.runtime")
            # The independent fixture copy is the script's source of truth.
            fake_bin = root / "bin"
            fake_bin.mkdir()
            args_log = root / "docker-args.log"
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DOCKER_ARGS_LOG\"\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            env = os.environ.copy()
            env.update({"PATH": f"{fake_bin}:{env.get('PATH', '')}",
                        "DOCKER_ARGS_LOG": str(args_log),
                        "BASE_IMAGE": "fixture-base", "FIXTURE_IMAGE": "fixture-test"})
            completed = subprocess.run(
                [str(fixture_copy / "run.sh")], cwd=root,
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            calls = args_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(calls), 4)
            context = (exported / "sotto-agent").resolve()
            expected_runtime = context / "runtime/sotto-chat/Dockerfile.runtime"
            expected_fixture = (fixture_copy / "Dockerfile").resolve()
            self.assertIn(f"--file {expected_runtime}", calls[0])
            self.assertTrue(calls[0].endswith(f" {context}"))
            self.assertIn(f"--file {expected_fixture}", calls[1])
            self.assertTrue(calls[1].endswith(f" {context}"))
            self.assertIn("--build-arg BASE_IMAGE=fixture-base", calls[1])
            self.assertIn("fixture-test", calls[2])
            self.assertIn("supervisor_preflight_smoke.py", calls[2])
            self.assertIn("--init", calls[2])
            self.assertIn("composed_failure_smoke.py", calls[3])
            self.assertIn("fixture-test", calls[3])
            self.assertNotIn("git rev-parse", completed.stderr)


if __name__ == "__main__":
    unittest.main()
