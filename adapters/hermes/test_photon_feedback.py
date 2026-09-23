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
            return True

        def _reactions_enabled(self):
            return True

        async def _add_reaction(self, chat_id, message_id, emoji):
            return await self._sidecar_try('/react', {'spaceId': chat_id, 'messageId': message_id,
                                                    'emoji': emoji}, 'add_reaction')

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


@pytest.mark.parametrize(('text', 'emoji'), [
    ('Can you research Maya before lunch?', '🔎'),
    ('Draft a reply to the meeting invite', '📝'),
    ('What is on my calendar tomorrow?', '📅'),
    ('Remember that I prefer afternoon calls', '🧠'),
    ('Thanks!', '❤️'),
    ('thank you so much 🙏', '❤️'),
    ('Thanks, but can you research Maya?', '🔎'),
    ('What changed?', '💭'),
    ('The bookshelf is useful', '💭'),  # no substring matches for "who's"
])
def test_contextual_tapbacks_replace_without_removal(photon, text, emoji):
    event = types.SimpleNamespace(text=text, source=types.SimpleNamespace(chat_id='chat'), message_id='incoming')
    async def turn():
        await photon.on_processing_start(event)
        await photon.on_processing_complete(event, types.SimpleNamespace(value='success'))
        await photon.on_processing_complete(event, types.SimpleNamespace(value='success'))
    asyncio.run(turn())
    expected = [emoji] if emoji == '❤️' else [emoji, '✅']
    assert photon.calls == [('/react', {'spaceId': 'chat', 'messageId': 'incoming', 'emoji': e}) for e in expected]


@pytest.mark.parametrize(('outcome', 'emoji'), [('failure', '⚠️'), ('cancelled', '⏸️')])
def test_failed_and_interrupted_turns_replace_instead_of_dislike_or_remove(photon, outcome, emoji):
    event = types.SimpleNamespace(text='Find out about Maya', source=types.SimpleNamespace(chat_id='chat'), message_id='incoming')
    async def turn():
        await photon.on_processing_start(event)
        await photon.on_processing_complete(event, types.SimpleNamespace(value=outcome))
    asyncio.run(turn())
    assert [p['emoji'] for _, p in photon.calls] == ['🔎', emoji]
    assert all(route == '/react' for route, _ in photon.calls)


def test_tapback_failure_does_not_block_reply_or_trigger_removal(photon):
    event = types.SimpleNamespace(text='A question', source=types.SimpleNamespace(chat_id='chat'), message_id='incoming')
    async def fail(*args):
        return False
    photon._add_reaction = fail
    async def turn():
        await photon.on_processing_start(event)
        assert (await photon.send('chat', 'Here is your answer')).success
        await photon.on_processing_complete(event, types.SimpleNamespace(value='success'))
    asyncio.run(turn())
    assert not hasattr(event, '_sotto_tapback')
    assert photon.calls == [('/send', {'spaceId': 'chat', 'text': 'Here is your answer'})]


def test_cancelled_uncertain_start_replaces_without_removal(photon):
    event = types.SimpleNamespace(text='A question', source=types.SimpleNamespace(chat_id='chat'), message_id='incoming')
    original = photon._add_reaction
    async def interrupted(*args):
        await original(*args)  # Provider may have accepted before the caller was cancelled.
        raise asyncio.CancelledError
    async def turn():
        photon._add_reaction = interrupted
        with pytest.raises(asyncio.CancelledError):
            await photon.on_processing_start(event)
        photon._add_reaction = original
        await photon.on_processing_complete(event, types.SimpleNamespace(value='cancelled'))
    asyncio.run(turn())
    assert [p['emoji'] for _, p in photon.calls] == ['💭', '⏸️']
    assert all(route == '/react' for route, _ in photon.calls)


@pytest.mark.parametrize('disabled', [True, False])
def test_no_lifecycle_reactions_when_disabled_or_target_missing(photon, disabled):
    photon._reactions_enabled = lambda: not disabled
    event = types.SimpleNamespace(text='A question', source=types.SimpleNamespace(chat_id='chat'),
                                  message_id='incoming' if disabled else None)
    async def turn():
        await photon.on_processing_start(event)
        for outcome in ('success', 'failure', 'cancelled'):
            await photon.on_processing_complete(event, types.SimpleNamespace(value=outcome))
    asyncio.run(turn())
    assert photon.calls == []


def test_reactions_stay_on_their_own_message(photon):
    events = [types.SimpleNamespace(text=text, source=types.SimpleNamespace(chat_id='chat'), message_id=identifier)
              for text, identifier in [('Research Maya', 'first'), ('Draft an email', 'second')]]
    async def turn():
        await photon.on_processing_start(events[0])
        await photon.on_processing_start(events[1])
        await photon.on_processing_complete(events[1], types.SimpleNamespace(value='success'))
        await photon.on_processing_complete(events[0], types.SimpleNamespace(value='cancelled'))
    asyncio.run(turn())
    assert [(p['messageId'], p['emoji']) for _, p in photon.calls] == [
        ('first', '🔎'), ('second', '📝'), ('second', '✅'), ('first', '⏸️')]
