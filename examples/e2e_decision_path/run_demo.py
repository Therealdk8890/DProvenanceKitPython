"""One-shot decision-path demo: Instrument → Record → Baseline → Gate → Attest → Verify.

Shows the product workflow with real CLI output:

1. Instrument + record a clean baseline path into SQLite
2. Pin it with ``dpk record``
3. Record a candidate that drops a CRITICAL step
4. ``dpk gate`` fails (HIGH) on the regression
5. Export the baseline as an attestable-trace JSON
6. ``dpk attest sign`` then ``dpk attest verify`` succeed on the baseline

Honest scope: this is a tamper-evident record + CI gate + signed attestation of a
recorded path — not a claim that the decision was "sound".

Requires::

    pip install -e ".[crypto]"

Run from the repository root::

    python -m examples.e2e_decision_path
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, TextIO
from uuid import UUID

from dprovenancekit.attestation import AttestableTrace, AttestableTraceEvent
from dprovenancekit.cli import main as dpk_main
from dprovenancekit.edge import TraceEdge
from dprovenancekit.event import AnyTraceableEvent
from dprovenancekit.sqlite_store import SQLiteTraceStore

from .agent import record_path, step_types

# Markers the CI test asserts on — keep stable.
MARKER_GATE_FAIL = "GATE_RESULT=FAIL"
MARKER_GATE_LEVEL = "GATE_LEVEL=high"
MARKER_ATTEST_OK = "ATTEST_VERIFY=valid"
MARKER_DEMO_OK = "DEMO_OK"


def _banner(log, step: int, title: str) -> None:
    log("")
    log("=" * 72)
    log(f"  [{step}] {title}")
    log("=" * 72)


def _log_cmd(log, argv: Sequence[str]) -> None:
    log("  $ dpk " + " ".join(argv))


def _run_to_attestable(store: SQLiteTraceStore, run_id: UUID) -> AttestableTrace:
    """Project a recorded TraceRun into the public AttestableTrace wire model."""
    run = store.get_run(run_id)
    if run is None:
        raise RuntimeError(f"run not found: {run_id}")

    events = []
    for event in sorted(run.events, key=lambda e: e.sequence):
        payload = event.payload
        if isinstance(payload, AnyTraceableEvent):
            payload_json = payload.raw_json
            priority = int(payload.priority_value)
            type_id = payload.type_identifier_value
        else:
            payload_json = payload.encode().decode("utf-8")
            priority = int(payload.priority)
            type_id = payload.type_identifier
        # Attestation requires parseable JSON; fall back to empty object.
        try:
            json.loads(payload_json)
        except Exception:
            payload_json = "{}"
        events.append(
            AttestableTraceEvent(
                id=event.id,
                run_id=event.run_id,
                context_id=event.context_id,
                engine_name=event.engine_name,
                schema_version=event.schema_version,
                sequence=event.sequence,
                span_id=event.span_id,
                parent_span_id=event.parent_span_id,
                type_identifier=type_id,
                priority=priority,
                payload_json=payload_json,
                timestamp_unix_microseconds=int(event.timestamp * 1_000_000),
            )
        )

    # Collect provenance edges that reference events in this run (may be empty).
    event_ids = {e.id for e in events}
    edges: List[TraceEdge] = []
    if event_ids:
        # Prefer store lineage when available; empty is valid for attestation.
        try:
            for eid in event_ids:
                for edge in store.lineage_edges(eid, max_depth=1):
                    if edge.source_id in event_ids and edge.target_id in event_ids:
                        edges.append(edge)
        except Exception:
            edges = []
    # Deduplicate
    seen = set()
    unique_edges: List[TraceEdge] = []
    for edge in edges:
        key = (edge.source_id, edge.target_id, edge.type)
        if key not in seen:
            seen.add(key)
            unique_edges.append(edge)

    return AttestableTrace(
        run_id=run.run_id,
        context_id=run.context_id,
        events=tuple(events),
        edges=tuple(unique_edges),
    )


def run_demo(
    output_dir: Optional[str] = None,
    *,
    log: Optional[TextIO] = None,
    keep: bool = False,
) -> int:
    """Execute the full decision-path story. Returns 0 on success of the demo story."""
    out = log or sys.stdout

    def emit(msg: str = "") -> None:
        print(msg, file=out)

    cleanup = False
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="dpk-e2e-")
        cleanup = not keep
    else:
        output_dir = os.path.abspath(output_dir)
        os.makedirs(output_dir, exist_ok=True)

    out_path = Path(output_dir)
    traces_db = str(out_path / "traces.sqlite")
    baseline_db = str(out_path / "baseline.sqlite")
    attestable_json = str(out_path / "baseline_attestable.json")
    attestation_json = str(out_path / "baseline_attestation.json")

    emit("DProvenanceKit e2e decision-path demo")
    emit("Scope: tamper-evident record / CI gate / attest record — not 'prove sound reasoning'.")
    emit(f"Artifacts: {out_path}")

    try:
        # ── 1. Instrument + Record baseline ──────────────────────────────────
        _banner(emit, 1, "Instrument → Record (clean baseline path)")
        emit(f"  fixture steps: {step_types('baseline_path')}")
        store = SQLiteTraceStore(AnyTraceableEvent, traces_db, start_writer=False)
        try:
            baseline_id = record_path(store, "baseline_path")
            store.flush()
        finally:
            store.close()
        emit(f"  recorded baseline run={baseline_id}")
        emit(f"  db={traces_db}")

        # ── 2. Pin baseline via CLI ───────────────────────────────────────────
        _banner(emit, 2, "Baseline (dpk record)")
        record_argv = [
            "record",
            "--db",
            traces_db,
            "--baseline",
            baseline_db,
            "--run",
            str(baseline_id),
        ]
        _log_cmd(emit, record_argv)
        code = dpk_main(record_argv)
        if code != 0:
            emit(f"error: dpk record failed with exit {code}",)
            return 2
        emit(f"  pinned → {baseline_db}")

        # ── 3. Record regressed candidate ────────────────────────────────────
        _banner(emit, 3, "Record candidate with CRITICAL-path regression")
        emit(f"  fixture steps: {step_types('candidate_regressed')}")
        emit("  regression: removed claimVerified (CRITICAL)")
        store = SQLiteTraceStore(AnyTraceableEvent, traces_db, start_writer=False)
        try:
            candidate_id = record_path(store, "candidate_regressed")
            store.flush()
        finally:
            store.close()
        emit(f"  recorded candidate run={candidate_id}")

        # ── 4. Compare / Gate (expect FAIL / HIGH) ────────────────────────────
        _banner(emit, 4, "Compare → Gate (expect FAIL, HIGH)")
        gate_argv = [
            "gate",
            "--db",
            traces_db,
            "--golden",
            str(baseline_id),
            "--candidate",
            str(candidate_id),
            "--json",
        ]
        _log_cmd(emit, gate_argv)
        # Capture gate JSON via temporary redirect? dpk prints to stdout.
        # We need the JSON; use a pipe by calling RegressionGate... but prefer CLI.
        # Re-run with capturing: temporarily replace stdout.
        import io

        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            gate_code = dpk_main(gate_argv)
        finally:
            sys.stdout = old_stdout
        gate_out = buf.getvalue()
        emit(gate_out.rstrip())
        try:
            report = json.loads(gate_out)
        except json.JSONDecodeError:
            emit("error: gate did not emit JSON")
            return 2

        level = str(report.get("regression_level", "")).lower()
        passed = bool(report.get("passed"))
        if gate_code == 0 or passed or level != "high":
            emit(
                f"error: expected gate FAIL/high, got exit={gate_code} "
                f"passed={passed} level={level}"
            )
            return 1
        emit(f"  {MARKER_GATE_FAIL}")
        emit(f"  {MARKER_GATE_LEVEL}")
        emit(f"  removed/changed critical path caught (exit={gate_code})")

        # ── 5. Attest baseline ───────────────────────────────────────────────
        _banner(emit, 5, "Attest baseline (DPK-BINARY-V1 software P-256)")
        store = SQLiteTraceStore(AnyTraceableEvent, baseline_db, start_writer=False)
        try:
            # Baseline db has exactly one run (the pinned golden).
            meta = store.list_run_metadata()
            if not meta:
                emit("error: baseline db has no runs")
                return 2
            pinned_id = UUID(meta[0].run_id)
            trace = _run_to_attestable(store, pinned_id)
        finally:
            store.close()

        Path(attestable_json).write_text(
            json.dumps({"trace": trace.to_wire()}, indent=2),
            encoding="utf-8",
        )
        emit(f"  wrote attestable trace → {attestable_json}")
        emit(f"  events={len(trace.events)} edges={len(trace.edges)}")

        sign_argv = [
            "attest",
            "sign",
            "--in",
            attestable_json,
            "--out",
            attestation_json,
        ]
        _log_cmd(emit, sign_argv)
        sign_code = dpk_main(sign_argv)
        if sign_code != 0:
            emit(f"error: dpk attest sign failed with exit {sign_code}")
            return 2

        # ── 6. Verify attestation ────────────────────────────────────────────
        _banner(emit, 6, "Verify attestation")
        verify_argv = ["attest", "verify", "--in", attestation_json]
        _log_cmd(emit, verify_argv)
        verify_code = dpk_main(verify_argv)
        if verify_code != 0:
            emit(f"error: dpk attest verify failed with exit {verify_code}")
            return 1
        emit(f"  {MARKER_ATTEST_OK}")

        emit("")
        emit("-" * 72)
        emit(f"{MARKER_DEMO_OK}: gate failed on regressed candidate; baseline attestation verified.")
        emit("Claims kept honest: recorded path + CI gate + tamper-evident attest — not soundness.")
        return 0
    finally:
        if cleanup and os.path.isdir(output_dir):
            shutil.rmtree(output_dir, ignore_errors=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m examples.e2e_decision_path",
        description=(
            "End-to-end decision-path demo "
            "(Instrument → Record → Baseline → Gate → Attest → Verify)."
        ),
    )
    ap.add_argument(
        "--output-dir",
        default=os.environ.get("DPROV_E2E_OUT"),
        help="directory for sqlite/json artifacts (default: temp; set to keep)",
    )
    ap.add_argument(
        "--keep",
        action="store_true",
        help="keep the temp output directory when --output-dir is omitted",
    )
    args = ap.parse_args(list(argv) if argv is not None else None)
    return run_demo(args.output_dir, keep=args.keep)


if __name__ == "__main__":
    sys.exit(main())
