"""Reject an external Hermes runtime in the built image, preserving adapted MIT code."""

import importlib.metadata
import importlib.util
from pathlib import Path
import re
import shutil

# These identify the retired agent, not every unrelated package containing "hermes".
DISTRIBUTIONS = frozenset({"hermes", "hermes-agent", "hermes-ai"})
MODULES = ("hermes_cli", "hermes_agent")
EXECUTABLES = ("hermes", "hermes-agent")
LEGACY_PATHS = (
    "opt/hermes",
    "opt/hermes-agent",
    "app/hermes",
    "app/hermes-agent",
    "app/adapters/hermes",
    "usr/local/lib/hermes-agent",
    "usr/local/bin/hermes",
    "usr/local/bin/hermes-agent",
    "app/hermes-image-commit.txt",
    "app/hermes-image-manifest.txt",
    "app/hermes-image-version.txt",
)


def assert_external_hermes_absent(root, package_names, which, find_spec):
    installed = {re.sub(r"[-_.]+", "-", name).lower() for name in package_names}
    assert not (installed & DISTRIBUTIONS), "external Hermes Python distribution is installed"
    for executable in EXECUTABLES:
        assert which(executable) is None, f"external Hermes launcher is on PATH: {executable}"
    for name in MODULES:
        assert find_spec(name) is None, f"external Hermes module is importable: {name}"
    for relative in LEGACY_PATHS:
        path = root / relative
        assert not path.exists() and not path.is_symlink(), f"legacy Hermes artifact is packaged: {relative}"


def main():
    assert_external_hermes_absent(
        Path("/"),
        (item.metadata["Name"] for item in importlib.metadata.distributions()),
        shutil.which,
        importlib.util.find_spec,
    )
    print("no external Hermes distribution, module, executable or checkout: PASS", flush=True)


if __name__ == "__main__":
    main()
