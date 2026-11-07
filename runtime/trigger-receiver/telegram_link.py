#!/usr/bin/env python3
"""
Telegram link engine — validate a bot token, then capture the owner's chat id from the first
private message that carries the pairing phrase.

Three steps, one owner:
  1. getMe      — is this a real bot token? Returns the @username, so the caller knows WHICH bot.
  2. getUpdates — long-poll until the first PRIVATE message from a human CARRYING THE PAIRING PHRASE
                  arrives. That sender IS the owner: their numeric id is CAPTURED, never looked up
                  in a second bot.
  3. persist    — $SOTTO_DATA/telegram-link.json (0600): the bot token and the owner together.

THE PAIRING PHRASE IS THE WHOLE OWNERSHIP CHECK. A bot's @username is discoverable, so "the first
human who messages the bot" would hand the deploy — and every brief after it — to whoever guessed
the name first. The caller passes the phrase (adapters/sotto/selfhost_setup.py passes the one-time
setup phrase only the operator can read). No phrase, no capture.

WHO READS telegram-link.json: the self-host setup (ensure_link), the Sotto runtime adapter's
private reader (adapters/sotto/runtime_api.py) and the `/setup` wizard's channel tile. This module
is the ONE owner of the Telegram handshake: nothing re-implements a Bot API call, a capture rule or
this file's shape elsewhere.

SECRETS: the bot token is a credential. This module writes to no log — it deliberately does not use
the tree's `sotto_log`/diag helpers — and `_redact` scrubs the token out of any transport detail
that reaches an error string (the API URL carries the token in its path).

Stdlib only (urllib/json/time), like the rest of the receiver tree.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# The receiver image's ONE atomic-write helper (docs/ARCHITECTURE.md names it as such). Reused
# rather than re-implemented: a second tmp+os.replace in this tree would be a second writer of the
# same concept. The bootstrap makes this module importable from any cwd — the same sys.path
# bootstrap the skills scripts use.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from connectors import write_json  # noqa: E402 — must follow the bootstrap above

DATA = os.environ.get("SOTTO_DATA", "/data")

API_BASE = "https://api.telegram.org"

# Five minutes: long enough to unlock a phone, find the bot and type something; short enough that a
# terminal left open doesn't sit long-polling Telegram all afternoon.
LINK_TIMEOUT_SECS = 300

# getUpdates is a LONG poll — Telegram holds the connection open until an update arrives — so 25s
# costs one request per 25s of waiting, not 25 spins. Comfortably under Telegram's 50s ceiling.
POLL_SECS = 25

# The HTTP request must outlive the long-poll it asked for, or every wait ends in a timeout.
HTTP_TIMEOUT_SECS = POLL_SECS + 10

# Refused identically at both entry points, because either one alone would link a stranger.
NO_PHRASE = "no pairing phrase — without one the first person to find the bot would own this deploy"


def validate_native_capture(token: str, provision_request: str, evidence: dict) -> dict:
    """Validate authenticated native Bot API evidence before starting a fresh runtime.

    The trusted native provisioning owner obtains these responses over TLS and keeps
    them in its protected setup record. This is a pure contract check, not a public
    proof verifier: arbitrary JSON cannot prove that Telegram produced a response.
    Only the canonical private link projection leaves this setup boundary.
    """
    def refuse():
        raise ValueError('native Telegram capture unavailable')

    def safe_id(value, minimum=1):
        return type(value) is int and minimum <= value <= 9007199254740991

    try:
        encoded = json.dumps(evidence, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        refuse()
    keys = {'version', 'provisionRequest', 'phrase', 'bot', 'webhook', 'updates', 'update',
            'acknowledgedOffset', 'acknowledgement', 'requestDigest'}
    if (len(encoded) > 64 * 1024 or not isinstance(evidence, dict) or set(evidence) != keys
            or type(evidence['version']) is not int or evidence['version'] != 1
            or not isinstance(provision_request, str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{24,128}', provision_request)
            or evidence['provisionRequest'] != provision_request
            or not isinstance(token, str)
            or not re.fullmatch(r'[1-9][0-9]{0,15}:[A-Za-z0-9_-]{20,256}', token)):
        refuse()
    phrase = evidence['phrase']
    if not isinstance(phrase, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,64}', phrase):
        refuse()
    digest = hashlib.sha256(('sotto-native-telegram-pair-v1\0' + provision_request
                             + '\0' + token + '\0' + phrase).encode()).hexdigest()
    if evidence['requestDigest'] != digest:
        refuse()
    bot, webhook = evidence['bot'], evidence['webhook']
    if (not isinstance(bot, dict) or not safe_id(bot.get('id'))
            or bot['id'] != int(token.split(':', 1)[0]) or bot.get('is_bot') is not True
            or not isinstance(bot.get('username'), str)
            or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', bot['username'])
            or not isinstance(webhook, dict) or webhook.get('url') != ''):
        refuse()
    updates, update = evidence['updates'], evidence['update']
    ack, offset = evidence['acknowledgement'], evidence['acknowledgedOffset']
    for batch in (updates, ack):
        if (not isinstance(batch, list) or len(batch) > 100
                or any(not isinstance(item, dict) or not safe_id(item.get('update_id'), 0)
                       for item in batch)
                or any(left['update_id'] >= right['update_id']
                       for left, right in zip(batch, batch[1:]))):
            refuse()

    def owner(item):
        message = item.get('message')
        if not isinstance(message, dict):
            return None
        sender, chat = message.get('from'), message.get('chat')
        if (not isinstance(sender, dict) or not isinstance(chat, dict)
                or sender.get('is_bot') is not False or not safe_id(sender.get('id'))
                or type(chat.get('id')) is not int or chat['id'] != sender['id']
                or message.get('text') not in (phrase, '/start ' + phrase)):
            return None
        # One private-human ownership predicate serves both native and operator setup.
        captured = _first_human_private_message(item, phrase)
        return captured['user_id'] if captured else None

    # Anyone else's update in the batch is ignored, so it cannot block pairing. The capture is
    # the first exact owner Start; the phrase arriving from two accounts is refused.
    # Existing version 1 captures may acknowledge the whole batch; new captures stop at Start.
    matches = [(item, found) for item in updates if (found := owner(item)) is not None]
    if (len({found for _, found in matches}) != 1 or update != matches[0][0]
            or not safe_id(offset) or offset not in (update['update_id'] + 1, updates[-1]['update_id'] + 1)
            or any(item['update_id'] < offset for item in ack)):
        refuse()
    return {'bot_token': token, 'allowed_user': matches[0][1]}

def link_path() -> str:
    """The handoff file for the future wizard tile. Read at call time so $SOTTO_DATA can move."""
    return os.path.join(DATA, "telegram-link.json")


def _get(url: str) -> tuple[int, bytes]:
    """The one wire primitive: (status, body). Transport failures return status 0 with the
    exception text as the body — same shape as connectors._http. Tests monkeypatch THIS symbol."""
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECS) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except OSError:
            return e.code, b""
    except Exception as e:
        return 0, str(e).encode()


def _redact(text: str, token: str) -> str:
    """No error line may carry the token — the API URL embeds it, so anything urllib hands back
    about a failed request is suspect until scrubbed."""
    return text.replace(token, "<token>") if token else text


def _api(token: str, method: str, params: dict | None = None) -> str:
    url = f"{API_BASE}/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


def _call(token: str, method: str, params: dict | None = None) -> tuple[dict | None, str]:
    """Call a Bot API method → (result, error). Exactly one of the two is set, and the error is
    ONE short line fit to print straight at a human."""
    status, body = _get(_api(token, method, params))
    text = _redact(body.decode("utf-8", "replace") if body else "", token)
    if status == 0:
        return None, f"could not reach api.telegram.org ({text[:120] or 'no detail'})"
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None, f"Telegram sent something that isn't JSON (HTTP {status})"
    if not isinstance(payload, dict) or not payload.get("ok"):
        described = str((payload or {}).get("description") or f"HTTP {status}")
        if status == 401:
            return None, "Telegram rejected that token — check you pasted all of it from @BotFather"
        return None, f"Telegram said: {described}"
    return payload.get("result"), ""


def validate_token(token: str) -> dict:
    """{"ok": True, "username": ..., "name": ...} or {"ok": False, "error": one short line}."""
    result, error = _call(token, "getMe")
    if error:
        return {"ok": False, "error": error}
    if not isinstance(result, dict) or not result.get("username"):
        return {"ok": False, "error": "that token works but the bot has no username — remake it with @BotFather"}
    return {"ok": True, "username": result["username"], "name": result.get("first_name") or result["username"]}


def deep_link(bot_username: str, phrase: str) -> str:
    """The one-tap pairing link. Telegram delivers `?start=<phrase>` to the bot as the message text
    `/start <phrase>`, so tapping the link and typing the phrase are the same thing to the filter
    below — and the link is the better instruction, because it cannot be mistyped."""
    return f"https://t.me/{bot_username}?start={urllib.parse.quote(phrase)}"


def _display_name(sender: dict) -> str:
    parts = [str(sender.get("first_name") or "").strip(), str(sender.get("last_name") or "").strip()]
    full = " ".join(p for p in parts if p)
    return full or str(sender.get("username") or "") or str(sender.get("id", "unknown"))


def _first_human_private_message(update: dict, phrase: str) -> dict | None:
    """The filter, in one place. A link is a PRIVATE message from a HUMAN that CARRIES THE PAIRING
    PHRASE — anything else is Telegram bookkeeping (edits, channel posts, callback queries), a group
    the bot was added to (linking the deploy to a group id would send every brief to that group), or
    a stranger who found a discoverable @username. The match is a case-insensitive containment, so
    the deep link's `/start <phrase>` and a typed phrase are the same message here."""
    if not phrase.strip():
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    if (message.get("chat") or {}).get("type") != "private":
        return None
    sender = message.get("from")
    if not isinstance(sender, dict) or sender.get("is_bot"):
        return None
    text = message.get("text") or ""
    if phrase.strip().lower() not in text.lower():
        return None
    user_id = sender.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    return {"ok": True, "user_id": user_id, "name": _display_name(sender), "text": text}


def capture_first_sender(token: str, phrase: str, timeout_secs: int = LINK_TIMEOUT_SECS,
                         poll_secs: int = POLL_SECS) -> dict:
    """Long-poll getUpdates until the first private human message carrying `phrase` → {"ok": True,
    user_id, name, text}. Timeout → {"ok": False, "error": "nobody sent the pairing link in time"}.

    The offset advances past EVERY update seen, ignored ones included: without it Telegram re-serves
    the same backlog on each poll and an ignored group message would be re-examined forever. A
    stranger's message is therefore consumed and dropped, never linked and never re-examined.
    """
    if not phrase.strip():
        return {"ok": False, "error": NO_PHRASE}
    deadline = time.monotonic() + timeout_secs
    offset: int | None = None
    while True:
        params: dict = {"timeout": poll_secs}
        if offset is not None:
            params["offset"] = offset
        result, error = _call(token, "getUpdates", params)
        if error:
            # A token that validated a second ago does not go bad mid-flow, so this is the network
            # or Telegram itself. Say which, rather than reporting it later as "nobody texted".
            return {"ok": False, "error": error}
        for update in result if isinstance(result, list) else []:
            if isinstance(update.get("update_id"), int):
                offset = update["update_id"] + 1
            hit = _first_human_private_message(update, phrase)
            if hit:
                # Telegram confirms an update only when a LATER getUpdates carries its offset.
                # Returning here left the pairing message unconfirmed, so the gateway that starts
                # minutes later received "/start <setup code>" as the user's first message and the
                # per-deploy secret went into an LLM transcript. One zero-wait call consumes it.
                if offset is not None:
                    _confirmed, acknowledge_error = _call(
                        token, "getUpdates", {"offset": offset, "timeout": 0})
                    if acknowledge_error or not isinstance(_confirmed, list):
                        return {"ok": False, "error": "pairing update acknowledgement unavailable"}
                else:
                    return {"ok": False, "error": "pairing update acknowledgement unavailable"}
                return hit
        if time.monotonic() >= deadline:
            return {"ok": False, "error": "nobody sent the pairing link in time"}


def persist(token: str, user_id: int, path: str | None = None, bot_username: str = "") -> None:
    """Write the link file setup and the wizard read: atomic (tmp + os.replace) and 0600, because it
    holds the bot token. `bot_username` is carried so the wizard tile can name the bot without a
    second getMe from the browser."""
    record = {
        "bot_token": token,
        "allowed_user": user_id,
        "linked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if bot_username:
        record["bot_username"] = bot_username
    write_json(path or link_path(), record, 0o600, indent=2)


def linked_user(token: str, path: str | None = None) -> int | None:
    """The chat id this volume already captured FOR THIS TOKEN, else None — a swapped bot must
    re-link rather than keep delivering to the chat the old bot captured."""
    try:
        with open(path or link_path(), encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("bot_token") != token:
        return None
    user_id = record.get("allowed_user")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    return user_id


def ensure_link(token: str, phrase: str, timeout_secs: int = LINK_TIMEOUT_SECS,
                path: str | None = None, announce=None) -> dict:
    """Setup's one-shot: the id already on the volume, else validate the token and capture one.

    {"ok": True, "user_id": int, "reused": bool, ...} or {"ok": False, "error": one short line}.

    `announce` is handed the one-tap deep link once the bot is known and before the wait begins, so
    the operator sees what to tap while there is still time to tap it; this module still writes to
    no log of its own.
    """
    path = path or link_path()
    already = linked_user(token, path)
    if already is not None:
        return {"ok": True, "user_id": already, "reused": True}
    if not phrase.strip():
        return {"ok": False, "error": NO_PHRASE}
    bot = validate_token(token)
    if not bot["ok"]:
        return {"ok": False, "error": bot["error"]}
    if announce:
        announce(deep_link(bot["username"], phrase))
    who = capture_first_sender(token, phrase, timeout_secs)
    if not who["ok"]:
        return {"ok": False, "error": who["error"], "username": bot["username"]}
    persist(token, who["user_id"], path, bot["username"])
    return {"ok": True, "user_id": who["user_id"], "name": who["name"],
            "username": bot["username"], "reused": False}
