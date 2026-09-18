"""Shared MCP configuration must never contain the private chat send grant."""
import importlib.util
from pathlib import Path

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
