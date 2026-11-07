"""Task presentation cannot discard the canonical summary used by exact selectors."""
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_shared/scripts'))
import loops_query


def test_trusted_task_read_keeps_summary_and_ask_while_display_stays_action_first():
    row = {'anchor_key': 'photo-selection', 'status': 'open', 'action_type': 'follow_up',
           'source': 'gmail', 'channel': 'email', 'created_at': '2026-11-01',
           'contact_name': 'Synthetic Person', 'summary': 'Choose dates for family photos',
           'ask': 'Pick a date and location'}
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    display = loops_query._entry(row, now, '2026-11-05')
    trusted = loops_query._entry(row, now, '2026-11-05', include_ask=True)
    assert display['what'] == 'Pick a date and location'
    assert trusted['what'] == 'Choose dates for family photos'
    assert trusted['ask'] == 'Pick a date and location'
    assert trusted['loop_version'] == display['loop_version']
    assert trusted['loop_identity'] == display['loop_identity']
