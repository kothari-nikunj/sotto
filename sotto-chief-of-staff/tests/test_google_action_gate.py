"""The gate on real effects — "Sotto drafts, you send" as code, not as prompt text, and an approval
bound to the bytes it was given rather than to a prompt's word that one happened.

`hermes -z` (cron briefs, the proactive watcher, event triage) auto-bypasses approvals with Code
Execution on, so the ONLY thing that can stop an unattended send or calendar write is a refusal
inside google_action.py. These tests pin: the refusal (shape + exit 2 + no network), what stays
allowed unattended, the metadata-only receipt with a payload hash that proves either way, and
`--offer-bound` — which turns a cross-process "sure" into a match against the exact content offered.
"""
import importlib.util, json, os, subprocess, sys
import pytest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "google_action_gate", os.path.join(HERE, "..", "_shared", "scripts", "google_action.py"))
ga = importlib.util.module_from_spec(spec); spec.loader.exec_module(ga)

_po_spec = importlib.util.spec_from_file_location(
    "gate_pending_offer", os.path.join(HERE, "..", "_shared", "scripts", "pending_offer.py"))
po = importlib.util.module_from_spec(_po_spec); _po_spec.loader.exec_module(po)

BODY = "Confirming Thursday 3pm — see you at the Ferry Building."
SUBJECT = "Re: Thursday"


def _send_payload(body=BODY, to="alex@acme.com", subject=""):
    return ga._canonical({"to": to, "subject": subject, "body": body})


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    """Receipts land on a tmp volume, and the run is attended unless a test says otherwise."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_UNATTENDED", raising=False)
    return tmp_path


def _receipts(tmp_path) -> list:
    p = tmp_path / "events" / "sends.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _no_network(monkeypatch):
    """Fail the test if ANYTHING reaches for the wire: the CLI hop (`_run` / subprocess) or the
    direct Gmail client. The refusal must land before all of it."""
    monkeypatch.setattr(ga, "_run", lambda args: pytest.fail(f"network touched: {args}"))
    monkeypatch.setattr(ga.subprocess, "run", lambda *a, **k: pytest.fail("subprocess spawned"))
    monkeypatch.setattr(ga, "_gmail_service", lambda: pytest.fail("gmail client built"))


# ── Refused: the unattended lanes ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("argv, ident", [
    (["gmail-send", "--to", "alex@acme.com", "--subject", SUBJECT, "--body", BODY],
     {"to": "alex@acme.com"}),
    (["gmail-reply", "--message-id", "M1", "--body", BODY],
     {"message_id": "M1"}),
])
def test_unattended_send_is_refused_before_any_network(argv, ident, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SOTTO_UNATTENDED", "1")
    _no_network(monkeypatch)
    monkeypatch.setattr("sys.argv", ["google_action.py", *argv])

    with pytest.raises(SystemExit) as exc:
        ga.main()
    assert exc.value.code == 2                       # a hard wall, not a 0-with-an-error

    out = json.loads(capsys.readouterr().out)
    assert out == {"status": "error",
                   "error": "refused: unattended run — Sotto drafts, you send. Propose the reply in "
                            "the brief instead.",
                   "fallback": "propose_in_brief"}   # the one thing it may do instead

    receipt, = _receipts(tmp_path)
    assert {k: receipt[k] for k in ident} == ident
    assert receipt["verb"] == argv[0]
    assert receipt["unattended"] is True and receipt["result"] == "refused"
    assert receipt["ts"].endswith("Z")
    expected_payload = (_send_payload(subject=SUBJECT) if argv[0] == 'gmail-send'
                        else ga._canonical({'message_id': 'M1', 'body': BODY}))
    assert receipt["payload_sha256"] == po.payload_hash(expected_payload.encode("utf-8"))


def test_any_non_empty_value_means_unattended(monkeypatch, capsys):
    """Fail closed: the receiver sets `1`, but `true`/`0`/whitespace must not read as attended."""
    for value in ("1", "true", "0", " ", "yes"):
        monkeypatch.setenv("SOTTO_UNATTENDED", value)
        assert ga._unattended() is True, value
    monkeypatch.setenv("SOTTO_UNATTENDED", "")
    assert ga._unattended() is False


CAL_ARGV = {
    "calendar-create": ["calendar-create", "--summary", "Sync",
                        "--start", "2026-08-13T14:00:00-07:00",
                        "--end", "2026-08-13T14:30:00-07:00"],
    "calendar-delete": ["calendar-delete", "--event-id", "E1"],
    "calendar-rsvp": ["calendar-rsvp", "--event-id", "E1", "--response", "declined"],
}


@pytest.mark.parametrize("verb", sorted(CAL_ARGV))
def test_unattended_calendar_write_is_refused_before_any_network(verb, monkeypatch, capsys, tmp_path):
    """A calendar write is a real effect on other people's days — an invite lands in their inbox, a
    decline notifies the organizer, a delete removes an event they hold. The policy always said a
    calendar action happens only when the user asks for it in that conversation; a scheduled run has
    no such conversation, so the rule is enforced here rather than asked for in a prompt."""
    monkeypatch.setenv("SOTTO_UNATTENDED", "1")
    monkeypatch.delenv("SOTTO_USER_EMAIL", raising=False)
    _no_network(monkeypatch)
    monkeypatch.setattr("sys.argv", ["google_action.py", *CAL_ARGV[verb]])

    with pytest.raises(SystemExit) as exc:
        ga.main()
    assert exc.value.code == 2

    out = json.loads(capsys.readouterr().out)
    assert out["error"].startswith("refused: unattended run")
    assert out["fallback"] == "propose_in_brief"     # the one thing it may do instead

    receipt, = _receipts(tmp_path)
    assert receipt["verb"] == verb
    assert receipt["unattended"] is True and receipt["result"] == "refused"
    assert len(receipt["payload_sha256"]) == 64      # refused attempts name their bytes too


# ── Allowed: drafts are not a real effect ────────────────────────────────────────────────────────

def test_unattended_gmail_draft_is_refused_before_any_network(monkeypatch, capsys, tmp_path):
    """approval-tiers.md: no cron, proactive tick or scheduled run may create a draft — a drafts
    folder filling itself while the user sleeps is busywork theater. That rule was prose-only (this
    test's predecessor pinned the opposite, external review Aug 31); now it is the same wall every
    other account write hits."""
    monkeypatch.setenv("SOTTO_UNATTENDED", "1")
    _no_network(monkeypatch)
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-draft",
                                     "--to", "alex@acme.com", "--subject", SUBJECT, "--body", BODY])
    with pytest.raises(SystemExit) as exc:
        ga.main()
    assert exc.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["fallback"] == "propose_in_brief" and "draft" in out["error"]
    receipt, = _receipts(tmp_path)
    assert receipt["verb"] == "gmail-draft" and receipt["result"] == "refused"


def test_attended_gmail_draft_still_proceeds(monkeypatch, capsys, tmp_path):
    """The conversation is the draft's approval — attended, the verb works exactly as before (and
    now leaves a receipt)."""
    monkeypatch.delenv("SOTTO_UNATTENDED", raising=False)
    cap = {}

    class _Exec:
        def execute(self): return {"id": "D1", "message": {"threadId": "T7"}}

    class _Drafts:
        def create(self, userId=None, body=None):
            cap["draft"] = body
            return _Exec()

    class _Users:
        def drafts(self): return _Drafts()

    class _Gmail:
        def users(self): return _Users()

    monkeypatch.setattr(ga, "_gmail_service", lambda: _Gmail())
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-draft",
                                     "--to", "alex@acme.com", "--subject", SUBJECT, "--body", BODY])
    ga.main()                                        # no SystemExit
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "drafted" and out["draft_id"] == "D1"
    assert cap["draft"]["message"]["raw"]            # the draft really was built
    receipt, = _receipts(tmp_path)                   # every account write leaves a receipt now
    assert receipt["verb"] == "gmail-draft" and receipt["result"] == "drafted"
    assert receipt["unattended"] is False and receipt["payload_sha256"]


def test_attended_calendar_write_proceeds_and_is_receipted(monkeypatch, capsys, tmp_path):
    """The chat lane is unchanged — and a calendar write now leaves the same proof a send does."""
    monkeypatch.delenv("SOTTO_USER_EMAIL", raising=False)
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "created", "id": "E1"})
    monkeypatch.setattr("sys.argv", ["google_action.py", *CAL_ARGV["calendar-create"],
                                     "--attendees", "alex@acme.com,dana@acme.com"])
    ga.main()
    assert json.loads(capsys.readouterr().out)["status"] == "created"

    receipt, = _receipts(tmp_path)
    assert receipt["verb"] == "calendar-create" and receipt["result"] == "written"
    assert receipt["attendee_count"] == 2            # a count, never the guest list
    assert receipt["unattended"] is False and receipt["offer_bound"] is False
    assert receipt["payload_sha256"] == ga._payload_hash(
        ga._event_payload("Sync", "2026-08-13T14:00:00-07:00", "2026-08-13T14:30:00-07:00",
                          "alex@acme.com,dana@acme.com", "", ""))


def test_a_reordered_guest_list_is_the_same_invite_but_an_added_guest_is_not(monkeypatch):
    """The hash has to survive how the caller happened to assemble the invite, or every binding
    fails for a reason that has nothing to do with the user's approval."""
    same = ga._event_payload("Sync", "S", "E", "Dana@Acme.com, alex@acme.com", "", "")
    assert same == ga._event_payload("Sync", "S", "E", "alex@acme.com,dana@acme.com", "", "")
    assert same != ga._event_payload("Sync", "S", "E", "alex@acme.com,dana@acme.com,eve@acme.com",
                                     "", "")


def test_an_rsvp_binds_to_the_response_not_just_the_event(monkeypatch):
    """An offer to decline must never bind an accept — the response is half the act."""
    decline = ga._canonical({"event_id": "E1", "response": "declined", "comment": ""})
    assert decline != ga._canonical({"event_id": "E1", "response": "accepted", "comment": ""})


def test_unattended_read_verb_is_untouched(monkeypatch):
    """`_run` is the shared read/write CLI hop — the gate lives at the verb, not in the plumbing,
    so a calendar get (and every other read) is unchanged."""
    monkeypatch.setenv("SOTTO_UNATTENDED", "1")
    monkeypatch.setattr(ga, "_run", lambda args: {"id": "EV1", "summary": "Board Dinner",
                                                  "attendees": [{"email": "me@x.com", "self": True}],
                                                  "start": {"dateTime": "2026-08-13T18:00:00-07:00"}}
                        if args[1] == "get" else {"status": "patched"})
    assert ga._rsvp("EV1", "accepted", "primary", "")["status"] == "rsvped"


# ── Attended: the chat lane is unchanged, and it leaves a receipt too ─────────────────────────────

def test_attended_send_proceeds_and_records_sent(monkeypatch, capsys, tmp_path):
    cap = {}
    monkeypatch.setattr(ga, "_run", lambda args: cap.__setitem__("args", args)
                        or {"status": "sent", "id": "m1", "threadId": "T1"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-send", "--to", "alex@acme.com",
                                     "--subject", SUBJECT, "--body", BODY])
    ga.main()                                        # no SystemExit
    assert cap["args"] == ["gmail", "send", "--to", "alex@acme.com", "--body", BODY,
                           "--subject", SUBJECT]
    assert json.loads(capsys.readouterr().out)["status"] == "sent"

    receipt, = _receipts(tmp_path)
    assert receipt == {"ts": receipt["ts"], "verb": "gmail-send", "to": "alex@acme.com",
                       "unattended": False, "result": "sent",
                       "payload_sha256": po.payload_hash(_send_payload(subject=SUBJECT).encode("utf-8")),
                       "offer_bound": False}


def test_attended_reply_failure_records_error(monkeypatch, capsys, tmp_path):
    """The receipt records the ATTEMPT and its outcome — a failed send is not a silent one."""
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "error", "error": "403 insufficient scope"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-reply", "--message-id", "M9",
                                     "--body", BODY])
    ga.main()
    assert json.loads(capsys.readouterr().out)["status"] == "error"
    receipt, = _receipts(tmp_path)
    assert receipt["verb"] == "gmail-reply" and receipt["message_id"] == "M9"
    assert receipt["unattended"] is False and receipt["result"] == "error"


# ── The receipt is metadata, never content ───────────────────────────────────────────────────────

def test_receipt_never_carries_subject_or_body(monkeypatch, tmp_path):
    """The ledger proves an outbound act happened; it is not a copy of the mail. A receipt that
    quoted the body would put every sent email on the /data volume in plaintext, forever."""
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "sent"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-send", "--to", "alex@acme.com",
                                     "--subject", SUBJECT, "--body", BODY])
    ga.main()
    monkeypatch.setenv("SOTTO_UNATTENDED", "1")
    monkeypatch.setattr(ga, "_run", lambda args: pytest.fail("network touched"))
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-reply", "--message-id", "M1",
                                     "--body", BODY])
    with pytest.raises(SystemExit):
        ga.main()

    raw = (tmp_path / "events" / "sends.jsonl").read_text(encoding="utf-8")
    assert BODY not in raw and "Ferry Building" not in raw
    assert SUBJECT not in raw and "subject" not in raw
    assert set(json.loads(raw.splitlines()[0])) == {"ts", "verb", "to", "unattended", "result",
                                                    "payload_sha256", "offer_bound"}


def test_the_payload_hash_names_the_bytes_without_keeping_them(monkeypatch, tmp_path):
    """The hash is what makes a receipt checkable: you can prove after the fact WHICH text was sent
    against what the user was shown, and the ledger still holds not one readable word of it."""
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "sent"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-send", "--to", "alex@acme.com",
                                     "--subject", SUBJECT, "--body", BODY])
    ga.main()
    receipt, = _receipts(tmp_path)
    assert receipt["payload_sha256"] == po.payload_hash(_send_payload(subject=SUBJECT).encode("utf-8"))
    assert receipt["payload_sha256"] != po.payload_hash((BODY + " ").encode("utf-8"))


# ── --offer-bound: the yes binds to the bytes, across processes ──────────────────────────────────

OFFER_Q = "Want me to send that confirmation?"


def _bind(tmp_path, monkeypatch, payload: str, action="gmail-send"):
    """The offering lane, three processes ago: one fresh offer carrying only the payload's hash."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(ga, "_pending_offer", lambda: po)
    return po.set_offer("commitment", OFFER_Q, person="Alex",
                        payload_sha256=po.payload_hash(payload.encode("utf-8")), action=action)


def _offer_file(tmp_path):
    return tmp_path / "proactive" / "pending_offer.json"


def test_offer_bound_send_matches_then_sends_and_clears(monkeypatch, capsys, tmp_path):
    offer = _bind(tmp_path, monkeypatch, _send_payload())
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "sent", "id": "m1"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-send", "--to", "alex@acme.com",
                                     "--body", BODY, "--offer-bound", "--offer-id", offer['offer_id']])
    ga.main()                                        # no SystemExit
    assert json.loads(capsys.readouterr().out)["status"] == "sent"

    started, receipt = _receipts(tmp_path)
    assert started['result'] == 'started' and started['offer_id'] == offer['offer_id']
    assert receipt["result"] == "sent" and receipt["offer_bound"] is True
    assert not _offer_file(tmp_path).exists()        # the yes is spent, exactly once


def test_offer_id_selects_exact_action_from_ambiguous_current_state(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(ga, "_pending_offer", lambda: po)
    digest = po.payload_hash(_send_payload().encode())
    first = po.set_offer("commitment", OFFER_Q, payload_sha256=digest, action="gmail-send")
    second = po.set_offer("commitment", "Delete the event?", payload_sha256=digest,
                          action="calendar-delete")
    first.update(offer_id="offer-send", message_id="m1")
    second.update(offer_id="offer-delete", message_id="m2")
    _offer_file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _offer_file(tmp_path).write_text(json.dumps({"offers": [first, second], "accepted": {}}))
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "sent"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-send", "--to", "alex@acme.com",
                                     "--body", BODY, "--offer-bound", "--offer-id", "offer-send"])
    ga.main()
    assert json.loads(capsys.readouterr().out)["status"] == "sent"
    assert po.get_offer(offer_id="offer-delete")["action"] == "calendar-delete"


def test_offer_id_refuses_offer_for_another_action(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(ga, "_pending_offer", lambda: po)
    offer = po.set_offer("commitment", OFFER_Q,
                         payload_sha256=po.payload_hash(BODY.encode()), action="calendar-delete")
    offer["offer_id"] = "wrong-action"
    _offer_file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _offer_file(tmp_path).write_text(json.dumps({"offers": [offer], "accepted": {}}))
    _no_network(monkeypatch)
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-send", "--to", "alex@acme.com",
                                     "--body", BODY, "--offer-bound", "--offer-id", "wrong-action"])
    with pytest.raises(SystemExit) as exc:
        ga.main()
    assert exc.value.code == 2
    assert "does not authorize this action" in json.loads(capsys.readouterr().out)["error"]


def test_offer_bound_refuses_a_mutated_payload_and_keeps_the_yes(monkeypatch, capsys, tmp_path):
    """Approve-then-mutate is the whole reason this exists: the user said yes to one sentence and
    the acting session assembled another. The offer is NOT cleared — their yes wasn't consumed by
    a send they never saw."""
    offer = _bind(tmp_path, monkeypatch, _send_payload())
    _no_network(monkeypatch)
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-send", "--to", "alex@acme.com",
                                     "--body", BODY + " Also, wire the deposit today.",
                                     "--offer-bound", "--offer-id", offer['offer_id']])
    with pytest.raises(SystemExit) as exc:
        ga.main()
    assert exc.value.code == 2

    out = json.loads(capsys.readouterr().out)
    assert out["error"] == ("refused: payload does not match what the user approved — the action "
                            "changed after the offer")
    assert out["fallback"] == "re_offer"

    receipt, = _receipts(tmp_path)
    assert receipt["result"] == "refused" and receipt["offer_bound"] is True
    assert _offer_file(tmp_path).exists()            # unspent: the question still stands


def test_offer_bound_refuses_recipient_or_subject_substitution(monkeypatch, capsys, tmp_path):
    offer = _bind(tmp_path, monkeypatch, _send_payload(subject='Approved subject'))
    _no_network(monkeypatch)
    monkeypatch.setattr('sys.argv', ['google_action.py', 'gmail-send', '--to', 'mallory@example.com',
                                    '--subject', 'Changed subject', '--body', BODY, '--offer-bound',
                                    '--offer-id', offer['offer_id']])
    with pytest.raises(SystemExit):
        ga.main()
    assert 'action changed' in json.loads(capsys.readouterr().out)['error']
    assert po.get_offer(offer_id=offer['offer_id'])  # validation failed before claim


def test_print_payload_is_the_exact_offer_writer_contract(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['google_action.py', 'gmail-send', '--to', 'alex@acme.com',
                                    '--subject', SUBJECT, '--body', BODY, '--print-payload'])
    ga.main()
    assert capsys.readouterr().out == _send_payload(subject=SUBJECT)


def test_cli_preview_file_offer_and_fake_effect_round_trip(tmp_path, monkeypatch):
    google = os.path.join(HERE, '..', '_shared', 'scripts', 'google_action.py')
    pending = os.path.join(HERE, '..', '_shared', 'scripts', 'pending_offer.py')
    env = {**os.environ, 'SOTTO_DATA': str(tmp_path)}
    preview = subprocess.run([sys.executable, google, 'gmail-send', '--to', 'alex@acme.com',
                              '--subject', SUBJECT, '--body', BODY, '--print-payload'],
                             check=True, capture_output=True, env=env).stdout
    assert not preview.endswith(b'\n')
    payload_file = tmp_path / 'payload.json'
    payload_file.write_bytes(preview)
    offered = subprocess.run([sys.executable, pending, 'set', '--kind', 'commitment',
                              '--question', OFFER_Q, '--action', 'gmail-send',
                              '--payload-file', str(payload_file)], check=True, capture_output=True,
                             text=True, env=env)
    offer = json.loads(offered.stdout)
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(ga, '_pending_offer', lambda: po)
    effects = []
    out, refused = ga._gated('gmail-send', {'to': 'alex@acme.com'}, preview.decode(),
                              lambda: effects.append(1) or {'status': 'sent'}, True,
                              offer['offer_id'])
    assert not refused and out['status'] == 'sent' and effects == [1]


def test_calendar_rsvp_payload_includes_calendar():
    from types import SimpleNamespace
    primary = ga._action_payload(SimpleNamespace(cmd='calendar-rsvp', event_id='e1', response='accepted',
                                                  calendar='primary', comment='', offer_bound=False,
                                                  offer_id='', print_payload=False))
    other = ga._action_payload(SimpleNamespace(cmd='calendar-rsvp', event_id='e1', response='accepted',
                                                calendar='other@example.com', comment='', offer_bound=False,
                                                offer_id='', print_payload=False))
    assert primary != other and 'other@example.com' in other


def test_two_concurrent_claimants_execute_only_one_effect(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(ga, '_pending_offer', lambda: po)
    payload = _send_payload()
    offer = _bind(tmp_path, monkeypatch, payload)
    effects = []
    def attempt():
        return ga._gated('gmail-send', {'to': 'alex@acme.com'}, payload,
                         lambda: effects.append(1) or {'status': 'sent'}, True, offer['offer_id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert effects == [1]
    assert sorted(refused for _, refused in results) == [False, True]


@pytest.mark.parametrize("prepare, reason", [
    (lambda tmp, mp: None,
     "selected offer is absent, expired, already claimed, or ambiguous"),
    (lambda tmp, mp: po.set_offer("commitment", OFFER_Q, ttl_min=-1,
                                  payload_sha256=po.payload_hash(_send_payload().encode()), action='gmail-send'),
     "selected offer is absent, expired, already claimed, or ambiguous"),
    (lambda tmp, mp: po.set_offer("meeting_prep", "want me to pull prep?", action='gmail-send'),
     "the offer carried no payload to bind to — re-offer with the content in view"),
])
def test_offer_bound_refuses_without_a_matching_offer(prepare, reason, monkeypatch, capsys, tmp_path):
    """Absent, expired, or an offer that never carried bytes — none of them is an authorization."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(ga, "_pending_offer", lambda: po)
    offer = prepare(tmp_path, monkeypatch)
    _no_network(monkeypatch)
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-send", "--to", "alex@acme.com",
                                     "--body", BODY, "--offer-bound", "--offer-id",
                                     offer['offer_id'] if offer else 'missing'])
    with pytest.raises(SystemExit) as exc:
        ga.main()
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == f"refused: {reason}"
    assert _receipts(tmp_path)[0]["result"] == "refused"


def test_an_unbound_send_is_unchanged_by_any_of_this(monkeypatch, capsys, tmp_path):
    """In-session approval doesn't go through the offer store, and must not start needing one — the
    binding is opt-in per invocation, exactly where a yes crossed a process boundary."""
    _bind(tmp_path, monkeypatch, "something else entirely")
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "sent"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-send", "--to", "alex@acme.com",
                                     "--body", BODY])
    ga.main()
    assert json.loads(capsys.readouterr().out)["status"] == "sent"
    assert _receipts(tmp_path)[0]["offer_bound"] is False
    assert _offer_file(tmp_path).exists()            # an unbound send never spends someone's yes


def test_offer_bound_calendar_write_binds_to_the_whole_event(monkeypatch, capsys, tmp_path):
    """Every real-effect verb takes the flag, not just the two gmail ones."""
    monkeypatch.delenv("SOTTO_USER_EMAIL", raising=False)
    payload = ga._event_payload("Sync", "2026-08-13T14:00:00-07:00", "2026-08-13T14:30:00-07:00",
                                "", "", "")
    offer = _bind(tmp_path, monkeypatch, payload, action="calendar-create")
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "created", "id": "E1"})
    monkeypatch.setattr("sys.argv", ["google_action.py", *CAL_ARGV["calendar-create"],
                                     "--offer-bound", "--offer-id", offer['offer_id']])
    ga.main()
    assert json.loads(capsys.readouterr().out)["status"] == "created"
    assert _receipts(tmp_path)[0]["offer_bound"] is True
    assert not _offer_file(tmp_path).exists()


def test_a_failed_bound_send_spends_the_offer(monkeypatch, capsys, tmp_path):
    """Once an effect begins, an error may be uncertain; retry requires a fresh approval."""
    offer = _bind(tmp_path, monkeypatch, _send_payload())
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "error", "error": "503"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-send", "--to", "alex@acme.com",
                                     "--body", BODY, "--offer-bound", "--offer-id", offer['offer_id']])
    ga.main()
    receipts = _receipts(tmp_path)
    assert [row['result'] for row in receipts] == ['started', 'error']
    assert receipts[0]['offer_id'] == receipts[1]['offer_id'] == offer['offer_id']
    assert not _offer_file(tmp_path).exists()


def test_unattended_beats_the_binding(monkeypatch, capsys, tmp_path):
    """A matching offer is not a conversation: the unattended wall is checked first, and the offer
    is left on file rather than spent by a run that may not send."""
    _bind(tmp_path, monkeypatch, BODY)
    monkeypatch.setenv("SOTTO_UNATTENDED", "1")
    _no_network(monkeypatch)
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-send", "--to", "alex@acme.com",
                                     "--body", BODY, "--offer-bound"])
    with pytest.raises(SystemExit) as exc:
        ga.main()
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["fallback"] == "propose_in_brief"
    assert _offer_file(tmp_path).exists()


# ── The claim and its limit are written where they are made ──────────────────────────────────────

def test_the_gate_and_its_honest_boundary_are_documented(tmp_path):
    """Overclaiming a security property is worse than the gap it hides. Both surfaces that describe
    the binding must also say where it stops holding — an agent that computes both sides of a check
    has not been checked."""
    for rel in (("..", "_shared", "references", "approval-tiers.md"),
                ("..", "..", "docs", "HOW-SOTTO-DECIDES.md")):
        with open(os.path.join(HERE, *rel), encoding="utf-8") as f:
            text = f.read()
        assert "payload_sha256" in text or "hash of the exact" in text, rel
        assert "prevention" in text or "does not pretend to prevent" in text, rel

    with open(os.path.join(HERE, "..", "_shared", "references", "approval-tiers.md"),
              encoding="utf-8") as f:
        tiers = f.read()
    for verb in sorted(ga.REAL_EFFECT):
        assert verb in tiers, f"{verb} is gated in code but not named in the tiers contract"


def test_an_unwritable_volume_never_blocks_the_verb(monkeypatch, capsys):
    """Best-effort: the receipt is observability. Losing it must not change what the user's send
    returns — and must not turn a refusal into a pass."""
    monkeypatch.setenv("SOTTO_DATA", "/proc/nope/nowhere")
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "sent"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-send", "--to", "a@b.com",
                                     "--body", BODY])
    ga.main()
    assert json.loads(capsys.readouterr().out)["status"] == "sent"

    monkeypatch.setenv("SOTTO_UNATTENDED", "1")
    monkeypatch.setattr(ga, "_run", lambda args: pytest.fail("network touched"))
    with pytest.raises(SystemExit) as exc:
        ga.main()
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["fallback"] == "propose_in_brief"
