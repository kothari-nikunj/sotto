#!/usr/bin/env python3
"""
Telegram link engine — "paste your bot's token, text it once, and Sotto tells you exactly what to
set". No user-id hunting via @userinfobot, no guessing which variable names to fill in.

One command, three steps:
  1. getMe      — is this a real bot token? Prints the @username, so you know WHICH bot you pasted.
  2. getUpdates — long-poll until the first PRIVATE message from a human arrives. That sender IS
                  the owner: their numeric id is CAPTURED, never looked up in a second bot.
  3. write+print— $SOTTO_DATA/telegram-link.json (0600), then the exact Railway variable block.

WHO READS telegram-link.json TODAY: nobody. It is written for the planned `/setup` wizard tile
(docs/plans/ROADMAP.md § Front Door), which will read it instead of re-running this flow. `start.sh`
does NOT consume it — the variables you paste into Railway are still what configures the gateway,
exactly as CHANNELS.md § Telegram setup describes. Said out loud, because a file that looks like
config but configures nothing is worse than no file at all.

SECRETS: the bot token is a credential. This module writes to no log — it deliberately does not use
the tree's `sotto_log`/diag helpers — and `_redact` scrubs the token out of any transport detail
that reaches an error string (the API URL carries the token in its path). It IS printed once, in
the settings block, on the terminal of the person who just typed it: that is the deliverable.

Stdlib only (urllib/json/time), like the rest of the receiver tree.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# The receiver image's ONE atomic-write helper (docs/ARCHITECTURE.md names it as such). Reused
# rather than re-implemented: a second tmp+os.replace in this tree would be a second writer of the
# same concept. The bootstrap makes `python3 /app/trigger-receiver/telegram_link.py` work from any
# cwd — the same sys.path bootstrap the skills scripts use.
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

# The variable names this prints. Source of truth: CHANNELS.md § Telegram setup — the token and
# allow/home-channel variables belong to Hermes (start.sh forwards every TELEGRAM_* into
# ~/.hermes/.env by prefix), and SOTTO_CRON_DELIVER is the one lever that moves the briefs.
TOKEN_VAR = "TELEGRAM_BOT_TOKEN"
ALLOWED_USERS_VAR = "TELEGRAM_ALLOWED_USERS"
HOME_CHANNEL_VAR = "TELEGRAM_HOME_CHANNEL"
DELIVER_VAR = "SOTTO_CRON_DELIVER"


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


def _display_name(sender: dict) -> str:
    parts = [str(sender.get("first_name") or "").strip(), str(sender.get("last_name") or "").strip()]
    full = " ".join(p for p in parts if p)
    return full or str(sender.get("username") or "") or str(sender.get("id", "unknown"))


def _first_human_private_message(update: dict) -> dict | None:
    """The filter, in one place. A link is a PRIVATE message from a HUMAN — anything else is either
    Telegram bookkeeping (edits, channel posts, callback queries) or a group the bot was added to,
    and linking the deploy to a group id would send every brief to that group."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    if (message.get("chat") or {}).get("type") != "private":
        return None
    sender = message.get("from")
    if not isinstance(sender, dict) or sender.get("is_bot"):
        return None
    user_id = sender.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    return {"ok": True, "user_id": user_id, "name": _display_name(sender), "text": message.get("text") or ""}


def capture_first_sender(token: str, timeout_secs: int = LINK_TIMEOUT_SECS,
                         poll_secs: int = POLL_SECS) -> dict:
    """Long-poll getUpdates until the first private human message → {"ok": True, user_id, name,
    text}. Timeout → {"ok": False, "error": "nobody texted the bot in time"}.

    The offset advances past EVERY update seen, ignored ones included: without it Telegram re-serves
    the same backlog on each poll and an ignored group message would be re-examined forever.
    """
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
            hit = _first_human_private_message(update)
            if hit:
                return hit
        if time.monotonic() >= deadline:
            return {"ok": False, "error": "nobody texted the bot in time"}


def persist(token: str, user_id: int, path: str | None = None) -> None:
    """Write the wizard handoff file: atomic (tmp + os.replace) and 0600, because it holds the bot
    token. Nothing reads it yet — see the module docstring."""
    record = {
        "bot_token": token,
        "allowed_user": user_id,
        "linked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_json(path or link_path(), record, 0o600, indent=2)


def settings_block(token: str, user_id: int) -> list[str]:
    """The exact lines to paste into Railway → Variables, one `NAME=value` per line and nothing
    else, so the whole block can be copied into Railway's raw editor unedited."""
    return [
        f"{TOKEN_VAR}={token}",
        f"{ALLOWED_USERS_VAR}={user_id}",
        f"{HOME_CHANNEL_VAR}={user_id}",
        f"{DELIVER_VAR}=telegram",
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="telegram_link.py",
        description="Link a Telegram bot to this deploy: validate the token, capture your user id "
                    "from the first message you send it, and print the variables to set.")
    ap.add_argument("--token", required=True, help="the bot token @BotFather gave you")
    ap.add_argument("--timeout", type=int, default=LINK_TIMEOUT_SECS,
                    help=f"seconds to wait for your message (default {LINK_TIMEOUT_SECS})")
    args = ap.parse_args(argv)
    token = args.token.strip()

    print("[1/3] Checking the token with Telegram...")
    bot = validate_token(token)
    if not bot["ok"]:
        print(f"      x {bot['error']}")
        return 1
    print(f"      ok - this is @{bot['username']}")

    minutes = max(1, round(args.timeout / 60))
    print(f"[2/3] Now text @{bot['username']} anything from your phone - waiting up to {minutes} min...")
    who = capture_first_sender(token, args.timeout)
    if not who["ok"]:
        print(f"      x {who['error']}")
        return 1
    print(f"      ok - Linked to {who['name']} (id {who['user_id']})")
    if who["text"]:
        print(f'           it said: "{who["text"]}"')

    path = link_path()
    persist(token, who["user_id"], path)
    print(f"[3/3] Saved {path} (0600) for the future /setup tile; nothing reads it yet.")

    print("")
    print("Paste these into Railway -> your service -> Variables, then Redeploy:")
    print("")
    for line in settings_block(token, who["user_id"]):
        print(line)
    print("")
    print("Optional: WHATSAPP_ENABLED=false skips the WhatsApp pairing wait on boot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
