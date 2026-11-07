"""Observed source references shared by brief composition and delayed learning."""
import re

from textutil import _arr, _obj, _s, unwrap_tool_result
from timeutil import parse_observed_time
from gemini import _diag


def _observed_references(inputs):
    from render_local import message_evidence_id, message_version_reference
    refs = {}
    bare_refs = {}
    canonical_refs = {}
    bindings, ambiguous_refs = {}, set()
    def bind_reference(alias, observed, identity):
        if alias in ambiguous_refs:
            return
        if alias in bindings and bindings[alias] != identity:
            ambiguous_refs.add(alias)
            refs.pop(alias, None)
            canonical_refs.pop(alias, None)
            return
        bindings[alias] = identity
        refs[alias] = observed
        if identity.startswith('msg:v2:'):
            canonical_refs[alias] = identity
    lanes = _evidence_lanes(inputs)
    from textutil import current_message_rows
    for source, records in lanes:
        if source in ('imessage', 'whatsapp'):
            records = current_message_rows(records)
        for record in records:
            if not isinstance(record, dict):
                continue
            ids = {_s(record.get(key)) for key in ("source_id", "id", "messageId", "message_id", "guid", "rowid", "threadId")}
            if source in ("imessage", "whatsapp"):
                ids.add(message_evidence_id(record, source))
            canonical = message_version_reference(record, source)
            observed_at = parse_observed_time(record.get("timestamp") or record.get("date")
                                              or record.get("internalDate"))
            observed = observed_at.date().isoformat() if observed_at else ""
            if canonical:
                ids.add(canonical)
            for ident in ids - {""}:
                identity = canonical or f'{source}:{ident}'
                bind_reference(ident if ident == canonical else f"{source}:{ident}", observed, identity)
                bare_refs.setdefault(ident, set()).add(source)
                if len(bare_refs[ident]) == 1:
                    bind_reference(ident, observed, identity)
                else:
                    ambiguous_refs.add(ident)
                    refs.pop(ident, None)  # unqualified collision cannot identify a source
                    canonical_refs.pop(ident, None)
    return refs, canonical_refs


def _evidence_lanes(inputs):
    from source_context import allowed, project_local
    local = project_local(unwrap_tool_result(inputs.get("local") or {}))
    lanes = [(source, records) for source, records in local.items() if isinstance(records, list)]
    lanes.extend((source, _arr(_obj(inputs, "google"), key))
                 for source, key in (("gmail", "emails"), ("calendar", "events"))
                 if allowed(source))
    granola = inputs.get("granola")
    if allowed('granola'):
        lanes.append(("granola", granola if isinstance(granola, list) else _arr(granola or {}, "meetings")))
    return lanes


def bind_fact_evidence(out: dict, inputs: dict, *, validate_all_refs=False) -> None:
    """A generated reference counts as evidence only while its observed source remains allowed."""
    refs, canonical_refs = _observed_references(inputs)
    evidence_counts = {"accepted": 0, "rejected": 0}
    for person in _arr(out.get("extracted_knowledge") or {}, "person_updates"):
        if not isinstance(person, dict):
            continue
        supported = []
        for fact in person.get("facts") or []:
            if not isinstance(fact, dict):
                evidence_counts["rejected"] += 1
                continue
            ref = _s(fact.get("source_ref"))
            extra = fact.get('evidence_refs', []) if validate_all_refs else []
            if (not ref or ref not in refs or not isinstance(extra, list)
                    or any(not isinstance(value, str) or value not in refs for value in extra)):
                evidence_counts["rejected"] += 1
                continue
            fact.pop("evidence_refs", None)  # composition's contract has one observed reference per fact
            if validate_all_refs and extra:
                fact['evidence_refs'] = list(dict.fromkeys(canonical_refs.get(value, value) for value in extra))
            if ref in canonical_refs:
                fact['source_ref'] = canonical_refs[ref]
            fact.pop("observed_date", None)  # observation time belongs to the source, not the model
            if ref and re.fullmatch(r"\d{4}-\d{2}-\d{2}", refs[ref]):
                fact["observed_date"] = refs[ref]
            supported.append(fact)
            evidence_counts["accepted"] += 1
        person["facts"] = supported
    out["learning_evidence"] = evidence_counts
    if evidence_counts["rejected"]:
        _diag(f"[compose_brief] learning evidence: {evidence_counts['accepted']} accepted, "
              f"{evidence_counts['rejected']} rejected (unobserved source reference)")



def project_learning_payload(learning_payload, sources, inputs):
    """Revalidate saved proposals when a consumed source loses authority."""
    from source_context import allowed
    revoked = any(not allowed(source) for source in sources if isinstance(source, str))
    if revoked:
        projection = {'extracted_knowledge': learning_payload}
        bind_fact_evidence(projection, inputs, validate_all_refs=True)
        counts = projection['learning_evidence']
        # These legacy fields have no individual evidence references. After a source
        # change they cannot borrow authority from an unrelated surviving fact.
        people = []
        for person in learning_payload.get('person_updates', []):
            if not isinstance(person, dict):
                counts['rejected'] += 1
                continue
            for field in ('profile_patch', 'relations', 'identifiers', 'last_researched'):
                if person.pop(field, None):
                    counts['rejected'] += 1
            if person.get('facts'):
                people.append(person)
            else:
                counts['rejected'] += 1  # includes unsupported name/identifier-only stubs
        learning_payload['person_updates'] = people
        counts['rejected'] += len(learning_payload.get('company_updates', []))
        learning_payload['company_updates'] = []
        previous = learning_payload.get('_learning_evidence') or {}
        learning_payload['_learning_evidence'] = {
            'accepted': counts['accepted'],
            'rejected': previous.get('rejected', 0) + counts['rejected']}
    return learning_payload
