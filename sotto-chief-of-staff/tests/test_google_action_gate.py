"""The send gate — "Sotto drafts, you send" as code, not as prompt text.

`hermes -z` (cron briefs, the proactive watcher, event triage) auto-bypasses approvals with Code
Execution on, so the ONLY thing that can stop an unattended outbound email is a refusal inside
google_action.py. These tests pin: the refusal (shape + exit 2 + no network), what stays allowed
unattended, and the metadata-only receipt that proves either way.
"""
import importlib.util, json, os
import pytest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "google_action_gate", os.path.join(HERE, "..", "_shared", "scripts", "google_action.py"))
ga = importlib.util.module_from_spec(spec); spec.loader.exec_module(ga)

BODY = "Confirming Thursday 3pm — see you at the Ferry Building."
SUBJECT = "Re: Thursday"


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
                   "error": "refused: unattended run — Sotto drafts, you send. Use gmail-draft instead.",
                   "fallback": "gmail-draft"}        # the one thing it may do instead

    receipt, = _receipts(tmp_path)
    assert {k: receipt[k] for k in ident} == ident
    assert receipt["verb"] == argv[0]
    assert receipt["unattended"] is True and receipt["result"] == "refused"
    assert receipt["ts"].endswith("Z")


def test_any_non_empty_value_means_unattended(monkeypatch, capsys):
    """Fail closed: the receiver sets `1`, but `true`/`0`/whitespace must not read as attended."""
    for value in ("1", "true", "0", " ", "yes"):
        monkeypatch.setenv("SOTTO_UNATTENDED", value)
        assert ga._unattended() is True, value
    monkeypatch.setenv("SOTTO_UNATTENDED", "")
    assert ga._unattended() is False


# ── Allowed: drafts and calendar are not outbound email ──────────────────────────────────────────

def test_unattended_gmail_draft_still_proceeds(monkeypatch, capsys, tmp_path):
    """A draft lands in the user's OWN drafts folder — it can never leave without them pressing
    send, so the gate has nothing to stop. It is also the refusal's prescribed fallback: gating it
    would leave an unattended run with nowhere to put the text."""
    monkeypatch.setenv("SOTTO_UNATTENDED", "1")
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
    assert _receipts(tmp_path) == []                 # a draft is not a send — no send receipt


def test_unattended_calendar_create_still_proceeds(monkeypatch, capsys, tmp_path):
    """An invite the user approved in chat is documented behavior, and a calendar write is not
    outbound email — the gate is scoped to the two verbs that put mail in someone else's inbox."""
    monkeypatch.setenv("SOTTO_UNATTENDED", "1")
    monkeypatch.delenv("SOTTO_USER_EMAIL", raising=False)
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "created", "id": "E1"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "calendar-create", "--summary", "Sync",
                                     "--start", "2026-08-13T14:00:00-07:00",
                                     "--end", "2026-08-13T14:30:00-07:00"])
    ga.main()
    assert json.loads(capsys.readouterr().out)["status"] == "created"
    assert _receipts(tmp_path) == []


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
                       "unattended": False, "result": "sent"}


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
    assert set(json.loads(raw.splitlines()[0])) == {"ts", "verb", "to", "unattended", "result"}


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
    assert json.loads(capsys.readouterr().out)["fallback"] == "gmail-draft"
