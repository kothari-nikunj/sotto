"""The Golden Corpus builder — the pseudonymizer and the whole build, on synthetic data.

The corpus itself is the owner's real history and never exists in CI, so these tests stand a
synthetic $SOTTO_DATA up in tmp_path and run the REAL builder over it. What they guard is the part
that must never regress quietly: referential integrity (one human, one alias, everywhere), the
same-domain relationship, the leak scan's refusal to write, and the "scrubbed is not shareable"
mechanics (map outside the corpus, corpus self-ignoring).

Design: docs/plans/golden-corpus.md. Runbook: evals/LABELING.md.
"""
import importlib.util
import json
import os
import stat

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))

_spec = importlib.util.spec_from_file_location(
    "build_golden_corpus", os.path.join(ROOT, "tools", "build_golden_corpus.py"))
bgc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bgc)

_ev = importlib.util.spec_from_file_location("run_evals", os.path.join(ROOT, "evals", "run_evals.py"))
ev = importlib.util.module_from_spec(_ev)
_ev.loader.exec_module(ev)

KEY_A = "ab" * 32
KEY_B = "cd" * 32

# Real identities the synthetic volume knows. Two colleagues share acmecorp.test; a third is
# elsewhere; one address appears ONLY inside message prose (the residual-sweep case).
DANA_PHONE = "+14155552211"
MARCUS_PHONE = "+12069994970"
DANA_EMAIL = "dana@acmecorp.test"
LEO_EMAIL = "leo@acmecorp.test"
MARCUS_EMAIL = "marcus@betaworks.test"
STRANGER_EMAIL = "nobody@unseen.test"
USER_EMAIL = "owner@myco.test"
# Named ONLY inside a person file's `relations:` block (and mentioned in prose) — the identity a
# scrub that walked past frontmatter relations would leak.
ROSA_NAME = "Rosa Iqbal"


def seed_data(root: str) -> str:
    """A $SOTTO_DATA volume with a knowledge graph, a ledger, preferences and a local snapshot."""
    for sub in ("knowledge/people", "knowledge/continuity", "knowledge/companies"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    with open(os.path.join(root, "knowledge", "people", "c_abc123.md"), "w", encoding="utf-8") as f:
        f.write("---\ncanonical_id: c_abc123\nname: Dana Wells\nidentifiers:\n"
                f"  - {DANA_EMAIL}\n  - \"{DANA_PHONE}\"\ncompany: AcmeCorp\nschema_version: 1\n"
                "relations:\n- type: introduced_by\n  slug: c_rosa01\n"
                f"  name: {ROSA_NAME}\n  source: brief_extraction\n  confidence: 0.95\n---\n"
                "\n## Summary\nDana Wells runs partnerships at AcmeCorp.\n")
    with open(os.path.join(root, "knowledge", "companies", "acmecorp.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: AcmeCorp\ndomain: acmecorp.test\n---\n\n## Context\nA partner.\n")
    with open(os.path.join(root, "knowledge", "continuity", "loop-dana.md"), "w", encoding="utf-8") as f:
        f.write("---\nanchor_key: \"email:follow_up:id:%s\"\nstatus: open\naction_type: reply\n"
                "channel: email\ncontact_name: Dana Wells\ncontact_identifier: %s\n"
                "created_at: \"2026-08-06 09:00:00\"\n"
                "summary: \"Dana asked for the redlines — %s made the intro\"\n---\n"
                % (DANA_EMAIL, DANA_EMAIL, ROSA_NAME))
    with open(os.path.join(root, "preferences.json"), "w", encoding="utf-8") as f:
        json.dump({"explicit": {"mute_senders": ["@newsletter.test"], "mute_people": ["Leo Fry"],
                                "tone_notes": ["warm but brief"]}}, f)

    local = {
        "generated_at": "2026-08-09T07:00:00Z", "window_hours": 24,
        "source_status": {"imessage": "ok", "whatsapp": "ok"},
        "contacts": [
            {"name": "Dana Wells", "phones": [DANA_PHONE], "emails": [DANA_EMAIL]},
            {"name": "Marcus Lee", "phones": [MARCUS_PHONE], "emails": [MARCUS_EMAIL]},
            {"name": "Leo Fry", "phones": [], "emails": [LEO_EMAIL]},
        ],
        "imessage": [
            {"handle": MARCUS_PHONE, "is_from_me": False, "is_group_chat": False,
             "timestamp": "2026-08-09 07:30:00",
             "text": f"Dana Wells said the AcmeCorp redlines land today — she's at {DANA_EMAIL}, "
                     f"or loop in {STRANGER_EMAIL}"},
            {"handle": DANA_PHONE, "is_from_me": True, "is_group_chat": False,
             "timestamp": "2026-08-08 18:00:00", "text": "Thanks Marcus, sending now."},
        ],
        "whatsapp": [
            {"contact_jid": f"{DANA_PHONE.lstrip('+')}@s.whatsapp.net", "partner_name": "Dana Wells",
             "is_from_me": False, "is_group_chat": False, "timestamp": "2026-08-09 09:10:00",
             "text": "Can you review by EOD?"},
        ],
        "calls": [{"phone": MARCUS_PHONE, "timestamp": "2026-08-09 08:00:00", "is_outgoing": False,
                   "is_answered": False, "call_type": "phone"}],
        "reminders": [{"title": "Send Dana the pricing model", "due_date": "2026-08-09"}],
    }
    snap = os.path.join(root, "knowledge", "last_local_snapshot.json")
    with open(snap, "w", encoding="utf-8") as f:
        json.dump({"captured_at": "2026-08-09T07:00:00Z", "local": local}, f)

    with open(os.path.join(root, "gmail.json"), "w", encoding="utf-8") as f:
        json.dump([
            {"id": "m1", "threadId": "t-1", "from": f"Dana Wells <{DANA_EMAIL}>", "to": USER_EMAIL,
             "subject": "Redlines for AcmeCorp", "snippet": "Can you sign off before noon?",
             "body": f"Leo Fry is copied at {LEO_EMAIL}. Call me on 415-555-2211.",
             "date": "2026-08-09T09:00:00+00:00", "labelIds": ["INBOX"]},
            {"id": "m2", "threadId": "t-2", "from": f"Marcus Lee <{MARCUS_EMAIL}>", "to": USER_EMAIL,
             "subject": "Pricing", "snippet": "please confirm", "body": "Need the model by tomorrow.",
             "date": "2026-08-08T15:00:00+00:00", "labelIds": ["INBOX"]},
        ], f)
    with open(os.path.join(root, "cal.json"), "w", encoding="utf-8") as f:
        json.dump([{"id": "evt-1", "summary": "AcmeCorp <> partnership",
                    "start": "2026-08-09T15:00:00+00:00", "end": "2026-08-09T16:00:00+00:00",
                    "description": "Walk Dana Wells through the redlines.",
                    "attendees": [{"email": USER_EMAIL, "displayName": "Owner", "self": True},
                                  {"email": DANA_EMAIL, "displayName": "Dana Wells"},
                                  {"email": LEO_EMAIL, "displayName": "Leo Fry"}]}], f)
    with open(os.path.join(root, "granola.json"), "w", encoding="utf-8") as f:
        json.dump({"meetings": [{"title": "AcmeCorp kickoff", "date": "2026-08-08",
                                 "attendee_emails": [DANA_EMAIL],
                                 "ai_summary": "Dana to send redlines."}]}, f)
    return root


def build_corpus(tmp_path, key=KEY_A, monkeypatch=None, mutate=None, **overrides) -> dict:
    """Run the REAL builder over a synthetic volume. Returns paths + the parsed output.
    `mutate(data_root)` edits the seeded volume before the build (extra contacts, extra prose)."""
    data = seed_data(str(tmp_path / "data"))
    if mutate:
        mutate(data)
    out = str(tmp_path / "corpus" / "corpus-v1")
    map_path = str(tmp_path / "keys" / "corpus-v1.map.json")
    argv = ["--out", out, "--map", map_path, "--days", "5", "--end", "2026-08-09",
            "--user-email", USER_EMAIL, "--gmail", os.path.join(data, "gmail.json"),
            "--calendar", os.path.join(data, "cal.json"),
            "--granola", os.path.join(data, "granola.json")]
    for k, v in overrides.items():
        argv += [f"--{k.replace('_', '-')}"] + ([v] if v is not True else [])
    env = {"SOTTO_DATA": data, "SOTTO_CORPUS_KEY": key}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        import sys
        saved_argv = sys.argv
        sys.argv = ["build_golden_corpus.py"] + argv
        rc = bgc.main()
        sys.argv = saved_argv
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    result = {"rc": rc, "data": data, "out": out, "map": map_path, "days": {}}
    if rc == 0:
        with open(os.path.join(out, "manifest.json"), encoding="utf-8") as f:
            result["manifest"] = json.load(f)
        for entry in result["manifest"]["days"]:
            with open(os.path.join(out, entry["file"]), encoding="utf-8") as f:
                result["days"][entry["name"]] = json.load(f)
        with open(map_path, encoding="utf-8") as f:
            result["map_data"] = json.load(f)
        with open(os.path.join(out, "labels.yaml"), encoding="utf-8") as f:
            result["labels_text"] = f.read()
    return result


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return build_corpus(tmp_path_factory.mktemp("golden"))


def _blob(corpus) -> str:
    return json.dumps({"m": corpus["manifest"], "d": corpus["days"]}, ensure_ascii=False)


# ── Layer 1: keyed pseudonymization with referential integrity ────────────────────────────────────

def test_build_succeeds_and_emits_every_day_with_data(corpus):
    assert corpus["rc"] == 0
    assert set(corpus["days"]) == {"day-00", "day-01"}       # only days that HAVE data


def test_one_human_is_one_alias_across_every_channel(corpus):
    """Dana is a phone on iMessage, a JID on WhatsApp, an address on Gmail and a name in Contacts.
    All four must land on the SAME fake human — that is the whole point of the corpus."""
    person = next(p for p in corpus["map_data"]["people"] if "Dana Wells" in p["real_names"])
    alias, blob = person["alias"]["name"], _blob(corpus)
    assert set(person["real_identifiers"]) >= {DANA_EMAIL, DANA_PHONE[-10:]}
    assert alias in blob
    fake_email = corpus["map_data"]["emails"][DANA_EMAIL]
    fake_phone = corpus["map_data"]["phones"][DANA_PHONE[-10:]]
    assert fake_email.startswith(person["alias"]["first"].lower())
    day = corpus["days"]["day-00"]
    assert day["inputs"]["local"]["whatsapp"][0]["partner_name"] == alias
    assert fake_phone.lstrip("+") in day["inputs"]["local"]["whatsapp"][0]["contact_jid"]
    assert any(fake_email in e["from"] for e in day["inputs"]["google"]["emails"])


def test_colleagues_stay_colleagues_and_strangers_stay_strangers(corpus):
    """Same real domain → same fake domain (Dana and Leo still work together); a different real
    domain → a different fake one (Marcus still doesn't)."""
    emails = corpus["map_data"]["emails"]
    dana_dom = emails[DANA_EMAIL].split("@")[1]
    leo_dom = emails[LEO_EMAIL].split("@")[1]
    marcus_dom = emails[MARCUS_EMAIL].split("@")[1]
    assert dana_dom == leo_dom
    assert marcus_dom != dana_dom
    assert dana_dom.endswith(".example")          # RFC 2606 reserved — can never be a real company


def test_the_map_is_key_scoped_and_the_build_is_reproducible(tmp_path):
    """Same volume + same key → byte-identical days (so scores compare across rebuilds); a different
    key → different aliases (so the map is the only thing that reverses the scrub)."""
    a1 = build_corpus(tmp_path / "a1")
    a2 = build_corpus(tmp_path / "a2")
    b = build_corpus(tmp_path / "b", key=KEY_B)
    assert a1["days"] == a2["days"]
    assert b["days"] != a1["days"]
    assert a1["manifest"]["key_fingerprint"] != b["manifest"]["key_fingerprint"]


# ── Layer 2 + the residual sweep: nothing real survives ───────────────────────────────────────────

def test_no_real_identity_survives_anywhere_in_the_corpus(corpus):
    blob = _blob(corpus) + corpus["labels_text"]
    for needle in ("Dana", "Wells", "Marcus", "Leo Fry", "AcmeCorp", "acmecorp.test",
                   "betaworks.test", DANA_EMAIL, MARCUS_EMAIL, LEO_EMAIL,
                   DANA_PHONE, MARCUS_PHONE, DANA_PHONE[-10:], "myco.test"):
        assert needle.lower() not in blob.lower(), f"{needle} leaked into the corpus"


def test_in_text_mentions_are_rewritten_to_the_SAME_alias_as_the_structure(corpus):
    """The 'NER' layer: a name written in prose must become the same fake human the structural
    fields got, or the corpus stops being referentially intact the moment anyone talks about anyone."""
    person = next(p for p in corpus["map_data"]["people"] if "Dana Wells" in p["real_names"])
    text = corpus["days"]["day-00"]["inputs"]["local"]["imessage"][0]["text"]
    assert person["alias"]["name"] in text
    assert corpus["map_data"]["emails"][DANA_EMAIL] in text
    assert corpus["map_data"]["companies"]["AcmeCorp"] in text


def test_an_address_the_graph_never_saw_is_still_swept(corpus):
    """The residual sweep is what makes 'imperfect NER' survivable: an address that appears ONLY in
    message prose is pseudonymized too, even though no contact or graph file ever mentioned it."""
    text = corpus["days"]["day-00"]["inputs"]["local"]["imessage"][0]["text"]
    assert STRANGER_EMAIL not in text
    assert corpus["map_data"]["emails"][STRANGER_EMAIL] in text


def test_bare_string_lists_are_scrubbed_not_walked_past(corpus):
    """contacts[].emails / attendee_emails are LISTS OF STRINGS — the shape a naive recursive walk
    sails straight past. This is how PII escapes, so it gets its own test."""
    for c in corpus["days"]["day-00"]["inputs"]["local"]["contacts"]:
        for v in c.get("emails", []) + c.get("phones", []):
            assert "acmecorp" not in v and "betaworks" not in v
            assert v.startswith("+1555") or v.endswith(".example")
    prefs = corpus["manifest"]["preferences"]
    assert prefs["mute_senders"] == ["@" + corpus["map_data"]["domains"]["newsletter.test"]]
    assert prefs["mute_people"] != ["Leo Fry"]


def test_a_named_sensitive_sender_is_dropped_entirely(tmp_path):
    """Layer 3, the safe half: the owner names a person and NOTHING of theirs enters the corpus —
    not their messages, not their emails, not their calls. Named in REAL terms, before the scrub."""
    res = build_corpus(tmp_path / "dropped", drop_sender=MARCUS_PHONE)
    assert res["rc"] == 0
    marcus = next(p for p in res["map_data"]["people"] if MARCUS_PHONE[-10:] in p["real_identifiers"])
    day = res["days"]["day-00"]
    assert not day["inputs"]["local"]["imessage"]
    assert not day["inputs"]["local"]["calls"]
    assert marcus["alias"]["name"] not in json.dumps(day["inputs"]["local"]["imessage"])


def _add_person_and_prose(name: str, email: str, prose: str):
    """Volume mutation: one more contact plus one iMessage mentioning them, inside the window."""
    def mutate(root):
        snap_path = os.path.join(root, "knowledge", "last_local_snapshot.json")
        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)
        snap["local"]["contacts"].append({"name": name, "phones": [], "emails": [email]})
        snap["local"]["imessage"].append({"handle": MARCUS_PHONE, "is_from_me": False,
                                          "is_group_chat": False,
                                          "timestamp": "2026-08-09 10:00:00", "text": prose})
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(snap, f)
    return mutate


def test_a_stop_listed_first_name_is_outside_the_promise_and_outside_the_scan(tmp_path):
    """The scrubber deliberately never rewrites a bare stop-listed name ('Grace', 'Mark', 'Will') —
    rewriting every English word that doubles as a name would eat the corpus. The scan must share
    that definition: a contact named Grace may not fail the build (the owner's real 617-person map
    did exactly this). Her FULL name is still scrubbed, and ordinary words stay intact."""
    res = build_corpus(tmp_path / "stop", mutate=_add_person_and_prose(
        "Grace Porter", "grace@porterco.test",
        "Grace Porter said Grace will chase it — graceful as ever."))
    assert res["rc"] == 0
    blob = _blob(res)
    assert "Grace Porter" not in blob and "Porter" not in blob
    assert "graceful" in blob                     # ordinary words untouched
    assert "Grace will chase" in blob             # the accepted, documented cost


def test_a_name_inside_a_longer_word_is_not_a_leak_but_standalone_is_rewritten(tmp_path):
    """'Rita' inside 'margarita' is not Rita — the old raw-substring scan failed builds on exactly
    this. Whole-word occurrences still get rewritten and would still be caught."""
    res = build_corpus(tmp_path / "infix", mutate=_add_person_and_prose(
        "Rita Okafor", "rita@okaforlabs.test",
        "margarita night — Rita Okafor is in, Rita confirmed."))
    assert res["rc"] == 0
    blob = _blob(res)
    assert "margarita" in blob
    assert "Okafor" not in blob
    assert "Rita" not in blob                     # standalone token: rewritten, everywhere


def test_handles_and_url_slugs_are_scrubbed(tmp_path):
    """'@dana' and 'linkedin.com/in/danawells' are exactly how names travel in real messages; both
    escaped the old rewriter (the @ lookbehind, the squashed no-boundary form)."""
    res = build_corpus(tmp_path / "slug", mutate=_add_person_and_prose(
        "Zuri Mbeki", "zuri@mbekico.test",
        "ping @dana or grab linkedin.com/in/danawells before the call"))
    assert res["rc"] == 0
    blob = _blob(res).lower()
    assert "danawells" not in blob
    assert "@dana" not in blob


def test_leak_scan_and_rewriter_share_one_definition_of_scrubbed():
    """Unit contract: the scan's needle IS the rewrite regex. A whole-word survivor is a hit; the
    same letters inside another word are not; a scrubbed text scans clean."""
    im = bgc.IdentityMap(bytes.fromhex(KEY_A))
    im.add(["Rita Okafor"], ["rita@okaforlabs.test"])
    im.freeze()
    hits = dict(bgc.leak_scan("dinner with Rita tonight, margarita after", im))
    assert hits.get("rita") == 1 and len(hits) == 1
    assert bgc.leak_scan(im.rewrite_text("dinner with Rita Okafor, margarita after"), im) == []


def test_a_fake_alias_never_collides_with_a_real_name(tmp_path):
    """A real contact named straight out of the alias pools ('Lena Ashby') must not reappear as
    someone's fake — the scan could no longer tell fake from leak."""
    res = build_corpus(tmp_path / "collide", mutate=_add_person_and_prose(
        "Lena Ashby", "lena@ashbyworks.test", "Lena Ashby wants the doc today."))
    assert res["rc"] == 0
    blob = _blob(res)
    assert "Lena" not in blob and "Ashby" not in blob
    for p in res["map_data"]["people"]:
        assert {p["alias"]["first"].lower(), p["alias"]["last"].lower()}.isdisjoint({"lena", "ashby"})


def test_archived_snapshots_extend_the_corpus_message_history(tmp_path):
    """One live snapshot holds ~a day of messages — the owner's first 42-day corpus had two days.
    Dated archives under knowledge/snapshots/ are merged in (exact duplicates collapsed), so the
    corpus grows a day of message history for every day the archive has been accumulating."""
    def mutate(root):
        arch = os.path.join(root, "knowledge", "snapshots")
        os.makedirs(arch)
        older = {"captured_at": "2026-08-06T07:00:00Z",
                 "local": {"generated_at": "2026-08-06T07:00:00Z",
                           "source_status": {"imessage": "ok"},
                           "imessage": [
                               {"handle": MARCUS_PHONE, "is_from_me": False, "is_group_chat": False,
                                "timestamp": "2026-08-06 09:15:00", "text": "archived-day ping"},
                               # exact duplicate of a live-snapshot row — must collapse, not double
                               {"handle": MARCUS_PHONE, "is_from_me": False, "is_group_chat": False,
                                "timestamp": "2026-08-09 07:30:00",
                                "text": f"Dana Wells said the AcmeCorp redlines land today — she's "
                                        f"at {DANA_EMAIL}, or loop in {STRANGER_EMAIL}"}]}}
        with open(os.path.join(arch, "2026-08-06.json"), "w", encoding="utf-8") as f:
            json.dump(older, f)
    res = build_corpus(tmp_path / "arch", mutate=mutate)
    assert res["rc"] == 0
    assert "day-03" in res["days"]                              # 2026-08-06, from the archive alone
    day0 = res["days"]["day-00"]
    assert len(day0["inputs"]["local"]["imessage"]) == 1        # the duplicate collapsed


def test_shapeless_registered_identifiers_are_swept_everywhere():
    """Ledger anchor_keys carry thread ids, event ids, graph slugs and masked phones — registered
    identifiers no email or phone regex can recognize. The owner's real build leaked 35 of them.
    The sweep replaces their exact bytes wherever they appear, with a stable keyed hash."""
    im = bgc.IdentityMap(bytes.fromhex(KEY_A))
    im.add(["Aalap Sanghvi"], ["aalap_sanghvi"])
    im.add([], ["19fa89c963f80869", "32665(smsft)", "+120****1166"])
    im.freeze()
    text = ("affinity:follow_up:id:aalap_sanghvi thread:19fa89c963f80869 "
            "handle 32665(smsft) call +120****1166 back")
    out = im.rewrite_text(text)
    for real in ("aalap_sanghvi", "19fa89c963f80869", "32665(smsft)", "+120****1166"):
        assert real not in out
    assert out == im.rewrite_text(text)                       # stable across mentions
    assert bgc.leak_scan(out, im) == []
    assert im.identifier_for("19fa89c963f80869") == im.identifier_for("19fa89c963f80869")
    assert "1555" not in im.identifier_for("32665(smsft)")    # letters ⇒ not phone-shaped


def test_real_subdomains_are_pseudonymized_in_fake_domains():
    """'andrew.cmu.edu' names a person — the fake domain must not keep the real subdomain."""
    im = bgc.IdentityMap(bytes.fromhex(KEY_A))
    im.add(["Andrew Lee"], ["prof@andrew.cmu.test"])
    im.freeze()
    fake = im.email_for("prof@andrew.cmu.test")
    assert "andrew" not in fake.lower() and "cmu" not in fake.lower()


def test_newsletter_names_do_not_eat_schema_enums_or_prose():
    """Senders are 'named' Morning Brew or Deadline; their tokens are also schema enums and plain
    English. Bare tokens are stop-listed (never rewritten, never scanned); the WHOLE name still is."""
    im = bgc.IdentityMap(bytes.fromhex(KEY_A))
    im.add(["Morning Brew"], ["crew@morningbrew.test"])
    im.freeze()
    assert bgc.scrub({"type": "morning"}, im) == {"type": "morning"}
    out = im.rewrite_text("the Morning Brew digest lands every morning")
    assert "Morning Brew" not in out
    assert out.endswith("every morning")
    assert bgc.leak_scan({"type": "morning", "deadline": "Monday morning"}, im) == []


def test_scan_reads_values_as_data_and_keys_as_schema():
    """A registered name matching a JSON KEY is not a leak ('google' the input key); the same name
    in a VALUE is. Hard identifiers stay leaks even as keys — a dict keyed by an address leaks."""
    im = bgc.IdentityMap(bytes.fromhex(KEY_A))
    im.add(["Rita Okafor"], ["rita@okaforlabs.test"])
    im.freeze()
    assert bgc.leak_scan({"rita": "all clean here"}, im) == []
    assert dict(bgc.leak_scan({"note": "ask Rita"}, im)).get("rita") == 1
    assert bgc.leak_scan({"rita@okaforlabs.test": "x"}, im) != []


def test_scrub_treats_every_unrouted_string_as_prose():
    """Fail toward scrubbing: the Bridge and the ledger grow fields faster than any allowlist can
    chase — the owner's real volume leaked 162 names through exactly such fields. Unknown string
    fields, unknown lists and non-identifier ident-field values all get rewritten; only
    timestamp-shaped strings pass through (the tokenizer owns them)."""
    im = bgc.IdentityMap(bytes.fromhex(KEY_A))
    im.add(["Rita Okafor"], ["rita@okaforlabs.test"])
    im.freeze()
    out = bgc.scrub({"brand_new_field": "Rita Okafor said hi",
                     "nested": [{"unknown_note": "call Rita back"}],
                     "bare_list": ["Rita Okafor owes a reply"],
                     "handle": "Rita Okafor",                    # ident field, not identifier-shaped
                     "seen_at_times": ["2026-08-09 07:30:00"],
                     "some_clock": "2026-08-09T07:30:00Z"}, im)
    blob = json.dumps(out)
    assert "Rita" not in blob and "Okafor" not in blob
    assert out["seen_at_times"] == ["2026-08-09 07:30:00"]
    assert out["some_clock"] == "2026-08-09T07:30:00Z"


def test_alias_allocation_survives_a_large_population_with_pool_collisions():
    """The owner's real 617-person volume exhausted the old allocator (its two-stride walk reached
    only 120 of the 720 first×last combos, and the avoid-set shrank that further). 750 people plus
    EVERY pool first name existing as a real contact must still allocate — unique aliases, none
    colliding with a real name."""
    im = bgc.IdentityMap(bytes.fromhex(KEY_A))
    for i in range(750):
        im.add([f"Bulkperson{i:03d} Qz{i:03d}"], [f"p{i:03d}@bulk{i % 40:02d}.test"])
    for first in bgc.FIRST_NAMES:
        im.add([f"{first} Vex{first.lower()}"], [f"{first.lower()}@vexco.test"])
    im.freeze()
    fulls = [a["name"] for a in im.alias.values()]
    assert len(fulls) == len(im._groups) and len(fulls) == len(set(fulls))
    pool_firsts = {f.lower() for f in bgc.FIRST_NAMES}
    assert all(a["first"].lower() not in pool_firsts for a in im.alias.values())


def test_the_builder_refuses_to_write_a_leaking_corpus(tmp_path, monkeypatch):
    """The leak scan is a gate, not a report: a scrub that no-ops must leave NOTHING on disk."""
    monkeypatch.setattr(bgc, "scrub", lambda obj, im: obj)
    res = build_corpus(tmp_path / "leak")
    assert res["rc"] == 3
    assert not os.path.exists(os.path.join(res["out"], "manifest.json"))


# ── Timestamps: absolute clock → the harness's relative tokens ────────────────────────────────────

def test_every_timestamp_became_a_rebasable_token(corpus):
    from datetime import datetime
    day = corpus["days"]["day-00"]
    assert "2026-08-09" not in json.dumps(day)           # no wall-clock date survives
    base = datetime(2030, 3, 4, 12, 0, 0)
    fx = ev.resolve_tokens(day, base)
    ts = fx["inputs"]["local"]["imessage"][0]["timestamp"]
    assert ts == "2030-03-04 07:30:00"                    # 07:30 that day, re-based intact
    assert fx["inputs"]["google"]["events"][0]["start"].startswith("2030-03-04T15:00:00")


def test_a_day_carries_only_the_contacts_its_own_messages_reference(corpus):
    """Day files stay small and referentially complete: Leo never texts, so Leo is not in the day's
    Contacts — but he is still the same fake human wherever he does appear."""
    names = {c["name"] for c in corpus["days"]["day-00"]["inputs"]["local"]["contacts"]}
    leo = next(p for p in corpus["map_data"]["people"] if LEO_EMAIL in p["real_identifiers"])
    assert leo["alias"]["name"] not in names
    assert leo["alias"]["name"] in _blob(corpus)          # present via the event attendee list


# ── "Scrubbed" never means "shareable" ────────────────────────────────────────────────────────────

def test_the_identity_map_lives_outside_the_corpus_and_is_owner_only(corpus):
    assert not corpus["map"].startswith(corpus["out"])
    assert stat.S_IMODE(os.stat(corpus["map"]).st_mode) == 0o600
    assert corpus["map_data"]["key"]


def test_the_corpus_carries_no_key_material_and_self_ignores(corpus):
    """The map is never needed to RUN evals — so the corpus must not contain it. And a corpus that
    self-ignores cannot be `git add`ed by accident, whatever the repo's .gitignore says."""
    assert "key" not in corpus["manifest"]
    assert corpus["manifest"]["key_fingerprint"] not in ("", None)
    assert KEY_A not in _blob(corpus)
    with open(os.path.join(corpus["out"], ".gitignore"), encoding="utf-8") as f:
        assert "*" in f.read().split("\n")


def test_the_map_refuses_to_be_written_inside_the_corpus(tmp_path):
    res = build_corpus(tmp_path / "inside", map=str(tmp_path / "inside" / "corpus" / "corpus-v1" / "m.json"))
    assert res["rc"] == 2


# ── The labeling handoff ──────────────────────────────────────────────────────────────────────────

def test_labels_are_drafted_unreviewed_with_real_ground_truth_entity_counts(corpus):
    import yaml
    labels = yaml.safe_load(corpus["labels_text"])
    assert labels["reviewed"] is False, "a draft must never claim to be reviewed"
    day = labels["days"]["day-00"]
    assert day["open_loops_after"] is None                 # the owner fills this; null = unscored
    assert day["entity_count"] > 0                         # ground truth, from the map not the model
    assert set(day["needs_attention"]) | set(day["not_needs_attention"])
    assert set(day["nudge"].values()) <= {"nudge", "queue", "drop"}


def test_existing_labels_are_never_clobbered_by_a_rebuild(tmp_path):
    """The owner's hour is the scarce ingredient. A rebuild writes labels.draft.yaml beside his
    labels.yaml — it never overwrites it."""
    res = build_corpus(tmp_path / "keep")
    with open(os.path.join(res["out"], "labels.yaml"), "w", encoding="utf-8") as f:
        f.write("corpus: corpus-v1\nreviewed: true\ndays: {}\n")
    again = build_corpus(tmp_path / "keep")
    with open(os.path.join(again["out"], "labels.yaml"), encoding="utf-8") as f:
        assert "reviewed: true" in f.read()
    assert os.path.exists(os.path.join(again["out"], "labels.draft.yaml"))


def test_a_name_known_only_from_a_relation_is_pseudonymized_too(corpus):
    """Relations carry a NAME as well as a slug. The slug is a canonical_id hash (no PII), but the
    name is a real human — and one an edge can name without any message, contact or calendar entry
    ever mentioning them. The builder registers relation names as identities, so the same person
    gets the same alias in prose that a contact would."""
    blob = _blob(corpus) + corpus["labels_text"]
    for needle in (ROSA_NAME, "Rosa", "Iqbal"):
        assert needle.lower() not in blob.lower(), f"{needle} leaked out of a relations block"
    person = next(p for p in corpus["map_data"]["people"] if ROSA_NAME in p["real_names"])
    summaries = " ".join(e["frontmatter"].get("summary", "")
                         for e in corpus["manifest"]["seed_ledger"])
    assert person["alias"]["name"] in summaries
