"""The retention sweep: each policy ages the right thing, spares the young, and can never reach the
memory.

The last test here is the important one. Every other assertion is about a number; that one is about
the doctrine — a fixture tree seeded with the graph, the continuity ledger and the golden corpus,
swept, and asserted byte-for-byte unchanged. If a future table entry ever reaches one of those, this
is where it stops."""
import importlib.util
import json
import os
import time

import pytest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("retention", os.path.join(HERE, "retention.py"))
ret = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ret)

DAY = 86400.0
NOW = time.time()


@pytest.fixture(autouse=True)
def _volume(tmp_path, monkeypatch):
    monkeypatch.setitem(ret.HOOKS, "data_root", lambda: str(tmp_path))
    return tmp_path


def _write(root, rel, text="", age_days=0.0):
    path = os.path.join(str(root), *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if age_days:
        stamp = NOW - age_days * DAY
        os.utime(path, (stamp, stamp))
    return path


def _line(days_ago: float, **extra) -> str:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - days_ago * DAY))
    return json.dumps({"ts": ts, **extra}) + "\n"


# ── delete-older-than-N-days ─────────────────────────────────────────────────────────────────────

def test_a_dated_file_ages_by_the_date_in_its_name(_volume):
    """The marker's own age claim wins over mtime: a `.delivered` some later process touched is
    still the marker for ITS day."""
    old = time.strftime("%Y-%m-%d", time.gmtime(NOW - 200 * DAY))
    young = time.strftime("%Y-%m-%d", time.gmtime(NOW - 2 * DAY))
    stale = _write(_volume, f"briefs/{old}.morning.delivered", "run-1")
    fresh = _write(_volume, f"briefs/{young}.morning.delivered", "run-2")
    os.utime(stale, (NOW, NOW))          # touched today; still 200 days old by its name

    ret.sweep(now=NOW)

    assert not os.path.exists(stale)
    assert os.path.exists(fresh)


def test_staged_payloads_go_a_week_after_the_brief_that_read_them(_volume):
    old = time.strftime("%Y-%m-%d", time.gmtime(NOW - (ret.STAGED_DAYS + 1) * DAY))
    young = time.strftime("%Y-%m-%d", time.gmtime(NOW - (ret.STAGED_DAYS - 1) * DAY))
    gone = _write(_volume, f"briefs/{old}.morning_ready.payload.json", "{}")
    kept = _write(_volume, f"briefs/{young}.morning_ready.payload.json", "{}")

    ret.sweep(now=NOW)

    assert not os.path.exists(gone) and os.path.exists(kept)


def test_a_brief_and_its_markers_outlive_the_payload(_volume):
    """The payload is a staged intermediate; the brief is the archive. Between STAGED_DAYS and
    BRIEF_ARCHIVE_DAYS exactly one of them survives."""
    day = time.strftime("%Y-%m-%d", time.gmtime(NOW - 30 * DAY))
    payload = _write(_volume, f"briefs/{day}.morning_ready.payload.json", "{}")
    brief = _write(_volume, f"briefs/{day}_morning.json", "{}")
    claim = _write(_volume, f"briefs/{day}.morning.claim", "")

    ret.sweep(now=NOW)

    assert not os.path.exists(payload)
    assert os.path.exists(brief) and os.path.exists(claim)


def test_undated_staging_ages_by_mtime(_volume):
    """`events/delivery-effects-<run>.json` carries no date, so mtime is the only claim there is."""
    gone = _write(_volume, "events/delivery-effects-abc.json", "[]", age_days=ret.STAGED_DAYS + 1)
    kept = _write(_volume, "events/delivery-effects-def.json", "[]", age_days=1)

    ret.sweep(now=NOW)

    assert not os.path.exists(gone) and os.path.exists(kept)


def test_the_pending_offer_is_not_a_dated_proactive_stamp(_volume):
    """`proactive/` holds both the dated dedup stamps and the one standing offer. The rule's glob is
    date-shaped precisely so the offer is never in range."""
    old = time.strftime("%Y-%m-%d", time.gmtime(NOW - 90 * DAY))
    stamp = _write(_volume, f"proactive/{old}.json", "{}")
    offer = _write(_volume, "proactive/pending_offer.json", "{}", age_days=90)

    ret.sweep(now=NOW)

    assert not os.path.exists(stamp)
    assert os.path.exists(offer)


# ── drop-jsonl-lines-older-than-N-days ───────────────────────────────────────────────────────────

def test_a_ledger_keeps_its_young_lines_and_drops_the_old(_volume):
    path = _write(_volume, "events/delivery.jsonl",
                  _line(400, label="ancient") + _line(1, label="yesterday")
                  + _line(ret.DELIVERY_RECEIPT_DAYS + 5, label="last-quarter")
                  + _line(0, label="today"))

    out = ret.sweep(now=NOW)

    with open(path, encoding="utf-8") as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    assert [r["label"] for r in rows] == ["yesterday", "today"]
    assert any(e["path"] == "events/delivery.jsonl" and e["amount"] == 2 for e in out["swept"])


def test_the_send_receipts_are_kept_twice_as_long_as_the_delivery_ones(_volume):
    """Same shape, deliberately different clock: an authorization trail outlives a 'did it land'."""
    age = ret.DELIVERY_RECEIPT_DAYS + 10
    delivery = _write(_volume, "events/delivery.jsonl", _line(age, label="d"))
    sends = _write(_volume, "events/sends.jsonl", _line(age, verb="reply"))

    ret.sweep(now=NOW)

    assert open(delivery, encoding="utf-8").read().strip() == ""
    assert "reply" in open(sends, encoding="utf-8").read()


def test_an_unreadable_line_is_kept_not_guessed_at(_volume):
    """A parser bug must never become data loss: a line the sweep can't read stays."""
    path = _write(_volume, "events/surfaced.jsonl",
                  "not json at all\n" + json.dumps({"no_ts": 1}) + "\n"
                  + _line(ret.TRIAGE_VERDICT_DAYS + 5, verdict="drop"))

    ret.sweep(now=NOW)

    body = open(path, encoding="utf-8").read()
    assert "not json at all" in body and "no_ts" in body and "drop" not in body


def test_the_rewrite_is_atomic_and_leaves_no_partial_file(_volume, monkeypatch):
    """The rewrite goes through HOOKS['write_text'] — the receiver's tmp-then-replace. A writer that
    dies mid-write leaves the ledger exactly as it was, never half of it."""
    original = _line(400, label="old") + _line(1, label="new")
    path = _write(_volume, "events/queue.jsonl", original)

    def _dies(_p, _t, mode=0o600):
        raise OSError("volume full")

    monkeypatch.setitem(ret.HOOKS, "write_text", _dies)
    out = ret.sweep(now=NOW)

    assert open(path, encoding="utf-8").read() == original
    assert any(e["path"] == "events/queue.jsonl" for e in out["errors"])


def test_an_untouched_ledger_is_not_rewritten(_volume):
    """Nothing old, nothing written — the inode (and any append-mode writer's position) is left
    alone."""
    path = _write(_volume, "events/drafts.jsonl", _line(1, text="hi"))
    before = os.stat(path)

    out = ret.sweep(now=NOW)

    assert os.stat(path).st_ino == before.st_ino
    assert not [e for e in out["swept"] if e["path"] == "events/drafts.jsonl"]


# ── truncate-to-the-last-N-bytes ─────────────────────────────────────────────────────────────────

def test_the_log_keeps_its_tail_and_its_inode(_volume):
    """Truncated IN PLACE: a brief mid-flight holds this file open in append mode, and swapping the
    inode under it would send every later line to a file nobody can read."""
    filler = ("x" * 99 + "\n") * ((ret.LOG_TAIL_MAX_BYTES // 100) + 200)
    path = _write(_volume, "logs/compose_brief.log", "THE FIRST LINE\n" + filler + "THE LAST LINE\n")
    before = os.stat(path)

    out = ret.sweep(now=NOW)

    body = open(path, encoding="utf-8").read()
    assert body.endswith("THE LAST LINE\n")
    assert "THE FIRST LINE" not in body          # the head went, not merely the whitespace
    assert len(body) <= ret.LOG_TAIL_MAX_BYTES
    assert os.stat(path).st_ino == before.st_ino
    assert out["swept"][0]["path"] == "logs/compose_brief.log"


def test_the_log_tail_starts_on_a_line_boundary(_volume):
    """A tail that begins mid-line is a line nobody can parse."""
    line = "y" * 50 + "\n"
    _write(_volume, "logs/compose_brief.log", line * ((ret.LOG_TAIL_MAX_BYTES // len(line)) + 100))

    ret.sweep(now=NOW)

    body = open(os.path.join(str(_volume), "logs", "compose_brief.log"), encoding="utf-8").read()
    assert all(ln == "y" * 50 for ln in body.splitlines())


def test_a_small_log_is_left_exactly_as_it_is(_volume):
    path = _write(_volume, "logs/compose_brief.log", "one quiet line\n")

    out = ret.sweep(now=NOW)

    assert open(path, encoding="utf-8").read() == "one quiet line\n"
    assert not out["swept"]


# ── the sweep's failure posture ──────────────────────────────────────────────────────────────────

def test_one_unreadable_entry_is_logged_and_the_rest_still_go(_volume, monkeypatch):
    """Retention is hygiene; hygiene that can take the daemon down is worse than exhaust."""
    old = time.strftime("%Y-%m-%d", time.gmtime(NOW - 200 * DAY))
    doomed = _write(_volume, "events/delivery.jsonl", _line(400, label="old"))
    marker = _write(_volume, f"briefs/{old}.evening.delivered", "")

    real_open = open

    def _explode(path, *a, **k):
        if str(path) == doomed:
            raise OSError("I/O error")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", _explode)
    out = ret.sweep(now=NOW)
    monkeypatch.undo()

    assert out["errors"] and out["errors"][0]["path"] == "events/delivery.jsonl"
    assert not os.path.exists(marker)


def test_an_empty_volume_is_a_clean_sweep(_volume):
    out = ret.sweep(now=NOW)
    assert out["count"] == 0 and out["errors"] == []


# ── the table itself ─────────────────────────────────────────────────────────────────────────────

def test_every_rule_has_a_policy_a_number_and_one_sentence():
    for rule in ret.SWEEP:
        assert rule.policy in ret._APPLY, rule
        assert isinstance(rule.amount, int) and rule.amount > 0, rule
        assert rule.why and rule.why[0].islower() and "." not in rule.why.rstrip("."), rule


def test_no_family_is_listed_twice():
    """A pattern in both halves is a rule that can be read two ways. accounts_for takes the first
    match, so the ambiguity would be silent."""
    patterns = [r.pattern for r in ret.SWEEP] + [e.pattern for e in ret.EXEMPT]
    assert len(patterns) == len(set(patterns))
    for rule in ret.SWEEP:
        for ex in ret.EXEMPT:
            assert not ret._to_regex(ex.pattern).match(rule.pattern), (rule.pattern, ex.pattern)


def test_globs_never_cross_a_directory_boundary():
    """`cache/*.json` must not reach `cache/sub/x.json` — a table entry means what it says."""
    assert ret._to_regex("cache/*.json").match("cache/x.json")
    assert not ret._to_regex("cache/*.json").match("cache/sub/x.json")
    assert ret._to_regex("knowledge/**").match("knowledge/people/deep/x.md")
    assert ret._to_regex("knowledge/**").match("knowledge")


# ── THE guard: the memory is never a target ──────────────────────────────────────────────────────

MEMORY = (
    "knowledge/master.md",
    "knowledge/people/sam-rivera.md",
    "knowledge/companies/northwind.md",
    "knowledge/continuity/loop-1.md",
    "knowledge/continuity/loop-2.md",
    "knowledge/style.json",
    "knowledge/outcomes.jsonl",
    "knowledge/relationship_state.json",
    "knowledge/last_local_snapshot.json",
    "knowledge/snapshots/2020-01-01.json",
    "corpus/v1/labels.yaml",
    "corpus/v1/messages.jsonl",
    "decks/abc123.pdf",
    "decks/abc123.json",
    "preferences.json",
    "intentions.jsonl",
    "config/settings.json",
    "connectors/granola.json",
    "setup_code",
)


def test_the_sweep_never_touches_the_graph_the_ledger_or_the_corpus(_volume):
    """Seed a volume with the memory — every file aged well past the longest TTL in the table — and
    sweep. Nothing may move. This is the doctrine, not a number: retention covers exhaust, and the
    graph, the continuity ledger, the corpus, your settings and your credentials are not exhaust."""
    before = {}
    for rel in MEMORY:
        path = _write(_volume, rel, f"contents of {rel}\n", age_days=3650)
        before[rel] = (open(path, encoding="utf-8").read(), os.stat(path).st_mtime)

    out = ret.sweep(now=NOW)

    for rel in MEMORY:
        path = os.path.join(str(_volume), *rel.split("/"))
        assert os.path.exists(path), f"the sweep deleted {rel}"
        assert (open(path, encoding="utf-8").read(), os.stat(path).st_mtime) == before[rel], rel
    assert out["errors"] == []   # not "protected", not an error — simply never matched


def test_no_table_entry_even_matches_a_memory_path():
    """Belt and braces: the paths above are spared because no RULE names them, not merely because
    the NEVER guard catches them afterwards."""
    for rel in MEMORY:
        entry = ret.accounts_for(rel)
        assert isinstance(entry, ret.Exempt), f"{rel} is covered by {entry!r}"
        assert ret.protected(rel) or rel in ("preferences.json", "intentions.jsonl"), rel


def test_the_never_guard_holds_even_if_a_rule_reaches_past_it(_volume, monkeypatch):
    """The guard is checked per path, below every glob, so a typo'd future entry still can't delete
    the memory — it reports itself instead."""
    path = _write(_volume, "knowledge/people/sam-rivera.md", "durable", age_days=3650)
    monkeypatch.setattr(ret, "SWEEP",
                        (ret.Rule("knowledge/people/*.md", ret.DELETE_OLDER, 1, "a typo"),))

    out = ret.sweep(now=NOW)

    assert os.path.exists(path)
    assert out["errors"][0]["error"].startswith("protected")
