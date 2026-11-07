"""Bounded, lossless Google People contact arrays for attended review/readback."""

import copy
import json
import re

CONTACT_FIELDS = frozenset(
    {"names", "emailAddresses", "phoneNumbers", "organizations", "birthdays", "addresses", "biographies"}
)
CONTACT_MASK = ",".join(sorted(CONTACT_FIELDS | {"metadata"}))
RESOURCE = re.compile(r"people/c[A-Za-z0-9_-]+\Z")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _json(value, depth=0):
    if depth > 8:
        raise ValueError("Contact JSON depth invalid")
    if value is None or type(value) is bool:
        return
    if type(value) is int and abs(value) <= 9007199254740991:
        return
    if type(value) is str:
        if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError("Contact Unicode invalid")
        return
    if type(value) is list:
        for item in value:
            _json(item, depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("Contact JSON key invalid")
            _json(key, depth + 1)
            _json(item, depth + 1)
        return
    raise ValueError("Contact JSON invalid")


def validate_fields(fields, *, empty=False):
    if type(fields) is not dict or (not fields and not empty) or set(fields) - CONTACT_FIELDS:
        raise ValueError("Contact fields invalid")
    if any(type(items) is not list for items in fields.values()):
        raise ValueError("Contact fields must be arrays")
    _json(fields)
    if len(canonical(fields).encode()) > 12288:
        raise ValueError("Contact fields too large")
    return copy.deepcopy(fields)


def _token(value):
    if (
        type(value) is not str
        or not value
        or len(value) > 1024
        or any(
            ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF or c in "\u2028\u2029"
            for c in value
        )
    ):
        raise ValueError("Contact version invalid")
    return value


def snapshot(person):
    if (
        type(person) is not dict
        or type(person.get("resourceName")) is not str
        or len(person["resourceName"]) > 256
        or not RESOURCE.fullmatch(person["resourceName"])
    ):
        raise ValueError("Contact resource invalid")
    metadata = person.get("metadata", {})
    if type(metadata) is not dict:
        raise ValueError("Contact metadata invalid")
    if metadata.get("deleted") is True:
        raise ValueError("Contact deleted")
    fields = validate_fields({key: person.get(key, []) for key in sorted(CONTACT_FIELDS)})
    sources = metadata.get("sources", [])
    if type(sources) is not list or any(type(s) is not dict for s in sources):
        raise ValueError("Contact sources invalid")
    projected = []
    for source in sources:
        if source.get("type") == "CONTACT":
            item = {"type": "CONTACT", "etag": _token(source.get("etag"))}
            if "id" in source:
                item["id"] = _token(source["id"])
            projected.append(item)
    if not projected:
        raise ValueError("Contact version unavailable")
    result = {
        "resource_name": person["resourceName"],
        "etag": _token(person.get("etag")),
        "fields": fields,
        "contact_sources": projected,
    }
    if len(canonical(result).encode()) > 16384:
        raise ValueError("Contact snapshot too large")
    return result


def validate_snapshot(value):
    if type(value) is not dict or set(value) != {"resource_name", "etag", "fields", "contact_sources"}:
        raise ValueError("Contact snapshot invalid")
    projected = snapshot(
        {
            "resourceName": value["resource_name"],
            "etag": value["etag"],
            **value["fields"],
            "metadata": {"sources": value["contact_sources"]},
        }
    )
    if projected != value:
        raise ValueError("Contact snapshot not canonical")
    return projected
