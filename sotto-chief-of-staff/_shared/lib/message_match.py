"""Who a source message is from or to: phone, WhatsApp-handle and email-recipient matching."""
import re
from email.utils import getaddresses


def _digits(s: str) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


_PHONE_HANDLE = re.compile(r"\+?[0-9\s().-]+\Z")
_WHATSAPP_PHONE_JID = re.compile(r"([0-9]+)@(s\.whatsapp\.net|c\.us)\Z", re.I)


def _phone_matches(a: str, b: str) -> bool:
    if not (_PHONE_HANDLE.fullmatch((a or "").strip())
            and _PHONE_HANDLE.fullmatch((b or "").strip())):
        return False
    da, db = _digits(a), _digits(b)
    if len(da) < 7 or len(db) < 7:
        return False
    from textutil import normalize_phone_for_comparison
    return normalize_phone_for_comparison(a) == normalize_phone_for_comparison(b)


def _handle_matches(handle: str, ident: str) -> bool:
    if handle == ident:
        return True
    if _jid_matches_phone(handle, ident) or _jid_matches_phone(ident, handle):
        return True
    if "@" in handle and "@" in ident:
        return handle.lower() == ident.lower()
    if "@" in handle or "@" in ident:
        return False
    return _phone_matches(handle, ident)


def _jid_matches_phone(jid: str, phone: str) -> bool:
    match = _WHATSAPP_PHONE_JID.fullmatch((jid or "").strip())
    return bool(match and _phone_matches(match.group(1), phone))


def _is_sent_email(m: dict) -> bool:
    labels = m.get("labelIds") or m.get("labels") or []
    return m.get("isSent") is True or (isinstance(labels, list)
                                     and "SENT" in {str(v).upper() for v in labels})


def _email_direction_known(m: dict) -> bool:
    """Drafts prove neither direction; otherwise a native SENT label is authoritative."""
    for field in ('labelIds', 'labels'):
        labels = m.get(field)
        if labels is not None and (not isinstance(labels, list)
                                   or any(not isinstance(value, str) for value in labels)
                                   or 'DRAFT' in {value.upper() for value in labels}):
            return False
    return 'isSent' not in m or type(m['isSent']) is bool or _is_sent_email(m)


def _email_recipients(m: dict) -> list:
    """Native recipient addresses; Bcc is evidence only on the owner's sent copy."""
    raw = []
    for field in (("to", "cc", "bcc") if _is_sent_email(m) else ("to", "cc")):
        v = m.get(field)
        raw += [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])
    return [addr.lower() for _name, addr in getaddresses(raw) if addr]
