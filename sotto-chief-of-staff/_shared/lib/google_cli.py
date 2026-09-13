"""Decode the Google skill CLI without masking unexpected provider failures."""
import json


def decode_output(stdout, args):
    if args[:2] == ["gmail", "search"] and stdout.strip() == "No messages found.":
        return []
    return json.loads(stdout or "null")
