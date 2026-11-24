"""Eval-only projection of one receipt-bound, accepted gallery; no artifact discovery."""
import hashlib
import io
import json
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


def deliver_brief(receiver, data, step, text):
    """Replay the isolated model's exact output at the receiver delivery boundary."""
    label = 'cron:sotto-morning-brief'
    run_id = hashlib.sha256(f'eval-gallery:{step}'.encode()).hexdigest()
    receiver._activation_tick()
    evidence = receiver._composition_evidence(label)
    body = receiver._delivery_body(text)
    if not evidence or body.strip() != text.strip():
        raise ValueError('Gallery replay requires current route proof and unchanged brief text')
    shared = Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff/_shared'
    sys.path.insert(0, str(shared / 'scripts'))
    from compose_brief import _archive_brief  # noqa: PLC0415 - selected packaged composer above
    _archive_brief({'brief_text': text}, 'morning')
    accepted = receiver._deliver_text(text, label, run_id=run_id,
            source_revision_at_composition=evidence['source_revision'],
            route_digest_at_composition=evidence['route_digest'],
            source_text_sha256=hashlib.sha256(body.encode()).hexdigest())
    rows = json.loads(Path(receiver.OUTBOX.path()).read_text())['rows']
    selected = [row for row in rows if row.get('id') == run_id]
    if len(selected) != 1:
        raise ValueError('Expected exactly one outbox row for the replay')
    row = selected[0]
    if not accepted:
        raise ValueError(f"Brief delivery did not reach accepted status: {row.get('last_error')}")
    receipt = row.get('receipt') or {}
    artifact = receipt.get('visual_artifact_id')
    reader = receiver._load_shared_lib('protected_brief_detail')
    verified = artifact and reader._verified_artifact(
        data, artifact, rows, evidence['source_revision'], evidence['route_digest'])
    import visual_brief  # noqa: PLC0415 - loaded by the delivery renderer
    if (not verified or receipt.get('message_ids') != [receipt.get('message_id')]
            or verified[0]['full_text'] != visual_brief.to_imessage(body, normalize_style=False).strip()):
        raise ValueError('Exact brief was not delivered with retained gallery proof; inspect receiver render diagnostics')
    manifest, receipt = verified
    return {'artifact_id': artifact, 'dispatch_id': row['id'], 'message_id': receipt['message_id'],
            'payload_hash': receipt['payload_hash'], 'route_digest': evidence['route_digest'],
            'source_revision': evidence['source_revision'], 'summary': manifest['summary'],
            'image_sha256s': manifest['retained_text_proof']['image_sha256s'],
            'content_sha256': manifest['retained_text_proof']['content_sha256']}
