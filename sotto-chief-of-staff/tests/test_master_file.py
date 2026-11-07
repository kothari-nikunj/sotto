"""master_file.py — the one writer/reader for knowledge/master.md (the master memory file):
section CRUD, the size cap, prompt rendering, cross-process no-tear, and the injection seams
(brief + prep prompts carry {{master_context}}; the persona documents read + confirmed capture)."""
import importlib.util
import json
import os
import subprocess
import sys
import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
MF_PATH = os.path.join(ROOT, "_shared", "knowledge", "master_file.py")

spec = importlib.util.spec_from_file_location("master_file", MF_PATH)
mf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mf)


def _env(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)


def test_set_get_sections_roundtrip(tmp_path):
    _env(tmp_path)
    mf.set_section("About", "Nikunj is a partner at FPV Ventures.")
    mf.set_section("Procedures", "- Intros are always forwardable emails.")
    text = mf.read()
    assert "## About" in text and "FPV Ventures" in text
    assert "## Procedures" in text and "forwardable" in text
    # replace, not append
    mf.set_section("About", "Rewritten.")
    text = mf.read()
    assert "Rewritten." in text and "FPV Ventures" not in text
    assert text.count("## About") == 1


def test_append_and_remove_and_case_insensitive_names(tmp_path):
    _env(tmp_path)
    mf.append_section("Procedures", "- Rule one.")
    mf.append_section("procedures", "- Rule two.")          # same section, any case
    text = mf.read()
    assert text.count("## Procedures") == 1
    assert "- Rule one." in text and "- Rule two." in text
    mf.remove_section("PROCEDURES")
    assert "Procedures" not in mf.read()


def test_preamble_before_first_heading_survives_edits(tmp_path):
    _env(tmp_path)
    os.makedirs(os.path.dirname(mf.path()), exist_ok=True)
    with open(mf.path(), "w", encoding="utf-8") as f:
        f.write("Hand-written preamble line.\n\n## About\nSomething.\n")
    mf.set_section("About", "Else.")
    text = mf.read()
    assert text.startswith("Hand-written preamble line.")
    assert "Else." in text


def test_cap_refuses_and_leaves_file_untouched(tmp_path):
    _env(tmp_path)
    mf.set_section("About", "small")
    before = mf.read()
    try:
        mf.set_section("Priorities", "x" * (mf.MASTER_CHAR_CAP + 1))
        raise AssertionError("expected OverCap")
    except mf.OverCap as e:
        assert "Priorities" in e.sizes
    assert mf.read() == before                               # nothing written


def test_render_for_prompt_frames_or_empty(tmp_path):
    _env(tmp_path)
    assert mf.render_for_prompt() == ""                      # absent file → no block at all
    mf.set_section("Procedures", "- Never book Fridays.")
    out = mf.render_for_prompt()
    assert out.startswith("## MASTER CONTEXT")
    assert "ground truth" in out and "- Never book Fridays." in out


def test_cli_verbs_and_overcap_exit_code(tmp_path):
    env = {**os.environ, "SOTTO_DATA": str(tmp_path)}
    r = subprocess.run([sys.executable, MF_PATH, "set", "--section", "About", "--text", "A line."],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and json.loads(r.stdout)["ok"] is True
    r = subprocess.run([sys.executable, MF_PATH, "get"], capture_output=True, text=True, env=env)
    assert "A line." in json.loads(r.stdout)["text"]
    r = subprocess.run([sys.executable, MF_PATH, "sections"], capture_output=True, text=True, env=env)
    assert "About" in json.loads(r.stdout)
    r = subprocess.run([sys.executable, MF_PATH, "set", "--section", "Big",
                        "--text", "y" * (mf.MASTER_CHAR_CAP + 1)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 2 and "cap" in json.loads(r.stdout)["error"]


def test_concurrent_cli_appends_do_not_tear(tmp_path):
    # Two processes append 10 rules each under the sidecar lock: all 20 land, no torn file.
    env = {**os.environ, "SOTTO_DATA": str(tmp_path)}
    script = ("import subprocess,sys\n"
              "for i in range(10):\n"
              f"    subprocess.run([sys.executable, {MF_PATH!r}, 'append', '--section',"
              " 'Procedures', '--text', f'- rule {sys.argv[1]}-{i}'], check=True)\n")
    ps = [subprocess.Popen([sys.executable, "-c", script, tag], env=env) for tag in ("a", "b")]
    for p in ps:
        assert p.wait() == 0
    os.environ["SOTTO_DATA"] = str(tmp_path)
    text = mf.read()
    for tag in ("a", "b"):
        for i in range(10):
            assert f"- rule {tag}-{i}" in text


def test_brief_and_prep_prompts_carry_the_placeholder():
    with open(os.path.join(ROOT, "morning-brief", "references", "extraction-prompt.md")) as f:
        assert "{{master_context}}" in f.read()
    with open(os.path.join(ROOT, "meeting-prep", "references", "meeting-prep-prompt.md")) as f:
        assert "{{master_context}}" in f.read()


def test_compose_brief_renders_master_into_the_field(tmp_path):
    _env(tmp_path)
    mf.set_section("People", "Partners: Wesley Chan, Pegah Ebrahimi.")
    spec2 = importlib.util.spec_from_file_location(
        "cb_master", os.path.join(ROOT, "_shared", "scripts", "compose_brief.py"))
    cb = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(cb)
    out = cb._format_master_context()
    assert out.startswith("## MASTER CONTEXT") and "Wesley Chan" in out


def test_dashboard_cap_mirror_agrees():
    # The receiver image can't import the skills tree, so dashboard.py carries MASTER_CHAR_CAP as a
    # documented mirror — this is the guard that keeps the two numbers one number.
    import re
    dash = os.path.join(ROOT, "..", "runtime", "trigger-receiver", "dashboard.py")
    with open(dash, encoding="utf-8") as f:
        m = re.search(r"^MASTER_CHAR_CAP = (\d+)$", f.read(), re.MULTILINE)
    assert m, "dashboard.py no longer declares MASTER_CHAR_CAP"
    assert int(m.group(1)) == mf.MASTER_CHAR_CAP


def test_setup_delivers_value_before_optional_preferences():
    with open(os.path.join(ROOT, "setup", "SKILL.md"), encoding="utf-8") as f:
        text = f.read()
    assert 'handles this automatically' in text
    assert 'no mandatory' in text and 'profile questionnaire' in text
    assert 'sotto-feedback' in text and 'master-file writer' in text
    assert 'Never turn observed activity into' in text
    assert 'permission' in text and 'actual per-source progress' in text


def test_procedure_offer_picks_fresh_rule_and_records_the_bridge(tmp_path):
    # The evening spotter: first candidate NOT already in the master file wins; the question is
    # appended deterministically and the pending-offer bridge is written so a bare "yes" lands on
    # it. The rule itself is never written here — the gateway writes it after the yes.
    _env(tmp_path)
    mf.set_section("Procedures", "- Intros are always forwardable emails.")
    spec2 = importlib.util.spec_from_file_location(
        "cb_proc", os.path.join(ROOT, "_shared", "scripts", "compose_brief.py"))
    cb = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(cb)
    fu = {"procedural_candidates": ["Intros are always forwardable emails.",   # already standing
                                    {"rule": "Never book me Fridays."}]}       # fresh (dict form)
    rule = cb._pick_procedural_candidate(fu)
    assert rule == "Never book me Fridays."
    out = cb._append_procedure_offer({"brief_markdown": "## Brief\nbody"},
                                     {"_procedure_offer": rule})
    assert "Never book me Fridays." in out["brief_markdown"]
    assert "standing rule" in out["brief_markdown"]
    with open(os.path.join(str(tmp_path), "proactive", "pending_offer.json")) as f:
        offer = json.load(f)
    assert offer["kind"] == "procedure" and offer["detail"] == "Never book me Fridays."
    # nothing fresh → no question, no offer, brief untouched
    out2 = cb._append_procedure_offer({"brief_markdown": "x"}, {"_procedure_offer": ""})
    assert out2["brief_markdown"] == "x"
    # master file never gained the rule from any of this (confirmed-first bar)
    assert "Fridays" not in mf.read()


def test_pending_offer_accepts_the_procedure_kind(tmp_path):
    env = {**os.environ, "SOTTO_DATA": str(tmp_path)}
    po = os.path.join(ROOT, "_shared", "scripts", "pending_offer.py")
    r = subprocess.run([sys.executable, po, "set", "--kind", "procedure",
                        "--question", "Make it standing?", "--detail", "Never book Fridays."],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    r = subprocess.run([sys.executable, po, "get"], capture_output=True, text=True, env=env)
    got = json.loads(r.stdout)
    assert got.get("kind") == "procedure" and got.get("detail") == "Never book Fridays."


def test_priorities_are_bounded_stable_and_replace_as_a_set(tmp_path):
    _env(tmp_path)
    mf.set_section("Priorities", "- Fund close\n- Maya's school")
    first = mf.priorities()
    assert [p["text"] for p in first["priorities"]] == ["Fund close", "Maya's school"]
    assert all(len(p["id"]) == 16 for p in first["priorities"])
    assert mf.priorities() == first
    mf.set_section("Priorities", "- Hiring")
    second = mf.priorities()
    assert [p["text"] for p in second["priorities"]] == ["Hiring"]
    assert second["revision"] != first["revision"]
    with pytest.raises(ValueError, match="at most 3"):
        mf.set_section("Priorities", "one\ntwo\nthree\nfour")


@pytest.mark.parametrize('instant, expected', [
    ('2026-10-14T02:14:00+00:00', 'Tuesday, 2026-10-13T19:14:00-07:00'),
    ('2026-10-14T07:01:00+00:00', 'Wednesday, 2026-10-14T00:01:00-07:00'),
    ('2026-12-14T02:14:00+00:00', 'Sunday, 2026-12-13T18:14:00-08:00'),
])
def test_chat_prompt_clock_uses_owner_zone_and_refreshes_without_saving(tmp_path, monkeypatch, instant, expected):
    from datetime import datetime
    import tzchain
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_TIMEZONE', raising=False)
    monkeypatch.delenv('TZ', raising=False)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'settings.json').write_text(json.dumps({'timezone':'America/Los_Angeles'}))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(instant).astimezone(tz)
    monkeypatch.setattr(tzchain, 'datetime', Clock)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    result = mf.render_for_chat_prompt('')
    assert expected in result
    assert '(America/Los_Angeles)' in result
    assert 'MASTER CONTEXT' not in result
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')) == before
    assert mf.render_for_chat_prompt('A saved rule.').endswith(mf.render_for_prompt('A saved rule.'))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    assert datetime.fromisoformat(instant).isoformat(timespec='seconds') in mf.render_for_chat_prompt('')


@pytest.mark.parametrize('zone', ['', 'Invalid/Zone'])
def test_chat_clock_does_not_present_utc_fallback_as_owner_local_date(tmp_path, monkeypatch, zone):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', zone)
    monkeypatch.delenv('TZ', raising=False)
    result = mf.render_for_chat_prompt('')
    assert result.startswith('Current UTC instant: ')
    assert "local timezone is unavailable" in result
    assert 'Current local date' not in result
