"""Optional presentation at the existing delivery seam; the source body remains authoritative."""
import logging
import os
import re
from pathlib import Path


def artifact_id(presentation):
    """Opaque gallery identity for the existing acceptance receipt, never image paths or text."""
    paths = presentation.get('images', []) if isinstance(presentation, dict) else []
    if not isinstance(paths, list) or len(paths) != 4 or not all(isinstance(p, str) for p in paths):
        return None
    parents = {Path(p).parent for p in paths}
    ident = next(iter(parents)).name if len(parents) == 1 else ''
    return ident if re.fullmatch(r'[a-f0-9]{24}', ident) else None


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
    'offer_not_visible': 'The question needs to remain visible as text.',
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


def prepare(body, label, target, load_visual, gallery_available, *, diagnostics=None,
            retained_source_revision=None, candidate_route_digest=None, required_questions=(), research_evidence=None):
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
        edition = ('evening' if label.split(':')[-1] in ('sotto-evening-brief', 'evening-brief')
                   else 'morning') if kind == 'brief' else None
        deck = visual.build(body, kind, preview=label.startswith('preview:'), edition=edition)
        if not deck:
            return fallback('text_layout')
        if kind == 'prep' and research_evidence is not None:
            from retained_brief_proof import validate_research_evidence
            deck['research_evidence'] = validate_research_evidence(research_evidence)
        # A handoff activates only if its exact question is in a complete rendered block.
        # render() preserves every brief block or fails back to the original text.
        blocks = [b['text'] for c in deck['cards'] for b in c['blocks']]
        if any(not any(question in block for block in blocks) for question in required_questions):
            return fallback('offer_not_visible')
        root = Path(os.environ.get('SOTTO_DATA', '/data')) / 'cache/visual-briefs'
        manifest = (visual.render(deck, root,
                                  retained_source_revision=retained_source_revision,
                                  candidate_route_digest=candidate_route_digest)
                    if retained_source_revision and candidate_route_digest
                    else visual.render(deck, root))
        if diagnostics is not None:
            diagnostics.update(format='gallery')
        result = {'summary': manifest['summary'], 'images': manifest['images']}
        if 'retained_text_proof' in manifest:
            result['retained_text_proof'] = manifest['retained_text_proof']
        return result
    except Exception as error:  # optional presentation must never take down canonical text delivery
        # Log metadata only; exception messages may contain source content or private paths.
        reason = getattr(error, 'reason', 'render_error')
        return fallback(reason if isinstance(reason, str) and reason in FALLBACK_REASONS else 'render_error', error)


def retain_text(body, research_evidence, source_revision, route_digest, *, stamp):
    """Stage a private text artifact; only the outbox's matching acceptance makes it readable."""
    import json
    import secrets
    import tempfile
    ident = secrets.token_hex(12)
    manifest = {'id': ident, 'kind': 'prep', 'preview': False,
                'title': body.splitlines()[0][:512], 'summary': '', 'full_text': body,
                'research_evidence': research_evidence}
    manifest['retained_text_proof'] = stamp(manifest, source_revision, route_digest, [], text=True)
    raw = json.dumps(manifest, ensure_ascii=False).encode('utf-8')
    if len(raw) > 128 * 1024:
        raise ValueError('manifest exceeds protected reader bound')
    folder = Path(os.environ.get('SOTTO_DATA', '/data')) / 'cache/visual-briefs' / ident
    folder.mkdir(parents=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=folder, suffix='.tmp')
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    Path(name).replace(folder / 'manifest.json')
    return {'id': ident, 'retained_text_proof': manifest['retained_text_proof']}
