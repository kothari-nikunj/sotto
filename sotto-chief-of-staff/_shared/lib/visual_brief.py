"""Deterministic, optional image presentation of the SAME composed brief/prep.

No model calls, inference, new relevance policy, or external image service. Cards quote complete
source paragraphs. Overlong or excess material remains in the original text, never shrunk/cropped.
The manifest retains exact source text and selection offsets for review and text-on-request.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import re

from chatfmt import to_imessage
from brief_validate import is_calendar_preview_note
from calendar_context import SCHEDULE_DAY as _DAY

VERSION = 15
MAX_CARDS = 4
MAX_WORDS = 200
MAX_PREP_WORDS = 200
WIDTH, HEIGHT = 1080, 1920
FONT_DIR = Path(__file__).resolve().parents[1] / 'assets/card-fonts'
SEAL_PATH = Path(__file__).resolve().parents[1] / 'assets/wax-seal.png'


def _heading(line, kind):
    line = line.rstrip(':').strip()
    if kind == 'brief':
        return {'needs attention now': 'Needs you', 'should handle today': 'Today',
                'coming up': 'Your day', 'already handled': 'Handled', '✅ already handled': 'Handled', 'still open': 'Open loops', 'still pending': 'Open loops', 'weekly review': 'Open loops', 'what moved today': 'Handled'}.get(line.lower())
    if line.lower() == 'the space & why now':
        return 'Context'
    if line.lower() in ('the founder', 'traction & signals'):
        return {'the founder': 'The founder', 'traction & signals': 'Signals'}[line.lower()]
    if line.lower() == 'your thread':
        return 'Your relationship'
    if re.fullmatch(r'what .+ builds', line, re.I):
        return 'The business'
    if line.lower() in ('angles', 'talking points', 'questions to ask'):
        return 'Questions to ask'
    return None


def build(text, kind='brief', preview=False):
    """An extractive preview, not a second composition. None means keep ordinary text delivery."""
    if kind not in ('brief', 'prep') or not isinstance(text, str) or len(text) > 100_000:
        return None
    plain = to_imessage(text, normalize_style=False).strip()
    if not plain:
        return None
    # Preserve explicit source emphasis before converting markdown to plain source text.
    # No name inference: an unlabelled sentence remains ordinary body text.
    source_emphasis = {}
    for raw in text.splitlines():
        match = re.match(r'^\s*(?:[-•]\s+)?(?P<mark>\*\*|\*)(?P<label>[^*\n]+)(?P=mark)', raw)
        if match:
            key = to_imessage(raw, normalize_style=False).strip().lstrip('• ').strip()
            source_emphasis[key] = len(match['label'])
    lines = plain.splitlines()
    title = lines[0].strip()
    sections, active = [], None
    agenda_day = ''
    preamble, filtered = [], False
    for index, raw in enumerate(lines):
        line = raw.strip().lstrip('• ').strip()
        if not line:
            continue
        heading = _heading(line, kind)
        if heading:
            active = {'title': heading, 'blocks': []}
            sections.append(active)
            agenda_day = ''
        elif line.lower() in ('already handled', '✅ already handled', 'filtered', 'still open',
                              'the founder', 'traction & signals', 'the space & why now'):
            active = None
            filtered = line.lower() == 'filtered'
        elif active is not None:
            if kind == 'brief' and active['title'] == 'Your day':
                if is_calendar_preview_note(line):
                    continue
                if re.fullmatch(_DAY, line.rstrip(':'), re.I):
                    agenda_day = line.rstrip(':')
                    continue
            # Long paragraphs and links belong in text. Never cut off a qualifier, date or amount.
            if len(line.split()) <= MAX_WORDS and (kind == 'brief' or not re.search(r'https?://|mailto:|/app#', line)):
                block = {'text': line, 'line': index}
                if line in source_emphasis:
                    block['emphasis_end'] = source_emphasis[line]
                if agenda_day and active['title'] == 'Your day':
                    block['day'] = agenda_day
                active['blocks'].append(block)
            elif kind == 'brief':
                return None  # never hide an action merely because it is hard to fit
        elif kind == 'brief' and index > 0 and not filtered:
            preamble.append(_display_text(line))
    if kind == 'prep':
        groups = [('Your relationship', ('Your relationship', 'The founder')),
                  ('The business', ('The business',)),
                  ('Signals & context', ('Signals', 'Context')),
                  ('Questions to ask', ('Questions to ask',))]
        sections = [{'title': title, 'blocks': [dict(b, section=part['title'])
                    for part in sections if part['title'] in titles for b in part['blocks']]}
                    for title, titles in groups]
    else:
        extras = [dict(b, section=name) for name in ('Open loops', 'Handled')
                  for part in sections if part['title'] == name for b in part['blocks']]
        sections = [part for part in sections if part['title'] not in ('Handled', 'Open loops')]
        if extras:
            sections.append({'title': 'Follow-ups', 'blocks': extras})
    cards = []
    for section in sections:
        blocks, words = [], 0
        required = kind == 'brief'
        limit = MAX_PREP_WORDS if kind == 'prep' and section['title'] != 'Your relationship' else MAX_WORDS
        for block in section['blocks']:
            count = len(block['text'].split())
            if words + count > limit:
                if not required:
                    break  # prep/schedule cards explicitly present selected highlights
                cards.append({'title': section['title'], 'blocks': blocks})
                blocks, words = [], 0
            blocks.append(block)
            words += count
        if blocks:
            cards.append({'title': section['title'], 'blocks': blocks})
    if kind == 'prep' and len(cards) > MAX_CARDS:
        return None  # keep the whole brief in text instead of losing an actionable ask
    # Four attachments produce the compact native gallery. Split only at real paragraph
    # boundaries; if there is too little source material, keep the short update as text.
    try:
        cards = _split_to_four(cards)
    except LayoutError:
        return None
    selected = {b['line'] for c in cards for b in c['blocks']}
    omitted = any(line.strip() and i not in selected and i > 0 and not _heading(line.strip(), kind)
                  for i, line in enumerate(lines))
    deck = {'version': VERSION, 'kind': kind, 'title': title, 'preview': bool(preview),
            'full_text': plain, 'cards': cards, 'excerpt': omitted}
    deck['id'] = hashlib.sha256(json.dumps(deck, sort_keys=True).encode()).hexdigest()[:24]
    # Keep the companion text short; dates and links remain selectable in Messages.
    deck['summary'] = ('Historical preview · ' if preview else '') + (_brief_caption(title) if kind == 'brief' else _display_text(title))
    if kind == 'brief':
        if preamble:
            deck['summary'] += '\n' + '\n'.join(preamble)
        links = _companion_links(plain)
        if links:
            deck['summary'] += '\n' + '\n'.join(links)
        if len(deck['summary'].encode('utf-16-le')) // 2 > 1000:
            return None  # preserve usable links without overflowing the transport caption
    return deck


def _companion_links(text):
    links = []
    for link in re.findall(r'https?://[^\s<>]+|mailto:[^\s<>]+', text):
        link = link.rstrip('.,;:!?')
        while link and link[-1] in ')]}':
            closer = link[-1]
            opener = {')': '(', ']': '[', '}': '{'}[closer]
            if link.count(closer) <= link.count(opener):
                break
            link = link[:-1].rstrip('.,;:!?')
        if link not in links:
            links.append(link)
    return links


class LayoutError(ValueError):
    """Only fixed reason codes and a card number may cross the diagnostics boundary."""
    def __init__(self, reason, message, card_index=None):
        super().__init__(message)
        self.reason = reason
        self.card_index = card_index


def _lines(draw, text, font, width):
    output, line = [], ''
    for word in text.split():
        if draw.textlength(word, font=font) > width:
            raise LayoutError('word_too_wide', 'card contains a word too wide to render')
        candidate = (line + ' ' + word).strip()
        if line and draw.textlength(candidate, font=font) > width:
            output.append(line)
            line = word
        else:
            line = candidate
    if line:
        output.append(line)
    return output


def _brief_caption(title):
    match = re.fullmatch(r'Good (morning|afternoon|evening)\s*[—–-]\s*(.+)', title, re.I)
    if not match:
        return _display_text(title)
    period, date = match.groups()
    date = re.sub(r'^[A-Za-z]+,\s*', '', date)
    return f"Good {period.lower()}. Here's your {period.lower()} brief for {date}."


def _display_text(text, kind=''):
    # Old archived briefs may still contain a dashboard-relative link. It is not a chat URL.
    text = re.sub(r'\s*[—–:-]?\s*see /app#loops\b', ". Ask me what's still open.", text, flags=re.I)
    text = re.sub(r'(?<![\w/])/app#loops\b', 'your open items', text)
    if kind == 'brief':
        text = re.sub(r'^(.{1,55}?) [—–-] ', r'\1: ', text)
    return text.replace(' — ', ' · ').replace('—', '; ')


# Recognize the formats already emitted by the composer, including date-prefixed rows.



def _agenda_parts(text):
    match = re.fullmatch(
        rf'(?:(?P<day>{_DAY})[,:]?\s+)?'
        r'(?P<time>\d{1,2}(?::\d{2})?\s*[AP]M|All day)\s*[:—–-]\s*(?P<event>.+)',
        text, re.I)
    if not match:
        return '', None, _display_text(text), ''
    title, delimiter, location = match['event'].partition(' | Location: ')
    if not delimiter:
        # Only an explicit street address may move out of parentheses. Attendees stay in place.
        address = re.search(r'\s+\((\d+\s+[^()]*\b(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|'
                            r'Dr|Drive|Ln|Lane|Way)\b[^()]*)\)$', title, re.I)
        if address:
            title, location = title[:address.start()], address[1]
    return match['day'] or '', match['time'], _display_text(title), _display_text(location.strip())


def _birthday_parts(text):
    plain = re.sub(r'^🎂[\ufe0f\ufe0e]?\s*', '', text)
    match = re.fullmatch(r'(.+?)\s+(?:[·—–-]\s*|\()(birthday\s+[^()]+)\)?', plain, re.I)
    if match:
        return match[1], re.sub(r'^birthday', 'Birthdays', match[2], flags=re.I)
    if plain != text:
        return plain, 'Birthdays'
    return None


def _emphasis_end(text, kind):
    """Only style explicit leading labels; don't infer names or rewrite source content."""
    if kind == 'brief':
        name = re.match(r'^(.{1,55}?) [—–-] ', text)
        if name:
            return len(name[1])
    label = re.match(r'^([^:\n]{1,65}):(?=\s)', text)
    if label and not re.search(r'\d$', label[1]):
        return label.end()
    return 0


def _rich_lines(draw, text, regular, bold, width, emphasis_end):
    """Wrap using the actual weight of each word so emphasis cannot cause clipping."""
    lines, current, used = [], [], 0
    for match in re.finditer(r'\S+', text):
        word = match[0]
        face = bold if match.start() < emphasis_end else regular
        word_width = draw.textlength(word, font=face)
        if word_width > width:
            raise LayoutError('word_too_wide', 'card contains a word too wide to render')
        gap = draw.textlength(' ', font=regular) if current else 0
        if current and used + gap + word_width > width:
            lines.append(current)
            current, used, gap = [], 0, 0
        current.append((word, face, used + gap))
        used += gap + word_width
    if current:
        lines.append(current)
    return lines


def _content_top(top, height):
    available = HEIGHT - 106 - top
    if height > available:
        raise LayoutError('card_height', 'card exceeds readable height; use text')
    return top


def _render_pages(deck, *, measure_only=False):
    """One layout path for fit checks and PNG delivery; never shrink text to fit."""
    from PIL import Image, ImageDraw, ImageFont
    if not measure_only and len(deck['cards']) != MAX_CARDS:
        raise LayoutError('card_count', 'an image brief must contain exactly four cards')
    def font(size, weight=400):
        face = ImageFont.truetype(str(FONT_DIR / 'Inter.ttf'), size)
        face.set_variation_by_axes([32 if size >= 60 else 14, weight])
        return face
    label_font, meta_font = font(36), font(28)
    seal = Image.open(SEAL_PATH).convert('RGBA')
    seal.thumbnail((78, 78), Image.Resampling.LANCZOS)
    seal.putalpha(seal.getchannel('A').point(lambda alpha: round(alpha * 0.42)))
    body_font, heading_font = font(48), font(76, 650)
    bold_font, section_font = font(48, 650), font(36, 650)
    row_font, time_font = font(48), font(36, 650)
    location_font = font(36)
    for index, card in enumerate(deck['cards'], 1):
        image = Image.new('RGB', (1, 1) if measure_only else (WIDTH, HEIGHT), '#F5F5F7')
        draw = ImageDraw.Draw(image)
        if measure_only:
            # Text measurement still uses Pillow; measuring must not paint or trigger
            # instrumentation intended for actual delivered pages.
            for method in ('text', 'line', 'ellipse', 'rounded_rectangle'):
                setattr(draw, method, lambda *args, **kwargs: None)
        # The company/person leads; the seal stays small and secondary.
        blocks = [b['text'] for b in card['blocks']]
        agenda = card['title'] == 'Your day'
        # These are typographic splits of the source, never inferred people/actions.
        title = 'Your day' if agenda else card['title']
        if deck['kind'] == 'prep':
            subject = re.split(r' [—–-] ', deck['title'], maxsplit=1)[0]
            company = re.search(r'\(([^()]+)\)', subject)
            title = company.group(1) if company else subject
        title = _display_text(title)
        title_lines = _lines(draw, title, heading_font, 870)
        if len(title_lines) > 2:
            raise LayoutError('heading_height', 'card heading exceeds readable height')
        y = 70
        if deck['kind'] == 'brief':
            # Use the composition's own date, never today's date during a replay.
            edition = re.sub(r'^Good (morning|afternoon|evening)\s*[—–-]\s*',
                             lambda match: match[1].capitalize() + ' brief · ', deck['title'], flags=re.I)
            edition = _display_text(edition)
            if draw.textlength(edition, font=meta_font) > WIDTH - 128:
                raise LayoutError('date_width', 'brief date exceeds readable width')
            draw.text((64, 45), edition, font=meta_font, fill='#6E6E73')
            y = 108
        for line in title_lines:
            draw.text((64, y), line, font=heading_font, fill='#1D1D1F')
            y += 88
        subtitle = {'Your relationship': 'Relationship & history',
                    'The business': 'Business', 'The founder': 'Founder',
                    'Questions to ask': 'Questions', 'Your day': 'Calendar highlights',
                    'Needs you': 'Time-sensitive', 'Today': 'To take care of',
                    'Follow-ups': 'Still open & handled'}.get(card['title'], card['title'])
        if subtitle != title:
            draw.text((64, y + 9), subtitle, font=label_font, fill='#6E6E73')
            y += 54 if deck['kind'] == 'brief' else 90
        else:
            y += 42
        y += 32  # stable breathing room below the fixed header
        if agenda:
            rows, current_day = [], ''
            for block, source in zip(blocks, card['blocks']):
                day, time, event, location = _agenda_parts(block)
                day = day or source.get('day', '')
                day_label = day if day and day != current_day else ''
                if day:
                    current_day = day
                birthday = _birthday_parts(event) if not time else None
                birthday_label = birthday[1] if birthday else ''
                if birthday:
                    event = birthday[0]
                lines = _lines(draw, event, row_font, 702 if time else 902)
                location_lines = _lines(draw, location, location_font, 702) if location else []
                padding = (44 if birthday else 36) if rows and (birthday or day_label) else 0
                label = ' · '.join(part for part in (day_label, birthday_label) if part)
                if draw.textlength(label, font=section_font) > 904:
                    raise LayoutError('calendar_label_width', 'calendar day label exceeds readable width; use text')
                height = max(100, len(lines) * 62 + 32 + len(location_lines) * 44) if time else len(lines) * 62 + 30
                rows.append((time, lines, height + padding + (46 if label else 0),
                             location_lines, padding, label))
            y = _content_top(y, sum(row[2] for row in rows))
            for time, lines, height, location_lines, padding, label in rows:
                y += padding
                height -= padding
                if label:
                    draw.text((88, y), label, font=section_font, fill='#6E6E73')
                    y += 46
                    height -= 46
                if time:
                    draw.text((88, y + 5), time, font=time_font, fill='#0066CC')
                    draw.line((300, y + height - 14, 1016, y + height - 14), fill='#DEDEE3', width=1)
                for offset, line in enumerate(lines):
                    draw.text((300 if time else 88, y + offset * 62), line, font=row_font, fill='#1D1D1F')
                for offset, line in enumerate(location_lines):
                    draw.text((300, y + len(lines) * 62 + 4 + offset * 44), line,
                              font=location_font, fill='#6E6E73')
                y += height

        else:
            blocks = [_display_text(block, deck['kind']) for block in blocks]
            emphasis = [max(_emphasis_end(block, deck['kind']), source.get('emphasis_end', 0))
                        for block, source in zip(blocks, card['blocks'])]
            bullets = [deck['kind'] == 'prep' and len(blocks) > 1 and not end for end in emphasis]
            counts = [bool(re.match(r'^\d+ other open loops?\b', block)) for block in blocks]
            line_heights = [48 if count else 62 for count in counts]
            laid_out = [_rich_lines(draw, block, label_font if count else body_font,
                                    label_font if count else bold_font, 878 if bullet else 904, end)
                        for block, bullet, end, count in zip(blocks, bullets, emphasis, counts)]
            sections = [b.get('section', '') for b in card['blocks']]
            merged = card.get('section_labels') or card['title'] in ('Your relationship', 'Signals & context', 'Follow-ups')
            starts = [merged and name and (i == 0 or name != sections[i-1]) for i, name in enumerate(sections)]
            # Measure the actual wrapped text, then spend spare height on paragraph gaps.
            # Dense cards may tighten spacing, but never shrink type or omit a paragraph.
            gaps = max(0, len(blocks) - 1)
            section_gap = 12
            fixed_height = (sum(len(lines) * line_height for lines, line_height in zip(laid_out, line_heights)) + 56
                            + sum(bool(x) for x in starts) * 46
                            + sum(bool(x) for x in starts[1:]) * section_gap)
            available = HEIGHT - 106 - y
            paragraph_gap = max(12, min(24, (available - fixed_height) // gaps)) if gaps else 24
            height = fixed_height + gaps * paragraph_gap
            y = _content_top(y, height)
            draw.rounded_rectangle((64, y, 1016, y + height), radius=32, fill='#FFFFFF')
            y += 24
            for position, lines in enumerate(laid_out):
                if starts[position]:
                    if position:
                        y += section_gap
                    draw.text((88, y), {'Your relationship': 'Your history', 'The founder': 'Background', 'Open loops': 'Still open'}.get(sections[position], sections[position]), font=section_font, fill='#1D1D1F')
                    y += 46
                bullet = bullets[position]
                if bullet:
                    draw.ellipse((88, y + 22, 96, y + 30), fill='#86868B')
                for line in lines:
                    for word, face, offset in line:
                        draw.text((88 + (26 if bullet else 0) + offset, y), word, font=face,
                                  fill='#6E6E73' if counts[position] else '#1D1D1F')
                    y += line_heights[position]
                if position < len(laid_out)-1:
                    y += paragraph_gap
        # Keep historical context explicit without repeating a second headline.
        footer = 'Historical preview' if deck['preview'] else ''
        seal_bounds = seal.getbbox()
        image.paste(seal, (64 - seal_bounds[0], HEIGHT - 90), seal)
        draw.text((154, HEIGHT - 65), footer, font=meta_font, fill='#6E6E73')
        for dot in range(MAX_CARDS):
            x = 928 + dot * 22
            draw.ellipse((x, HEIGHT - 56, x + 9, HEIGHT - 47), fill='#1D1D1F' if dot == index - 1 else '#D1D1D6')
        yield image


def _measure(deck, cards):
    # Same wrapping, fonts, headers and gaps as the PNG path, on a one-pixel canvas.
    # No artifacts and no second formula for deciding whether a page fits.
    completed = 0
    try:
        for page in _render_pages(dict(deck, cards=cards), measure_only=True):
            page.close()
            completed += 1
    except LayoutError as error:
        error.card_index = completed + 1
        raise


def _split_to_four(cards):
    cards = list(cards)
    while len(cards) < MAX_CARDS:
        candidates = [c for c in cards if len(c['blocks']) > 1]
        if not candidates:
            raise LayoutError('card_count', 'an image brief must contain exactly four cards')
        largest = max(candidates, key=lambda c: sum(len(b['text']) for b in c['blocks']))
        at, cut = cards.index(largest), max(1, len(largest['blocks']) // 2)
        cards[at:at + 1] = [dict(largest, blocks=largest['blocks'][:cut]),
                           dict(largest, blocks=largest['blocks'][cut:])]
    return cards


def _packed(deck):
    """Keep ordinary layouts; repair crowded briefs at complete paragraph boundaries."""
    cards = deck['cards']
    try:
        if len(cards) == MAX_CARDS:
            _measure(deck, cards)
            return deck
    except LayoutError:
        if deck['kind'] != 'brief':
            raise
    if deck['kind'] != 'brief':
        raise LayoutError('card_count', 'an image brief must contain exactly four cards')

    # Only adjacent action sections share a page. Calendar rows retain their own
    # layout, day labels and blue times; follow-ups retain Still open/Handled labels.
    groups = []
    for card in cards:
        group = 'actions' if card['title'] in ('Needs you', 'Today') else card['title']
        blocks = [dict(b, section=b.get('section') or card['title']) for b in card['blocks']]
        if groups and groups[-1][0] == group:
            groups[-1][1].extend(blocks)
        else:
            groups.append((group, blocks))

    packed = []
    for group, blocks in groups:
        def candidate(items):
            sections = {b['section'] for b in items}
            title = (next(iter(sections)) if len(sections) == 1 else 'Today') if group == 'actions' else group
            return {'title': title, 'blocks': items,
                    'section_labels': group == 'actions' and len(sections) > 1}
        current = []
        for block in blocks:
            proposed = candidate([*current, block])
            try:
                if sum(len(b['text'].split()) for b in proposed['blocks']) > MAX_WORDS:
                    raise LayoutError('card_height', 'card exceeds readable height; use text')
                _measure(deck, [proposed])
            except LayoutError as error:
                if not current:
                    error.card_index = len(packed) + 1
                    raise
                packed.append(candidate(current))
                current = [block]
                try:
                    _measure(deck, [candidate(current)])
                except LayoutError as error:
                    error.card_index = len(packed) + 1
                    raise
            else:
                current.append(block)
            if len(packed) >= MAX_CARDS:
                raise LayoutError('card_count', 'content exceeds four readable cards', MAX_CARDS + 1)
        if current:
            packed.append(candidate(current))
    if len(packed) > MAX_CARDS:
        raise LayoutError('card_count', 'content exceeds four readable cards', MAX_CARDS + 1)
    packed = _split_to_four(packed)
    _measure(deck, packed)
    # The content/version identity already determines this layout. Preserve it for
    # callers that locate the manifest using the original build result.
    return dict(deck, cards=packed)


def fits(deck):
    """Measure the delivery layout, including reflow, without writing artifacts."""
    if not deck:
        return False
    try:
        _packed(deck)
        return True
    except (ImportError, OSError, ValueError):
        return False


def render(deck, destination):
    """Write bounded PNGs plus an immutable manifest; private files, no network or HTML execution."""
    deck = _packed(deck)
    destination = Path(destination) / deck['id']
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    pages = []
    for index, image in enumerate(_render_pages(deck), 1):
        path = destination / f'{index:02d}.png'
        fd, name = tempfile.mkstemp(dir=destination, suffix='.tmp')
        tmp = Path(name)
        with open(fd, 'wb') as stream:
            image.save(stream, format='PNG', optimize=True)
        tmp.replace(path)
        pages.append(str(path))
    manifest = {**deck, 'images': pages}
    path = destination / 'manifest.json'
    fd, name = tempfile.mkstemp(dir=destination, suffix='.tmp')
    tmp = Path(name)
    with open(fd, 'w') as stream:
        json.dump(manifest, stream, ensure_ascii=False)
    tmp.replace(path)
    return manifest
