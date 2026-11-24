"""Reminders read the owner's local day, the same day birthdays use, not the UTC day."""
import compose_brief as cb


def test_evening_brief_in_los_angeles_dates_reminders_by_the_local_day(tmp_path, monkeypatch):
    # 17:30 PDT on Aug 7 is 00:30Z on Aug 8: the UTC day would call today's reminder a day overdue.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    local = {"reminders": [{"title": "Send the deck", "due_date": "2026-08-07"},
                           {"title": "Book the venue", "due_date": "2026-08-08"}],
             "contacts": [{"name": "Robin Green", "birthday": "08-07"}],
             "source_status": {"reminders": "ok"}}
    prompt = cb.build_prompt(cb._load_prompt(), {
        "type": "evening", "now": "2026-08-08T00:30:00Z", "local": local,
        "google": {"events": [], "userTimezone": "America/Los_Angeles"}})
    assert "- Send the deck (due today)" in prompt
    assert "- Book the venue (due 2026-08-08)" in prompt
    assert "day ago" not in prompt
    assert "Robin Green** — birthday TODAY" in prompt  # the same local day as birthdays
