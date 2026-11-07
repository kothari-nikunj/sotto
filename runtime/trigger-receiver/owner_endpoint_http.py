"""Closed owner endpoint metadata and bounded private HTTP message handling."""
from dataclasses import dataclass
import json
from typing import Callable

_TOOL_RESPONSE = 512 * 1024

@dataclass(frozen=True)
class OwnerEndpoint:
    perform: Callable
    unavailable: type[Exception]
    request_limit: int = 65536
    response_limit: int = _TOOL_RESPONSE
    error_name: str = 'reminder'


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def read_body(handler, limit):
    size = int(handler.headers.get('Content-Length', '0'))
    content_type = handler.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
    if not 0 < size <= limit or content_type != 'application/json':
        raise ValueError('invalid request')
    return json.loads(handler.rfile.read(size).decode('utf-8'), object_pairs_hook=_unique)


