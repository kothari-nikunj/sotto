"""The generation lock's required-learning completion receipt."""
import time


def complete_essential(scratch, learn):
    import jsonstore
    marker = scratch / 'essential-done.json'
    if not jsonstore.read(str(marker), default={}).get('complete'):
        receipt = learn()
        jsonstore.write_atomic(str(marker), {'complete': True,
                               'learning_status': receipt['completion'], 'finished_at': time.time()})
