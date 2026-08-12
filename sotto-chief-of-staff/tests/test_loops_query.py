"""loops_query.py — splits the continuity ledger into you-owe vs waiting-on, oldest/overdue first."""
import importlib.util, os, sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
spec = importlib.util.spec_from_file_location("lq", os.path.join(ROOT, "_shared", "scripts", "loops_query.py"))
lq = importlib.util.module_from_spec(spec); spec.loader.exec_module(lq)


def _write(d, name, fm):
    import yaml
    (d / f"{name}.md").write_text("---\n" + yaml.safe_dump(fm) + "---\nbody\n")


def test_splits_direction_and_orders(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    cdir = tmp_path / "knowledge" / "continuity"
    cdir.mkdir(parents=True)
    today = lq._now_local("+00:00")
    old = (today.replace(microsecond=0)).strftime("%Y-%m-%dT%H:%M:%S")
    import datetime
    days5 = (today - datetime.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
    days1 = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    overdue_dl = (today - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    _write(cdir, "a", {"status": "open", "action_type": "reply", "contact_name": "Dhruv",
                       "summary": "reply re LOI", "created_at": days1})
    _write(cdir, "b", {"status": "open", "action_type": "reply", "contact_name": "Old",
                       "summary": "old thread", "created_at": days5, "deadline": overdue_dl})
    _write(cdir, "c", {"status": "waiting", "action_type": "waiting_on", "contact_name": "Vendor",
                       "summary": "awaiting quote", "created_at": days5})
    _write(cdir, "d", {"status": "resolved", "action_type": "reply", "contact_name": "Done",
                       "summary": "closed", "created_at": old})  # terminal → excluded

    out = lq.query()
    assert out["counts"] == {"you_owe": 2, "waiting_on_them": 1}
    assert [e["name"] for e in out["you_owe"]] == ["Old", "Dhruv"]   # overdue first, then older
    assert out["you_owe"][0]["overdue"] is True
    assert out["waiting_on_them"][0]["name"] == "Vendor"
    assert all(e["name"] != "Done" for e in out["you_owe"])          # resolved excluded


def test_empty_when_no_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    out = lq.query()
    assert out["counts"] == {"you_owe": 0, "waiting_on_them": 0}

def test_chase_state_is_surfaced_so_the_answer_can_name_it(tmp_path, monkeypatch):
    """"What am I waiting on" should be able to say "chased once, Tuesday" — the fields come from
    the ledger, which continuity_resolve alone writes."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    cdir = tmp_path / "knowledge" / "continuity"
    cdir.mkdir(parents=True)
    _write(cdir, "w", {"status": "open", "action_type": "waiting_on", "contact_name": "Vendor",
                       "summary": "awaiting quote", "created_at": "2026-08-01",
                       "chased_count": 1, "last_chased_at": "2026-08-04", "chase_after": "2026-08-07"})
    _write(cdir, "o", {"status": "open", "action_type": "reply", "contact_name": "Dhruv",
                       "summary": "reply re LOI", "created_at": "2026-08-01"})
    out = lq.query()
    w = out["waiting_on_them"][0]
    assert (w["chased_count"], w["last_chased_at"], w["chase_after"]) == (1, "2026-08-04", "2026-08-07")
    assert out["you_owe"][0]["chased_count"] == 0 and out["you_owe"][0]["last_chased_at"] is None


def test_follow_up_stale_is_something_you_owe_them(tmp_path, monkeypatch):
    """One predicate, in ledger_io: `waiting_on` is what THEY owe you. `follow_up_stale` resolves on
    YOUR outgoing message and expires on YOUR clock, so it belongs under you_owe — which is also
    where the resolver has always treated it. Variant spellings classify identically."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    cdir = tmp_path / "knowledge" / "continuity"
    cdir.mkdir(parents=True)
    _write(cdir, "s", {"status": "open", "action_type": "follow_up_stale", "contact_name": "Priya",
                       "summary": "sent the LOI", "created_at": "2026-08-01"})
    _write(cdir, "v", {"status": "open", "action_type": "Waiting-On ", "contact_name": "Vendor",
                       "summary": "awaiting quote", "created_at": "2026-08-01"})
    out = lq.query()
    assert [e["name"] for e in out["you_owe"]] == ["Priya"]
    assert [e["name"] for e in out["waiting_on_them"]] == ["Vendor"]


def test_chased_out_and_chase_pending_are_exposed_to_the_nudge_lane(tmp_path, monkeypatch):
    """The hand-off ("chased its quota") and today's proposed chase are both data, so the lanes
    that surface them need no second ledger reader."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    cdir = tmp_path / "knowledge" / "continuity"
    cdir.mkdir(parents=True)
    _write(cdir, "done", {"status": "open", "action_type": "waiting_on", "anchor_key": "done",
                          "contact_name": "Acme", "summary": "the contract", "chased_count": 2,
                          "created_at": "2026-08-01"})
    _write(cdir, "todo", {"status": "open", "action_type": "waiting_on", "anchor_key": "todo",
                          "contact_name": "Vendor", "summary": "a quote", "created_at": "2026-08-01",
                          "chase_pending": "2026-08-09"})
    rows = {e["anchor_key"]: e for e in lq.query()["waiting_on_them"]}
    assert rows["done"]["chased_out"] is True and rows["done"]["chase_pending"] is None
    assert rows["todo"]["chased_out"] is False and rows["todo"]["chase_pending"] == "2026-08-09"


def test_thread_id_is_surfaced_so_an_email_draft_can_be_threaded(tmp_path, monkeypatch):
    """The ledger has carried `source_thread_id` since the first row; the READ view dropped it, so
    an offer made off a loop had no way to thread. A reply that starts a new thread is worse than a
    link — so the thread id travels with the loop. Non-email loops carry ""."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    cdir = tmp_path / "knowledge" / "continuity"
    cdir.mkdir(parents=True)
    _write(cdir, "a", {"status": "open", "action_type": "reply", "channel": "email",
                       "contact_name": "Pegah", "contact_identifier": "pegah@example.com",
                       "summary": "SAFE allocation call", "source_thread_id": "T7"})
    _write(cdir, "b", {"status": "open", "action_type": "reply", "channel": "imessage",
                       "contact_name": "Dhruv", "contact_identifier": "+15551234567",
                       "summary": "ping back"})
    out = lq.query()
    by_name = {e["name"]: e for e in out["you_owe"]}
    assert by_name["Pegah"]["thread_id"] == "T7"
    assert by_name["Dhruv"]["thread_id"] == ""
