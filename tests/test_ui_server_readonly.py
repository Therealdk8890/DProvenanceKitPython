"""Regression tests for read-only and multi-source local trace viewing."""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
import uuid

import pytest

from dprovenancekit import DProvenanceKit, SQLiteTraceStore, TracePriority
from dprovenancekit.event import AnyTraceableEvent
from dprovenancekit.ui_server import ThreadingHTTPServer, create_handler


def _event(name: str, payload: dict) -> AnyTraceableEvent:
    return AnyTraceableEvent(
        type_identifier_value=name,
        priority_value=TracePriority.CRITICAL.value,
        raw_json=json.dumps(payload),
    )


def _record(db_path: str, context: str = "ctx") -> uuid.UUID:
    store = SQLiteTraceStore(AnyTraceableEvent, db_path, start_writer=False)
    kit = DProvenanceKit(AnyTraceableEvent)
    with kit.run(context_id=context, store=store) as run:
        kit.record(_event("step", {"value": context}))
    run_id = run.run_id
    store.close()
    return run_id


@pytest.fixture
def serve():
    servers = []

    def start(handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(base_url: str, path: str):
    try:
        with urllib.request.urlopen(base_url + path) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def test_read_only_reader_does_not_change_database_bytes(temp_db_path, serve):
    _record(temp_db_path)
    before = _sha256(temp_db_path)
    base_url = serve(create_handler(temp_db_path))

    status, runs = _get(base_url, "/api/runs")
    assert status == 200
    assert len(runs) == 1

    status, run = _get(base_url, f"/api/runs/{runs[0]['run_id']}")
    assert status == 200
    assert run["events"]

    assert _sha256(temp_db_path) == before


def test_multi_source_index_and_source_aware_details(tmp_path, serve):
    first = str(tmp_path / "first.sqlite")
    second = str(tmp_path / "second.sqlite")
    first_id = _record(first, "first-context")
    second_id = _record(second, "second-context")
    base_url = serve(create_handler(
        sources=[
            {"source_id": "first", "label": "First trace set", "db_path": first},
            {"source_id": "second", "label": "Second trace set", "db_path": second},
        ]
    ))

    status, sources = _get(base_url, "/api/sources")
    assert status == 200
    assert [(s["source_id"], s["status"]) for s in sources] == [
        ("first", "healthy"), ("second", "healthy")
    ]
    assert all("db_path" not in source for source in sources)

    status, runs = _get(base_url, "/api/runs")
    assert status == 200
    assert {run["source_id"] for run in runs} == {"first", "second"}

    status, missing_source = _get(base_url, f"/api/runs/{first_id}")
    assert status == 400

    status, first_run = _get(base_url, f"/api/runs/{first_id}?source=first")
    assert status == 200
    assert first_run["source_id"] == "first"
    assert first_run["context_id"] == "first-context"

    status, second_run = _get(base_url, f"/api/runs/{second_id}?source=second")
    assert status == 200
    assert second_run["source_label"] == "Second trace set"


def test_multi_source_diff_can_compare_across_databases(tmp_path, serve):
    first = str(tmp_path / "golden.sqlite")
    second = str(tmp_path / "candidate.sqlite")
    golden = _record(first, "golden")
    candidate = _record(second, "candidate")
    base_url = serve(create_handler(sources=[
        {"source_id": "golden-db", "label": "Golden", "db_path": first},
        {"source_id": "candidate-db", "label": "Candidate", "db_path": second},
    ]))

    status, result = _get(
        base_url,
        f"/api/diff?golden={golden}&golden_source=golden-db"
        f"&candidate={candidate}&candidate_source=candidate-db",
    )
    assert status == 200
    assert result["golden_source"] == "golden-db"
    assert result["candidate_source"] == "candidate-db"
    assert result["alignments"]


def test_unhealthy_source_is_reported_without_hiding_healthy_sources(tmp_path, serve):
    healthy = str(tmp_path / "healthy.sqlite")
    broken = str(tmp_path / "not-a-database.sqlite")
    _record(healthy, "available")
    with open(broken, "w", encoding="utf-8") as handle:
        handle.write("not sqlite")

    base_url = serve(create_handler(sources=[
        {"source_id": "healthy", "label": "Healthy", "db_path": healthy},
        {"source_id": "broken", "label": "Broken", "db_path": broken},
    ]))
    status, sources = _get(base_url, "/api/sources")
    assert status == 200
    assert sources[0]["status"] == "healthy"
    assert sources[1]["status"] == "error"

    status, runs = _get(base_url, "/api/runs")
    assert status == 200
    assert len(runs) == 1
    assert runs[0]["source_id"] == "healthy"


def test_source_registry_rejects_duplicate_or_unsafe_ids(tmp_path):
    db = str(tmp_path / "trace.sqlite")
    source = {"source_id": "same", "label": "Trace", "db_path": db}
    with pytest.raises(ValueError, match="duplicate source_id"):
        create_handler(sources=[source, source])
    with pytest.raises(ValueError, match="source_id"):
        create_handler(sources=[{"source_id": "../bad", "label": "Bad", "db_path": db}])


def test_duplicate_run_ids_are_disambiguated_by_source(tmp_path, serve):
    first = str(tmp_path / "one.sqlite")
    second = str(tmp_path / "two.sqlite")
    shared_id = _record(first, "one")
    # Copy the database so the exact same run UUID exists in both sources.
    import shutil
    shutil.copyfile(first, second)

    base_url = serve(create_handler(sources=[
        {"source_id": "one", "label": "One", "db_path": first},
        {"source_id": "two", "label": "Two", "db_path": second},
    ]))
    status, one = _get(base_url, f"/api/runs/{shared_id}?source=one")
    assert status == 200
    status, two = _get(base_url, f"/api/runs/{shared_id}?source=two")
    assert status == 200
    assert one["source_id"] != two["source_id"]
