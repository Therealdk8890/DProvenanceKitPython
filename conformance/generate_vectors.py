#!/usr/bin/env python3
"""Regenerate the Trace Specification v1 golden vectors from the Python reference SDK.

The Python implementation is the v1 conformance oracle (it is a faithful, fully-tested
port of the Swift SDK). This script drives that implementation over a fixed set of
inputs and freezes the observed outputs as language-neutral JSON under ``vectors/``.

    python conformance/generate_vectors.py

Regenerating is an *intentional* act: review the resulting git diff. ``tests/
test_conformance.py`` reads the committed JSON and fails if the implementation drifts
from it, so an unreviewed change to encoding, fingerprinting, query semantics, the
profile hash, or alignment will surface as a red test rather than a silent regeneration.

Every value emitted here is a contract a Swift / Rust / TypeScript SDK must reproduce.
"""

from __future__ import annotations

import json
import os
import tempfile

from conformance_event import (
    EXACT_EQUALITY_EVALUATOR_ID,
    ConformanceEvent,
    build_run,
    dsl_from_wire_dsl,
    exact_equality_evaluator,
)

from dprovenancekit import (
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
from dprovenancekit.alignment_contract import AlignmentExecutionContract

SPEC_VERSION = "1.0"
VECTORS_DIR = os.path.join(os.path.dirname(__file__), "vectors")


def _write(name: str, doc: dict) -> None:
    os.makedirs(VECTORS_DIR, exist_ok=True)
    path = os.path.join(VECTORS_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    count = len(doc.get("cases", doc.get("corpus", [])))
    print(f"wrote {os.path.relpath(path)}  ({count} entries)")


# -- 1. Canonical payload encoding ----------------------------------------------
# Pins the bytes a payload serializes to. The run fingerprint does NOT depend on these
# bytes, so a byte-different (but semantically equal) encoding from another SDK is still
# conformant for run equivalence -- see the spec's "Conformance Notes".

def gen_payload_encoding() -> dict:
    samples = [
        {"type": "documentEvaluated", "priority": 2, "attributes": {"score": 0.95, "doc": "A"}},
        {"type": "finalDecisionMade", "priority": 3, "attributes": {"approved": False}},
        {"type": "conflictDetected", "priority": 1, "attributes": {}},
        # Key-order independence: same logical event, attributes given out of order.
        {"type": "z_event", "priority": 0, "attributes": {"b": 2, "a": 1}},
    ]
    cases = []
    for spec in samples:
        event = ConformanceEvent.from_spec(spec)
        canonical = event.encode().decode("utf-8")
        cases.append({"event": spec, "canonical_json": canonical})
    return {
        "spec_version": SPEC_VERSION,
        "description": "Canonical JSON encoding of an event payload (keys sorted, UTF-8).",
        "cases": cases,
    }


# -- 2. Run fingerprint ---------------------------------------------------------
# The hard cross-language equivalence anchor: SHA-1 over the per-event signature
# `type + ":" + engine + "|"` folded in commit order. Derived here from the real
# SQLite store so the vector pins true behavior, not a re-derivation.

def _fingerprint_for(events) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "fp.sqlite")
        store = SQLiteTraceStore(ConformanceEvent, path, start_writer=False)
        kit = DProvenanceKit(ConformanceEvent)
        with kit.run(context_id="fingerprint", store=store) as run:
            for ev in events:
                with kit.with_engine(ev.get("engine", "Unknown")):
                    kit.record(ConformanceEvent(type_name=ev["type"]))
        store.flush()
        rows = store._db.query(
            "SELECT fingerprint FROM runs WHERE run_id = ?", (str(run.run_id),)
        )
        return rows[0][0]


def gen_run_fingerprint() -> dict:
    samples = [
        {
            "description": "Single-engine three-step run.",
            "events": [
                {"type": "promptGenerated", "engine": "Planner"},
                {"type": "documentEvaluated", "engine": "Planner"},
                {"type": "finalDecisionMade", "engine": "Planner"},
            ],
        },
        {
            "description": "Engine changes mid-run (engine is part of the signature).",
            "events": [
                {"type": "promptGenerated", "engine": "Planner"},
                {"type": "documentEvaluated", "engine": "Analyzer"},
                {"type": "finalDecisionMade", "engine": "Decider"},
            ],
        },
        {
            "description": "Reordering the same steps yields a different fingerprint.",
            "events": [
                {"type": "documentEvaluated", "engine": "Planner"},
                {"type": "promptGenerated", "engine": "Planner"},
                {"type": "finalDecisionMade", "engine": "Planner"},
            ],
        },
        {
            "description": "Single event.",
            "events": [{"type": "promptGenerated", "engine": "Planner"}],
        },
    ]
    cases = []
    for spec in samples:
        cases.append({**spec, "fingerprint": _fingerprint_for(spec["events"])})
    return {
        "spec_version": SPEC_VERSION,
        "algorithm": "sha1( concat( type + ':' + engine + '|' for each event in commit order ) )",
        "description": "Run fingerprint: the per-run identity over (type, engine) signatures.",
        "cases": cases,
    }


# -- 3. Query semantics ---------------------------------------------------------
# A shared corpus + a query (in wire form) -> the exact set of matching context_ids.
# Both Python backends must reproduce this; a remote/cloud backend must too.

_QUERY_CORPUS = [
    {
        "context_id": "run-complete",
        "engine": "Reasoner",
        "events": [
            {"type": "documentEvaluated"},
            {"type": "conflictDetected"},
            {"type": "finalDecisionMade"},
        ],
    },
    {
        "context_id": "run-unverified-conflict",
        "engine": "Reasoner",
        "events": [
            {"type": "conflictDetected"},
            {"type": "finalDecisionMade"},
        ],
    },
    {
        "context_id": "run-reordered",
        "engine": "Auditor",
        "events": [
            {"type": "conflictDetected"},
            {"type": "documentEvaluated"},
            {"type": "finalDecisionMade"},
        ],
    },
    {
        "context_id": "run-no-decision",
        "engine": "Auditor",
        "events": [
            {"type": "documentEvaluated"},
        ],
    },
]


def _query_cases():
    return [
        {
            "description": "Conflict reported but no document ever evaluated (the README query).",
            "dsl": {
                "type": "and",
                "nodes": [
                    {"type": "containsStep", "step": "conflictDetected"},
                    {"type": "missingStep", "step": "documentEvaluated"},
                ],
            },
        },
        {
            "description": "documentEvaluated occurs before the first finalDecisionMade.",
            "dsl": {"type": "before", "step": "finalDecisionMade", "precededBy": "documentEvaluated"},
        },
        {
            "description": "Ordered subsequence: evaluate then decide.",
            "dsl": {"type": "sequence", "steps": ["documentEvaluated", "finalDecisionMade"]},
        },
        {
            "description": "Runs produced by the Auditor engine.",
            "dsl": {"type": "engineNameEquals", "name": "Auditor"},
        },
        {
            "description": "Any run that reached a final decision.",
            "dsl": {"type": "containsStep", "step": "finalDecisionMade"},
        },
    ]


def gen_query_semantics() -> dict:
    store = InMemoryTraceStore()
    kit = DProvenanceKit(ConformanceEvent)
    for spec in _QUERY_CORPUS:
        with kit.run(context_id=spec["context_id"], store=store):
            with kit.with_engine(spec["engine"]):
                for ev in spec["events"]:
                    kit.record(ConformanceEvent.from_spec(ev))

    cases = []
    for case in _query_cases():
        dsl = dsl_from_wire_dsl(case["dsl"])
        matched = sorted(r.context_id for r in store.query_runs(dsl))
        cases.append({**case, "expected_context_ids": matched})
    return {
        "spec_version": SPEC_VERSION,
        "description": "Query semantics: a corpus + DSL (wire form) -> matching context_ids.",
        "corpus": _QUERY_CORPUS,
        "cases": cases,
    }


# -- 4. Profile hash ------------------------------------------------------------
# SHA-256 over the frozen execution contract. Float formatting follows the spec's
# rule exactly (integral values keep one decimal). Sensitive to every weight,
# threshold, the evaluator identifier, and the engine version.

def _profile_to_wire(profile: AlignmentProfile) -> dict:
    return {
        "strategy": profile.strategy.value,
        "version": profile.version,
        "type_weight": profile.type_weight,
        "payload_weight": profile.payload_weight,
        "structural_weight": profile.structural_weight,
        "temporal_weight": profile.temporal_weight,
        "semantic_threshold": profile.semantic_threshold,
        "max_ambiguous_candidates": profile.max_ambiguous_candidates,
        "ambiguity_delta_threshold": profile.ambiguity_delta_threshold,
        "alignment_mode": profile.alignment_mode.value,
    }


def gen_profile_hash() -> dict:
    custom = AlignmentProfile(
        strategy=AlignmentStrategy.SEMANTIC_EXPLORATION,
        version=2,
        type_weight=0.3,
        payload_weight=0.45,
        structural_weight=0.15,
        temporal_weight=0.1,
        semantic_threshold=0.8,
        max_ambiguous_candidates=5,
        ambiguity_delta_threshold=0.25,
        alignment_mode=AlignmentMode.FULL_GRAPH,
    )
    samples = [
        ("strict_audit_v1", AlignmentProfile.strict_audit_v1, EXACT_EQUALITY_EVALUATOR_ID, "1.0.0"),
        ("developer_debug_v1", AlignmentProfile.developer_debug_v1, EXACT_EQUALITY_EVALUATOR_ID, "1.0.0"),
        ("strict_audit_v1 / different evaluator", AlignmentProfile.strict_audit_v1, "OtherEvaluator", "1.0.0"),
        ("strict_audit_v1 / different engine version", AlignmentProfile.strict_audit_v1, EXACT_EQUALITY_EVALUATOR_ID, "2.0.0"),
        ("custom semantic-exploration profile", custom, EXACT_EQUALITY_EVALUATOR_ID, "1.0.0"),
    ]
    cases = []
    for description, profile, evaluator_id, engine_version in samples:
        expected = AlignmentExecutionContract.compute_profile_hash(
            profile=profile, evaluator_identifier=evaluator_id, engine_version=engine_version
        )
        cases.append(
            {
                "description": description,
                "profile": _profile_to_wire(profile),
                "evaluator_identifier": evaluator_id,
                "engine_version": engine_version,
                "profile_hash": expected,
            }
        )
    return {
        "spec_version": SPEC_VERSION,
        "contract_version": AlignmentExecutionContract.contract_version,
        "algorithm": "sha256 over the frozen contract string (see TRACE_SPEC_v1.md section 6).",
        "description": "Alignment profile hash: the version-stamp two runs must share to be comparable.",
        "cases": cases,
    }


# -- 5. Alignment verdict -------------------------------------------------------
# base run + comparison run + profile -> regression level and the ordered alignment
# state kinds. The canonical exact-equality evaluator makes this language-neutral.

_PROFILES = {
    "strict_audit_v1": AlignmentProfile.strict_audit_v1,
    "developer_debug_v1": AlignmentProfile.developer_debug_v1,
}


def _verdict_for(case: dict) -> dict:
    config = AlignmentConfiguration(
        profile=_PROFILES[case["profile"]],
        equivalence_evaluator=exact_equality_evaluator(),
    )
    engine = TraceAlignmentEngine(config)
    base = build_run(0, {"context_id": "base", "engine": "E", "events": case["base"]})
    comparison = build_run(1, {"context_id": "comparison", "engine": "E", "events": case["comparison"]})
    minimum = TracePriority[case.get("minimum_priority", "STRUCTURAL")]
    result = engine.align(base=base, comparison=comparison, minimum_priority=minimum)

    ordered = AlignmentExecutionContract.canonical_sort_alignments(result.alignments)
    return {
        "regression_level": result.regression_risk.level.value,
        "regression_strength": round(result.regression_risk.strength, 6),
        "alignment_state_kinds": [a.state.kind.value for a in ordered],
    }


def gen_alignment_verdict() -> dict:
    samples = [
        {
            "description": "Identical runs -> no regression, all exact matches.",
            "profile": "strict_audit_v1",
            "base": [
                {"type": "documentEvaluated", "attributes": {"doc": "A"}},
                {"type": "finalDecisionMade", "attributes": {"approved": True}},
            ],
            "comparison": [
                {"type": "documentEvaluated", "attributes": {"doc": "A"}},
                {"type": "finalDecisionMade", "attributes": {"approved": True}},
            ],
        },
        {
            "description": "A critical step present in base is absent in comparison.",
            "profile": "strict_audit_v1",
            "base": [
                {"type": "documentEvaluated", "attributes": {"doc": "A"}},
                {"type": "conflictDetected"},
                {"type": "finalDecisionMade", "attributes": {"approved": True}},
            ],
            "comparison": [
                {"type": "documentEvaluated", "attributes": {"doc": "A"}},
                {"type": "finalDecisionMade", "attributes": {"approved": True}},
            ],
        },
        {
            "description": "Same steps, payload of one step changed.",
            "profile": "strict_audit_v1",
            "base": [
                {"type": "documentEvaluated", "attributes": {"doc": "A", "score": 0.9}},
                {"type": "finalDecisionMade", "attributes": {"approved": True}},
            ],
            "comparison": [
                {"type": "documentEvaluated", "attributes": {"doc": "A", "score": 0.4}},
                {"type": "finalDecisionMade", "attributes": {"approved": True}},
            ],
        },
        {
            "description": "Comparison adds a step base never had.",
            "profile": "developer_debug_v1",
            "base": [
                {"type": "documentEvaluated", "attributes": {"doc": "A"}},
                {"type": "finalDecisionMade", "attributes": {"approved": True}},
            ],
            "comparison": [
                {"type": "documentEvaluated", "attributes": {"doc": "A"}},
                {"type": "conflictDetected"},
                {"type": "finalDecisionMade", "attributes": {"approved": True}},
            ],
        },
    ]
    cases = []
    for spec in samples:
        cases.append({**spec, "expected": _verdict_for(spec)})
    return {
        "spec_version": SPEC_VERSION,
        "evaluator_identifier": EXACT_EQUALITY_EVALUATOR_ID,
        "evaluator_rule": "similarity(a, b) = 1.0 if payloads are fully equal else 0.0",
        "description": "Alignment verdict: regression level + ordered per-event alignment states.",
        "cases": cases,
    }


def main() -> None:
    _write("payload_encoding.json", gen_payload_encoding())
    _write("run_fingerprint.json", gen_run_fingerprint())
    _write("query_semantics.json", gen_query_semantics())
    _write("profile_hash.json", gen_profile_hash())
    _write("alignment_verdict.json", gen_alignment_verdict())
    print("done.")


if __name__ == "__main__":
    main()
