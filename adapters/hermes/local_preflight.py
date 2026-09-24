#!/usr/bin/env python3
"""Fail before changing a local installation when Sotto's runtime is missing."""
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys


def check(root: Path) -> list[str]:
    problems = []
    if sys.version_info < (3, 12):
        problems.append('Python 3.12 or newer is required')
    for line in (root / 'requirements.in').read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        package, expected = line.split('==', 1)
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = None
        if installed != expected:
            problems.append(f'{package}: install the version in requirements.txt')
    return problems


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    problems = check(root)
    if not problems:
        return 0
    print('Sotto Python environment is not ready. Nothing was installed.', file=sys.stderr)
    for problem in problems:
        print(f'  {problem}', file=sys.stderr)
    print('From the Sotto repo root, run:\n'
          '  python3.12 -m venv .venv\n'
          '  .venv/bin/python -m pip install --require-hashes -r requirements.txt\n'
          '  source .venv/bin/activate\n'
          'Then rerun the installer in that shell.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
