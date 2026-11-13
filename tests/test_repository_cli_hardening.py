"""Adversarial coverage for read-only repository sealing and CLI trust gates."""

import json
import sqlite3
import uuid

import pytest

from dprovenancekit import ReasoningRepository, SQLiteTraceStore
from dprovenancekit.event import AnyTraceableEvent, TraceEvent
from dprovenancekit.repository_cli import _explicit_success, main


@pytest.fixture
def finished_capture(tmp_path):
    history = tmp_path / "repository.sqlite"
    capture = tmp_path / "capture.sqlite"
    run_id = uuid.uuid4()
    with ReasoningRepository(history) as repo:
        workflow = repo.snapshot_workflow({"prompt.txt": b"check before answering"})
        repo.bind_run(run_id, workflow)
    with SQLiteTraceStore(AnyTraceableEvent, str(capture)) as store:
        store.begin_run(run_id, "case")
        store.bind_workflow(run_id, workflow)
        store.record(TraceEvent(
            run_id=run_id, context_id="case", engine_name="guard",
            schema_version=1, sequence=0, span_id=None, parent_span_id=None,
            payload=AnyTraceableEvent("verify", 2, '{"passed":true}'),
        ))
        store.finish_run(run_id)
    return history, capture, run_id


def _commit(history, capture, run_id, *options):
    return main([
        "--repo", str(history), "commit", "--run", str(run_id),
        "--db", str(capture), "-m", "verified", *options,
    ])


def test_sealing_uses_read_only_capture_and_preserves_database(finished_capture, capsys):
    history, capture, run_id = finished_capture
    original_bytes = capture.read_bytes()
    assert _commit(history, capture, run_id) == 0
    assert capture.read_bytes() == original_bytes
    assert len(capsys.readouterr().out.strip()) == 64
    with ReasoningRepository(history, read_only=True) as repo:
        assert repo.lookup_run(run_id) is not None


def test_corrupt_payload_never_seals_as_complete(finished_capture, capsys):
    history, capture, run_id = finished_capture
    with sqlite3.connect(capture) as db:
        db.execute("UPDATE trace_events SET payload=? WHERE run_id=?",
                   (b"\xff\x00", str(run_id)))
    assert _commit(history, capture, run_id) == 2
    with ReasoningRepository(history, read_only=True) as repo:
        assert repo.lookup_run(run_id) is None
    assert "dpk repo:" in capsys.readouterr().err


@pytest.mark.parametrize("change", [
    {"lifecycle": "recording"},
    {"sealed": False},
    {"dropped_events": 1},
    {"attempted_events": 3},
    {"schema_provenance": "legacy_assumed_v1"},
])
def test_incomplete_or_lossy_capture_fails_closed(finished_capture, change):
    history, capture, run_id = finished_capture
    with sqlite3.connect(capture) as db:
        row = db.execute("SELECT quality_json FROM run_capture WHERE run_id=?",
                         (str(run_id),)).fetchone()
        quality = json.loads(row[0])
        quality.update(change)
        db.execute("UPDATE run_capture SET quality_json=? WHERE run_id=?",
                   (json.dumps(quality), str(run_id)))
    assert _commit(history, capture, run_id) == 2


def test_existing_branch_requires_expected_parent(finished_capture, capsys):
    history, capture, run_id = finished_capture
    assert _commit(history, capture, run_id) == 0
    first = capsys.readouterr().out.strip()
    assert _commit(history, capture, run_id) == 2
    assert _commit(history, capture, run_id, "--expected-parent", "0" * 64) == 2
    assert _commit(history, capture, run_id, "--expected-parent", first) == 0


@pytest.mark.parametrize("value", [
    None, [], True, "true", {}, {"valid": "true"}, {"valid": 1},
    {"valid": True, "ok": False}, {"valid": False}, {"ok": 1},
])
def test_cli_rejects_non_boolean_or_conflicting_verdicts(value):
    assert not _explicit_success(value, "valid", "ok")


def test_cli_accepts_only_explicit_boolean_verdicts():
    assert _explicit_success({"valid": True}, "valid", "ok")
    assert _explicit_success({"valid": True, "ok": True}, "valid", "ok")
