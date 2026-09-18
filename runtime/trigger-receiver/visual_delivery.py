"""Optional presentation at the existing delivery seam; the source body remains authoritative."""
import logging
import os
from pathlib import Path


def prepare(body, label, target, load_visual, gallery_available):
    if os.environ.get('SOTTO_VISUAL_BRIEFS', '1') != '1' or (target != 'photon' and not target.startswith('photon:')):
        return None
    if 'prep' in label:
        kind = 'prep'
    elif label.split(':')[-1] in ('sotto-morning-brief', 'sotto-evening-brief', 'morning-brief', 'evening-brief'):
        kind = 'brief'
    else:
        return None  # ordinary nudges, digests and unsupported formats stay text
    try:
        if not gallery_available():
            return None
        visual = load_visual()
        deck = visual.build(body, kind, preview=label.startswith('preview:'))
        if not deck:
            return None
        root = Path(os.environ.get('SOTTO_DATA', '/data')) / 'cache/visual-briefs'
        manifest = visual.render(deck, root)
        return {'summary': manifest['summary'], 'images': manifest['images']}
    except Exception as error:  # optional presentation must never take down canonical text delivery
        # Log metadata only; exception messages may contain source content or private paths.
        logging.getLogger(__name__).warning('visual_brief_fallback kind=%s error=%s', kind, type(error).__name__)
        return None  # image presentation can never prevent an ordinary brief
