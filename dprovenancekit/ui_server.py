"""Local trace visualization UI server.

Viewer reads use ReadOnlySQLiteTraceReader, never SQLiteTraceStore: opening a
viewer connection must not migrate, reconcile, checkpoint, or write trace data.
"""

import ipaddress
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse
from typing import Optional

from .event import AnyTraceableEvent
from .ui_reader import ReadOnlySQLiteTraceReader


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


@dataclass(frozen=True)
class TraceSource:
    """An explicitly configured local trace database."""

    source_id: str
    label: str
    db_path: str


def _normalize_sources(sources):
    normalized = []
    seen = set()
    for source in sources or ():
        if isinstance(source, TraceSource):
            item = source
        elif isinstance(source, dict):
            item = TraceSource(
                source_id=str(source.get("source_id", "")),
                label=str(source.get("label", "")),
                db_path=str(source.get("db_path", "")),
            )
        else:
            raise TypeError("each source must be a TraceSource or mapping")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", item.source_id):
            raise ValueError("source_id must be 1-64 safe ASCII characters")
        if item.source_id in seen:
            raise ValueError(f"duplicate source_id: {item.source_id}")
        if not item.label.strip() or not item.db_path.strip():
            raise ValueError("each source requires a non-empty label and db_path")
        seen.add(item.source_id)
        normalized.append(item)
    if not normalized:
        raise ValueError("multi-source mode requires at least one source")
    return tuple(normalized)


def _json_serializable(obj):
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def _host_only(host_header: str) -> str:
    """The hostname from a Host header, without the optional :port (IPv6-aware)."""
    host_header = host_header.strip()
    if host_header.startswith("["):
        host = host_header[1 : host_header.find("]")] if "]" in host_header else host_header
    else:
        host = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
    return host.lower().rstrip(".")


def create_handler(db_path: Optional[str] = None, allowed_hosts=None, sources=None):
    """Create a handler for legacy single-db mode or explicit multi-source mode.

    Existing positional/keyword db_path calls remain supported. In multi-source
    mode, every detail and diff request must identify its source explicitly.
    """
    if sources is not None:
        source_configs = _normalize_sources(sources)
        multi_source = True
        if db_path is not None:
            raise ValueError("use db_path or sources, not both")
    else:
        if not db_path:
            raise ValueError("db_path is required when sources are not supplied")
        source_configs = (TraceSource("default", os.path.basename(db_path) or "Trace database", str(db_path)),)
        multi_source = False
    source_map = {source.source_id: source for source in source_configs}

    class UIHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if allowed_hosts is not None:
                host = _host_only(self.headers.get("Host", ""))
                if host not in allowed_hosts:
                    self.send_error(403, "Forbidden")
                    return

            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                self.serve_file("index.html", "text/html")
            elif path == "/api/sources":
                self.serve_api_sources()
            elif path == "/api/runs":
                self.serve_api_runs()
            elif path == "/api/diff":
                self.serve_api_diff(parsed.query)
            elif path.startswith("/api/runs/"):
                self.serve_api_run(path.split("/")[-1], parsed.query)
            else:
                self.send_error(404, "Not Found")

        def _reader(self, source):
            return ReadOnlySQLiteTraceReader(AnyTraceableEvent, source.db_path)

        def _send_json(self, status, payload):
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _source_from_query(self, query_string, key="source"):
            source_id = parse_qs(query_string).get(key, [""])[0]
            if not source_id:
                if multi_source:
                    self.send_error(400, f"Missing {key} source identifier")
                    return None
                return source_configs[0]
            source = source_map.get(source_id)
            if source is None:
                self.send_error(404, "Source not found")
                return None
            return source

        def serve_file(self, filename, content_type):
            file_path = os.path.join(os.path.dirname(__file__), filename)
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except OSError:
                self.send_error(404, "File not found")

        def serve_api_sources(self):
            results = []
            for source in source_configs:
                try:
                    with self._reader(source) as reader:
                        count = len(reader.list_run_metadata())
                    results.append({
                        "source_id": source.source_id,
                        "label": source.label,
                        "multi_source": multi_source,
                        "status": "healthy",
                        "run_count": count,
                    })
                except Exception:
                    results.append({
                        "source_id": source.source_id,
                        "label": source.label,
                        "multi_source": multi_source,
                        "status": "error",
                        "error": "Source unavailable or schema incompatible",
                    })
            self._send_json(200, results)

        def serve_api_runs(self):
            runs_data = []
            for source in source_configs:
                try:
                    with self._reader(source) as reader:
                        rows = reader.list_run_metadata()
                    for row in rows:
                        item = dict(row)
                        if multi_source:
                            item["source_id"] = source.source_id
                            item["source_label"] = source.label
                        runs_data.append(item)
                except Exception:
                    # Health is separately visible at /api/sources; one bad source
                    # must not take healthy sources out of the unified index.
                    continue
            runs_data.sort(key=lambda row: (row.get("start_time", 0), row.get("run_id", "")), reverse=True)
            self._send_json(200, runs_data)

        def serve_api_run(self, run_id_str, query_string=""):
            source = self._source_from_query(query_string)
            if source is None:
                return
            try:
                run_id = uuid.UUID(run_id_str)
            except ValueError:
                self.send_error(400, "Invalid run ID format")
                return
            try:
                with self._reader(source) as reader:
                    run = reader.get_run(run_id)
                if not run:
                    self._send_json(404, {"error": "Run not found"})
                    return
                events_data = []
                for event in sorted(run.events, key=lambda e: e.sequence):
                    events_data.append({
                        "sequence": event.sequence,
                        "engine_name": event.engine_name,
                        "timestamp": event.timestamp,
                        "payload": {
                            "type_identifier": event.payload.type_identifier,
                            "data": _json_serializable(event.payload),
                        },
                    })
                from .testing import run_fingerprint
                run_data = {
                    "run_id": str(run.run_id),
                    "context_id": run.context_id,
                    "fingerprint": run_fingerprint(run),
                    "events": events_data,
                }
                if multi_source:
                    run_data["source_id"] = source.source_id
                    run_data["source_label"] = source.label
                self._send_json(200, run_data)
            except sqlite3.OperationalError as exc:
                if "unable to open database file" in str(exc).lower():
                    self._send_json(404, {"error": "Run not found"})
                else:
                    self._server_error()
            except Exception:
                self._server_error()

        def serve_api_diff(self, query_string):
            params = parse_qs(query_string)
            golden_source = self._source_from_query(query_string, "golden_source")
            if golden_source is None:
                return
            candidate_source = self._source_from_query(query_string, "candidate_source")
            if candidate_source is None:
                return
            golden_id_str = params.get("golden", [""])[0]
            candidate_id_str = params.get("candidate", [""])[0]
            if not golden_id_str or not candidate_id_str:
                self.send_error(400, "Missing golden or candidate run IDs")
                return
            try:
                golden_id = uuid.UUID(golden_id_str)
                candidate_id = uuid.UUID(candidate_id_str)
            except ValueError:
                self.send_error(400, "Invalid run ID format")
                return
            try:
                with self._reader(golden_source) as reader:
                    golden_run = reader.get_run(golden_id)
                with self._reader(candidate_source) as reader:
                    candidate_run = reader.get_run(candidate_id)
                if not golden_run or not candidate_run:
                    self._send_json(404, {"error": "Run not found"})
                    return

                from .testing import exact_equality_evaluator
                from .alignment_engine import TraceAlignmentEngine
                from .alignment_config import AlignmentConfiguration, AlignmentProfile
                engine = TraceAlignmentEngine(
                    AlignmentConfiguration(
                        profile=AlignmentProfile.strict_audit_v1,
                        equivalence_evaluator=exact_equality_evaluator(),
                    )
                )
                result = engine.align(base=golden_run, comparison=candidate_run)
                alignments_data = []
                for alignment in result.alignments:
                    base_payload = None
                    candidate_payload = None
                    if alignment.base_event:
                        base_payload = {
                            "sequence": alignment.base_event.sequence,
                            "type_identifier": alignment.base_event.payload.type_identifier,
                            "data": _json_serializable(alignment.base_event.payload),
                        }
                    if alignment.comparison_event:
                        candidate_payload = {
                            "sequence": alignment.comparison_event.sequence,
                            "type_identifier": alignment.comparison_event.payload.type_identifier,
                            "data": _json_serializable(alignment.comparison_event.payload),
                        }
                    alignments_data.append({
                        "kind": alignment.state.kind.value,
                        "base": base_payload,
                        "candidate": candidate_payload,
                    })
                response = {"alignments": alignments_data}
                if multi_source:
                    response["golden_source"] = golden_source.source_id
                    response["candidate_source"] = candidate_source.source_id
                self._send_json(200, response)
            except sqlite3.OperationalError as exc:
                if "unable to open database file" in str(exc).lower():
                    self._send_json(404, {"error": "Run not found"})
                else:
                    self._server_error()
            except Exception:
                self._server_error()

        def _server_error(self):
            import traceback
            traceback.print_exc()
            self.send_error(500, "Internal Server Error")

    return UIHandler


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def create_server(
    db_path: Optional[str] = None,
    port: int = 8080,
    host: str = "127.0.0.1",
    sources=None,
    allowed_hosts=None,
):
    """Build the viewer with loopback protections or an explicit Host allow-list.

    A non-loopback bind remains an explicit exposure choice. Container deployments
    can supply allowed_hosts to retain DNS-rebinding protection while binding inside
    the container to 0.0.0.0; publish the container port only on host loopback.
    """
    if allowed_hosts is None:
        try:
            is_loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host.lower() == "localhost"
        effective_allowed_hosts = _LOOPBACK_HOSTS if is_loopback else None
    else:
        effective_allowed_hosts = frozenset(str(value).lower().rstrip(".") for value in allowed_hosts)
    handler = create_handler(db_path, effective_allowed_hosts, sources=sources)
    return ThreadingHTTPServer((host, port), handler)


def run_ui_server(
    db_path: Optional[str] = None,
    port: int = 8080,
    host: str = "127.0.0.1",
    sources=None,
    allowed_hosts=None,
):
    server = create_server(db_path, port, host, sources=sources, allowed_hosts=allowed_hosts)
    display_host = "localhost" if host == "127.0.0.1" else host
    serving = f"{len(sources)} configured sources" if sources is not None else db_path
    print(f"Starting DProvenanceKit UI at http://{display_host}:{port} (serving {serving})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\\nShutting down server.")
        server.server_close()
