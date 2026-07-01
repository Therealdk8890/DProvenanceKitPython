"""Trace Specification v1 conformance suite.

Reads the frozen, language-neutral golden vectors under ``conformance/vectors/`` and
asserts the Python implementation reproduces every one. The committed JSON is the
contract; this test is the Python SDK's claim of conformance to it. A Swift / Rust /
TypeScript SDK satisfies the same spec by reproducing the same files.

If a vector and the implementation disagree, exactly one of two things is true:
  1. the implementation regressed  -> fix the code; or
  2. the contract changed on purpose -> rerun ``conformance/generate_vectors.py`` and
     review the diff (the regeneration is the deliberate, reviewable act).

Run just this suite with:  ``python -m pytest tests/test_conformance.py``
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

# The shared reference helpers live alongside the vectors, not in the package.
_CONFORMANCE_DIR = os.path.join(os.path.dirname(__file__), "..", "conformance")
if _CONFORMANCE_DIR not in sys.path:
    sys.path.insert(0, _CONFORMANCE_DIR)

from conformance_event import (  # noqa: E402
    ConformanceEvent,
    build_run,
    dsl_from_wire_dsl,
    exact_equality_evaluator,
)

from dprovenancekit import (  # noqa: E402
    AlignmentConfiguration,
    AlignmentMode,
    AlignmentProfile,
    AlignmentStrategy,
    DProvenanceKit,
    InMemoryTraceStore,
    SQLiteTraceStore,
    TraceAlignmentEngine,
    TracePriority,
)
from dprovenancekit.alignment_contract import AlignmentExecutionContract  # noqa: E402

_VECTORS_DIR = os.path.join(_CONFORMANCE_DIR, "vectors")


def _load(name: str) -> dict:
    with open(os.path.join(_VECTORS_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def _cases(name: str):
    """Yield (id, case) so pytest shows the case description on failure."""
    doc = _load(name)
    return [(c.get("description", str(i)), c) for i, c in enumerate(doc["cases"])]


# ── 1. Canonical payload encoding ──────────────────────────────────────────────

@pytest.mark.parametrize("desc, case", _cases("payload_encoding.json"))
def test_payload_encoding(desc, case):
    event = ConformanceEvent.from_spec(case["event"])
    assert event.encode().decode("utf-8") == case["canonical_json"]
    # The encoding must round-trip back to an equal payload.
    assert ConformanceEvent.decode(event.encode()) == event


# ── 2. Run fingerprint ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("desc, case", _cases("run_fingerprint.json"))
def test_run_fingerprint(desc, case):
    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteTraceStore(
            ConformanceEvent, os.path.join(tmp, "fp.sqlite"), start_writer=False
        )
        kit = DProvenanceKit(ConformanceEvent)
        with kit.run(context_id="fingerprint", store=store) as run:
            for ev in case["events"]:
                with kit.with_engine(ev.get("engine", "Unknown")):
                    kit.record(ConformanceEvent(type_name=ev["type"]))
        store.flush()
        rows = store._db.query(
            "SELECT fingerprint FROM runs WHERE run_id = ?", (str(run.run_id),)
        )
    assert rows[0][0] == case["fingerprint"]


# ── 3. Query semantics (both backends must agree with the vector) ──────────────

def _seed_in_memory(corpus):
    store = InMemoryTraceStore()
    kit = DProvenanceKit(ConformanceEvent)
    for spec in corpus:
        with kit.run(context_id=spec["context_id"], store=store):
            with kit.with_engine(spec["engine"]):
                for ev in spec["events"]:
                    kit.record(ConformanceEvent.from_spec(ev))
    return store


def _seed_sqlite(corpus, path):
    store = SQLiteTraceStore(ConformanceEvent, path, start_writer=False)
    kit = DProvenanceKit(ConformanceEvent)
    for spec in corpus:
        with kit.run(context_id=spec["context_id"], store=store):
            with kit.with_engine(spec["engine"]):
                for ev in spec["events"]:
                    kit.record(ConformanceEvent.from_spec(ev))
    store.flush()
    return store


@pytest.mark.parametrize("backend", ["in_memory", "sqlite"])
@pytest.mark.parametrize("desc, case", _cases("query_semantics.json"))
def test_query_semantics(backend, desc, case):
    corpus = _load("query_semantics.json")["corpus"]
    with tempfile.TemporaryDirectory() as tmp:
        if backend == "in_memory":
            store = _seed_in_memory(corpus)
        else:
            store = _seed_sqlite(corpus, os.path.join(tmp, "q.sqlite"))

        dsl = dsl_from_wire_dsl(case["dsl"])
        matched = sorted(r.context_id for r in store.query_runs(dsl))
    assert matched == case["expected_context_ids"]


# ── 4. Profile hash ────────────────────────────────────────────────────────────

def _profile_from_wire(d: dict) -> AlignmentProfile:
    return AlignmentProfile(
        strategy=AlignmentStrategy(d["strategy"]),
        version=d["version"],
        type_weight=d["type_weight"],
        payload_weight=d["payload_weight"],
        structural_weight=d["structural_weight"],
        temporal_weight=d["temporal_weight"],
        semantic_threshold=d["semantic_threshold"],
        max_ambiguous_candidates=d["max_ambiguous_candidates"],
        ambiguity_delta_threshold=d["ambiguity_delta_threshold"],
        alignment_mode=AlignmentMode(d["alignment_mode"]),
    )


@pytest.mark.parametrize("desc, case", _cases("profile_hash.json"))
def test_profile_hash(desc, case):
    computed = AlignmentExecutionContract.compute_profile_hash(
        profile=_profile_from_wire(case["profile"]),
        evaluator_identifier=case["evaluator_identifier"],
        engine_version=case["engine_version"],
    )
    assert computed == case["profile_hash"]


# ── 5. Alignment verdict ───────────────────────────────────────────────────────

_NAMED_PROFILES = {
    "strict_audit_v1": AlignmentProfile.strict_audit_v1,
    "developer_debug_v1": AlignmentProfile.developer_debug_v1,
}


@pytest.mark.parametrize("desc, case", _cases("alignment_verdict.json"))
def test_alignment_verdict(desc, case):
    config = AlignmentConfiguration(
        profile=_NAMED_PROFILES[case["profile"]],
        equivalence_evaluator=exact_equality_evaluator(),
    )
    engine = TraceAlignmentEngine(config)
    base = build_run(0, {"context_id": "base", "engine": "E", "events": case["base"]})
    comparison = build_run(
        1, {"context_id": "comparison", "engine": "E", "events": case["comparison"]}
    )
    minimum = TracePriority[case.get("minimum_priority", "STRUCTURAL")]
    result = engine.align(base=base, comparison=comparison, minimum_priority=minimum)

    ordered = AlignmentExecutionContract.canonical_sort_alignments(result.alignments)
    expected = case["expected"]
    assert result.regression_risk.level.value == expected["regression_level"]
    assert round(result.regression_risk.strength, 6) == expected["regression_strength"]
    assert [a.state.kind.value for a in ordered] == expected["alignment_state_kinds"]

# git-blob-rewrite
