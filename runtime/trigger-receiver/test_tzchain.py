"""tzchain — THE timezone chain, and the guard that keeps it the only one."""
import importlib.util
import json
import os
import re
from datetime import timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
CHAIN = os.path.join(REPO, "sotto-chief-of-staff", "_shared", "lib", "tzchain.py")


def _load():
    spec = importlib.util.spec_from_file_location("tzchain_under_test", CHAIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_chain_in_order_env_then_wizard_then_utc(tmp_path, monkeypatch):
    tz = _load()
    for key in ("SOTTO_TIMEZONE", "TZ"):
        monkeypatch.delenv(key, raising=False)
    assert tz.configured_tz_source(str(tmp_path)) == ("", "")
    assert tz.local_now(str(tmp_path)).tzinfo == timezone.utc, "the last rung is UTC, never the server"
    os.makedirs(tmp_path / "config")
    (tmp_path / "config" / "settings.json").write_text(json.dumps({"timezone": "Europe/London"}))
    assert tz.configured_tz_source(str(tmp_path)) == ("Europe/London", "settings")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert tz.configured_tz_source(str(tmp_path)) == ("Asia/Tokyo", "env:TZ")
    monkeypatch.setenv("SOTTO_TIMEZONE", "America/Los_Angeles")
    assert tz.configured_tz_source(str(tmp_path)) == ("America/Los_Angeles", "env:SOTTO_TIMEZONE")
    assert str(tz.local_now(str(tmp_path)).tzinfo) == "America/Los_Angeles"
    # a fixed offset resolves too; an unknown name resolves to nothing (the caller then uses UTC)
    assert tz.resolve("+05:30").utcoffset(None).total_seconds() == 5.5 * 3600
    assert tz.resolve("Mars/Olympus_Mons") is None and tz.resolve("") is None


def test_every_runtime_resolves_the_day_from_the_one_file(tmp_path, monkeypatch):
    """The receiver, the dashboard and the skills' timeutil must all answer with tzchain's answer —
    and none of them may carry a chain of its own. The guard is textual on purpose: a second
    `os.environ.get("SOTTO_TIMEZONE")` anywhere is a second implementation, whatever it returns."""
    monkeypatch.setenv("SOTTO_TIMEZONE", "Pacific/Auckland")
    tz = _load()
    expected = tz.local_today(str(tmp_path))
    spec = importlib.util.spec_from_file_location("receiver_tz_test", os.path.join(HERE, "receiver.py"))
    rec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rec)
    rec.DATA = str(tmp_path)
    assert rec._configured_tz_name() == "Pacific/Auckland"
    assert rec._local_now().strftime("%Y-%m-%d") == expected
    rec.DASHBOARD.HOOKS["data_root"] = lambda: str(tmp_path)
    assert rec.DASHBOARD._local_today() == expected
    for rel in ("runtime/trigger-receiver/receiver.py", "runtime/trigger-receiver/dashboard.py",
                "sotto-chief-of-staff/_shared/lib/timeutil.py", "adapters/hermes/start.sh"):
        text = open(os.path.join(REPO, rel), encoding="utf-8").read()
        # (reading settings["timezone"] to DISPLAY it is fine; resolving the zone from env is the chain)
        assert not re.search(r'environ\.get\("SOTTO_TIMEZONE"\)|environ\.get\("TZ"\)|\$\{TZ:-\}', text), \
            f"{rel} carries its own timezone chain"
