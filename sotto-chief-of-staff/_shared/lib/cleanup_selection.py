"""Bounded numeric answers inherit intent only from the open-task done question."""
import re


def short_numbered_completion(review, selection, clause, indices, residual):
    complete = complete_open_selection(review)
    return (review.get('scope') == 'open' and len(selection['clauses']) == 1 and clause['correction'] == selection['correction']
            and (re.fullmatch(r'(?:\s|,|&|\band\b)*[.!]?', residual, re.I) is not None
                 or complete and len(indices) == len(review['rows'])
                 and re.fullmatch(r'all\s*\?(?:\s|,|&|\band\b)*[.!?]?', residual.strip(), re.I) is not None))


def complete_open_selection(review):
    return (review.get('scope') == 'open'
            and (review['anchor_keys'] == [row['anchor_key'] for row in review['rows']]
                 if 'anchor_keys' in review else review['remaining'] == 0))


def explicit_displayed_reference(reference):
    return type(reference) is str and re.fullmatch(r'all of (?:them|this|that|these|those)', reference, re.I) is not None


def all_open_selection(review, reference):
    return (complete_open_selection(review)
            or review.get('scope') == 'open' and 'anchor_keys' not in review
            and bool(review['rows']) and explicit_displayed_reference(reference))
