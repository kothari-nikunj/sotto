#!/usr/bin/env python3
"""
pending_offer.py — the one question Sotto is currently waiting on an answer to.

THE BUG THIS EXISTS FOR (real WhatsApp transcript, Aug 2026): the 11:15 proactive tick sent
"You're meeting Shivani in ~44 min at Sightglass — want me to pull full prep on her?". At 11:44
the user replied "Sure". The gateway session that received "Sure" had never seen the question —
the nudge was composed and delivered by a DETACHED run (the cron lane today, `hermes send`
tomorrow) — so it resolved the bare affirmative against the last thing in its own history, a
group-chat item from the morning brief, and answered about that. The user had to retype "Give me
full prep on Shivani".

No prompt can fix a session that does not contain the question. So the question is written down
where the other process can read it: the lane that ASKS calls `set`, the gateway that receives the
bare "sure" calls `get`, acts on what comes back, and calls `clear`.

    pending_offer.py set --kind meeting_prep --person "Shivani" \
        --question "You're meeting Shivani in ~44 min at Sightglass — want me to pull full prep on her?"
    pending_offer.py get --offer-id ID → that offer JSON if fresh, `{}` if absent or expired
    pending_offer.py clear    → removes it
    pending_offer.py dismiss-reply --text "no" [get selectors] → declines only the offer; keeps the loop

Negative/completion replies use dismiss-reply so their meaning is enforced by code. Legacy mute
offers are no longer returned by get: their input conflated unused drafts with user feedback.

Detached runs stage the exact question until provider acceptance. Explicit reply IDs select their
exact question. A transport-scoped --latest lookup selects the last delivered question before the
reply. With another question pending, one accepted after or within ACCEPT_SKEW_SECONDS of the reply
makes it ambiguous, as do unscoped lookups and multiple questions in one message. dismiss-reply takes the same selectors.
Legacy direct interactive callers still write a single visible offer.

Expiry is checked at READ (`expires_at`, default 180 min): nothing daemonic, no sweeper, and a
stale file simply never answers. "Sure" three hours after the meeting started is not a yes to it.

WHEN THE YES CAUSES A REAL EFFECT, BIND IT TO THE FULL ACTION. Generate the canonical payload with
`google_action.py <verb and args> --print-payload`, then pass that file with `--payload-file PATH`; only
`payload_sha256` is stored, never the payload. The acting verb (`google_action.py --offer-bound`)
recomputes that hash over what it is about to do and refuses when it differs, so an approval covers
the content the user actually saw and nothing else. Offers whose yes only runs a read — "want me to
pull prep?" — have nothing to hash and pass no payload file.

Ephemeral by construction: `{offer_id, ts, kind, question, person, detail, action,
payload_sha256, expires_at}` and
nothing else. This is not memory — a question the user answered (or didn't) three months ago could
not be used by any brief or prep, so nothing here is ever promoted to the graph.

State: `$SOTTO_DATA/proactive/pending_offer.json`, read/written under jsonstore's lock because the
writer (proactive lane) and the reader (gateway) are different processes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jsonstore  # noqa: E402

KINDS = ("meeting_prep", "commitment", "chase", "handoff", "procedure", "intention")
DEFAULT_TTL_MIN = 180   # a question goes stale in three hours; a named constant, not a knob

# Declining an offer is not cancelling its obligation. Only explicit completion/cancellation
# words change a loop; generic negatives clear the offer and leave the ledger untouched.
DISMISS_RESOLVED = ("done", "handled", "already handled", "sorted", "taken care of")
DISMISS_DROPPED = ("let it go", "drop it", "stop tracking this", "drop this loop")
DECLINE_OFFER = ("no", "nah", "nope", "no thanks", "no thank you", "not now", "not yet",
                 "skip", "skip it", "leave it", "forget it")
LOOP_KINDS = frozenset(("chase", "commitment", "handoff"))
# accepted_at is the receiver's clock after `hermes send` returns; the reply timestamp is the
# provider's. A question accepted within this window of the reply may be what it answers.
ACCEPT_SKEW_SECONDS = 5


def _path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "proactive", "pending_offer.json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str):
    try:
        d = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def payload_hash(payload: bytes) -> str:
    """sha256 hex of the exact bytes an offer covers — THE hasher, imported by the acting verb
    rather than reimplemented there, because the lane that offers and the verb that acts must agree
    byte-for-byte or the binding is theater. A hash is not content: it names which bytes without
    keeping any of them."""
    return hashlib.sha256(payload).hexdigest()


def set_offer(kind: str, question: str, person: str = "", detail: str = "",
              ttl_min: int = DEFAULT_TTL_MIN, payload_sha256: str = "", anchor_key: str = "",
              action: str = "") -> dict:
    """Stage a detached question, or record the visible direct interactive question.

    `payload_sha256` is set only when the yes causes a real effect; the payload itself is never
    stored, here or in the receipt the acting verb writes. `anchor_key` names the ledger loop the
    offer is about (a chase, a commitment, a hand-off), so "done" or "let it go" lands on that row
    without re-matching by name."""
    now = _now()
    offer = {
        "offer_id": secrets.token_hex(16),
        "ts": now.isoformat(),
        "kind": kind,
        "question": question,
        "person": person or "",
        "detail": detail or "",
        "anchor_key": anchor_key or "",
        "payload_sha256": payload_sha256 or "",
        "action": action or "",
        "expires_at": (now + timedelta(minutes=ttl_min)).isoformat(),
    }
    import delivery_effects
    ident = delivery_effects.run_id()
    if ident:
        # The reply gateway must never discover an offer whose question failed to send.
        offer['offer_id'] = payload_hash((ident + json.dumps({k: offer[k] for k in
            ('kind', 'question', 'anchor_key', 'payload_sha256', 'action')}, sort_keys=True)).encode())
        if os.environ.get('SOTTO_CRON_DELIVER') == 'photon':
            offer['chat_id'] = os.environ.get('PHOTON_HOME_CHANNEL', '')
        delivery_effects.stage([{'kind': 'pending_offer', 'offer': offer}])
    else:
        path = _path()
        with jsonstore.lock(path):
            jsonstore.write_atomic(path, offer)
    return offer


def activate_offer(offer, receipt):
    """Idempotent ACK effect retaining delivery order and destination for short replies."""
    if not _fresh_offer(offer):
        return
    with jsonstore.lock(_path()):
        state = jsonstore.read(_path(), {})
        offers = _retained_offers(state)
        accepted = {key: expiry for key, expiry in state.get('accepted', {}).items()
                    if (_parse(expiry) or _now()) > _now()}
        if offer['offer_id'] in accepted:
            return
        accepted[offer['offer_id']] = offer['expires_at']
        offers.append({**offer, 'message_id': receipt.get('message_id', ''),
                       'target': receipt.get('target'),
                       'chat_id': receipt.get('chat_id') or offer.get('chat_id', ''),
                       'accepted_at': receipt['accepted_at']})
        jsonstore.write_atomic(_path(), {'offers': offers, 'accepted': accepted})


def _fresh_offers(data):
    items = data.get('offers', [data]) if isinstance(data, dict) else []
    return [item for item in items if _fresh_offer(item)]


def _accepted_at(offer):
    value = offer.get('accepted_at')
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) and value > 0 else 0
    stamp = _parse(value) if isinstance(value, str) else None
    return stamp.timestamp() if stamp else 0


def _delivery_anchor(offer):
    """Content-free barrier: an answered/expired latest question cannot select an older one."""
    return {key: offer.get(key) for key in
            ('offer_id', 'message_id', 'target', 'chat_id', 'accepted_at', 'expires_at')}


def _retained_offers(data):
    fresh = _fresh_offers(data)
    items = data.get('offers', [data]) if isinstance(data, dict) else []
    # Keep a barrier only while its chat still has an actionable older offer. Otherwise there
    # is nothing it could accidentally authorize. Ordinary activations prune obsolete barriers.
    scopes = {(o.get('target'), o.get('chat_id')) for o in fresh
              if o.get('target') and o.get('chat_id')}
    for scope in scopes:
        candidates = [o for o in items if isinstance(o, dict)
                      and (o.get('target'), o.get('chat_id')) == scope and _accepted_at(o)]
        if not candidates:
            continue
        newest = max(_accepted_at(o) for o in candidates)
        last_ids = {o.get('message_id') for o in candidates if _accepted_at(o) == newest}
        for offer in candidates:
            if ((_accepted_at(offer) == newest or offer.get('message_id') in last_ids)
                    and offer not in fresh):
                fresh.append(_delivery_anchor(offer))
    return fresh


def get_offer(offer_id='', message_id='', *, latest=False, target='', chat_id='', before=None) -> dict:
    """The fresh offer, or `{}` — absent, unparseable, or past `expires_at`. Never partial: a
    caller gets a whole question it can act on, or nothing it could mistake for one."""
    path = _path()
    with jsonstore.lock(path):
        data = jsonstore.read(path, {})
    offers = _retained_offers(data) if latest and not (offer_id or message_id) else _fresh_offers(data)
    if target:
        offers = [o for o in offers if o.get('target') == target]
    if chat_id:
        offers = [o for o in offers if o.get('chat_id') == chat_id]
    if offer_id or message_id:
        # Explicit anchors never fall through to another, newer question.
        offers = [o for o in offers if (not offer_id or o.get('offer_id') == offer_id)
                  and (not message_id or o.get('message_id') == message_id)]
    elif latest:
        # Only a transport-bound conversation can opt into recency. Legacy/unbound records
        # cannot authorize an action in another chat. Same-message multiple offers stay ambiguous.
        if not target or not chat_id:
            return {'ambiguous': True, 'offers': offers} if offers else {}
        offers = [o for o in offers if _accepted_at(o)]
        if before is not None:
            # A reply may answer a question that settled after it; never pick an older one instead.
            if len(offers) > 1 and any(_accepted_at(o) > before - ACCEPT_SKEW_SECONDS for o in offers):
                return {'ambiguous': True, 'offers': offers}
            offers = [o for o in offers if _accepted_at(o) <= before + ACCEPT_SKEW_SECONDS]
        if offers:
            newest = max(_accepted_at(o) for o in offers)
            last_ids = {o.get('message_id') for o in offers if _accepted_at(o) == newest}
            offers = [o for o in offers if _accepted_at(o) == newest or
                      (o.get('message_id') and o.get('message_id') in last_ids)]
    if len(offers) > 1:
        return {'ambiguous': True, 'offers': offers}
    return _fresh_offer(offers[0]) if offers else {}


def claim_offer(offer_id: str, action: str, payload_sha256: str) -> tuple[dict, str]:
    """Atomically validate and consume one approval before its effect starts.

    Once returned, success, provider refusal, timeout and process death all leave the approval
    spent. The user must approve a fresh offer before another effect attempt.
    """
    if not offer_id:
        return {}, 'an exact --offer-id is required for an offer-bound action'
    path = _path()
    with jsonstore.lock(path):
        state = jsonstore.read(path, {})
        offers = _fresh_offers(state)
        selected = [offer for offer in offers if offer.get('offer_id') == offer_id]
        if len(selected) != 1:
            return {}, 'selected offer is absent, expired, already claimed, or ambiguous'
        offer = selected[0]
        if offer.get('action') != action:
            return {}, 'offer does not authorize this action — re-offer with the exact action'
        if not offer.get('payload_sha256'):
            return {}, 'the offer carried no payload to bind to — re-offer with the content in view'
        if offer['payload_sha256'] != payload_sha256:
            return {}, 'payload does not match what the user approved — the action changed after the offer'
        if isinstance(state, dict) and 'offers' in state:
            jsonstore.write_atomic(path, {**state, 'offers': [_delivery_anchor(o) if o == offer else o for o in state['offers']]})
        else:
            os.remove(path)
        return offer, ''


def _fresh_offer(data) -> dict:
    if not isinstance(data, dict) or not data.get("question"):
        return {}
    # Retired inference/tune-up lanes are not fresh authorization after an upgrade.
    if data.get("kind") in ("mute", "retune_offer"):
        return {}
    expires = _parse(data.get("expires_at", ""))
    if expires is None or _now() >= expires:
        return {}
    return data


def dismiss_reply(reply: str, **selectors) -> dict:
    """Act on a snapshot; never hold the offer lock while waiting for the ledger.

    Only clear the same snapshot afterwards, so a newly delivered question survives a slow
    ledger write. This command takes the user's actual words, not an agent paraphrase.
    """
    text = " ".join(reply.lower().rstrip(" .!…\t\r\n").split())
    offer = get_offer(**selectors)
    if not offer:
        return {"action": "no_offer", "reason": "no fresh offer", "loop_changed": False}
    if offer.get('ambiguous'):
        return {'action': 'clarify', 'reason': 'more than one delivered question is unanswered',
                'loop_changed': False}
    if text in DECLINE_OFFER:
        result = {"action": "declined", "loop_changed": False}
    elif text in DISMISS_RESOLVED or text in DISMISS_DROPPED:
        result = {"action": "declined", "loop_changed": False}
        if offer.get("kind") in LOOP_KINDS:
            anchor = offer.get("anchor_key")
            if not anchor:
                return {"action": "clarify", "reason": "offer has no loop anchor",
                        "loop_changed": False}
            target = "resolved" if text in DISMISS_RESOLVED else "dismissed"
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "knowledge"))
                import knowledge_edit  # noqa: PLC0415
                knowledge_edit.op_loop(anchor, target)
            except Exception as e:  # noqa: BLE001 — failed writes must produce a JSON receipt
                return {"action": "clarify", "reason": f"loop write could not be confirmed: {type(e).__name__}",
                        "anchor_key": anchor, "loop_changed": None}
            result = {"action": target, "anchor_key": anchor, "loop_changed": True}
    else:
        return {"action": "clarify", "reason": "reply does not specify an unambiguous action",
                "loop_changed": False}
    try:
        cleared = clear_offer(expected=offer)
    except Exception:  # noqa: BLE001 — a cleanup failure cannot hide a successful ledger write
        cleared = False
    return {**result, "person": offer.get("person", ""), "kind": offer.get("kind", ""),
            "offer_cleared": cleared}


def clear_offer(expected: dict | None = None) -> bool:
    """Clear an offer, optionally only if it still equals the snapshot acted on."""
    path = _path()
    with jsonstore.lock(path):
        state = jsonstore.read(path, {})
        if expected is not None and state != expected:
            offers = state.get('offers', []) if isinstance(state, dict) else []
            if expected not in offers:
                return False
            jsonstore.write_atomic(path, {**state, 'offers': [_delivery_anchor(o) if o == expected else o for o in offers]})
            return True
        if isinstance(state, dict) and 'accepted' in state:
            jsonstore.write_atomic(path, {**state, 'offers': []})
            return True
        try:
            os.remove(path)
        except OSError:
            return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("set", help="record the question just delivered (overwrites)")
    s.add_argument("--kind", required=True, choices=KINDS)
    s.add_argument("--question", required=True, help="the sentence as delivered, verbatim")
    s.add_argument("--person", default="", help="who the offer is about, if it names someone")
    s.add_argument("--detail", default="", help="free text the acting session may need")
    s.add_argument("--anchor-key", default="",
                   help="the ledger loop this offer is about (chase / commitment / handoff), so a "
                        "'done' or 'let it go' resolves or drops that exact row")
    s.add_argument("--ttl-min", type=int, default=DEFAULT_TTL_MIN)
    s.add_argument("--payload-file", default="",
                   help="file holding the EXACT bytes offered (a draft body, a canonical event); "
                        "only its sha256 is stored, and the acting verb must match it")
    s.add_argument("--action", default="",
                   help="exact google_action verb this approval permits")

    get = sub.add_parser("get", help="print the fresh offer, ambiguity, or {}")
    get.add_argument('--offer-id', default='')
    get.add_argument('--message-id', default='')
    get.add_argument('--latest', action='store_true')
    get.add_argument('--target', default='')
    get.add_argument('--chat-id', default='')
    get.add_argument('--before', type=float)
    clear = sub.add_parser('clear', help='remove the offer acted on')
    clear.add_argument('--offer-id', default='')
    clear.add_argument('--message-id', default='')
    reply = sub.add_parser("dismiss-reply", help="handle the user's exact negative/completion reply")
    reply.add_argument("--text", required=True, help="the user's actual words, never a paraphrase")
    for arg in ('--message-id', '--target', '--chat-id'):
        reply.add_argument(arg, default='')
    reply.add_argument('--latest', action='store_true')
    reply.add_argument('--before', type=float)

    a = ap.parse_args()
    if a.cmd == "set":
        digest = ""
        if a.payload_file:
            with open(a.payload_file, "rb") as f:
                digest = payload_hash(f.read())
        print(json.dumps(set_offer(a.kind, a.question, a.person, a.detail, a.ttl_min, digest,
                                   a.anchor_key, a.action)))
    elif a.cmd == "get":
        print(json.dumps(get_offer(a.offer_id, a.message_id, latest=a.latest,
                                   target=a.target, chat_id=a.chat_id, before=a.before)))
    elif a.cmd == "dismiss-reply":
        print(json.dumps(dismiss_reply(a.text, message_id=a.message_id, latest=a.latest,
                                       target=a.target, chat_id=a.chat_id, before=a.before)))
    else:
        expected = get_offer(a.offer_id, a.message_id) if a.offer_id or a.message_id else None
        print('cleared' if (expected is None or expected) and clear_offer(expected=expected) else 'none')


if __name__ == "__main__":
    main()
