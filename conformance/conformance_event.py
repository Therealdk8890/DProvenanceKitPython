"""Shared reference helpers for the Trace Specification v1 conformance suite.

These are deliberately tiny and dependency-free so that both the vector *generator*
(``generate_vectors.py``) and the Python *checker* (``tests/test_conformance.py``) build
their runs, events, queries, and evaluators from one definition — there is no second
copy to drift.

Everything here is the Python expression of constructs the language-neutral spec defines
in prose (see ``TRACE_SPEC_v1.md``). A Swift / Rust / TypeScript SDK reimplements these
same constructs natively; the JSON vectors are the contract they must all reproduce.
"""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

# Make the package importable whether this module is loaded by the generator (run from
# anywhere) or by pytest (which adds ``src`` via conftest, but we must not depend on that).
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from dprovenancekit import (  # noqa: E402
    AlignmentConfiguration,
    AnyEquivalenceEvaluator,
    TraceableEvent,
    TraceEvent,
    TracePriority,
    TraceRun,
)
from dprovenancekit.query import (  # noqa: E402
    AfterNode,
    AndNode,
    BeforeNode,
    ContainsStep,
    ContextIDEquals,
    EngineNameEquals,
    MissingStep,
    NotNode,
    OrNode,
    SequenceNode,
    TraceQueryDSL,
    TraceQueryNode,
)

#: Stable identifier for the canonical conformance equivalence evaluator. Part of the
#: profile hash, so it is a contract value: any SDK reproducing the alignment vectors
#: MUST use this exact string.
EXACT_EQUALITY_EVALUATOR_ID = "ExactEquality_v1"


@dataclass(frozen=True)
class ConformanceEvent(TraceableEvent):
    """A fully self-describing event whose entire state lives in the vector JSON.

    ``attributes`` is held as a sorted tuple of pairs (not a dict) so the event is
    hashable and its canonical encoding is deterministic regardless of insertion order.
    """

    type_name: str
    priority_value: int = int(TracePriority.CRITICAL)
    attributes: Tuple[Tuple[str, object], ...] = ()

    @property
    def type_identifier(self) -> str:
        return self.type_name

    @property
    def priority(self) -> TracePriority:
        try:
            return TracePriority(self.priority_value)
        except ValueError:
            return TracePriority.TELEMETRY

    def to_dict(self) -> dict:
        out: Dict[str, object] = {"type": self.type_name, "priority": self.priority_value}
        for key, value in self.attributes:
            out[key] = value
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "ConformanceEvent":
        attrs = tuple(
            sorted((k, v) for k, v in data.items() if k not in ("type", "priority"))
        )
        return cls(
            type_name=data["type"],
            priority_value=int(data.get("priority", int(TracePriority.CRITICAL))),
            attributes=attrs,
        )

    @classmethod
    def from_spec(cls, spec: dict) -> "ConformanceEvent":
        """Build from a vector ``event`` object: ``{type, priority?, attributes?}``."""
        attrs = tuple(sorted((spec.get("attributes") or {}).items()))
        return cls(
            type_name=spec["type"],
            priority_value=int(spec.get("priority", int(TracePriority.CRITICAL))),
            attributes=attrs,
        )


def deterministic_uuid(n: int) -> uuid.UUID:
    """A reproducible UUID so regenerating vectors never churns ids in the git diff."""
    return uuid.UUID(int=n)


def build_run(run_index: int, spec: dict) -> TraceRun:
    """Materialize a vector run spec into an in-memory :class:`TraceRun`.

    ``spec`` shape: ``{"context_id": str, "engine": str, "events": [event_spec, ...]}``
    where each ``event_spec`` may carry ``span_id`` / ``parent_span_id``. Sequence and
    timestamp are assigned from event order so generation is fully deterministic.
    """
    run_id = deterministic_uuid(run_index + 1)
    engine = spec.get("engine", "Unknown")
    events: List[TraceEvent] = []
    for i, ev in enumerate(spec["events"]):
        events.append(
            TraceEvent(
                run_id=run_id,
                context_id=spec["context_id"],
                engine_name=engine,
                schema_version=1,
                sequence=i,
                span_id=ev.get("span_id"),
                parent_span_id=ev.get("parent_span_id"),
                payload=ConformanceEvent.from_spec(ev),
                # Prefer an explicit id from the spec — the alignment vectors pin event ids
                # because the canonical alignment ordering tiebreaks on (sequence, id).
                id=(uuid.UUID(ev["id"]) if ev.get("id") else deterministic_uuid((run_index + 1) * 1000 + i)),
                timestamp=float(i),
            )
        )
    return TraceRun(run_id=run_id, context_id=spec["context_id"], events=events)


def exact_equality_evaluator() -> AnyEquivalenceEvaluator:
    """The canonical conformance evaluator: payloads are equivalent iff fully equal."""
    return AnyEquivalenceEvaluator(
        evaluator_identifier=EXACT_EQUALITY_EVALUATOR_ID,
        evaluator=lambda a, b: 1.0 if a == b else 0.0,
    )


def dsl_from_wire(node: dict) -> TraceQueryNode:
    """Reference deserializer for the query wire form (inverse of ``_serialize_node``).

    The spec defines the wire form; this turns it back into an evaluable AST so the same
    JSON a Swift client would POST can drive the Python backends in the conformance test.
    """
    kind = node["type"]
    if kind == "and":
        return AndNode(nodes=tuple(dsl_from_wire(n) for n in node["nodes"]))
    if kind == "or":
        return OrNode(nodes=tuple(dsl_from_wire(n) for n in node["nodes"]))
    if kind == "not":
        return NotNode(node=dsl_from_wire(node["node"]))
    if kind == "contextIDEquals":
        return ContextIDEquals(context_id=node["id"])
    if kind == "engineNameEquals":
        return EngineNameEquals(name=node["name"])
    if kind == "containsStep":
        return ContainsStep(step=node["step"])
    if kind == "missingStep":
        return MissingStep(step=node["step"])
    if kind == "sequence":
        return SequenceNode(steps=tuple(node["steps"]))
    if kind == "after":
        return AfterNode(step=node["step"], followed_by=node["followedBy"])
    if kind == "before":
        return BeforeNode(step=node["step"], preceded_by=node["precededBy"])
    raise ValueError(f"unknown query wire node: {kind!r}")


def dsl_from_wire_dsl(node: dict) -> TraceQueryDSL:
    """Wrap :func:`dsl_from_wire` in the DSL container the stores accept."""
    return TraceQueryDSL(_root=dsl_from_wire(node))
