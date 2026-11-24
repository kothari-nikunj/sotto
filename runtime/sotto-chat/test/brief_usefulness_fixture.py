"""Stage a fictional brief and accept it through the real outbox for connected chat tests."""

import json
from pathlib import Path
import time


def seed(receiver):
    root = Path(receiver.DATA)
    artifact = root / 'events/work-inputs/brief-usefulness-fixture/artifact.json'
    if artifact.exists():
        return
    run = 'a' * 32
    text = 'School deadline: return the form by Friday.\nDonation item: community fundraiser next week.'
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({'brief_text': text, 'composed_at': time.time(),
                                    '_source_permissions': ['gmail'], '_delivery_run_ids': [run]}))
    box = receiver.OUTBOX
    hooks = dict(box.HOOKS)
    box.HOOKS.update(send=lambda *_: (True, '', {'acceptance': 'accepted', 'message_id': 'fictional-brief'}),
                     brief_gate=lambda *_: box.GATE_SEND, offer_route=lambda _: {},
                     on_delivered=lambda _: None, on_terminal=lambda _: None, record=lambda *_, **__: None)
    try:
        assert box.deliver({'run_id': run, 'label': 'cron:sotto-morning-brief',
                            'body': receiver._delivery_body(text), 'target': 'photon'})
    finally:
        box.HOOKS.update(hooks)
