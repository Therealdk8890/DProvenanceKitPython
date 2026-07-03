"""End-to-end coverage for the local UI server's ``GET /api/diff`` endpoint.

This exercises ``create_handler`` through a real in-process ``ThreadingHTTPServer``
so the whole request path runs, including the deferred imports inside
``serve_api_diff``. It is the regression guard for the class of bug where the
handler imports ``AlignmentConfiguration`` / ``AlignmentProfile`` from the wrong
module: a bad import raises ``ImportError`` at request time and the endpoint
returns 500 instead of a diff.

Runs are stored as :class:`AnyTraceableEvent` envelopes, which is how the UI
reads any database (the server opens the store with ``AnyTraceableEvent``), so
identity and priority round-trip exactly and the alignment engine produces
real alignments.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid

import pytest

from dprovenancekit import DProvenanceKit, SQLiteTraceStore
from dprovenancekit.event import AnyTraceableEvent
from dprovenancekit.ui_server import ThreadingHTTPServer, create_handler


def _event(type_identifier: str, payload: dict) -> AnyTraceableEvent:
    """A CRITICAL-priority event stored as its own round-trippable envelope."""
    from dprovenancekit import TracePriority

    return AnyTraceableEvent(
        type_identifier_value=type_identifier,
        priority_value=TracePriority.CRITICAL.value,
        raw_json=json.dumps(payload),
    )


def _record_run(db_path: str, events: list[AnyTraceableEvent]) -> uuid.UUID:
    store = SQLiteTraceStore(AnyTraceableEvent, db_path, start_writer=False)
    kit = DProvenanceKit(AnyTraceableEvent)
    with kit.run(context_id="ctx", store=store) as run:
        for event in events:
            kit.record(event)
    run_id = run.run_id
    store.close()  # flush the run to disk before the server reads it
    return run_id


@pytest.fixture
def diff_server(temp_db_path):
    """Serve a fresh db on an ephemeral port; yields ``(base_url, db_path)``."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(temp_db_path))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", temp_db_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(base_url: str, path: str):
    try:
        with urllib.request.urlopen(base_url + path) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_api_diff_returns_alignments_for_two_runs(diff_server):
    base_url, db_path = diff_server
    golden = _record_run(
        db_path,
        [
            _event("processStarted", {"a": 1}),
            _event("stepA", {"x": 1}),
            _event("processFinished", {"z": 1}),
        ],
    )
    # Candidate drops ``stepA`` — the diff must report it as ``removed``.
    candidate = _record_run(
        db_path,
        [
            _event("processStarted", {"a": 1}),
            _event("processFinished", {"z": 1}),
        ],
    )

    status, body = _get(base_url, f"/api/diff?golden={golden}&candidate={candidate}")

    assert status == 200, body
    kinds = [a["kind"] for a in body["alignments"]]
    assert kinds, "expected a non-empty diff"
    assert "removed" in kinds  # the dropped step
    assert "exactMatch" in kinds  # the shared start/finish events


def test_api_diff_missing_params_is_400(diff_server):
    base_url, _ = diff_server
    status, _ = _get(base_url, "/api/diff?golden=" + str(uuid.uuid4()))
    assert status == 400


def test_api_diff_invalid_run_id_is_400(diff_server):
    base_url, _ = diff_server
    status, _ = _get(base_url, "/api/diff?golden=not-a-uuid&candidate=also-not")
    assert status == 400


def test_api_diff_unknown_run_is_404(diff_server):
    base_url, _ = diff_server
    status, _ = _get(
        base_url,
        f"/api/diff?golden={uuid.uuid4()}&candidate={uuid.uuid4()}",
    )
    assert status == 404
