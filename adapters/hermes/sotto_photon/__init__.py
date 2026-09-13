"""Shared Sotto Photon behavior with additional managed tenant guards."""
import asyncio
import json
import re
import sys
import os
from pathlib import Path


BUDGET_ERROR = re.compile(r'sotto_budget_exhausted|tenant request budget exhausted', re.IGNORECASE)
BUDGET_NOTICE = ("Sotto has reached its pilot model allowance. New model work is paused until the allowance "
                 "is reviewed. Waiting or retrying will not reset it.")
TYPING_START_DELAY_SECONDS = 2.0


def install_budget_error_reply():
    # Keep the pinned Hermes error-category adaptation inside its managed adapter.
    # Its generic 429 response incorrectly tells the user to retry a durable budget cap.
    for name in ('gateway.run', '__main__'):
        module = sys.modules.get(name)
        replies = getattr(module, '_PROVIDER_ERROR_REPLIES', None)
        if replies is not None and not any(reply == BUDGET_NOTICE for _, reply in replies):
            module._PROVIDER_ERROR_REPLIES = ((BUDGET_ERROR, BUDGET_NOTICE), *replies)


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


def register(ctx):
    from plugins.platforms.photon import adapter as upstream  # noqa: PLC0415 — optional runtime, loaded by Hermes
    from .chatfmt import compact_handled, to_imessage  # noqa: PLC0415 — copied from the shared skill library at boot

    if managed():
        install_budget_error_reply()

    # The upstream scoped-secret reader also consults its persisted .env. Override
    # its single format decision for this managed registration, including CLI sends.
    upstream._markdown_enabled = lambda: False

    class SottoPhoton(upstream.PhotonAdapter):
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

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            # All gateway status/final notices use this path. Serialize so concurrent
            # retry callbacks cannot each emit the same budget explanation.
            if not managed():
                return await super().send(chat_id, content, reply_to, metadata)
            if not hasattr(self, '_budget_notice_lock'):
                self._budget_notice_lock = asyncio.Lock()
                self._budget_notice_sent = False
            async with self._budget_notice_lock:
                is_budget = content.strip() == BUDGET_NOTICE or bool(BUDGET_ERROR.search(content))
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
            if not managed():
                return await super().handle_message(event)
            owner = os.environ.get("PHOTON_HOME_CHANNEL", "")
            if not permits(event.source, owner):
                return
            # A content-free receipt separates infrastructure readiness from the first
            # owner-initiated conversation required by Photon's shared-line policy.
            root = Path(os.environ.get("SOTTO_DATA", "/data")) / "config"
            root.mkdir(parents=True, exist_ok=True)
            receipt = root / "photon-activation.json"
            payload = {"tenant_id": os.environ["SOTTO_TENANT_ID"], "owner": owner,
                       "chat_id": event.source.chat_id, "activated": True}
            if not receipt.exists():
                tmp = receipt.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload))
                tmp.chmod(0o600)
                tmp.replace(receipt)
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
                "Check google_action.py capabilities before offering to save a Gmail draft; "
                "Gmail read access does not imply draft permission."
            )
            return ctx.register_platform(**kwargs)

    upstream.register(Registration())
