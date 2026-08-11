"""The docs drift-guard — a stale doc is a failing suite, not a hope.

Both playgrounds render every numeric rule from ONE embedded data island:

    <script type="application/json" id="sotto-rules"> … </script>

This file is the other half of that design. It loads the island out of each HTML file, imports the
real constants from the tree, and compares them field by field. It also asserts that
`docs/HOW-SOTTO-DECIDES.md` still states each of those numbers in plain text — the pages and the
prose doc are two renderings of the same claims, so they drift the same way.

CLAUDE.md's docs-stay-true rule names this file as the enforcement layer: a behaviour change
**updates the playground's rules island in the same commit**, or this suite goes red.

WHERE THE ASSERTIONS LIVE — all of them, here. The receiver-side constants (calcache's tap cap and
refresh cadence, receiver's fork budgets and valve heartbeat) are imported across the tree the same
way `tests/test_trigger_receiver_claim.py` already imports `receiver.py`. Splitting the file across
the two suites would make two half-guards that can each pass while a page still lies: the island is
ONE document making ONE claim, and one claim gets one guard.

WHAT IS DELIBERATELY NOT GUARDED HERE (prose that is prose):
  * structural counts the pages compute from their own data — "five modules", "four daemon
    threads", "sixteen skills", "the seven loops", "6 stations". They are rendered from the
    page's own arrays, so they cannot go stale against themselves.
  * illustrations, not knobs — "three calls in ten minutes is one nudge", "a three-hour meeting",
    "notices at 2:37, six minutes past the grace period", "two concurrent spends each reading 3
    and writing 4".
  * unbuilt work — the Step 3 matcher's "24-hour window", "three golden days", "under five
    seconds". There is no constant to drift from yet.
  * the OTP/shortcode digit ranges ("4-8 digit code", "3-6 digit"). Those live inside regex
    literals, not named constants; asserting them would mean parsing `triage_event.py`'s regex
    source, which is a worse guard than the funnel tests that exercise the behaviour directly.
"""
import importlib.util
import inspect
import json
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.abspath(os.path.join(HERE, ".."))          # sotto-chief-of-staff/
HERMES = os.path.abspath(os.path.join(PACK, ".."))        # sotto-hermes/
DOCS = os.path.join(HERMES, "docs")

RULE = ("CLAUDE.md § the docs-stay-true rule: every behaviour change ships with its documentation "
        "in the same commit — update the playground's rules island in the same commit as the code.")


# ── loading ─────────────────────────────────────────────────────────────────────────────────────

def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(*parts))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


te = _load("dd_triage", PACK, "event-triage", "scripts", "triage_event.py")
dc = _load("dd_digest", PACK, "event-triage", "scripts", "digest_check.py")
cr = _load("dd_continuity", PACK, "morning-brief", "scripts", "continuity_resolve.py")
ps = _load("dd_proactive", PACK, "proactive", "scripts", "proactive_scan.py")
sx = _load("dd_style_extract", PACK, "_shared", "scripts", "style_extract.py")
lp = _load("dd_learn_prefs", PACK, "approval-tiers", "scripts", "learn_preferences.py")
rec = _load("dd_receiver", HERMES, "runtime", "trigger-receiver", "receiver.py")
cal = _load("dd_calcache", HERMES, "runtime", "trigger-receiver", "calcache.py")
dsh = _load("dd_dashboard", HERMES, "runtime", "trigger-receiver", "dashboard.py")

ISLAND_RE = re.compile(
    r'<script\s+type="application/json"\s+id="sotto-rules">(.*?)</script>', re.S)


def _island(filename):
    path = os.path.join(DOCS, filename)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    found = ISLAND_RE.findall(src)
    assert len(found) == 1, f"{filename}: expected exactly one #sotto-rules island, found {len(found)}"
    return json.loads(found[0])


ARCH = _island("playground-architecture.html")
LOOPS = _island("playground-feedback-loops.html")
R = ARCH   # the two are asserted identical below; read from one of them everywhere else

with open(os.path.join(DOCS, "HOW-SOTTO-DECIDES.md"), encoding="utf-8") as _f:
    DECIDES = re.sub(r"\s+", " ", _f.read())


# ── env-default extraction ──────────────────────────────────────────────────────────────────────

def _env_default(mod, name):
    """The default a module pairs with `SOTTO_*`, as an int.

    Two shapes cover the tree: `_int_env("NAME", <default>)` and `os.environ.get("NAME"…) or
    <default>`; the default may be a literal or a module constant. Every occurrence must agree —
    a module that reads one knob with two different defaults IS the bug this catches.
    """
    flat = re.sub(r"\s+", " ", inspect.getsource(mod))
    q = re.escape(name)
    call = re.compile(r"_int_env\(\s*[\"']" + q + r"[\"']\s*,\s*([A-Z_][A-Z_0-9]*|\d+)\s*\)")
    # only consulted when the knob is read without the _int_env helper — `… or <default>)`
    fallback = re.compile(r"[\"']" + q + r"[\"'].{0,140}?\bor\s+([A-Z_][A-Z_0-9]*|\d+)\b")

    def _resolve(tok):
        return int(tok) if tok.isdigit() else int(getattr(mod, tok))

    found = {_resolve(m.group(1)) for m in call.finditer(flat)}
    if not found:
        found = {_resolve(m.group(1)) for m in fallback.finditer(flat)
                 if m.group(1).isdigit() or isinstance(getattr(mod, m.group(1), None), int)}
    assert len(found) == 1, (
        f"{name}: expected ONE default in {mod.__name__}, found {sorted(found) or 'none'}")
    return found.pop()


def _same(field, page, code):
    assert page == code, (
        f"docs drift — the playgrounds' rules island says {field} = {page!r}, the code says "
        f"{code!r}.\n{RULE}")


def _anchor(text):
    assert text in DECIDES, (
        f"docs drift — docs/HOW-SOTTO-DECIDES.md no longer states {text!r}.\n{RULE}")


# ── the island itself ───────────────────────────────────────────────────────────────────────────

def test_both_playgrounds_carry_the_same_island():
    """One shape in both files. A half-update — the funnel page corrected, the loops page not —
    is exactly the drift the island exists to make impossible."""
    assert ARCH == LOOPS, (
        "docs drift — playground-architecture.html and playground-feedback-loops.html carry "
        f"different #sotto-rules islands.\n{RULE}")


def test_island_is_flat_json_of_numbers_and_sets():
    """No prose, no HTML, no nesting beyond one level of grouping (plus the two maps that ARE
    maps in code: style.ttl_days and classes.*). Keeps the island reviewable at a glance."""
    for group, body in R.items():
        assert isinstance(body, dict), f"{group}: island groups are objects"
        for key, val in body.items():
            assert isinstance(val, (int, float, str, list, dict)), f"{group}.{key}"
            if isinstance(val, list):
                assert all(isinstance(x, str) for x in val), f"{group}.{key}: sets are string lists"
            if isinstance(val, dict):
                assert all(isinstance(x, (int, float)) for x in val.values()), f"{group}.{key}"


# ── the funnel's volume controls ────────────────────────────────────────────────────────────────

def test_daily_interrupt_budget():
    _same("budget.nudge_per_day", R["budget"]["nudge_per_day"],
          _env_default(te, "SOTTO_NUDGE_BUDGET"))
    # the dashboard's Cadence page reads the same knob — two readers, one default
    _same("budget.nudge_per_day (dashboard)", R["budget"]["nudge_per_day"],
          _env_default(dsh, "SOTTO_NUDGE_BUDGET"))


def test_post_meeting_tap_cap_and_grace():
    _same("tap.max_per_day", R["tap"]["max_per_day"], cal.TAP_MAX_PER_DAY_DEFAULT)
    _same("tap.max_per_day (env)", R["tap"]["max_per_day"],
          _env_default(cal, "SOTTO_TAP_MAX_PER_DAY"))
    _same("tap.max_per_day (dashboard)", R["tap"]["max_per_day"],
          _env_default(dsh, "SOTTO_TAP_MAX_PER_DAY"))
    _same("tap.grace_min", R["tap"]["grace_min"], cal.TAP_GRACE_MIN_DEFAULT)


def test_cooldown_escalation_and_freshness():
    _same("cooldown.min", R["cooldown"]["min"], _env_default(te, "SOTTO_EVENT_COOLDOWN_MIN"))
    _same("escalation.window_min", R["escalation"]["window_min"], te.ESCALATION_WINDOW_MIN_DEFAULT)
    _same("freshness.event_max_age_min", R["freshness"]["event_max_age_min"],
          te.EVENT_MAX_AGE_MIN)


def test_quiet_hours_and_vip_floor():
    _same("quiet.start_hour", R["quiet"]["start_hour"], _env_default(te, "SOTTO_QUIET_START"))
    _same("quiet.end_hour", R["quiet"]["end_hour"], _env_default(te, "SOTTO_QUIET_END"))
    # the dashboard renders the same window on the Cadence page
    _same("quiet.start_hour (dashboard)", R["quiet"]["start_hour"],
          _env_default(dsh, "SOTTO_QUIET_START"))
    _same("quiet.end_hour (dashboard)", R["quiet"]["end_hour"],
          _env_default(dsh, "SOTTO_QUIET_END"))
    _same("vip.min_priority", R["vip"]["min_priority"], te.VIP_PRIORITY_MIN)


def test_tier1_prompt_cap():
    _same("tier1.text_max_chars", R["tier1"]["text_max_chars"], te.TIER1_TEXT_MAX)


def test_release_valve():
    _same("valve.max_per_tick", R["valve"]["max_per_tick"], te.VALVE_MAX_PER_TICK)
    _same("valve.max_per_hour", R["valve"]["max_per_hour"], te.VALVE_MAX_PER_HOUR)
    # the dashboard keeps its own named mirror for the Cadence panel
    _same("valve.max_per_hour (dashboard)", R["valve"]["max_per_hour"],
          dsh.VALVE_MAX_PER_HOUR_DEFAULT)
    _same("valve.max_age_min", R["valve"]["max_age_min"], te.VALVE_MAX_AGE_MIN)
    # dashboard.py keeps its own named mirror for the Cadence waiting room
    _same("valve.max_age_min (dashboard)", R["valve"]["max_age_min"], dsh.VALVE_MAX_AGE_MIN_DEFAULT)
    _same("intervals.valve_interval_secs", R["intervals"]["valve_interval_secs"],
          rec.VALVE_INTERVAL_SECS_DEFAULT)


def test_calendar_cache_and_staleness():
    _same("calendar.ttl_secs", R["calendar"]["ttl_secs"], cal.CALENDAR_TTL_SECS)
    _same("calendar.stale_intervals", R["calendar"]["stale_intervals"], te.CALENDAR_STALE_INTERVALS)
    _same("intervals.calendar_refresh_secs", R["intervals"]["calendar_refresh_secs"],
          cal.REFRESH_SECS_DEFAULT)
    # triage_event's docstring calls its copy a mirror — hold it to that
    _same("intervals.calendar_refresh_secs (funnel mirror)",
          R["intervals"]["calendar_refresh_secs"], te.CALENDAR_REFRESH_SECS_DEFAULT)


def test_subprocess_budgets_and_poll_cadences():
    _same("intervals.triage_fork_secs", R["intervals"]["triage_fork_secs"], rec.TRIAGE_TIMEOUT_SECS)
    _same("intervals.calendar_gather_fork_secs", R["intervals"]["calendar_gather_fork_secs"],
          cal.GATHER_TIMEOUT_SECS)
    _same("intervals.dashboard_cli_fork_secs", R["intervals"]["dashboard_cli_fork_secs"],
          dsh.EDIT_TIMEOUT_SECS)
    _same("intervals.email_poll_secs", R["intervals"]["email_poll_secs"],
          _env_default(rec, "SOTTO_EMAIL_POLL_SECS"))
    gmail = re.findall(r"timeout=(\d+)", inspect.getsource(rec._poll_gmail_once))
    _same("intervals.gmail_fork_secs", [R["intervals"]["gmail_fork_secs"]], [int(x) for x in gmail])


def test_bridge_event_tick():
    """The Bridge half is Rust, and it does not ship in the public tree — so assert against the
    engine when we have it, and against the env table (which always ships) either way."""
    row = re.search(r"`SOTTO_EVENTS_TICK_SECS`.*?\(default `(\d+)`\)",
                    open(os.path.join(HERMES, "RAILWAY.md"), encoding="utf-8").read())
    assert row, "RAILWAY.md no longer documents SOTTO_EVENTS_TICK_SECS's default"
    _same("intervals.events_tick_secs (RAILWAY env table)",
          R["intervals"]["events_tick_secs"], int(row.group(1)))
    watcher = os.path.join(HERMES, "sotto-bridge", "core", "src", "watcher.rs")
    if not os.path.exists(watcher):
        pytest.skip("sotto-bridge/ is excluded from the distribution tree")
    with open(watcher, encoding="utf-8") as f:
        body = re.search(r"pub fn tick_secs\(\).*?\n}", f.read(), re.S)
    assert body, "watcher.rs no longer defines tick_secs()"
    _same("intervals.events_tick_secs", R["intervals"]["events_tick_secs"],
          int(re.search(r"unwrap_or\((\d+)\)", body.group(0)).group(1)))


# ── the digest, the ledgers, and the class sets ─────────────────────────────────────────────────

def test_digest_threshold():
    _same("digest.min_signals", R["digest"]["min_signals"], _env_default(dc, "SOTTO_DIGEST_MIN"))


def test_ledger_bounds():
    mb, lines = R["ledger"]["max_mb"], R["ledger"]["keep_lines"]
    _same("ledger.max_mb (queue)", mb * 1024 * 1024, te.QUEUE_MAX_BYTES)
    _same("ledger.keep_lines (queue)", lines, te.QUEUE_KEEP_LINES)
    _same("ledger.max_mb (surfaced)", mb * 1024 * 1024, te.SURFACED_MAX_BYTES)
    _same("ledger.keep_lines (surfaced)", lines, te.SURFACED_KEEP_LINES)


def test_class_sets():
    sets = {
        "promotable": te.PROMOTABLE_CLASSES,
        "tier1_nudge": te.ESCALATION_ASK_CLASSES,
        "budget_exempt": te.BUDGET_EXEMPT_CLASSES,
        "meeting_hold_exempt": te.MEETING_HOLD_EXEMPT_CLASSES,
        "cooldown_exempt": te.COOLDOWN_EXEMPT_CLASSES,
        "digest_count": dc.COUNT_CLASSES,
        "digest_actionable": dc.ACTIONABLE_CLASSES,
    }
    assert set(R["classes"]) == set(sets), (
        f"docs drift — the island's class sets are {sorted(R['classes'])}, the code's are "
        f"{sorted(sets)}.\n{RULE}")
    for name, code in sets.items():
        # the island keeps DISPLAY order (the sentence around it reads in that order); the claim
        # being guarded is membership
        _same(f"classes.{name}", set(R["classes"][name]), set(code))


# ── the learning loops ──────────────────────────────────────────────────────────────────────────

def test_style_ttls_and_confirmed_floor():
    day_ms = 24 * 3600 * 1000
    _same("style.ttl_days buckets", set(R["style"]["ttl_days"]), set(sx.ALL_BUCKETS))
    for bucket, days in R["style"]["ttl_days"].items():
        _same(f"style.ttl_days.{bucket}", days * day_ms, sx.CANONICAL_TTL_MS[bucket])
    # the floor is applied inline in score_sample, so probe the behaviour rather than a constant
    _same("style.confirmed_floor", R["style"]["confirmed_floor"],
          sx.score_sample({"text": "ok", "source": "confirmed"}))


def test_learned_approval_default_thresholds():
    _same("approval.min_accepted", R["approval"]["min_accepted"], lp.MIN_ACCEPTED_FOR_DEFAULT)
    _same("approval.min_accept_rate", R["approval"]["min_accept_rate"], lp.MIN_ACCEPT_RATE)


def test_continuity_resolution_windows():
    _same("continuity.deadline_grace_days", R["continuity"]["deadline_grace_days"],
          cr.DEADLINE_GRACE_DAYS)
    _same("continuity.terminal_retention_days", R["continuity"]["terminal_retention_days"],
          cr.TERMINAL_RETENTION_DAYS)


def test_chase_and_birthday_cadence():
    """The two knobs the chase/birthday work added. One writer for the chase constants
    (continuity_resolve), one reader for the lead time (the proactive watcher), one number each."""
    # SOTTO_CHASE_AFTER_DAYS is read through continuity_resolve's own floored `_int(env, DEFAULT)`
    # helper rather than the two shapes _env_default knows, so the named constant IS the assertion.
    _same("chase.after_days", R["chase"]["after_days"], cr.CHASE_AFTER_DAYS)
    _same("chase.max", R["chase"]["max"], cr.CHASE_MAX)
    # the dashboard keeps its own named mirror, so /api/loops can say "chased out"
    _same("chase.max (dashboard)", R["chase"]["max"], dsh.CHASE_MAX_DEFAULT)
    _same("birthday.lead_days", R["birthday"]["lead_days"],
          _env_default(ps, "SOTTO_BIRTHDAY_LEAD_DAYS"))


# ── the schedule ────────────────────────────────────────────────────────────────────────────────

def test_cron_line_matches_crons_json():
    """`adapters/hermes/crons.json` IS the schedule (CLAUDE.md). The architecture page states it in
    prose; the prose reads its times from the island, and the island answers to the file."""
    with open(os.path.join(HERMES, "adapters", "hermes", "crons.json"), encoding="utf-8") as f:
        crons = {c["name"]: c["schedule"] for c in json.load(f)}

    def hhmm(schedule):
        minute, hour = schedule.split()[0], schedule.split()[1]
        return f"{int(hour)}:{int(minute):02d}"

    _same("cron.morning", R["cron"]["morning"], hhmm(crons["sotto-morning-brief"]))
    _same("cron.midday", R["cron"]["midday"], hhmm(crons["sotto-midday-digest"]))
    _same("cron.evening", R["cron"]["evening"], hhmm(crons["sotto-evening-brief"]))
    _same("cron.pulse_time", R["cron"]["pulse_time"], hhmm(crons["sotto-relationship-pulse"]))
    days = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"]
    _same("cron.pulse_day", R["cron"]["pulse_day"],
          days[int(crons["sotto-relationship-pulse"].split()[4])])
    _same("cron.proactive_min", R["cron"]["proactive_min"],
          int(crons["sotto-proactive"].split()[0].lstrip("*/")))
    # the valve heartbeat rides the same */15 cadence the proactive cron does
    _same("cron.proactive_min (valve heartbeat)", R["cron"]["proactive_min"] * 60,
          rec.VALVE_INTERVAL_SECS_DEFAULT)


# ── the prose doc ───────────────────────────────────────────────────────────────────────────────

def test_how_sotto_decides_states_every_number():
    """The playground islands and HOW-SOTTO-DECIDES.md are two renderings of the same claims.
    Anchors are built FROM the code constants, so a knob change breaks the doc, not just the page."""
    budget = _env_default(te, "SOTTO_NUDGE_BUDGET")
    _anchor(f"`SOTTO_NUDGE_BUDGET` (default {budget})")
    _anchor(f"({budget} nudges today)")
    _anchor(f"`SOTTO_TAP_MAX_PER_DAY` (default {cal.TAP_MAX_PER_DAY_DEFAULT})")
    _anchor(f"a {te.ESCALATION_WINDOW_MIN_DEFAULT}-minute window")
    _anchor(f"`SOTTO_EVENT_COOLDOWN_MIN` (default {_env_default(te, 'SOTTO_EVENT_COOLDOWN_MIN')} min)")
    _anchor("(default {:02d}:00–{:02d}:00".format(_env_default(te, "SOTTO_QUIET_START"),
                                                  _env_default(te, "SOTTO_QUIET_END")))
    _anchor(f"up to {te.VALVE_MAX_PER_TICK} queued events per tick")
    _anchor(f"at most {te.VALVE_MAX_PER_HOUR}/hour")
    _anchor(f"younger than {te.VALVE_MAX_AGE_MIN // 60}h")
    _anchor(f"`SOTTO_DIGEST_MIN` (default {_env_default(dc, 'SOTTO_DIGEST_MIN')})")
    _anchor(f"older than {te.EVENT_MAX_AGE_MIN} min")
    _anchor(f"`SOTTO_CHASE_AFTER_DAYS` (default {cr.CHASE_AFTER_DAYS})")
    _anchor(f"at most {cr.CHASE_MAX} per item")
    _anchor(f"after {cr.AGE_EXPIRY_DAYS} silent days")
    _anchor(f"`SOTTO_CALENDAR_REFRESH_SECS` (default {cal.REFRESH_SECS_DEFAULT // 60} min)")
    _anchor(f"`SOTTO_EMAIL_POLL_SECS` (default {_env_default(rec, 'SOTTO_EMAIL_POLL_SECS')}s)")
    _anchor(f"`SOTTO_EVENTS_TICK_SECS`, default {R['intervals']['events_tick_secs']}s")
    _anchor(" · ".join(f"`{c}`" for c in R["classes"]["tier1_nudge"]) + " → nudge")


# ── the vendored key module, which exists in two processes ──────────────────────────────────────

def test_the_key_module_is_vendored_byte_for_byte():
    """queue_key / sample_hash are ids two RUNTIMES must agree on: the skills tree mints them
    (`triage_event --promote`, `style_extract --confirm`) and the receiver's dashboard renders the
    entries the user clicks. The receiver image must work with no skills tree on the box, so it
    carries a VENDORED copy of `_shared/lib/keys.py` rather than importing it — and this asserts
    the copy is byte-identical, so nobody hand-maintains two copies of a hash algorithm again.
    Change one, copy it across, in the same commit."""
    canonical = os.path.join(PACK, "_shared", "lib", "keys.py")
    vendored = os.path.join(HERMES, "runtime", "trigger-receiver", "keys.py")
    with open(canonical, "rb") as f:
        a = f.read()
    with open(vendored, "rb") as f:
        b = f.read()
    assert a == b, (
        "docs drift — runtime/trigger-receiver/keys.py is no longer a byte-identical copy of "
        f"sotto-chief-of-staff/_shared/lib/keys.py.\n{RULE}")


# ── the relation vocabulary, which exists in two processes ──────────────────────────────────────

def test_the_relation_sentences_are_the_same_table_in_both_processes():
    """A relation reads as ONE sentence, and it is rendered in two places: the skills tree packs it
    for the LLM, the receiver's dashboard renders it on the person page. The receiver image cannot
    import the skills tree, so the table is duplicated exactly once — and this is the guard that
    keeps the copy honest. Change kg.RELATION_SENTENCE, change dashboard.RELATION_SENTENCE, in the
    same commit."""
    import knowledge as kg  # noqa: PLC0415 — the pack's lib, on conftest's path
    assert dsh.RELATION_SENTENCE == kg.RELATION_SENTENCE, RULE
    # …and the dashboard renders exactly the vocabulary the writer can store — no more, no less.
    assert set(dsh.RELATION_SENTENCE) == set(kg.RELATION_INVERSE)
    for rel_type in kg.RELATION_INVERSE:
        assert dsh.relation_sentence(rel_type, "Vishnu Sharma", "2026-05-14") == \
            kg.relation_sentence(rel_type, "Vishnu Sharma", "2026-05-14")
    # every inverse is itself in the vocabulary, and inverting twice is identity
    for rel_type, inverse in kg.RELATION_INVERSE.items():
        assert kg.RELATION_INVERSE[inverse] == rel_type
