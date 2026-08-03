"""loops_query.py — splits the continuity ledger into you-owe vs waiting-on, oldest/overdue first."""
import importlib.util, os, sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))
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
    today = lq.cb._now_local("+00:00")
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
