"""Real receiver fixture process: synthetic isolated data, loopback only, no credentials."""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.dont_write_bytecode = True


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(2)
    source_root = Path(sys.argv[1]).resolve()
    data_root = Path(sys.argv[2]).resolve()
    if not source_root.is_dir() or not data_root.is_dir() or any(data_root.iterdir()):
        raise SystemExit(2)

    safe_env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP")
                if key in os.environ}
    safe_env["HOME"] = str(data_root / "synthetic-home")
    (data_root / "synthetic-home").mkdir(mode=0o700)
    os.environ.clear()
    os.environ.update(safe_env)
    os.environ.update({
        "SOTTO_DATA": str(data_root),
        "SOTTO_MCP_TOKEN": "fixture-root-bearer-not-a-real-secret",
        "SOTTO_DEPLOYMENT_MODE": "self_host",
        })

    chief = source_root / "sotto-chief-of-staff"
    sys.path[:0] = [str(chief / "_shared" / part) for part in ("lib", "scripts", "knowledge")]
    sys.path.insert(0, str(source_root / "runtime" / "trigger-receiver"))
    import receiver  # noqa: PLC0415
    from knowledge_edit import (  # noqa: PLC0415
        op_loop_add,
        op_loop_add_admitted_owner_text,
    )

    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    receiver._local_now = lambda: now
    older = now - timedelta(days=9)
    op_loop_add_admitted_owner_text(
        "Fixture owner-only obligation", owner_text_span="Fixture owner-only obligation",
        owner_event_id="fixture-event-1", now=older,
    )
    op_loop_add("Fixture unproved legacy obligation", now=older)

    server = ThreadingHTTPServer(("127.0.0.1", 0), receiver.Handler)
    server.daemon_threads = True
    stopped = threading.Event()

    def stop(_signum, _frame):
        if not stopped.is_set():
            stopped.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(json.dumps({
        "ready": True,
        "url": f"http://127.0.0.1:{server.server_address[1]}/mcp",
        "readBearer": receiver.MCP_TOKEN,
        "expectedVisibleSummary": "Fixture owner-only obligation",
    }), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
