#!/usr/bin/env python3
"""
run_evals.py — brief-quality eval harness for the sotto-chief-of-staff pack.

Two modes (see evals/README.md for the full workflow):

  --deterministic   (DEFAULT) Fully offline, no network, no wall-clock dependence, <5s.
                    Loads three golden "day" fixtures, runs the REAL pipeline pieces
                    (normalize → continuity resolve → build_prompt → compose with a stub
                    LLM → tap-link post-processing) into a throwaway $SOTTO_DATA sandbox,
                    then asserts a suite of named INVARIANTS (not exact prose). Prints a
                    scorecard and exits nonzero on any failure. tests/test_evals.py runs
                    the same checks under pytest so they're guarded by CI by default.

  --live            Requires GOOGLE_AI_API_KEY. Runs the REAL Gemini extraction per fixture
                    and scores each brief with the pack's own brief critic (compose_brief's
                    run_critic). Writes/compares the baseline scores.json — under
                    $SOTTO_DATA/evals/baselines/ in-container (the skills tree is read-only /
                    wiped each boot), else repo-local evals/baselines/ for dev — and fails if a
                    fixture regresses by more than --threshold points. NEVER run automatically
                    in CI; it is a deliberate, human-invoked scored run.

The harness writes ONLY under a temp sandbox — it never mutates repo files. Stdlib only.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)                       # sotto-chief-of-staff
FIX_DIR = os.path.join(HERE, "fixtures")


def _baselines_dir() -> str:
    """Where the live-mode baseline lives. In-container the skills tree is read-only and start.sh wipes
    it every boot, so persist the baseline on the $SOTTO_DATA volume when it's set; fall back to the
    repo-local evals/baselines/ for the dev workflow (unchanged). Resolved at call time so a test can
    set SOTTO_DATA per-run."""
    data = os.environ.get("SOTTO_DATA")
    if data:
        return os.path.join(data, "evals", "baselines")
    return os.path.join(HERE, "baselines")


def _scores_path() -> str:
    return os.path.join(_baselines_dir(), "scores.json")

# The pack imports its own siblings by bare name off these paths (compose_brief, ledger_io).
sys.path.insert(0, os.path.join(PACK, "_shared", "lib"))
sys.path.insert(0, os.path.join(PACK, "_shared", "scripts"))


def _load_module(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PACK, rel_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod                        # so cross-imports (import compose_brief) resolve
    spec.loader.exec_module(mod)
    return mod


cb = _load_module(os.path.join("_shared", "scripts", "compose_brief.py"), "compose_brief")
cr = _load_module(os.path.join("morning-brief", "scripts", "continuity_resolve.py"), "continuity_resolve")

FIXTURES = ["rich_day", "quiet_day", "edge_day"]

# ── Token substitution — deterministic timestamps parameterized off a FIXED base date ─────────────
# Fixtures carry relative tokens instead of wall-clock literals. At load we resolve them against a
# single base datetime captured once, so a fixture produces the SAME invariants every run regardless
# of when it runs (mirrors how test_continuity_resolve pins 'today' + a fixed NOW).
_TOKEN_RE = re.compile(r"\{\{(D|TS|ISO|MD)([+-]\d+[hdm])?\}\}")


def _apply_offset(base: datetime, offset: str) -> datetime:
    if not offset:
        return base
    sign = 1 if offset[0] == "+" else -1
    n, unit = int(offset[1:-1]), offset[-1]
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "m": timedelta(minutes=n)}[unit]
    return base + sign * delta


def _render_token(kind: str, offset: str, base: datetime) -> str:
    dt = _apply_offset(base, offset)
    if kind == "D":
        return dt.strftime("%Y-%m-%d")
    if kind == "TS":                              # Bridge-style naive local timestamp
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if kind == "ISO":                             # Google-style ISO with offset
        return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    if kind == "MD":                              # Apple-Contacts birthday MM-DD
        return dt.strftime("%m-%d")
    return kind


def resolve_tokens(obj, base: datetime):
    if isinstance(obj, dict):
        return {k: resolve_tokens(v, base) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_tokens(v, base) for v in obj]
    if isinstance(obj, str):
        return _TOKEN_RE.sub(lambda m: _render_token(m.group(1), m.group(2), base), obj)
    return obj


def _base_datetime() -> datetime:
    """Noon 'today' (UTC), naive — the single reference every fixture timestamp derives from. Noon
    keeps hour offsets on the same calendar day and sidesteps midnight boundary flakiness."""
    return datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0, tzinfo=None)


# ── Sandbox seeding (stdlib-only frontmatter emitter; PyYAML stays a pack dependency, not ours) ────

def _emit_frontmatter(fm: dict) -> str:
    lines = []
    for k, v in fm.items():
        if isinstance(v, bool):
            val = "true" if v else "false"
        elif isinstance(v, (int, float)):
            val = str(v)
        elif v is None:
            val = "null"
        else:
            s = str(v).replace("\\", "\\\\").replace('"', '\\"')
            val = f'"{s}"'
        lines.append(f"{k}: {val}")
    return "\n".join(lines) + "\n"


def _seed_sandbox(fixture: dict) -> tuple[str, list]:
    sandbox = tempfile.mkdtemp(prefix="sotto-eval-")
    prefs = fixture.get("preferences")
    if prefs is not None:
        with open(os.path.join(sandbox, "preferences.json"), "w", encoding="utf-8") as f:
            json.dump({"explicit": prefs}, f)
    cdir = os.path.join(sandbox, "knowledge", "continuity")
    os.makedirs(cdir, exist_ok=True)
    malformed = []
    for entry in fixture.get("continuity_ledger", []):
        path = os.path.join(cdir, entry["filename"])
        if "raw" in entry:
            content = entry["raw"]
        else:
            content = "---\n" + _emit_frontmatter(entry["frontmatter"]) + "---\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        if entry.get("malformed"):
            malformed.append(path)
    return sandbox, malformed


def _read_files(paths: list) -> dict:
    out = {}
    for p in paths:
        try:
            with open(p, "rb") as f:
                out[p] = f.read()
        except OSError:
            out[p] = None
    return out


# ── The pipeline run per fixture ──────────────────────────────────────────────────────────────────

def run_pipeline(name: str) -> dict:
    with open(os.path.join(FIX_DIR, name + ".json"), encoding="utf-8") as f:
        fixture = json.load(f)
    base = _base_datetime()
    fx = resolve_tokens(fixture, base)
    today = base.strftime("%Y-%m-%d")

    sandbox, malformed_paths = _seed_sandbox(fx)
    saved = {k: os.environ.get(k) for k in ("SOTTO_DATA", "SOTTO_TIMEZONE", "SOTTO_CRITIC", "SOTTO_LLM_STUB")}
    result = {"name": name, "description": fixture.get("description", ""), "exception": None,
              "prompt": "", "continuity": {}, "out": {}, "coverage": "",
              "critic_ran": False, "critic_skipped": False,
              "malformed_before": {}, "malformed_after": {}}
    try:
        os.environ["SOTTO_DATA"] = sandbox
        os.environ["SOTTO_TIMEZONE"] = "+00:00"       # deterministic zone
        os.environ.pop("SOTTO_CRITIC", None)          # default = auto (critic decision under test)
        os.environ.pop("SOTTO_LLM_STUB", None)

        inputs = fx["inputs"]
        stub = fx["stub_response"]

        calls = {"critic": False}

        def stub_llm(prompt_text, in_):
            """Offline stub — the documented SOTTO_LLM_STUB mechanism, as an injected llm so we can
            also observe whether the critic/revise pass fired (the auto-critic invariant)."""
            if in_.get("_critic"):
                calls["critic"] = True
                return json.dumps({"patches": [], "score": 88, "summary": "ok"})
            if in_.get("_revise"):
                return json.dumps({"brief_markdown": stub.get("brief_markdown", ""),
                                   "actions": stub.get("actions", [])})
            return json.dumps(stub)

        # 1) build_prompt (folds top-level google/granola into local, resolves names, mutes)
        result["prompt"] = cb.build_prompt(cb._load_prompt(), inputs)

        # 2) continuity resolve (deterministic, pre-LLM) — pinned 'today' + fixed 'now'
        result["malformed_before"] = _read_files(malformed_paths)
        cont_payload = {
            "today": today,
            "signals": fx.get("signals", {}),
            "local": inputs.get("local", {}),
            "events": inputs.get("google", {}).get("events", []),
            "new_actions": fx.get("new_actions", []),
        }
        result["continuity"] = cr.resolve(cont_payload, base)
        result["malformed_after"] = _read_files(malformed_paths)

        # 3) compose with the stub LLM + critic=auto → 4) tap-link post-processing (inside compose)
        out = cb.compose(inputs, llm=stub_llm, critic=True)
        result["out"] = out
        result["critic_ran"] = calls["critic"]
        result["critic_skipped"] = bool(isinstance(out.get("_critic"), dict) and out["_critic"].get("skipped"))

        # coverage line — computed on the same normalized inputs the brief sees
        local_res = cb.resolve_contact_names(cb._normalize_local(inputs))
        google = inputs.get("google", {})
        emails = [cb._trim_email(e) for e in google.get("emails", [])]
        result["coverage"] = cb._coverage_line(local_res, local_res.get("_source_availability") or {},
                                               google.get("events", []), emails)
        result["local_resolved"] = local_res
    except Exception:                                  # noqa: BLE001 — edge_day asserts "no exception"
        result["exception"] = traceback.format_exc()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(sandbox, ignore_errors=True)
    return result


# ── Invariant checks — each returns (name, passed, detail). Detail is the one-line failure message. ─

_ALLOWED_TAP_PREFIXES = ("mailto:", "sms:+", "tel:+", "https://wa.me/", "https://mail.google.com/",
                         "https://www.google.com/calendar/", "https://calendar.google.com/",
                         "https://meet.google.com/")
_GROUP_MARKERS = ("group_", "@g.us", "@lid", "@c.us")


def _actions(r):
    return [a for a in (r["out"].get("actions") or []) if isinstance(a, dict)]


def chk_no_exception(r):
    return ("no_exception", r["exception"] is None,
            "pipeline raised: " + (r["exception"] or "").strip().splitlines()[-1] if r["exception"] else "ok")


def chk_tap_links_wellformed(r):
    bad = []
    for a in _actions(r):
        link = a.get("tap_link")
        if not link:
            continue
        if not link.startswith(_ALLOWED_TAP_PREFIXES) or any(m in link for m in _GROUP_MARKERS):
            bad.append(f"{a.get('id')}:{link}")
    return ("tap_links_wellformed", not bad,
            "malformed/invented tap_link(s): " + ", ".join(bad) if bad else "all tap_links use safe schemes")


def _mk_present(check_name, needles, description):
    def _c(r):
        missing = [n for n in needles if n not in r["prompt"]]
        return (check_name, not missing, f"prompt missing {description}: {missing}" if missing else "all present")
    return _c


def _mk_absent(check_name, needles, description):
    def _c(r):
        found = [n for n in needles if n in r["prompt"]]
        return (check_name, not found, f"{description} leaked into prompt: {found}" if found else "correctly absent")
    return _c


# rich_day -----------------------------------------------------------------------------------------

def chk_rich_muted_sender(r):
    leaked = [n for n in ("Your Morning Newsletter", "digest@news.example.com") if n in r["prompt"]]
    real = "Series B wire instructions" in r["prompt"]
    ok = not leaked and real
    return ("muted_sender_absent", ok,
            f"muted newsletter leaked: {leaked}" if leaked else
            "positive control (real email) missing" if not real else "muted sender dropped, real email kept")


def chk_rich_loops(r):
    resolved_names = {i.get("contact_name") for i in r["continuity"].get("resolved", [])}
    active_names = {i.get("contact_name") for i in r["continuity"].get("active", [])}
    dana = "Dana Wells" in resolved_names
    marcus = "Marcus Lee" in active_names
    return ("loop_resolved_and_surfaced", dana and marcus,
            f"Dana(resolved)={dana} Marcus(active)={marcus}; resolved={resolved_names} active={active_names}")


def chk_rich_group_no_deeplink(r):
    a6 = next((a for a in _actions(r) if a.get("id") == "a6"), None)
    group_leak = [a.get("tap_link") for a in _actions(r)
                  if a.get("tap_link") and any(m in a["tap_link"] for m in _GROUP_MARKERS)]
    ok = a6 is not None and not a6.get("tap_link") and not group_leak
    return ("group_action_no_deeplink", ok,
            f"group action tap_link={a6.get('tap_link') if a6 else 'MISSING'} leaks={group_leak}")


def chk_rich_calendar_linked(r):
    a5 = next((a for a in _actions(r) if a.get("id") == "a5"), None)
    ok = bool(a5) and a5.get("tap_link") == "https://meet.google.com/rich-acme-abc"
    return ("calendar_action_linked", ok,
            f"calendar action tap_link={a5.get('tap_link') if a5 else 'MISSING'}")


def chk_rich_coverage(r):
    c = r["coverage"]
    need = ["your email and calendar", "iMessage", "WhatsApp", "Granola"]
    missing = [n for n in need if n not in c]
    extra_link = "Link" in c
    return ("coverage_names_sources", not missing and not extra_link,
            f"coverage='{c}' missing={missing} unexpected_link={extra_link}")


def chk_rich_critic_ran(r):
    return ("critic_ran", r["critic_ran"] is True,
            "critic pass did not run on the busy brief" if not r["critic_ran"] else "critic ran")


# quiet_day ----------------------------------------------------------------------------------------

def chk_quiet_critic_skipped(r):
    return ("critic_skipped", r["critic_skipped"] is True,
            f"critic did not auto-skip: _critic={r['out'].get('_critic')}")


def chk_quiet_coverage(r):
    c = r["coverage"]
    ok = ("your email and calendar" in c) and ("iMessage" not in c) and ("WhatsApp" not in c) and ("Link Granola" in c)
    return ("coverage_email_calendar_only", ok, f"coverage='{c}'")


def chk_quiet_empties(r):
    no_bdays = cb._format_birthdays(r.get("local_resolved", {})) == ""
    none_threads = "(none)" in r["prompt"]           # empty message sections render as "(none)"
    return ("empty_sections_omitted", no_bdays and none_threads,
            f"birthdays_empty={no_bdays} none_placeholder={none_threads}")


# edge_day -----------------------------------------------------------------------------------------

def chk_edge_malformed_untouched(r):
    before, after = r["malformed_before"], r["malformed_after"]
    ok = bool(before) and before == after
    return ("malformed_ledger_untouched", ok,
            f"malformed file changed or missing (before={ {k: len(v or b'') for k, v in before.items()} })")


def chk_edge_muted_person(r):
    p = r["prompt"]
    ok = ("going quiet for three weeks" not in p) and ("waiting five days on the proposal" in p) \
        and ("Do NOT surface or flag these people anywhere in the brief: Bob Vance" in p)
    return ("muted_person_suppressed", ok,
            "Bob(reason) present OR Carol(reason) absent OR restated-mute missing")


def chk_edge_expired_loop(r):
    expired = {i.get("contact_name") for i in r["continuity"].get("expired", [])}
    active = {i.get("contact_name") for i in r["continuity"].get("active", [])}
    ok = "Old Thread" in expired and "Nadia Ops" in active
    return ("expired_loop_ages_out", ok, f"expired={expired} active={active}")


def chk_edge_empty_google_coverage(r):
    c = r["coverage"]
    ok = ("Link Gmail + Calendar" in c) and ("iMessage" in c)
    return ("empty_google_coverage", ok, f"coverage='{c}'")


def chk_edge_unicode(r):
    needles = ["🎉", "你好", "会議"]
    missing = [n for n in needles if n not in r["prompt"]]
    return ("unicode_preserved", not missing, f"unicode dropped from prompt: {missing}")


def chk_edge_phone_only(r):
    return ("phone_only_identity_resolved", "Nadia Ops" in r["prompt"],
            "phone-only contact did not resolve to a name in the prompt")


def chk_edge_group_names_not_invented(r):
    """A group chat surfaces its group_name VERBATIM (named group) or a participant label built from
    RESOLVED contact names (unnamed group) as its bold header — NEVER a topical invention, NEVER a raw
    id. This holds for BOTH channels: iMessage AND WhatsApp flow through the same chat_guid keying +
    _group_display_label path, so each group collapses all senders into ONE
    '### <label> [GROUP - no deep link]' header."""
    p = r["prompt"]
    # iMessage: named group uses its display_name; unnamed group uses the participant name label.
    im_named = "### Ops War Room [GROUP - no deep link]" in p
    im_label = "### Nadia Ops, Yuki Tanaka & 1 other [GROUP - no deep link]" in p
    # WhatsApp: named group uses its subject (ZPARTNERNAME); unnamed group uses member-JID → contact
    # name resolution ("Nadia Ops & Yuki Tanaka"), NOT the raw group JID and NOT a topic guess.
    wa_named = "### Weekend Hikers [GROUP - no deep link]" in p
    wa_label = "### Nadia Ops & Yuki Tanaka [GROUP - no deep link]" in p
    # No group may be re-labelled as a single sender, and no raw group JID may surface as a header.
    single_sender = "### Nadia Ops [GROUP - no deep link]" in p
    raw_jid = "@g.us [GROUP - no deep link]" in p
    ok = im_named and im_label and wa_named and wa_label and not single_sender and not raw_jid
    return ("group_names_not_invented", ok,
            f"im_named={im_named} im_label={im_label} wa_named={wa_named} wa_label={wa_label} "
            f"single_sender={single_sender} raw_jid={raw_jid}")


def chk_edge_group_no_deeplink(r):
    """The group reply action (e3) must get NO tap_link — group deep-linking stays unsupported — and no
    action anywhere may leak a group id / JID as a fake deep link."""
    e3 = next((a for a in _actions(r) if a.get("id") == "e3"), None)
    group_leak = [a.get("tap_link") for a in _actions(r)
                  if a.get("tap_link") and any(m in a["tap_link"] for m in _GROUP_MARKERS)]
    ok = e3 is not None and not e3.get("tap_link") and not group_leak
    return ("group_action_no_deeplink", ok,
            f"group action tap_link={e3.get('tap_link') if e3 else 'MISSING'} leaks={group_leak}")


def chk_edge_group_backward_compat(r):
    """A group message that carries NONE of the new Bridge fields (no chat_guid/group_name/
    group_participants) still renders as a group without crashing and does NOT get a topical name —
    it falls back to the sender-derived phone label ('+1 (555) 888-7777')."""
    p = r["prompt"]
    renders = "### +1 (555) 888-7777 [GROUP - no deep link]" in p
    # No thread header may be derived from the group's message topic ("…review the deck…"): the
    # renderer must never mint a header like "### Deck Review" from conversation content.
    topic_header = "### Deck" in p or "### Review" in p
    ok = renders and not topic_header
    return ("group_backward_compat_no_topic", ok,
            f"backward_compat_group_rendered={renders} topic_derived_header={topic_header}")


CHECKS = {
    "rich_day": [chk_no_exception, chk_rich_muted_sender, chk_rich_loops, chk_rich_group_no_deeplink,
                 chk_rich_calendar_linked, chk_rich_coverage, chk_rich_critic_ran, chk_tap_links_wellformed,
                 _mk_present("sections_present", ["Series B wire instructions", "Acme <> Sotto Partnership",
                                                  "coffee Thursday", "Mira Solberg"],
                             "expected rich-day sections")],
    "quiet_day": [chk_no_exception, chk_quiet_critic_skipped, chk_quiet_coverage, chk_quiet_empties,
                  chk_tap_links_wellformed],
    "edge_day": [chk_no_exception, chk_edge_malformed_untouched, chk_edge_muted_person,
                 chk_edge_expired_loop, chk_edge_empty_google_coverage, chk_edge_unicode,
                 chk_edge_phone_only, chk_tap_links_wellformed, chk_edge_group_names_not_invented,
                 chk_edge_group_no_deeplink, chk_edge_group_backward_compat],
}


def evaluate(name: str):
    """Run the pipeline for a fixture and return (result, [(check_name, passed, detail), ...])."""
    r = run_pipeline(name)
    checks = []
    for fn in CHECKS[name]:
        try:
            checks.append(fn(r))
        except Exception as e:                         # a check that itself blows up = a failure
            checks.append((getattr(fn, "__name__", "check"), False, f"check raised: {e!r}"))
    return r, checks


# ── Deterministic scorecard ───────────────────────────────────────────────────────────────────────

def run_deterministic() -> int:
    all_checks = []
    print("\nSotto brief-quality evals — DETERMINISTIC (offline)\n" + "=" * 64)
    for name in FIXTURES:
        _, checks = evaluate(name)
        passed = sum(1 for _, ok, _ in checks if ok)
        print(f"\n{name}  ({passed}/{len(checks)} invariants)")
        for cname, ok, detail in checks:
            mark = "PASS" if ok else "FAIL"
            line = f"  [{mark}] {cname}"
            if not ok:
                line += f"  — {detail}"
            print(line)
            all_checks.append((name, cname, ok, detail))
    total = len(all_checks)
    passed = sum(1 for _n, _c, ok, _d in all_checks if ok)
    failed = [(n, c, d) for n, c, ok, d in all_checks if not ok]
    print("\n" + "=" * 64)
    print(f"SCORECARD: {passed}/{total} invariants passed across {len(FIXTURES)} fixtures")
    if failed:
        print(f"FAILURES ({len(failed)}):")
        for n, c, d in failed:
            print(f"  - {n}/{c}: {d}")
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


# ── Live scored mode (human-invoked; needs GOOGLE_AI_API_KEY) ──────────────────────────────────────

def _score_live(name: str) -> dict:
    """Real Gemini extraction for a fixture, scored by the pack's own brief critic (run_critic).
    Returns {score, model}. Reuses compose_brief.call_gemini + run_critic — no reinvented scorer."""
    with open(os.path.join(FIX_DIR, name + ".json"), encoding="utf-8") as f:
        fixture = json.load(f)
    base = _base_datetime()
    fx = resolve_tokens(fixture, base)
    sandbox, _ = _seed_sandbox(fx)
    saved = {k: os.environ.get(k) for k in ("SOTTO_DATA", "SOTTO_TIMEZONE", "SOTTO_LLM_STUB")}
    try:
        os.environ["SOTTO_DATA"] = sandbox
        os.environ["SOTTO_TIMEZONE"] = "+00:00"
        os.environ.pop("SOTTO_LLM_STUB", None)         # force the real model
        inputs = fx["inputs"]
        out = cb.compose(inputs, critic=False)         # real extraction, no revise
        manifest = cb.build_data_manifest(inputs)
        critic = cb.run_critic(out.get("brief_markdown", ""), out.get("actions", []), manifest, cb.call_gemini)
        return {"score": float(critic.get("score", -1)),
                "model": os.environ.get("SOTTO_GEMINI_MODEL", "gemini-3-flash-preview")}
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(sandbox, ignore_errors=True)


def run_live(threshold: float, update: bool) -> int:
    if not os.environ.get("GOOGLE_AI_API_KEY"):
        print("--live requires GOOGLE_AI_API_KEY (the host's native Gemini key). Aborting.")
        return 2
    scores_path = _scores_path()
    baseline = {}
    if os.path.exists(scores_path):
        with open(scores_path, encoding="utf-8") as f:
            baseline = json.load(f)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    scores, regressions = {}, []
    print("\nSotto brief-quality evals — LIVE (scored by the brief critic)\n" + "=" * 64)
    for name in FIXTURES:
        s = _score_live(name)
        scores[name] = {"score": s["score"], "date": today, "model": s["model"]}
        prev = (baseline.get(name) or {}).get("score")
        delta = "" if prev is None else f"  (baseline {prev:+.1f} → Δ {s['score'] - prev:+.1f})"
        print(f"  {name}: {s['score']:.1f}{delta}")
        if prev is not None and (prev - s["score"]) > threshold:
            regressions.append((name, prev, s["score"]))
    if update:
        os.makedirs(_baselines_dir(), exist_ok=True)
        with open(scores_path, "w", encoding="utf-8") as f:
            json.dump(scores, f, indent=2, sort_keys=True)
        print(f"\nBaseline rewritten → {scores_path}")
        return 0
    print("\n" + "=" * 64)
    if regressions:
        print(f"REGRESSIONS beyond {threshold} pts:")
        for n, prev, cur in regressions:
            print(f"  - {n}: {prev:.1f} → {cur:.1f}")
        print("RESULT: FAIL")
        return 1
    if not baseline:
        print("No baseline yet — run with --update-baseline to record one.")
    print("RESULT: PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Sotto brief-quality eval harness.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--deterministic", action="store_true", help="offline invariant suite (default)")
    mode.add_argument("--live", action="store_true", help="real Gemini extraction, scored by the brief critic")
    ap.add_argument("--threshold", type=float, default=1.0, help="live: max allowed score drop before failing")
    ap.add_argument("--update-baseline", dest="update", action="store_true", help="live: rewrite the stored baseline")
    args = ap.parse_args()
    if args.live:
        return run_live(args.threshold, args.update)
    return run_deterministic()


if __name__ == "__main__":
    sys.exit(main())
