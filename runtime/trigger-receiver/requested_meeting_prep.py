"""Strict accepted-occurrence wrapper around the complete shared meeting-prep skill."""


import json


class HandoffError(RuntimeError):
    """The composed prep cannot be delivered safely."""


class SourceChangedError(HandoffError):
    pass


def check_route(job, digest):
    meta = json.loads((job or {}).get('feedback') or '{}')
    if not meta:
        return False
    if not digest or meta.get('route_digest') != digest:
        raise HandoffError('prep recipient changed')
    return True


def reject_handoff(job, current_route, saved_route, invalid_text):
    if invalid_text:
        raise HandoffError('prep text proof invalid')
    if not check_route(job, current_route):
        raise HandoffError('prep admission route unavailable')
    if saved_route and current_route != saved_route:
        raise HandoffError('prep recipient changed')
    raise SourceChangedError('prep composition invalidated')


def run(request, command):
    from meeting_prep_pipeline import run as compose
    from delivery_effects import stage
    offer = request['offer']
    result = compose(request['pack'], {'mode': 'focus', 'focus': offer.get('person', '')}, command,
                     exact_meeting=offer['meeting'])
    stage([{'kind': 'source_permissions', 'sources': result['sources'], 'coverage_until': result['coverage_until']},
           {'kind': 'eligibility', 'source': 'calendar', 'calendar_event_id': offer['meeting']['event_id'],
            'calendar_start': offer['meeting']['start'], 'calendar_observed_at': result['calendar_observed_at']},
           {'kind': 'accepted_offer_clear', 'offer': offer}])
    return result['text']
