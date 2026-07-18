"""Tests for the ``dprovenancekit export`` CLI subcommand (JSONL for Datadog/Splunk)."""

from __future__ import annotations

import json
import uuid

import pytest

from dprovenancekit import DProvenanceKit, SQLiteTraceStore, TracePriority
from dprovenancekit.cli import main
from dprovenancekit.event import AnyTraceableEvent


def _event(kind, priority, raw="{}"):
    return AnyTraceableEvent(
        type_identifier_value=kind, priority_value=int(priority), raw_json=raw
    )


@pytest.fixture
def trace_db(tmp_path):
    db = str(tmp_path / "trace.sqlite")
    store = SQLiteTraceStore(AnyTraceableEvent, db, start_writer=False)
    kit = DProvenanceKit(AnyTraceableEvent)
    with kit.run(context_id="case", store=store) as run:
        kit.record(_event("retrieved", TracePriority.STRUCTURAL, '{"sources": 3}'))
        kit.record(_event("decided", TracePriority.CRITICAL, '{"answer": "yes"}'))
        run_id = run.run_id
    store.flush()
    store._db.close()
    return db, run_id


def test_export_emits_valid_ordered_jsonl(trace_db, capsys):
    db, run_id = trace_db
    assert main(["export", "--db", db, "--run", str(run_id)]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    rows = [json.loads(ln) for ln in lines]  # every line must be valid JSON
    assert [r["type"] for r in rows] == ["retrieved", "decided"]
    assert [r["sequence"] for r in rows] == [0, 1]
    assert all(r["run_id"] == str(run_id) for r in rows)
    assert rows[0]["data"] == {"sources": 3}  # payload decoded, not a raw string
    assert rows[1]["priority"] == int(TracePriority.CRITICAL)


def test_export_never_emits_nan_or_infinity(tmp_path, capsys):
    # A non-finite float in a payload must not produce invalid JSON (NaN/Infinity).
    db = str(tmp_path / "t.sqlite")
    store = SQLiteTraceStore(AnyTraceableEvent, db, start_writer=False)
    kit = DProvenanceKit(AnyTraceableEvent)
    with kit.run(context_id="c", store=store) as run:
        # raw_json carrying a bare NaN token, as Python's json.dumps would have written.
        kit.record(_event("scored", TracePriority.STRUCTURAL, '{"score": NaN}'))
        run_id = run.run_id
    store.flush()
    store._db.close()

    assert main(["export", "--db", db, "--run", str(run_id)]) == 0
    out = capsys.readouterr().out
    assert "NaN" not in out and "Infinity" not in out
    row = json.loads(out.strip())  # strict parse rejects NaN — must succeed
    assert row["data"]["score"] is None


def test_export_bad_uuid_exits_2(trace_db, capsys):
    db, _ = trace_db
    assert main(["export", "--db", db, "--run", "not-a-uuid"]) == 2
    assert "valid run id" in capsys.readouterr().err


def test_export_missing_db_exits_2_without_creating_file(tmp_path, capsys):
    missing = tmp_path / "nope.sqlite"
    assert main(["export", "--db", str(missing), "--run", str(uuid.uuid4())]) == 2
    assert "no such database" in capsys.readouterr().err
    assert not missing.exists()  # read-only command must not create the file


def test_export_run_not_found_exits_2(trace_db, capsys):
    db, _ = trace_db
    assert main(["export", "--db", db, "--run", str(uuid.uuid4())]) == 2
    assert "run not found" in capsys.readouterr().err


def test_gate_subcommand_routes_to_gate_not_banner(trace_db, capsys):
    # Regression guard for the dispatch bug: `gate` must reach the gate handler
    # (which errors on a missing golden source, exit 2) — never the fall-through
    # benchmark banner (which returned 0 and made the CI gate a silent no-op).
    db, _ = trace_db
    rc = main(["gate", "--db", db])
    assert rc == 2
    assert "DProvenanceKit CLI Evaluator" not in capsys.readouterr().out
