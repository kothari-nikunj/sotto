"""Shared guard for attended owner effects on Google (Calendar, Contacts, Gmail).

Each tool keeps its own review, apply and receipt logic. This module owns only what
was identical in all three: the refusal type, the installed Google action module,
the source-authority and grant check, and the post-acceptance read-back decision.
"""
import re
from functools import partial
from installed_writers import google_action

google = partial(google_action, anchor=__file__)

HEX = re.compile(r'[a-f0-9]{64}\Z')


class RefusalError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def authority(policy, google, revision, *, source, grants, refusal):
    """Return the current complete source revision, or refuse.

    Attended Sotto only; `source` must be in the complete Google source set; a given
    `revision` must still be current; every named capability grant must be present.
    """
    if google._unattended() or not policy.get('SOTTO_HOME'):
        raise RefusalError('authority_changed')
    current = policy['_source_authority']()
    if (current.get('complete') is not True or source not in current.get('google_sources', [])
            or type(current.get('revision')) is not str or not HEX.fullmatch(current['revision'])
            or revision is not None and current['revision'] != revision):
        raise RefusalError('authority_changed')
    capabilities = google.capabilities()
    if not all(capabilities.get(grant) for grant in grants):
        raise RefusalError(refusal)
    return current['revision']


def readback_allowed(check):
    """Authority gates provider contact; after acceptance it only decides whether Sotto may read back."""
    try:
        check()
        return True
    except Exception:  # noqa: BLE001 -- a changed or unreadable authority forbids only the private read
        return False
