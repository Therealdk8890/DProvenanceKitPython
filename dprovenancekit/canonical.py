"""The canonical, vendor-neutral event vocabulary.

Each live integration emits its framework's own event names (openai-agents
``function.start``, LangChain ``toolStarted``, …), and :mod:`dprovenancekit.otel_ingest`
normalizes OTLP spans to a third set. This module is the single source of truth for the
vendor-neutral vocabulary those all map onto, so that:

* a run recorded from openai-agents diffs against the *same* agent recorded from
  LangChain (they carry the same ``type_identifier`` stream), and
* one ruleset — e.g. the bundled ``agent.json`` — fires on runs from any of them.

Canonical emission is **opt-in** per integration (``canonical=True``); the native event
names remain the default so existing golden baselines and diffs are unaffected. When it
is enabled, an event's ``type_identifier`` becomes ``<kind>.<lifecycle>`` and the
original name is preserved in the ``native_type`` attribute.

The kind constants here MUST equal the ones :mod:`dprovenancekit.otel_ingest` produces
(``tests/test_canonical.py`` pins this), so ingested and live-recorded traces share one
vocabulary.
"""

from __future__ import annotations

from typing import Optional

# ── Canonical step kinds (must match otel_ingest's constants) ────────────────────
LLM_CALL = "llm_call"
TOOL_CALL = "tool_call"
AGENT_INVOCATION = "agent_invocation"
AGENT_CREATION = "agent_creation"
CHAIN = "chain"
RETRIEVAL = "retrieval"
EMBEDDING = "embedding"
RERANK = "rerank"
GUARDRAIL = "guardrail"

# ── Lifecycle suffixes ───────────────────────────────────────────────────────────
START = "start"
END = "end"
ERROR = "error"

# The attribute under which the pre-canonical event name is preserved.
NATIVE_TYPE_ATTR = "native_type"


def event_name(kind: str, lifecycle: str) -> str:
    """Assemble a canonical event identifier, e.g. ``("tool_call", "start")`` →
    ``"tool_call.start"``."""
    return f"{kind}.{lifecycle}"


# ── openai-agents: span kind → canonical kind ────────────────────────────────────
# openai-agents events are already ``<spanType>.<lifecycle>`` (e.g. ``function.end``);
# only the base needs mapping.
_OPENAI_AGENTS_KINDS = {
    "agent": AGENT_INVOCATION,
    "generation": LLM_CALL,
    "response": LLM_CALL,
    "function": TOOL_CALL,
    "handoff": AGENT_INVOCATION,
    "guardrail": GUARDRAIL,
    "speech": LLM_CALL,
    "transcription": LLM_CALL,
    "custom": CHAIN,
    "task": CHAIN,
}
_LIFECYCLES = {START, END, ERROR}


def canonicalize_openai_agents(type_name: str) -> Optional[str]:
    """Canonical name for an openai-agents ``<kind>.<lifecycle>`` event, or ``None`` if
    the kind is unrecognized (the caller keeps the native name)."""
    base, dot, lifecycle = type_name.rpartition(".")
    if not dot or lifecycle not in _LIFECYCLES:
        return None
    kind = _OPENAI_AGENTS_KINDS.get(base)
    if kind is None:
        return None
    return event_name(kind, lifecycle)


# ── LangChain: native event name → (canonical kind, lifecycle) ───────────────────
_LANGCHAIN_EVENTS = {
    "chainStarted": (CHAIN, START),
    "chainEnded": (CHAIN, END),
    "chainError": (CHAIN, ERROR),
    "llmStarted": (LLM_CALL, START),
    "llmEnded": (LLM_CALL, END),
    "llmError": (LLM_CALL, ERROR),
    "chatModelStarted": (LLM_CALL, START),
    "toolStarted": (TOOL_CALL, START),
    "toolEnded": (TOOL_CALL, END),
    "toolError": (TOOL_CALL, ERROR),
    "retrieverStarted": (RETRIEVAL, START),
    "retrieverEnded": (RETRIEVAL, END),
    "retrieverError": (RETRIEVAL, ERROR),
    "agentAction": (AGENT_INVOCATION, START),
    "agentFinish": (AGENT_INVOCATION, END),
}


def canonicalize_langchain(type_name: str) -> Optional[str]:
    """Canonical name for a LangChain event, or ``None`` if unrecognized (the caller
    keeps the native name — e.g. the ``text`` marker has no canonical kind)."""
    mapped = _LANGCHAIN_EVENTS.get(type_name)
    if mapped is None:
        return None
    return event_name(*mapped)


__all__ = [
    "LLM_CALL",
    "TOOL_CALL",
    "AGENT_INVOCATION",
    "AGENT_CREATION",
    "CHAIN",
    "RETRIEVAL",
    "EMBEDDING",
    "RERANK",
    "GUARDRAIL",
    "START",
    "END",
    "ERROR",
    "NATIVE_TYPE_ATTR",
    "event_name",
    "canonicalize_openai_agents",
    "canonicalize_langchain",
]
