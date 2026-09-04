"""learn_step.py — the Learn step as ONE command, with a receipt the receiver can check."""
import json
import os
import subprocess
import sys
import types

import learn_step as ls

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "_shared", "scripts", "learn_step.py")


def _args(tmp_path, **over):
    base = {"type": "morning", "local": "", "gmail": "", "granola": "", "knowledge_out": "",
            "continuity": "", "day": "2026-09-04"}
    base.update(over)
    return types.SimpleNamespace(**base)


def _touch(tmp_path, name, body="{}"):
    p = tmp_path / name
    p.write_text(body)
    return str(p)


def test_every_writer_runs_in_order_and_the_receipt_says_what_ran(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    calls = []

    def fake_run(argv, **kw):
        calls.append(os.path.basename(argv[1]))
        rc = 3 if argv[1].endswith("granola_graph.py") else 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="boom\n" if rc else "")

    args = _args(tmp_path, local=_touch(tmp_path, "local.json"), gmail=_touch(tmp_path, "gmail.json"),
                 granola=_touch(tmp_path, "granola.json"), knowledge_out=_touch(tmp_path, "know.json"),
                 continuity=_touch(tmp_path, "cont.json"))
    receipt = ls.learn(args, run=fake_run)
    assert calls == ["knowledge_update.py", "continuity_resolve.py", "learn_preferences.py",
                     "style_extract.py", "granola_graph.py", "prewarm_graph.py"]
    assert receipt["ok"] is False and receipt["day"] == "2026-09-04" and receipt["kind"] == "morning"
    assert receipt["steps"]["granola"] == {"status": "failed", "exit": 3, "detail": "boom"}
    assert {k: v["status"] for k, v in receipt["steps"].items() if k != "granola"} == {
        "knowledge": "ok", "continuity": "ok", "preferences": "ok", "style": "ok", "contacts": "ok"}
    on_disk = json.load(open(ls.receipt_path("2026-09-04", "morning")))
    assert on_disk == receipt


def test_a_step_with_no_input_is_skipped_never_failed(tmp_path, monkeypatch):
    """A quiet day with no Granola meetings and no extracted knowledge is not a failed Learn — only
    the two input-less writers (preferences, contacts) must always run."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    calls = []
    fake_run = lambda argv, **kw: (calls.append(os.path.basename(argv[1])),
                                   types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1]
    receipt = ls.learn(_args(tmp_path), run=fake_run)
    assert calls == ["learn_preferences.py", "prewarm_graph.py"]
    assert receipt["ok"] is True
    assert {k: v["status"] for k, v in receipt["steps"].items()} == {
        "knowledge": "skipped", "continuity": "skipped", "preferences": "ok",
        "style": "skipped", "granola": "skipped", "contacts": "ok"}


def test_a_missing_gmail_file_never_skips_the_voice_writer(tmp_path, monkeypatch):
    """--gmail is optional to style (a Gmail-less day, a failed gather): the local snapshot alone
    runs the writer, without the flag. Skipping the whole voice step on a green receipt was the
    exact failure this runner exists to prevent."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv[1:])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    local = _touch(tmp_path, "local.json")
    receipt = ls.learn(_args(tmp_path, local=local, gmail=str(tmp_path / "nope.json")), run=fake_run)
    style = next(a for a in calls if a[0].endswith("style_extract.py"))
    assert style == [style[0], local] and receipt["steps"]["style"]["status"] == "ok"


def test_the_cli_runs_the_real_writers_end_to_end(tmp_path, monkeypatch):
    """The whole step on an empty volume with minimal inputs: six real scripts, exit 0, a receipt.
    This is the run a brief makes on its first quiet day."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    local = _touch(tmp_path, "local.json", json.dumps({
        "contacts": [{"name": "Sarah Chen", "phones": ["+14155551234"], "emails": ["sarah@example.com"]}],
        "imessage": [{"handle": "+14155551234", "is_from_me": True, "timestamp": "2026-09-04 09:00:00",
                      "text": "sounds good, sending the deck over tonight", "is_group_chat": False}]}))
    know = _touch(tmp_path, "know.json", json.dumps({"person_updates": [], "company_updates": []}))
    cont = _touch(tmp_path, "cont.json", json.dumps({"today": "2026-09-04", "new_actions": [], "local": {}}))
    granola = _touch(tmp_path, "granola.json", json.dumps({"meetings": []}))
    r = subprocess.run([sys.executable, SCRIPT, "--type", "evening", "--local", local,
                        "--knowledge-out", know, "--continuity", cont, "--granola", granola,
                        "--day", "2026-09-04"],
                       capture_output=True, text=True, env={**os.environ, "SOTTO_DATA": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    receipt = json.loads(r.stdout.strip().splitlines()[-1])
    assert receipt["ok"] is True and receipt["kind"] == "evening"
    assert all(v["status"] == "ok" for v in receipt["steps"].values()), receipt["steps"]
    assert os.path.exists(os.path.join(str(tmp_path), "briefs", "2026-09-04.evening.learned.json"))
    assert os.path.exists(os.path.join(str(tmp_path), "style.json"))        # style really ran
