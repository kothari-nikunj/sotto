"""Pin-checked Hermes metadata binding for Sotto gallery recovery.

Hermes owns the delivery obligation. The adapter must receive that exact ID on
the initial final send and on a claimed recovery; reply text is not identity.
"""
import hashlib
import os
from pathlib import Path
import sys
import tempfile


PINS = {
    'base.py': '63b344ed86e9994ea2e96bc76ec5464fd46382a3c683a3bec178dffa79ad9ce6',
    'run_startup.py': '187b3324eeec09f15ff923fcf21e80c7b687c02194267791e0ed4d157401c08b',
}
OLD = {
    'base.py': '''        result = await delivery_adapter._send_with_retry(
            chat_id=event.source.chat_id, content=text_content,
            reply_to=_reply_anchor_for_event(event), metadata=metadata)
''',
    'run_startup.py': '''            metadata = {"thread_id": row["thread_id"]} if row.get("thread_id") else None
            try:
                result = await adapter.send(chat_id=row["chat_id"], content=content, metadata=metadata)
''',
}
NEW = {
    'base.py': '''        # The ledger ID is assigned here, after external metadata is received. Never trust a
        # caller-supplied gallery identity, even when the ledger is disabled for this turn.
        _sotto_delivery_metadata = dict(metadata or {})
        _sotto_delivery_metadata.pop("sotto_obligation_id", None)
        if _obligation_id is not None:
            _sotto_delivery_metadata["sotto_obligation_id"] = _obligation_id
        result = await delivery_adapter._send_with_retry(
            chat_id=event.source.chat_id, content=text_content,
            reply_to=_reply_anchor_for_event(event), metadata=_sotto_delivery_metadata)
''',
    'run_startup.py': '''            metadata = {"thread_id": row["thread_id"]} if row.get("thread_id") else {}
            # This ID came from the claimed ledger row, never from the recovered text.
            metadata["sotto_obligation_id"] = row["obligation_id"]
            try:
                result = await adapter.send(chat_id=row["chat_id"], content=content, metadata=metadata)
''',
}


def original(path):
    path = Path(path)
    name = path.name
    if name not in PINS:
        raise RuntimeError('unsupported Hermes gallery gateway file')
    text = path.read_text()
    unpatched = text.replace(NEW[name], OLD[name], 1) if NEW[name] in text else text
    if (hashlib.sha256(unpatched.encode()).hexdigest() != PINS[name]
            or unpatched.count(OLD[name]) != 1
            or (NEW[name] in text and text.count(NEW[name]) != 1)):
        raise RuntimeError('Hermes gallery obligation source differs from the reviewed pin')
    return unpatched


def check(path):
    original(path)


def patch(path):
    path = Path(path)
    text = path.read_text()
    unpatched = original(path)
    if text != unpatched:
        return
    fd, temporary = tempfile.mkstemp(prefix='.gallery-obligation-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(unpatched.replace(OLD[path.name], NEW[path.name], 1))
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv):
    if len(argv) not in (1, 2) or (len(argv) == 2 and argv[0] != '--check'):
        print('usage: gallery_obligation_compat.py [--check] <base.py|run_startup.py>', file=sys.stderr)
        return 2
    try:
        check(argv[-1]) if len(argv) == 2 else patch(argv[-1])
    except (OSError, RuntimeError) as error:
        print(f'[sotto] FATAL: {error}; review the Hermes pin before changing gallery recovery', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
