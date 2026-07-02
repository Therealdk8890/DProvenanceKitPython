"""Tests for the turnkey golden-baseline workflow.

Covers the two surfaces that remove run-id plumbing:

- ``dprovenancekit gate --golden-context/--candidate-context`` — newest-run-by-context
  selection in the CLI.
- the ``golden_trace`` pytest fixture — record a block, pin it with
  ``--dprov-update-golden``, gate every run after.
"""

from __future__ import annotations

import time

import pytest

from dprovenancekit import DProvenanceKit, SQLiteTraceStore, TracePriority
from dprovenancekit.cli import main
from dprovenancekit.event import AnyTraceableEvent

pytest_plugins = ["pytester"]


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


GOLDEN_STEPS = [
    ("retrieved", TracePriority.STRUCTURAL, '{"d": "3 sources"}'),
    ("verified", TracePriority.CRITICAL, '{"d": "2 of 3 agree"}'),
    ("decided", TracePriority.CRITICAL, '{"d": "supported"}'),
]
REGRESSED_STEPS = [GOLDEN_STEPS[0], GOLDEN_STEPS[2]]  # dropped the CRITICAL "verified"


@pytest.fixture
def trace_db(tmp_path):
    db = str(tmp_path / "trace.sqlite")
    store = SQLiteTraceStore(AnyTraceableEvent, db, start_writer=False)
    ids = {
        "golden": _record(store, "golden", GOLDEN_STEPS),
        "pass": _record(store, "candidate", GOLDEN_STEPS),
    }
    store.flush()
    store._db.close()
    return db, ids


# --- CLI: context-based run selection ---------------------------------------


def test_gate_selects_runs_by_context(trace_db, capsys):
    db, _ = trace_db
    code = main(
        [
            "gate",
            "--db",
            db,
            "--golden-context",
            "golden",
            "--candidate-context",
            "candidate",
        ]
    )
    assert code == 0
    assert "PASS" in capsys.readouterr().out


def test_gate_context_picks_the_newest_run(trace_db, capsys):
    db, _ = trace_db
    # Re-record the same candidate context: first a regressed run, then (newer) a good
    # one. Context selection must pick the newest, so the gate passes.
    store = SQLiteTraceStore(AnyTraceableEvent, db, start_writer=False)
    _record(store, "repeated", REGRESSED_STEPS)
    time.sleep(0.01)  # ensure a strictly later start_time for the newer run
    _record(store, "repeated", GOLDEN_STEPS)
    store.flush()
    store._db.close()

    code = main(
        [
            "gate",
            "--db",
            db,
            "--golden-context",
            "golden",
            "--candidate-context",
            "repeated",
        ]
    )
    assert code == 0


def test_gate_mixes_run_id_and_context(trace_db, capsys):
    db, ids = trace_db
    code = main(
        [
            "gate",
            "--db",
            db,
            "--golden",
            str(ids["golden"]),
            "--candidate-context",
            "candidate",
        ]
    )
    assert code == 0


def test_gate_unknown_context_exits_2(trace_db, capsys):
    db, _ = trace_db
    code = main(
        [
            "gate",
            "--db",
            db,
            "--golden-context",
            "nope",
            "--candidate-context",
            "candidate",
        ]
    )
    assert code == 2
    assert "no run with context id 'nope'" in capsys.readouterr().err


def test_gate_rejects_both_id_and_context(trace_db, capsys):
    db, ids = trace_db
    code = main(
        [
            "gate",
            "--db",
            db,
            "--golden",
            str(ids["golden"]),
            "--golden-context",
            "golden",
            "--candidate-context",
            "candidate",
        ]
    )
    assert code == 2
    assert "exactly one of --golden or --golden-context" in capsys.readouterr().err


def test_gate_rejects_neither_id_nor_context(trace_db, capsys):
    db, _ = trace_db
    code = main(["gate", "--db", db, "--candidate-context", "candidate"])
    assert code == 2
    assert "exactly one of --golden or --golden-context" in capsys.readouterr().err


# --- pytest fixture: golden_trace -------------------------------------------


AGENT_OK = """
    from dprovenancekit import traced, record_event

    @traced
    def retrieve(q):
        return ["doc"]

    @traced
    def verify(c):
        return True

    def test_agent(golden_trace):
        with golden_trace("demo-agent"):
            retrieve("q")
            record_event("plan.chosen", {"strategy": "rag"})
            verify("claim")
"""

AGENT_DROPPED_STEP = """
    from dprovenancekit import traced, record_event

    @traced
    def retrieve(q):
        return ["doc"]

    def test_agent(golden_trace):
        with golden_trace("demo-agent"):
            retrieve("q")
            record_event("plan.chosen", {"strategy": "rag"})
"""


def test_golden_trace_record_then_gate(pytester):
    pytester.makepyfile(AGENT_OK)

    # 1. Record the baseline.
    result = pytester.runpytest("--dprov-update-golden")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*golden baseline*commit this file*"])
    golden = pytester.path / "tests" / "goldens" / "demo-agent.sqlite"
    assert golden.exists()

    # 2. An identical run gates clean.
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)

    # 3. The agent silently drops its verify step -> the gate fails the test.
    pytester.makepyfile(AGENT_DROPPED_STEP)
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*RegressionError*"])


def test_golden_trace_missing_baseline_fails_with_instructions(pytester):
    pytester.makepyfile(AGENT_OK)
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*no golden baseline*", "*--dprov-update-golden*"])


def test_golden_trace_update_rerecords(pytester):
    # Pin the dropped-step variant first, then update to the full agent: the
    # re-recorded baseline must gate the full agent clean.
    pytester.makepyfile(AGENT_DROPPED_STEP)
    result = pytester.runpytest("--dprov-update-golden")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*golden baseline*commit this file*"])

    pytester.makepyfile(AGENT_OK)
    result = pytester.runpytest("--dprov-update-golden")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*golden baseline*commit this file*"])
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_golden_trace_custom_dir_via_ini(pytester):
    pytester.makeini(
        """
        [pytest]
        dprov_golden_dir = snapshots
        """
    )
    pytester.makepyfile(AGENT_OK)
    result = pytester.runpytest("--dprov-update-golden")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*golden baseline*commit this file*"])
    assert (pytester.path / "snapshots" / "demo-agent.sqlite").exists()


def test_golden_trace_block_exception_propagates(pytester):
    pytester.makepyfile(
        """
        def test_agent(golden_trace):
            with golden_trace("boom-agent"):
                raise RuntimeError("agent crashed")
        """
    )
    result = pytester.runpytest("--dprov-update-golden")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*agent crashed*"])
