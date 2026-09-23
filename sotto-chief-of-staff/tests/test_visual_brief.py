"""Cards quote existing content, preserve asks, and never shrink unreadable text."""
import json
from pathlib import Path

import pytest
from PIL import Image

import visual_brief as v

BRIEF = """Good morning — Monday, September 14
Needs Attention Now
Alex — Complete the preschool waiver by tomorrow; the form is still outstanding.
Should Handle Today
Jordan — Reply to the lunch invitation for Thursday.
Coming Up
10:00 AM — Team meeting
12:00 PM — Lunch with Alex
Already Handled
Sam — Replied yesterday.
Filtered
Donation blast and social chatter.
"""


def test_quotes_provenance_and_renders_private_readable_cards(tmp_path):
    deck = v.build(BRIEF, preview=True)
    assert deck == v.build(BRIEF, preview=True)
    assert [c['title'] for c in deck['cards']] == ['Needs you', 'Today', 'Your day', 'Follow-ups']
    for card in deck['cards']:
        assert sum(len(b['text'].split()) for b in card['blocks']) <= v.MAX_WORDS
        for b in card['blocks']:
            assert b['text'] == deck['full_text'].splitlines()[b['line']]
    manifest = v.render(deck, tmp_path)
    assert len(manifest['images']) == 4
    for path in manifest['images']:
        assert Image.open(path).size == (1080, 1920)
        assert Path(path).stat().st_mode & 0o777 == 0o600
    saved = json.loads((Path(manifest['images'][0]).parent / 'manifest.json').read_text())
    assert saved['full_text'] == BRIEF.strip()
    assert 'Donation' not in str(deck['cards'])
    assert 'Historical preview' in deck['summary']


def test_never_omits_overlong_action_or_pads_a_single_link_into_four_cards():
    for text in ('word ' * (v.MAX_WORDS + 1), 'Sign the waiver at https://example.com/waiver'):
        assert v.build('Morning\nNeeds Attention Now\n' + text) is None


def test_partitions_actions_and_rejects_more_than_four_readable_cards(tmp_path):
    paragraph = 'Alex — ' + 'word ' * (v.MAX_WORDS // 2)
    deck = v.build('Morning\nNeeds Attention Now\n' + '\n'.join([paragraph] * 4))
    assert len(deck['cards']) == 4
    crowded = v.build('Morning\nNeeds Attention Now\n' + '\n'.join([paragraph] * 5))
    assert not v.fits(crowded)
    with pytest.raises(v.LayoutError, match='four readable cards'):
        v.render(crowded, tmp_path)
    assert not list(tmp_path.iterdir())


def test_prep_sections_remain_extracts_not_new_research():
    deck = v.build('Meeting with Alex\nYour thread\nMet on Friday.\nWhat Example builds\nSoftware for clinics.\nThe founder\nWas an engineer.\nTraction & signals\nThree pilots are signed.\nAngles\nAsk how pilots convert to paid contracts.', 'prep')
    assert [c['title'] for c in deck['cards']] == ['Your relationship', 'The business', 'Signals & context', 'Questions to ask']
    assert 'Was an engineer' in str(deck['cards'])


def test_unrenderable_word_uses_failure_instead_of_clipping(tmp_path):
    deck = v.build('Morning\nNeeds Attention Now\n' + 'W' * 150 + '\nA — reply.\nB — reply.\nC — reply.')
    with pytest.raises(ValueError, match='too wide'):
        v.render(deck, tmp_path)
    assert not list(tmp_path.rglob('manifest.json'))


def test_heading_without_greeting_and_empty_inputs():
    assert v.build('Needs Attention Now\nAlex — reply today.') is None
    assert len(v.build(BRIEF.split('\n', 1)[1])['cards']) == 4
    for body in ('', 'No sections here', None):
        assert v.build(body) is None


def test_portrait_prep_keeps_background_signals_context_and_all_questions(tmp_path):
    paragraphs = [
        ('Your thread', 'We met through a mutual colleague and spoke last week about their first customers.', 4),
        ('The founder', 'Previously led engineering and worked with the cofounder for several years before starting the company.', 3),
        ('What Example builds', 'Their software helps operations teams review incoming requests and coordinate work across existing internal tools.', 4),
        ('Traction & signals', 'The team reports three paid pilots and is collecting feedback before expanding to additional customers.', 2),
        ('The space & why now', 'Customers currently coordinate this work manually and want better visibility into outstanding requests and deadlines.', 2),
        ('Angles', 'Ask how pilots convert to paid contracts, who owns the budget, and what needs to happen before a broader rollout.', 4),
    ]
    source = 'Alex (Example) — Historical meeting\n' + '\n'.join(heading + '\n' + '\n'.join([text] * count) for heading, text, count in paragraphs)
    deck = v.build(source, 'prep', preview=True)
    assert sum(len(c['blocks']) for c in deck['cards']) == sum(count for _, _, count in paragraphs)
    assert not deck['excerpt']
    manifest = v.render(deck, tmp_path)
    assert len(manifest['images']) == 4


def test_emphasis_targets_names_and_labels_not_times_or_sentences():
    assert v._emphasis_end('Alex Smith — Reply before lunch.', 'brief') == len('Alex Smith')
    assert v._emphasis_end('The Problem: Existing tools are slow.', 'prep') == len('The Problem:')
    assert v._emphasis_end('11:42 AM — Meeting.', 'prep') == 0
    assert v._emphasis_end('We met at 11:42 AM yesterday.', 'prep') == 0


def test_mixed_weight_wrapping_preserves_words_and_respects_width():
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(Image.new('RGB', (v.WIDTH, v.HEIGHT)))
    regular = ImageFont.truetype(str(v.FONT_DIR / 'Inter.ttf'), 42)
    bold = ImageFont.truetype(str(v.FONT_DIR / 'Inter.ttf'), 42)
    regular.set_variation_by_axes([14, 400])
    bold.set_variation_by_axes([14, 650])
    text = 'Distribution & Enterprise GTM: How do customers find the company and who owns the budget?'
    end = v._emphasis_end(text, 'prep')
    lines = v._rich_lines(draw, text, regular, bold, 340, end)
    assert [word for line in lines for word, _, _ in line] == text.split()
    assert lines[0][0][1] is bold
    assert lines[-1][-1][1] is regular
    for line in lines:
        word, face, offset = line[-1]
        assert offset + draw.textlength(word, font=face) <= 340


def test_top_alignment_keeps_content_inside_header_and_footer():
    top, height = 248, 700
    actual = v._content_top(top, height)
    assert actual == top
    assert v._content_top(top, 100) == actual
    with pytest.raises(ValueError, match='readable height'):
        v._content_top(top, v.HEIGHT)


@pytest.mark.parametrize('handled_count, fits', [(5, True), (7, True), (18, False)])
def test_dense_outcomes_tighten_gaps_without_shrinking_or_dropping_text(tmp_path, monkeypatch, handled_count, fits):
    # A normal-length brief can overflow from names, line wrapping, and paragraph gaps,
    # even with fewer than 200 words on each card. Keep fixed type and a real gap floor.
    source = BRIEF.replace('Sam — Replied yesterday.', '\n'.join(
        f'Alexandra Montgomery {i} - Confirmed the meeting with the project team and shared the notes for next week.'
        for i in range(handled_count)) + '\nStill open\n'
        'Jordan Smith - Please review the invitation and reply before Friday.\n'
        '3 other open loops - see /app#loops')
    deck = v.build(source)
    before = json.dumps(deck, sort_keys=True)
    assert sum(len(c['blocks']) for c in deck['cards'] if c['title'] == 'Follow-ups') == handled_count + 2
    from PIL import ImageDraw
    draw_text = ImageDraw.ImageDraw.text
    body_sizes = set()

    def record_text(self, xy, text, *args, **kwargs):
        face = kwargs.get('font')
        if face is not None and 238 < xy[1] < v.HEIGHT - 106:
            body_sizes.add(face.size)
        return draw_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', record_text)
    if fits:
        manifest = v.render(deck, tmp_path)
        assert len(manifest['images']) == 4
        assert (tmp_path / deck['id'] / 'manifest.json').is_file()
        assert sorted(b['line'] for c in manifest['cards'] for b in c['blocks']) == sorted(
            b['line'] for c in deck['cards'] for b in c['blocks'])
        if handled_count == 5:
            assert manifest['cards'] == deck['cards']
        assert body_sizes <= {36, 48}
        assert 48 in body_sizes
    else:
        with pytest.raises(v.LayoutError):
            v.render(deck, tmp_path)
        assert not list(tmp_path.rglob('manifest.json'))
    assert json.dumps(deck, sort_keys=True) == before


def test_agenda_locations_wrap_without_clipping_or_losing_unicode(tmp_path, monkeypatch):
    from PIL import ImageDraw
    address = ('1355 Market Street, Suite 900, San Francisco, California 94103, '
               'Entrance on José Plaza near Café München')
    source = BRIEF.replace('12:00 PM — Lunch with Alex', '12:00 PM — Lunch with Alex | Location: 535 Mission St, Suite 800')
    source = source.replace('535 Mission St, Suite 800', address)
    deck = v.build(source, preview=True)
    assert '—' not in deck['summary']
    assert '—' in deck['full_text']
    assert v._agenda_parts('12:00 PM — Lunch with Alex | Location: ' + address) == ('', '12:00 PM', 'Lunch with Alex', address)
    assert v._agenda_parts('10:00 AM — Team meeting') == ('', '10:00 AM', 'Team meeting', '')
    assert v._display_text('Alex — Reply before lunch.', 'brief') == 'Alex: Reply before lunch.'
    assert '—' not in v._display_text('A long-term plan—already discussed.')
    drawn = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        if kwargs.get('font') and kwargs['font'].size == 36:
            drawn.append((xy, text))
        return original(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', record)
    manifest = v.render(deck, tmp_path)
    assert len(manifest['images']) == 4 and address in manifest['full_text']
    location_lines = [text for (x, _y), text in drawn if x == 300 and text in address]
    assert len(location_lines) >= 2
    assert ' '.join(location_lines) == address
    assert all(Path(path).read_bytes().startswith(b'\x89PNG') for path in manifest['images'])

    too_long = v.build(source.replace(address, 'California Boulevard Entrance Plaza ' * 38))
    assert too_long is not None
    with pytest.raises(ValueError, match='readable height'):
        v.render(too_long, tmp_path)


def test_evening_keeps_pending_asks_and_outcome_receipts(tmp_path):
    source = """Good evening - Monday, September 14
Needs Attention Now
Alex - Sign the waiver tomorrow.
Coming Up
Tomorrow
10:00 AM - Meeting with Jordan
Still Pending
Sam - Reply to the invitation by tomorrow.
What moved today
You called Taylor back.
Filtered
Two newsletters.
"""
    deck = v.build(source)
    assert deck and len(deck['cards']) == 4
    blocks = [b['text'] for c in deck['cards'] for b in c['blocks']]
    assert 'Sam - Reply to the invitation by tomorrow.' in blocks
    assert 'You called Taylor back.' in blocks
    assert len(v.render(deck, tmp_path)['images']) == 4
    linked = v.build(source.replace('Sam - Reply to the invitation by tomorrow.',
                                    'Sam - Reply at https://example.com/invite'))
    assert linked and 'https://example.com/invite' in linked['summary']
    assert len(v.render(linked, tmp_path)['images']) == 4
    footer = v.build(source.replace('Sam - Reply to the invitation by tomorrow.',
                                    '3 other open loops - see /app#loops'))
    assert footer and '/app#loops' not in footer['summary']
    assert any("Ask me what's still open." in v._display_text(b['text'])
               for c in footer['cards'] for b in c['blocks'])
    assert len(v.render(footer, tmp_path)['images']) == 4
    assert v.build(source.replace('Sam - Reply to the invitation by tomorrow.',
                                 'Sam - Reply at https://example.com/' + 'x' * 1000)) is None


def test_companion_links_exclude_prose_punctuation_but_keep_balanced_parentheses():
    text = v.to_imessage('[invite](https://example.com/invite). (https://example.com/invite). '
                         'Read https://example.com/wiki/Example_(topic).', normalize_style=False)
    assert v._companion_links(text) == ['https://example.com/invite',
                                       'https://example.com/wiki/Example_(topic)']


@pytest.mark.parametrize('source, expected', [
    ('Thu Sep 17 2:00 PM: Investment Team Meeting', ('Thu Sep 17', '2:00 PM', 'Investment Team Meeting', '')),
    ('Thursday, September 17 6:00 PM: whim x hwn party (535 Mission St)',
     ('Thursday, September 17', '6:00 PM', 'whim x hwn party', '535 Mission St')),
    ('Tomorrow 2 PM - Investment Team Meeting', ('Tomorrow', '2 PM', 'Investment Team Meeting', '')),
    ('All day - OOO', ('', 'All day', 'OOO', '')),
    ('12:00 PM - Lunch (Alex)', ('', '12:00 PM', 'Lunch (Alex)', '')),
])
def test_calendar_formats_keep_date_time_attendees_and_exact_address(source, expected):
    assert v._agenda_parts(source) == expected


def test_real_calendar_and_followups_presentation(tmp_path, monkeypatch):
    from PIL import ImageDraw
    source = BRIEF.replace('10:00 AM — Team meeting\n12:00 PM — Lunch with Alex',
        'Calendar preview - open Calendar for the full schedule.\n'
        'Calendar preview (open Calendar for the full schedule):\n'
        'Calendar preview: open Calendar for the full schedule.\n'
        'Thu Sep 17 2:00 PM: Investment Team Meeting\n'
        'Thu Sep 17 6:00 PM: whim x hwn party (535 Mission St)\n'
        'Fri Sep 18 2:00 PM: David & Bri Wedding')
    source += '\nStill open\nJames Raybould - Send the lunch invitation for September 30.\n3 other open loops - see /app#loops'
    rendered = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        rendered.append((text, xy, kwargs.get('fill')))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', record)
    deck = v.build(source)
    assert deck['summary'] == "Good morning. Here's your morning brief for September 14."
    v.render(deck, tmp_path)
    assert ('2:00 PM', '#0066CC') in [(t, color) for t, _, color in rendered]
    assert ('6:00 PM', '#0066CC') in [(t, color) for t, _, color in rendered]
    assert sum(t == 'Thu Sep 17' for t, _, _ in rendered) == 1
    assert any(t == 'Fri Sep 18' for t, _, _ in rendered)
    assert any(t == '535 Mission St' for t, _, _ in rendered)
    assert not any('/app#loops' in t or 'Calendar preview' in t or t == 'In the loop' for t, _, _ in rendered)
    text = ' '.join(t for t, _, _ in rendered)
    assert '3 other open loops' in text
    assert 'Send the lunch invitation for September 30.' in text
    assert text.index('Still open') < text.index('Handled')
    assert [xy for t, xy, _ in rendered if t in ('Needs you', 'Today', 'Your day', 'Follow-ups')] == [(64, 108)] * 4


def test_day_group_survives_gallery_splitting(tmp_path, monkeypatch):
    from PIL import ImageDraw
    source = 'Good evening - Wednesday, September 16\nComing Up\nTomorrow\n' + '\n'.join(
        f'{hour}:00 PM - Meeting {hour}' for hour in range(1, 5))
    deck = v.build(source)
    assert all(c['blocks'][0]['day'] == 'Tomorrow' for c in deck['cards'])
    drawn = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', record)
    v.render(deck, tmp_path)
    assert drawn.count('Tomorrow') == 4


def test_source_warning_before_sections_survives_in_caption():
    warning = 'Your Mac is offline; local messages are from yesterday.'
    deck = v.build(BRIEF.replace('Needs Attention Now', warning + '\nNeeds Attention Now'))
    assert warning in deck['summary']


def test_schedule_overflow_cannot_silently_drop_the_last_event():
    source = BRIEF.replace('12:00 PM — Lunch with Alex', '12:00 PM - ' + 'meeting ' * (v.MAX_WORDS + 1))
    assert v.build(source) is None


@pytest.mark.parametrize('marker', ['*', '**'])
def test_original_name_emphasis_survives_without_a_separator(tmp_path, monkeypatch, marker):
    from PIL import ImageDraw
    source = BRIEF.replace('Sam — Replied yesterday.',
        f'{marker}Wesley Chan{marker}<!--id:w@example.com|ch:email--> has not replied to your forward.')
    deck = v.build(source)
    block = deck['cards'][-1]['blocks'][0]
    assert block['emphasis_end'] == len('Wesley Chan')
    assert block['text'] == 'Wesley Chan has not replied to your forward.'
    words = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        words.append((text, kwargs.get('font')))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', record)
    v.render(deck, tmp_path)
    faces = {text: face for text, face in words if text in ('Wesley', 'Chan', 'has')}
    assert faces['Wesley'] is faces['Chan']
    assert faces['Wesley'] is not faces['has']


def test_birthday_cannot_swallow_the_following_meetings_day(tmp_path, monkeypatch):
    from PIL import ImageDraw
    source = BRIEF.replace('10:00 AM — Team meeting\n12:00 PM — Lunch with Alex',
        'Tomorrow\n🎂 Alex - birthday in 1 day\n12:00 PM - Lunch with Alex')
    drawn = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', record)
    v.render(v.build(source), tmp_path)
    assert 'Tomorrow · Birthdays in 1 day' in drawn
    assert '12:00 PM' in drawn


@pytest.mark.parametrize('birthday, label', [
    ('🎂 Alex Smith (birthday today)', 'Birthdays today'),
    ('🎂️ Alex Smith (birthday tomorrow)', 'Birthdays tomorrow'),
    ('Alex Smith - birthday in 2 days', 'Birthdays in 2 days'),
    ('🎂 Alex Smith · birthday today', 'Birthdays today'),
])
def test_birthday_forms_render_readable_words_without_missing_glyphs(tmp_path, monkeypatch, birthday, label):
    from PIL import ImageDraw
    source = BRIEF.replace('10:00 AM — Team meeting', birthday)
    drawn = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', record)
    deck = v.build(source)
    assert birthday in deck['full_text']
    assert v.fits(deck)
    v.render(deck, tmp_path)
    assert label in drawn and 'Alex Smith' in drawn
    assert not any('🎂' in text or '\ufe0f' in text for text in drawn)
    assert '12:00 PM' in drawn


def test_fit_check_and_delivery_agree_on_overflow(tmp_path):
    deck = v.build(BRIEF.replace('Alex — Complete the preschool waiver by tomorrow; the form is still outstanding.', 'W' * 150))
    assert not v.fits(deck)
    with pytest.raises(ValueError, match='too wide'):
        v.render(deck, tmp_path)
