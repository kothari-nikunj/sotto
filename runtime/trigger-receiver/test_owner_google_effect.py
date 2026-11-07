from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff/_shared/lib'))
import owner_google_effect as effect  # noqa: E402
import runtime_owner_calendar_tool as calendar  # noqa: E402
import runtime_owner_contacts_tool as contacts  # noqa: E402
import runtime_owner_gmail_draft_tool as gmail  # noqa: E402

REVISION = 'a' * 64
ALL_GRANTS = {'calendar_read', 'calendar_write', 'contacts_read', 'contacts_write',
              'gmail_read', 'gmail_draft', 'gmail_send'}


class Google:
    def __init__(self, grants=ALL_GRANTS, unattended=False):
        self.grants, self.unattended = set(grants), unattended

    def _unattended(self):
        return self.unattended

    def capabilities(self):
        return {grant: True for grant in self.grants}


def policy(**authority):
    current = {'complete': True, 'google_sources': ['calendar', 'google_contacts', 'gmail'],
               'revision': REVISION, **authority}
    return {'SOTTO_HOME': '/data/sotto-home', '_source_authority': lambda: current}


TOOLS = [
    (lambda p, g, r=None: calendar._authority(p, g, r), 'calendar', {'calendar_read', 'calendar_write'},
     'calendar_permission_unavailable'),
    (lambda p, g, r=None: contacts._authority(p, g, r), 'google_contacts', {'contacts_read', 'contacts_write'},
     'contacts_permission_unavailable'),
    (lambda p, g, r=None: gmail._authority(p, g, r), 'gmail', {'gmail_read', 'gmail_draft'},
     'draft_permission_unavailable'),
    (lambda p, g, r=None: gmail._authority(p, g, r, sending=True), 'gmail', {'gmail_read', 'gmail_send'},
     'send_permission_unavailable'),
    (lambda p, g, r=None: gmail._authority(p, g, r, read_only=True), 'gmail', {'gmail_read'},
     'draft_permission_unavailable'),
]


def code(call):
    with pytest.raises(ValueError) as refused:
        call()
    return refused.value.code


@pytest.mark.parametrize('check,source,grants,refusal', TOOLS)
def test_each_tool_refuses_exactly_on_its_source_and_grants(check, source, grants, refusal):
    assert check(policy(), Google(grants)) == REVISION
    assert check(policy(), Google(grants), REVISION) == REVISION
    for grant in grants:
        assert code(lambda: check(policy(), Google(grants - {grant}))) == refusal
    others = [name for name in ('calendar', 'google_contacts', 'gmail') if name != source]
    assert code(lambda: check(policy(google_sources=others), Google())) == 'authority_changed'


@pytest.mark.parametrize('check', [tool[0] for tool in TOOLS])
@pytest.mark.parametrize('changed', [
    lambda p, g: (p.pop('SOTTO_HOME'), g),
    lambda p, g: (p, setattr(g, 'unattended', True)),
    lambda p, g: (p.update(_source_authority=lambda: {**policy()['_source_authority'](), 'complete': False}), g),
    lambda p, g: (p.update(_source_authority=lambda: {**policy()['_source_authority'](), 'revision': 'b' * 63}), g),
])
def test_unattended_incomplete_or_malformed_authority_is_changed(check, changed):
    p, g = policy(), Google()
    changed(p, g)
    assert code(lambda: check(p, g)) == 'authority_changed'


@pytest.mark.parametrize('check', [tool[0] for tool in TOOLS])
def test_a_reviewed_revision_must_still_be_current(check):
    assert code(lambda: check(policy(), Google(), 'b' * 64)) == 'authority_changed'


def test_one_refusal_type_and_read_back_decision_for_every_tool():
    assert calendar.RefusalError is contacts.RefusalError is gmail.RefusalError is effect.RefusalError
    assert issubclass(gmail._PrecontactRefusalError, effect.RefusalError)
    assert effect.readback_allowed(lambda: None) is True
    assert effect.readback_allowed(lambda: (_ for _ in ()).throw(effect.RefusalError('authority_changed'))) is False
    assert effect.readback_allowed(lambda: (_ for _ in ()).throw(KeyError('unreadable'))) is False
