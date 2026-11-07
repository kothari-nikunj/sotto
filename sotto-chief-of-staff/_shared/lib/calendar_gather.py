"""One independent Calendar worker for the ordinary Google gather."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import source_context


@contextmanager
def calendar_gather(api, back_days, read_calendar, *, enabled):
    def read():
        observation = {}
        rows = (read_calendar(api, back_days=back_days, observation=observation)
                if source_context.allowed('calendar') else [])
        return rows, observation

    with ThreadPoolExecutor(max_workers=1) as pool:
        yield pool.submit(read) if api and enabled and source_context.allowed('calendar') else None


def calendar_result(future, classify_error):
    try:
        rows, observation = future.result()
        complete = observation.get('complete') is True
        receipt = source_context.source_result('ok' if complete else 'partial', complete=complete,
                                **observation.get('coverage', {}))
        receipt['calendars'] = observation.get('calendars', [])
        return rows, receipt, None
    except Exception as error:
        return [], source_context.source_result('unavailable', error=classify_error(error)), f'calendar: {error}'
