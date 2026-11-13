"""Read-only local history HTTP surface; never migrates or mutates repositories."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .repository import ReasoningRepository
from .repository_cli import _json_value


def create_repository_handler(repository_path, allowed_hosts=None):
    class RepositoryHandler(BaseHTTPRequestHandler):
        def _json(self, status, value):
            content = json.dumps(_json_value(value), ensure_ascii=False).encode("utf-8")
            self._send(status, content, "application/json")

        def _send(self, status, content, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            from .ui_server import _host_only

            if allowed_hosts is not None and _host_only(self.headers.get("Host", "")) not in allowed_hosts:
                self._json(403, {"error": "Forbidden"})
                return
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if parsed.path in ("/", "/index.html"):
                content = Path(__file__).with_name("repository.html").read_bytes()
                self._send(200, content, "text/html; charset=utf-8")
                return
            try:
                with ReasoningRepository(repository_path, read_only=True) as repo:
                    if parsed.path == "/api/repository":
                        status = repo.status()
                        status.pop("path", None)
                        self._json(200, status)
                    elif parsed.path == "/api/refs":
                        self._json(200, repo.refs())
                    elif parsed.path == "/api/history":
                        limit = int(params.get("limit", ["50"])[0])
                        offset = int(params.get("offset", ["0"])[0])
                        if not 1 <= limit <= 100 or offset < 0:
                            raise ValueError("page limit must be 1-100; offset must be nonnegative")
                        self._json(200, repo.log(params.get("ref", ["HEAD"])[0], limit=limit, offset=offset))
                    elif parsed.path.startswith("/api/objects/"):
                        self._json(200, repo.get_object(repo.resolve(parsed.path.rsplit("/", 1)[1])))
                    elif parsed.path == "/api/repository-diff":
                        baseline = params.get("baseline", [""])[0]
                        candidate = params.get("candidate", [""])[0]
                        if not baseline or not candidate:
                            raise ValueError("baseline and candidate are required")
                        self._json(200, repo.compare(baseline, candidate, persist=False))
                    else:
                        self._json(404, {"error": "Not found"})
            except (ValueError, KeyError, TypeError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception:
                self._json(503, {"error": "Repository unavailable or incompatible"})

    return RepositoryHandler
