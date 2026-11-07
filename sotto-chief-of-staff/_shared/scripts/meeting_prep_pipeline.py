"""Shared complete meeting-prep orchestration; callers own admission and delivery effects."""
import json
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from time import monotonic


@contextmanager
def _timed(stage):
    from sotto_log import diag
    started = monotonic()
    try:
        yield
    finally:
        # Callers supply fixed labels only; never log inputs, identities or error text.
        diag(f"[meeting-prep timing] {stage} elapsed_ms={round((monotonic() - started) * 1000)}")


def _timed_call(stage, function, *args, **kwargs):
    with _timed(stage):
        return function(*args, **kwargs)


def run(pack, selection, command, *, exact_meeting=None):
    from source_context import allowed, read_local, project_local, project_emails, project_x, used_sources
    from textutil import unwrap_tool_result
    from delivery_effects import instant
    from local_snapshot import resolve_effective_local
    from timeutil import configured_tz, configured_user_email, _now_local
    sys.path.insert(0, str(Path(pack) / 'meeting-prep/scripts'))
    from compose_meeting_prep import compose
    from prep_selection import require_exact_meeting, upcoming, focus_hits, time_focus_hits, title_focus_hits
    if not allowed('calendar'):
        raise RuntimeError('Calendar prep source unavailable')
    if selection.get('meeting') is not None:
        if exact_meeting is not None and exact_meeting != selection['meeting']:
            raise ValueError('Conflicting meeting occurrence')
        exact_meeting = selection['meeting']
    focus = selection.get('focus', '') if selection['mode'] == 'focus' else ''
    cutoff = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix='sotto-meeting-prep-') as directory:
        root = Path(directory)
        gmail, calendar, receipts = (root / name for name in ('gmail.json', 'calendar.json', 'sources.json'))
        with _timed('gather'):
            # Independent source reads share no inputs. Join both before projection, selection or
            # model work; this preserves the full context without adding another provider request.
            with ThreadPoolExecutor(max_workers=2) as gather:
                google_read = gather.submit(_timed_call, 'gather_google', command, '_shared/scripts/gather_google.py',
                    '--gmail-out', gmail, '--cal-out', calendar,
                    '--source-results-out', receipts, '--skip-stale', decode=False)
                local_read = gather.submit(_timed_call, 'gather_local', read_local, 168)
                google_read.result()
                raw_local = local_read.result()
        with _timed('identity'):
            observations = json.loads(receipts.read_text())
            if observations.get('calendar', {}).get('status') not in ('ok', 'partial'):
                raise RuntimeError('Calendar prep source unavailable')
            events = json.loads(calendar.read_text())
            if exact_meeting is not None:
                requested_start = instant(exact_meeting['start'])
                calendar_id = exact_meeting.get('calendar_id')
                if 'calendar_id' in exact_meeting:
                    # Independent worker validation reuses its shared read contract,
                    # without importing receiver action/admission modules.
                    from google_calendar_read import validate_selection as validate_calendars
                    calendar_id = validate_calendars({'mode': 'selected', 'calendar_ids': [calendar_id]})['calendar_ids'][0]
                # Typed reads retain provider IDs; gathered offers use calendar-scoped IDs.
                # Unscoped raw IDs need complete coverage to exclude hidden calendar collisions.
                events = [e for e in events if isinstance(e, dict)
                          and instant(e.get('start')) == requested_start
                          and (calendar_id is None or e.get('calendar_id') == calendar_id)
                          and (e.get('id') == exact_meeting['event_id']
                               or ((calendar_id is not None or observations.get('calendar', {}).get('status') == 'ok')
                                   and isinstance(e.get('calendar_id'), str) and e['calendar_id']
                                   and e.get('provider_event_id') == exact_meeting['event_id']))]
                if len(events) != 1 or requested_start is None:
                    raise RuntimeError('Offered calendar occurrence unavailable')
                exact_meeting = {'event_id': events[0]['id'], 'start': exact_meeting['start']}
            local = resolve_effective_local(project_local(unwrap_tool_result(raw_local)))
            emails = project_emails(json.loads(gmail.read_text()))
            sources = set(used_sources(local, emails, already_projected=True)) | {'calendar'}

            def guard(stage):
                if any(not allowed(source) for source in sources):
                    raise RuntimeError('source permission changed during prep ' + stage)

            guard('gather')
            inputs = {'google': {'emails': emails, 'events': events,
                                'userEmail': configured_user_email(), 'userTimezone': configured_tz(),
                                '_source_availability': {
                                    source: 'available' if row['status'] == 'ok' else row['status']
                                    for source, row in observations.items()
                                    if source in ('gmail', 'calendar') and isinstance(row, dict)
                                    and row.get('status') in ('ok', 'partial', 'unavailable', 'disabled')}},
                      'source_results': observations,
                      'local': local, 'focus': focus}
            timed_focus = time_focus_hits(inputs) if focus and exact_meeting is None else None
            selected_focus = timed_focus
            if (selected_focus is None and focus
                    and (exact_meeting is None or selection.get('meeting') is not None)
                    and not (exact_meeting is not None and focus_hits(inputs))):
                # An exact occurrence already resolves the event. Preserve a matched
                # attendee focus even when that person's name is also the event title.
                selected_focus = title_focus_hits(inputs)
            if selected_focus is not None:
                if len(selected_focus) != 1 or (exact_meeting is None and observations.get('calendar', {}).get('status') != 'ok'):
                    return {'text': "I couldn't match that time to one clear meeting on your calendar. Which day or meeting should I prep?" if timed_focus is not None else "I found more than one matching meeting, or the calendar is incomplete. Which day and time should I prep?",
                            'sources': sorted(sources), 'coverage_until': cutoff,
                            'calendar_observed_at': observations.get('calendar', {}).get('observed_at') or cutoff}
                event = selected_focus[0][1]
                exact_meeting = {'event_id': event.get('id'), 'start': event.get('start')}
                inputs['google']['events'] = [event]
                # Selection comes from a literal clock or exact title, not an inferred person. A single
                # attendee can reuse the existing deep-research focus after exact event selection.
                focus = selected_focus[0][4][0]['email'] if len(selected_focus[0][4]) == 1 else ''
                inputs['focus'] = focus
            if exact_meeting is not None or focus:
                # Offers and direct named requests use the same email-bound name evidence.
                # Only upcoming external attendees are inspected, with no new provider calls.
                from prep_identity import PrepIdentityResolver
                from personal_context import conversation_snapshot
                from functools import cache
                identity = PrepIdentityResolver(local, cache(conversation_snapshot))
                names = {}
                for meeting in upcoming(inputs):
                    external = {a['email'] for a in meeting[4]}
                    for attendee in meeting[1].get('attendees', []):
                        email = str(attendee.get('email', '')).strip().lower()
                        if email not in external:
                            continue
                        key = (email, attendee.get('displayName', ''))
                        if key not in names:
                            name, source = identity.resolve(attendee, _now_local(configured_tz()))
                            names[key] = (name, source)
                        name, source = names[key]
                        if name:
                            attendee['displayName'] = name
                            if source == 'gmail':
                                sources.add('gmail')
                guard('attendee identity')
            if exact_meeting is not None:
                inputs['exact_meeting'] = exact_meeting
                require_exact_meeting(inputs)
            elif focus:
                from research_contract import matches_focus
                hits = focus_hits(inputs)
                identities = {a['email'] for meeting in hits for a in meeting[4]
                              if matches_focus(focus, a['name'], a['email'])}
                if not hits or len(identities) != 1:
                    return {'text': "I couldn't match that person to one clear attendee in the upcoming meetings. Which person or meeting should I prep?",
                            'sources': sorted(sources), 'coverage_until': cutoff,
                            'calendar_observed_at': observations.get('calendar', {}).get('observed_at') or cutoff}
                inputs['google']['events'] = [hits[0][1]]
            calendar.write_text(json.dumps(inputs['google']['events']))
            gmail.write_text(json.dumps(emails))
        with _timed('notes_knowledge'):
            granola_path = root / 'granola.json'
            if allowed('granola'):
                command('_shared/scripts/gather_granola.py', '--days', 30, '--transcripts-since-hours', 168,
                        '--out', granola_path, decode=False)
                granola = json.loads(granola_path.read_text())
                local['granola_meetings'] = granola.get('meetings', [])
                sources.update(used_sources(granola=granola, already_projected=True))
            guard('Granola gather')
            local_path = root / 'local.json'
            local_path.write_text(json.dumps(local))
            inputs['prior_knowledge'] = command('_shared/knowledge/knowledge_query.py',
                '--calendar', calendar, '--gmail', gmail, '--local', local_path)
        with _timed('comms'):
            input_path = root / 'inputs.json'
            input_path.write_text(json.dumps(inputs))
            selected = command('morning-brief/scripts/select_attendees.py', input_path)
            # Known attendees still contribute their communications; focus always earns a deep pass.
            all_attendees = {attendee['email']: attendee for meeting in upcoming(inputs) for attendee in meeting[4]}
            if selected_focus is not None and not focus:
                for attendee in all_attendees.values():
                    if not any(row.get('email') == attendee['email'] for row in selected):
                        selected.append(attendee)
            if focus:
                for meeting in focus_hits(inputs):
                    for attendee in meeting[4]:
                        from research_contract import matches_focus
                        if matches_focus(focus, attendee['name'], attendee['email']) and not any(
                                row.get('email') == attendee['email'] for row in selected):
                            selected.append(attendee)
            attendees, comms_attendees, comms_path = (root / name for name in ('attendees.json', 'comms-attendees.json', 'comms.json'))
            attendees.write_text(json.dumps(selected))
            comms_attendees.write_text(json.dumps(list(all_attendees.values())))
            comms = {}
            if allowed('gmail'):
                command('_shared/scripts/gather_google.py', '--attendee-comms', comms_attendees,
                        '--comms-out', comms_path, *(['--expand-threads'] if focus or exact_meeting is not None else []), decode=False)
                comms = json.loads(comms_path.read_text())
                sources.add('gmail')
            comms_path.write_text(json.dumps(comms))
            inputs['attendee_comms'] = comms
            guard('preparation')
            filter_args = ['--filter-fresh', attendees]
            if focus:
                filter_args.extend(['--keep', focus])
            command('meeting-prep/scripts/persist_prep.py', *filter_args)
            guard('freshness filter')
        with _timed('research'):
            research_args = ['--attendees', attendees, '--context', calendar, '--comms', comms_path,
                             '--out', root / 'research.json']
            if focus:
                research_args.extend(['--focus', focus])
            research = command('_shared/scripts/research_attendees.py', *research_args)
            inputs['attendee_research'] = research
            guard('research')
        with _timed('x'):
            x_path = root / 'x.json'
            research_path = root / 'research.json'
            research_path.write_text(json.dumps(research))
            if allowed('x'):
                command('_shared/scripts/x_connectivity.py', '--calendar', calendar,
                        '--research', research_path, '--out', x_path, decode=False)
                raw_x = json.loads(x_path.read_text())
                sources.update(used_sources(x_context=raw_x, already_projected=True))
                guard('X gather')
                inputs['x_context'] = project_x(raw_x)
            guard('X gather')
        with _timed('compose'):
            result = compose(inputs)
            guard('composition')
            if not result.get('prep_markdown', '').strip():
                raise RuntimeError('meeting prep produced no result')
            from research_contract import prep_research_evidence
            from delivery_effects import stage
            research_evidence = prep_research_evidence(research)
            # Existing run-owned effects document survives the child/worker boundary and restart.
            # This is data only; it adds no effect and grants no read authority.
            stage([], research_evidence=research_evidence)
        with _timed('persist'):
            guard('persistence')
            try:
                persisted = command('meeting-prep/scripts/persist_prep.py', '--research', research_path,
                                    '--attendees', attendees)
            except RuntimeError:
                persisted = {'status': 'error', 'error': 'research persistence failed'}
            guard('persistence')
            if isinstance(persisted, dict) and persisted.get('error'):
                print('[meeting-prep] research persistence failed; prep remains available', file=sys.stderr)
        return {'text': result['prep_markdown'], 'sources': sorted(sources), 'coverage_until': cutoff,
                'research_evidence': research_evidence,
                'calendar_observed_at': observations.get('calendar', {}).get('observed_at') or cutoff,
                'persistence': persisted}
