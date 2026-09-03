"""sessions.py — the one session-archive implementation, against the listing Hermes really prints."""
import types

import sessions

# Verbatim from the owner's container, Sep 3 2026 (`hermes sessions list | head`).
LISTING = """Title                        Workspace          Last Active   ID
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
—                            —                  just now      cron_964b334424a8_20260902_203042
Use execute_code to run th   /                  just now      20260903_033026_65967d
sotto-proactive · Sep 02 2   —                  14m ago       cron_964b334424a8_20260902_201542
Here is #56                  /                  21m ago       20260903_030638_ec5f25
sotto-proactive · Sep 02 2   —                  29m ago       cron_6bce68cd5a58_20260902_200045
H #3                         /                  1h ago        20260903_022841_413f39
Her #10                      /                  1h ago        20260903_021340_3a62bc
Run the sotto-proactive sk   /                  1h ago        20260903_013630_e248e2
"""


def test_the_id_column_is_read_and_cron_runs_are_skipped():
    """The regex both callers used (`[0-9a-f]{12,}` under word boundaries) matched none of these:
    the ids carry underscores and dates. The ID column is the last token of a data row."""
    assert sessions.session_ids(LISTING) == [
        "20260903_033026_65967d", "20260903_030638_ec5f25", "20260903_022841_413f39",
        "20260903_021340_3a62bc", "20260903_013630_e248e2"]
    assert sessions.session_ids("") == [] and sessions.session_ids("Title  ID\n────\n") == []


def test_archive_all_counts_successes_and_answers_none_without_the_cli():
    calls = []

    def fake_run(argv, **_kw):
        calls.append(argv)
        if argv[1:3] == ["sessions", "list"]:
            return types.SimpleNamespace(returncode=0, stdout=LISTING)
        return types.SimpleNamespace(returncode=1 if argv[-1].endswith("e248e2") else 0, stdout="")

    assert sessions.archive_all(run=fake_run) == 4
    assert sum(1 for a in calls if a[1:3] == ["sessions", "archive"]) == 5, "every non-cron id is tried"
    assert sessions.archive_all(run=lambda *a, **k: (_ for _ in ()).throw(OSError("no hermes"))) is None
