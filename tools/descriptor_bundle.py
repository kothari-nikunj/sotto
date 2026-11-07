"""Bounded descriptor JSON and immutable, byte-preserving bundle copies."""
import json
from pathlib import Path

MAX_DESCRIPTOR_BYTES = 4096


def read_descriptor(raw, *, label):
    if len(raw) > MAX_DESCRIPTOR_BYTES:
        raise ValueError(f'{label} exceeds limit')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate {label} key')
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique)


def bundle_descriptor(source, destination, validate, *, label, conflict_message):
    source = Path(source)
    if source.stat().st_size > MAX_DESCRIPTOR_BYTES:
        raise ValueError(f'{label} exceeds limit')
    raw = source.read_bytes()
    value = validate(raw)
    # Existing resources cannot be silently replaced in the sign-only lane.
    destination = Path(destination)
    if destination.exists():
        if validate(destination.read_bytes()) != value:
            raise ValueError(conflict_message)
        return
    with destination.open('xb') as handle:
        handle.write(raw)
