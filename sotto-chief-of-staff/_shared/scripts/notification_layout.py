"""How one admitted nudge reads on the owner's phone, and whether it may be sent at all."""
import json
import re

import action_links
import pending_offer
from brief_validate import is_urgent
from funnel_policy import MEETING_HOLD_EXEMPT_CLASSES
from message_targets import normalized_target
from timeutil import parse_observed_time

# The send rule: a nudge goes out only when it carries a draft or something the owner can answer,
# unless it is time-critical (the classes the in-meeting hold already lets through, a Tier-1
# `urgent` verdict, or something due within a day) or the owner asked for it (a reminder they set,
# a birthday). Anything else keeps its state for the brief.
SEND_WITHOUT_ASK_KINDS = MEETING_HOLD_EXEMPT_CLASSES | {'urgent', 'intention', 'birthday'}
WITHHELD = 'withheld:'  # marker in proactive/<date>.json `nudged`, beside the scan's `held:`
REPLY_CHANNELS = ('email', 'gmail', 'apple_mail', 'mail', 'imessage', 'sms', 'whatsapp')


class Copy(str):
    """Rendered text that knows whether it carries a draft or something the owner can answer."""
    def __new__(cls, text, asks):
        value = super().__new__(cls, text)
        value.asks = asks
        return value


def quoted(draft):
    return '“' + draft.strip() + '”'


def reply_offer(item):
    """A self-contained offer for a direct ask that has no draft yet; the owner's reply names the
    person, so it never depends on the chat session having seen this nudge."""
    person = str(item.get('person') or '').strip()
    if (not (item.get('inbound') or item.get('kind') == 'chase') or not person or '@' in person
            or item.get('is_group_chat') or item.get('channel') not in REPLY_CHANNELS
            or not normalized_target(item.get('channel', ''), item.get('identifier', ''))):
        return ''
    noun = 'a reply' if item.get('inbound') else 'a note'
    return f'Say “draft {noun} to {person}” and I’ll write one.'


def _labeled_link(link, person):
    if link.startswith('tel:'):
        label = f'Call {person}'
    elif link.startswith('https://wa.me/'):
        label = 'Open draft in WhatsApp'
    else:
        label = 'Open draft in Messages'
    return f'{label}:\n{link}'


def render(row, item):
    """One paragraph each for what happened, the marked draft(s), and the question or offer."""
    text, draft, decline = row['text'].strip(), row['draft'].strip(), row['decline'].strip()
    if item.get('kind') == 'scheduling_ask' and not item.get('slots'):
        return Copy(text.rstrip(' ?') + ' How would you like to respond?', True)
    if item.get('slots'):
        draft = draft.replace('{{slots}}', ' or '.join(item['slots']))
    if '{{slots}}' in draft:
        raise ValueError('unresolved scheduling options')
    channel, ident = item.get('channel', ''), item.get('identifier', '')
    paragraphs = [text]
    if draft:
        paragraphs.append(('Draft to accept:\n' if decline else 'Draft:\n') + quoted(draft))
    if decline:
        paragraphs.append('Draft to decline:\n' + quoted(decline))
        if ident and not item.get('is_group_chat'):
            action_links.record_draft(channel, ident, decline, 'decline')
    if draft and ident and not item.get('is_group_chat'):
        if channel in ('email', 'gmail', 'apple_mail', 'mail'):
            action_links.record_draft(channel, ident, draft, 'reply')
            # Two alternatives require an explicit choice, never bind a bare yes to the accept.
            if not decline:
                paragraphs.append('Want this in your Gmail drafts?')
                pending_offer.set_offer('chase' if item.get('kind') == 'chase' else 'commitment', 'Want this in your Gmail drafts?', item.get('person', ''),
                                        json.dumps({'to': ident, 'body': draft, 'thread_id': item.get('thread_id', '')}),
                                        anchor_key=item.get('anchor_key', ''), source_loop_snapshot=item.get('_loop_snapshot') or {})
        elif channel in ('imessage', 'sms', 'whatsapp', 'whatsapp_call', 'phone', 'facetime', 'tel'):
            link = action_links.link_for(channel, ident, draft, action_type='reply')
            if link:
                paragraphs.append(_labeled_link(link, item.get('person') or ident))
    if not draft and not decline:
        paragraphs.append(reply_offer(item))
    paragraphs = [p for p in paragraphs if p]
    return Copy('\n\n'.join(paragraphs), len(paragraphs) > 1)


def urgent(item, now):
    """Time-critical or owner-set (SEND_WITHOUT_ASK_KINDS), or due within a day by the brief's own clock."""
    raw = str(item.get('action_due_date') or item.get('deadline') or '').strip()
    # A calendar date is used as written; anything else must be an ISO instant, never a bare number.
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
        due = raw
    else:
        stamp = parse_observed_time(raw) if re.match(r'\d{4}-\d{2}-\d{2}T', raw) else None
        due = stamp.astimezone(now.tzinfo).date().isoformat() if stamp else ''
    return (item['kind'] in SEND_WITHOUT_ASK_KINDS or bool(item.get('location_update'))
            or is_urgent({'deadline': due}, now.date().isoformat()))


def sends(item, rendered, now):
    """The send rule. A template carries its own question (a hand-off, a prep offer) when it ends with one."""
    return (rendered.asks if isinstance(rendered, Copy) else rendered.endswith('?')) or urgent(item, now)


def withhold(date, key):
    """Record a withheld proactive key in the scan's day state (the same proactive_state transaction the
    scan and delivery finalisation use), so it is not proposed or charged again today unless urgent."""
    import delivery_effects
    with delivery_effects.proactive_state(date) as state:
        state['nudged'] = sorted(set(state['nudged']) | {WITHHELD + key})
