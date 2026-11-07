"""Runtime mechanics shared by the independent managed and self-host bootstraps."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading


def clean_environment():
    os.environ.clear()
    os.environ.update({'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
                       'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'})


def health_only():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'OK' if self.path == '/health' else b''
            self.send_response(200 if body else 404)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('0.0.0.0', 8080), Handler)
    server.daemon_threads = True
    timer = threading.Timer(900, server.shutdown)
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever()
    finally:
        timer.cancel()
        server.server_close()
