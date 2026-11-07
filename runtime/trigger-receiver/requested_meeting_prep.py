"""Strict accepted-occurrence wrapper around the complete shared meeting-prep skill."""


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
