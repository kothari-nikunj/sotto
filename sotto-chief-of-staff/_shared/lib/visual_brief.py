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

VERSION = 10
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
                'coming up': 'Your day', 'already handled': 'Handled', '✅ already handled': 'Handled', 'still open': 'Open loops', 'still pending': 'Open loops', 'what moved today': 'Handled'}.get(line.lower())
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
    lines = plain.splitlines()
    title = lines[0].strip()
    sections, active = [], None
    for index, raw in enumerate(lines):
        line = raw.strip().lstrip('• ').strip()
        if not line:
            continue
        heading = _heading(line, kind)
        if heading:
            active = {'title': heading, 'blocks': []}
            sections.append(active)
        elif line.lower() in ('already handled', '✅ already handled', 'filtered', 'still open',
                              'the founder', 'traction & signals', 'the space & why now'):
            active = None
        elif active is not None:
            # Long paragraphs and links belong in text. Never cut off a qualifier, date or amount.
            if len(line.split()) <= MAX_WORDS and (kind == 'brief' or not re.search(r'https?://|mailto:|/app#', line)):
                active['blocks'].append({'text': line, 'line': index})
            elif kind == 'brief' and active['title'] in ('Needs you', 'Today', 'Open loops', 'In the loop'):
                return None  # never hide an action merely because it is hard to fit
    if kind == 'prep':
        groups = [('Your relationship', ('Your relationship', 'The founder')),
                  ('The business', ('The business',)),
                  ('Signals & context', ('Signals', 'Context')),
                  ('Questions to ask', ('Questions to ask',))]
        sections = [{'title': title, 'blocks': [dict(b, section=part['title'])
                    for part in sections if part['title'] in titles for b in part['blocks']]}
                    for title, titles in groups]
    else:
        extras = [dict(b, section=part['title']) for part in sections
                  if part['title'] in ('Handled', 'Open loops') for b in part['blocks']]
        sections = [part for part in sections if part['title'] not in ('Handled', 'Open loops')]
        if extras:
            sections.append({'title': 'In the loop', 'blocks': extras})
    cards = []
    for section in sections:
        blocks, words = [], 0
        required = kind == 'brief' and section['title'] in ('Needs you', 'Today', 'Open loops', 'In the loop')
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
    if len(cards) > MAX_CARDS:
        return None  # keep the whole brief in text instead of losing an actionable ask
    # Four attachments produce the compact native gallery. Split only at real paragraph
    # boundaries; if there is too little source material, keep the short update as text.
    while cards and len(cards) < MAX_CARDS:
        candidates = [c for c in cards if len(c['blocks']) > 1]
        if not candidates:
            return None
        largest = max(candidates, key=lambda c: sum(len(b['text']) for b in c['blocks']))
        at = cards.index(largest)
        cut = max(1, len(largest['blocks']) // 2)
        cards[at:at + 1] = [dict(largest, blocks=largest['blocks'][:cut]),
                           dict(largest, blocks=largest['blocks'][cut:])]
    if not cards:
        return None
    selected = {b['line'] for c in cards for b in c['blocks']}
    omitted = any(line.strip() and i not in selected and i > 0 and not _heading(line.strip(), kind)
                  for i, line in enumerate(lines))
    deck = {'version': VERSION, 'kind': kind, 'title': title, 'preview': bool(preview),
            'full_text': plain, 'cards': cards, 'excerpt': omitted}
    deck['id'] = hashlib.sha256(json.dumps(deck, sort_keys=True).encode()).hexdigest()[:24]
    # Keep the companion text short; dates and links remain selectable in Messages.
    deck['summary'] = ('Historical preview · ' if preview else '') + _display_text(title)
    if kind == 'brief':
        links = _companion_links(plain)
        if links:
            deck['summary'] += '\n' + '\n'.join(links)
        if len(deck['summary'].encode('utf-16-le')) // 2 > 1000:
            return None  # preserve usable links without overflowing the transport caption
    return deck


def _companion_links(text):
    links = []
    for link in re.findall(r'https?://[^\s<>]+|mailto:[^\s<>]+|/app#[^\s<>]+', text):
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


def _lines(draw, text, font, width):
    output, line = [], ''
    for word in text.split():
        if draw.textlength(word, font=font) > width:
            raise ValueError('card contains a word too wide to render')
        candidate = (line + ' ' + word).strip()
        if line and draw.textlength(candidate, font=font) > width:
            output.append(line)
            line = word
        else:
            line = candidate
    if line:
        output.append(line)
    return output


def _display_text(text, kind=''):
    if kind == 'brief':
        text = re.sub(r'^(.{1,55}?) [—–-] ', r'\1: ', text)
    return text.replace(' — ', ' · ').replace('—', '; ')


def _agenda_parts(text):
    match = re.match(r'(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s*[—–-]\s*(?P<event>.+)', text)
    if not match:
        return None, _display_text(text), ''
    title, delimiter, location = match['event'].partition(' | Location: ')
    return match['time'], _display_text(title), _display_text(location.strip()) if delimiter else ''


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
            raise ValueError('card contains a word too wide to render')
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
        raise ValueError('card exceeds readable height; use text')
    return top + (available - height) // 2


def render(deck, destination):
    """Write bounded PNGs plus an immutable manifest; private files, no network or HTML execution."""
    from PIL import Image, ImageDraw, ImageFont
    destination = Path(destination) / deck['id']
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if len(deck['cards']) != MAX_CARDS:
        raise ValueError('an image brief must contain exactly four cards')
    def font(size, weight=400):
        face = ImageFont.truetype(str(FONT_DIR / 'Inter.ttf'), size)
        face.set_variation_by_axes([32 if size >= 60 else 14, weight])
        return face
    label_font, meta_font = font(32), font(26)
    seal = Image.open(SEAL_PATH).convert('RGBA')
    seal.thumbnail((78, 78), Image.Resampling.LANCZOS)
    seal.putalpha(seal.getchannel('A').point(lambda alpha: round(alpha * 0.42)))
    body_font, heading_font = font(42), font(76, 650)
    bold_font, section_font = font(42, 650), font(32, 650)
    row_font, time_font = font(42), font(32, 650)
    location_font = font(32)
    pages = []
    for index, card in enumerate(deck['cards'], 1):
        image = Image.new('RGB', (WIDTH, HEIGHT), '#F5F5F7')
        draw = ImageDraw.Draw(image)
        # The company/person leads; the seal stays small and secondary.
        blocks = [b['text'] for b in card['blocks']]
        agenda = card['title'] == 'Your day'
        # These are typographic splits of the source, never inferred people/actions.
        title = 'Your day' if agenda else card['title']
        if deck['kind'] == 'prep':
            subject = re.split(r' [—–-] ', deck['title'], maxsplit=1)[0]
            company = re.search(r'\(([^()]+)\)', subject)
            title = company.group(1) if company else subject
        elif len(blocks) == 1:
            parts = re.split(r' [—–-] ', blocks[0], maxsplit=1)
            if len(parts) == 2 and len(parts[0]) <= 55:
                title, blocks[0] = parts
        title = _display_text(title)
        title_lines = _lines(draw, title, heading_font, 870)
        if len(title_lines) > 2:
            raise ValueError('card heading exceeds readable height')
        y = 70
        if deck['kind'] == 'brief':
            # Use the composition's own date, never today's date during a replay.
            edition = re.sub(r'^Good (morning|afternoon|evening)\s*[—–-]\s*',
                             lambda match: match[1].capitalize() + ' brief · ', deck['title'], flags=re.I)
            edition = _display_text(edition)
            if draw.textlength(edition, font=meta_font) > WIDTH - 128:
                raise ValueError('brief date exceeds readable width')
            draw.text((64, 45), edition, font=meta_font, fill='#6E6E73')
            y = 108
        for line in title_lines:
            draw.text((64, y), line, font=heading_font, fill='#1D1D1F')
            y += 88
        subtitle = {'Your relationship': 'Relationship & history',
                    'The business': 'Business', 'The founder': 'Founder',
                    'Questions to ask': 'Questions', 'Your day': 'Schedule'}.get(card['title'], card['title'])
        if subtitle != title:
            draw.text((64, y + 9), subtitle, font=label_font, fill='#6E6E73')
            y += 90
        else:
            y += 42
        if agenda:
            rows = []
            for block in blocks:
                time, event, location = _agenda_parts(block)
                birthday = re.fullmatch(r'(?:🎂\s*)?(.+?) · (birthday (?:today|in .+))', event) if not time else None
                birthday_label = birthday[2].replace('birthday', 'Birthdays', 1) if birthday else ''
                if birthday:
                    event = birthday[1]
                lines = _lines(draw, event, row_font, 702 if time else 902)
                if location and len(_lines(draw, location, location_font, 702)) > 1:
                    raise ValueError('meeting location exceeds one readable line; use text')
                padding = 44 if rows and birthday else 0
                height = max(100, len(lines) * 55 + 32 + (42 if location else 0)) if time else len(lines) * 55 + 30
                rows.append((time, lines, height + padding + (42 if birthday else 0), location, padding, birthday_label))
            y = _content_top(y, sum(row[2] for row in rows))
            for time, lines, height, location, padding, birthday_label in rows:
                y += padding
                height -= padding
                if birthday_label:
                    draw.text((88, y), birthday_label, font=label_font, fill='#6E6E73')
                    y += 42
                    height -= 42
                if time:
                    draw.text((88, y + 5), time, font=time_font, fill='#0066CC')
                    draw.line((300, y + height - 14, 1016, y + height - 14), fill='#DEDEE3', width=1)
                for offset, line in enumerate(lines):
                    draw.text((300 if time else 88, y + offset * 55), line, font=row_font,
                              fill='#1D1D1F' if time else '#6E6E73')
                if location:
                    draw.text((300, y + len(lines) * 55 + 4), location, font=location_font, fill='#6E6E73')
                y += height
        else:
            blocks = [_display_text(block, deck['kind']) for block in blocks]
            emphasis = [_emphasis_end(block, deck['kind']) for block in blocks]
            bullets = [deck['kind'] == 'prep' and len(blocks) > 1 and not end for end in emphasis]
            laid_out = [_rich_lines(draw, block, body_font, bold_font, 878 if bullet else 904, end)
                        for block, bullet, end in zip(blocks, bullets, emphasis)]
            sections = [b.get('section', '') for b in card['blocks']]
            merged = card['title'] in ('Your relationship', 'Signals & context', 'In the loop')
            starts = [merged and name and (i == 0 or name != sections[i-1]) for i, name in enumerate(sections)]
            height = sum(len(lines) * 55 for lines in laid_out) + max(0, len(blocks)-1) * 24 + 56 + sum(bool(x) for x in starts) * 42
            y = _content_top(y, height)
            draw.rounded_rectangle((64, y, 1016, y + height), radius=32, fill='#FFFFFF')
            y += 24
            for position, lines in enumerate(laid_out):
                if starts[position]:
                    draw.text((88, y), {'Your relationship': 'Your history', 'The founder': 'Background'}.get(sections[position], sections[position]), font=section_font, fill='#1D1D1F')
                    y += 42
                bullet = bullets[position]
                if bullet:
                    draw.ellipse((88, y + 22, 96, y + 30), fill='#86868B')
                for line in lines:
                    for word, face, offset in line:
                        draw.text((88 + (26 if bullet else 0) + offset, y), word, font=face, fill='#1D1D1F')
                    y += 55
                if position < len(laid_out)-1:
                    y += 24
        # Keep historical context explicit without repeating a second headline.
        footer = 'Historical preview' if deck['preview'] else ''
        seal_bounds = seal.getbbox()
        image.paste(seal, (64 - seal_bounds[0], HEIGHT - 90), seal)
        draw.text((154, HEIGHT - 65), footer, font=meta_font, fill='#6E6E73')
        for dot in range(MAX_CARDS):
            x = 928 + dot * 22
            draw.ellipse((x, HEIGHT - 56, x + 9, HEIGHT - 47), fill='#1D1D1F' if dot == index - 1 else '#D1D1D6')
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
