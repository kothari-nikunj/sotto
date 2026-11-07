"""Eval-only projection of one receipt-bound, accepted gallery; no artifact discovery."""
import hashlib
import io
from pathlib import Path
import sys


def project_gallery(data, rows, row, source_revision):
    receipt = row.get('receipt') or {}
    artifact = receipt.get('visual_artifact_id')
    if not artifact:
        return None
    try:
        shared = Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff/_shared/lib'
        sys.path.insert(0, str(shared))
        import protected_brief_detail  # noqa: PLC0415 - selected packaged library above
        import visual_brief  # noqa: PLC0415 - selected packaged renderer above
        verified = protected_brief_detail._verified_artifact(
            data, artifact, rows, source_revision, receipt.get('route_digest'))
        if verified is None:
            return {'status': 'unavailable'}
        manifest, receipt = verified
        paths = [str(Path(data)/'cache/visual-briefs'/artifact/f'{index:02d}.png') for index in range(1, 5)]
        if manifest.get('kind') != 'prep' or manifest.get('images') != paths or len(manifest.get('cards', [])) != 4:
            return {'status': 'unavailable'}
        # Existing proof binds full_text and image bytes, not the cards field. Reuse
        # the actual renderer to prove these blocks produce the accepted PNGs.
        rendered = []
        for image in visual_brief._render_pages(manifest):
            try:
                stream = io.BytesIO()
                image.save(stream, format='PNG', optimize=True)
                rendered.append(hashlib.sha256(stream.getvalue()).hexdigest())
            finally:
                image.close()
        proof = receipt['retained_text_proof']
        if rendered != proof['image_sha256s']:
            return {'status': 'unavailable'}
        return {'status': 'verified', 'artifact_id': artifact, 'summary': manifest['summary'],
                'card_text': '\n'.join(visual_brief._display_text(block['text'], 'prep')
                                       for card in manifest['cards'] for block in card['blocks']),
                'image_sha256s': rendered, 'image_paths': paths,
                'content_sha256': proof['content_sha256']}
    except Exception:  # noqa: BLE001 - evidence failure must withhold content, never crash the eval peer
        return {'status': 'unavailable'}
