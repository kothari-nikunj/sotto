#!/usr/bin/env python3
"""Fresh bounded triage inputs through the original gather and queue builders."""
import json
import os
from pathlib import Path
import sys

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1] / 'lib')]
import gather_google as google  # noqa: E402
import source_context as sources  # noqa: E402
import triage_queue as queue  # noqa: E402
from textutil import unwrap_tool_result  # noqa: E402

ORDER = ('gmail', 'imessage', 'whatsapp', 'contacts')


def query(permitted):
    permitted = set(permitted)
    coverage = {source: {'source': source, 'status': 'disabled', 'complete': False} for source in ORDER}
    emails, local = [], {}
    if 'gmail' in permitted:
        coverage['gmail']['status'] = 'unavailable'
        try:
            api = google._find_google_api()
            if not api:
                raise RuntimeError('Google unavailable')
            google._refresh_google_token()
            # Queue membership uses metadata; the selected item is read in full before drafting.
            inbox = google.gather_gmail(api, 40, 0, attachments=False)
            emails = list(inbox)
            partial = bool(getattr(inbox, 'capped', len(inbox) >= 40) or getattr(inbox, 'rate_limited', False))
            if not getattr(inbox, 'rate_limited', False):
                try:
                    sent = google.gather_sent(api, bodies=0)
                    emails = google.merge_sent(emails, sent)
                    partial |= bool(getattr(sent, 'capped', False) or getattr(sent, 'rate_limited', False))
                except Exception:
                    partial = True
            coverage['gmail'].update(status='partial' if partial else 'ok', complete=not partial)
        except Exception:
            emails = []
    if permitted.intersection(ORDER[1:]):
        for source in ORDER[1:]:
            if source in permitted:
                coverage[source]['status'] = 'unavailable'
        try:
            raw = unwrap_tool_result(sources.bridge_call('read_local', {'since_hours': 72}))
            if not isinstance(raw, dict):
                raise ValueError('Local input unavailable')
            local = sources.project_local(raw)
            for source, fields in sources.SOURCE_FIELDS.items():
                if source not in permitted:
                    for field in fields:
                        local.pop(field, None)
            statuses = sources._local_statuses(raw.get('source_status'))
            for source in ORDER[1:]:
                if source not in permitted:
                    continue
                status = statuses.get(source)
                # No explicit availability receipt cannot establish an empty successful search.
                if status in ('ok', 'partial'):
                    coverage[source].update(status=status, complete=status == 'ok')
                elif status == 'disabled':
                    coverage[source].update(status='disabled', complete=False)
                else:
                    for field in sources.SOURCE_FIELDS[source]:
                        local.pop(field, None)
        except Exception:
            local = {}
    result = {'email': queue.email_queue(emails),
              'imessage': queue.local_queue(local, 'imessage'),
              'whatsapp': queue.local_queue(local, 'whatsapp')}
    result['counts'] = {source: len(rows) for source, rows in result.items()}
    result['coverage'] = {'kind': 'bounded_current_sources', 'local_since_hours': 72,
                          'gmail_query': 'newer_than:1d',
                          'sources': [coverage[source] for source in ORDER]}
    return result


if __name__ == '__main__':
    print(json.dumps(query(json.loads(os.environ['SOTTO_CAPTURED_SOURCES']))))
