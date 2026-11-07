#!/usr/bin/env python3
"""
run_golden.py — replay the Golden Corpus through the REAL pipeline and score it against the owner's
labels (docs/archive/plans/golden-corpus.md, evals/LABELING.md).

The corpus is the owner's own history, pseudonymized and tokenized by
`tools/build_golden_corpus.py`. A corpus day is a fixture in the exact shape `evals/fixtures/*.json`
already use, so this harness borrows run_evals.py's token resolver and sandbox seeder rather than
inventing a second one.

  python3 evals/run_golden.py --dry                  # offline: funnel + loops + entity dedup
  python3 evals/run_golden.py --live --sample 5      # real Gemini brief per day + the judge
  python3 evals/run_golden.py --live --update-baseline

Two modes, one replay:

  --dry   (DEFAULT) No network. Runs the deterministic half — the event funnel (nudge/queue/drop),
          continuity resolution, and identity dedup — against the labels. Fast, safe, and the half
          that regresses silently.
  --live  Requires GOOGLE_AI_API_KEY. Adds the real brief per sampled day and, with --judge, an
          LLM judge on a PINNED model scoring fabrication / voice / triage. ~$1-2 for 5 days.

Days replay IN ORDER into ONE sandbox, so the continuity ledger and the interrupt budget accumulate
exactly as they would across a real week — that's the flywheel time-travel the plan asks for.

Three standing rules:
  · A corpus is real user data. It never leaves the owner's infrastructure — gitignored, deleted by
    tools/prepare-public-repo.sh, and guarded there (CORPUS GUARD).
  · Unreviewed labels are not a baseline. This harness refuses to score against a labels.yaml that
    still says `reviewed: false` unless you pass --allow-unreviewed.
  · A gate without a baseline is unarmed. Until --update-baseline records one, every run ends
    RESULT: NO BASELINE (exit 2) — never PASS.
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import math
import os
import shutil
import sys
import tempfile
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from contextlib import nullcontext

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
CORPUS_ROOT = os.path.join(HERE, "corpus")

sys.path.insert(0, os.path.join(PACK, "_shared", "lib"))
sys.path.insert(0, os.path.join(PACK, "_shared", "scripts"))
sys.path.insert(0, os.path.join(PACK, "morning-brief", "scripts"))
sys.path.insert(0, os.path.join(PACK, "event-triage", "scripts"))

_spec = importlib.util.spec_from_file_location("run_evals", os.path.join(HERE, "run_evals.py"))
ev = importlib.util.module_from_spec(_spec)
sys.modules["run_evals"] = ev
_spec.loader.exec_module(ev)

cb = ev.cb                                   # compose_brief, already loaded by run_evals
cr = ev.cr                                   # continuity_resolve
import textutil                              # noqa: E402  — the owners of the shared helpers
import timeutil                              # noqa: E402
import render_local                          # noqa: E402
from gemini import call_gemini as gemini_call  # noqa: E402
import gemini_transport                      # noqa: E402

DEFAULT_VERSION = "corpus-v1"
DEFAULT_SAMPLE = 5
JUDGE_MODEL = "gemini-3-flash-preview"       # pinned: a moving judge is not a baseline


# ── Corpus loading ────────────────────────────────────────────────────────────────────────────────

def corpus_dir(version: str) -> str:
    return os.path.join(CORPUS_ROOT, version)


def load_corpus(version: str, root: str = "") -> dict:
    """manifest + every day + the labels. Raises FileNotFoundError with the build command when the
    corpus isn't there — a missing corpus is the normal state of a fresh checkout, not an error."""
    d = root or corpus_dir(version)
    manifest_path = os.path.join(d, "manifest.json")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(
            f"no corpus at {d} — build one where the data lives:\n"
            f"    SOTTO_DATA=/data python3 tools/build_golden_corpus.py --version {version}")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    days = []
    for entry in manifest.get("days", []):
        with open(os.path.join(d, entry["file"]), encoding="utf-8") as f:
            days.append(json.load(f))
    days.sort(key=lambda x: -int(x.get("offset_days", 0)))     # oldest first — replay runs forward
    return {"dir": d, "manifest": manifest, "days": days, "labels": load_labels(d)}


def load_labels(d: str) -> dict:
    path = os.path.join(d, "labels.yaml")
    if not os.path.exists(path):
        return {"reviewed": False, "days": {}}
    import yaml                                     # noqa: PLC0415 — a pack dependency, not ours
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {"reviewed": False, "days": {}}


def sample_days(days: list, n: int) -> list:
    """N evenly-spaced days — representative and reproducible, where a random sample is neither."""
    if n <= 0 or n >= len(days):
        return list(days)
    step = (len(days) - 1) / (n - 1) if n > 1 else 0
    return [days[round(i * step)] for i in range(n)]


def base_for(day: dict, frozen: datetime) -> datetime:
    """The day's own clock: the frozen 'most recent day' walked back by its offset."""
    base = frozen - timedelta(days=int(day.get("offset_days", 0)))
    return base.replace(hour=23, minute=59, second=0, microsecond=0) if day.get('replay_clock') == 'day_end' else base


def frozen_base(override: str = "") -> datetime:
    """The replay clock is NOON TODAY walked back one day per corpus day, so the most recent day
    replays as 'today' — the pipeline reads its own wall clock, and a corpus pinned to a date months
    away would make every message look stale. --frozen-base pins it for a byte-reproducible run."""
    if override:
        return datetime.fromisoformat(override).replace(hour=12, minute=0, second=0, microsecond=0)
    return ev._base_datetime()


# ── The funnel projection (ONE definition — the builder imports this one for its label draft) ─────

def funnel_events(day: dict) -> list:
    """A day's INBOUND items, shaped for event-triage's funnel: the nudge/queue/drop replay the
    Editor's interrupt budget is tuned against. The user's own outbound is not an interruption."""
    g, local = day["inputs"].get("google", {}), day["inputs"].get("local", {})
    out = []
    for src, items in (("imessage", local.get("imessage") or []),
                       ("whatsapp", local.get("whatsapp") or []),
                       ("calls", local.get("calls") or []),
                       ("email", g.get("emails") or [])):
        for it in items:
            if not isinstance(it, dict) or it.get("is_from_me") or it.get("isSent"):
                continue
            out.append(dict(it, source=src))
    return out


# ── The replay ────────────────────────────────────────────────────────────────────────────────────

def _stub_llm(_prompt, _inputs):
    """A valid placeholder, never scored as prose. Empty text is a failed extraction in production;
    dry mode still exercises the deterministic half without pretending to generate a brief."""
    return json.dumps({"brief_markdown": "Offline replay: prose generation disabled.",
                       "actions": [], "extracted_knowledge": {}})


def oldest_base(corpus: dict, frozen: datetime) -> datetime:
    """The clock of the day the replay STARTS from — the reference the seed ledger was tokenized
    against, so a seeded loop is as old on day one of the replay as it was in real life."""
    offsets = [int(d.get("offset_days", 0)) for d in (corpus.get("days") or [])]
    return frozen - timedelta(days=max(offsets) if offsets else 0)


def _seed_sandbox(corpus: dict, base: datetime | None = None) -> str:
    """One sandbox for the WHOLE replay — the ledger and the interrupt budget must accumulate across
    days, which is the point of replaying them in order. The seed ledger's clock tokens resolve
    against the oldest day, exactly like every day payload's do against their own."""
    sandbox = tempfile.mkdtemp(prefix="sotto-golden-")
    prefs = corpus["manifest"].get("preferences") or {}
    with open(os.path.join(sandbox, "preferences.json"), "w", encoding="utf-8") as f:
        json.dump({"explicit": prefs}, f)
    cdir = os.path.join(sandbox, "knowledge", "continuity")
    os.makedirs(cdir, exist_ok=True)
    for entry in corpus["manifest"].get("seed_ledger", []):
        fm = entry["frontmatter"]
        if base is not None:
            fm = ev.resolve_tokens(fm, base)
        with open(os.path.join(cdir, entry["filename"]), "w", encoding="utf-8") as f:
            f.write("---\n" + ev._emit_frontmatter(fm) + "---\n")
    return sandbox


def _open_anchors(sandbox: str) -> dict:
    """The loops still open in the sandbox ledger, each with its chase state — `open_loops_after` is
    scored against the keys, `chases_after` against the counts. A chase decision that no metric can
    see is a chase decision no fix is guarded by."""
    import yaml                                    # noqa: PLC0415 — a pack dependency, not ours
    kn = importlib.import_module("knowledge")
    out = {}
    for path in glob.glob(os.path.join(sandbox, "knowledge", "continuity", "*.md")):
        try:
            with open(path, encoding="utf-8") as f:
                head, _body = kn.split_frontmatter_body(f.read())
            fm = yaml.safe_load(head) if head else None
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if isinstance(fm, dict) and fm.get("status") in ("open", "waiting", "blocked", "failed") and fm.get("anchor_key"):
            try:
                chased = int(fm.get("chased_count") or 0)
            except (TypeError, ValueError):
                chased = 0
            out[str(fm["anchor_key"])] = {
                "chased_count": chased,
                "chase_pending": textutil._s(fm.get("chase_pending"))[:10] or None,
                "last_chased_at": textutil._s(fm.get("last_chased_at"))[:10] or None,
            }
    return out


def _distinct_entities(inputs: dict) -> int:
    """How many DISTINCT humans the pipeline resolves this day to — the dedup measurement. Every
    identifier the day carries is resolved through the brief's own contact lookup and grouped by the
    resolved NAME; an unresolvable identifier counts as its own person (which is what the user
    sees). A split identity ('Ben' + 'Ben Butler') shows up as a count ABOVE the label."""
    local = cb._normalize_local(inputs)
    lookup = render_local.build_contact_lookup(local.get("contacts") or [])
    pairs = []

    def _add(name, ident):
        n = textutil._s(name).strip()
        pairs.append(("" if textutil._looks_like_phone_number(n) else n, textutil._s(ident).strip()))

    for m in local.get("imessage") or []:
        _add(render_local.resolve_imessage_name(textutil._s(m.get("handle")), lookup), m.get("handle"))
    for m in local.get("whatsapp") or []:
        _add(render_local.resolve_whatsapp_name(textutil._s(m.get("contact_jid")), textutil._s(m.get("partner_name")), lookup),
             m.get("contact_jid"))
    for c in local.get("calls") or []:
        _add(render_local.resolve_call_name(textutil._s(c.get("phone")), lookup), c.get("phone"))
    for c in local.get("contacts") or []:
        for ident in (c.get("emails") or []) + (c.get("phones") or []):
            _add(c.get("name"), ident)
    google = inputs.get("google") or {}
    _add("", google.get("userEmail"))
    for e in google.get("emails") or []:
        for header in ("from", "to"):
            addr = textutil._sender_addr(textutil._s(e.get(header)))
            if addr:
                _add(lookup.get(addr) or textutil._extract_sender_name(textutil._s(e.get(header))), addr)
    for evt in google.get("events") or []:
        for a in evt.get("attendees") or []:
            if isinstance(a, dict):
                _add(a.get("displayName"), a.get("email"))

    # Two passes on purpose: an identifier that anyone named ANYWHERE inherits that name, so the
    # same human seen once as a bare address and once as "Name <address>" counts as one person.
    name_of = {textutil._normalize_identifier(i): textutil._normalize_name_key(n)
               for n, i in pairs if n and i}
    groups = {name_of.get(textutil._normalize_identifier(i)) or textutil._normalize_name_key(n)
              or textutil._normalize_identifier(i) for n, i in pairs}
    groups.discard("")
    return len(groups)


def _project_inputs(inputs: dict) -> dict:
    """Use the same source boundary for composition, event replay and the independent judge."""
    from source_context import allowed, project_local, project_emails, project_x
    inputs = dict(inputs)
    inputs['local'] = project_local(inputs.get('local') or {})
    google = dict(inputs.get('google') or {})
    google['emails'] = project_emails(google.get('emails') or [])
    if not allowed('gmail'):
        google.pop('staleThreads', None)
        google.pop('stale_threads', None)
    if not allowed('calendar'):
        google['events'] = []
        inputs['attendee_research'] = []
        inputs['x_context'] = {}
    inputs['google'] = google
    if not allowed('granola'):
        inputs['granola'] = {'meetings': []}
    inputs['x_context'] = project_x(inputs.get('x_context') or {})
    return inputs


def replay_day(day: dict, base: datetime, sandbox: str, live: bool) -> dict:
    """One corpus day through the real pipeline. Chronological days replay inbound events before
    day-end composition/learning; snapshot comparisons retain their historical ordering."""
    fx = ev.resolve_tokens(day, base)
    inputs = fx["inputs"]
    # The brief reasons about the day being REPLAYED, not the wall clock: without this every ledger
    # age in the prompt is inflated by (today − this corpus day), up to the length of the corpus.
    inputs["now"] = base.strftime("%Y-%m-%d %H:%M:%S")
    result = {"name": day["name"], "exception": None, "brief": "", "actions": [],
              "verdicts": {}, "open_loops": set(), "chases": {}, "entities": 0}
    try:
        # Match the production file-input boundary before composing or resolving. The
        # composer itself loads the current protected ledger from this same sandbox.
        from source_context import allowed, record_bridge_status, used_sources
        record_bridge_status(inputs.get('local') or {}, data_root=sandbox)
        inputs.update(_project_inputs(inputs))
        google = inputs['google']
        permissions = set(used_sources(inputs['local'], google, google.get('events', []),
            inputs.get('granola'), inputs.get('x_context'), already_projected=True))
        chronological = day.get('replay_clock') == 'day_end'
        if chronological:
            # Morning events cannot read the snapshot/knowledge written by tonight's composer.
            result['verdicts'] = _replay_funnel(fx, base, live=live)
            if any(str(v).startswith('error:') for v in result['verdicts'].values()):
                raise RuntimeError('funnel execution failed')
        continuity = {'today': base.strftime('%Y-%m-%d'), 'signals': fx.get('signals', {}),
                      'local': inputs['local'], 'emails': google['emails'],
                      'events': google.get('events', [])}
        # Production resolves observed deterministic signals before the model sees the ledger.
        cr.resolve(continuity, base, merge=False)
        out = cb.compose(inputs, llm=(gemini_call if live else _stub_llm), critic=live)
        permissions.update(out.get('_loop_source_permissions') or [])
        if any(not allowed(source) for source in permissions):
            raise RuntimeError('source permission changed while composing')
        result['_source_permissions'] = sorted(permissions)
        result['_judge_inputs'] = inputs
        result["brief"] = out.get("brief_markdown", "")
        result["actions"] = [a for a in (out.get("actions") or []) if isinstance(a, dict)]
        continuity.update(new_actions=result['actions'] or fx.get('new_actions', []),
                          loop_updates=out.get('loop_updates') or [])
        cr.resolve(continuity, base, resolve_existing=False)
        anchors = _open_anchors(sandbox)
        result["open_loops"] = set(anchors)
        result["chases"] = {k: v["chased_count"] for k, v in anchors.items() if v["chased_count"]}
        result["entities"] = _distinct_entities(inputs)
        if not chronological:
            result["verdicts"] = _replay_funnel(fx, base, live=live)
        if any(str(verdict).startswith('error:') for verdict in result['verdicts'].values()):
            raise RuntimeError('funnel execution failed')
    except Exception as e:  # noqa: BLE001 — one bad day must not abort a 42-day replay
        result["exception"] = f"{type(e).__name__}: {e}"
    return result


def _replay_funnel(fx: dict, base: datetime, *, live: bool = False) -> dict:
    """Each inbound item triaged AS IF IT HAD JUST ARRIVED — its own timestamp is the clock, so
    quiet hours, cooldowns and the daily interrupt budget all land where they really would."""
    te = importlib.import_module("triage_event")
    import personal_context
    chronological = fx.get('replay_clock') == 'day_end'
    events = funnel_events(fx)
    def observed(event):
        stamp = timeutil._parse_ts(textutil._s(event.get('timestamp')) or textutil._s(event.get('date')))
        if stamp is None:
            if chronological:
                raise ValueError('chronological event requires an observed timestamp')
            stamp = base
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    candidates = []
    if chronological:
        events.sort(key=observed)
        local, google = fx['inputs'].get('local') or {}, fx['inputs'].get('google') or {}
        candidates = personal_context.conversation_snapshot()
        for source, rows in (('imessage', local.get('imessage')), ('whatsapp', local.get('whatsapp')),
                             ('gmail', google.get('emails'))):
            candidates.extend(({**row, 'source': source,
                                'is_from_me': bool(row.get('is_from_me') or row.get('isSent'))}, '', '')
                              for row in rows or [] if isinstance(row, dict))
    verdicts = {}
    for e in events:
        event_clock = observed(e)
        ts = event_clock.replace(tzinfo=None)
        now = ts + timedelta(minutes=1)
        try:
            # A dry replay remains offline even on an operator machine with provider keys.
            classifier = te._classify_tier1
            failures = []
            def classify(*args, **kwargs):
                if not live:
                    raise RuntimeError('offline replay: Tier-1 not executed')
                try:
                    return classifier(*args, **kwargs)
                except Exception as error:
                    failures.append(error)
                    raise
            # Keep production selection/formatting, but evaluate its window at this event, not
            # the machine's wall clock. Later outbound messages become visible only later.
            conversation = personal_context.current_conversation
            def as_of(event, now=None, candidates=None):
                return conversation(event, now=event_clock, candidates=candidates)
            with (patch.object(te, '_classify_tier1', side_effect=classify),
                  patch.object(personal_context, 'conversation_snapshot', return_value=candidates)
                  if chronological else nullcontext(),
                  patch.object(personal_context, 'current_conversation', side_effect=as_of)
                  if chronological else nullcontext()):
                out = te.triage({"events": [e]}, now_local=now, now_utc=now.replace(tzinfo=timezone.utc))
            # Production queues a failed classifier safely. It is not a completed eval verdict.
            if failures:
                raise failures[0]
            verdicts[e.get("_gid", "?")] = {"agent": "nudge"}.get(out.get("verdict"), out.get("verdict"))
        except Exception as exc:  # noqa: BLE001 — the funnel's own posture: fail toward silence
            verdicts[e.get("_gid", "?")] = f"error:{type(exc).__name__}"
    return verdicts


# ── Scoring ───────────────────────────────────────────────────────────────────────────────────────

def _prf(expected: set, actual: set) -> dict:
    tp = len(expected & actual)
    precision = tp / len(actual) if actual else (1.0 if not expected else 0.0)
    recall = tp / len(expected) if expected else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
            "tp": tp, "expected": len(expected), "actual": len(actual)}


def score_day(day: dict, res: dict, labels: dict, live: bool) -> list:
    """(metric, value, detail) rows. A label that is None is UNSCORED — the owner labels the days
    they have judgment about, and the harness never invents the rest."""
    lab = (labels.get("days") or {}).get(day["name"]) or {}
    rows = []
    if res["exception"]:
        return [("replay", None, f"raised {res['exception']}")]

    nudge_labels = lab.get("nudge") or {}
    if nudge_labels:
        scored = {g: v for g, v in res["verdicts"].items() if g in nudge_labels}
        agree = sum(1 for g, v in scored.items() if v == nudge_labels[g])
        rows.append(("funnel_agreement", round(agree / len(scored), 3) if scored else None,
                     f"{agree}/{len(scored)} verdicts match the label"))
        rows.append(("nudge_prf", _prf({g for g, v in nudge_labels.items() if v == "nudge"},
                                       {g for g, v in scored.items() if v == "nudge"}),
                     "nudge precision/recall — the interrupt-budget tuning signal"))

    if lab.get("entity_count") is not None:
        delta = res["entities"] - int(lab["entity_count"])
        rows.append(("entity_dedup", delta,
                     f"resolved {res['entities']} vs {lab['entity_count']} real people "
                     f"({'split identities' if delta > 0 else 'over-merged' if delta < 0 else 'exact'})"))

    if lab.get("open_loops_after") is not None:
        rows.append(("open_loops", _prf(set(lab["open_loops_after"]), set(res["open_loops"])),
                     "open continuity loops at end of day"))

    if lab.get("chases_after") is not None:
        expected = {str(k): int(v) for k, v in (lab["chases_after"] or {}).items()}
        actual = res.get("chases") or {}
        agree = sum(1 for k, v in expected.items() if actual.get(k, 0) == v)
        rows.append(("chases", round(agree / len(expected), 3) if expected else None,
                     f"{agree}/{len(expected)} chase counts match the label "
                     f"(meaningful at --sample 0 only)"))

    if live and lab.get("needs_attention") is not None:
        expected = set(lab["needs_attention"])
        actual = {gid for gid in expected | set(lab.get("not_needs_attention") or [])
                  if _gid_in_section(gid, res["actions"], day)}
        rows.append(("triage_prf", _prf(expected, actual), "Needs Attention precision/recall"))
    return rows


def _gid_in_section(gid: str, actions: list, day: dict) -> bool:
    """Did the brief put this corpus item in 'needs attention'? Matched by the item's subject/text,
    because the model writes prose about the item, never its golden id."""
    item = next((x for x in (day["inputs"]["google"].get("emails") or []) if x.get("_gid") == gid), None)
    if not item:
        return False
    needle = (textutil._s(item.get("subject")) or textutil._s(item.get("snippet")))[:40].strip().lower()
    if not needle:
        return False
    return any(needle in json.dumps(a, ensure_ascii=False).lower()
               for a in actions if a.get("section") == "needs_attention")


# ── The LLM judge (live only, pinned model) ───────────────────────────────────────────────────────

_JUDGE_PROMPT = """You are an INDEPENDENT judge scoring a morning brief against the data it was built from.
Score 0-10 on each dimension and answer JSON only:
{"fabrication": <10 = every claim traceable to the data, 0 = invented facts>,
 "voice": <10 = sounds like a trusted chief of staff, 0 = generic assistant filler>,
 "triage": <10 = the right things are in "needs attention", 0 = noise on top>,
 "notes": "<one sentence>"}

DATA MANIFEST:
%s

BRIEF:
%s
"""


def judge_day(res: dict, day: dict, base: datetime) -> dict:
    """One judge call per day on a PINNED model — a judge that drifts is not a baseline."""
    if not res["brief"]:
        return {}
    from source_context import allowed
    if any(not allowed(source) for source in res.get('_source_permissions', [])):
        return {'error': 'source permission changed before judging'}
    manifest = cb.build_data_manifest(_project_inputs(
        res.get('_judge_inputs', ev.resolve_tokens(day, base)['inputs'])))
    # SOTTO_BRIEF_MODEL outranks SOTTO_GEMINI_MODEL in model resolution, and a fallback could
    # swap the model mid-run: clear both, or the brief model could end up judging its own brief.
    pins = {"SOTTO_BRIEF_MODEL": "", "SOTTO_FALLBACK_MODEL": "", "SOTTO_GEMINI_MODEL": judge_model()}
    saved = {name: os.environ.get(name) for name in pins}
    os.environ.update(pins)
    try:
        raw = gemini_call(_JUDGE_PROMPT % (json.dumps(manifest, ensure_ascii=False)[:20000],
                                              res["brief"][:20000]), {})
        return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception as e:  # noqa: BLE001 — an unavailable judge costs a dimension, not the run
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def judge_model() -> str:
    return (os.environ.get("SOTTO_JUDGE_MODEL") or "").strip() or JUDGE_MODEL


def run_signature(live: bool, judge: bool, sample: int, critic_policy: str) -> dict:
    """What a baseline was measured with. A run on another compose or judge model is a different
    measurement, so it is reported as a mismatch rather than compared silently."""
    signature = {"mode": ("live+judge" if live and judge else "live" if live else "dry"),
                 "sample": sample, "critic_policy": critic_policy,
                 "metric_policy": "active-states-and-absolute-entity-error-v2",
                 "chronological_funnel_policy": "event-time-before-day-end-compose-v1"}
    if live:
        signature["compose_model"] = "/".join(gemini_transport.effective_compose_model())
    if live and judge:
        signature["judge_model"] = judge_model()
    return signature


# ── Runner ────────────────────────────────────────────────────────────────────────────────────────

def run(version: str, sample: int, live: bool, judge: bool, allow_unreviewed: bool,
        update: bool, threshold: float, root: str = "", base_override: str = "") -> int:
    if not _valid_threshold(threshold):
        print('Invalid regression threshold: choose a finite number from 0 to 1.')
        return 2
    try:
        corpus = load_corpus(version, root)
    except FileNotFoundError as e:
        print(str(e))
        return 2
    labels = corpus["labels"]
    if corpus['manifest'].get('replay_policy', {}).get('mode') == 'chronological':
        if (0 < sample < len(corpus['days']) or corpus['manifest'].get('seed_ledger')
                or corpus['manifest'].get('preferences')):
            print('Chronological replay requires every retained day and no current-state seeds.')
            return 2
    if not labels.get("reviewed") and (not allow_unreviewed or update):
        print(f"labels.yaml for {version} is still `reviewed: false` — an unreviewed draft is not a "
              f"baseline.\nRun the labeling session (evals/LABELING.md), or pass --allow-unreviewed "
              f"to see the numbers anyway.")
        return 2
    if live and not os.environ.get("GOOGLE_AI_API_KEY"):
        print("--live requires GOOGLE_AI_API_KEY. Aborting.")
        return 2

    # Live replay uses compose_brief's production normalization. Dry replay passes critic=False,
    # so ambient SOTTO_CRITIC cannot change its work or its baseline signature.
    critic_policy = cb._critic_mode() if live else "disabled"

    days = sample_days(corpus["days"], sample)
    frozen = frozen_base(base_override)
    sandbox = _seed_sandbox(corpus, oldest_base(corpus, frozen))
    saved = {k: os.environ.get(k) for k in ("SOTTO_DATA", "SOTTO_TIMEZONE", "SOTTO_CRITIC",
                                            "SOTTO_LLM_STUB", "SOTTO_FALLBACK_MODEL")}
    rows, scores = [], {}
    coverage = {'expected': [day['name'] for day in days], 'executed': [], 'failed': [],
                'unlabeled': [], 'scored': {}}
    tier1 = "on" if live and os.environ.get("GOOGLE_AI_API_KEY") else "OFF (funnel scores are Tier-0 only)"
    print(f"\nSotto GOLDEN evals — {version} · {len(days)}/{len(corpus['days'])} days · "
          f"{'LIVE' if live else 'DRY'} · clock {frozen:%Y-%m-%d} · funnel Tier-1 {tier1}\n" + "=" * 72)
    if corpus['manifest'].get('replay_policy', {}).get('mode') != 'chronological':
        print('Snapshot replay: current retained state may be seeded. This is not chronological learning acceptance.')
    try:
        os.environ["SOTTO_DATA"] = sandbox
        os.environ["SOTTO_TIMEZONE"] = corpus["manifest"].get("timezone") or "+00:00"
        os.environ.pop("SOTTO_LLM_STUB", None)
        # The signature names ONE compose model: a fallback would score another model's briefs
        # against it, so a quota blip fails the day instead.
        os.environ["SOTTO_FALLBACK_MODEL"] = ""
        for day in days:
            base = base_for(day, frozen)
            try:
                res = replay_day(day, base, sandbox, live)
            except Exception as error:
                res = {'exception': f'{type(error).__name__}: {error}', 'brief': ''}
            day_rows = score_day(day, res, labels, live)
            if live and judge and not res["exception"]:
                j = judge_day(res, day, base)
                if (not isinstance(j, dict) or 'error' in j
                        or any(not _finite_number(j.get(k)) or not 0 <= j[k] <= 10
                               for k in ('fabrication', 'voice', 'triage'))):
                    res['exception'] = 'judge execution failed'
                    day_rows.append(('judge', None, 'judge execution failed'))
                else:
                    # One SCALAR row per dimension, normalized 0-1 so --threshold means the same
                    # thing on every metric. (These were once a nested dict that aggregate()
                    # silently dropped — a scored dimension that never reached the baseline.)
                    for k in ("fabrication", "voice", "triage"):
                        if isinstance(j.get(k), (int, float)):
                            day_rows.append((f"judge_{k}", round(float(j[k]) / 10, 3),
                                             textutil._s(j.get("notes")) if k == "fabrication"
                                             else f"judge's {k} score, 0-1"))
            if res['exception']:
                coverage['failed'].append(day['name'])
            else:
                coverage['executed'].append(day['name'])
                scored = [m for m, value, _ in day_rows if value is not None]
                if not scored:
                    coverage['unlabeled'].append(day['name'])
                for metric in scored:
                    coverage['scored'].setdefault(metric, []).append(day['name'])
            print(f"\n{day['name']}  ({day.get('description', '')})")
            for metric, value, detail in day_rows:
                print(f"  {metric:<18} {json.dumps(value)}   — {detail}")
            rows.extend((day["name"], m, v, d) for m, v, d in day_rows)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(sandbox, ignore_errors=True)

    scores = aggregate(rows)
    print("\n" + "=" * 72)
    print("AGGREGATE: " + json.dumps(scores, sort_keys=True))
    run_sig = run_signature(live, judge, sample, critic_policy)
    print('COVERAGE: ' + json.dumps(coverage, sort_keys=True))
    if not labels.get('reviewed'):
        print('RESULT: NO BASELINE (unreviewed labels; numbers only)')
        return 2
    return _baseline_gate(version, scores, update, threshold, run_sig, coverage)


def _finite_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _valid_threshold(value) -> bool:
    return _finite_number(value) and 0 <= value <= 1


def _valid_scores(scores: dict) -> bool:
    return isinstance(scores, dict) and bool(scores) and all(
        _finite_number(value) and (not metric.startswith('judge_') or 0 <= value <= 1)
        for metric, value in scores.items())


def aggregate(rows: list) -> dict:
    """Corpus-level numbers: means of the scalar metrics, micro-averaged F1 of the PRF ones.
    A dict value without "f1" is a shape this function cannot score — it raises rather than
    silently dropping a metric out of the baseline."""
    out, prf = {}, {}
    for _day, metric, value, _detail in rows:
        if isinstance(value, dict) and "f1" in value:
            acc = prf.setdefault(metric, {"tp": 0, "expected": 0, "actual": 0})
            for k in acc:
                acc[k] += value[k]
        elif isinstance(value, dict):
            raise ValueError(f"unaggregatable metric {metric!r}: a dict without 'f1' — "
                             f"emit one scalar row per dimension instead")
        elif isinstance(value, (int, float)):
            if not _valid_scores({metric: value}):
                raise ValueError(f"invalid score for metric {metric!r}")
            out.setdefault(metric, []).append(abs(float(value)) if metric == 'entity_dedup' else float(value))
    agg = {m: round(sum(v) / len(v), 3) for m, v in out.items() if v}
    for metric, a in prf.items():
        p = a["tp"] / a["actual"] if a["actual"] else (1.0 if not a["expected"] else 0.0)
        r = a["tp"] / a["expected"] if a["expected"] else 1.0
        agg[metric] = round(2 * p * r / (p + r), 3) if (p + r) else 0.0
    if agg and not _valid_scores(agg):
        raise ValueError("invalid aggregate scores")
    return agg


def _baseline_path(version: str) -> str:
    return os.path.join(ev._baselines_dir(), f"golden-{version}.json")


def _baseline_gate(version: str, scores: dict, update: bool, threshold: float,
                   run_sig: dict, coverage: dict | None = None) -> int:
    """PASS only against a recorded baseline. No baseline is NO BASELINE (exit 2, the same family
    as a missing corpus) — a gate that passes because it has nothing to compare against is unarmed.
    The comparison walks the BASELINE's metrics, so a metric that vanishes from the current run
    (the exact shape of the judge-dict bug) fails instead of slipping out of the comparison."""
    if not _valid_threshold(threshold):
        print('RESULT: FAIL (invalid regression threshold; baseline unchanged)')
        return 1
    path = _baseline_path(version)
    expected = (coverage or {}).get('expected', [])
    executed = (coverage or {}).get('executed', [])
    failed = (coverage or {}).get('failed', [])
    if (not expected or len(set(expected)) != len(expected) or failed
            or executed != expected or not scores):
        print('INCOMPLETE — failed or unexecuted cases: ' + ', '.join(
            failed or [case for case in expected if case not in executed])
            + ('; no scored metrics' if not scores else ''))
        print('RESULT: FAIL (incomplete execution; baseline unchanged)')
        return 1
    if not _valid_scores(scores):
        print('RESULT: FAIL (invalid scores; baseline unchanged)')
        return 1
    if update:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"scores": scores, "run": run_sig, "coverage": coverage,
                       "date": datetime.now(timezone.utc).strftime("%Y-%m-%d")},
                      f, indent=2, sort_keys=True)
        print(f"Baseline rewritten → {path}")
        return 0
    if not os.path.exists(path):
        print("No golden baseline yet — this run gated NOTHING. Record one with --update-baseline "
              "after the labeling hour.\nRESULT: NO BASELINE")
        return 2
    with open(path, encoding="utf-8") as f:
        doc = json.load(f) or {}
    prev, prev_run = doc.get("scores") or {}, doc.get("run") or {}
    if not _valid_scores(prev):
        print('RESULT: FAIL (invalid recorded baseline scores; baseline unchanged)')
        return 1
    coverage_changed = doc.get('coverage') != coverage or prev_run != run_sig
    if coverage_changed:
        print('COVERAGE MISMATCH — expected/executed/scored cases or run mode differ; '
              'complete and review a new baseline explicitly.')
        for key in sorted(set(prev_run) | set(run_sig)):
            if prev_run.get(key) != run_sig.get(key):
                print(f"  - {key}: {json.dumps(prev_run.get(key))} → {json.dumps(run_sig.get(key))}")
    missing = sorted(m for m in prev if m not in scores)
    # entity_dedup is a signed distance from 0 (+ split identities, - over-merged): drift either way regresses.
    regressions = [(m, prev[m], scores[m]) for m in sorted(prev)
                   if m in scores and ((abs(scores[m]) - abs(prev[m])) if m == 'entity_dedup'
                                       else (prev[m] - scores[m])) > threshold]
    if missing:
        print(f"MISSING — in the baseline but absent from this run: {', '.join(missing)}")
    if regressions:
        print(f"REGRESSIONS beyond {threshold}:")
        for m, a, b in regressions:
            print(f"  - {m}: {a} → {b}")
    if missing or regressions or coverage_changed:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay the Golden Corpus and score it against labels.")
    ap.add_argument("--version", default=DEFAULT_VERSION, help=f"corpus version (default {DEFAULT_VERSION})")
    ap.add_argument("--corpus-dir", dest="corpus_dir", default="",
                    help="corpus location (default evals/corpus/<version>; point it at the volume "
                         "when the corpus lives off-tree)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry", action="store_true", help="offline: funnel + loops + dedup (default)")
    mode.add_argument("--live", action="store_true", help="real Gemini brief per day")
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help=f"evenly-spaced days to replay, 0 = all (default {DEFAULT_SAMPLE})")
    ap.add_argument("--judge", action="store_true", help="live: add the pinned LLM judge")
    ap.add_argument("--allow-unreviewed", dest="allow_unreviewed", action="store_true",
                    help="score against a labels.yaml the owner hasn't reviewed yet")
    ap.add_argument("--update-baseline", dest="update", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.05, help="max allowed drop (default 0.05)")
    ap.add_argument("--frozen-base", dest="base", default="",
                    help="pin the replay clock (YYYY-MM-DD) instead of 'noon today'")
    a = ap.parse_args()
    return run(a.version, a.sample, a.live, a.judge, a.allow_unreviewed, a.update, a.threshold,
               a.corpus_dir, a.base)


if __name__ == "__main__":
    sys.exit(main())
