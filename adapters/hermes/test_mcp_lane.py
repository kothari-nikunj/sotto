"""Shared MCP configuration must never contain the private chat send grant."""
import hashlib
import hmac
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import yaml


def test_mcp_config_only_persists_read_credential(tmp_path, monkeypatch):
    path = Path(__file__).with_name('configure_mcp.py')
    spec = importlib.util.spec_from_file_location('configure_mcp_lane', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = tmp_path / 'config.yaml'
    monkeypatch.setenv('SOTTO_CHAT_SEND_TOKEN', 'private-chat-test')
    monkeypatch.setattr('sys.argv', ['configure_mcp.py', '--url', 'http://127.0.0.1:8787/mcp',
        '--token', 'test-root', '--derive-mcp', '--config', str(config)])
    module.main()
    entry = yaml.safe_load(config.read_text())['mcp_servers']['sotto-local']
    assert 'X-Sotto-Chat-Send' not in entry['headers']
    assert 'private-chat-test' not in config.read_text()
    assert entry['headers']['Authorization'] == 'Bearer ' + module.derive_mcp_token('test-root')


def test_start_boot_mcp_registration_binds_leading_dash_root_token(tmp_path):
    """Run the actual boot shell block, including its argument quoting, against the real CLI."""
    here = Path(__file__).parent
    start = (here / 'start.sh').read_text()
    beginning = 'if [ -n "${BRIDGE_TOKEN:-}" ]; then\n'
    assert start.count(beginning) == 1
    block = start.split(beginning, 1)[1].split('\nfi\n', 1)[0]
    script = ('runtime_python() { shift; "$TEST_PYTHON" "$TEST_CONFIGURE_MCP" "$@"; }\n'
              + beginning + block + '\nfi\n')
    token = '-leading-dash-root'
    env = os.environ.copy() | {'HOME': str(tmp_path), 'BRIDGE_TOKEN': token,
                               'PORT': '8787', 'TEST_PYTHON': sys.executable,
                               'TEST_CONFIGURE_MCP': str(here / 'configure_mcp.py')}
    result = subprocess.run(['bash', '-e', '-c', script], env=env, text=True,
                            capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
    content = (tmp_path / '.hermes/config.yaml').read_text()
    entry = yaml.safe_load(content)['mcp_servers']['sotto-local']
    assert entry['url'] == 'http://127.0.0.1:8787/mcp'
    expected = hmac.new(token.encode(), b'sotto-mcp', hashlib.sha256).hexdigest()
    assert entry['headers']['Authorization'] == 'Bearer ' + expected
    assert token not in content + result.stdout + result.stderr
