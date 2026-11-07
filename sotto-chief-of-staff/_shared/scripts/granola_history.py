#!/usr/bin/env python3
"""Bounded Granola MCP discovery or one exact meeting; no extraction or ledger writes."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1] / 'lib')]
import gather_granola as gather  # noqa: E402
import meeting_context  # noqa: E402
from loops_query import permitted_rows  # noqa: E402

MAX_RESPONSE = 2 * 1024 * 1024
MAX_TOTAL = 8 * 1024 * 1024
MAX_MEETINGS = 50
TRANSCRIPT_CHARS = 12000


class UnavailableError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def bounded_factory(deadline, *, transport=None):
    remaining_bytes = [MAX_TOTAL]
    opener = urllib.request.build_opener(_NoRedirect())
    def bounded(url, body, headers, timeout):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
            raise UnavailableError('Granola endpoint unavailable')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UnavailableError('Granola deadline')
        if transport is None:
            request = urllib.request.Request(url, body, headers, method='POST')
            try:
                response = opener.open(request, timeout=min(timeout, remaining))
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status, reply_headers, raw = response.code, dict(response.headers), response.read(MAX_RESPONSE + 1)
        else:
            status, reply_headers, raw = transport(url, body, headers, min(timeout, remaining))
        remaining_bytes[0] -= len(raw)
        if len(raw) > MAX_RESPONSE or remaining_bytes[0] < 0 or time.monotonic() >= deadline or 300 <= status < 400:
            raise UnavailableError('Granola transport bound')
        return status, reply_headers, raw
    def factory(force_refresh=False):
        if time.monotonic() >= deadline:
            raise UnavailableError('Granola deadline')
        def refresh_http(url, body, headers, timeout=30):
            status, _headers, raw = bounded(url, body, headers, timeout)
            return status, raw
        token, url = gather.get_access_token('granola', http=refresh_http, force_refresh=force_refresh)
        url = url or gather.DEFAULT_MCP_URL
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
            raise UnavailableError('Granola endpoint unavailable')
        return gather.McpClient(url, token, transport=bounded)
    return factory


def query(data_root, permitted, *, days=14, meeting_id=None, client_factory=None, now=None):
    if 'granola' not in permitted:
        raise UnavailableError('Granola unavailable')
    factory = client_factory or bounded_factory(time.monotonic() + 45)
    meetings, warnings = gather.gather_mcp_with_retry(days, days * 24, client_factory=factory, now=now,
        metadata_only=meeting_id is None, selected_id=meeting_id, max_meetings=MAX_MEETINGS)
    if warnings:
        raise UnavailableError('Granola retrieval unavailable')
    for meeting in meetings:
        identity = meeting.get('meeting_id')
        if (type(identity) is not str or not identity or len(identity) > 512
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in identity)
                or type(meeting.get('title')) is not str
                or any(meeting.get(key) is not None and type(meeting[key]) is not str for key in ('start', 'end'))):
            raise UnavailableError('Granola metadata unavailable')
    if meeting_id is None:
        result = {'status': 'found', 'days': days, 'meetings': meetings, 'coverage': 'supplied_granola_window'}
    else:
        result = meeting_context.query(meeting_id, {'meetings': meetings},
            rows=permitted_rows(str(data_root), permitted) if len(meetings) == 1 else [], permitted=lambda source: source in permitted)
        result['days'] = days
        if result['status'] == 'found':
            transcript = meetings[0].get('transcript') or ''
            result.update(transcript=transcript[:TRANSCRIPT_CHARS], transcript_truncated=len(transcript) > TRANSCRIPT_CHARS,
                          transcript_available=bool(transcript))
    if len(json.dumps(result, allow_nan=False).encode()) > 64 * 1024:
        raise UnavailableError('Granola result bound')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--meeting-id')
    args = parser.parse_args()
    try:
        result = query(os.environ['SOTTO_DATA'], json.loads(os.environ['SOTTO_CAPTURED_SOURCES']),
                       days=args.days, meeting_id=args.meeting_id)
        print(json.dumps(result, allow_nan=False))
    except Exception:  # noqa: BLE001 - private provider errors never cross the boundary
        print('Granola history unavailable', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
