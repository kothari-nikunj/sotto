"""Validate action destinations without turning contact IDs into phone numbers."""
import re
from urllib.parse import unquote


HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_LINK = re.compile(
    r'(?<![A-Za-z0-9/])(?:\[(?P<label>[^\]]*)\]\(\s*)?'
    r'<?(?P<scheme>imessage://|sms:|tel:|https://wa\.me/)'
    # A target must start with a plausible recipient character (digit/+/letter for an Apple ID) —
    # otherwise a bare "Tel:"/"sms:" in prose (no target follows) never matches at all, so its own
    # scheme text is never a candidate for deletion.
    r'(?P<target>[+A-Za-z0-9][^\s<>\]\)?&]*)[^\s<>\]\)]*>?(?(label)\s*\))', re.I)
_STUB = re.compile(r'[\W_]*(?:tap\s+(?:to\s+send|here)|send|reply)?[\W_]*', re.I)
# wa.me also serves non-phone click-to-chat paths (e.g. /message/<id>). Only a target that already
# looks phone-shaped is ours to validate; anything else is a URL, not a recipient we can judge.
_PHONEY = re.compile(r'\+?[0-9][0-9(). -]*')


def email_address(value: str) -> str:
    """A mailbox or Apple ID, excluding protocol identities that resemble mail."""
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'+/=_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value):
        return ''
    if value.lower().endswith(('@s.whatsapp.net', '@c.us', '@g.us', '@lid', '@rbm.goog')):
        return ''
    return value


def normalized_target(channel: str, identifier: str) -> str:
    """Return a supported address, or empty when no safe action link can be built.

    Only phone punctuation is removable. Bare 5/6-digit SMS short codes are
    supported in Messages; graph IDs, business handles and group IDs are not.
    Validation establishes address syntax, not identity: callers must resolve
    the recipient from the actual conversation or contact first.
    """
    value = identifier.strip() if isinstance(identifier, str) else ""
    channel = channel.lower()
    if channel in ('email', 'gmail', 'apple_mail', 'mail'):
        return email_address(value)
    if channel in ('whatsapp', 'whatsapp_call'):
        jid = re.fullmatch(r'([0-9]{7,15})@(?:s\.whatsapp\.net|c\.us)', value)
        if jid:
            value = jid[1]
    if channel in ('imessage', 'facetime') and '@' in value:
        return email_address(value)
    if not re.fullmatch(r'\+?[0-9(][0-9(). -]*', value):
        return ''
    phone = re.sub(r'[(). -]', '', value)
    if re.fullmatch(r'\+?[0-9]{7,15}', phone):
        return phone.lstrip('+') if channel in ('whatsapp', 'whatsapp_call') else phone
    if channel in ('imessage', 'sms') and re.fullmatch(r'[0-9]{5,6}', value):
        return value
    return ''


def strip_invalid_links(text: str) -> str:
    """Remove unsupported action URLs and orphaned 'Tap to send' lines.

    Keep labels, draft text and valid destinations verbatim. Use the same target
    rules as the builder, including Messages email addresses and SMS short codes.
    """
    def replace(match):
        channel = {'imessage://': 'imessage', 'sms:': 'sms', 'tel:': 'tel',
                   'https://wa.me/': 'whatsapp'}[match['scheme'].lower()]
        target = unquote(match['target'])
        if channel == 'whatsapp' and not _PHONEY.fullmatch(target):
            return match[0]
        # A URL must already contain a normalized address. Do not repair a bad
        # address at delivery time, when its original provenance is unavailable.
        if normalized_target(channel, target) == target:
            return match[0]
        return match['label'] or ''

    lines = []
    for line in text.split('\n'):
        clean = _LINK.sub(replace, line)
        if clean != line:
            clean = clean.rstrip()
            if _STUB.fullmatch(clean):
                previous = len(lines) - 1
                while previous >= 0 and not lines[previous].strip():
                    previous -= 1
                if previous >= 0 and _STUB.fullmatch(lines[previous]):
                    del lines[previous:]
                continue
        lines.append(clean)
    return '\n'.join(lines)


# Remove unsupported recipient links and mailto URLs from unattended output.
_MAILTO_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*mailto:[^)]*\)")
_MAILTO_URL_RE = re.compile(r"<?mailto:[^\s<>\]\)]+>?")


def strip_unattended_links(text: str) -> str:
    """Remove every `mailto:` link, and every other tap link whose recipient message_targets does
    not validate (phone 7-15 digits, a Messages short code, or an Apple ID email) — keeping its
    label text if it had one — from a message body, and any line that was nothing but one."""
    text = strip_invalid_links(text)
    if "mailto:" not in text:
        return text
    kept = []
    for line in text.split("\n"):
        rest = line
        if "mailto:" in rest:
            rest = _MAILTO_URL_RE.sub("", _MAILTO_MD_LINK_RE.sub(r"\1", rest))
        if rest == line:
            kept.append(line)
            continue
        if _STUB.fullmatch(rest):
            continue
        kept.append(rest.rstrip())
    return "\n".join(kept)


def delivery_body(text: str) -> str:
    body = strip_unattended_links(HTML_COMMENT_RE.sub('', text or ''))
    return re.sub(r'\n{3,}', '\n\n', body).strip()
