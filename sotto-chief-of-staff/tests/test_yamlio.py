"""The fast and the pure-Python safe loaders must build identical objects on frontmatter shapes."""
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "_shared", "lib"))
import yamlio  # noqa: E402

TRICKY = """\
schema: 2
canonical_id: p_01HZX
name: "Zoë Müller-O'Brien 🚀"
plain_unicode: Łukasz Żółć — naïve café
title: 'CEO: Acme, Inc.'
url: https://example.com/a?b=1#c
time_like: 12:30:45
colon_tail: "ends with colon:"
due: 2026-11-09
created_at: 2026-11-09T08:15:00Z
offset_ts: 2026-11-09 08:15:00.25 -08:00
quoted_date: "2026-11-09"
yes_plain: yes
no_plain: no
on_plain: on
off_plain: Off
yes_quoted: "yes"
no_quoted: 'no'
on_quoted: "on"
true_word: True
null_word: ~
empty:
phone_quoted: "+14155550123"
phone_plain: +14155550123
phone_digits: 4155550123
phone_leading_zero: 0044207946000
phone_dashed: 415-555-0123
octal_like: 0o17
hex_like: 0x1F
sexagesimal: 1:30
float_like: 1.50
exp_like: 1e3
version: 1.2.3
inf: .inf
underscored: 1_000
multiline_literal: |
  line one: with colon
  line two — "quoted"

  after blank
multiline_folded: >-
  folded text
  continues here
multiline_plain: first part
  second part
identifiers:
  - "+14155550123"
  - person@example.com
  - 12345@lid
facts:
  f_1: {text: "Said \\"hi\\"\\n\\tthere", status: active, confidence: 0.9, refs: [a, b]}
  f_2:
    text: 'it''s # not a comment'
    observed: 2026-01-02
anchor: &a {k: v}
alias: *a
merged:
  <<: *a
  extra: 1
? complex key
: value
binary: !!binary aGVsbG8=
a_set: !!set {x, y}
"""

MALFORMED = [
    "a: [1, 2",
    "a: b: c",
    "a: 1\n b: 2\n\tc: 3",
    "key: !!python/object/apply:os.system ['true']",
    "a: *missing",
    "a: 'unclosed",
    "- a\nb: c",
]


def _both(text):
    return yaml.load(text, Loader=yaml.SafeLoader), yaml.load(text, Loader=yaml.CSafeLoader)  # noqa: S506


@pytest.mark.skipif(not yaml.__with_libyaml__, reason="PyYAML built without libyaml")
def test_c_and_python_safe_loaders_build_identical_frontmatter():
    assert yamlio.SAFE_LOADER is yaml.CSafeLoader and "\t" not in TRICKY
    slow, fast = _both(TRICKY)
    assert fast == slow
    assert {k: type(v) for k, v in fast.items()} == {k: type(v) for k, v in slow.items()}
    assert yamlio.safe_load(TRICKY) == slow
    assert fast["yes_plain"] is True and fast["on_quoted"] == "on" and fast["no_quoted"] == "no"
    assert fast["phone_quoted"] == "+14155550123" and isinstance(fast["phone_plain"], int)
    for scalar in ("", "# only a comment\n", "{}", "just text", "- 1\n- two\n"):
        slow, fast = _both(scalar)
        assert fast == slow and type(fast) is type(slow)


@pytest.mark.skipif(not yaml.__with_libyaml__, reason="PyYAML built without libyaml")
@pytest.mark.parametrize("text", MALFORMED)
def test_both_loaders_reject_the_same_malformed_frontmatter(text):
    for loader in (yaml.SafeLoader, yaml.CSafeLoader):
        with pytest.raises(yaml.YAMLError):
            yaml.load(text, Loader=loader)  # noqa: S506


def test_falls_back_to_the_pure_python_loader(monkeypatch):
    import importlib
    monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    try:
        assert importlib.reload(yamlio).SAFE_LOADER is yaml.SafeLoader
        assert yamlio.safe_load("a: yes") == {"a": True}
    finally:
        monkeypatch.undo()
        importlib.reload(yamlio)


def _outcome(load, text):
    try:
        return "ok", load(text)
    except (yaml.YAMLError, RecursionError) as error:
        return "error", type(error).__name__


# Where libyaml and the pure-Python loader disagree (differential fuzz, Nov 2026): libyaml accepts
# a tab as separation, and nests deeper than Python's recursion limit allows.
DIVERGENT = ["k: a\tb\n", "a\tb: v\n", "a:\t1\n", "a: [1,\t2]\n", "a:\n\tb: 1\n", 'k: "a\tb"\n',
             "a: " + "[" * 500 + "]" * 500 + "\n", "a: " + "{k: " * 500 + "1" + "}" * 500 + "\n",
             "\n".join(" " * i + "k:" for i in range(500)) + " 1\n", "- " * 500 + "a\n",
             "a: &a [*a]\n", "a: " + "[" * 90 + "]" * 90 + "\n"]


@pytest.mark.parametrize("text", DIVERGENT, ids=range(len(DIVERGENT)))
def test_every_host_gets_the_pure_python_answer_where_the_loaders_disagree(text):
    pure = _outcome(lambda t: yaml.load(t, Loader=yaml.SafeLoader), text)  # noqa: S506
    if pure[0] == "ok" and "&a" in text:
        assert _outcome(yamlio.safe_load, text)[0] == "ok"   # a self-referencing list has no ==
    else:
        assert _outcome(yamlio.safe_load, text) == pure


def test_the_strict_ledger_check_rejects_what_the_pure_loader_rejects():
    import protected_ledger_io
    assert protected_ledger_io._strict_frontmatter_has_unique_keys("---\na: 1\nb: [x]\n---\n")
    assert not protected_ledger_io._strict_frontmatter_has_unique_keys("---\na: 1\na: 2\n---\n")
    assert not protected_ledger_io._strict_frontmatter_has_unique_keys("---\na:\t1\n---\n")
    assert not protected_ledger_io._strict_frontmatter_has_unique_keys(
        "---\na: " + "[" * 500 + "]" * 500 + "\n---\n")
