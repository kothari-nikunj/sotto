#!/usr/bin/env python3
"""
textutil.py — string / identifier / domain normalization primitives for the brief pipeline.

Extracted verbatim from compose_brief.py (the 2,400-line monolith split) with ZERO behavior
change. This is the leaf utility layer: defensive dict accessors (_arr/_obj/_s), phone/email/
identifier normalization (contacts.ts port), strict name matching (name-matching.ts port),
automated-sender detection, and email-domain helpers (_base_domain/_is_excluded_domain/
_sender_addr) that the signal-correlation code relies on. No dependency on any sibling module.

compose_brief.py re-exports every name here at its old location, so `import compose_brief as cb;
cb._s(...)` and the ~10 sibling scripts that do `import compose_brief as cb` keep working.
"""
from __future__ import annotations

import re
import unicodedata


# ---------------------------------------------------------------------------
# Defensive accessors — every source may be missing before the Bridge emits it.
# ---------------------------------------------------------------------------

def _arr(d, key):
    v = (d or {}).get(key)
    return v if isinstance(v, list) else []




def _obj(d, key):
    v = (d or {}).get(key)
    return v if isinstance(v, dict) else {}




def _s(v) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))




# ---------------------------------------------------------------------------
# Contact-name resolution (port of api/src/lib/contacts.ts)
# ---------------------------------------------------------------------------

def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")




def normalize_phone_for_comparison(phone: str) -> str:
    """Last 10 digits — handles US country-code variations (contacts.ts)."""
    d = _digits(phone)
    return d[-10:] if len(d) >= 10 else d




def _format_phone_for_display(d: str) -> str:
    if len(d) == 10:
        return f"({d[0:3]}) {d[3:6]}-{d[6:]}"
    if len(d) == 11 and d.startswith("1"):
        return f"+1 ({d[1:4]}) {d[4:7]}-{d[7:]}"
    if len(d) > 11 and d.startswith("1"):
        return f"+1 ({d[1:4]}) {d[4:7]}-{d[7:11]}"
    if len(d) > 10:
        return f"({d[0:3]}) {d[3:6]}-{d[6:10]}"
    if len(d) > 0:
        return f"+{d}"
    return "Unknown"




def _normalize_identifier(idv: str) -> str:
    trimmed = (idv or "").strip().lower()
    before_at = re.sub(r"@.*", "", trimmed)
    if re.fullmatch(r"[\d\s\-\+\(\)]+", before_at or ""):
        return _digits(trimmed)[-10:]
    return trimmed




def _looks_like_phone_number(name: str) -> bool:
    """Port of api/src/lib/name-matching.ts `looksLikePhoneNumber`: a thread whose resolved
    name is empty, starts with '+' or '(', or is >60% digits never resolved to a real contact —
    it's a raw phone number / shortcode / OTP sender, i.e. an unknown."""
    n = (name or "").strip()
    if not n:
        return True
    if n.startswith("+") or n.startswith("("):
        return True
    digits = sum(c.isdigit() for c in n)
    return digits > len(n) * 0.6




def _normalize_name_key(name: str) -> str:
    """Port of name-matching.ts normalizeNameKey: lowercase, trim, strip diacritics (NFD) so
    'Tomás' matches 'Tomas'."""
    n = unicodedata.normalize("NFD", (name or "").lower().strip())
    return "".join(c for c in n if not unicodedata.combining(c))




def _names_match(attendee_name: str, contact_name: str) -> bool:
    """STRICT port of name-matching.ts namesMatch — deliberately avoids first-name-only matches
    ('Marcus' must NOT match 'Marcus Wallace'; the caller verifies those via email instead). Rules:
    exact match; or first names equal AND (last names equal | last-initial | 3-char last prefix)."""
    a = _normalize_name_key(attendee_name)
    c = _normalize_name_key(contact_name)
    if not a or not c:
        return False
    if a == c:
        return True
    a_parts, c_parts = a.split(), c.split()
    if a_parts[0] != c_parts[0]:
        return False
    if len(a_parts) == 1 or len(c_parts) == 1:
        return False  # first-name-only — too many false positives; defer to email
    a_last, c_last = a_parts[-1], c_parts[-1]
    if a_last == c_last:
        return True
    if len(a_last) == 1 and c_last.startswith(a_last):
        return True
    if len(c_last) == 1 and a_last.startswith(c_last):
        return True
    if len(a_last) >= 3 and len(c_last) >= 3:
        if a_last.startswith(c_last[:3]) or c_last.startswith(a_last[:3]):
            return True
    return False




def _is_likely_automated(email_address: str) -> bool:
    """Port of contacts.ts isLikelyAutomated — no-reply / notifications / billing senders."""
    email = (email_address or "").strip().lower()
    if not email or "@" not in email:
        return False
    local = email.split("@")[0]
    if re.match(r"^(no-?reply|do-?not-?reply|donotreply|notifications?|alerts?|updates?|support|"
                r"billing|receipts?|mailer-daemon|postmaster)([+._-]|$)", local):
        return True
    return bool(re.search(r"\b(no-?reply|do-?not-?reply|donotreply|notification|receipt|automated)\b", email))




# ── Email-domain helpers (used by signal correlation; port of api/src/pipeline/signals.ts) ──────────
# The domain sets + _base_domain/_is_excluded_domain/_sender_addr that keep signal correlation
# high-signal: hosting/consumer domains never count, so a shared corporate domain is never guessed as a
# person match. The correlation logic that consumes these lives in compose_brief._correlate_signals.
_HOSTING_DOMAINS = {"google.com", "dropbox.com", "box.com", "live.com", "amazonaws.com", "cloudfront.net",
                    "github.com", "githubusercontent.com", "sharepoint.com", "notion.so", "gstatic.com",
                    "googleapis.com", "icloud.com"}


_CONSUMER_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com",
                     "protonmail.com", "proton.me", "me.com", "mac.com"}




def _is_excluded_domain(dom: str) -> bool:
    return not dom or dom in _HOSTING_DOMAINS or dom in _CONSUMER_DOMAINS




def _base_domain(host_or_email: str) -> str:
    """'john@help.salesforce.com' / 'help.salesforce.com' → 'salesforce.com' (light ccTLD handling)."""
    h = _s(host_or_email).lower().strip()
    if "@" in h:
        h = h.split("@")[-1]
    h = h.split("//")[-1].split("/")[0]
    parts = [p for p in h.split(".") if p]
    if len(parts) < 2:
        return ""
    if len(parts) >= 3 and parts[-1] in ("uk", "au", "br", "in", "jp") and parts[-2] in ("co", "com", "net", "org", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])




def _sender_addr(from_header: str) -> str:
    s = _s(from_header)
    m = re.search(r"<([^>]+)>", s)
    a = (m.group(1) if m else s).strip().lower()
    return a if "@" in a else ""




def _extract_sender_name(from_header: str) -> str:
    """'Sarah Chen <sarah@acme.com>' → 'Sarah Chen'; bare address → '' (no display name)."""
    f = _s(from_header).strip()
    if "<" in f:
        return f.split("<", 1)[0].strip().strip('"')
    return "" if "@" in f else f
