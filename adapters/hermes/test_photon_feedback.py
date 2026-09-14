"""Observe sidecar feedback across turn lifecycle; no Photon connection or model calls."""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(params=['managed', 'self-host'])
def photon(request, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', request.param)
    captured = {}

    class PhotonAdapter:
        def __init__(self):
            self.calls = []
            self._typing_paused = set()

        async def _sidecar_try(self, route, payload, label):
            self.calls.append((route, payload.copy()))

        async def stop_typing(self, chat_id):
            await self._sidecar_try('/typing', {'spaceId': chat_id, 'state': 'stop'}, 'stop_typing')

        async def _keep_typing(self, chat_id, interval=2.0, metadata=None, stop_event=None):
            # Hermes owns this lifecycle: periodic refresh, approval pauses, stop and cleanup.
            try:
                while stop_event is None or not stop_event.is_set():
                    if chat_id not in self._typing_paused:
                        await self.send_typing(chat_id, metadata)
                    await asyncio.sleep(interval)
            finally:
                await self.stop_typing(chat_id)

        def _record_sent_message(self, identifier):
            self.calls.append(('/receipt', identifier))

        async def _send_with_retry(self, chat_id, content, reply_to=None, metadata=None, **kwargs):
            return await self.send(chat_id, content, reply_to, metadata)

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.calls.append(('/send', {'spaceId': chat_id, 'text': content}))
            return types.SimpleNamespace(success=True)

    upstream = types.ModuleType('plugins.platforms.photon.adapter')
    upstream.PhotonAdapter = PhotonAdapter
    upstream.SendResult = types.SimpleNamespace
    upstream.register = lambda ctx: ctx.register_platform(adapter_factory=PhotonAdapter)
    for name in ['plugins', 'plugins.platforms', 'plugins.platforms.photon']:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, upstream.__name__, upstream)
    monkeypatch.setattr(sys.modules['plugins.platforms.photon'], 'adapter', upstream, raising=False)
    name = 'sotto_photon_feedback_test'
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / 'sotto_photon/__init__.py')
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, plugin)
    formatter = types.ModuleType(name + '.chatfmt')
    formatter.compact_handled = formatter.to_imessage = lambda value: value
    monkeypatch.setitem(sys.modules, formatter.__name__, formatter)
    gallery = types.ModuleType(name + '.gallery')
    gallery.prepare_prep = lambda _: None
    gallery.recovery_receipt = lambda *a: None
    monkeypatch.setitem(sys.modules, gallery.__name__, gallery)
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', 'chat')
    spec.loader.exec_module(plugin)
    plugin.register(types.SimpleNamespace(register_platform=lambda **kw: captured.update(kw)))
    monkeypatch.setattr(plugin, 'TYPING_START_DELAY_SECONDS', 0.01)
    instance = captured['adapter_factory']()
    instance.gallery_test = gallery
    return instance


def test_isolated_typing_calls_and_fast_replies_do_not_flash(photon):
    async def turn():
        await photon.send_typing('chat')  # isolated startup/progress callback
        task = asyncio.create_task(photon._keep_typing('chat'))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await photon.send('chat', 'Ready')
        await photon.send_typing('chat')  # late callback must not resurrect dots
    asyncio.run(turn())
    assert photon.calls == [('/send', {'spaceId': 'chat', 'text': 'Ready'})]


def test_long_turn_refreshes_continuously_until_reply_then_stays_stopped(photon):
    async def turn():
        task = asyncio.create_task(photon._keep_typing('chat', interval=0.005))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await photon.send('chat', 'Ready')
        await photon.send_typing('chat')
    asyncio.run(turn())
    starts = [c for c in photon.calls if c[1].get('state') == 'start']
    assert len(starts) >= 3  # no upstream five-second throttle between refresh ticks
    assert photon.calls[-2:] == [('/typing', {'spaceId': 'chat', 'state': 'stop'}),
                                 ('/send', {'spaceId': 'chat', 'text': 'Ready'})]
    assert photon._sotto_typing_runs == {}


def test_approval_pause_and_interruption_do_not_leave_or_restart_dots(photon):
    async def turn():
        stop = asyncio.Event()
        photon._typing_paused.add('chat')
        task = asyncio.create_task(photon._keep_typing('chat', interval=0.005, stop_event=stop))
        await asyncio.sleep(0.025)
        assert photon.calls == []
        photon._typing_paused.clear()
        await asyncio.sleep(0.02)
        stop.set()
        await task
        await photon.send_typing('chat')
        assert photon.calls[-1][1]['state'] == 'stop'
        assert photon._sotto_typing_runs == {}
        # A follow-up has a fresh owner, not a permanently muted chat.
        stop = asyncio.Event()
        task = asyncio.create_task(photon._keep_typing('chat', interval=0.005, stop_event=stop))
        await asyncio.sleep(0.025)
        assert photon.calls[-1][1]['state'] == 'start'
        stop.set()
        await task
    asyncio.run(turn())


def test_interruption_during_start_delay_never_starts_typing(photon):
    async def turn():
        stop = asyncio.Event()
        task = asyncio.create_task(photon._keep_typing('chat', stop_event=stop))
        stop.set()
        await task
    asyncio.run(turn())
    assert photon.calls == []


@pytest.mark.parametrize('acceptance', ['accepted', 'unknown', 'not_attempted'])
def test_focused_prep_gallery_does_not_duplicate_uncertain_send(photon, acceptance):
    calls = []
    photon.gallery_test.prepare_prep = lambda _: {'images': ['1', '2', '3', '4'], 'summary': 'Company'}
    def send(*args):
        calls.append(args)
        return acceptance == 'accepted', 'unconfirmed', {'acceptance': acceptance, 'message_id': 'parent', 'message_ids': ['child']}
    photon.gallery_test.send_once = send
    result = asyncio.run(photon._send_with_retry('chat', 'Complete original prep'))
    assert len(calls) == 1
    texts = [c for c in photon.calls if c[0] == '/send']
    if acceptance == 'accepted':
        assert result.success and result.message_id == 'parent'
        assert photon.calls == [('/receipt', 'parent'), ('/receipt', 'child')]
    elif acceptance == 'unknown':
        assert not result.success and not texts
    else:
        assert texts == [('/send', {'spaceId': 'chat', 'text': 'Complete original prep'})]


def test_ordinary_chat_uses_existing_delivery(photon):
    assert asyncio.run(photon._send_with_retry('chat', 'Hello')).success
    assert photon.calls == [('/send', {'spaceId': 'chat', 'text': 'Hello'})]


@pytest.mark.parametrize('acceptance', ['accepted', 'unknown'])
def test_gateway_recovery_never_resends_a_possible_gallery_as_text(photon, acceptance):
    photon.gallery_test.recovery_receipt = lambda *a: {'acceptance': acceptance, 'message_id': 'parent'}
    result = asyncio.run(photon.send('chat', 'Recovered reply plus original full prep'))
    assert result.success == (acceptance == 'accepted')
    assert photon.calls == []
