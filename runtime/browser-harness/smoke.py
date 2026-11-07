"""Real driver smoke against only an ephemeral synthetic loopback fixture."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading


class Fixture(BaseHTTPRequestHandler):
    agents = []

    def do_GET(self):
        self.agents.append(self.headers.get("User-Agent"))
        body = b"""<!doctype html><title>Sotto isolated fixture</title>
        <body><h1>Browser harness smoke</h1><p id='status'>Rendered locally</p>
        <button style='position:fixed;left:10px;top:180px;width:150px;height:40px'
        onclick="document.querySelector('#status').textContent='Fixture click succeeded'">Click fixture</button>
        <script>document.querySelector('#status').textContent='JavaScript rendered successfully';</script>
        </body>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        result = subprocess.run([
            sys.executable, str(Path(__file__).with_name("adapter.py")), "--chromium", args.chromium,
            "--fixture-origin", origin, "--allow-interactions",
        ], input=json.dumps({"actions": [{"type": "navigate", "url": origin}, {"type": "snapshot"},
                                         {"type": "click", "x": 50, "y": 200}, {"type": "snapshot"}]}),
           text=True, capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError(result.stdout or result.stderr)
        output = json.loads(result.stdout)
        assert output["ok"] and output["driver"] == "browser-harness/0.1.13"
        assert output["results"][-1]["title"] == "Sotto isolated fixture"
        assert "JavaScript rendered successfully" in output["results"][1]["text"]
        assert "Fixture click succeeded" in output["results"][-1]["text"]
        assert Fixture.agents and all(x == "OpenAI File Downloader, XaiImageApiFetch/1.0"
                                      for x in Fixture.agents)
        print(json.dumps({"ok": True, "driver": output["driver"], "renderedFixture": True,
                          "observedHttpRequests": len(Fixture.agents), "networkSandbox": False}))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
