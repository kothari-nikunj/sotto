"""Shared Sotto Photon behavior with additional managed tenant guards."""
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path


BUDGET_NOTICE = ("This account has reached its usage allowance. "
                 "The account owner needs to review the limit before I can continue.")
TYPING_START_DELAY_SECONDS = 2.0


def missing_download_link(event):
    """A standalone 'download this' often arrives before its link in the next message.

    Clarify that narrow text-only request before a model can act on an unrelated old deck.
    Explicit URLs, reply targets, attachments and named requests retain ordinary handling.
    """
    if (getattr(event, 'media_urls', None) or getattr(event, 'reply_to_message_id', None)
            or getattr(event, 'reply_to_text', None)):
        return False
    text = ' '.join(str(getattr(event, 'text', '') or '').split())
    return bool(re.fullmatch(r'(?:(?:can you|could you|would you|please)\s+)?download\s+'
                             r'(?:this|that|it)(?:\s+(?:to|as)\s+(?:a\s+)?pdf)?[?.!]*', text, re.I))


def processing_tapback(text):
    """A bounded, local hint about the request, never a claim that an action ran."""
    text = ' '.join(str(text or '').casefold().split())[:512]
    if re.fullmatch(r'(?:thanks|thank you|thank you so much|thanks so much|appreciate it)[.!\s🙏❤️]*', text):
        return '❤️'
    # Specific requests win over incidental words ("draft a meeting invite").
    for pattern, emoji in (
        (r'\b(?:draft|write|rewrite|compose|wording|reply)\b', '📝'),
        (r'\b(?:remember|forget|memory|memories)\b', '🧠'),
        (r'\b(?:research|look up|lookup|find out|background|prep|prepare|who is|who.s|what do (?:i|you) know)\b', '🔎'),
        (r'\b(?:calendar|schedule|reschedule|availability|available|meeting|meetings|appointment)\b', '📅'),
    ):
        if re.search(pattern, text):
            return emoji
    return '💭'


def managed():
    return os.environ.get("SOTTO_DEPLOYMENT_MODE") == "managed"


def permits(source, owner):
    return bool(owner and source and source.chat_type == "dm" and source.user_id == owner)


def owner_destination(chat_id):
    owner = os.environ.get('PHOTON_HOME_CHANNEL', '')
    if owner and chat_id == owner:
        return True
    receipt = Path(os.environ.get('SOTTO_DATA', '/data')) / 'config/photon-activation.json'
    try:
        state = json.loads(receipt.read_text())
        return bool(state.get('activated') is True and state.get('owner') == owner
                    and state.get('tenant_id') == os.environ.get('SOTTO_TENANT_ID')
                    and state.get('chat_id') == chat_id)
    except (OSError, ValueError):
        return False


def record_owner_activation(source):
    """Only an authenticated owner DM can open this instance's first-brief gate."""
    owner = os.environ.get('PHOTON_HOME_CHANNEL', '')
    chat = getattr(source, 'chat_id', None)
    if not permits(source, owner) or not isinstance(chat, str) or not chat:
        return False
    root = Path(os.environ.get('SOTTO_DATA', '/data')) / 'config'
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / 'photon-activation.json'
    payload = {'tenant_id': os.environ['SOTTO_TENANT_ID'], 'owner': owner,
               'chat_id': chat, 'activated': True}
    try:
        if json.loads(receipt.read_text()) == payload:
            return True
    except (OSError, ValueError):
        pass
    # A receipt for an earlier owner/tenant/conversation must not strand a new
    # invited user. Unique temporary files also tolerate simultaneous callbacks.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=root, prefix='.photon-', delete=False) as out:
            temporary = Path(out.name)
            json.dump(payload, out)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(receipt)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def register(ctx):
    from plugins.platforms.photon import adapter as upstream  # noqa: PLC0415 — optional runtime, loaded by Hermes
    from .chatfmt import compact_handled, to_imessage  # noqa: PLC0415 — copied from the shared skill library at boot

    # The upstream scoped-secret reader also consults its persisted .env. Override
    # its single format decision for this managed registration, including CLI sends.
    upstream._markdown_enabled = lambda: False

    class SottoPhoton(upstream.PhotonAdapter):
        async def _processing_tapback(self, event, emoji):
            if not self._reactions_enabled():
                return
            chat_id = getattr(getattr(event, 'source', None), 'chat_id', None)
            message_id = getattr(event, 'message_id', None)
            if not chat_id or not message_id or getattr(event, '_sotto_tapback', None) == emoji:
                return
            # Photon sets the sender's reaction on the existing target. Calling
            # /unreact first creates an avoidable removal event/notification.
            event._sotto_tapback_attempted = True
            if await self._add_reaction(chat_id, message_id, emoji):
                event._sotto_tapback = emoji

        async def on_processing_start(self, event):
            await self._processing_tapback(event, processing_tapback(getattr(event, 'text', '')))

        async def on_processing_complete(self, event, outcome):
            outcome = getattr(outcome, 'value', outcome)
            emoji = {'success': '✅', 'failure': '⚠️', 'cancelled': '⏸️'}.get(outcome)
            if outcome == 'success' and getattr(event, '_sotto_tapback', None) == '❤️':
                return  # Acknowledgments keep their heart without a second notification.
            if outcome == 'cancelled' and not getattr(event, '_sotto_tapback_attempted', False):
                return  # Nothing was shown; do not add a new reaction for cancellation.
            if emoji:
                await self._processing_tapback(event, emoji)

        async def _keep_typing(self, chat_id, interval=2.0, metadata=None, stop_event=None):
            # Only Hermes' turn-owned refresh loop may start typing. Its isolated
            # startup/progress calls otherwise flash once and expire during tool work.
            await asyncio.sleep(TYPING_START_DELAY_SECONDS)
            if stop_event is not None and stop_event.is_set():
                return
            if not hasattr(self, '_sotto_typing_runs'):
                self._sotto_typing_runs = {}
            owner = object()
            self._sotto_typing_runs[chat_id] = owner
            try:
                await super()._keep_typing(chat_id, interval, metadata, stop_event)
            finally:
                if self._sotto_typing_runs.get(chat_id) is owner:
                    self._sotto_typing_runs.pop(chat_id, None)

        async def send_typing(self, chat_id, metadata=None):
            if chat_id not in getattr(self, '_sotto_typing_runs', {}):
                return
            # The upstream five-second cooldown turns a two-second refresh into a
            # six-second gap, long enough for the indicator to lapse. The owning
            # loop supplies the cadence; keep its pause, cancellation and timeout rules.
            await self._sidecar_try('/typing', {'spaceId': chat_id, 'state': 'start'}, 'send_typing')

        async def _send_with_retry(self, chat_id, content, reply_to=None, metadata=None,
                                   max_retries=1, base_delay=2.0):
            # Intercept the complete focused prep before upstream text formatting/truncation.
            # Return an uncertain gallery result directly: upstream's text fallback could duplicate
            # an accepted multipart message. Ordinary messages retain upstream's existing retries.
            from . import gallery  # noqa: PLC0415 - installed by photon_setup
            if managed() and not owner_destination(chat_id):
                return upstream.SendResult(success=False, error='Gallery destination refused')
            presentation = await asyncio.to_thread(gallery.prepare_prep, content)
            if presentation:
                ok, error, receipt = await asyncio.to_thread(
                    gallery.send_once, presentation['images'], presentation['summary'], 'photon:' + chat_id, reply_to)
                if ok:
                    self._record_sent_message(receipt['message_id'])
                    for identifier in receipt.get('message_ids', []):
                        self._record_sent_message(identifier)
                    return upstream.SendResult(success=True, message_id=receipt['message_id'], raw_response=receipt)
                if receipt.get('acceptance') == 'unknown':
                    return upstream.SendResult(success=False, error=error, raw_response=receipt)
                # Proven unsent/rejected media can safely use the original complete text.
            return await super()._send_with_retry(chat_id, content, reply_to, metadata,
                                                  max_retries=max_retries, base_delay=base_delay)

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            from . import gallery  # noqa: PLC0415 - installed by photon_setup
            receipt = await asyncio.to_thread(gallery.recovery_receipt, content, 'photon:' + chat_id)
            if receipt:
                accepted = receipt.get('acceptance') == 'accepted'
                return upstream.SendResult(success=accepted, message_id=receipt.get('message_id'),
                                           error=None if accepted else 'gallery acceptance unconfirmed',
                                           raw_response=receipt)
            # All gateway status/final notices use this path. Serialize so concurrent
            # retry callbacks cannot each emit the same budget explanation.
            if not managed():
                return await super().send(chat_id, content, reply_to, metadata)
            if not hasattr(self, '_budget_notice_lock'):
                self._budget_notice_lock = asyncio.Lock()
                self._budget_notice_sent = False
            async with self._budget_notice_lock:
                is_budget = content.strip() == BUDGET_NOTICE
                if is_budget and self._budget_notice_sent:
                    return upstream.SendResult(success=True, raw_response={'suppressed': 'duplicate_budget_notice'})
                result = await super().send(chat_id, BUDGET_NOTICE if is_budget else content, reply_to, metadata)
                if result.success:
                    self._budget_notice_sent = is_budget
                return result

        def format_message(self, content):
            return compact_handled(to_imessage(content))

        async def _dispatch_inbound(self, event):
            # Upstream treats an absent/unknown space type as a DM. Managed
            # ingress must prove that it is an owner DM before normalization.
            space, sender = event.get('space') or {}, event.get('sender') or {}
            if managed() and (space.get('type') != 'dm' or sender.get('id') != os.environ.get('PHOTON_HOME_CHANNEL')):
                return
            return await super()._dispatch_inbound(event)

        async def _sidecar_call(self, path, body):
            if managed() and 'spaceId' in body and not owner_destination(body['spaceId']):
                raise PermissionError('Managed iMessage can only address the tenant owner')
            return await super()._sidecar_call(path, body)

        async def handle_message(self, event):
            if managed():
                owner = os.environ.get("PHOTON_HOME_CHANNEL", "")
                if not permits(event.source, owner):
                    return
                if not record_owner_activation(event.source):
                    return
            if managed() and missing_download_link(event):
                return await self.send(event.source.chat_id, "Send me the link you want saved as a PDF.",
                                       reply_to=getattr(event, 'message_id', None))
            return await super().handle_message(event)

    async def owner_send(config, chat_id, message, **kwargs):
        if managed() and not owner_destination(chat_id):
            return {'error': 'Managed iMessage can only address the tenant owner'}
        return await upstream._standalone_send(config, chat_id, compact_handled(to_imessage(message)), **kwargs)

    class Registration:
        def __getattr__(self, key):
            return getattr(ctx, key)

        def register_platform(self, **kwargs):
            kwargs["adapter_factory"] = SottoPhoton
            kwargs['standalone_sender_fn'] = owner_send
            # The upstream /update chat command must not move a managed runtime pin.
            if managed():
                kwargs["allow_update_command"] = False
            kwargs["platform_hint"] = (
                "The user is texting Sotto in Apple Messages. Use short, plain-text paragraphs "
                "and • bullets. No Markdown headings, emphasis markers, quote prefixes or tables. "
                "Web links must be ordinary URLs, not Markdown links. Never offer a mailto URL. "
                "For a link request, use the URL in this message or its explicit reply target. "
                "If the requested link is missing or ambiguous, ask for it; do not pick an older "
                "deck from the conversation. A new URL replaces the earlier target. "
                "Check google_action.py capabilities before offering to save a Gmail draft; "
                "Gmail read access does not imply draft permission."
            )
            return ctx.register_platform(**kwargs)

    upstream.register(Registration())
