"""One automatic first useful look; the receiver/outbox remain the only delivery lane."""
from datetime import datetime, timezone
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import re
import time

from connectors import json_transaction

LABEL = 'onboarding:sotto-welcome-brief'
LEASE_SECONDS = 20 * 60
RETRY_SECONDS = 30 * 60
ATTEMPTS_PER_DAY = 3
_MARKER = re.compile(r'(\d{4}-\d{2}-\d{2})\.(morning|evening)\.delivered\Z')
_RUN_ID = re.compile(r'[0-9a-f]{32}\Z')


def _markers(root):
    markers = []
    for path in (root / 'briefs').glob('*.delivered'):
        match = _MARKER.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        try:
            markers.append((match.group(1), match.group(2), path.read_text().strip(),
                            path.stat().st_mtime))
        except OSError:
            continue
    return markers


def _outbox_rows(root):
    try:
        rows = json.loads((root / 'events/outbox.json').read_text()).get('rows', [])
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError, AttributeError):
        return []


@lru_cache(maxsize=1)
def _tzchain():
    # The same tzchain file receiver/dashboard use; a UTC date can differ from
    # the local date embedded in a brief marker near midnight.
    here = Path(__file__).resolve()
    for candidate in (here.with_name('tzchain.py'),
                      here.parents[2] / 'sotto-chief-of-staff/_shared/lib/tzchain.py'):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location('onboarding_tzchain', candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


def _accepted_scheduled_brief(root):
    """A marker is a claim; only a matching provider-accepted record proves delivery."""
    markers = _markers(root)
    if not markers:
        return False
    for row in _outbox_rows(root):
        if not isinstance(row, dict) or row.get('status') != 'delivered' \
                or row.get('acceptance') != 'accepted':
            continue
        receipt = row.get('receipt') or {}
        if not isinstance(receipt, dict) or not receipt.get('accepted_at') or not (
                receipt.get('message_id') or receipt.get('message_ids')):
            continue
        payload = row.get('payload') or {}
        if not isinstance(payload, dict):
            continue
        label = payload.get('label') or ''
        for day, kind, owner, _ in markers:
            if row.get('day') != day or not label.endswith(f'sotto-{kind}-brief'):
                continue
            # Applied effects compact payload to its label. The durable outbox
            # id is the run id used by the brief gate and the marker.
            if owner and owner != row.get('id'):
                continue
            return True
    # Older installations may have pruned the outbox row. The receiver's
    # delivery ledger is written only after its channel reports acceptance.
    tzchain = _tzchain()
    zone = (tzchain.resolve(tzchain.configured_tz_name(str(root))) if tzchain else None) or timezone.utc
    try:
        with (root / 'events/delivery.jsonl').open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or row.get('status') != 'delivered':
                        continue
                    stamp = datetime.fromisoformat(row['ts'].replace('Z', '+00:00'))
                    if stamp.tzinfo is None:
                        continue
                    accepted_at = stamp.astimezone(timezone.utc).timestamp()
                    label = row.get('label') or ''
                    local_day = datetime.fromtimestamp(accepted_at, zone).date().isoformat()
                    for day, kind, owner, claimed_at in markers:
                        if not label.endswith(f'sotto-{kind}-brief'):
                            continue
                        if day != local_day:
                            continue
                        if owner and row.get('run_id') and owner != row['run_id']:
                            continue
                        if _RUN_ID.fullmatch(owner) and not row.get('run_id'):
                            continue
                        if -60 <= accepted_at - claimed_at <= 6 * 3600:
                            return True
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
    except OSError:
        pass
    return False


def _pending_state(row):
    """A pending send that may already have reached the provider is ambiguous, not queued.

    That is a row marked uncertain, or a gallery the outbox holds for its receipt and will never
    resend; either can stay pending indefinitely, so it must not hold later briefs like a queued one.
    """
    payload = row.get('payload') or {}
    if (row.get('acceptance_uncertain') or row.get('acceptance') == 'unknown'
            or (isinstance(payload, dict) and payload.get('presentation')
                and row.get('acceptance') == 'in_flight')):
        return 'waiting'
    return 'queued'


def _unresolved_scheduled_brief(root):
    """A claimed scheduled send may still land, or may already have landed."""
    # The receiver persists composed work in the outbox before the send seam
    # creates its brief marker. Do not admit welcome through that gap.
    pending = set()
    for row in _outbox_rows(root):
        payload = row.get('payload') if isinstance(row, dict) else None
        label = payload.get('label', '') if isinstance(payload, dict) else ''
        if (label in ('cron:sotto-morning-brief', 'cron:sotto-evening-brief',
                      'brief:sotto-morning-brief', 'brief:sotto-evening-brief')
                and row.get('status') == 'pending'):
            pending.add(_pending_state(row))
    if pending:
        # A genuinely queued brief still holds later scheduled work until it is attempted.
        return 'queued' if 'queued' in pending else 'waiting'
    markers = {(day, kind, owner) for day, kind, owner, _ in _markers(root) if owner}
    for row in _outbox_rows(root):
        if not isinstance(row, dict):
            continue
        payload = row.get('payload') or {}
        if not isinstance(payload, dict):
            continue
        if not any(row.get('day') == day and row.get('id') == owner
                   and (payload.get('label') or '').endswith(f'sotto-{kind}-brief')
                   for day, kind, owner in markers):
            continue
        if row.get('status') == 'pending':
            return _pending_state(row)
        if (row.get('acceptance_uncertain') or row.get('acceptance') in
                ('unknown', 'in_flight', 'accepted')):
            return 'waiting'
    return None


def scheduled_inflight(data):
    """A scheduled send is still queued or has uncertain provider acceptance."""
    return bool(_unresolved_scheduled_brief(Path(data)))


def _adopt_existing(root, now):
    if not _accepted_scheduled_brief(root):
        return False
    with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
        # Any unfinished welcome phase: a failed or abandoned first look must not keep an
        # install that already receives scheduled briefs "fresh", or compose a second first look.
        if state.get('phase') != 'delivered':
            state.update(phase='existing', completed_at=now, acceptance_proven=True)
    return True


_UNCERTAIN = ('unknown', 'in_flight', 'accepted')


def _welcome_ambiguous(rows):
    """A welcome send that may have landed; like a scheduled brief, it never authorizes a replay."""
    return any(isinstance(r, dict) and r.get('status') != 'delivered'
               and (r.get('acceptance_uncertain') or r.get('acceptance') in _UNCERTAIN)
               for r in rows)


def tick(data, ready, start, now=None, *, model_ready=None):
    """Reserve before spawning; resume after crashes, and finish only on a channel receipt."""
    now = time.time() if now is None else now
    root = Path(data)
    try:
        snapshot = json.loads((root / 'config/onboarding.json').read_text())
    except (OSError, ValueError, AttributeError):
        snapshot = {}
    if snapshot.get('phase') == 'delivered':
        return 'delivered'
    if snapshot.get('phase') == 'existing':
        return 'existing'  # historical acceptance evidence may have aged out
    if _adopt_existing(root, now):
        return 'existing'
    if not snapshot.get('phase'):
        if snapshot.get('scheduled_ambiguous'):
            return 'waiting'  # terminal outbox evidence may have been pruned
        unresolved = _unresolved_scheduled_brief(root)
        if unresolved:
            if unresolved == 'waiting':
                with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
                    if not state.get('phase'):
                        state['scheduled_ambiguous'] = True
            return unresolved
    try:
        outbox_rows = json.loads((root / 'events/outbox.json').read_text()).get('rows', [])
    except (OSError, ValueError, AttributeError):
        outbox_rows = []
    welcome_rows = [r for r in outbox_rows if r.get('payload', {}).get('label') == LABEL]
    if not any(r.get('status') in ('delivered', 'pending') for r in welcome_rows) and (
            snapshot.get('welcome_ambiguous') or _welcome_ambiguous(welcome_rows)):
        if not snapshot.get('welcome_ambiguous'):
            with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
                state['welcome_ambiguous'] = True
        return 'waiting'
    if snapshot.get('phase') == 'model_held':
        # A provider-accepted composition wins even if a stale worker later reported a model hold.
        if any(r.get('status') == 'delivered' for r in welcome_rows):
            delivered(data, now)
            return 'delivered'
        if any(r.get('status') == 'pending' for r in welcome_rows):
            return 'queued'
        unresolved = _unresolved_scheduled_brief(root)
        if unresolved:
            return unresolved
        try:
            if model_ready is None or model_ready() is not True:
                return 'model_held'
        except Exception:
            return 'model_held'  # uncertain capability never spends another source/model attempt
    if not (ready() if callable(ready) else ready):
        return 'waiting_for_context'
    if (snapshot.get('phase') == 'composing'
            and max(snapshot.get('lease_until', 0), snapshot.get('retry_at', 0)) > now):
        return 'composing'
    if snapshot.get('phase') == 'queued' and any(r.get('status') == 'pending' for r in welcome_rows):
        return 'queued'
    with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
        if state.get('phase') in ('delivered', 'existing'):
            return state['phase']
        welcome = welcome_rows
        if any(r.get('status') == 'delivered' for r in welcome):
            state.update(phase='delivered', completed_at=now)
            return 'delivered'
        if any(r.get('status') == 'pending' for r in welcome):
            state['phase'] = 'queued'
            return 'queued'
        # Upgrade in place: an established installation keeps its memory and conversation.
        # A concurrent accepted send is picked up on the next tick; the claim
        # alone cannot turn this state terminal while the outbox lock is held.
        if state.get('lease_until', 0) > now or state.get('retry_at', 0) > now:
            return state.get('phase', 'waiting')
        day = int(now // 86400)
        if state.get('attempt_day') != day:
            state.update(attempt_day=day, attempts=0)
        if state.get('attempts', 0) >= ATTEMPTS_PER_DAY:
            return 'retry_later'
        state.update(phase='composing', attempts=state.get('attempts', 0) + 1,
                     lease_until=now + LEASE_SECONDS, retry_at=now + RETRY_SECONDS)
    started = start()  # outside the state lock: completion can call delivered() immediately
    if started is False:
        with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
            if state.get('phase') == 'model_held':
                return 'model_held'
            if state.get('phase') == 'composing' and state.get('lease_until') == now + LEASE_SECONDS:
                state.update(phase='waiting', lease_until=0, retry_at=0,
                             attempts=max(0, state.get('attempts', 1) - 1))
        return 'not_started'
    return 'started'


def delivered(data, now=None):
    with json_transaction(str(Path(data) / 'config/onboarding.json'), default={}) as state:
        state.update(phase='delivered', completed_at=time.time() if now is None else now)


def model_budget_held(data):
    """Park an undelivered welcome until a bounded model capability check admits it."""
    with json_transaction(str(Path(data) / 'config/onboarding.json'), default={}) as state:
        if state.get('phase') not in ('delivered', 'existing'):
            if state.get('phase') == 'composing':
                # Only the reserved welcome attempt failed. A repeated denial or recovery
                # projection sees model_held and cannot refund another attempt.
                state['attempts'] = max(0, state.get('attempts', 0) - 1)
            state.update(phase='model_held', lease_until=0, retry_at=0)


def scheduled_hold(data, now=None):
    """The first look arriving at 6:30 should not immediately be followed by the same day's recap."""
    now = time.time() if now is None else now
    try:
        state = json.loads((Path(data) / 'config/onboarding.json').read_text())
        return ((state.get('phase') in ('composing', 'queued') and state.get('lease_until', 0) > now)
                or (state.get('phase') == 'delivered' and state.get('completed_at', 0) + RETRY_SECONDS > now))
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def status(data, now=None):
    """Read-only setup progress. Composition is never presented as a sent brief."""
    now = time.time() if now is None else now
    root = Path(data)
    try:
        state = json.loads((root / 'config/onboarding.json').read_text())
        if not isinstance(state, dict):
            return 'waiting'
    except (OSError, ValueError):
        state = {}
    try:
        rows = json.loads((root / 'events/outbox.json').read_text()).get('rows', [])
        welcome = [r for r in rows if isinstance(r, dict) and
                   isinstance(r.get('payload'), dict) and r['payload'].get('label') == LABEL]
    except (OSError, ValueError, AttributeError, TypeError):
        welcome = []
    if any(r.get('status') == 'delivered' for r in welcome):
        return 'delivered'
    if any(r.get('status') == 'pending' for r in welcome):
        return 'queued'
    phase = state.get('phase')
    if phase in ('delivered', 'existing'):
        return phase
    if _accepted_scheduled_brief(root):
        return 'existing'
    if state.get('welcome_ambiguous') or _welcome_ambiguous(welcome):
        return 'waiting'  # acceptance unknown: never shown as retrying or sent
    if phase == 'model_held':
        unresolved = _unresolved_scheduled_brief(root)
        if unresolved:
            return unresolved
        return 'model_held'
    if not phase:
        if state.get('scheduled_ambiguous'):
            return 'waiting'
        unresolved = _unresolved_scheduled_brief(root)
        if unresolved:
            return unresolved
    if phase == 'composing':
        try:
            if float(state.get('lease_until', 0)) > now:
                return 'composing'
        except (TypeError, ValueError):
            pass
        return 'retrying'
    if phase == 'queued' or any(r.get('status') == 'failed' for r in welcome):
        return 'retrying'
    return 'waiting'


DISCOVERY_LABEL = 'onboarding:history-discovery:sotto-welcome-brief'
DISCOVERY_KEY = 'onboarding:history-discovery:v1'
DISCOVERY_OBSERVATION_LIMIT = 6


def discovery_context(data, pack, permitted):
    """Bounded canonical observations from completed permitted initial scans."""
    import sys
    sys.path.insert(0, str(Path(pack) / '_shared/knowledge'))
    sys.path.insert(0, str(Path(pack) / '_shared/lib'))
    sys.path.insert(0, str(Path(pack) / '_shared/scripts'))
    import knowledge as kg
    import yaml
    from knowledge_query import _conflicts
    from relationship_importance import for_contact
    import preferences
    import jsonstore
    from source_context import _source_rows, _read_source_state, _state_path
    import history_coverage
    import math
    try:
        checkpoints = json.loads((Path(data) / 'knowledge/history-state.json').read_text())['sources']
        source_names = ('imessage', 'whatsapp', 'gmail')
        permitted_sources = [source for source in source_names if permitted(source)]
        checkpoints = checkpoints if isinstance(checkpoints, dict) else {}

        def initial_review(row):
            initial = row.get('initial_coverage')
            return initial if isinstance(initial, dict) else row

        def excluded_count(row, review):
            count = review.get('excluded_page_count')
            if count is not None:
                return count
            legacy = row.get('excluded_pages')
            return len(legacy) if isinstance(legacy, (dict, list)) else int(bool(legacy))

        def initial_is_eligible(row):
            review = initial_review(row)
            excluded = excluded_count(row, review)
            # Once a completed initial snapshot has been retained, later continuation errors
            # do not invalidate it. Before that snapshot exists, current failure markers may
            # belong to the initial window itself and must keep the source ineligible.
            initial_saved = isinstance(row.get('initial_coverage'), dict)
            current_failure = (row.get('error') or row.get('error_code')
                              or row.get('rejected_request_revision'))
            return (row.get('initial_complete') is True and row.get('last_success')
                    and review.get('complete', row.get('initial_complete') is True) is True
                    and excluded == 0 and (initial_saved or not current_failure))

        eligible_sources = {source: row for source, row in checkpoints.items()
                            if source in permitted_sources and isinstance(row, dict)
                            and initial_is_eligible(row)}
        if not eligible_sources:
            return None
        # Preserve coverage for every currently permitted history source. A held, partial or
        # not-yet-started source explains the limits of this early handoff; it cannot ground
        # the selected fact or contribute relationship-ranking evidence.
        coverage = {}
        for source in permitted_sources:
            row = checkpoints.get(source)
            if not isinstance(row, dict):
                coverage[source] = {'status': 'unknown', 'review_scope': 'unknown'}
                continue
            projection = dict(history_coverage.window_coverage(row).get('initial_review',
                                                                       {'review_scope': 'unknown'}))
            review_scope = projection.get('review_scope', 'unknown')
            attempt = row.get('last_attempt')
            attempt = attempt if isinstance(attempt, dict) else {}
            initial_saved = isinstance(row.get('initial_coverage'), dict)
            current_failure = (row.get('error') or row.get('error_code')
                               or row.get('rejected_request_revision'))
            eligible = initial_is_eligible(row)
            if not eligible and (excluded_count(row, initial_review(row)) or (
                    not initial_saved and (current_failure or attempt.get('status') in ('failed', 'excluded')))):
                # An interrupted initial review is partial even before traversal completes.
                review_scope = 'partial'
                projection['review_scope'] = review_scope
            elif review_scope == 'reviewed_window' and not eligible:
                # The shared projection measures extraction completeness; discovery also
                # requires a successful initial extraction. Preserve known exclusions/failures
                # as partial and leave unproven success unknown.
                review_scope = 'unknown'
                projection['review_scope'] = review_scope
            if eligible and review_scope == 'reviewed_window':
                status = 'reviewed'
            elif review_scope == 'partial':
                status = 'partial'
            elif attempt.get('status') == 'held':
                status = 'held'
            elif review_scope == 'in_progress':
                status = 'in_progress'
            else:
                status = 'unknown'
            # Keep the shared model-facing projection. Cursor bounds and extraction counters
            # are window evidence only; missing counters remain unknown rather than complete.
            if 'window' in projection:
                projection['requested_window'] = projection.pop('window')
            coverage[source] = {**projection, 'status': status}
        source_rows = _source_rows(_read_source_state(_state_path(data)))
        regrants = {}
        for source, row in eligible_sources.items():
            cutoff = source_rows.get(source, {}).get('consent_regrant_epoch')
            if cutoff is None:
                continue
            if type(cutoff) not in (int, float) or not math.isfinite(cutoff) or cutoff <= 0:
                return None
            success = datetime.fromisoformat(str(row['last_success']).replace('Z', '+00:00'))
            if success.tzinfo is None or success.timestamp() <= cutoff:
                return None  # require fresh learning; the immutable initial coverage stays historical
            regrants[source] = cutoff
        try:
            history = json.loads((Path(data) / 'knowledge/relationship_state.json').read_text()).get('history', {})
        except (OSError, ValueError, AttributeError):
            history = {}
        try:
            conflicts = json.loads((Path(data) / 'knowledge/conflicts.json').read_text())
        except (OSError, ValueError):
            conflicts = {}
        candidates, people = [], []
        for path in sorted((Path(data) / 'knowledge/people').glob('*.md')):
            try:
                person = kg.parse_person_file(path.read_text())
            except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
                continue
            people.append(person)
        prefs = preferences.load_explicit()
        explicit = prefs.get('vip_people', [])
        for person in people:
            if any(preferences.proactively_muted(person.name, {'address': identifier}, prefs)
                   for identifier in ['', *person.identifiers]):
                continue
            unique_name = sum(p.name.casefold() == person.name.casefold() for p in people) == 1
            disputed = {fid for pair in _conflicts(person, conflicts) for fid in pair}
            record = history.get(person.canonical_id, {}) if isinstance(history, dict) else {}
            channels = set((record.get('channels') or {}).keys()) if isinstance(record, dict) else set()
            channels = {'gmail' if source == 'email' else source for source in channels}
            cutoff = max((regrants.get(source, 0) for source in channels), default=0)
            if cutoff and isinstance(record, dict):
                since = datetime.fromtimestamp(cutoff, timezone.utc).date().isoformat()
                record = {**record, 'importance_evidence': {
                    key: [day for day in days if isinstance(day, str) and day > since]
                    for key, days in (record.get('importance_evidence') or {}).items()}}
            # Aggregated dated importance cannot be split by source: retain it only when every
            # contributing channel is covered and permitted. Canonical identity avoids name collisions.
            history_allowed = bool(person.canonical_id and channels and channels <= eligible_sources.keys())
            importance = for_contact({'canonical_id': person.canonical_id, 'name': person.name},
                {person.canonical_id: record} if history_allowed else {},
                explicit if unique_name else [], datetime.now(timezone.utc))
            if not history_allowed:
                channels = set()
            for _fid, fact in kg.sorted_active_facts(person.facts, datetime.now(timezone.utc)):
                refs = list(dict.fromkeys([fact.source_ref, *fact.evidence_refs]))
                refs = [ref for ref in refs if isinstance(ref, str) and ref]
                cited = {ref.split(':', 1)[0] for ref in refs}
                if (_fid in disputed or fact.source != 'observed_message' or not refs
                        or not cited <= eligible_sources.keys()
                        or not fact.text.strip() or not person.name.strip()):
                    continue
                cutoff = max((regrants.get(source, 0) for source in cited), default=0)
                if cutoff and (not isinstance(fact.last, str) or fact.last[:10] <=
                        datetime.fromtimestamp(cutoff, timezone.utc).date().isoformat()):
                    continue  # date-only facts cannot prove observation after a same-day regrant
                candidate = {'canonical_id': person.canonical_id,
                        'person': person.name[:200], 'fact': fact.text[:700],
                        'observed_date': fact.last, 'source_refs': refs[:12],
                        'fact_sources': sorted(cited), 'importance': importance}
                candidates.append(candidate)
        candidates.sort(key=lambda candidate: (
            -{'vvip': 2, 'vip': 1}.get(candidate['importance'].get('tier'), 0),
            -len(candidate['fact_sources']), candidate['person']))
        by_person = {}
        for candidate in candidates:
            identity = candidate['canonical_id'] or candidate['person'].casefold()
            by_person.setdefault(identity, []).append(candidate)
        observations = []
        while by_person and len(observations) < DISCOVERY_OBSERVATION_LIMIT:
            for identity in list(by_person):
                observations.append(by_person[identity].pop(0))
                if not by_person[identity]:
                    del by_person[identity]
                if len(observations) == DISCOVERY_OBSERVATION_LIMIT:
                    break
        # Bind every permission represented in coverage to the composition/outbox recheck.
        return {'observations': observations, 'sources': sorted(coverage),
                'coverage': coverage} if observations else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, jsonstore.Unreadable):
        return None


def discovery_delivered(data, now=None):
    with json_transaction(str(Path(data) / 'config/onboarding.json'), default={}) as state:
        state['discovery'] = {'phase': 'delivered', 'completed_at': time.time() if now is None else now}


def _definite_non_send(row):
    """A closed row that provably never reached the provider; nothing else may release a claim."""
    return (row.get('status') not in ('pending', 'delivered')
            and row.get('acceptance') in ('not_attempted', 'rejected')
            and not row.get('acceptance_uncertain'))


def _discovery_claims_unresolved(discovery, rows):
    """Settle recorded send claims against outbox rows; True while any claim may have landed.

    The send seam records each claim permanently before provider contact (`discovery_claimed`).
    Only a definite non-send releases one. A claim whose row is still pending stays open; a row
    that is missing (pruned) or uncertain can never be proven unsent, so it holds forever.
    """
    claims = discovery.get('claims')
    if not isinstance(claims, dict):
        claims = discovery['claims'] = {}
    by_id = {row.get('id'): row for row in rows if row.get('id')}
    for run_id in list(claims):
        row = by_id.get(run_id)
        if row is not None and _definite_non_send(row):
            del claims[run_id]
    return bool(claims)


def discovery_claimed(data, run_id, day):
    """Permanently record a discovery send claim at the send seam, before provider contact.

    Returns False (do not send) when a discovery was delivered, is ambiguous, or another run's
    claim is unresolved. The record lives in the onboarding state, which the outbox never prunes.
    """
    root = Path(data)
    rows = [row for row in _outbox_rows(root) if isinstance(row, dict)
            and isinstance(row.get('payload'), dict) and row['payload'].get('label') == DISCOVERY_LABEL]
    with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
        discovery = state.setdefault('discovery', {})
        if discovery.get('phase') in ('delivered', 'ambiguous'):
            return False
        _discovery_claims_unresolved(discovery, [row for row in rows if row.get('id') != run_id])
        claims = discovery['claims']
        if any(other != run_id for other in claims):
            return False
        claims[run_id] = str(day)
    return True


def discovery_tick(data, ready, start, now=None):
    """One history handoff after the fast first look; ambiguous acceptance never authorizes replay."""
    now = time.time() if now is None else now
    root = Path(data)
    try:
        snapshot = json.loads((root / 'config/onboarding.json').read_text())
    except (OSError, ValueError):
        return 'waiting_for_first_look'
    if not isinstance(snapshot, dict) or snapshot.get('phase') != 'delivered':
        return 'waiting_for_first_look'
    with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
        if state.get('phase') != 'delivered':
            return 'waiting_for_first_look'
        discovery = state.setdefault('discovery', {})
        if discovery.get('phase') == 'delivered':
            return 'delivered'
        rows = [row for row in _outbox_rows(root) if isinstance(row, dict)
                and isinstance(row.get('payload'), dict)
                and row['payload'].get('label') == DISCOVERY_LABEL]
        if any(row.get('status') == 'delivered' and row.get('acceptance') == 'accepted'
               and isinstance(row.get('receipt'), dict) and row['receipt'].get('accepted_at')
               and (row['receipt'].get('message_id') or row['receipt'].get('message_ids'))
               for row in rows):
            discovery.update(phase='delivered', completed_at=now)
            return 'delivered'
        if discovery.get('phase') == 'ambiguous' or _welcome_ambiguous(rows):
            discovery['phase'] = 'ambiguous'
            return 'ambiguous'
        if any(row.get('status') == 'pending' for row in rows):
            return 'queued'
        if _discovery_claims_unresolved(discovery, rows):
            # A claimed send whose outbox evidence aged out (or was never proven unsent) may
            # have landed, including when its delivery callback failed. Never offer another.
            discovery['phase'] = 'ambiguous'
            return 'ambiguous'
        if discovery.get('retry_at', 0) > now:
            return 'composing'
        if not ready():
            return 'waiting_for_history'
        day = int(now // 86400)
        if discovery.get('attempt_day') != day:
            discovery.update(attempt_day=day, attempts=0)
        if discovery.get('attempts', 0) >= ATTEMPTS_PER_DAY:
            return 'retry_later'
        discovery.update(phase='composing', attempts=discovery.get('attempts', 0) + 1,
                         retry_at=now + RETRY_SECONDS)
    if start() is False:
        with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
            discovery = state.get('discovery', {})
            if discovery.get('phase') == 'composing' and discovery.get('retry_at') == now + RETRY_SECONDS:
                discovery.update(phase='waiting', retry_at=0,
                                 attempts=max(0, discovery.get('attempts', 1) - 1))
        return 'not_started'
    return 'started'
