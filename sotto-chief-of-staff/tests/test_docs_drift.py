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
import ast
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
rec = _load("dd_receiver", HERMES, "runtime", "trigger-receiver", "receiver.py")
cal = _load("dd_calcache", HERMES, "runtime", "trigger-receiver", "calcache.py")
ob = _load("dd_outbox", HERMES, "runtime", "trigger-receiver", "outbox.py")
rt = _load("dd_retention", HERMES, "runtime", "trigger-receiver", "retention.py")
fg = _load("dd_forget", PACK, "tools", "forget.py")
dsh = _load("dd_dashboard", HERMES, "runtime", "trigger-receiver", "dashboard.py")
att = _load("dd_attachments", PACK, "_shared", "lib", "attachments.py")
bv = _load("dd_brief_validate", PACK, "_shared", "lib", "brief_validate.py")
on = _load("dd_onboarding", HERMES, "runtime", "trigger-receiver", "onboarding.py")
br = _load("dd_brief_runner", HERMES, "runtime", "trigger-receiver", "brief_runner.py")
mc = _load("dd_memory_cycle", PACK, "_shared", "scripts", "memory_cycle.py")
pc = _load("dd_personal_context", PACK, "_shared", "lib", "personal_context.py")

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


def _literal_constant(path, name):
    """Read one module-level literal without importing a service and its runtime dependencies."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    values = [ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, (ast.Assign, ast.AnnAssign))
              for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
              if isinstance(target, ast.Name) and target.id == name]
    assert len(values) == 1, f"expected one literal {name} in {path}, found {len(values)}"
    return values[0]


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
    _same("freshness.missed_call_max_age_min", R["freshness"]["missed_call_max_age_min"],
          te.MISSED_CALL_MAX_AGE_MIN)
    # One clock, stated in the docs: the missed-call ceiling IS the valve's age cap — a held nudge
    # and a stale call age out together. If either constant moves alone, the doctrine broke.
    assert te.MISSED_CALL_MAX_AGE_MIN == te.VALVE_MAX_AGE_MIN, RULE
    _anchor(f"a missed call buzzes up to {te.MISSED_CALL_MAX_AGE_MIN // 60} hours after the ring")


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


def test_delivery_outbox():
    """The outbox's retry numbers, and the one it is not allowed to invent.

    A queued nudge and a held nudge are the same nudge at two different moments, so they must age
    out on the SAME clock: `outbox.NUDGE_MAX_AGE_MIN` mirrors `triage_event.VALVE_MAX_AGE_MIN` for
    the same copy-plus-guard reason keys.py and the dashboard's mirrors exist — the receiver image
    cannot import the skills tree, so the constant is duplicated exactly once and this is what keeps
    the copies honest. The day-long expiries (a brief, the digest) are a date comparison against the
    local day, not a number, so there is nothing here for them to drift from."""
    _same("outbox.drain_interval_secs", R["outbox"]["drain_interval_secs"], ob.DRAIN_INTERVAL_SECS)
    _same("outbox.backoff_base_secs", R["outbox"]["backoff_base_secs"], ob.BACKOFF_BASE_SECS)
    _same("outbox.backoff_max_secs", R["outbox"]["backoff_max_secs"], ob.BACKOFF_MAX_SECS)
    _same("outbox.max_attempts", R["outbox"]["max_attempts"], ob.MAX_ATTEMPTS)
    _same("outbox.effect_attempts", R["outbox"]["effect_attempts"], ob.MAX_EFFECT_ATTEMPTS)
    _same("outbox.nudge_max_age_min", R["outbox"]["nudge_max_age_min"], ob.NUDGE_MAX_AGE_MIN)
    _same("outbox.nudge_max_age_min (the funnel's own window)", ob.NUDGE_MAX_AGE_MIN,
          te.VALVE_MAX_AGE_MIN)
    _anchor("retries every minute")
    # The send seam's two "did this run actually do the work" checks share one window: the
    # composed archive (a body is a brief only if compose_brief archived one) and the Learn receipt.
    _same("outbox.composed_brief_max_age_hours", R["outbox"]["composed_brief_max_age_hours"] * 3600,
          rec.COMPOSED_BRIEF_MAX_AGE_SECS)
    _same("work.max_attempts", R["work"]["max_attempts"], rec.WORK_QUEUE.MAX_ATTEMPTS)
    _same("work.max_workers", R["work"]["max_workers"], rec.WORK_QUEUE.MAX_WORKERS)
    _same("work.background_max_wait_seconds", R["work"]["background_max_wait_seconds"],
          rec.WORK_QUEUE.BACKGROUND_MAX_WAIT_SECONDS)
    _same("work.lease_seconds", R["work"]["lease_seconds"], rec.WORK_QUEUE.LEASE_SECONDS)
    _same("work.prepare_minutes", R["work"]["prepare_minutes"], rec.BRIEF_PREPARE_SECONDS // 60)
    _same("work.compose_lead_seconds", R["work"]["compose_lead_seconds"],
          rec.BRIEF_COMPOSE_LEAD_SECONDS)
    _same("valve.deadline_horizon_hours", R["valve"]["deadline_horizon_hours"] * 3600,
          te.ASK_DEADLINE_HORIZON_SECONDS)
    _same("shared actionable deadline horizon", te.ASK_DEADLINE_HORIZON_SECONDS,
          bv.ACTION_DEADLINE_HORIZON_SECONDS)
    _same("work.catchup_hours", R["work"]["catchup_hours"], rec.DAILY_CATCHUP_SECONDS // 3600)
    _same("work.retention_days", R["work"]["retention_days"], rec.WORK_QUEUE.RETENTION_SECONDS // 86400)
    _anchor("at most three execution attempts")


def test_onboarding_brief_and_learning_caps():
    _same('onboarding.lease_minutes', R['onboarding']['lease_minutes'], on.LEASE_SECONDS // 60)
    _same('onboarding.retry_minutes', R['onboarding']['retry_minutes'], on.RETRY_SECONDS // 60)
    _same('onboarding.attempts_per_day', R['onboarding']['attempts_per_day'], on.ATTEMPTS_PER_DAY)
    _same('brief.preparation_max_age_minutes', R['brief']['preparation_max_age_minutes'],
          br.PREPARATION_MAX_AGE_SECONDS // 60)
    _same('brief.notes_cache_hours', R['brief']['notes_cache_hours'], br.NOTES_CACHE_MAX_AGE_SECONDS // 3600)
    # the retention group states the same cache age in days — one number, two renderings
    _same('retention.notes_cache_days', R['retention']['notes_cache_days'] * 86400,
          br.NOTES_CACHE_MAX_AGE_SECONDS)
    _same('brief.welcome_seed_seconds', R['brief']['welcome_seed_seconds'], br.WELCOME_SEED_BUDGET_SECONDS)
    _same('brief.followup_retention_days', R['brief']['followup_retention_days'],
          br.FOLLOWUP_RETENTION_SECONDS // 86400)
    _same('learning.source_retry_seconds', R['learning']['source_retry_seconds'], mc.SOURCE_RETRY_SECONDS)
    _same('learning.unchanged_extraction_attempts', R['learning']['unchanged_extraction_attempts'],
          mc.UNCHANGED_EXTRACTION_ATTEMPTS)
    _same('learning.conversation_messages', R['learning']['conversation_messages'], pc.CONVERSATION_MESSAGES)
    _same('learning.conversation_text_chars', R['learning']['conversation_text_chars'], pc.CONVERSATION_TEXT_CHARS)
    _same('learning.conversation_total_chars', R['learning']['conversation_total_chars'], pc.CONVERSATION_TOTAL_CHARS)
    _anchor('five post-delivery effect attempts')
    _anchor('two attempts for the same source revision')


def test_background_cadences_are_stated_in_prose():
    """The cadences that have no island field but are still claims about code: the memory pass
    interval, the two delivery-eligibility windows, and the managed model-lease timings. Each is
    built FROM its constant so a moved knob breaks the doc, not just the page."""
    de = _load("dd_delivery_effects", PACK, "_shared", "lib", "delivery_effects.py")
    ml = _load("dd_model_lease", HERMES, "adapters", "hermes", "model_lease.py")
    _anchor(f"at most once every {rec.MEMORY_INTERVAL_SECONDS // 60} minutes "
            "(`receiver.MEMORY_INTERVAL_SECONDS`)")
    _anchor(f"at most {mc.PAGES_PER_RUN} rotating history pages")
    _anchor(f"younger than {de.CALENDAR_MIN_FRESH_SECONDS} seconds")
    _anchor(f"deliverable for {de.POST_MEETING_VALID_SECONDS // 60} minutes after the meeting ended")
    _anchor(f"within {ml.RENEW_BEFORE_SECONDS // 3600} hours of expiry")
    assert ml.RETRY_SECONDS == 3600, RULE      # "retries hourly" is the sentence; hold it to that
    _anchor("retries hourly (`model_lease.RETRY_SECONDS`)")


def test_account_and_proxy_capacity_guards():
    accounts = os.path.join(HERMES, 'cloud', 'accounts', 'server.py')
    model_proxy = os.path.join(HERMES, 'cloud', 'model-proxy', 'server.py')
    _same('accounts.pending_signins', R['accounts']['pending_signins'],
          _literal_constant(accounts, 'PENDING_SIGNINS'))
    _same('accounts.max_signins', R['accounts']['max_signins'],
          _literal_constant(accounts, 'MAX_SIGNINS'))
    _same('accounts.device_code_attempts', R['accounts']['device_code_attempts'],
          _literal_constant(accounts, 'DEVICE_CODE_ATTEMPTS'))
    _same('proxy.requests_per_window', R['proxy']['requests_per_window'],
          _literal_constant(model_proxy, 'REQUESTS_PER_WINDOW'))
    _same('proxy.rate_window_seconds', R['proxy']['rate_window_seconds'],
          _literal_constant(model_proxy, 'RATE_WINDOW_SECONDS'))
    _anchor('50 unverified pending sign-ins')
    _anchor('500 total sign-in sessions')
    _anchor('five native code attempts')
    _anchor('60 admitted requests per tenant in a rolling 60-second window')


def test_the_learn_step_is_one_command_with_a_receipt():
    """Three loops (knowledge, preferences, style) rest on the Learn step, so the step is machinery:
    learn_step.py runs the six writers and leaves briefs/<day>.<kind>.learned.json, the receiver
    reads it on the ack, and retention ages it with the brief."""
    ls = _load("dd_learn_step", PACK, "_shared", "scripts", "learn_step.py")
    _same("learn.step_timeout_secs", R["learn"]["step_timeout_secs"], ls.STEP_TIMEOUT_SECS)
    assert [name for name, _rel, _b in ls.STEPS] == [
        "knowledge", "continuity", "drafts", "style", "granola", "contacts"], RULE
    assert ls.receipt_path("2026-09-04", "morning").endswith("briefs/2026-09-04.morning.learned.json")
    assert isinstance(rt.accounts_for("briefs/2026-09-04.morning.learned.json"), rt.Rule), RULE
    _anchor("one command")
    _anchor("learned.json")
    _anchor("doubling to a fifteen-minute cap")
    _anchor("older than 240 minutes")
    # deliver-once is MACHINERY at the send seam, not an instruction in a skill (Aug 30)
    _same("outbox.superseded (the terminal the gate writes)", ob.STATUS_SUPERSEDED, "superseded")
    _anchor("the send seam itself claims the deliver-once marker")
    _anchor(f"receipted `{ob.STATUS_SUPERSEDED}`, never sent")


def test_calendar_cache_and_staleness():
    _same("calendar.ttl_secs", R["calendar"]["ttl_secs"], cal.CALENDAR_TTL_SECS)
    _same("calendar.stale_intervals", R["calendar"]["stale_intervals"], te.CALENDAR_STALE_INTERVALS)
    _same("calendar.change_window_hours", R["calendar"]["change_window_hours"],
          cal.CHANGE_WINDOW_HOURS)
    _same("calendar.decline_window_hours", R["calendar"]["decline_window_hours"],
          cal.DECLINE_WINDOW_HOURS)
    _same("calendar.invite_soon_hours", R["calendar"]["invite_soon_hours"], cal.INVITE_SOON_HOURS)
    _same("calendar.invite_grace_min", R["calendar"]["invite_grace_min"], cal.INVITE_GRACE_MIN)
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
    # the one bucket the draft→outcome matcher grows automatically is capped where it grows, and
    # the retention exemption states the same number
    _same("style.confirmed_cap", R["style"]["confirmed_cap"], sx.CONFIRMED_CAP)
    assert f"newest {sx.CONFIRMED_CAP}" in rt.accounts_for("style.json").why, RULE
    _anchor(f"newest {sx.CONFIRMED_CAP}")


def test_never_tell_you_twice_is_measured():
    """The 'Already Nudged Today' block used to be a prompt with no measurement behind it; rule (h)
    is the measurement, and its one number is stated on both pages."""
    bv = _load("dd_brief_validate", PACK, "_shared", "lib", "brief_validate.py")
    _same("surfacing.already_nudged_max_lines", R["surfacing"]["already_nudged_max_lines"],
          bv.ALREADY_NUDGED_MAX_LINES)
    _anchor(f"at most {bv.ALREADY_NUDGED_MAX_LINES} lines")


def test_x_reads_a_bounded_page_of_posts():
    xc = _load("dd_x_connectivity", PACK, "_shared", "scripts", "x_connectivity.py")
    _same("x.max_recent_posts", R["x"]["max_recent_posts"], xc.MAX_RECENT_POSTS)


def test_the_bridge_daily_contacts_subset():
    """The Bridge half is Rust and does not ship in the public tree — assert against the engine
    when we have it (same posture as test_bridge_event_tick)."""
    readers = os.path.join(HERMES, "sotto-bridge", "core", "src", "readers.rs")
    if not os.path.exists(readers):
        pytest.skip("sotto-bridge/ is excluded from the distribution tree")
    with open(readers, encoding="utf-8") as f:
        body = f.read()
    for field, const in (("subset_max_hours", "CONTACTS_SUBSET_MAX_HOURS"),
                         ("birthday_lookahead_days", "BIRTHDAY_LOOKAHEAD_DAYS")):
        m = re.search(rf"pub const {const}: i64 = (\d+);", body)
        assert m, f"readers.rs no longer defines {const}"
        _same(f"contacts.{field}", R["contacts"][field], int(m.group(1)))




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


# ── the attachment lane ─────────────────────────────────────────────────────────────────────────

def test_the_stale_sent_lane_and_the_rsvp_ask():
    """Three rules a chief of staff would not need told (Sep 2026): an email you sent that nobody
    answered is a debt owed to you; a meeting you declined is not on your day; a meeting you haven't
    answered is an ask. The stale lane's silence clock IS the chase clock — one number, twice."""
    gg = _load("dd_gather_google", PACK, "_shared", "scripts", "gather_google.py")
    cb = _load("dd_compose_brief", PACK, "_shared", "scripts", "compose_brief.py")
    _same("stale.silent_days", R["stale"]["silent_days"], gg.STALE_SILENT_DAYS)
    _same("stale.silent_days (the chase clock)", gg.STALE_SILENT_DAYS, cr.CHASE_AFTER_DAYS)
    _same("stale.max_days", R["stale"]["max_days"], gg.STALE_MAX_DAYS)
    _same("stale.max_threads", R["stale"]["max_threads"], gg.STALE_MAX_THREADS)
    _same("calendar.rsvp_ask_hours", R["calendar"]["rsvp_ask_hours"], cb.RSVP_ASK_HOURS)
    # The mute producer is paused; no active cooldown may be advertised by the rules islands.
    assert "mute_offer" not in R
    assert not hasattr(cb, "MUTE_OFFER_COOLDOWN_DAYS")
    _anchor(f"{gg.STALE_SILENT_DAYS} to {gg.STALE_MAX_DAYS} days ago")
    _anchor(f"{gg.STALE_MAX_THREADS} threads at most")
    _anchor(f"within {cb.RSVP_ASK_HOURS} hours")
    _anchor("A meeting you declined is not on your day")
    _anchor("They replied, they just haven't delivered")


def test_attachment_caps():
    """The three caps of the attachment lane, whose ONE owner is `_shared/lib/attachments.py` —
    gather_google imports them from there rather than declaring its own, so there is exactly one
    number per cap in the tree and the island states that number."""
    _same("attachment.per_email", R["attachment"]["per_email"], att.MAX_ATTACHMENTS_PER_EMAIL)
    _same("attachment.max_bytes", R["attachment"]["max_bytes"], att.MAX_ATTACHMENT_BYTES)
    _same("attachment.max_chars", R["attachment"]["max_chars"], att.MAX_ATTACHMENT_CHARS)
    _same("attachment.per_brief", R["attachment"]["per_brief"], att.MAX_ATTACHMENT_CHARS_PER_BRIEF)


def test_the_attachment_caps_have_no_second_declaration():
    """`attachments.py` owns the caps for BOTH the fetch side and the prompt side. A sibling that
    re-declared one would pass the island check above while quietly enforcing a different number —
    so the guard is that no other file declares them at all."""
    for rel in (("_shared", "scripts", "gather_google.py"), ("_shared", "lib", "render_local.py")):
        with open(os.path.join(PACK, *rel), encoding="utf-8") as f:
            src = f.read()
        assert "from attachments import" in src, (rel, "must import the caps, not restate them")
        assert not re.search(r"^MAX_ATTACHMENT\w*\s*=", src, re.M), (
            f"{'/'.join(rel)} declares its own attachment cap — attachments.py is the one owner.\n{RULE}")


# ── the retention sweep ─────────────────────────────────────────────────────────────────────────

def test_retention_ttls():
    """`retention.py` holds THE table of what the volume keeps; the island and DATA-FLOW.md are its
    two renderings. Every number here is a promise made to a reader about their own data."""
    _same("retention.sweep_hour", R["retention"]["sweep_hour"], rt.SWEEP_LOCAL[0])
    _same("retention.sweep_minute", R["retention"]["sweep_minute"], rt.SWEEP_LOCAL[1])
    _same("retention.delivery_receipt_days", R["retention"]["delivery_receipt_days"],
          rt.DELIVERY_RECEIPT_DAYS)
    _same("retention.send_receipt_days", R["retention"]["send_receipt_days"], rt.SEND_RECEIPT_DAYS)
    _same("retention.triage_verdict_days", R["retention"]["triage_verdict_days"],
          rt.TRIAGE_VERDICT_DAYS)
    _same("retention.dashboard_audit_days", R["retention"]["dashboard_audit_days"],
          rt.DASHBOARD_AUDIT_DAYS)
    _same("retention.draft_ledger_days", R["retention"]["draft_ledger_days"], rt.DRAFT_LEDGER_DAYS)
    _same("retention.brief_archive_days", R["retention"]["brief_archive_days"],
          rt.BRIEF_ARCHIVE_DAYS)
    _same("retention.staged_days", R["retention"]["staged_days"], rt.STAGED_DAYS)
    _same("retention.proactive_stamp_days", R["retention"]["proactive_stamp_days"],
          rt.PROACTIVE_STAMP_DAYS)
    _same("retention.log_tail_mb", R["retention"]["log_tail_mb"],
          rt.LOG_TAIL_MAX_BYTES // (1024 * 1024))
    _same("retention.outcome_days", R["retention"]["outcome_days"], rt.OUTCOME_DAYS)
    # …and no DROP_LINES_OLDER rule may exist without an island field: a rule added to the SWEEP
    # and never drawn is exactly how `outcomes.jsonl` went unstated for a week.
    stated = {rt.DELIVERY_RECEIPT_DAYS, rt.SEND_RECEIPT_DAYS, rt.TRIAGE_VERDICT_DAYS,
              rt.DASHBOARD_AUDIT_DAYS, rt.DRAFT_LEDGER_DAYS, rt.OUTCOME_DAYS}
    for rule in rt.SWEEP:
        if rule.policy == rt.DROP_LINES_OLDER:
            assert rule.amount in stated, f"{rule.pattern}: a line-age rule the island does not state.\n{RULE}"
    _anchor(f"**{rt.SEND_RECEIPT_DAYS} days**")
    _anchor(f"**{rt.DRAFT_LEDGER_DAYS} days**")
    _anchor(f"daily sweep at {rt.SWEEP_LOCAL[0]}:{rt.SWEEP_LOCAL[1]:02d} AM local")


def test_the_families_the_sweep_leaves_alone_are_the_ones_it_may_never_delete():
    """The graph, the ledger and the corpus are exempt AND protected — two independent statements,
    because the exemption is a design decision and the guard is what survives a typo in the table."""
    for family in ("knowledge", "corpus"):
        assert isinstance(rt.accounts_for(f"{family}/anything.md"), rt.Exempt), family
        assert rt.protected(f"{family}/anything.md"), family
    for rule in rt.SWEEP:
        assert not rt.protected(rule.pattern.split("*")[0].rstrip("/")), rule


# ── retention.py and forget.py are two views of ONE answer ──────────────────────────────────────
#
# `forget.py` is what a person runs; `retention.py` is what the daemon runs. They answer the same
# question — "what does Sotto keep?" — from opposite ends, and a family that one names and the other
# has never heard of is the drift that makes the docs' retention column a guess. The bind is
# mechanical: forget.py's OWN target patterns, read out of its source, must each be accounted for by
# the retention table (swept by a rule, or exempt with a reason).

_FORGET_VERB_RE = re.compile(r'if\s+"(\w+)"\s+in\s+verbs:')
_FORGET_ADD_RE = re.compile(r'add\(\s*"([^"]+)"')


def _forget_targets():
    """{verb: [pattern]} straight out of `forget._targets`. Read from source, not from a fixture
    volume, because a verb whose files don't happen to exist must still be covered."""
    verb, out = None, {}
    for line in inspect.getsource(fg._targets).splitlines():
        found = _FORGET_VERB_RE.search(line)
        if found:
            verb = found.group(1)
            out.setdefault(verb, [])
            continue
        added = _FORGET_ADD_RE.search(line)
        if added and verb:
            out[verb].append(added.group(1))
    return out


def test_every_forget_verb_is_accounted_for_by_the_retention_table():
    targets = _forget_targets()
    assert set(targets) == set(fg.VERBS), (
        f"forget.py's verbs are {sorted(fg.VERBS)} but its targets cover {sorted(targets)} — a verb "
        f"with no readable target list makes this guard vacuous.\n{RULE}")
    missing = {}
    for verb, patterns in targets.items():
        assert patterns, f"forget.py --{verb} names no files"
        for pattern in patterns:
            if rt.accounts_for(pattern) is None:
                missing.setdefault(verb, []).append(pattern)
    assert not missing, (
        "retention drift — forget.py deletes families that retention.py's table does not name:\n"
        + "\n".join(f"  --{v}: {', '.join(p)}" for v, p in sorted(missing.items()))
        + "\nAdd a SWEEP rule, or an EXEMPT entry saying who ages it instead (or why nothing has "
        f"to).\n{RULE}")


def test_the_forget_guard_is_not_satisfied_by_exemptions_alone():
    """An all-exempt answer would be a table that explains nothing. At least one thing a person can
    delete by hand is also aged automatically — that is the whole point of finding #5."""
    entries = [rt.accounts_for(p) for ps in _forget_targets().values() for p in ps]
    assert any(isinstance(e, rt.Rule) for e in entries)


def test_what_forget_refuses_to_touch_the_sweep_refuses_too():
    """`forget.PROTECTED` and `retention.NEVER` are two spellings of the same line between memory and
    exhaust. The sweep's list is wider (it also spares your settings and credentials); it may never
    be narrower."""
    for family in fg.PROTECTED:
        assert rt.protected(f"{family}/x.md"), (
            f"forget.py refuses to delete {family} but the retention sweep does not.\n{RULE}")


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
    # the wake-push's cron-compose window: a wake this soon after a brief's scheduled time folds
    # into the snapshot instead of racing the run that is still composing it
    _same("cron.brief_window_min", R["cron"]["brief_window_min"], rec.BRIEF_CRON_WINDOW_MIN)
    _anchor(f"within **{rec.BRIEF_CRON_WINDOW_MIN} minutes** of a brief's scheduled time")


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
    # Tier 1's ask classes: only `urgent` nudges at once; the other two queue for the release
    # valve (`_classify_tier1` returns "queue" for them) — the doc must say so in those words.
    asks = R["classes"]["tier1_nudge"]
    _anchor("`urgent` → nudge")
    _anchor(" · ".join(f"`{c}`" for c in asks if c != "urgent") + " → queued")
    _anchor("only the release valve (below) can turn one into a nudge")
    # the attachment lane — the same three numbers the island carries, stated in prose
    _anchor(f"At most {att.MAX_ATTACHMENTS_PER_EMAIL} converted per email")
    _anchor(f"{att.MAX_ATTACHMENT_BYTES // 1_000_000} MB per attachment")
    _anchor(f"{att.MAX_ATTACHMENT_CHARS:,} characters each")
    _anchor(f"share one {att.MAX_ATTACHMENT_CHARS_PER_BRIEF:,}-character budget")
    _anchor("An attachment Sotto can read becomes text under its email; "
            "one it can't is named, never guessed.")


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


# ── the shared-file map, which exists in two renderings ─────────────────────────────────────────

def _norm_shared_file(cell: str) -> str:
    """One shared-file NAME, however it was written. The prose doc marks code with backticks and the
    shared-write row with bold; the page HTML-escapes its angle brackets. Strip the presentation and
    compare the claim."""
    cell = cell.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    cell = cell.replace("`", "").replace("<code>", "").replace("</code>", "").strip()
    while cell.startswith("**") and cell.endswith("**") and len(cell) > 4:
        cell = cell[2:-2].strip()   # bold markers wrap the cell; `knowledge/**` is a real glob
    return " ".join(cell.split())


_ARCH_TABLE_RE = re.compile(r"## The shared `\$SOTTO_DATA` files(.*?)\n## ", re.S)
_PAGE_ROW_RE = re.compile(r'^\s*\["([^"]+)",\s*"[^"]*",\s*"[^"]*"\],?\s*$', re.M)


def test_the_shared_file_map_is_the_same_set_in_the_doc_and_the_page():
    """`$SOTTO_DATA` is the ONE thing the two processes share, so the list of files that cross it is
    the map you check before adding a writer — and it is rendered twice: as a table in
    ARCHITECTURE.md and as `SHARED_FILES` in the architecture playground. The rules island already
    guards the numbers; this guards the map. A file added to one rendering and forgotten in the
    other is exactly the drift that made the page claim `cache/update.json` for a year while the
    code wrote `cache/update_check.json`.

    Only the file column is compared: the writer/reader cells are prose and are allowed to be
    shorter on the page. Naming is not — a row present in one and absent from the other is a bug."""
    with open(os.path.join(DOCS, "ARCHITECTURE.md"), encoding="utf-8") as f:
        arch = f.read()
    with open(os.path.join(DOCS, "playground-architecture.html"), encoding="utf-8") as f:
        page = f.read()

    section = _ARCH_TABLE_RE.search(arch)
    assert section, "ARCHITECTURE.md no longer has a '## The shared `$SOTTO_DATA` files' section"
    doc_files = set()
    for line in section.group(1).splitlines():
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| File "):
            continue
        doc_files.add(_norm_shared_file(line.split("|")[1]))

    block = page[page.index("const SHARED_FILES = ["):page.index("const SHARED_WRITE_ROW")]
    page_files = {_norm_shared_file(m) for m in _PAGE_ROW_RE.findall(block)}
    write_row = re.search(r'const SHARED_WRITE_ROW = \["([^"]+)"', page)
    assert write_row, "the playground no longer carries SHARED_WRITE_ROW"
    page_files.add(_norm_shared_file(write_row.group(1)))

    assert doc_files == page_files, (
        "docs drift — the shared-$SOTTO_DATA map disagrees between its two renderings.\n"
        f"  only in ARCHITECTURE.md: {sorted(doc_files - page_files)}\n"
        f"  only in the playground : {sorted(page_files - doc_files)}\n{RULE}")

    # The shared-write file is real, and it is exactly one: the whole no-lock argument rests on it.
    assert _norm_shared_file(write_row.group(1)) == "preferences.json"


# ── …and the map must be COMPLETE, not merely self-consistent ───────────────────────────────────
#
# The guard above proves the two RENDERINGS agree. Agreement is not completeness: a file the code
# writes and neither rendering names passes it happily, and seven did — events/gmail_seen.json,
# events/last_digest.txt, events/{queue,surfaced,sends}.jsonl, the whole proactive/ directory, the
# WhatsApp session creds, knowledge/relationship_state.json. This is the guard that closes the
# class: it reads the CODE, not the docs, and asks the docs to account for what it finds.

# One literal path built off the volume root. Three spellings cover the tree: the inline
# `os.environ.get("SOTTO_DATA", …)` the skills scripts use, the module-level `DATA` constant in
# receiver.py, and the dashboard's `_root()`.
_DATA_JOIN_RE = re.compile(
    r'os\.path\.join\(\s*(?:os\.environ\.get\(\s*["\']SOTTO_DATA["\'][^)]*\)|DATA|_root\(\))'
    r'\s*,\s*([^)]{0,160}?)\)')

# A quoted string, with any prefix (f/r/b) — the only kind of path component this guard can read.
_STR_LITERAL_RE = re.compile(r'''[fFrRbB]{0,2}("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')''')

# THE ALLOWLIST — kept small and named on purpose; an allowlist that grows is this guard failing
# quietly. Artifacts that are internal to one writer and cross no boundary:
_INTERNAL_SUFFIXES = (".lock", ".tmp")      # jsonstore's flock sidecar · atomic-write temporaries
# Directories are not on it and need no names: `os.path.join(DATA, "events")` is a mkdir/listdir
# target, and it is allowed exactly when some documented row LIVES in that directory — derived
# from the map itself below, so a new directory can never be allowed by accident.

_SKIP_DIRS = {".git", "__pycache__", "node_modules", "target", ".pytest_cache", "venv", ".venv"}


def _literal_parts(capture: str):
    """The join's arguments as literal path components, or None if ANY component is a variable.

    `os.path.join(DATA, "cache", name)` names no file — the guard can't know what `name` is, and
    guessing would be worse than skipping. Every component must be a string literal for the hit to
    count."""
    parts, pos = [], 0
    for m in _STR_LITERAL_RE.finditer(capture):
        if capture[pos:m.start()].strip() not in ("", ","):
            return None                      # something that isn't a literal sits between them
        parts.append(m.group(1)[1:-1])
        pos = m.end()
    if not parts or capture[pos:].strip() not in ("", ","):
        return None
    return parts


def _code_path(parts) -> str:
    """The components as one `$SOTTO_DATA`-relative path, with f-string placeholders normalized to
    `*` — `f"{date}.{kind}.claim"` and the doc's `<date>.<kind>.claim` are the same claim."""
    return re.sub(r"\{[^{}]*\}", "*", "/".join(parts))


def _matcher(name: str):
    """A documented file NAME as a regex. `<date>` and `*` both mean "one path segment's worth of
    anything" — so `knowledge/<kind>/*.md` cannot quietly cover `knowledge/anything.json`."""
    name = re.sub(r"<[^>]*>", "*", name)
    return re.compile("^" + "".join("[^/]*" if ch == "*" else re.escape(ch) for ch in name) + "$")


def _documented_matchers():
    """Every file the ARCHITECTURE.md map names, plus the directory each one lives in.

    Only that table is read: the guard above already forces the playground to carry the same set,
    so asking both would be asking one question twice."""
    with open(os.path.join(DOCS, "ARCHITECTURE.md"), encoding="utf-8") as f:
        section = _ARCH_TABLE_RE.search(f.read())
    assert section, "ARCHITECTURE.md no longer has a '## The shared `$SOTTO_DATA` files' section"
    files, dirs = [], []
    for line in section.group(1).splitlines():
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| File "):
            continue
        for name in line.split("|")[1].split("·"):
            name = _norm_shared_file(name)
            if not name:
                continue
            files.append(name)
            while "/" in name:                        # every parent directory of a documented row
                name = name.rsplit("/", 1)[0]
                dirs.append(name)
    return [_matcher(n) for n in files], [_matcher(n) for n in dirs]


def _scan_data_paths():
    """{normalized path: {source files}} for every literal `$SOTTO_DATA/…` path in the tree.

    `test_*.py` is skipped: a test builds paths under its own tmp_path, and a fixture tree is not
    the volume."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(HERMES):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for filename in filenames:
            if not filename.endswith(".py") or filename.startswith("test_"):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, encoding="utf-8") as f:
                src = f.read()
            for m in _DATA_JOIN_RE.finditer(src):
                parts = _literal_parts(m.group(1))
                if parts:
                    found.setdefault(_code_path(parts), set()).add(
                        os.path.relpath(path, HERMES))
            for rel_path in _pathlib_data_paths(src):
                found.setdefault(rel_path, set()).add(os.path.relpath(path, HERMES))
    return found


def _pathlib_data_paths(src):
    """Literal descendants of `Path(DATA|data|_root()|SOTTO_DATA)`, including chained `/` joins.

    A variable literally named `root` counts as a base too — but only in a module that BINDS it to
    one (`root = Path(os.environ.get('SOTTO_DATA', …))`, `root = Path(data)`, `root = _root()`), or
    to a literal descendant of one (`root = Path(…) / "config"`, whose prefix is then carried into
    every `root / "x"` below it). A `root` that is a function parameter, `Path(__file__)…` or
    `HERMES_HOME` names some other tree, and guessing would flag files that are not on the volume.
    Every base binding in a module must agree on the prefix; a module that binds `root` two ways
    is not scanned through that name at all."""
    root_prefix = [None]          # closed over by parts(); set once the module's bindings are read

    def base(node):
        if isinstance(node, ast.Name) and node.id in ('DATA', 'data'):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == '_root':
            return True
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'get' and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'SOTTO_DATA'):
            return True
        if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                and node.slice.value == 'SOTTO_DATA'):
            return True               # os.environ['SOTTO_DATA']
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'Path' and len(node.args) == 1 and base(node.args[0]))

    def parts(node):
        if isinstance(node, ast.Name) and node.id == 'root':
            return None if root_prefix[0] is None else list(root_prefix[0])
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Path'
                and len(node.args) == 1 and isinstance(node.args[0], ast.Name)
                and node.args[0].id == 'root'):
            return parts(node.args[0])                    # Path(root) / "x"
        if base(node):
            return []
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = parts(node.left)
            if left is not None and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
                return [*left, node.right.value]
            if left is not None and isinstance(node.right, ast.JoinedStr):
                rendered = ''.join(
                    value.value if isinstance(value, ast.Constant) else '*'
                    for value in node.right.values)
                return [*left, rendered]
        return None

    found = set()
    tree = ast.parse(src)
    # What does this module mean by `root`? Read every `root = …` binding with `root` still
    # unbound, so a self-referential `root = root / "x"` resolves to nothing rather than to itself.
    bindings = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == 'root'):
            prefix = parts(node.value)
            bindings.add(None if prefix is None else tuple(prefix))
    if len(bindings) == 1 and None not in bindings:
        root_prefix[0] = list(bindings.pop())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        parent = parents.get(node)
        if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div) and parent.left is node:
            continue
        value = parts(node)
        if value:
            found.add(_code_path(value))
    return found


def test_pathlib_volume_scanner_finds_literal_files_and_skips_dynamic_names():
    fixture = '''
from pathlib import Path
lease = Path(data) / "config" / "model-lease.json"
history = Path(DATA) / "knowledge" / f"{day}.json"
dynamic = Path(DATA) / name
outside = Path("/tmp") / "unrelated.json"
'''
    assert _pathlib_data_paths(fixture) == {
        'config/model-lease.json', 'knowledge/*.json'}


def test_pathlib_volume_scanner_follows_a_root_bound_to_the_volume():
    """`root / 'events/outbox.json'` counts exactly when the module binds `root` to the volume —
    directly, or to a literal child of it (the prefix rides along). A `root` bound elsewhere or
    merely received as a parameter is not the volume, and joins off it are not scanned."""
    bound = '''
from pathlib import Path
root = Path(os.environ.get('SOTTO_DATA', '/data'))
outbox = root / 'events/outbox.json'
delivered = root / 'briefs' / f'{day}.{kind}.delivered'
'''
    assert _pathlib_data_paths(bound) == {'events/outbox.json', 'briefs/*.*.delivered'}
    child = '''
root = Path(data) / "config"
receipt = root / "photon-activation.json"
'''
    assert _pathlib_data_paths(child) == {'config', 'config/photon-activation.json'}
    elsewhere = '''
def check(root):
    return (root / 'google_token.json').exists()
def main():
    root = Path(__file__).resolve().parents[2]
    return root / 'tools/prepare-public-repo.sh'
'''
    assert _pathlib_data_paths(elsewhere) == set()


def test_the_shared_file_map_covers_every_path_the_code_writes():
    """Every literal `$SOTTO_DATA` path in the tree is accounted for by a row in the map.

    The map is the thing a reader checks before adding a writer, and the thing a user checks to
    learn what Sotto keeps about them. A file the code writes and the map doesn't list is invisible
    to both — and it is invisible in the way that matters most, because the undocumented ones were
    exactly the message-derived ones (the Gmail dedup ring, the send receipts, the WhatsApp session
    creds). Add the row in the same commit as the writer; both renderings, per the guard above."""
    file_matchers, dir_matchers = _documented_matchers()
    undocumented = {}
    for rel_path, sources in sorted(_scan_data_paths().items()):
        if rel_path.endswith(_INTERNAL_SUFFIXES) or ".tmp." in rel_path:
            continue
        if any(m.match(rel_path) for m in file_matchers):
            continue
        if any(m.match(rel_path) for m in dir_matchers):
            continue                                  # a directory some documented row lives in
        undocumented[rel_path] = sorted(sources)

    assert not undocumented, (
        "docs drift — the code writes $SOTTO_DATA paths that the shared-file map in "
        "docs/ARCHITECTURE.md does not name:\n"
        + "\n".join(f"  {p}   ← {', '.join(s)}" for p, s in sorted(undocumented.items()))
        + "\nAdd a row to BOTH renderings (the table and the playground's SHARED_FILES), or, if it "
        f"is genuinely internal, to _INTERNAL_SUFFIXES with a reason.\n{RULE}")


def test_the_completeness_scan_actually_finds_paths():
    """A scan that silently matches nothing would make the guard above pass forever. Pin the floor:
    it must find the map's own landmark rows in the real tree."""
    found = _scan_data_paths()
    for expected in ("events/gmail_seen.json", "events/sends.jsonl", "proactive/wake_run.last",
                     "knowledge/relationship_state.json", "config/settings.json",
                     "events/outbox.json",        # onboarding.py, through a volume-bound `root`
                     "config/photon-activation.json"):   # sotto_photon, through `root = … / "config"`
        assert expected in found, (
            f"the $SOTTO_DATA path scan no longer finds {expected!r} — the scan regex has drifted "
            f"from how the tree builds paths, and the completeness guard is now vacuous.\n{RULE}")


# ── the provider ladder, which exists in two processes ──────────────────────────────────────────

def test_the_search_providers_are_the_same_ladder_in_both_processes():
    """The search ladder lives in `_shared/scripts/web_research.py` — that module decides who
    answers a lookup. The receiver renders the same providers on the Connections page so "is Exa
    on?" has an answer, and it cannot import the skills tree, so the table is duplicated exactly
    once. This is the guard that keeps the copy honest: add a provider or reorder a capability's
    precedence in web_research, do it in connectors.py in the same commit.

    A page that names a provider the ladder wouldn't use, or omits one it would, is worse than no
    page — it is a confident wrong answer about what Sotto can do."""
    wr = _load("dd_web_research", PACK, "_shared", "scripts", "web_research.py")
    import connectors as conn  # noqa: PLC0415 — receiver-side module, on conftest's path
    assert set(conn.KEY_PROVIDERS) == set(wr.KEY_ENV), (
        f"docs drift — providers differ: receiver {sorted(conn.KEY_PROVIDERS)} vs "
        f"web_research {sorted(wr.KEY_ENV)}.\n{RULE}")
    for pid, p in conn.KEY_PROVIDERS.items():
        assert p["env"] == wr.KEY_ENV[pid], (
            f"docs drift — {pid} reads {p['env']} on the page but {wr.KEY_ENV[pid]} in the "
            f"ladder.\n{RULE}")
    # Precedence is the claim the page makes most loudly ("first one set answers"), so it is
    # compared in ORDER, not as a set.
    assert conn.CAPABILITIES == dict(wr.CAPABILITIES), (
        f"docs drift — capability precedence differs: receiver {conn.CAPABILITIES} vs "
        f"web_research {dict(wr.CAPABILITIES)}.\n{RULE}")


# ── the read-modify-write lock, which exists in two processes ───────────────────────────────────

def test_both_runtimes_lock_preferences_on_the_same_sidecar():
    """The skills and receiver runtimes share the JSON lock protocol. flock is an
    OS primitive, so they serialise correctly ONLY if they name the same lock file. The receiver
    cannot import the skills tree, so the protocol is duplicated exactly once — and this is the
    guard that keeps the two names identical. Change one, change the other, same commit."""
    js = _load("dd_jsonstore", PACK, "_shared", "lib", "jsonstore.py")
    import connectors as conn  # noqa: PLC0415
    assert js.LOCK_SUFFIX == conn.LOCK_SUFFIX, (
        f"docs drift — lock suffix differs: skills {js.LOCK_SUFFIX!r} vs receiver "
        f"{conn.LOCK_SUFFIX!r}. Two writers taking DIFFERENT locks is the same as no lock.\n{RULE}")
    probe = "/tmp/x/preferences.json"
    assert js.lock_path(probe) == probe + conn.LOCK_SUFFIX


def test_receiver_marker_strip_matches_chatfmt():
    """The receiver's send-seam marker strip is a VENDORED copy of chatfmt._MARKER_RE (its owner) —
    the same two-runtimes contract keys.py lives under. If the owner's pattern ever changes, this
    fails until the vendored copy moves with it."""
    cf = _load("dd_chatfmt", PACK, "_shared", "lib", "chatfmt.py")
    assert rec._MARKER_RE.pattern == cf._MARKER_RE.pattern, (
        f"receiver._MARKER_RE ({rec._MARKER_RE.pattern!r}) drifted from its owner "
        f"chatfmt._MARKER_RE ({cf._MARKER_RE.pattern!r}).\n{RULE}")
    assert rec._MARKER_RE.flags == cf._MARKER_RE.flags


def test_birthday_importance_rules_match_shared_policy():
    import relationship_importance as importance
    for key, value in {
        'importance_window_days': importance.WINDOW_DAYS,
        'vip_active_days': importance.VIP_ACTIVE_DAYS,
        'vip_active_weeks': importance.VIP_ACTIVE_WEEKS,
        'vip_direction_days': importance.VIP_EACH_DIRECTION_DAYS,
        'vvip_active_days': importance.VVIP_ACTIVE_DAYS,
        'vvip_active_weeks': importance.VVIP_ACTIVE_WEEKS,
        'vvip_direction_days': importance.VVIP_EACH_DIRECTION_DAYS,
    }.items():
        _same('birthday.' + key, R['birthday'][key], value)
