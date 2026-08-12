#!/usr/bin/env python3
"""
run_golden.py — replay the Golden Corpus through the REAL pipeline and score it against the owner's
labels (docs/plans/golden-corpus.md, evals/LABELING.md).

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

Two standing rules:
  · A corpus is real user data. It never leaves the owner's infrastructure — gitignored, deleted by
    tools/prepare-public-repo.sh, and guarded there (CORPUS GUARD).
  · Unreviewed labels are not a baseline. This harness refuses to score against a labels.yaml that
    still says `reviewed: false` unless you pass --allow-unreviewed.
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

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
    return frozen - timedelta(days=int(day.get("offset_days", 0)))


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
    """Offline extraction: structurally valid, deliberately empty. Dry mode scores the deterministic
    half of the pipeline; inventing brief prose here would only score the stub."""
    return json.dumps({"brief_markdown": "", "actions": [], "extracted_knowledge": {}})


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
        if isinstance(fm, dict) and fm.get("status") == "open" and fm.get("anchor_key"):
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


def replay_day(day: dict, base: datetime, sandbox: str, live: bool) -> dict:
    """One corpus day through the real pipeline: re-base its tokens onto `base`, compose, resolve
    continuity, then push every inbound item through the event funnel one at a time."""
    fx = ev.resolve_tokens(day, base)
    inputs = fx["inputs"]
    # The brief reasons about the day being REPLAYED, not the wall clock: without this every ledger
    # age in the prompt is inflated by (today − this corpus day), up to the length of the corpus.
    inputs["now"] = base.strftime("%Y-%m-%d %H:%M:%S")
    result = {"name": day["name"], "exception": None, "brief": "", "actions": [],
              "verdicts": {}, "open_loops": set(), "chases": {}, "entities": 0}
    try:
        out = cb.compose(inputs, llm=(gemini_call if live else _stub_llm), critic=False)
        result["brief"] = out.get("brief_markdown", "")
        result["actions"] = [a for a in (out.get("actions") or []) if isinstance(a, dict)]
        cr.resolve({"today": base.strftime("%Y-%m-%d"),
                    "signals": fx.get("signals", {}),
                    "local": inputs.get("local", {}),
                    "events": inputs.get("google", {}).get("events", []),
                    "new_actions": result["actions"] or fx.get("new_actions", [])}, base)
        anchors = _open_anchors(sandbox)
        result["open_loops"] = set(anchors)
        result["chases"] = {k: v["chased_count"] for k, v in anchors.items() if v["chased_count"]}
        result["entities"] = _distinct_entities(inputs)
        result["verdicts"] = _replay_funnel(fx, base)
    except Exception as e:  # noqa: BLE001 — one bad day must not abort a 42-day replay
        result["exception"] = f"{type(e).__name__}: {e}"
    return result


def _replay_funnel(fx: dict, base: datetime) -> dict:
    """Each inbound item triaged AS IF IT HAD JUST ARRIVED — its own timestamp is the clock, so
    quiet hours, cooldowns and the daily interrupt budget all land where they really would."""
    te = importlib.import_module("triage_event")
    verdicts = {}
    for e in funnel_events(fx):
        ts = timeutil._parse_ts(textutil._s(e.get("timestamp")) or textutil._s(e.get("date"))) or base
        if ts.tzinfo is not None:
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        now = ts + timedelta(minutes=1)
        try:
            out = te.triage({"events": [e]}, now_local=now, now_utc=now.replace(tzinfo=timezone.utc))
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
    manifest = cb.build_data_manifest(ev.resolve_tokens(day, base)["inputs"])
    saved = os.environ.get("SOTTO_GEMINI_MODEL")
    os.environ["SOTTO_GEMINI_MODEL"] = os.environ.get("SOTTO_JUDGE_MODEL", JUDGE_MODEL)
    try:
        raw = gemini_call(_JUDGE_PROMPT % (json.dumps(manifest, ensure_ascii=False)[:20000],
                                              res["brief"][:20000]), {})
        return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception as e:  # noqa: BLE001 — an unavailable judge costs a dimension, not the run
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        if saved is None:
            os.environ.pop("SOTTO_GEMINI_MODEL", None)
        else:
            os.environ["SOTTO_GEMINI_MODEL"] = saved


# ── Runner ────────────────────────────────────────────────────────────────────────────────────────

def run(version: str, sample: int, live: bool, judge: bool, allow_unreviewed: bool,
        update: bool, threshold: float, root: str = "", base_override: str = "") -> int:
    try:
        corpus = load_corpus(version, root)
    except FileNotFoundError as e:
        print(str(e))
        return 2
    labels = corpus["labels"]
    if not labels.get("reviewed") and not allow_unreviewed:
        print(f"labels.yaml for {version} is still `reviewed: false` — an unreviewed draft is not a "
              f"baseline.\nRun the labeling session (evals/LABELING.md), or pass --allow-unreviewed "
              f"to see the numbers anyway.")
        return 2
    if live and not os.environ.get("GOOGLE_AI_API_KEY"):
        print("--live requires GOOGLE_AI_API_KEY. Aborting.")
        return 2

    days = sample_days(corpus["days"], sample)
    frozen = frozen_base(base_override)
    sandbox = _seed_sandbox(corpus, oldest_base(corpus, frozen))
    saved = {k: os.environ.get(k) for k in ("SOTTO_DATA", "SOTTO_TIMEZONE", "SOTTO_CRITIC",
                                            "SOTTO_LLM_STUB")}
    rows, scores = [], {}
    tier1 = "on" if os.environ.get("GOOGLE_AI_API_KEY") else "OFF (funnel scores are Tier-0 only)"
    print(f"\nSotto GOLDEN evals — {version} · {len(days)}/{len(corpus['days'])} days · "
          f"{'LIVE' if live else 'DRY'} · clock {frozen:%Y-%m-%d} · funnel Tier-1 {tier1}\n" + "=" * 72)
    try:
        os.environ["SOTTO_DATA"] = sandbox
        os.environ["SOTTO_TIMEZONE"] = corpus["manifest"].get("timezone") or "+00:00"
        os.environ.pop("SOTTO_CRITIC", None)
        os.environ.pop("SOTTO_LLM_STUB", None)
        for day in days:
            base = base_for(day, frozen)
            res = replay_day(day, base, sandbox, live)
            day_rows = score_day(day, res, labels, live)
            if live and judge:
                j = judge_day(res, day, base)
                if j and "error" not in j:
                    day_rows.append(("judge", {k: j.get(k) for k in ("fabrication", "voice", "triage")},
                                     textutil._s(j.get("notes"))))
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
    return _baseline_gate(version, scores, update, threshold)


def aggregate(rows: list) -> dict:
    """Corpus-level numbers: means of the scalar metrics, micro-averaged F1 of the PRF ones."""
    out, prf = {}, {}
    for _day, metric, value, _detail in rows:
        if isinstance(value, dict) and "f1" in value:
            acc = prf.setdefault(metric, {"tp": 0, "expected": 0, "actual": 0})
            for k in acc:
                acc[k] += value[k]
        elif isinstance(value, (int, float)):
            out.setdefault(metric, []).append(float(value))
    agg = {m: round(sum(v) / len(v), 3) for m, v in out.items() if v}
    for metric, a in prf.items():
        p = a["tp"] / a["actual"] if a["actual"] else (1.0 if not a["expected"] else 0.0)
        r = a["tp"] / a["expected"] if a["expected"] else 1.0
        agg[metric] = round(2 * p * r / (p + r), 3) if (p + r) else 0.0
    return agg


def _baseline_path(version: str) -> str:
    return os.path.join(ev._baselines_dir(), f"golden-{version}.json")


def _baseline_gate(version: str, scores: dict, update: bool, threshold: float) -> int:
    path = _baseline_path(version)
    if update:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"scores": scores, "date": datetime.now(timezone.utc).strftime("%Y-%m-%d")},
                      f, indent=2, sort_keys=True)
        print(f"Baseline rewritten → {path}")
        return 0
    if not os.path.exists(path):
        print("No golden baseline yet — run with --update-baseline to record one.\nRESULT: PASS")
        return 0
    with open(path, encoding="utf-8") as f:
        prev = (json.load(f) or {}).get("scores") or {}
    regressions = [(m, prev[m], scores[m]) for m in scores
                   if m in prev and (prev[m] - scores[m]) > threshold]
    if regressions:
        print(f"REGRESSIONS beyond {threshold}:")
        for m, a, b in regressions:
            print(f"  - {m}: {a} → {b}")
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
