"""Local trace visualization UI server."""

import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

from .event import AnyTraceableEvent
from .sqlite_store import SQLiteTraceStore


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _json_serializable(obj):
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return str(obj)


def create_handler(db_path: str):
    class UIHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/" or path == "/index.html":
                self.serve_file("index.html", "text/html")
            elif path == "/api/runs":
                self.serve_api_runs()
            elif path.startswith("/api/diff"):
                self.serve_api_diff(parsed.query)
            elif path.startswith("/api/runs/"):
                run_id_str = path.split("/")[-1]
                self.serve_api_run(run_id_str)
            else:
                self.send_error(404, "Not Found")

        def serve_file(self, filename, content_type):
            file_path = os.path.join(os.path.dirname(__file__), filename)
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                self.wfile.write(content)
            except OSError:
                self.send_error(404, "File not found")

        def serve_api_runs(self):
            try:
                store = SQLiteTraceStore(AnyTraceableEvent, db_path, start_writer=False)
                runs_meta = store.list_run_metadata()
                store.close()

                runs_data = [
                    {
                        "run_id": str(r.run_id),
                        "context_id": r.context_id,
                        "start_time": r.start_time,
                        "event_count": r.event_count,
                        "fingerprint": r.fingerprint,
                    }
                    for r in runs_meta
                ]

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(runs_data).encode("utf-8"))
            except Exception as e:
                self.send_error(500, f"Internal Server Error: {e}")

        def serve_api_run(self, run_id_str):
            try:
                run_id = uuid.UUID(run_id_str)
                store = SQLiteTraceStore(AnyTraceableEvent, db_path, start_writer=False)
                run = store.get_run(run_id)
                store.close()

                if not run:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        json.dumps({"error": "Run not found"}).encode("utf-8")
                    )
                    return

                # Serialize events
                events_data = []
                for event in sorted(run.events, key=lambda e: e.sequence):
                    events_data.append(
                        {
                            "sequence": event.sequence,
                            "engine_name": event.engine_name,
                            "timestamp": event.timestamp,
                            "payload": {
                                "type_identifier": event.payload.type_identifier,
                                "data": _json_serializable(event.payload),
                            },
                        }
                    )

                from .testing import run_fingerprint

                fp = run_fingerprint(run)

                run_data = {
                    "run_id": str(run.run_id),
                    "context_id": run.context_id,
                    "fingerprint": fp,
                    "events": events_data,
                }

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(run_data, default=str).encode("utf-8"))
            except ValueError:
                self.send_error(400, "Invalid run ID format")
            except Exception as e:
                self.send_error(500, f"Internal Server Error: {e}")

        def serve_api_diff(self, query_string):
            try:
                from urllib.parse import parse_qs
                from .testing import exact_equality_evaluator
                from .alignment_engine import TraceAlignmentEngine
                from .alignment_models import AlignmentConfiguration, AlignmentProfile

                query_params = parse_qs(query_string)
                golden_id_str = query_params.get("golden", [""])[0]
                candidate_id_str = query_params.get("candidate", [""])[0]

                if not golden_id_str or not candidate_id_str:
                    self.send_error(400, "Missing golden or candidate run IDs")
                    return

                golden_id = uuid.UUID(golden_id_str)
                candidate_id = uuid.UUID(candidate_id_str)

                store = SQLiteTraceStore(AnyTraceableEvent, db_path, start_writer=False)
                golden_run = store.get_run(golden_id)
                candidate_run = store.get_run(candidate_id)
                store.close()

                if not golden_run or not candidate_run:
                    self.send_error(404, "Run not found")
                    return

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
                            "data": _json_serializable(
                                alignment.comparison_event.payload
                            ),
                        }

                    alignments_data.append(
                        {
                            "kind": alignment.state.kind.value,
                            "base": base_payload,
                            "candidate": candidate_payload,
                        }
                    )

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"alignments": alignments_data}, default=str).encode(
                        "utf-8"
                    )
                )
            except ValueError:
                self.send_error(400, "Invalid run ID format")
            except Exception as e:
                import traceback

                traceback.print_exc()
                self.send_error(500, f"Internal Server Error: {e}")

    return UIHandler


def run_ui_server(db_path: str, port: int = 8080):
    print(
        f"Starting DProvenanceKit UI at http://localhost:{port} (serving from {db_path})"
    )
    handler = create_handler(db_path)
    server = ThreadingHTTPServer(("", port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.server_close()
