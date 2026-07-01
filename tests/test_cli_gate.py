"""Tests for the server-less ``dprovenancekit gate`` CLI subcommand."""

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


def _record(store, context_id, steps):
    kit = DProvenanceKit(AnyTraceableEvent)
    with kit.run(context_id=context_id, store=store) as run:
        for kind, priority, raw in steps:
            kit.record(_event(kind, priority, raw))
        return run.run_id


# A golden run: a STRUCTURAL retrieval and two CRITICAL reasoning steps.
GOLDEN_STEPS = [
    ("retrieved", TracePriority.STRUCTURAL, '{"d": "3 sources"}'),
    ("verified", TracePriority.CRITICAL, '{"d": "2 of 3 agree"}'),
    ("decided", TracePriority.CRITICAL, '{"d": "supported"}'),
]


@pytest.fixture
def trace_db(tmp_path):
    db = str(tmp_path / "trace.sqlite")
    store = SQLiteTraceStore(AnyTraceableEvent, db, start_writer=False)
    ids = {
        "golden": _record(store, "golden", GOLDEN_STEPS),
        "pass": _record(store, "candidate-pass", GOLDEN_STEPS),
        # A candidate that dropped the CRITICAL "verified" step.
        "regressed": _record(store, "candidate-regressed", [GOLDEN_STEPS[0], GOLDEN_STEPS[2]]),
    }
    store.flush()
    store._db.close()
    return db, ids


def test_gate_passes_on_identical_run(trace_db, capsys):
    db, ids = trace_db
    code = main(["gate", "--db", db, "--golden", str(ids["golden"]), "--candidate", str(ids["pass"])])
    out = capsys.readouterr().out
    assert code == 0
    assert "PASS" in out
    # The evaluator banner must NOT leak into the gate subcommand's output.
    assert "CLI Evaluator" not in out


def test_gate_fails_on_dropped_critical_step(trace_db, capsys):
    db, ids = trace_db
    code = main(
        ["gate", "--db", db, "--golden", str(ids["golden"]), "--candidate", str(ids["regressed"])]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "verified" in out


def test_gate_json_output(trace_db, capsys):
    db, ids = trace_db
    code = main(
        [
            "gate", "--db", db,
            "--golden", str(ids["golden"]),
            "--candidate", str(ids["regressed"]),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["passed"] is False
    assert payload["regression_level"] == "high"
    assert "verified" in payload["steps_by_change"].get("removed", [])


def test_gate_allow_divergent_tolerates_non_critical_change(trace_db, capsys):
    db, ids = trace_db
    # An added STRUCTURAL step is a divergence but not a severity escalation.
    store = SQLiteTraceStore(AnyTraceableEvent, db, start_writer=False)
    added = _record(
        store, "candidate-added", GOLDEN_STEPS + [("note_extra", TracePriority.STRUCTURAL, '{"d": "x"}')]
    )
    store.flush()
    store._db.close()

    strict = main(["gate", "--db", db, "--golden", str(ids["golden"]), "--candidate", str(added)])
    assert strict == 1
    capsys.readouterr()  # drain
    lenient = main(
        ["gate", "--db", db, "--golden", str(ids["golden"]), "--candidate", str(added), "--allow-divergent"]
    )
    assert lenient == 0


def test_gate_missing_run_returns_usage_error(trace_db, capsys):
    db, ids = trace_db
    code = main(
        ["gate", "--db", db, "--golden", str(ids["golden"]), "--candidate", str(uuid.uuid4())]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "not found" in err


def test_gate_unopenable_db_exits_2(tmp_path, trace_db, capsys):
    _, ids = trace_db
    # A directory is not a valid SQLite file; the gate must exit 2, not crash with a traceback.
    code = main(
        ["gate", "--db", str(tmp_path), "--golden", str(ids["golden"]), "--candidate", str(ids["pass"])]
    )
    assert code == 2
    assert "could not open database" in capsys.readouterr().err


def test_gate_across_separate_databases(tmp_path):
    # Golden in a restored baseline db, candidate in this PR's db — the baseline-selection flow.
    golden_db = str(tmp_path / "baseline.sqlite")
    candidate_db = str(tmp_path / "candidate.sqlite")

    gstore = SQLiteTraceStore(AnyTraceableEvent, golden_db, start_writer=False)
    golden = _record(gstore, "golden", GOLDEN_STEPS)
    gstore.close()

    cstore = SQLiteTraceStore(AnyTraceableEvent, candidate_db, start_writer=False)
    matching = _record(cstore, "candidate", GOLDEN_STEPS)
    regressed = _record(cstore, "candidate-regressed", [GOLDEN_STEPS[0], GOLDEN_STEPS[2]])
    cstore.close()

    ok = main(["gate", "--golden-db", golden_db, "--golden", str(golden),
               "--candidate-db", candidate_db, "--candidate", str(matching)])
    assert ok == 0

    fail = main(["gate", "--golden-db", golden_db, "--golden", str(golden),
                 "--candidate-db", candidate_db, "--candidate", str(regressed)])
    assert fail == 1


def test_gate_requires_a_db_source(trace_db, capsys):
    _, ids = trace_db
    code = main(["gate", "--golden", str(ids["golden"]), "--candidate", str(ids["pass"])])
    assert code == 2
    assert "provide --db" in capsys.readouterr().err

# git-blob-rewrite
