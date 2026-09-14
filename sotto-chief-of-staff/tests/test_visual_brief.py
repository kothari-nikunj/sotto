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
    assert [c['title'] for c in deck['cards']] == ['Needs you', 'Today', 'Your day', 'In the loop']
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


def test_never_omits_overlong_action_or_required_link():
    for text in ('word ' * (v.MAX_WORDS + 1), 'Sign the waiver at https://example.com/waiver'):
        assert v.build('Morning\nNeeds Attention Now\n' + text) is None


def test_partitions_actions_and_falls_back_if_more_than_four_cards():
    paragraph = 'Alex — ' + 'word ' * (v.MAX_WORDS // 2)
    deck = v.build('Morning\nNeeds Attention Now\n' + '\n'.join([paragraph] * 4))
    assert len(deck['cards']) == 4
    assert v.build('Morning\nNeeds Attention Now\n' + '\n'.join([paragraph] * 5)) is None


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


def test_vertical_centering_keeps_content_inside_header_and_footer():
    top, height = 248, 700
    actual = v._content_top(top, height)
    assert abs((actual - top) - (v.HEIGHT - 106 - actual - height)) <= 1
    with pytest.raises(ValueError, match='readable height'):
        v._content_top(top, v.HEIGHT)


def test_agenda_locations_and_dash_free_presentation(tmp_path):
    source = BRIEF.replace('12:00 PM — Lunch with Alex', '12:00 PM — Lunch with Alex | Location: 535 Mission St, Suite 800')
    deck = v.build(source, preview=True)
    assert '—' not in deck['summary']
    assert '—' in deck['full_text']
    assert v._agenda_parts('12:00 PM — Lunch with Alex | Location: 535 Mission St, Suite 800') == ('12:00 PM', 'Lunch with Alex', '535 Mission St, Suite 800')
    assert v._agenda_parts('10:00 AM — Team meeting') == ('10:00 AM', 'Team meeting', '')
    assert v._display_text('Alex — Reply before lunch.', 'brief') == 'Alex: Reply before lunch.'
    assert '—' not in v._display_text('A long-term plan—already discussed.')
    assert len(v.render(deck, tmp_path)['images']) == 4
    too_long = v.build(source.replace('535 Mission St, Suite 800', 'A very long street address ' * 8))
    with pytest.raises(ValueError, match='location exceeds'):
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
    assert v.build(source.replace('Sam - Reply to the invitation by tomorrow.',
                                 'Sam - Reply at https://example.com/invite')) is None
