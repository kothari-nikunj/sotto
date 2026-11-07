"""Only the Mac transport is synthetic; the runtime/receiver approval path is real."""

import hashlib
import json
from pathlib import Path


def configure(receiver):
    root = Path(receiver.DATA)
    receiver._runtime_source_authority = lambda: {
        "version": 2,
        "complete": True,
        "revision": "a" * 64,
        "bridge_sources": [],
        "google_sources": [],
        "connector_sources": [],
    }
    import managed  # noqa: PLC0415 - receiver fixture sets its import path first

    managed.record_bridge_consent(root, [], [], allow_send=True)

    def rpc(body, timeout=90.0):
        if body["method"] == "initialize":
            return {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}}
        if body["method"] == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "tools": []
                    if (root / "send-off").exists()
                    else [{"name": "send_message", "inputSchema": {"type": "object", "properties": {}}}]
                },
            }
        assert body["params"]["name"] == "send_message"
        args = body["params"]["arguments"]
        with (root / "bridge-sends.jsonl").open("a") as stream:
            stream.write(json.dumps(args) + "\n")
        if (root / "send-lost").exists():
            raise TimeoutError("synthetic Mac response loss")
        digest = hashlib.sha256()
        for part in (args["channel"], args["to"], args["body"]):
            raw = part.encode()
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
        return {
            "result": {
                "structuredContent": {
                    "status": "sent",
                    "channel": args["channel"],
                    "to": args["to"],
                    "request_id": args["request_id"],
                    "payload_hash": digest.hexdigest(),
                }
            }
        }

    receiver.RELAY.mcp_call = rpc
