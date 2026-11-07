"""Receipt-selected actual rendered pixels, never a generic cache text search."""
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.fixture
def gallery(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    monkeypatch.syspath_prepend(str(HERE.parents[2]/'sotto-chief-of-staff/_shared/lib'))
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    import visual_brief  # noqa: PLC0415 - fixture selects source library above
    from accepted_gallery import project_gallery  # noqa: PLC0415 - fixture selects eval directory above
    deck = {'version': visual_brief.VERSION, 'kind': 'prep', 'preview': False,
            'id': 'a'*24, 'title': 'Synthetic notebook review', 'summary': 'Synthetic notebook review',
            'full_text': 'Notebook review. Meet at the west entrance.', 'excerpt': False,
            'cards': [{'title': title, 'blocks': [{'text': text, 'line': index, 'section': title}]}
                      for index, (title, text) in enumerate([
                          ('Your relationship', 'Private context is unavailable for this prep.'),
                          ('The business', 'Synthetic clinic workflow tools.'),
                          ('Questions to ask', 'Review the notebook.'),
                          ('Questions to ask', 'Confirm pickup at the west entrance.')])]}
    manifest = visual_brief.render(deck, tmp_path/'cache/visual-briefs',
                                  retained_source_revision='a'*64, candidate_route_digest='b'*64)
    proof = manifest['retained_text_proof']
    row = {'id': 'job', 'status': 'delivered', 'acceptance': 'accepted', 'receipt': {
        'visual_artifact_id': manifest['id'], 'retained_text_proof': proof,
        'route_proof_version': 1, 'route_digest': 'b'*64, 'payload_hash': proof['gallery_payload_sha256'],
        'message_id': 'accepted-send', 'accepted_at': 1}}
    path = tmp_path/'cache/visual-briefs'/manifest['id']/'manifest.json'
    return project_gallery, tmp_path, row, manifest, path


def test_exact_accepted_gallery_exposes_actual_rendered_blocks_and_paths(gallery):
    project, root, row, manifest, path = gallery
    found = project(root, [row], row, 'a'*64)
    assert found['status'] == 'verified' and 'west entrance' in found['card_text']
    assert found['image_paths'] == manifest['images']
    assert found['image_sha256s'] == manifest['retained_text_proof']['image_sha256s']


@pytest.mark.parametrize('change', ['cards_only', 'png', 'receipt', 'ambiguous', 'revoked'])
def test_gallery_withholds_mismatched_or_unaccepted_evidence(gallery, change):
    project, root, row, manifest, path = gallery
    rows, revision = [row], 'a'*64
    if change == 'cards_only':
        manifest['cards'][3]['blocks'][0]['text'] = 'Different source text.'
        path.write_text(json.dumps(manifest))
    elif change == 'png':
        Path(manifest['images'][0]).write_bytes(b'not the accepted PNG')
    elif change == 'receipt':
        row['receipt']['payload_hash'] = 'c'*64
    elif change == 'ambiguous':
        rows.append(row)
    else:
        revision = 'c'*64
    assert project(root, rows, row, revision) == {'status': 'unavailable'}


def test_retained_full_text_cannot_substitute_for_an_omitted_card_block(gallery):
    project, root, row, manifest, path = gallery
    import visual_brief  # noqa: PLC0415 - gallery fixture selects source library
    manifest['cards'][3]['blocks'][0]['text'] = 'Confirm the notebook handoff logistics.'
    replacement = visual_brief.render(manifest, root/'cache/visual-briefs',
                                     retained_source_revision='a'*64, candidate_route_digest='b'*64)
    proof = replacement['retained_text_proof']
    row['receipt'].update(visual_artifact_id=replacement['id'], retained_text_proof=proof,
                          payload_hash=proof['gallery_payload_sha256'])
    found = project(root, [row], row, 'a'*64)
    assert found['status'] == 'verified'
    assert 'west entrance' in replacement['full_text']
    assert 'west entrance' not in found['card_text']
