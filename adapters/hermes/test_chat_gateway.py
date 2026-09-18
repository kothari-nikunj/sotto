import importlib.util
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('chat_gateway_test', HERE / 'chat_gateway.py')
chat_gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chat_gateway)


def test_injects_only_exact_local_sotto_entry_without_mutating_config():
    original = {
        'sotto-local': {'url': 'http://127.0.0.1:8787/mcp',
                        'headers': {'Authorization': 'Bearer bridge'}},
        'other': {'url': 'http://127.0.0.1:8787/mcp', 'headers': {}},
    }
    result = chat_gateway.add_private_header(original, 'private',
                                              'http://127.0.0.1:8787/mcp')
    assert result['sotto-local']['headers']['X-Sotto-Chat-Send'] == 'private'
    assert result['sotto-local']['keepalive_interval'] == 30.0
    assert 'X-Sotto-Chat-Send' not in result['other']['headers']
    assert original['sotto-local']['headers'] == {'Authorization': 'Bearer bridge'}
    assert 'keepalive_interval' not in original['sotto-local']


def test_loader_never_writes_persisted_config(tmp_path):
    path = tmp_path / 'config.json'
    saved = {'sotto-local': {'url': 'http://127.0.0.1:8787/mcp', 'headers': {}}}
    path.write_text(json.dumps(saved, sort_keys=True))
    before = path.read_bytes()
    module = SimpleNamespace(_load_mcp_config=lambda: json.loads(path.read_text()))
    chat_gateway.install_loader('private', 'http://127.0.0.1:8787/mcp', lambda name: module)
    assert module._load_mcp_config()['sotto-local']['headers']['X-Sotto-Chat-Send'] == 'private'
    assert path.read_bytes() == before


@pytest.mark.parametrize('url', [
    'http://localhost:8787/mcp', 'http://127.0.0.1:8788/mcp',
    'https://127.0.0.1:8787/mcp', 'http://127.0.0.1:8787/mcp/',
])
def test_arbitrary_sotto_url_never_gets_private_header(url):
    config = {'sotto-local': {'url': url, 'headers': {}}}
    result = chat_gateway.add_private_header(config, 'private',
                                              'http://127.0.0.1:8787/mcp')
    assert 'X-Sotto-Chat-Send' not in result['sotto-local']['headers']
    assert 'keepalive_interval' not in result['sotto-local']


def fake_health_module():
    class Health:
        async def _keepalive_probe(self):
            self.calls.append('original')

        async def _refresh_tools(self):
            self.calls.append('refresh')

    return SimpleNamespace(MCPServerHealthMixin=Health)


def fake_server(health, *, name='sotto-local', url='http://127.0.0.1:8787/mcp', locked=False):
    server = health.MCPServerHealthMixin()
    server.name = name
    server._config = {'url': url}
    server.calls = []
    server._rpc_lock = SimpleNamespace(locked=lambda: locked)
    server._pending_refresh_tasks = set()
    server._reconnect_event = SimpleNamespace(set=lambda: server.calls.append('reconnect'))
    server.mark_suspect = lambda reason: server.calls.append(('suspect', reason))
    return server


def test_exact_local_keepalive_refreshes_while_other_servers_keep_original_probe():
    health = fake_health_module()
    chat_gateway.install_local_tool_refresh('http://127.0.0.1:8787/mcp', lambda name: health)
    local = fake_server(health)
    other = fake_server(health, name='other')
    asyncio.run(local._keepalive_probe())
    asyncio.run(other._keepalive_probe())
    assert local.calls == ['original', 'refresh']
    assert other.calls == ['original']


def test_locked_keepalive_defers_refresh_until_outer_rpc_lock_is_released():
    health = fake_health_module()
    chat_gateway.install_local_tool_refresh('http://127.0.0.1:8787/mcp', lambda name: health)

    async def exercise():
        local = fake_server(health)
        local._rpc_lock = asyncio.Lock()

        async def refresh():
            async with local._rpc_lock:
                local.calls.append('refresh')

        local._refresh_tools = refresh
        async with local._rpc_lock:
            await asyncio.wait_for(local._keepalive_probe(), timeout=1)
            assert local.calls == ['original'] and len(local._pending_refresh_tasks) == 1
            await local._keepalive_probe()
            assert len(local._pending_refresh_tasks) == 1
        await asyncio.wait_for(asyncio.gather(*local._pending_refresh_tasks), timeout=1)
        assert local.calls == ['original', 'original', 'refresh']

    asyncio.run(exercise())


def test_local_refresh_is_bounded_and_failure_propagates(monkeypatch):
    health = fake_health_module()
    chat_gateway.install_local_tool_refresh('http://127.0.0.1:8787/mcp', lambda name: health)
    local = fake_server(health)
    seen = []

    async def bounded(awaitable, timeout):
        await awaitable
        seen.append(timeout)
        raise TimeoutError('bounded')

    monkeypatch.setattr(chat_gateway.asyncio, 'wait_for', bounded)
    with pytest.raises(TimeoutError, match='bounded'):
        asyncio.run(local._keepalive_probe())
    assert seen == [30.0]
    assert local.calls[-2][0] == 'suspect'
    assert local.calls[-1] == 'reconnect'


def test_deferred_refresh_failure_requests_reconnect_and_is_retrieved():
    health = fake_health_module()

    async def fail_refresh(self):
        raise RuntimeError('refresh failed')

    health.MCPServerHealthMixin._refresh_tools = fail_refresh
    chat_gateway.install_local_tool_refresh('http://127.0.0.1:8787/mcp', lambda name: health)

    async def exercise():
        local = fake_server(health, locked=True)
        await local._keepalive_probe()
        task = next(iter(local._pending_refresh_tasks))
        local._rpc_lock = SimpleNamespace(locked=lambda: False)
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], RuntimeError)
        assert local.calls[0] == 'original'
        assert local.calls[-2][0] == 'suspect'
        assert local.calls[-1] == 'reconnect'

    asyncio.run(exercise())


def test_main_erases_child_environment_and_runs_pinned_gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'path', list(sys.path))
    entrypoint = tmp_path / 'hermes'
    entrypoint.write_text('# fixture')
    env = {'SOTTO_CHAT_SEND_TOKEN': 'private', 'PYTHONPATH': '/untrusted',
           'PYTHONHOME': '/python', 'HOME': '/home/sotto', 'PORT': '9999'}
    loader = SimpleNamespace(_load_mcp_config=lambda: {
        'sotto-local': {'url': 'http://127.0.0.1:9999/mcp', 'headers': {}}})
    health = fake_health_module()
    seen = {}

    def run_path(path, run_name):
        seen.update(path=path, run_name=run_name, env=dict(env), argv=list(sys.argv),
                    loaded=loader._load_mcp_config())

    monkeypatch.setattr(sys, 'argv', list(sys.argv))
    chat_gateway.main(env=env, importer=lambda name: health if name.endswith('mcp_tool_health') else loader,
                      run_path=run_path,
                      nondumpable_fn=lambda: seen.setdefault('nondumpable', True),
                      root=tmp_path, entrypoint=entrypoint)
    assert seen['nondumpable'] is True
    assert seen['argv'] == [str(entrypoint), 'gateway']
    assert seen['env'] == {'PORT': '9999', 'HOME': '/home/sotto'}
    assert seen['loaded']['sotto-local']['headers']['X-Sotto-Chat-Send'] == 'private'


def test_missing_token_or_pinned_loader_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'path', list(sys.path))
    entrypoint = tmp_path / 'hermes'
    entrypoint.write_text('# fixture')
    with pytest.raises(RuntimeError, match='required'):
        chat_gateway.main(env={}, root=tmp_path, entrypoint=entrypoint)
    env = {'SOTTO_CHAT_SEND_TOKEN': 'private'}
    with pytest.raises(RuntimeError, match='loader'):
        chat_gateway.main(env=env, importer=lambda name: (_ for _ in ()).throw(ImportError()),
                          nondumpable_fn=lambda: None, root=tmp_path, entrypoint=entrypoint)
    assert 'SOTTO_CHAT_SEND_TOKEN' not in env


def test_real_pinned_loader_interface_when_checkout_is_available(monkeypatch):
    checkout = HERE.parents[3] / 'hermes-pinned-proof'
    loader = checkout / 'tools' / 'mcp_tool_config.py'
    if not loader.is_file():
        pytest.skip('pinned Hermes proof checkout unavailable')
    monkeypatch.syspath_prepend(str(checkout))
    monkeypatch.setenv('HERMES_SAFE_MODE', '1')
    try:
        module = chat_gateway.install_loader('private', 'http://127.0.0.1:8787/mcp')
        loaded = module._load_mcp_config()
    except (ImportError, RuntimeError) as exc:
        pytest.skip(f'pinned Hermes dependencies unavailable: {exc}')
    assert callable(module._load_mcp_config)
    assert loaded == {}
