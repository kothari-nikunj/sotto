"""Pack complete notification evidence in priority order within the writer's budget."""
import json

import pending_offer


REPLY_NOTICE_GUIDANCE = """
For a plain commitment reminder use the supplied reply_notice verbatim if it fits the latest evidence
and the owner's instructions. Use it as the whole notification. For extra context, a draft, conflicting
evidence, sensitive wording or multiple tasks, write your own text and omit the completion invitation."""


def fit_batch(items, project, budget):
    # Optional person facts lack source attribution for actionable offers. Strip them from
    # every projected candidate if any offer could be selected in the batch.
    has_offer = any(item.get('kind') in pending_offer.LOOP_KINDS for item in items)
    admitted, parts = [], []
    remaining = budget - len('[]')
    separator = ', '
    for item in items:
        evidence = {k: v for k, v in item.items() if k != 'person_facts'} if has_offer else item
        part = json.dumps(project(evidence), ensure_ascii=False, sort_keys=True)
        cost = len(part) + (len(separator) if parts else 0)
        if cost <= remaining:
            admitted.append(item)
            parts.append(part)
            remaining -= cost

    # The model can use all admitted evidence to write even one chosen offer. Include those
    # dependencies in its delivery-time check and never credit evidence omitted by the budget.
    if has_offer:
        known = all(item.get('_composition_sources') is not None for item in admitted)
        sources = sorted({source for item in admitted for source in item['_composition_sources']}) if known else []
        for item in admitted:
            if item.get('_loop_snapshot'):
                item['_loop_snapshot'] = {**item['_loop_snapshot'], 'sources': sources} if known else {}
        admitted = [{k: v for k, v in item.items() if k != 'person_facts'} for item in admitted]
    return admitted, '[' + separator.join(parts) + ']'
