"""Optional presentation at the existing delivery seam; the source body remains authoritative."""
import logging
import os
from pathlib import Path


# Fixed copy only. Model text, exception messages and private paths never become diagnostics.
FALLBACK_REASONS = {
    'gallery_unavailable': 'Photo sending was unavailable.',
    'text_layout': 'This update did not form a four-photo brief.',
    'card_count': 'The content did not fit four readable photos.',
    'card_height': 'A section did not fit in the photos.',
    'word_too_wide': 'A word or link was too wide for the photos.',
    'heading_height': 'A heading did not fit in the photos.',
    'date_width': 'The brief date did not fit in the photos.',
    'calendar_label_width': 'A calendar label did not fit in the photos.',
    'render_error': 'The photos could not be prepared.',
}


def delivery_detail(presentation_status):
    """Called only after provider acceptance, including a later outbox retry."""
    if not isinstance(presentation_status, dict):
        return ''
    if presentation_status.get('format') == 'gallery':
        return 'Sent as four photos. Device display is not confirmed.'
    code = presentation_status.get('reason')
    reason = FALLBACK_REASONS.get(code) if isinstance(code, str) else None
    return ('Sent as text. ' + reason + ' Device display is not confirmed.') if reason else ''


def prepare(body, label, target, load_visual, gallery_available, *, diagnostics=None):
    if os.environ.get('SOTTO_VISUAL_BRIEFS', '1') != '1' or (target != 'photon' and not target.startswith('photon:')):
        return None
    if 'prep' in label:
        kind = 'prep'
    elif label.split(':')[-1] in ('sotto-morning-brief', 'sotto-evening-brief', 'morning-brief', 'evening-brief'):
        kind = 'brief'
    else:
        return None  # ordinary nudges, digests and unsupported formats stay text
    def fallback(reason, error=None):
        card_index = getattr(error, 'card_index', None)
        if type(card_index) is not int or not 1 <= card_index <= 5:
            card_index = None
        if diagnostics is not None:
            diagnostics.update(format='text', reason=reason, card_index=card_index)
        logging.getLogger(__name__).warning('visual_brief_fallback kind=%s error=%s reason=%s card=%s',
                                            kind, type(error).__name__ if error else 'none', reason, card_index)
        return None

    try:
        if not gallery_available():
            return fallback('gallery_unavailable')
        visual = load_visual()
        deck = visual.build(body, kind, preview=label.startswith('preview:'))
        if not deck:
            return fallback('text_layout')
        root = Path(os.environ.get('SOTTO_DATA', '/data')) / 'cache/visual-briefs'
        manifest = visual.render(deck, root)
        if diagnostics is not None:
            diagnostics.update(format='gallery')
        return {'summary': manifest['summary'], 'images': manifest['images']}
    except Exception as error:  # optional presentation must never take down canonical text delivery
        # Log metadata only; exception messages may contain source content or private paths.
        reason = getattr(error, 'reason', 'render_error')
        return fallback(reason if isinstance(reason, str) and reason in FALLBACK_REASONS else 'render_error', error)
