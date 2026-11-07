import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from bundle_runtime_descriptor import bundle, validate  # noqa: E402


def example():
    return {'version': 1, 'image': 'ghcr.io/kothari-nikunj/sotto-selfhost@sha256:' + 'a' * 64,
            'sourceCommit': 'b' * 40, 'chatModelId': 'gemini-fixture', 'nativeMediaModelId': 'gemini-media-fixture'}


@pytest.mark.parametrize('key,value', [('image', 'ghcr.io/kothari-nikunj/sotto-selfhost:latest'),
    ('sourceCommit', 'main'), ('version', True), ('version', 1.0), ('extra', 'ignored'), ('chatModelId', '')])
def test_rejects_unqualified_descriptor_shape(key, value):
    record = example()
    record[key] = value
    with pytest.raises(ValueError):
        validate(json.dumps(record).encode())


def test_duplicate_and_oversize_rejected():
    with pytest.raises(ValueError):
        validate(json.dumps(example()).replace('"version": 1', '"version": 1, "version": 1').encode())
    with pytest.raises(ValueError):
        validate(b' ' * 4097)


def test_bundle_is_same_artifact_and_does_not_replace_another_release(tmp_path):
    source, destination = tmp_path / 'source.json', tmp_path / 'bundled.json'
    source.write_text(json.dumps(example()))
    bundle(source, destination)
    assert source.read_bytes() == destination.read_bytes()
    bundle(source, destination)
    next_release = example()
    next_release['sourceCommit'] = 'c' * 40
    source.write_text(json.dumps(next_release))
    with pytest.raises(ValueError):
        bundle(source, destination)
    assert json.loads(destination.read_bytes()) == example()
