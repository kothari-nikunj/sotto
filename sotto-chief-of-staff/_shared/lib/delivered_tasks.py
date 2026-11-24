"""Select obligations from one accepted background delivery, using the original task versions."""
import hashlib
import re
from collections import Counter


SELECTOR_VERSION = 5
MAX_BINDINGS = 50
MIN_QUOTED_WORDS = 2
DONE_INVITATION = ' Reply to this message with “Done”.'
SCHEDULED_NUDGES = {'proactive', 'cron:sotto-proactive', 'run-now:sotto-proactive'}


def _tokens(value):
    return re.findall(r"[^\W_]+", str(value or '').casefold())


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _contains(words, phrase):
    return bool(phrase) and any(words[i:i + len(phrase)] == phrase for i in range(len(words)))


def compose_nudge(compose, nudges, effects):
    """Quiet ticks never reach composition. Every composed nudge records its composing revision, so
    a reply can see it; only a task nudge's eligibility later binds tasks (see snapshot)."""
    from source_authority import current_authority
    from delivery_effects import stage
    try:
        authority = current_authority()
    except Exception:
        authority = {}
    output = compose('proactive', nudges)
    if authority.get('complete') and output:
        try:
            stage([{'kind': 'task_nudge_context', 'revision': authority['revision'],
                    'body_sha256': _hash(output.strip())}])
        except (OSError, ValueError):
            pass  # The original notification can still be delivered.
    return output


def nudge_scope(effects, output, read_scope):
    revisions = {e.get('revision') for e in effects if e.get('kind') == 'task_nudge_context'
                 and e.get('body_sha256') == _hash(output.strip())}
    if len(revisions) == 1:
        scope = read_scope()
        if scope and scope['source_revision'] in revisions:
            return scope
    return None


def attach(payload, scope, *, visible_lines=()):
    import ledger_io
    try:
        result = snapshot(payload, scope, ledger_io.load_active(), visible_lines=visible_lines)
        if result:
            payload.update(task_snapshot=result, prep_source_revision=scope['source_revision'],
                           prep_route_digest=scope['route_digest'])
    except Exception as error:
        import logging
        logging.getLogger(__name__).warning('delivered_task_context_unavailable error=%s', type(error).__name__)


def _displayed_alternatives(line, asks):
    """An explicit "to redirect or transfer the domain" shares the saved task's object."""
    for i in range(len(line) - 3):
        if line[i] == 'to' and line[i + 2] == 'or' and any(
                len(ask) >= MIN_QUOTED_WORDS and line[i + 3:i + 3 + len(ask)] == ask for ask in asks):
            yield line[i + 1]


def _enough_terms(shown, terms):
    return bool(terms) and len(shown) >= min(2, len(terms))


def _heading_supports_ask(words, header, ask):
    if tuple(words[:len(header)]) == header:
        return True
    for prefix in (('you', 'promised', 'to'), ('you', 'agreed', 'to'), ('could', 'you'), ('can', 'you'),
                   ('still', 'has'), ('did', 'you'), ('please',), ('any',), ('a',), ('an',), ('the',)):
        if tuple(words[:len(prefix)]) == prefix:
            words = words[len(prefix):]
            break
    if _contains(words, ask):
        return words[:len(ask)] == ask or words[1:2] == ['or'] and words[2:2 + len(ask)] == ask
    return bool(words) and words[0] in ask


def _shows_task(line, tasks, namesakes, *, quote, paraphrase):
    from brief_validate import ASK_STOP_WORDS
    words = set(line) - ASK_STOP_WORDS
    alternatives = [(phrase, words & terms) for phrase, terms in namesakes if _enough_terms(words & terms, terms)]
    for ask, terms in tasks:
        shown = words & terms
        if quote and len(ask) >= MIN_QUOTED_WORDS and _contains(line, ask):
            # A verbatim action can distinguish “send the deck” from “review the deck”.
            if all(words.intersection(phrase) < words.intersection(ask) for phrase, _ in alternatives):
                return True
        if paraphrase and _enough_terms(shown, terms) and all(other < shown for _, other in alternatives):
            if not any(shown <= other_terms for _, other_terms in namesakes):
                return True
    return False


def _only_saved_task(line, header, name, asks):
    # Free-form copy can support bare Done only when it explicitly assigns the complete task to the owner.
    words = line[len(header):] if header else line
    assigned = [*name, 'asked', 'you', 'to']
    if name and line[:len(assigned)] == assigned:
        words = line[len(assigned):]
    elif words[:3] in (['you', 'promised', 'to'], ['you', 'agreed', 'to']):
        words = words[3:]
    else:
        return False
    text = ' '.join(words)
    text = re.sub(r' (?:is |was )?(?:overdue|due (?:today|tomorrow|yesterday|monday|tuesday|'
                  r'wednesday|thursday|friday|saturday|sunday))$', '', text)
    return text.split() in asks


def _task_phrases(row):
    return [_tokens(row[field]) for field in ('summary', 'ask', 'deliverable') if row.get(field)]


def _contact_context(rows):
    contacts, dotted = {}, set()
    for row in rows:
        name = row.get('contact_name') or ''
        person = tuple(_tokens(name))
        if person:
            identity = row.get('contact_identifier') or row.get('anchor_key')
            contacts.setdefault(person, {}).setdefault(identity, set()).update(map(tuple, _task_phrases(row)))
            if '.' in name:
                dotted.add(re.escape(name))
    if not dotted:
        return contacts, None
    names = '|'.join(sorted(dotted, key=len, reverse=True))
    return contacts, re.compile(r'(?<!\w)(?:' + names + r')(?!\w)', re.I)


def _continues_task(sentence, words, header, owners, *, first):
    if tuple(words[:len(header)]) == header:
        return True
    asks = {ask for phrases in owners.values() for ask in phrases if ask}
    quoted = [list(ask) for ask in asks if _contains(words, list(ask))]
    if quoted:
        return any(_heading_supports_ask(words, header, ask) for ask in quoted)
    if first:
        return True
    if words[:1] == ['any'] and sentence.rstrip().endswith('?'):
        subject = words[1:-2] if words[-2:] == ['in', 'mind'] else words[1:]
        return any(_contains(list(ask), subject) for ask in asks)
    return False


def _item_sentences(item, contacts, dotted, raw_clauses):
    if dotted:
        item = dotted.sub(lambda match: re.sub(r'\.(?=\s+\S)', ' ', match.group()), item)
    header = ()
    for separator in re.finditer(r' +[—–-] +|:\s+|\.\s+', item):
        candidate = tuple(_tokens(item[:separator.start()]))
        if candidate in contacts:
            header, item = candidate, item[separator.end():]
            break
    for index, sentence in enumerate(re.split(r'(?<=[.!?;])\s+', item)):
        words = _tokens(sentence)
        if words:
            raw_clauses.append((header if not index else (), words))
            people = {person for person in contacts if _contains(words, list(person))}
            other_full_names = [person for person in people - {header} if len(person) > 1]
            if header:
                if index and any(words[:len(person)] == list(person) for person in other_full_names):
                    header = None
                elif not _continues_task(sentence, words, header, contacts[header], first=not index):
                    yield [], people | {header}, ()  # The names were printed but the task owner is unknown.
                    header = None
                    continue
            if header is None and not people:
                yield [], set(), ()  # Lost the item owner. Count the sentence but authorize no task.
                continue
            yield ([*header, *words], people | {header}, header) if header else (words, people, ())
            if header and people - {header}:
                header = None


def _displayed_sentences(items, contacts, dotted, raw_clauses):
    named_context = False
    for item in items:
        for words, people, header in _item_sentences(item, contacts, dotted, raw_clauses):
            if people:
                named_context = True
            # Later anonymous prose could continue the preceding person's item.
            yield ([] if named_context and not people else words), people, header


def _participants_match(people, owner, asks, line):
    if not people:
        return True
    if owner not in people:
        return False
    others = people - {owner}
    return not others or any(_contains(line, ask) and all(_contains(ask, list(person)) for person in others)
                             for ask in asks)


def _visible_names(displayed, contacts):
    # A shared spelling represents at least two possible owners, even when only one task binds.
    named = {person for _, people, _ in displayed for person in people}
    return [sorted(_hash(word) for word in set(person)) for person in sorted(named)
            for _ in range(min(2, len(contacts[person])))]


def _send_object(ask, contacts=()):
    from brief_validate import ASK_STOP_WORDS
    if not ask[2:] or ask[0] != 'send' or ask[1] not in ('a', 'an', 'the'):
        return None
    # Consume the entire obligation. A recipient is one word or a named contact.
    if ask[3:] and (ask[3] != 'to' or not (len(ask[4:]) == 1 or tuple(ask[4:]) in contacts)):
        return None
    if ask[2] not in ASK_STOP_WORDS and ask[2] not in ('me', 'us', 'him', 'them', 'these', 'those'):
        return ask[2]


def commitment_notice(row):
    """Offer only a complete current obligation; a shorter deliverable may omit a second action."""
    from datetime import date
    from ledger_io import contact_label, is_waiting_on, task_label
    from timeutil import _calendar_day
    label, name = task_label(row), contact_label(row)
    day = _calendar_day(row.get('deadline'))
    if (not day or not label or name == '(unknown)' or is_waiting_on(row.get('action_type'))
            or len((name + label).splitlines()) != 1
            or not all(_contains(_tokens(label), phrase) for phrase in _task_phrases(row))):
        return None
    due = date.fromisoformat(day)
    return f'{name} — {label} (due {due:%b} {due.day}, {due.year}).'


def task_label_digest(row):
    from ledger_io import task_label
    return _hash(task_label(row))


def _whole_send(row, asks, visible, contacts):
    from ledger_io import is_waiting_on, task_label
    if is_waiting_on(row.get('action_type')):
        return None
    canonical = _tokens(task_label(row))
    obj = _send_object(canonical, contacts)
    if not obj or canonical not in asks or not any(_contains(line, canonical) for line in visible):
        return None
    # A shorter deliverable cannot discard part of the recorded ask or promise a different recipient.
    ask, summary = _tokens(row.get('ask')), _tokens(row.get('summary'))
    # A topic heading may name just the object or recipient, as in “Deck follow-up”.
    topic = summary[:-2]
    heading = topic and summary[-2:] == ['follow', 'up'] and topic in ([obj], canonical[4:])
    for phrase in (ask, [] if heading else summary):
        if phrase and (_send_object(phrase, contacts) != obj or phrase[3:] and phrase[3:] != canonical[3:]):
            return None
    return obj, task_label_digest(row)


def _visible_binding(row, displayed, grounded, nudge_key, contacts):
    from brief_validate import TASK_STOP_WORDS
    from ledger_io import is_waiting_on
    from delivery_effects import loop_identity, loop_version
    key = row.get('anchor_key')
    version = loop_version(row)
    proved = (key, version) in grounded
    name = _tokens(row.get('contact_name'))
    owners = contacts.get(tuple(name), {})
    ambiguous_name = len(owners) > 1
    if not key or ambiguous_name and not proved:
        return None
    identity = row.get('contact_identifier') or key
    namesake_phrases = {phrase for other, phrases in owners.items() if other != identity for phrase in phrases}
    asks = [ask for ask in _task_phrases(row) if tuple(ask) not in namesake_phrases]
    tasks = [(ask, set(ask).difference(name, TASK_STOP_WORDS)) for ask in asks]
    namesakes = [(phrase, set(phrase).difference(name, TASK_STOP_WORDS)) for phrase in namesake_phrases]
    visible, participant_words = [], set()
    for line, people, header in displayed:
        if (header and header != tuple(name)) or not _participants_match(people, tuple(name), asks, line):
            continue
        if len(name) == 1 and people and not header and key != nudge_key:
            # A prose word such as “Will” is not an owner label. A quoted intro can name its owner.
            if not (proved and any(_contains(ask, name) and _contains(line, ask) for ask in asks)):
                continue
        candidates = [task for task in tasks if not header or _heading_supports_ask(line[len(header):], header, task[0])]
        if _shows_task(line, candidates, namesakes, quote=bool(people or proved),
                       paraphrase=bool(proved and (people or key == nudge_key))):
            visible.append(line)
            participant_words.update(word for person in people - {tuple(name)} for word in person)
    if not visible:
        return None
    shown = {word for line in visible for word in line}
    words = set(name).union(*asks) & shown
    for line in visible:
        words.update(_displayed_alternatives(line, asks))
    binding = dict(anchor_key=key,
                   loop_identity=loop_identity(row), loop_version=version,
                   owner_name=sorted(_hash(w) for w in set(name)),
                   words=sorted(_hash(w) for w in words - TASK_STOP_WORDS))
    if ambiguous_name:
        participant_words.update(shown.intersection(name))
    if participant_words:
        binding['participant_words'] = sorted(_hash(w) for w in participant_words)
    singleton = False
    if proved and key == nudge_key and len(displayed) == 1 and not is_waiting_on(row.get('action_type')):
        full_asks = [ask for ask in asks if all(_contains(ask, phrase) for phrase in _task_phrases(row))]
        singleton = _only_saved_task(visible[0], displayed[0][2], name, full_asks)
    return binding, singleton, _whole_send(row, asks, visible, contacts)


def _freeze_bindings(matches, raw_clauses):
    counts = Counter(word for _, line in raw_clauses for word in line)
    bindings = []
    for binding, _, whole_send in matches:
        if whole_send:
            obj, digest = whole_send
            if counts[obj] == 1:
                binding['whole_send'] = {'object': _hash(obj), 'task': digest}
        bindings.append(binding)
    candidates = {word for binding in bindings for word in binding['words']}
    # Count a printed heading for a generic close, but never as a sent object's mention.
    for header, line in raw_clauses:
        if len(header) == 1 or not _contains(line, list(header)):
            counts.update(header)
    repeated = {_hash(word) for word, count in counts.items() if count > 1}
    return bindings, sorted(candidates & repeated)


def snapshot(payload, scope, rows, *, visible_lines=()):
    """Capture visible selectors only. An eligibility or brief effect identifies its original row."""
    effects = payload.get('effects') or []
    eligibility = [e for e in effects if e.get('kind') == 'eligibility']
    grounded = {(e.get('anchor_key'), e.get('loop_version')) for e in effects
                if e.get('kind') in ('eligibility', 'loop_surfaced')}
    for effect in effects:
        if effect.get('kind') in ('brief_named', 'prep_tasks_offered'):
            grounded.update(effect.get('displayed_versions', {}).items())
    contacts, dotted = _contact_context(rows)
    presentation = payload.get('presentation')
    if presentation and not visible_lines:
        return None
    displayed = [presentation['summary'], *visible_lines] if presentation else payload['body'].splitlines()
    raw_clauses = []
    attributed = list(_displayed_sentences(displayed, contacts, dotted, raw_clauses))
    label = payload.get('label', '')
    nudge = label in SCHEDULED_NUDGES or label == 'event' or label.endswith(':proactive')
    scans = [e for e in eligibility if not e.get('notification_id')]
    selected = [e for e in eligibility if e.get('notification_id')]
    notice_key = selected[0].get('reply_task_anchor') if len(selected) == 1 and nudge and not presentation else None
    nudge_key = eligibility[0].get('anchor_key') if nudge and len(eligibility) == 1 else notice_key
    eligible = {(e.get('anchor_key'), e.get('loop_version')) for e in scans}
    chasing = {e.get('anchor_key') for e in effects if e.get('kind') in ('chase', 'handoff')}
    matches = []
    for row in rows:
        match = _visible_binding(row, attributed, grounded, nudge_key, contacts)
        if match:
            binding, singleton, send = match
            if (notice_key and binding['anchor_key'] == notice_key and (notice_key, binding['loop_version']) in eligible
                    and (notice := commitment_notice(row)) and payload['body'] == notice + DONE_INVITATION):
                singleton = True
            singleton = singleton and (binding['anchor_key'], binding['loop_version']) in eligible and binding['anchor_key'] not in chasing
            if singleton:
                binding['task_label_digest'] = task_label_digest(row)
            matches.append((binding, singleton, send))
    if not matches or len(matches) > MAX_BINDINGS:
        return None
    bindings, repeated = _freeze_bindings(matches, raw_clauses)
    if presentation:
        expected = (presentation.get('retained_text_proof') or {}).get('gallery_payload_sha256')
    else:
        from retained_brief_proof import text_payload_hash
        expected = text_payload_hash(payload['body'])
    if expected:
        return {**scope, 'selector_version': SELECTOR_VERSION, 'payload_hash': expected, 'bindings': bindings,
                'repeated_task_words': repeated, 'visible_names': _visible_names(attributed, contacts), **({'visible_cards': True} if presentation else {}),
                'singleton': len(matches) == 1 and matches[0][1]}


def accepted(payload, receipt, current):
    saved = payload.get('task_snapshot')
    if (current and saved and saved.get('selector_version') == SELECTOR_VERSION and isinstance(receipt, dict) and receipt.get('acceptance') == 'accepted'
            and (not payload.get('presentation') or saved.get('visible_cards') is True)
            and receipt.get('route_proof_version') == 1 and receipt.get('route_digest') == saved['route_digest']
            and receipt.get('payload_hash') == saved['payload_hash'] and receipt.get('message_id')
            and receipt.get('message_ids') == [receipt['message_id']]):
        return {'delivered_tasks': saved}
    return {}


def sent_selection(text, phrase):
    return bool(re.fullmatch(r"(?:I\s+)?sent\s+(?:(?:a|an|the)\s+)?" + re.escape(phrase) + r"[.!]?", text.strip(), re.I))


def _task_word_matches(saved, hashes, named):
    if (not named or len(hashes) == 1) and hashes.issubset(saved['repeated_task_words']):
        return []
    matches = [b for b in saved['bindings'] if hashes.issubset(b['words'])
               and hashes.difference(b.get('participant_words', ()))]
    if named and (len(matches) != 1 or named != [matches[0].get('owner_name')]):
        return []
    return matches


def select(data_root, body, window_ms, route):
    from protected_brief_detail import UnavailableError, outbox_rows
    args, admitted_at_ms, text = body['args'], body['admitted_at_ms'], body['text']
    dispatch = args['delivery']
    try:
        rows = [row for row in outbox_rows(data_root) if row.get('id') == dispatch['dispatch_id']]
    except UnavailableError:
        return None
    if len(rows) != 1 or rows[0].get('status') != 'delivered' or rows[0].get('acceptance') != 'accepted':
        return None
    receipt = rows[0].get('receipt') or {}
    saved = receipt.get('delivered_tasks')
    if (not saved or saved.get('selector_version') != SELECTOR_VERSION
            or receipt.get('visual_artifact_id') and saved.get('visible_cards') is not True
            or route != saved.get('route_digest') or dispatch['route_digest'] != route
            or saved['source_revision'] != args['source_revision']
            or receipt.get('acceptance') != 'accepted' or receipt.get('route_digest') != route
            or receipt.get('message_ids') != [dispatch['message_id']]
            or receipt.get('message_id') != dispatch['message_id']
            or receipt.get('payload_hash') != dispatch['payload_hash']
            or not 0 <= admitted_at_ms - receipt['accepted_at'] * 1000 <= window_ms):
        return None
    selected = []
    from brief_validate import ASK_STOP_WORDS
    for item in args['items']:
        words = _tokens(item['name'])
        sent = sent_selection(text, item['name'])
        if not sent and words == ['done'] and len(args['items']) == 1:
            matches = saved['bindings'] if saved['singleton'] else []
            if len(matches) != 1 or not matches[0].get('task_label_digest'):
                matches = []
        else:
            hashes = {_hash(w) for w in words if w not in ASK_STOP_WORDS}
            named = [name for name in saved['visible_names'] if hashes.issubset(name)]
            if sent:
                report = _tokens(text)
                obj = report[report.index('sent') + 1:]
                article = obj[:1] in (['a'], ['an'], ['the'])
                object_hash = _hash(' '.join(obj[1:] if article else obj))
                matches = [b for b in saved['bindings'] if (article or not named)
                           and object_hash == b.get('whole_send', {}).get('object')]
            else:
                matches = _task_word_matches(saved, hashes, named)
        binding = {k: matches[0][k] for k in ('anchor_key', 'loop_version', 'loop_identity')} if len(matches) == 1 else {}
        if binding and sent:
            binding['sent_task_digest'] = matches[0]['whole_send']['task']
        elif binding and words == ['done'] and matches[0].get('task_label_digest'):
            binding['task_label_digest'] = matches[0]['task_label_digest']
        selected.append({**item, **binding})
    keys = [item['anchor_key'] for item in selected if 'anchor_key' in item]
    return [item if keys.count(item.get('anchor_key')) <= 1 else original
            for original, item in zip(args['items'], selected, strict=True)]
