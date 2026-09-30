"""Hermes update hints must use installed provenance, not moving upstream HEAD."""

import os
from pathlib import Path
import re
import subprocess

import pytest


START = Path(__file__).with_name('start.sh')


def stable_version(line):
    source = START.read_text()
    function = re.search(r'^stable_hermes_version\(\) \{\n.*?^\}', source, re.M | re.S)
    assert function, 'start.sh must expose the version normalization used at boot'
    result = subprocess.run(['bash', '-c', function.group() + '\nstable_hermes_version'],
                            input=line + '\n', capture_output=True, text=True, check=True)
    return result.stdout.strip()


def test_upstream_movement_does_not_change_displayed_installed_version():
    prefix = 'Hermes Agent v0.21.0 (2026.8.31)'
    built = prefix + ' · upstream 890db533 · local 245e4800'
    running = prefix + ' · upstream 959c7649 · local 245e4800'
    assert stable_version(built) == stable_version(running)
    assert 'local 245e4800' in stable_version(running)
    assert stable_version(running.replace('245e4800', 'other123')) != stable_version(built)


@pytest.mark.parametrize(('running', 'image', 'warning'), [
    ('245e4800', '245e4800', None),
    ('different', '245e4800', 'installed Hermes checkout differs from the image pin'),
    ('', '245e4800', 'installed Hermes checkout provenance could not be verified'),
    ('245e4800', '', 'installed Hermes checkout provenance could not be verified'),
])
def test_checkout_warning_uses_stable_commits_without_volume_refresh_advice(running, image, warning):
    source = START.read_text()
    diagnostic = re.search(
        r'^if \[ -z "\$RUN_HCOMMIT" \].*?^fi$', source, re.M | re.S)
    assert diagnostic, 'start.sh must check installed checkout provenance'
    result = subprocess.run(['bash', '-c', diagnostic.group()],
                            env={**os.environ, 'RUN_HCOMMIT': running, 'IMG_HCOMMIT': image},
                            capture_output=True, text=True, check=True)
    output = result.stdout + result.stderr
    if warning is None:
        assert output == ''
    else:
        assert warning in output
    assert 'volume seed is stale' not in output
    assert 'SOTTO_REFRESH_HERMES' not in output
