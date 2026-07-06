#!/usr/bin/env python3
"""
style_extract.py — extract/accumulate the user's writing-style fingerprint (style.json, schema v2).

PORT SOURCE: api/src/services/style-profile.ts (the full fingerprint, not a sketch).
The premise: the real "sounds like me" signal is VERBATIM sample messages, bucketed by context
(work_email / work_message / personal_message), accumulated across briefs with quality scoring,
promotion, TTL eviction, and per-person adaptation. style_apply.py injects those samples into the
drafter prompt. Writes $SOTTO_DATA/style.json.

Usage: style_extract.py '{"sent_messages":[{"text":"...","channel":"email|imessage|whatsapp",
                          "recipient":"sarah@acme.com","canonical_id":"c_..","work":true,"date":"..."}],
                          "contact_index":[...], "cached_calendar_attendees":[...]}'
       style_extract.py /tmp/sotto_seed.json      (setup seed: a raw Bridge read_local snapshot —
                          sent_messages are derived from the is_from_me messages, see _adapt_read_local)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

# ── Constants (style-profile.ts:156-182) ─────────────────────────────────────
MAX_PERSON_SAMPLES = 8
CANONICAL_CAP = {"work_email": 30, "work_message": 25, "personal_message": 25}
RECENT_CAP = 30
PROMOTION_QUALITY = 0.55
PROMOTION_AGE_MS = 7 * 24 * 3600 * 1000
DAY = 24 * 3600 * 1000
CANONICAL_TTL_MS = {"work_email": 90 * DAY, "work_message": 60 * DAY, "personal_message": 30 * DAY}
RECENT_TTL_MS = 7 * DAY
ALL_BUCKETS = ["work_email", "work_message", "personal_message"]
PERSONAL_DOMAINS = {"gmail.com", "yahoo.com", "icloud.com", "me.com", "mac.com",
                    "hotmail.com", "outlook.com", "aol.com", "protonmail.com", "proton.me"}
BACK_CHANNEL = {"ok", "okay", "k", "kk", "yes", "yep", "yup", "no", "nope", "sure", "lol",
                "lmao", "haha", "hahaha", "thx", "ty", "thanks", "np", "gotcha", "got it",
                "cool", "nice", "word", "bet", "sounds good", "sg", "rip"}
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⌀-⏿]")


def _root():
    return os.environ.get("SOTTO_DATA", "/data")


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _age_ms(added_at: str, now: datetime) -> float:
    try:
        dt = datetime.fromisoformat((added_at or "").replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() * 1000
    except Exception:
        return 0.0


# ── Cleaning / filtering (style-profile.ts:1071-1149) ────────────────────────
def sanitize_sample_text(raw: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", raw or "")).strip()


def clean_email_body(body: str) -> str:
    body = body or ""
    body = re.sub(r"(?m)^>.*$", "", body)
    body = re.sub(r"(?ms)^--\s*\n.*$", "", body)
    body = re.sub(r"(?mi)^Sent from my .*$", "", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def is_usable_sample(text: str, min_len: int, max_len: int) -> bool:
    t = (text or "").strip()
    if not (min_len <= len(t) < max_len):
        return False
    if t.lower().rstrip("!.?") in BACK_CHANNEL:
        return False
    if len(_EMOJI.sub("", t).strip()) < 3:
        return False
    return True


# ── Context classification (style-profile.ts:1156-1191) ──────────────────────
def build_work_canonical_id_set(payload: dict) -> set:
    work = set()
    for c in payload.get("contact_index", []) or []:
        cid = c.get("canonical_id")
        if not cid:
            continue
        for ident in (c.get("identifiers") or []):
            if "@" in ident:
                dom = ident.split("@")[-1].lower().strip()
                if dom and dom not in PERSONAL_DOMAINS:
                    work.add(cid)
    for a in payload.get("cached_calendar_attendees", []) or []:
        if (a.get("meeting_count") or 0) > 0 and a.get("canonical_id"):
            work.add(a["canonical_id"])
    return work


def classify_context(channel: str, canonical_id, work_set: set, work_flag=None) -> str:
    if channel in ("email", "gmail", "apple_mail"):
        return "work"
    if canonical_id and canonical_id in work_set:
        return "work"
    if work_flag is True:        # caller-supplied fallback when no contact graph
        return "work"
    if canonical_id or work_flag is False:
        return "personal"
    return "unknown"


def bucket_for_sample(s: dict) -> str:
    if s.get("channel") in ("email", "gmail", "apple_mail"):
        return "work_email"
    return "personal_message" if s.get("context") == "personal" else "work_message"


# ── Quality scoring (style-profile.ts:199-219) ───────────────────────────────
def score_sample(s: dict) -> float:
    t = s.get("text") or ""
    n = len(t)
    score = 0.5
    if 50 <= n <= 300:
        score += 0.25
    elif 30 <= n < 500:
        score += 0.10
    if n < 20 or n > 500:
        score -= 0.20
    if s.get("channel") in ("email", "gmail", "apple_mail"):
        if re.match(r"^(hi|hello|hey|dear|good)", t, re.I):
            score += 0.05
        if re.search(r"(thanks|best|cheers|regards|sincerely|talk soon)", t[-100:], re.I):
            score += 0.05
    if len(re.findall(r"[.!?]", t)) >= 2:
        score += 0.10
    if s.get("source") == "confirmed":
        score = max(score, 0.95)
    return max(0.0, min(1.0, score))


# ── Master style (style-profile.ts:993-1069) ─────────────────────────────────
_GREET_RE = re.compile(r"^(hey|hi|hello|yo|sup|good morning|good evening|morning|evening|dear|thanks|thank you)", re.I)
_SIGN_RE = re.compile(r"(?:^|\n)(best|thanks|cheers|regards|sincerely|talk soon|sent from|thx|ty|lmk|ttyl|xo)[\s,.!]*$", re.I)


def _top3(counter: dict) -> list:
    return [k for k, _ in sorted(counter.items(), key=lambda x: -x[1])[:3]]


def analyze_master_style(texts: list) -> dict:
    if not texts:
        return empty_master_style()
    greet, sign = {}, {}
    excl = em = semi = ell = lower = upper = 0
    total_len = 0
    for t in texts:
        total_len += len(t)
        m = _GREET_RE.match(t.strip())
        if m:
            g = m.group(1).lower()
            greet[g] = greet.get(g, 0) + 1
        m = _SIGN_RE.search(t.strip())
        if m:
            sg = m.group(1).lower()
            sign[sg] = sign.get(sg, 0) + 1
        if "!" in t:
            excl += 1
        if "—" in t or "–" in t:
            em += 1
        if ";" in t:
            semi += 1
        if "..." in t:
            ell += 1
        first = t.strip()[:1]
        if first.islower():
            lower += 1
        elif first.isupper():
            upper += 1
    n = len(texts)
    avg = total_len / n
    excl_rate = excl / n
    if lower > upper * 2:
        cap = "lowercase"
    elif upper > lower * 2:
        cap = "sentence_case"
    else:
        cap = "mixed"
    greetings, signoffs = _top3(greet), _top3(sign)
    has_formal = any(s in ("regards", "sincerely", "best") for s in signoffs)
    has_casual = any(g in ("hey", "yo", "sup") for g in greetings)
    if avg > 200 and has_formal:
        formality = "formal"
    elif avg < 80 and has_casual:
        formality = "casual"
    else:
        formality = "mixed"
    patterns = []
    if avg < 50:
        patterns.append("very short messages")
    elif avg < 100:
        patterns.append("short, direct messages")
    elif avg > 300:
        patterns.append("detailed, longer messages")
    if cap == "lowercase":
        patterns.append("starts messages lowercase")
    if excl_rate < 0.1:
        patterns.append("rarely uses exclamation marks")
    elif excl_rate > 0.5:
        patterns.append("frequently uses exclamation marks")
    return {
        "avg_length": round(avg, 1), "greetings": greetings, "signoffs": signoffs,
        "uses_exclamation_marks": excl_rate > 0.2, "uses_em_dashes": em / n > 0.1,
        "uses_semicolons": semi / n > 0.1, "uses_ellipsis": ell / n > 0.15,
        "capitalization": cap, "formality": formality, "patterns": patterns,
    }


def empty_master_style() -> dict:
    return {"avg_length": 100, "greetings": [], "signoffs": [], "uses_exclamation_marks": False,
            "uses_em_dashes": False, "uses_semicolons": False, "uses_ellipsis": False,
            "capitalization": "mixed", "formality": "mixed", "patterns": []}


# ── read_local snapshot adaptation (setup seed) ──────────────────────────────
# setup/SKILL.md step 3 passes the raw Bridge read_local snapshot (contracts/local_data.schema.json:
# flat imessage/whatsapp arrays with per-message is_from_me, plus contacts). That shape has no
# `sent_messages`, so without this adapter the seed was a silent no-op — style.json seeded empty
# while setup announced "learned your writing style". Detect the shape and derive what _ingest needs.
_READ_LOCAL_KEYS = ("imessage", "whatsapp", "contacts", "source_status", "generated_at")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _handle_key(handle: str) -> str:
    """Normalize an iMessage handle (phone or email) or WhatsApp JID to a lookup key."""
    h = (handle or "").strip()
    if "@" in h and not h.lower().endswith("@s.whatsapp.net"):
        return h.lower()
    d = _digits(h.split("@")[0])                 # WhatsApp JID: phone is the prefix before '@'
    return d[-10:] if len(d) >= 7 else ""


def _seed_contact_index(contacts) -> tuple:
    """Bridge contacts → (contact_index for build_work_canonical_id_set, identifier→(cid,name) map).
    canonical_id uses the knowledge-graph scheme (knowledge.py generate_canonical_id with the
    knowledge_update seed) so seeded per-person profiles line up with the prewarm_graph stubs."""
    index, lookup = [], {}
    for c in contacts or []:
        name = ((c or {}).get("name") or "").strip()
        if not name:
            continue
        emails = c.get("emails") or []
        phones = c.get("phones") or []
        if isinstance(emails, str):
            emails = [emails]
        if isinstance(phones, str):
            phones = [phones]
        emails = [e.strip().lower() for e in emails if isinstance(e, str) and e.strip()]
        phones = [p.strip() for p in phones if isinstance(p, str) and p.strip()]
        seed = f"kf:{name}|{emails[0]}" if emails else f"kf:{name}"
        cid = "c_" + hashlib.sha256(seed.encode()).hexdigest()[:12]
        index.append({"canonical_id": cid, "name": name, "identifiers": emails + phones})
        for e in emails:
            lookup.setdefault(e, (cid, name))
        for p in phones:
            d = _digits(p)
            if len(d) >= 7:
                lookup.setdefault(d[-10:], (cid, name))
    return index, lookup


def _adapt_read_local(payload: dict) -> dict:
    """If the payload is a read_local snapshot (no `sent_messages`), build sent_messages from the
    user's own messages (is_from_me, 1:1 chats — group chats have no clear recipient) and a
    contact_index from `contacts`. Payloads that already carry `sent_messages` (the cloud/brief
    callers) pass through untouched."""
    payload = payload or {}
    if "sent_messages" in payload or not any(k in payload for k in _READ_LOCAL_KEYS):
        return payload
    index, lookup = _seed_contact_index(payload.get("contacts"))
    sent = []
    for m in payload.get("imessage") or []:
        if not m.get("is_from_me") or m.get("is_group_chat"):
            continue
        cid, name = lookup.get(_handle_key(m.get("handle")), (None, None))
        sent.append({"text": m.get("text"), "channel": "imessage",
                     "recipient": name or (m.get("handle") or "").strip(),
                     "date": m.get("timestamp"), "canonical_id": cid})
    for m in payload.get("whatsapp") or []:
        if not m.get("is_from_me") or m.get("is_group_chat"):
            continue
        cid, name = lookup.get(_handle_key(m.get("contact_jid")), (None, None))
        sent.append({"text": m.get("text"), "channel": "whatsapp",
                     "recipient": name or (m.get("partner_name") or "").strip()
                     or (m.get("contact_jid") or "").strip(),
                     "date": m.get("timestamp"), "canonical_id": cid})
    return {"sent_messages": sent, "contact_index": index,
            "cached_calendar_attendees": payload.get("cached_calendar_attendees") or []}


# ── Dedup / accumulation (style-profile.ts:581-622, 1106-1111) ───────────────
def sample_key(s: dict) -> str:
    return "|".join([s.get("channel") or "", (s.get("date") or "")[:10],
                     (s.get("recipient") or "").lower(), (s.get("text") or "")[:120].lower()])


def _ingest(payload: dict, now: datetime) -> list:
    """Clean → sanitize → filter → score each sent message into a StyleSample."""
    work_set = build_work_canonical_id_set(payload)
    out = []
    for m in payload.get("sent_messages", []):
        ch = m.get("channel") or "imessage"
        raw = m.get("text") or ""
        if ch in ("email", "gmail", "apple_mail"):
            text = sanitize_sample_text(clean_email_body(raw))
            min_len, max_len = 15, 1000
        else:
            text = sanitize_sample_text(raw)
            min_len, max_len = 5, 500
        if not is_usable_sample(text, min_len, max_len):
            continue
        ctx = classify_context(ch, m.get("canonical_id"), work_set, m.get("work"))
        s = {"text": text, "channel": ch, "recipient": m.get("recipient"),
             "date": m.get("date"), "canonical_id": m.get("canonical_id"),
             "context": ctx, "added_at": _iso(now), "source": "extracted"}
        s["bucket"] = bucket_for_sample(s)
        s["quality"] = score_sample(s)
        out.append(s)
    return out


def _dedupe(samples: list) -> list:
    seen, out = set(), []
    for s in samples:
        k = sample_key(s)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _evict_canonical(samples: list, bucket: str, now: datetime) -> list:
    ttl = CANONICAL_TTL_MS[bucket]
    kept = [s for s in samples
            if s.get("source") == "confirmed" or s.get("quality", 0) >= 0.85 or _age_ms(s.get("added_at"), now) <= ttl]
    kept.sort(key=lambda s: (s.get("quality", 0), s.get("added_at", "")), reverse=True)
    return kept[:CANONICAL_CAP[bucket]]


def _rebuild_per_person(all_samples: list) -> dict:
    groups = {}
    for s in all_samples:
        key = s.get("canonical_id") or (f"name:{(s.get('recipient') or '').lower()}" if s.get("recipient") else None)
        if not key:
            continue
        groups.setdefault(key, []).append(s)
    master_avg = (sum(len(s["text"]) for s in all_samples) / len(all_samples)) if all_samples else 100
    out = {}
    for key, samples in groups.items():
        if len(samples) < 2:
            continue
        avg = sum(len(s["text"]) for s in samples) / len(samples)
        if avg > master_avg * 1.5:
            shift = "more_formal"
        elif avg < master_avg * 0.6:
            shift = "more_casual"
        else:
            shift = "same"
        ctxs = {}
        for s in samples:
            ctxs[s["context"]] = ctxs.get(s["context"], 0) + 1
        context = max(ctxs, key=ctxs.get)
        notes = [] if shift == "same" else [f"formality: {shift} (avg {round(avg)} chars vs master {round(master_avg)})"]
        ranked = sorted(samples, key=lambda s: s.get("quality", 0), reverse=True)[:MAX_PERSON_SAMPLES]
        out[key] = {"name": samples[0].get("recipient") or key, "canonical_id": samples[0].get("canonical_id"),
                    "context": context, "formality_shift": shift, "notes": notes,
                    "samples": [{**s, "text": s["text"][:300]} for s in ranked]}
    return out


def extract(payload: dict, now=None) -> dict:
    now = _now(now)
    payload = _adapt_read_local(payload)
    path = os.path.join(_root(), "style.json")
    style = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            style = json.load(f)

    canonical = {b: list(style.get("canonical", {}).get(b, [])) for b in ALL_BUCKETS}
    recent = list(style.get("recent", []))
    seen_keys = set(style.get("sample_keys", []))

    fresh = [s for s in _dedupe(_ingest(payload, now)) if sample_key(s) not in seen_keys]
    new_keys = [sample_key(s) for s in fresh]

    # Cold start: if every canonical pool is empty, seed canonical directly from the full pool.
    cold = not any(canonical[b] for b in ALL_BUCKETS)
    if cold:
        for b in ALL_BUCKETS:
            pool = sorted([s for s in fresh if s["bucket"] == b], key=lambda s: s.get("quality", 0), reverse=True)
            canonical[b] = pool[:CANONICAL_CAP[b]]
        promoted_keys = {sample_key(s) for b in ALL_BUCKETS for s in canonical[b]}
        recent += [s for s in fresh if sample_key(s) not in promoted_keys]
    else:
        recent += fresh

    # Promote aged, high-quality recent samples into canonical; evict TTL/cap; trim recent.
    still_recent = []
    for s in recent:
        b = s.get("bucket")
        if b in canonical and s.get("quality", 0) >= PROMOTION_QUALITY and _age_ms(s.get("added_at"), now) >= PROMOTION_AGE_MS:
            canonical[b].append(s)
        else:
            still_recent.append(s)
    for b in ALL_BUCKETS:
        canonical[b] = _evict_canonical(_dedupe(canonical[b]), b, now)
    still_recent = [s for s in _dedupe(still_recent) if _age_ms(s.get("added_at"), now) <= RECENT_TTL_MS]
    still_recent.sort(key=lambda s: s.get("added_at", ""), reverse=True)
    recent = still_recent[:RECENT_CAP]

    all_canonical = [s for b in ALL_BUCKETS for s in canonical[b]]
    pool_for_person = all_canonical + recent

    master_by_context = {b: analyze_master_style([s["text"] for s in canonical[b]]) for b in ALL_BUCKETS}
    style.update({
        "schema_version": 2,
        "updated_at": _iso(now),
        "messages_analyzed": len(seen_keys | set(new_keys)),
        "master": analyze_master_style([s["text"] for s in all_canonical]),
        "master_by_context": master_by_context,
        "canonical": {b: [{**s, "text": s["text"][:300]} if b == "personal_message" else s for s in canonical[b]] for b in ALL_BUCKETS},
        "recent": [{**s, "text": s["text"][:300]} for s in recent],
        "per_person": _rebuild_per_person(pool_for_person),
        "confirmed": style.get("confirmed", []),
        "samples": sorted(all_canonical + recent, key=lambda s: s.get("date") or "", reverse=True)[:30],
        "sample_keys": list(seen_keys | set(new_keys))[-500:],
        "preferences": style.get("preferences", []),
    })
    os.makedirs(_root(), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(style, f, indent=2)
    return {"messages_analyzed": style["messages_analyzed"],
            "canonical_counts": {b: len(canonical[b]) for b in ALL_BUCKETS},
            "people": len(style["per_person"])}


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg and not arg.lstrip().startswith("{") and os.path.exists(arg):
        raw = open(arg, encoding="utf-8").read()   # setup passes a file path (like prewarm_graph.py)
    else:
        raw = arg or sys.stdin.read()
    print(json.dumps(extract(json.loads(raw))))
