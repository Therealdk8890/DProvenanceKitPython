"""The trace query language: one AST, two backends.

Queries are built with a fluent DSL (:class:`TraceQueryDSL`) that lowers to an AST
(:class:`TraceQueryNode`). That one AST is evaluated two completely different ways: in
memory (:meth:`TraceQueryNode.evaluate`) and compiled to SQL
(:class:`TraceQueryCompiler`). The two must agree on every input — see the parity tests.

All temporal operators order events by ``sequence``, the authoritative per-run causal
counter, never by ``timestamp`` (which can tie under bursts). ``.after`` / ``.before``
anchor to the FIRST occurrence of ``step`` (``MIN(sequence)``), mirroring the in-memory
evaluator's ``first index of`` semantics.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import List, Set

from .event import TraceEvent


@dataclass(frozen=True)
class TraceRun:
    run_id: uuid.UUID
    context_id: str
    events: List[TraceEvent]


# MARK: - AST nodes --------------------------------------------------------------


class TraceQueryNode:
    """Base class for query AST nodes."""

    def evaluate(self, run: TraceRun) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError


@dataclass(frozen=True)
class AndNode(TraceQueryNode):
    nodes: tuple

    def evaluate(self, run: TraceRun) -> bool:
        if not self.nodes:
            return True
        return all(n.evaluate(run) for n in self.nodes)


@dataclass(frozen=True)
class OrNode(TraceQueryNode):
    nodes: tuple

    def evaluate(self, run: TraceRun) -> bool:
        if not self.nodes:
            return True
        return any(n.evaluate(run) for n in self.nodes)


@dataclass(frozen=True)
class NotNode(TraceQueryNode):
    node: TraceQueryNode

    def evaluate(self, run: TraceRun) -> bool:
        return not self.node.evaluate(run)


@dataclass(frozen=True)
class ContextIDEquals(TraceQueryNode):
    context_id: str

    def evaluate(self, run: TraceRun) -> bool:
        return run.context_id == self.context_id


@dataclass(frozen=True)
class EngineNameEquals(TraceQueryNode):
    name: str

    def evaluate(self, run: TraceRun) -> bool:
        return any(e.engine_name == self.name for e in run.events)


@dataclass(frozen=True)
class ContainsStep(TraceQueryNode):
    step: str

    def evaluate(self, run: TraceRun) -> bool:
        return any(e.payload.type_identifier == self.step for e in run.events)


@dataclass(frozen=True)
class MissingStep(TraceQueryNode):
    step: str

    def evaluate(self, run: TraceRun) -> bool:
        return all(e.payload.type_identifier != self.step for e in run.events)


@dataclass(frozen=True)
class SequenceNode(TraceQueryNode):
    steps: tuple

    def evaluate(self, run: TraceRun) -> bool:
        if not self.steps:
            return True
        types = [e.payload.type_identifier for e in run.events]
        current = 0
        for t in types:
            if t == self.steps[current]:
                current += 1
                if current == len(self.steps):
                    return True
        return False


@dataclass(frozen=True)
class AfterNode(TraceQueryNode):
    step: str
    followed_by: str

    def evaluate(self, run: TraceRun) -> bool:
        types = [e.payload.type_identifier for e in run.events]
        if self.step in types:
            first_idx = types.index(self.step)
            return self.followed_by in types[first_idx:]
        return False


@dataclass(frozen=True)
class BeforeNode(TraceQueryNode):
    step: str
    preceded_by: str

    def evaluate(self, run: TraceRun) -> bool:
        types = [e.payload.type_identifier for e in run.events]
        if self.step in types:
            first_idx = types.index(self.step)
            return self.preceded_by in types[:first_idx]
        return False


# MARK: - Fluent DSL -------------------------------------------------------------


@dataclass(frozen=True)
class TraceQueryDSL:
    """A fluent, immutable builder that lowers to a :class:`TraceQueryNode` AST."""

    schema_version: str = "1.0"
    _root: TraceQueryNode = field(default_factory=lambda: AndNode(nodes=()))

    @property
    def ast(self) -> TraceQueryNode:
        return self._root

    def _with(self, root: TraceQueryNode) -> "TraceQueryDSL":
        return TraceQueryDSL(schema_version=self.schema_version, _root=root)

    def _append_to_and(self, node: TraceQueryNode) -> "TraceQueryDSL":
        if isinstance(self._root, AndNode):
            return self._with(AndNode(nodes=self._root.nodes + (node,)))
        return self._with(AndNode(nodes=(self._root, node)))

    def filter_context_id(self, context_id: str) -> "TraceQueryDSL":
        return self._append_to_and(ContextIDEquals(context_id))

    def filter_engine_name(self, engine_name: str) -> "TraceQueryDSL":
        return self._append_to_and(EngineNameEquals(engine_name))

    def requiring_step(self, step: str) -> "TraceQueryDSL":
        return self._append_to_and(ContainsStep(step))

    def missing_step(self, step: str) -> "TraceQueryDSL":
        return self._append_to_and(MissingStep(step))

    def requiring_sequence(self, sequence: List[str]) -> "TraceQueryDSL":
        return self._append_to_and(SequenceNode(steps=tuple(sequence)))

    def requiring_followed_by(self, step: str, followed_by: str) -> "TraceQueryDSL":
        return self._append_to_and(AfterNode(step=step, followed_by=followed_by))

    def requiring_preceded_by(self, step: str, preceded_by: str) -> "TraceQueryDSL":
        return self._append_to_and(BeforeNode(step=step, preceded_by=preceded_by))

    def or_(self, other: "TraceQueryDSL") -> "TraceQueryDSL":
        return self._with(OrNode(nodes=(self._root, other._root)))


# MARK: - Query planner ----------------------------------------------------------


@dataclass(frozen=True)
class IndexConstraint:
    """A guaranteed index constraint extracted from an AST. ``kind`` is one of
    ``contextID`` / ``engineName`` / ``decisionType``."""

    kind: str
    value: str


class TraceQueryPlanner:
    @staticmethod
    def extract_guaranteed_constraints(ast: TraceQueryNode) -> Set[IndexConstraint]:
        """Constraints every valid evaluation of the AST MUST satisfy."""
        if isinstance(ast, AndNode):
            out: Set[IndexConstraint] = set()
            for node in ast.nodes:
                out |= TraceQueryPlanner.extract_guaranteed_constraints(node)
            return out
        if isinstance(ast, OrNode):
            if not ast.nodes:
                return set()
            shared = TraceQueryPlanner.extract_guaranteed_constraints(ast.nodes[0])
            for node in ast.nodes[1:]:
                shared &= TraceQueryPlanner.extract_guaranteed_constraints(node)
            return shared
        if isinstance(ast, NotNode):
            return set()
        if isinstance(ast, ContextIDEquals):
            return {IndexConstraint("contextID", ast.context_id)}
        if isinstance(ast, EngineNameEquals):
            return {IndexConstraint("engineName", ast.name)}
        if isinstance(ast, ContainsStep):
            return {IndexConstraint("decisionType", ast.step)}
        if isinstance(ast, SequenceNode):
            return {IndexConstraint("decisionType", s) for s in ast.steps}
        if isinstance(ast, AfterNode):
            return {
                IndexConstraint("decisionType", ast.step),
                IndexConstraint("decisionType", ast.followed_by),
            }
        if isinstance(ast, BeforeNode):
            return {
                IndexConstraint("decisionType", ast.step),
                IndexConstraint("decisionType", ast.preceded_by),
            }
        # MissingStep: a negative constraint, not indexable for presence inclusion.
        return set()

    @staticmethod
    def extract_all_referenced_decision_types(ast: TraceQueryNode) -> Set[str]:
        """All decision types referenced by the AST, required, missing, or sequential."""
        if isinstance(ast, (AndNode, OrNode)):
            out: Set[str] = set()
            for node in ast.nodes:
                out |= TraceQueryPlanner.extract_all_referenced_decision_types(node)
            return out
        if isinstance(ast, NotNode):
            return TraceQueryPlanner.extract_all_referenced_decision_types(ast.node)
        if isinstance(ast, (ContainsStep, MissingStep)):
            return {ast.step}
        if isinstance(ast, SequenceNode):
            return set(ast.steps)
        if isinstance(ast, AfterNode):
            return {ast.step, ast.followed_by}
        if isinstance(ast, BeforeNode):
            return {ast.step, ast.preceded_by}
        return set()


# MARK: - SQL compiler -----------------------------------------------------------


@dataclass(frozen=True)
class CompiledSQLQuery:
    sql: str
    bindings: List[str]


class TraceQueryCompiler:
    @staticmethod
    def compile(node: TraceQueryNode) -> CompiledSQLQuery:
        return TraceQueryCompiler._compile_node(node)

    @staticmethod
    def _compile_node(node: TraceQueryNode) -> CompiledSQLQuery:
        if isinstance(node, AndNode):
            if not node.nodes:
                return CompiledSQLQuery("SELECT run_id FROM runs", [])
            compiled = [TraceQueryCompiler._compile_node(n) for n in node.nodes]
            sql = "\nINTERSECT\n".join(c.sql for c in compiled)
            bindings: List[str] = [b for c in compiled for b in c.bindings]
            return CompiledSQLQuery(sql, bindings)

        if isinstance(node, OrNode):
            if not node.nodes:
                return CompiledSQLQuery("SELECT run_id FROM runs", [])
            compiled = [TraceQueryCompiler._compile_node(n) for n in node.nodes]
            sql = "\nUNION\n".join(c.sql for c in compiled)
            bindings = [b for c in compiled for b in c.bindings]
            return CompiledSQLQuery(sql, bindings)

        if isinstance(node, NotNode):
            compiled = TraceQueryCompiler._compile_node(node.node)
            return CompiledSQLQuery(
                f"SELECT run_id FROM runs EXCEPT\n{compiled.sql}", compiled.bindings
            )

        if isinstance(node, ContextIDEquals):
            return CompiledSQLQuery(
                "SELECT run_id FROM runs WHERE context_id = ?", [node.context_id]
            )

        if isinstance(node, EngineNameEquals):
            return CompiledSQLQuery(
                "SELECT DISTINCT run_id FROM trace_events WHERE engine = ?", [node.name]
            )

        if isinstance(node, ContainsStep):
            return CompiledSQLQuery(
                "SELECT DISTINCT run_id FROM trace_events WHERE type = ?", [node.step]
            )

        if isinstance(node, MissingStep):
            return CompiledSQLQuery(
                "SELECT run_id FROM runs EXCEPT "
                "SELECT DISTINCT run_id FROM trace_events WHERE type = ?",
                [node.step],
            )

        if isinstance(node, AfterNode):
            # `followed_by` occurs at or after the FIRST occurrence of `step`.
            sql = (
                "SELECT DISTINCT e.run_id\n"
                "FROM trace_events e\n"
                "JOIN (\n"
                "    SELECT run_id, MIN(sequence) AS anchor_seq\n"
                "    FROM trace_events\n"
                "    WHERE type = ?\n"
                "    GROUP BY run_id\n"
                ") anchor ON e.run_id = anchor.run_id\n"
                "WHERE e.type = ? AND e.sequence >= anchor.anchor_seq"
            )
            return CompiledSQLQuery(sql, [node.step, node.followed_by])

        if isinstance(node, BeforeNode):
            # `preceded_by` occurs strictly before the FIRST occurrence of `step`.
            sql = (
                "SELECT DISTINCT e.run_id\n"
                "FROM trace_events e\n"
                "JOIN (\n"
                "    SELECT run_id, MIN(sequence) AS anchor_seq\n"
                "    FROM trace_events\n"
                "    WHERE type = ?\n"
                "    GROUP BY run_id\n"
                ") anchor ON e.run_id = anchor.run_id\n"
                "WHERE e.type = ? AND e.sequence < anchor.anchor_seq"
            )
            return CompiledSQLQuery(sql, [node.step, node.preceded_by])

        if isinstance(node, SequenceNode):
            steps = node.steps
            if not steps:
                return CompiledSQLQuery("SELECT run_id FROM runs", [])
            if len(steps) == 1:
                return TraceQueryCompiler._compile_node(ContainsStep(steps[0]))

            # Subsequence existence: a strictly increasing chain of DISTINCT events
            # whose types match `steps` in order, chained on `sequence`.
            sql = "SELECT DISTINCT e0.run_id\nFROM trace_events e0"
            for i in range(1, len(steps)):
                sql += f"\nJOIN trace_events e{i} ON e{i}.run_id = e{i - 1}.run_id"
            sql += "\nWHERE e0.type = ?"
            for i in range(1, len(steps)):
                sql += f" AND e{i}.type = ?"
            for i in range(1, len(steps)):
                sql += f" AND e{i - 1}.sequence < e{i}.sequence"
            return CompiledSQLQuery(sql, list(steps))

        raise ValueError(f"Unknown query node: {node!r}")  # pragma: no cover
