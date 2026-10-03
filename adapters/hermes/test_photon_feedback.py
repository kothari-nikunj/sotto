"""Observe sidecar feedback across turn lifecycle; no Photon connection or model calls."""
import asyncio
import importlib.util
import os
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

import pytest


@pytest.fixture(params=['managed', 'self-host'])
def photon(request, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', request.param)
    captured = {}

    class PhotonAdapter:
        def __init__(self):
            self.calls = []
            self._typing_paused = set()

        async def handle_message(self, event):
            self.calls.append(('/model_turn', event.text))

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
    # Managed activation requires the same exact owner allowlist as production.
    monkeypatch.setenv('PHOTON_ALLOWED_USERS', 'chat')
    spec.loader.exec_module(plugin)
    plugin.register(types.SimpleNamespace(register_platform=lambda **kw: captured.update(kw)))
    monkeypatch.setattr(plugin, 'TYPING_START_DELAY_SECONDS', 0.01)
    instance = captured['adapter_factory']()
    instance.gallery_test = gallery
    instance.plugin_test = plugin
    return instance


@pytest.mark.parametrize('text,expected', [
    ('Give me my evening brief', {'kind': 'evening', 'refresh': False}),
    ('send me the morning brief again!', {'kind': 'morning', 'refresh': False}),
    ('Show me my brief?', {'kind': 'auto', 'refresh': False}),
    ('Could you give me my evening brief, please?', {'kind': 'evening', 'refresh': False}),
    ('Could you give me my evening brief please?', {'kind': 'evening', 'refresh': False}),
    ('Can you show me the morning brief?', {'kind': 'morning', 'refresh': False}),
    ('Refresh my evening brief', {'kind': 'evening', 'refresh': True}),
    ('update the brief', {'kind': 'auto', 'refresh': True}),
    ('Could I get my evening brief, please?', {'kind': 'evening', 'refresh': False}),
    ("I'd like my morning brief", {'kind': 'morning', 'refresh': False}),
    ('Give me my evening brief and text Sam', None),
    ('What is in my evening brief?', None),
])
def test_brief_request_is_narrow(photon, text, expected):
    event = types.SimpleNamespace(text=text)
    assert photon.plugin_test.brief_request(event) == expected
    event.media_urls = ['photo']
    assert photon.plugin_test.brief_request(event) is None


@pytest.mark.parametrize('text,expected', [
    ('Give me my evening brief and tell me what is on my calendar',
     ({'kind': 'evening', 'refresh': False}, 'tell me what is on my calendar')),
    ('What is on my calendar and could I get my morning brief?',
     ({'kind': 'morning', 'refresh': False}, 'What is on my calendar')),
    ('Refresh my brief and remind me to call Sam',
     ({'kind': 'auto', 'refresh': True}, 'remind me to call Sam')),
    ('Give me my brief and a summary of the week', None),
    ('Give me my brief and send it to Alex', None),
    ('Give me my brief and explain that to Sam', None),
    ('Give me my brief and tell me about it', None),
    ('Give me my brief and send the brief to Alex', None),
    ('Give me my brief and give me my evening brief', None),
    ('Give me my brief and tell me a joke and text Sam', None),
    ("Don't give me my brief and tell me about it", None),
    ('“Give me my brief” and tell me why that phrase is odd', None),
    ('What did I say about "give me my brief" and my calendar?', None),
    ('What is in my evening brief and when will it arrive?', None),
])
def test_brief_turn_extracts_only_independent_explicit_request(photon, text, expected):
    event = types.SimpleNamespace(text=text)
    assert photon.plugin_test.brief_turn(event) == expected
    event.reply_to_text = 'Give me my brief'
    assert photon.plugin_test.brief_turn(event) is None
    event.reply_to_text = None
    event.media_urls = ['attachment']
    assert photon.plugin_test.brief_turn(event) is None


@pytest.mark.parametrize('text,remainder', [
    ('Give me my evening brief and tell me about tomorrow', 'tell me about tomorrow'),
    ('Tell me about tomorrow and give me my evening brief', 'Tell me about tomorrow'),
])
def test_compound_brief_queues_and_preserves_other_chat_clause(
        photon, tmp_path, monkeypatch, text, remainder):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture')
    monkeypatch.setattr(photon.plugin_test, '_brief_receiver_config', lambda: ('loopback', {}))
    seen = []
    monkeypatch.setattr(photon.plugin_test, '_admit_brief',
                        lambda config, payload: seen.append(payload) or 'queued')
    event = types.SimpleNamespace(text=text,
        message_id='compound-1', source=types.SimpleNamespace(chat_id='chat', user_id='chat',
                                                              chat_type='dm'))
    asyncio.run(photon.handle_message(event))
    assert seen == [{'kind': 'evening', 'refresh': False, 'message_id': 'compound-1',
                     'owner': 'chat', 'chat_id': 'chat'}]
    assert photon.calls == [('/send', {'spaceId': 'chat', 'text': 'Your evening brief is queued.'}),
                            ('/model_turn', remainder)]
    assert event.text == text


def test_compound_redelivery_does_not_repeat_chat_clause(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture')
    monkeypatch.setattr(photon.plugin_test, '_brief_receiver_config', lambda: ('loopback', {}))
    monkeypatch.setattr(photon.plugin_test, '_admit_brief', lambda *_: 'known_request')
    event = types.SimpleNamespace(text='Give me my brief and tell me a joke', message_id='compound-1',
        source=types.SimpleNamespace(chat_id='chat', user_id='chat', chat_type='dm'))
    asyncio.run(photon.handle_message(event))
    assert photon.calls == [('/send', {'spaceId': 'chat',
                                      'text': photon.plugin_test.BRIEF_REPLAY_OTHER})]


def test_compound_without_provider_id_never_admits(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture')
    monkeypatch.setattr(photon.plugin_test, '_brief_receiver_config', lambda: ('loopback', {}))
    monkeypatch.setattr(photon.plugin_test, '_admit_brief',
                        lambda *_: pytest.fail('no stable provider identity'))
    event = types.SimpleNamespace(text='Give me my brief and tell me a joke', message_id=None,
        source=types.SimpleNamespace(chat_id='chat', user_id='chat', chat_type='dm'))
    asyncio.run(photon.handle_message(event))
    assert photon.calls == [('/send', {'spaceId': 'chat',
                                      'text': photon.plugin_test.BRIEF_UNAVAILABLE
                                              + photon.plugin_test.BRIEF_OTHER_NOT_PROCESSED})]


@pytest.mark.parametrize('status,notice', [
    (None, 'BRIEF_UNAVAILABLE'),
    ('needs_context', None),
    ('pending_delivery', 'BRIEF_PENDING_DELIVERY'),
])
def test_compound_nonadmission_explicitly_defers_other_clause(
        photon, tmp_path, monkeypatch, status, notice):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture')
    monkeypatch.setattr(photon.plugin_test, '_brief_receiver_config', lambda: ('loopback', {}))
    monkeypatch.setattr(photon.plugin_test, '_admit_brief', lambda *_: status)
    event = types.SimpleNamespace(text='Give me my brief and tell me a joke', message_id='compound-3',
        source=types.SimpleNamespace(chat_id='chat', user_id='chat', chat_type='dm'))
    asyncio.run(photon.handle_message(event))
    prefix = (getattr(photon.plugin_test, notice) if notice else
              'I need a connected source before I can prepare your brief.')
    assert photon.calls == [('/send', {'spaceId': 'chat',
                                      'text': prefix + photon.plugin_test.BRIEF_OTHER_NOT_PROCESSED})]


def test_compound_without_receiver_keeps_complete_chat_turn(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture')
    monkeypatch.setattr(photon.plugin_test, '_brief_receiver_config', lambda: None)
    event = types.SimpleNamespace(text='Give me my brief and tell me a joke', message_id='compound-2',
        source=types.SimpleNamespace(chat_id='chat', user_id='chat', chat_type='dm'))
    asyncio.run(photon.handle_message(event))
    assert photon.calls == [('/model_turn', event.text)]


def test_brief_request_queues_without_model_turn(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture')
    monkeypatch.setattr(photon.plugin_test, '_brief_receiver_config', lambda: ('loopback', {}))
    seen = []
    monkeypatch.setattr(photon.plugin_test, '_admit_brief',
                        lambda config, payload: seen.append(payload) or 'queued')
    event = types.SimpleNamespace(text='Give me my evening brief', message_id='inbound-1',
        source=types.SimpleNamespace(chat_id='chat', user_id='chat', chat_type='dm'))
    asyncio.run(photon.handle_message(event))
    assert seen == [{'kind': 'evening', 'refresh': False, 'message_id': 'inbound-1',
                     'owner': 'chat', 'chat_id': 'chat'}]
    assert photon.calls == [('/send', {'spaceId': 'chat', 'text': 'Your evening brief is queued.'})]


def test_brief_unknown_admission_never_runs_model(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture')
    monkeypatch.setattr(photon.plugin_test, '_brief_receiver_config', lambda: ('loopback', {}))
    monkeypatch.setattr(photon.plugin_test, '_admit_brief', lambda *_: None)
    event = types.SimpleNamespace(text='Refresh my morning brief', message_id='inbound-2',
        source=types.SimpleNamespace(chat_id='chat', user_id='chat', chat_type='dm'))
    asyncio.run(photon.handle_message(event))
    assert photon.calls == [('/send', {'spaceId': 'chat', 'text': photon.plugin_test.BRIEF_UNAVAILABLE})]


def test_prior_uncertain_gallery_waits_without_model_turn(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture')
    monkeypatch.setattr(photon.plugin_test, '_brief_receiver_config', lambda: ('loopback', {}))
    monkeypatch.setattr(photon.plugin_test, '_admit_brief', lambda *_: 'pending_delivery')
    event = types.SimpleNamespace(text='Send me my evening brief again', message_id='inbound-4',
        source=types.SimpleNamespace(chat_id='chat', user_id='chat', chat_type='dm'))
    asyncio.run(photon.handle_message(event))
    assert photon.calls == [('/send', {'spaceId': 'chat',
                                      'text': photon.plugin_test.BRIEF_PENDING_DELIVERY})]


def test_known_inbound_redelivery_has_no_second_reply_or_model_turn(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture')
    monkeypatch.setattr(photon.plugin_test, '_brief_receiver_config', lambda: ('loopback', {}))
    monkeypatch.setattr(photon.plugin_test, '_admit_brief', lambda *_: 'known_request')
    event = types.SimpleNamespace(text='Give me my evening brief', message_id='original-id',
        source=types.SimpleNamespace(chat_id='chat', user_id='chat', chat_type='dm'))
    asyncio.run(photon.handle_message(event))
    assert photon.calls == []


def test_no_receiver_binding_leaves_existing_model_turn(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture')
    monkeypatch.setattr(photon.plugin_test, '_brief_receiver_config', lambda: None)
    event = types.SimpleNamespace(text='Give me my evening brief', message_id='inbound-3',
        source=types.SimpleNamespace(chat_id='chat', user_id='chat', chat_type='dm'))
    asyncio.run(photon.handle_message(event))
    assert photon.calls == [('/model_turn', event.text)]


def test_brief_request_respects_owner_boundary(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture')
    monkeypatch.setattr(photon.plugin_test, '_brief_receiver_config',
                        lambda: pytest.fail('foreign user must not inspect receiver binding'))
    event = types.SimpleNamespace(text='Give me my evening brief', message_id='foreign',
        source=types.SimpleNamespace(chat_id='stranger', user_id='stranger', chat_type='dm'))
    asyncio.run(photon.handle_message(event))
    if os.environ['SOTTO_DEPLOYMENT_MODE'] == 'managed':
        assert photon.calls == []
    else:
        assert photon.calls == [('/model_turn', event.text)]


def test_brief_receiver_binding_requires_exact_loopback_and_private_header(photon, monkeypatch):
    loader = types.ModuleType('tools.mcp_tool_config')
    tools = types.ModuleType('tools')
    monkeypatch.setitem(sys.modules, 'tools', tools)
    monkeypatch.setitem(sys.modules, 'tools.mcp_tool_config', loader)
    monkeypatch.setenv('PORT', '8787')
    entry = {'url': 'http://127.0.0.1:8787/mcp',
             'headers': {'Authorization': 'Bearer test', 'X-Sotto-Chat-Send': 'private'}}
    loader._load_mcp_config = lambda: {'sotto-local': entry}
    assert photon.plugin_test._brief_receiver_config() == ('http://127.0.0.1:8787', entry['headers'])
    for url in ('https://evil.example/mcp', 'http://127.0.0.1:8788/mcp',
                'http://localhost:8787/mcp', 'http://127.0.0.1:8787/mcp/other'):
        entry['url'] = url
        assert photon.plugin_test._brief_receiver_config() is None
    entry['url'] = 'http://127.0.0.1:8787/mcp'
    entry['headers'].pop('X-Sotto-Chat-Send')
    assert photon.plugin_test._brief_receiver_config() is False


def test_brief_admission_rejects_redirect_without_second_request(photon, monkeypatch):
    calls = []

    class RedirectingOpener:
        def open(self, request, timeout):
            calls.append((request.full_url, request.get_method(), timeout,
                          request.get_header('X-sotto-chat-send')))
            raise urllib.error.HTTPError(request.full_url, 302, 'redirect',
                                         {'Location': 'https://elsewhere.example'}, None)

    def opener(*handlers):
        assert any(isinstance(h, photon.plugin_test._NoRedirect) for h in handlers)
        return RedirectingOpener()

    monkeypatch.setattr(urllib.request, 'build_opener', opener)
    status = photon.plugin_test._admit_brief(
        ('http://127.0.0.1:8787', {'X-Sotto-Chat-Send': 'private'}),
        {'kind': 'evening', 'refresh': False, 'message_id': 'inbound',
         'owner': 'chat', 'chat_id': 'chat'})
    assert status is None
    assert calls == [('http://127.0.0.1:8787/chat/brief', 'POST', 10, 'private')]


def test_brief_admission_reads_pending_delivery_status(photon, monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _limit):
            return b'{"status":"pending_delivery"}'

    monkeypatch.setattr(urllib.request, 'build_opener',
                        lambda *_: types.SimpleNamespace(open=lambda *_args, **_kw: Response()))
    assert photon.plugin_test._admit_brief(
        ('http://127.0.0.1:8787', {'X-Sotto-Chat-Send': 'private'}),
        {'kind': 'evening', 'refresh': False, 'message_id': 'inbound',
         'owner': 'chat', 'chat_id': 'chat'}) == 'pending_delivery'


def test_brief_admission_reads_known_request_status(photon, monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _limit):
            return b'{"status":"known_request"}'

    monkeypatch.setattr(urllib.request, 'build_opener',
                        lambda *_: types.SimpleNamespace(open=lambda *_args, **_kw: Response()))
    assert photon.plugin_test._admit_brief(
        ('http://127.0.0.1:8787', {'X-Sotto-Chat-Send': 'private'}),
        {'kind': 'evening', 'refresh': False, 'message_id': 'inbound',
         'owner': 'chat', 'chat_id': 'chat'}) == 'known_request'


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
    result = asyncio.run(photon._send_with_retry('chat', 'Complete original prep',
                         metadata={'sotto_obligation_id': 'a' * 24}))
    assert len(calls) == 1
    assert calls[0][-2:] == ('a' * 24, 'Complete original prep')
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


def test_prep_without_gateway_identity_uses_existing_text_delivery(photon):
    photon.gallery_test.prepare_prep = lambda _: pytest.fail('missing obligation identity must not render')
    assert asyncio.run(photon._send_with_retry('chat', 'Complete original prep')).success
    assert photon.calls == [('/send', {'spaceId': 'chat', 'text': 'Complete original prep'})]


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


def test_download_without_a_link_clarifies_before_starting_a_model_turn(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    event = types.SimpleNamespace(text='Can you download this to PDF?', message_id='new-request',
        source=types.SimpleNamespace(chat_id='chat', user_id='chat', chat_type='dm'))
    asyncio.run(photon.handle_message(event))
    if os.environ['SOTTO_DEPLOYMENT_MODE'] != 'managed':
        # Keep the host's gateway authorization ahead of all self-hosted replies.
        assert photon.calls == [('/model_turn', event.text)]
        return
    assert photon.calls == [('/send', {'spaceId': 'chat', 'text': 'Send me the link you want saved as a PDF.'})]
    # The next message is the new target. No old document lookup has started.
    event.text = 'https://acme.docsend.com/view/new-deck'
    asyncio.run(photon.handle_message(event))
    assert photon.calls[-1] == ('/model_turn', event.text)


@pytest.mark.parametrize('extra,text', [
    ({'reply_to_message_id': 'deck-message'}, 'Can you download this to PDF?'),
    ({'reply_to_text': 'https://acme.docsend.com/view/current'}, 'Download this'),
    ({'media_urls': ['attachment.pdf']}, 'Download this as PDF'),
    ({}, 'Download the Acme deck as PDF'),
    ({}, 'Download this to PDF: https://acme.docsend.com/view/current'),
    ({}, 'Save our meeting notes as a PDF'),
])
def test_bound_or_named_downloads_keep_normal_handling(photon, tmp_path, monkeypatch, extra, text):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    event = types.SimpleNamespace(text=text, source=types.SimpleNamespace(chat_id='chat', user_id='chat', chat_type='dm'), **extra)
    asyncio.run(photon.handle_message(event))
    assert photon.calls == [('/model_turn', text)]


def test_missing_link_cannot_bypass_sender_authorization(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture')
    event = types.SimpleNamespace(text='Download this as PDF', source=types.SimpleNamespace(
        chat_id='stranger-chat', user_id='stranger', chat_type='dm'))
    asyncio.run(photon.handle_message(event))
    if os.environ['SOTTO_DEPLOYMENT_MODE'] == 'managed':
        assert photon.calls == []
    else:
        assert photon.calls == [('/model_turn', event.text)]  # upstream authorization, no direct reply


@pytest.mark.parametrize("clock", [1790899100.0, 1790899200.0, 1790899300.0])
def test_reply_context_preserves_transport_target_and_time(photon, monkeypatch, clock):
    plugin = photon.plugin_test
    event = types.SimpleNamespace(text='reaction:added:👍', reply_to_message_id='older-message',
        source=types.SimpleNamespace(chat_id='owner-chat'), timestamp=datetime(2026, 10, 2, tzinfo=timezone.utc))
    monkeypatch.setattr(plugin.time, 'time', lambda: clock)
    result = plugin.reply_context(event)
    assert result is not event and result.text == event.text == 'reaction:added:👍'
    assert 'older-message' in result.channel_prompt and 'owner-chat' in result.channel_prompt
    assert f'"before": {min(1790899200.0, clock)}' in result.channel_prompt


def test_reply_metadata_preserves_reaction_feedback_and_commands(photon):
    plugin = photon.plugin_test
    event = types.SimpleNamespace(text='Thanks!', source=types.SimpleNamespace(chat_id='chat'),
        message_id='thanks', timestamp=datetime(2026, 10, 2, tzinfo=timezone.utc))
    contextual = plugin.reply_context(event)
    asyncio.run(photon.on_processing_start(contextual))
    assert ('/react', {'spaceId': 'chat', 'messageId': 'thanks', 'emoji': '❤️'}) in photon.calls
    event.text = '/new'
    assert plugin.reply_context(event) is event


@pytest.mark.parametrize('text', ['yes', 'no', 'ok', '👍', 'approve', '2', 'restart gateway',
                                  'reaction:added:👍'])
def test_reply_context_never_changes_text_that_gateway_intercepts_match(photon, text):
    # Hermes matches approval words, clarify digits, slash confirms and restart coercion on the
    # exact event.text; the metadata rides this turn's ephemeral channel_prompt instead.
    plugin = photon.plugin_test
    event = types.SimpleNamespace(text=text, channel_prompt='Existing channel prompt.',
        source=types.SimpleNamespace(chat_id='any;-;chat', user_id='chat', chat_type='dm'),
        message_id='in-1', timestamp=datetime(2026, 10, 2, tzinfo=timezone.utc))
    result = plugin.reply_context(event)
    assert result.text == text and event.channel_prompt == 'Existing channel prompt.'
    assert result.channel_prompt.startswith('Existing channel prompt.\n\n[Sotto transport reply context: ')
    assert '"chat_id": "chat"' in result.channel_prompt  # the owner key detached offers record


def test_reply_context_keeps_other_conversations_and_bare_media_unchanged(photon):
    plugin = photon.plugin_test
    stamp = datetime(2026, 10, 2, tzinfo=timezone.utc)
    group = types.SimpleNamespace(text='yes', timestamp=stamp,
        source=types.SimpleNamespace(chat_id='any;+;group', user_id='chat', chat_type='group'))
    assert '"chat_id": "any;+;group"' in plugin.reply_context(group).channel_prompt
    media = types.SimpleNamespace(text='', media_urls=['/tmp/p.jpg'], timestamp=stamp,
        source=types.SimpleNamespace(chat_id='any;-;chat', user_id='chat', chat_type='dm'))
    assert plugin.reply_context(media) is media and media.text == ''
