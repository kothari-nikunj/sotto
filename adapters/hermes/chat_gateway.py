#!/usr/local/lib/hermes-agent/venv/bin/python
"""Launch the managed chat gateway with a process-private local-send credential."""
import asyncio
import copy
import importlib
import os
from pathlib import Path
import runpy
import sys

HERMES_ROOT = Path('/usr/local/lib/hermes-agent')
HERMES_ENTRYPOINT = HERMES_ROOT / 'hermes'
TOKEN_ENV = 'SOTTO_CHAT_SEND_TOKEN'
HEADER = 'X-Sotto-Chat-Send'
REFRESH_SECONDS = 30.0


def expected_url(env=os.environ):
    return f'http://127.0.0.1:{env.get("PORT", "8787")}/mcp'


def add_private_header(config, token, url):
    """Return an isolated config copy; inject only into the exact local Sotto endpoint."""
    if not isinstance(config, dict):
        return config
    result = copy.deepcopy(config)
    entry = result.get('sotto-local')
    if not isinstance(entry, dict) or entry.get('url') != url:
        return result
    headers = entry.get('headers', {})
    if not isinstance(headers, dict):
        return result
    entry['headers'] = {**headers, HEADER: token}
    entry['keepalive_interval'] = REFRESH_SECONDS
    return result


def install_loader(token, url, importer=importlib.import_module):
    """Patch the pinned Hermes loader in memory, failing closed on interface drift."""
    try:
        module = importer('tools.mcp_tool_config')
    except Exception as exc:
        raise RuntimeError('pinned Hermes MCP loader is unavailable') from exc
    original = getattr(module, '_load_mcp_config', None)
    if not callable(original):
        raise RuntimeError('pinned Hermes _load_mcp_config interface is unavailable')

    def load_with_private_send():
        return add_private_header(original(), token, url)

    module._load_mcp_config = load_with_private_send
    return module


def install_local_tool_refresh(url, importer=importlib.import_module):
    """Use the existing keepalive cadence to refresh the exact local Bridge tool list."""
    try:
        module = importer('tools.mcp_tool_health')
        health = module.MCPServerHealthMixin
        original = health._keepalive_probe
    except Exception as exc:
        raise RuntimeError('pinned Hermes MCP health interface is unavailable') from exc
    if not callable(original) or not callable(getattr(health, '_refresh_tools', None)):
        raise RuntimeError('pinned Hermes MCP health interface is unavailable')

    async def bounded_refresh(server):
        try:
            await asyncio.wait_for(server._refresh_tools(), timeout=REFRESH_SECONDS)
        except Exception as exc:
            server.mark_suspect(f'periodic tool refresh failed: {type(exc).__name__}: {exc}')
            server._reconnect_event.set()
            raise

    async def keepalive_or_refresh(server):
        if server.name != 'sotto-local' or server._config.get('url') != url:
            return await original(server)
        await original(server)
        # The lifecycle loop calls probes while holding _rpc_lock, and _refresh_tools acquires that
        # same lock. Defer onto the current loop so it starts after this probe returns and the caller
        # releases the lock. Suspect probes do not hold it and can propagate failure directly.
        if server._rpc_lock.locked():
            if any(not task.done() and task.get_name() == 'sotto-chat-tool-refresh'
                   for task in server._pending_refresh_tasks):
                return None

            async def refresh_after_probe():
                await bounded_refresh(server)

            task = asyncio.create_task(refresh_after_probe(), name='sotto-chat-tool-refresh')
            server._pending_refresh_tasks.add(task)

            def finished(done):
                server._pending_refresh_tasks.discard(done)
                if not done.cancelled():
                    done.exception()  # retrieve failures; reconnect was requested above

            task.add_done_callback(finished)
            return None
        return await bounded_refresh(server)

    health._keepalive_probe = keepalive_or_refresh
    return module


def sanitize_process(env=os.environ):
    token = env.pop(TOKEN_ENV, '')
    if not token:
        raise RuntimeError(f'{TOKEN_ENV} is required')
    inherited_paths = [path for path in env.get('PYTHONPATH', '').split(os.pathsep) if path]
    sys.path[:] = [path for path in sys.path if path not in inherited_paths]
    for key in ('PYTHONPATH', 'PYTHONHOME'):
        env.pop(key, None)
    return token


def main(*, env=os.environ, importer=importlib.import_module,
         run_path=runpy.run_path, nondumpable_fn=None, root=HERMES_ROOT,
         entrypoint=HERMES_ENTRYPOINT):
    # Capture and erase the credential before any Hermes module is imported.
    token = sanitize_process(env)
    if nondumpable_fn is None:
        from managed_exec import nondumpable as nondumpable_fn  # noqa: PLC0415 - after erasing the credential
    nondumpable_fn()
    root = Path(root).resolve()
    entrypoint = Path(entrypoint).resolve()
    if entrypoint.parent != root or not entrypoint.is_file():
        raise RuntimeError('pinned Hermes entrypoint is unavailable')
    root_text = str(root)
    if root_text in sys.path:
        sys.path.remove(root_text)
    sys.path.insert(0, root_text)
    install_loader(token, expected_url(env), importer)
    install_local_tool_refresh(expected_url(env), importer)
    sys.argv = [str(entrypoint), 'gateway']
    run_path(str(entrypoint), run_name='__main__')


if __name__ == '__main__':
    main()
