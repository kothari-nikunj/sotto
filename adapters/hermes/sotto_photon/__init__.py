"""Shared Sotto Photon behavior with additional managed tenant guards."""
import asyncio
import copy
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


BUDGET_NOTICE = ("This account has reached its usage allowance. "
                 "The account owner needs to review the limit before I can continue.")
TYPING_START_DELAY_SECONDS = 2.0
BRIEF_UNAVAILABLE = ("I couldn't confirm your brief request. It may still arrive; "
                     "please check before asking again.")
BRIEF_PENDING_DELIVERY = ("An earlier brief may still be on its way. "
                          "I'll wait for its delivery status before starting another.")
BRIEF_OTHER_NOT_PROCESSED = (" I didn't process the other request in your message. "
                             "Please send it separately.")
BRIEF_REPLAY_OTHER = ("I already have this brief request. I can't confirm the other "
                      "part here; if it's still needed, send that separately.")


def reply_context(event):
    """Attach transport metadata for short answers as this turn's ephemeral system prompt.

    Hermes matches approval words, clarify digits, slash confirmations and restart coercion on the
    exact `event.text`, so the user's words are never changed. `channel_prompt` is applied at API
    call time for this turn only and is never persisted to the transcript.
    """
    text = str(getattr(event, 'text', '') or '')
    reply_id = getattr(event, 'reply_to_message_id', None)
    if not text.strip() or text.startswith('/') or len(text.split()) > 5 or len(text) > 80:
        return event
    source = getattr(event, 'source', None)
    chat_id = getattr(source, 'chat_id', None)
    stamp = getattr(event, 'timestamp', None)
    if not chat_id or not hasattr(stamp, 'timestamp'):
        return event
    # Detached offers record the owner's handle; the inbound DM arrives under its space id.
    owner = os.environ.get('PHOTON_HOME_CHANNEL', '')
    if owner and ((getattr(source, 'chat_type', None) == 'dm'
                   and getattr(source, 'user_id', None) == owner) or owner_destination(chat_id)):
        chat_id = owner
    metadata = {'target': 'photon', 'chat_id': chat_id, 'before': min(stamp.timestamp(), time.time())}
    if reply_id:
        metadata['reply_to_message_id'] = reply_id
    note = '[Sotto transport reply context: ' + json.dumps(metadata) + ']'
    result = copy.copy(event)
    result.channel_prompt = '\n\n'.join(p for p in (getattr(event, 'channel_prompt', None), note) if p)
    return result


def brief_request(event):
    """Recognize a complete, unbound owner request."""
    if (getattr(event, 'media_urls', None) or getattr(event, 'reply_to_message_id', None)
            or getattr(event, 'reply_to_text', None)):
        return None
    text = ' '.join(str(getattr(event, 'text', '') or '').split())
    if len(text) > 120 or re.search(r'["“”‘]|(?<!\w)[\x27’]|[\x27’](?!\w)', text):
        return None
    match = re.fullmatch(
        r'(?:please\s+)?(?:(?:(?:can|could|would)\s+you|(?:can|could)\s+i)\s+)?'
        r'(?:give|send|show|get)\s+(?:me\s+)?(?:my|the)\s+'
        r'(?:(morning|evening)\s+)?brief(?:\s+again)?(?:,?\s+please)?[?.!]*', text, re.I)
    if match:
        return {'kind': (match.group(1) or 'auto').lower(), 'refresh': False}
    match = re.fullmatch(
        r'(?:please\s+)?(?:i\s+would|i\x27d|i’d)\s+like\s+(?:my|the)\s+'
        r'(?:(morning|evening)\s+)?brief(?:,?\s+please)?[?.!]*', text, re.I)
    if match:
        return {'kind': (match.group(1) or 'auto').lower(), 'refresh': False}
    match = re.fullmatch(
        r'(?:please\s+)?(?:refresh|update)\s+(?:my|the)\s+'
        r'(?:(morning|evening)\s+)?brief[?.!]*', text, re.I)
    if match:
        return {'kind': (match.group(1) or 'auto').lower(), 'refresh': True}
    return None


def brief_turn(event):
    """Extract at most one explicit brief command and one independent chat clause.

    A quoted, replied-to, attached, negated, or ambiguous request stays intact for
    ordinary conversation. This deliberately does not infer intent from a mention
    of the word "brief" inside a broader sentence.
    """
    text = ' '.join(str(getattr(event, 'text', '') or '').split())
    if (len(text) > 400 or getattr(event, 'media_urls', None)
            or getattr(event, 'reply_to_message_id', None) or getattr(event, 'reply_to_text', None)
            or re.search(r'["“”‘]|(?<!\w)[\x27’]|[\x27’](?!\w)|\b(?:don[\x27’]t|do not|never|without|instead of)\b', text, re.I)):
        return None
    request = brief_request(event)
    if request:
        return request, None
    clauses = re.split(r'\s*,?\s+and\s+', text, maxsplit=2, flags=re.I)
    if len(clauses) != 2:
        return None
    matches = []
    for clause in clauses:
        candidate = copy.copy(event)
        candidate.text = clause
        matches.append(brief_request(candidate))
    if sum(request is not None for request in matches) != 1:
        return None
    brief_index = 0 if matches[0] else 1
    remainder = clauses[1 - brief_index].strip()
    # Require an independent command or question, not a dangling object such as
    # "and a summary" that may modify the brief request itself.
    if not re.match(
        r'(?:please\s+)?(?:tell|explain|text|send|draft|write|remind|schedule|'
        r'look|find|check|summarize|show|give|update|what|when|where|who|how|why|'
        r'can|could|would|is|are|do|does|did)\b', remainder, re.I):
        return None
    if re.search(r'\b(?:it|this|that|them|those|brief)\b', remainder, re.I):
        return None
    return matches[brief_index], remainder


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _brief_receiver_config():
    """Read Hermes' process-private MCP binding, never a persisted config or arbitrary URL."""
    try:
        from tools.mcp_tool_config import _load_mcp_config  # noqa: PLC0415 - optional pinned runtime
        entry = _load_mcp_config().get('sotto-local')
        port = os.environ.get('PORT', '8787')
        if not isinstance(entry, dict) or not port.isdecimal() or not 1 <= int(port) <= 65535:
            return None
        origin = f'http://127.0.0.1:{port}'
        if entry.get('url') != origin + '/mcp':
            return None
        headers = entry.get('headers')
        if (not isinstance(headers, dict) or not isinstance(headers.get('X-Sotto-Chat-Send'), str)
                or not headers['X-Sotto-Chat-Send']):
            return False  # Bound receiver, but private admission is unavailable.
        return origin, headers
    except (AttributeError, ImportError, TypeError, ValueError):
        return None


def _admit_brief(config, payload):
    """One bounded loopback attempt. An uncertain response is never retried here."""
    origin, headers = config
    request = urllib.request.Request(origin + '/chat/brief',
        data=json.dumps(payload, separators=(',', ':')).encode(),
        headers={**headers, 'Content-Type': 'application/json'}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            if response.status not in (200, 202):
                return None
            result = json.loads(response.read(4097))
            if not isinstance(result, dict):
                return None
            if result.get('status') == 'queued' and isinstance(result.get('request_id'), str):
                return 'queued'
            if result.get('status') == 'needs_context':
                return 'needs_context'
            if result.get('status') == 'pending_delivery':
                return 'pending_delivery'
            if result.get('status') == 'known_request':
                return 'known_request'
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        pass
    return None


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


def project_fingerprint(project_id):
    """Bind an activation receipt to a project without persisting its credential."""
    if not isinstance(project_id, str) or not project_id.strip():
        return None
    return hashlib.sha256(('sotto-photon-project-v1:' + project_id).encode()).hexdigest()


def restricted_allowlist():
    return (os.environ.get('PHOTON_ALLOW_ALL_USERS', '').strip().lower()
            in ('', 'false', '0', 'no', 'off'))


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
    if (not restricted_allowlist() or os.environ.get('PHOTON_ALLOWED_USERS') != owner
            or not permits(source, owner)
            or not isinstance(chat, str) or not chat):
        return False
    fingerprint = project_fingerprint(os.environ.get('PHOTON_PROJECT_ID'))
    if not managed() and not fingerprint:
        return False
    root = Path(os.environ.get('SOTTO_DATA', '/data')) / 'config'
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / 'photon-activation.json'
    payload = {'tenant_id': os.environ.get('SOTTO_TENANT_ID', ''), 'owner': owner,
               'chat_id': chat, 'activated': True}
    if fingerprint:
        payload['project_fingerprint'] = fingerprint
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
            obligation_id = metadata.get('sotto_obligation_id') if isinstance(metadata, dict) else None
            # Without the gateway's durable obligation identity, a gallery could not be
            # distinguished from an identical later reply after a crash. Keep text delivery.
            presentation = await asyncio.to_thread(gallery.prepare_prep, content) if obligation_id else None
            if presentation:
                ok, error, receipt = await asyncio.to_thread(
                    gallery.send_once, presentation['images'], presentation['summary'],
                    'photon:' + chat_id, reply_to, obligation_id, content)
                if ok:
                    self._record_sent_message(receipt['message_id'])
                    for identifier in receipt.get('message_ids', []):
                        self._record_sent_message(identifier)
                    return upstream.SendResult(success=True, message_id=receipt['message_id'], raw_response=receipt)
                if receipt.get('acceptance') == 'unknown':
                    return upstream.SendResult(success=False, error=error, raw_response=receipt)
                # Proven unsent/rejected media can safely use the original complete text.
            # Base _send_with_retry calls this class's send(). Mark its initial path so
            # a direct send never consults an unrelated recovery receipt.
            initial_metadata = {**(metadata if isinstance(metadata, dict) else {}),
                                'sotto_initial_delivery': True}
            return await super()._send_with_retry(chat_id, content, reply_to, initial_metadata,
                                                  max_retries=max_retries, base_delay=base_delay)

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            from . import gallery  # noqa: PLC0415 - installed by photon_setup
            obligation_id = metadata.get('sotto_obligation_id') if isinstance(metadata, dict) else None
            initial = bool(isinstance(metadata, dict) and metadata.get('sotto_initial_delivery'))
            receipt = (None if initial else await asyncio.to_thread(
                gallery.recovery_receipt, content, 'photon:' + chat_id, obligation_id))
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
            elif permits(event.source, os.environ.get('PHOTON_HOME_CHANNEL', '')):
                # The public helper preselects exactly one owner. Keep the same
                # authenticated DM receipt in self-host mode for setup status.
                record_owner_activation(event.source)
            parsed = brief_turn(event)
            request, remainder = parsed if parsed else (None, None)
            source = getattr(event, 'source', None)
            owner = os.environ.get('PHOTON_HOME_CHANNEL', '')
            if (request and restricted_allowlist() and os.environ.get('PHOTON_ALLOWED_USERS') == owner
                    and permits(source, owner)
                    and (not managed() or owner_destination(source.chat_id))):
                config = _brief_receiver_config()
                if config is not None:
                    message_id = getattr(event, 'message_id', None)
                    if not isinstance(message_id, str) or not message_id:
                        status = None  # No stable replay identity: do not admit or improvise.
                    else:
                        payload = {**request, 'message_id': message_id, 'owner': owner,
                                   'chat_id': source.chat_id}
                        status = await asyncio.to_thread(_admit_brief, config, payload) if config else None
                    if status == 'known_request':
                        if remainder:
                            return await self.send(source.chat_id, BRIEF_REPLAY_OTHER,
                                                   reply_to=message_id)
                        return  # Gateway redelivery: the original request already owns its outcome.
                    if status == 'queued':
                        kind = request['kind'] if request['kind'] != 'auto' else 'daily'
                        notice = f"Your {kind} brief is queued."
                    elif status == 'needs_context':
                        notice = "I need a connected source before I can prepare your brief."
                    elif status == 'pending_delivery':
                        notice = BRIEF_PENDING_DELIVERY
                    else:
                        notice = BRIEF_UNAVAILABLE
                    if remainder and status != 'queued':
                        notice += BRIEF_OTHER_NOT_PROCESSED
                    await self.send(source.chat_id, notice, reply_to=message_id)
                    if remainder and status == 'queued':
                        chat_event = copy.copy(event)
                        chat_event.text = remainder
                        return await super().handle_message(chat_event)
                    return
            if managed() and missing_download_link(event):
                return await self.send(event.source.chat_id, "Send me the link you want saved as a PDF.",
                                       reply_to=getattr(event, 'message_id', None))
            return await super().handle_message(reply_context(event))

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
