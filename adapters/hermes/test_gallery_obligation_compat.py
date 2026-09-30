"""The pin-checked gateway seam supplies identity from Hermes, never caller metadata."""
import asyncio
import hashlib
import importlib.util
from pathlib import Path
import types

import pytest


def _compat():
    path = Path(__file__).with_name('gallery_obligation_compat.py')
    spec = importlib.util.spec_from_file_location('gallery_obligation_test_compat', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('name', ['base.py', 'run_startup.py'])
def test_pinned_gateway_patch_is_idempotent_and_rejects_drift(tmp_path, monkeypatch, name):
    compat = _compat()
    original = 'async def source():\n' + compat.OLD[name] + '    return result\n'
    path = tmp_path / name
    path.write_text(original)
    monkeypatch.setitem(compat.PINS, name, hashlib.sha256(original.encode()).hexdigest())
    compat.check(path)
    compat.patch(path)
    first = path.read_bytes()
    assert compat.NEW[name] in first.decode()
    compat.check(path)
    compat.patch(path)
    assert path.read_bytes() == first
    path.write_bytes(first + b'\n# unreviewed upstream change\n')
    with pytest.raises(RuntimeError, match='differs from the reviewed pin'):
        compat.patch(path)


def test_gateway_overwrites_forged_caller_identity(tmp_path, monkeypatch):
    compat = _compat()
    name = 'base.py'
    original = ('async def issue(metadata, _obligation_id):\n' + compat.OLD[name]
                + '        return result\n')
    path = tmp_path / name
    path.write_text(original)
    monkeypatch.setitem(compat.PINS, name, hashlib.sha256(original.encode()).hexdigest())
    compat.patch(path)
    observed = []

    class Adapter:
        async def _send_with_retry(self, **kwargs):
            observed.append(kwargs['metadata'])
            return True

    namespace = {
        'delivery_adapter': Adapter(),
        'event': types.SimpleNamespace(source=types.SimpleNamespace(chat_id='owner')),
        'text_content': 'Synthetic reply',
        '_reply_anchor_for_event': lambda event: 'inbound',
    }
    exec(compile(path.read_text(), str(path), 'exec'), namespace)
    asyncio.run(namespace['issue']({'sotto_obligation_id': 'f' * 24}, 'a' * 24))
    asyncio.run(namespace['issue']({'sotto_obligation_id': 'f' * 24}, None))
    assert observed == [{'sotto_obligation_id': 'a' * 24}, {}]
