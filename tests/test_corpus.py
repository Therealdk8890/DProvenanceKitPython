"""Ports DProvenanceCorpusTests."""

from __future__ import annotations

import pytest

from dprovenancekit import (
    AlignmentConfiguration,
    AlignmentProfile,
    AlignmentStateKind,
    AnyEquivalenceEvaluator,
    DProvenanceCorpus,
    RegressionLevel,
    TraceAlignmentEngine,
)

AgentEvent = DProvenanceCorpus.AgentEvent
_K = AgentEvent("decision").kind.__class__  # the private _AgentKind enum


def _corpus_evaluator():
    def evaluate(b, c):
        if b.kind != c.kind:
            return 0.0
        from dprovenancekit.corpus import _AgentKind

        if b.kind == _AgentKind.FILE_IO:
            return 1.0 if (b.action == c.action and b.file == c.file) else 0.0
        if b.kind == _AgentKind.TOOL_EXECUTION:
            if b.tool_name == c.tool_name and b.params == c.params:
                return 1.0
            if {b.tool_name, c.tool_name} == {"SearchDocumentation", "LookupAPIDocs"} and b.params == c.params:
                return 0.95
            return 0.0
        if b.kind == _AgentKind.PLANNING:
            return 1.0 if b.hypothesis == c.hypothesis else 0.0
        if b.kind == _AgentKind.DECISION:
            return 1.0 if b.action == c.action else 0.0
        return 0.0

    return AnyEquivalenceEvaluator(
        evaluator_identifier="corpus_evaluator",
        evaluator=evaluate,
        ambiguity_threshold_fn=lambda _e: 0.8,
    )


@pytest.fixture
def engine():
    config = AlignmentConfiguration(AlignmentProfile.developer_debug_v1, _corpus_evaluator())
    return TraceAlignmentEngine(config)


def test_coding_agent_regression(engine):
    base, comp = DProvenanceCorpus.coding_agent_regression()
    result = engine.align(base, comp)
    assert len(result.alignments) == 5
    removed = [a for a in result.alignments if a.state.is_removed]
    assert len(removed) == 3
    assert result.regression_risk.level == RegressionLevel.HIGH


def test_semantic_evolution(engine):
    base, comp = DProvenanceCorpus.semantic_evolution()
    result = engine.align(base, comp)
    match = result.alignments[0]
    assert match.state.kind == AlignmentStateKind.SEMANTIC_MATCH
    assert match.state.strength > 0.9


def test_reordering(engine):
    base, comp = DProvenanceCorpus.reordering()
    result = engine.align(base, comp)
    reordered = [a for a in result.alignments if a.state.kind == AlignmentStateKind.REORDERED]
    assert len(reordered) == 2


def test_branch_collapse(engine):
    from dprovenancekit import TracePriority

    base, comp = DProvenanceCorpus.branch_collapse()
    result = engine.align(base, comp, minimum_priority=TracePriority.DIAGNOSTIC)
    removed = [a for a in result.alignments if a.state.is_removed]
    assert len(removed) == 1
    assert removed[0].base_event.payload.type_identifier == "planning"

# git-blob-rewrite
