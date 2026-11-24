"""Original ratings consumed by actual brief/relevance/drafting prompts."""

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import runtime_owner_preference_tool as tool
from test_runtime_owner_preference_tool import context, request, call

__all__ = ["context"]


def _seed_accepted(data, reference, text, sources=None):
    day, kind = reference.split('_')
    run = hashlib.sha256((reference + text).encode()).hexdigest()[:32]
    stamp = datetime.now(timezone.utc).timestamp()
    artifact = data / f'events/work-inputs/brief-{run}/artifact.json'
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({'brief_text': text, 'composed_at': stamp,
        '_delivery_run_ids': [run], '_source_permissions': ['gmail'] if sources is None else sources}))
    outbox = data / 'events/outbox.json'
    rows = json.loads(outbox.read_text())['rows'] if outbox.exists() else []
    rows.append({'id': run, 'kind': 'brief', 'day': day, 'status': 'delivered',
        'acceptance': 'accepted', 'acceptance_uncertain': False, 'contacted_at': stamp,
        'payload': {'label': f'cron:sotto-{kind}-brief', 'run_id': run},
        'receipt': {'message_id': f'fictional-{run}', 'accepted_at': stamp,
                    'brief_body_sha256': hashlib.sha256(text.encode()).hexdigest()}})
    outbox.write_text(json.dumps({'rows': rows}))
    return artifact


@pytest.fixture
def feedback(context, monkeypatch):
    data, policy, _ = context
    writer = tool._preferences()
    monkeypatch.setattr(tool, "_preferences", lambda: writer)
    from runtime_owner_loop import _load_writer

    _load_writer()
    import usefulness_feedback
    import personal_context
    import compose_brief
    import relevance
    import style_apply

    policy["_source_authority"] = lambda: {
        "complete": True,
        "revision": "c" * 64,
        "google_sources": ["gmail"],
        "bridge_sources": [],
        "connector_sources": [],
    }
    monkeypatch.setenv("SOTTO_DEPLOYMENT_MODE", "managed")
    monkeypatch.setenv("SOTTO_TENANT_ID", "synthetic-owner")
    config = data / "config/managed-capabilities.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        json.dumps(
            {"tenant_id": "synthetic-owner", "sources": {"gmail": {"consented": True, "connected": True}}}
        )
    )
    ref = datetime.now(timezone.utc).strftime("%Y-%m-%d") + "_morning"
    root = data / "briefs"
    root.mkdir()
    archive = root / (ref + ".json")
    text = "The school waiver is due tomorrow.\n\nThe donation drive is optional."
    archive.write_text(json.dumps({"brief_text": text, "_source_permissions": ["gmail"]}))
    marker = root / (ref.replace("_", ".") + ".delivered")
    marker.write_text("accepted-run")
    artifact = _seed_accepted(data, ref, text)

    def listing():
        return call(
            request({"operation": "usefulness-list", "source_revision": "c" * 64}, operation="read"), context
        )

    def rating(excerpt="", rating="useful", event="d" * 64):
        item = listing()["items"][0]
        return request(
            {
                "operation": "usefulness",
                "rating": rating,
                "reference": ref,
                "archive_version": item["archive_version"],
                "excerpt": excerpt,
                "source_revision": "c" * 64,
            },
            event=event,
            text="That school waiver was useful.",
        )

    return (
        writer,
        usefulness_feedback,
        personal_context,
        compose_brief,
        relevance,
        style_apply,
        listing,
        rating,
        archive,
        artifact,
        config,
    )


def test_actual_next_prompt_consumers_and_rating_replacement(feedback, context):
    writer, original, pc, cb, relevance, style, listing, rating, archive, _, _ = feedback
    listed = listing()
    assert listed["status"] == "usefulness_list" and len(listed["items"]) == 1
    first = "The school waiver is due tomorrow."
    second = "The donation drive is optional."
    one = call(rating(first), context)
    assert one["status"] == "committed"
    call(rating(second, "not_useful", "e" * 64), context)
    call(rating(second, "useful", "f" * 64), context)
    assert [(r["outcome"], r["item"]) for r in pc.feedback()] == [("useful", first), ("useful", second)]
    prompt = cb.build_prompt(cb._load_prompt(), {"type": "morning", "google": {"events": []}, "local": {}})
    assert first in prompt and "USER FEEDBACK ON SPECIFIC PAST OUTPUTS" in prompt
    assert first in relevance.judgment_prompt("Synthetic inbound school question")
    assert first in style.apply({"channel": "imessage"})["guidance"]
    outcomes = (archive.parent.parent / "outcomes.jsonl").read_text()
    assert first not in outcomes and second not in outcomes
    saved = writer.jsonstore.read(writer._path())["owner_preference_receipts"]
    assert first not in json.dumps(saved) and second not in json.dumps(saved)
    writer.add("vip_people", "Dana")
    assert writer.jsonstore.read(writer._path())["owner_preference_receipts"] == saved
    assert original.items()[0]["reference"] == archive.stem


@pytest.mark.parametrize("lost", ["artifact", "outbox", "consent", "provenance"])
def test_managed_downstream_revoke_or_archive_loss_withholds(feedback, context, lost):
    _, _, pc, cb, relevance, style, _, rating, archive, artifact, config = feedback
    body = rating()
    result = call(body, context)
    assert result["status"] == "committed"
    if lost == "artifact":
        artifact.unlink()
    elif lost == "outbox":
        (context[0] / "events/outbox.json").unlink()
    elif lost == "consent":
        config.write_text(
            json.dumps(
                {
                    "tenant_id": "synthetic-owner",
                    "sources": {"gmail": {"consented": False, "connected": True}},
                }
            )
        )
    else:
        value = json.loads(artifact.read_text())
        value.pop('_source_permissions')
        artifact.write_text(json.dumps(value))
    assert pc.feedback() == []
    assert "USER FEEDBACK ON SPECIFIC PAST OUTPUTS" not in cb.build_prompt(
        cb._load_prompt(), {"type": "morning", "google": {}, "local": {}}
    )
    assert "USER FEEDBACK ON SPECIFIC PAST OUTPUTS" not in relevance.judgment_prompt("Synthetic")
    assert "USER FEEDBACK ON SPECIFIC PAST OUTPUTS" not in style.apply({"channel": "imessage"})["guidance"]
    context[1]["_source_authority"] = lambda: {"complete": False}
    assert call(dict(body, operation="inspect"), context) == result


def test_archive_change_ambiguous_excerpt_and_missing_target_have_no_write(feedback, context):
    _, _, _, _, _, _, _, rating, archive, _, _ = feedback
    body = rating("The school waiver is due tomorrow.")
    _seed_accepted(context[0], archive.stem, "repeat repeat")
    assert call(body, context)["code"] == "target_changed"
    assert call(rating("repeat"), context)["code"] == "target_unavailable"
    assert not (context[0] / "outcomes.jsonl").exists()


def test_contacting_outcomes_sync_failure_never_reappends(feedback, context, monkeypatch):
    writer, original, _, _, _, _, _, rating, archive, _, _ = feedback
    body = rating("The school waiver is due tomorrow.")
    sync = writer._sync_existing_preferences

    def fail(path):
        if str(path) == original.log_outcome._path():
            raise OSError("synthetic outcomes sync fault")
        return sync(path)

    monkeypatch.setattr(writer, "_sync_existing_preferences", fail)
    assert call(body, context) == {"version": 1, "status": "unknown"}
    outcomes = archive.parent.parent / "outcomes.jsonl"
    before = outcomes.read_bytes()
    assert call(dict(body, operation="inspect"), context) == {"version": 1, "status": "unknown"}
    assert call(body, context) == {"version": 1, "status": "unknown"} and outcomes.read_bytes() == before


def test_strict_source_bookends_and_feedback_age(feedback, context):
    _, _, pc, _, _, _, listing, rating, archive, _, _ = feedback
    body = rating()
    context[1]["_source_authority"] = lambda: {
        "complete": True,
        "revision": "b" * 64,
        "google_sources": ["gmail"],
    }
    assert call(body, context)["code"] == "source_unavailable"
    assert listing()["code"] == "source_unavailable"
    context[1]["_source_authority"] = lambda: {
        "complete": True,
        "revision": "c" * 64,
        "google_sources": ["gmail"],
    }
    assert call(body, context)["status"] == "committed"
    assert pc.feedback(datetime.now(timezone.utc) + timedelta(days=43)) == []


def test_source_union_is_checked_on_the_accepted_artifact(feedback, context):
    _, _, _, _, _, _, listing, _, _, artifact, _ = feedback
    value = json.loads(artifact.read_text())
    value["_source_permissions"].append("granola")
    artifact.write_text(json.dumps(value))
    assert listing()["items"] == []
    assert not (context[0] / "outcomes.jsonl").exists()


def test_listing_size_refuses_whole_response_without_truncating(feedback):
    _, _, _, _, _, _, listing, _, archive, _, _ = feedback
    _seed_accepted(archive.parent.parent, archive.stem, "😀" * 100000)
    with pytest.raises(tool.UnavailableError, match="listing unavailable"):
        listing()
    assert not (archive.parent.parent / "outcomes.jsonl").exists()


def test_list_bounded_eight_and_no_write(feedback):
    _, _, _, _, _, _, listing, _, archive, _, _ = feedback
    day = datetime.now(timezone.utc)
    for offset in range(10):
        date = (day - timedelta(days=offset)).strftime("%Y-%m-%d")
        _seed_accepted(archive.parent.parent, date + "_evening", "Retained synthetic output", [])
    before = {p.name: p.read_bytes() for p in archive.parent.iterdir()}
    assert len(listing()["items"]) == 8
    assert {p.name: p.read_bytes() for p in archive.parent.iterdir()} == before
    assert not (archive.parent.parent / "preferences.json").exists()


def test_authority_change_during_selection_never_contacts(feedback, context):
    _, _, _, _, _, _, _, rating, archive, _, _ = feedback
    body = rating()
    reads = 0

    def authority():
        nonlocal reads
        reads += 1
        return {
            "complete": True,
            "revision": "c" * 64 if reads == 1 else "e" * 64,
            "google_sources": ["gmail"],
        }

    context[1]["_source_authority"] = authority
    assert call(body, context)["code"] == "source_unavailable"
    assert not (archive.parent.parent / "outcomes.jsonl").exists()


def test_captured_grant_cannot_override_original_managed_consent(feedback, context):
    _, original, _, _, _, _, listing, rating, archive, _, config = feedback
    body = rating()
    config.write_text(
        json.dumps(
            {"tenant_id": "synthetic-owner", "sources": {"gmail": {"consented": False, "connected": True}}}
        )
    )
    assert original.items(permitted=lambda source: True, versions=True) == []
    assert listing()["items"] == []
    assert call(body, context)["code"] == "target_unavailable"
    assert not (archive.parent.parent / "outcomes.jsonl").exists()


@pytest.mark.parametrize(
    "excerpt", [" ", " The school waiver is due tomorrow.", "The school waiver is due tomorrow. "]
)
def test_admitted_excerpt_is_canonical_and_replayable(feedback, context, excerpt):
    _, _, _, _, _, _, _, rating, archive, _, _ = feedback
    body = rating(excerpt)
    with pytest.raises(ValueError, match="target invalid"):
        call(body, context)
    assert not (archive.parent.parent / "outcomes.jsonl").exists()


def test_unconfirmed_original_result_stays_unknown(feedback, context, monkeypatch):
    _, original, _, _, _, _, _, rating, archive, _, _ = feedback
    body = rating()
    record = original.record

    def unconfirmed(*args, **kwargs):
        record(*args, **kwargs)
        return {"logged": False, "outcome": "useful"}

    monkeypatch.setattr(original, "record", unconfirmed)
    assert call(body, context) == {"version": 1, "status": "unknown"}
    outcomes = archive.parent.parent / "outcomes.jsonl"
    before = outcomes.read_bytes()
    assert call(dict(body, operation="inspect"), context) == {"version": 1, "status": "unknown"}
    assert outcomes.read_bytes() == before


@pytest.fixture
def authenticated_call(feedback, context, monkeypatch):
    import threading
    import urllib.request
    import urllib.error
    from http.server import ThreadingHTTPServer
    from test_receiver import rec
    from test_runtime_owner_loop_close import ROOT, IDENTITY, capability

    data, policy, activation = context
    for key, value in policy.items():
        if isinstance(value, str):
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("SOTTO_RUNTIME_PROFILE", "managed-photon-fixed-five-v1")
    monkeypatch.setenv("SOTTO_MCP_TOKEN", ROOT)
    monkeypatch.setenv("SOTTO_TENANT_ID", IDENTITY["tenant_id"])
    # Authority and original consent agree on this managed synthetic owner.
    config = data / "config/managed-capabilities.json"
    config.write_text(
        json.dumps(
            {"tenant_id": IDENTITY["tenant_id"], "sources": {"gmail": {"consented": True, "connected": True}}}
        )
    )
    monkeypatch.setattr(rec, "DATA", str(data))
    monkeypatch.setattr(rec, "RUNTIME_ACTIVATION", activation)
    monkeypatch.setattr(rec, "RUNTIME_OWNER_PREFERENCE_TOOL", tool)
    monkeypatch.setattr(rec, "_runtime_source_authority", policy["_source_authority"])
    server = ThreadingHTTPServer(("127.0.0.1", 0), rec.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def send(body):
        http = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/runtime/owner-preference-tool",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + capability.derive(ROOT, IDENTITY),
                "Content-Type": "application/json",
                "User-Agent": "OpenAI File Downloader, XaiImageApiFetch/1.0",
            },
        )
        try:
            response = urllib.request.urlopen(http, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    try:
        yield send
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("chars,expected", [(600000, 200), (1100000, 503)])
def test_actual_authenticated_handler_listing_response_bound(feedback, context, authenticated_call, chars, expected):
    _, _, _, _, _, _, _, _, archive, _, _ = feedback
    _seed_accepted(context[0], archive.stem, "x" * chars)
    body = request({"operation": "usefulness-list", "source_revision": "c" * 64}, operation="read")
    status, raw = authenticated_call(body)
    assert status == expected
    if expected == 200:
        assert 512 * 1024 < len(raw) < 1024 * 1024
        assert json.loads(raw)["items"][0]["text"] == "x" * chars
    else:
        assert "x" * 100 not in raw.decode()


@pytest.fixture
def notification_rating(context, monkeypatch):
    from test_outbox import _load_outbox, _transaction
    from test_runtime_owner_loop_close import WHEN
    import hashlib
    from types import SimpleNamespace

    data, policy, _ = context
    route = 'a' * 64
    source_revision = 'c' * 64
    dispatch = 'd' * 32
    decision = 'e' * 16
    source_id = 'private-original-gmail-message'
    text = 'The Cedar update is ready.'
    payload_hash = hashlib.sha256(json.dumps({'text': text}, separators=(',', ':')).encode()).hexdigest()
    grants = ['gmail']
    current_route = [route]
    policy['_source_authority'] = lambda: {'complete': True, 'revision': source_revision, 'google_sources': grants}
    policy['_retained_route_digest'] = lambda: current_route[0]
    box = _load_outbox('notification_usefulness_outbox')
    monkeypatch.setattr(box, 'time', SimpleNamespace(time=lambda: WHEN.timestamp() - 1))
    box.HOOKS.update(data_root=lambda: str(data), json_transaction=_transaction,
                     retained_proof_current=lambda _proof: True, local_today=lambda: WHEN.date().isoformat(),
                     on_delivered=lambda _payload: None, record=lambda *_, **__: None)
    box.HOOKS['send'] = lambda *_args, **_kwargs: (True, '', {
        'acceptance': 'accepted', 'message_id': 'delivered-provider-id', 'message_ids': ['delivered-provider-id'],
        'route_proof_version': 1, 'route_digest': route, 'payload_hash': payload_hash})
    payload = {'run_id': dispatch, 'label': 'event', 'body': text, 'target': 'photon', 'decision_ids': [decision],
               'notification_feedback_scope': {'source_revision': source_revision, 'route_digest': route},
               'effects': [{'kind': 'eligibility', 'notification_id': decision, 'source': 'gmail',
                            'request_reference': {'source': 'gmail', 'id': source_id}}]}
    assert box.deliver(payload)
    outbox = data / 'events/outbox.json'
    outbox.chmod(0o600)
    # Production owns the receipt callback; the test provides the real outbox against its isolated root.
    policy['_notification_feedback_receipt'] = box.delivery_proof().notification_feedback_receipt
    args = {'operation': 'notification-usefulness', 'rating': 'not_useful', 'dispatch_id': dispatch,
            'provider_message_id': 'delivered-provider-id', 'route_digest': route,
            'source_revision': source_revision, 'payload_hash': payload_hash}
    def rate(event, *, words='Not useful.', **changes):
        return call(request(args | changes, event=event, text=words), context)

    return rate, current_route, grants, outbox, route, decision, text, source_id


def test_exact_notification_rating_uses_one_outcome_without_muting_sender(notification_rating, context):
    rate, current_route, grants, outbox, route, decision, text, source_id = notification_rating
    data = context[0]
    assert rate('1' * 64, words='Useful.')['code'] == 'owner_span_unavailable'
    assert rate('2' * 64, dispatch_id='a' * 32)['code'] == 'target_unavailable'
    assert rate('3' * 64, provider_message_id='different-message')['code'] == 'target_unavailable'
    assert rate('4' * 64, payload_hash='f' * 64)['code'] == 'target_unavailable'
    assert rate('5' * 64, source_revision='f' * 64)['code'] == 'source_unavailable'
    authority = context[1]['_source_authority']
    context[1]['_source_authority'] = lambda: authority() | {'revision': 'f' * 64}
    assert rate('5' * 64, source_revision='f' * 64)['code'] == 'target_unavailable'
    context[1]['_source_authority'] = authority
    current_route[0] = 'f' * 64
    assert rate('6' * 64)['code'] == 'target_unavailable'
    current_route[0] = route
    grants.clear()
    assert rate('7' * 64)['code'] == 'source_unavailable'
    grants.append('gmail')
    accepted = outbox.read_text()
    for state in ('pending', 'superseded'):
        withdrawn = json.loads(accepted)
        withdrawn['rows'][0]['status'] = state
        outbox.write_text(json.dumps(withdrawn))
        assert rate('7' * 64)['code'] == 'target_unavailable'
    outbox.write_text(accepted)
    assert not (data / 'outcomes.jsonl').exists()

    recorded = rate('8' * 64)
    assert recorded['status'] == 'committed' and recorded['outcome']['rating'] == 'not_useful'
    assert rate('8' * 64) == recorded
    assert rate('9' * 64)['outcome'] == recorded['outcome']
    outcomes = (data / 'outcomes.jsonl').read_text()
    outcome, = map(json.loads, outcomes.splitlines())
    assert outcome['source'] == 'notification_feedback' and outcome['decision_id'] == decision
    assert all(value not in outcomes for value in (text, source_id, 'delivered-provider-id'))
    assert all(value not in outbox.read_text() for value in (text, source_id))
    from personal_context import conversation_filter
    from preferences import load_explicit
    urgent = {'source': 'gmail', 'from': 'Cedar <cedar@example.test>', 'id': 'urgent-unrelated-mail'}
    assert conversation_filter(load_explicit(), lookup={})(urgent)


@pytest.fixture
def brief_delivery(feedback, context, authenticated_call):
    from test_receiver import rec
    from test_outbox import _load_outbox, _transaction

    _, _, _, _, _, _, _, _, archive, seeded, _ = feedback
    data = context[0]
    day = archive.stem.split('_')[0]
    box = _load_outbox('brief_usefulness_outbox')
    box.HOOKS.update(data_root=lambda: str(data), json_transaction=_transaction,
                     local_today=lambda: day, brief_gate=lambda *_: box.GATE_SEND,
                     on_delivered=lambda _payload: None, record=lambda *_, **__: None)
    original = json.loads(archive.read_text())
    outbox = data / 'events/outbox.json'
    outbox.unlink()
    seeded.unlink()

    def stage(edition, value):
        ident = edition * 32
        artifact = data / f'events/work-inputs/brief-{edition}/artifact.json'
        artifact.parent.mkdir(parents=True)
        artifact.write_text(json.dumps(value | {'_delivery_run_ids': [ident], 'composed_at': time.time()}))
        return {'run_id': ident, 'label': 'cron:sotto-morning-brief',
                'body': rec._delivery_body(value['brief_text']), 'target': 'fictional-owner'}

    def http(body, expected=200):
        status, raw = authenticated_call(body)
        assert status == expected
        return json.loads(raw)

    def listed():
        return http(request({"operation": "usefulness-list", "source_revision": "c" * 64},
                            operation="read"))['items']

    a = stage('a', original)
    box._enqueue(a['run_id'], box.KIND_BRIEF, a, time.time(), day)
    assert listed() == []  # the first send is pending even though the day claim exists
    box.HOOKS['send'] = lambda *_: (True, '', {'acceptance': 'accepted', 'message_id': 'fictional-a'})
    assert box._attempt(a['run_id'])
    first, = listed()
    assert first['text'] == original['brief_text']
    return box, stage, http, listed, first, original, a['run_id']


def test_pending_replacement_cannot_inherit_accepted_brief(feedback, brief_delivery, context):
    _, _, pc, cb, _, _, _, rating, archive, _, _ = feedback
    box, stage, http, listed, first, original, original_run = brief_delivery
    outbox = context[0] / 'events/outbox.json'
    assert http(rating())['status'] == 'committed'
    cb._archive_brief({'brief_markdown': 'Replacement still pending.', '_source_permissions': ['gmail']},
                      'morning')
    assert json.loads(archive.read_text())['brief_markdown'] == 'Replacement still pending.'
    assert listed() == [first] and len(pc.feedback()) == 1
    failed = stage('c', original | {'brief_text': 'A terminally failed replacement.'})
    box.HOOKS['brief_gate'] = lambda *_: box.GATE_NOT_A_BRIEF
    assert not box.deliver(failed)
    assert next(row for row in json.loads(outbox.read_text())['rows']
                if row['id'] == failed['run_id'])['status'] == 'failed'
    assert listed() == [first]
    box.HOOKS['brief_gate'] = lambda *_: box.GATE_SEND
    # Even copying the accepted run ID into changed staged text cannot prove acceptance.
    rebound = stage('d', original | {'brief_text': 'Different staged text.'})
    forged = context[0] / 'events/work-inputs/brief-d/artifact.json'
    forged.write_text(forged.read_text().replace(rebound['run_id'], original_run))
    assert listed() == [first]



def _confirm_after_restart(box, payload, observe_unconfirmed):
    from test_outbox import _load_outbox, _transaction

    run = payload['run_id']
    box.HOOKS['send'] = lambda *_: (False, 'fictional rejection', {'acceptance': 'rejected'})
    assert not box._attempt(run)
    observe_unconfirmed()
    with _transaction(box.path(), {}) as doc:
        doc['rows'][-1]['next_at'] = 0
    box.HOOKS['send'] = lambda *_: (False, 'fictional lost response', {'acceptance': 'unknown'})
    assert not box._attempt(run)
    restarted = _load_outbox('brief_usefulness_restarted')
    restarted.HOOKS.update(box.HOOKS)
    with _transaction(box.path(), {}) as doc:
        doc['rows'][-1]['next_at'] = 0
    assert not restarted._attempt(run)
    observe_unconfirmed()

    restarted.HOOKS['gallery_receipt'] = lambda _: {'acceptance': 'accepted', 'message_id': 'fictional-b'}
    with _transaction(box.path(), {}) as doc:
        doc['rows'][-1]['next_at'] = 0
    assert restarted._attempt(run)


def test_replacement_needs_its_own_acceptance_and_rating(feedback, context, brief_delivery):
    _, _, pc, cb, _, _, _, rating, archive, _, config = feedback
    box, stage, http, listed, first, original, _ = brief_delivery
    data = context[0]
    day = archive.stem.split('_')[0]
    stale = rating(event='1' * 64)
    body = rating()
    assert http(body)['status'] == 'committed'
    assert http(body)['status'] == 'committed'  # replay of the same owner event
    excerpt = original['brief_text'].splitlines()[0]
    assert http(rating(excerpt, event='2' * 64))['status'] == 'committed'
    assert http(rating(excerpt, 'not_useful', '3' * 64))['status'] == 'committed'
    outcomes = data / 'outcomes.jsonl'
    old_rows = outcomes.read_text().splitlines()
    assert len(old_rows) == 3 and excerpt not in outcomes.read_text()
    assert all(json.loads(row)['archive_version'] == first['archive_version'] for row in old_rows)
    with outcomes.open('a') as handle:
        for row in old_rows[:2]:
            legacy = json.loads(row)
            legacy.pop('archive_version')
            legacy['outcome'] = 'not_useful'
            handle.write(json.dumps(legacy) + '\n')

    next_text = excerpt + '\n\nThe replacement has a new deadline.'
    cb._archive_brief({'brief_markdown': next_text, '_source_permissions': ['gmail']}, 'morning')
    assert json.loads(archive.read_text())['brief_markdown'] == next_text
    b = stage('b', original | {'brief_text': next_text + '\n<!--machine-note-->', '_composition_direction': True,
                               '_contextual_offer': {'question': 'Fictional offer?'}})
    box._enqueue(b['run_id'], box.KIND_BRIEF, b, time.time(), day)
    assert listed() == [first]
    assert [(row['outcome'], row.get('item')) for row in pc.feedback()] == [
        ('useful', None), ('not_useful', excerpt)]

    def observe_unconfirmed():
        assert listed() == [first] and len(pc.feedback()) == 2

    _confirm_after_restart(box, b, observe_unconfirmed)
    second, = listed()
    assert second['text'] == next_text and second['archive_version'] != first['archive_version']
    assert pc.feedback() == []  # even the verbatim item shared by both editions cannot inherit a rating
    assert http(stale, 409)['code'] == 'target_changed'
    assert http(rating(excerpt, event='4' * 64))['status'] == 'committed'
    assert [(row['outcome'], row['item']) for row in pc.feedback()] == [('useful', excerpt)]
    saved = [json.loads(row) for row in outcomes.read_text().splitlines()]
    assert saved[-1]['archive_version'] == second['archive_version']
    assert len(saved) == 6 and saved[:3] == list(map(json.loads, old_rows))
    grants = json.loads(config.read_text())
    grants['sources']['gmail']['consented'] = False
    config.write_text(json.dumps(grants))
    assert listed() == [] and pc.feedback() == []


def test_pre_digest_receipt_is_not_new_rating_proof(feedback, context):
    _, _, pc, _, _, _, listing, rating, _, _, _ = feedback
    submission = rating()
    outbox = context[0] / 'events/outbox.json'
    document = json.loads(outbox.read_text())
    document['rows'][0]['receipt'].pop('brief_body_sha256')
    outbox.write_text(json.dumps(document))
    assert listing()['items'] == [] and pc.feedback() == []
    assert call(submission, context)['code'] == 'target_unavailable'
    assert not (context[0] / 'outcomes.jsonl').exists()


@pytest.mark.parametrize('contacted', [None, True, 0, float('inf'), float('nan')])
def test_unproven_contact_order_cannot_supply_rating(feedback, context, contacted):
    _, _, pc, _, _, _, listing, rating, _, _, _ = feedback
    submission = rating()
    outbox = context[0] / 'events/outbox.json'
    document = json.loads(outbox.read_text())
    document['rows'][0]['contacted_at'] = contacted
    outbox.write_text(json.dumps(document))
    assert listing()['items'] == [] and pc.feedback() == []
    assert call(submission, context)['code'] == 'target_unavailable'
    assert not (context[0] / 'outcomes.jsonl').exists()


def test_older_confirmation_cannot_replace_newer_contacted_brief(feedback, context, brief_delivery, monkeypatch):
    from types import SimpleNamespace
    from test_receiver import rec
    from test_outbox import _transaction

    _, _, pc, _, _, _, _, rating, _, _, _ = feedback
    box, stage, http, listed, _, original, _ = brief_delivery
    clock = [time.time() + 10]
    monkeypatch.setattr(box, 'time', SimpleNamespace(time=lambda: clock[0]))
    box.HOOKS['brief_gate'] = rec._brief_marker_gate
    older = stage('c', original | {'brief_text': 'Earlier requested brief.'})
    newer = stage('d', original | {'brief_text': 'Latest requested brief.'})
    day = datetime.now(timezone.utc).date().isoformat()
    # Both owner requests can queue before either send becomes uncertain. The real
    # requested-brief send gate does not use the scheduled brief's daily claim.
    for payload in (older, newer):
        payload['label'] = f"requested:{payload['run_id']}:sotto-morning-brief"
        box._enqueue(payload['run_id'], box.KIND_BRIEF, payload, clock[0], day)
    box.HOOKS['send'] = lambda *_: (False, 'response lost', {'acceptance': 'unknown'})
    assert not box._attempt(older['run_id'])
    clock[0] += 10
    box.HOOKS['send'] = lambda *_: (True, '', {'acceptance': 'accepted', 'message_id': 'newer'})
    assert box._attempt(newer['run_id'])
    latest, = listed()
    assert latest['text'] == newer['body']
    assert http(rating())['status'] == 'committed'
    before = pc.feedback()
    assert len(before) == 1
    clock[0] += 10
    box.HOOKS['gallery_receipt'] = lambda _: {'acceptance': 'accepted', 'message_id': 'older'}
    with _transaction(box.path(), {}) as doc:
        next(row for row in doc['rows'] if row['id'] == older['run_id'])['next_at'] = 0
    assert box._attempt(older['run_id'])
    assert listed() == [latest]
    assert pc.feedback() == before
