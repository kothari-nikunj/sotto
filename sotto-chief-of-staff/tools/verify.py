#!/usr/bin/env python3
"""One credential-free Python/release contract, shared by source and distribution.

Run with the interpreter whose dependencies were installed from requirements-dev.txt.
Bridge compilation/tests remain an explicit platform step; no deployment is performed.
"""
from pathlib import Path
import subprocess
import sys
import tempfile


def commands(root):
    python = sys.executable
    pack = root / 'sotto-chief-of-staff'
    return [
        (root, [python, '-m', 'ruff', 'check', '.']),
        (pack, [python, '-m', 'pytest', 'tests', '-q']),
        (pack, [python, 'tools/validate_skills.py']),
        (root, [python, '-m', 'pytest', 'runtime/trigger-receiver', '-q']),
        (root, [python, '-m', 'pytest', 'adapters/hermes', '-q']),
        (root, [python, '-m', 'pytest', 'cloud/model-proxy', '-q']),
        (root, [python, '-m', 'pytest', 'cloud/accounts', '-q']),
        (root, [python, 'adapters/hermes/reconcile_crons.py', '--help']),
        (root, [python, 'adapters/hermes/configure_mcp.py', '--help']),
    ]


def main():
    root = Path(__file__).resolve().parents[2]
    for cwd, argv in commands(root):
        print('[verify] ' + ' '.join(argv[1:]), flush=True)
        subprocess.run(argv, cwd=cwd, check=True)
    for directory in ('adapters', 'tools'):
        for script in sorted((root / directory).rglob('*.sh')):
            subprocess.run(['bash', '-n', str(script)], check=True)
    generator = root / 'tools/prepare-public-repo.sh'
    if generator.is_file():
        with tempfile.TemporaryDirectory(prefix='sotto-verify-') as scratch:
            subprocess.run(['bash', str(generator), str(Path(scratch) / 'distribution'),
                            'kothari-nikunj/sotto'], cwd=root, check=True)
    print('[verify] shared runtime and release contracts passed', flush=True)


if __name__ == '__main__':
    main()
