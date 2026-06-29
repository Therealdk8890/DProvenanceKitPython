"""Socket adapter: serve the pure ``Server.handle`` over HTTP (stdlib, no dependencies)."""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .app import Server


def _make_handler(server: Server):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k: v for k, v in self.headers.items()}
            status, resp_headers, out = server.handle(method, self.path, headers, body)
            self.send_response(status)
            for k, v in resp_headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def log_message(self, fmt, *args):  # keep stdout clean
            return

    return Handler


def _maybe_seed(srv: Server) -> None:
    """First run in tenancy mode: create a demo project + key so the dashboard works."""
    if srv.tenancy is not None and srv.tenancy.is_empty():
        project_id = srv.tenancy.create_project("demo")
        key = srv.tenancy.create_api_key(project_id, "seed")
        print(f"  seeded demo project ({project_id}); API key (save it now): {key}")


def serve(host: str = "127.0.0.1", port: int = 8787, server: Optional[Server] = None) -> None:
    srv = server or Server()
    _maybe_seed(srv)
    httpd = ThreadingHTTPServer((host, port), _make_handler(srv))
    print(f"DProvenanceKit backend → http://{host}:{port}   (dashboard at /)")
    print(f"  {srv.describe()}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


def main() -> None:
    serve(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8787")))
