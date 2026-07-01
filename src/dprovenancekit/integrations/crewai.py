"""CrewAI integration for DProvenanceKit.

Since CrewAI utilizes LangChain under the hood, we can leverage a tailored
LangChain callback handler that specifically listens for CrewAI's Agent 
and Task abstractions.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Callable
from uuid import UUID

try:
    from langchain_core.callbacks.base import BaseCallbackHandler
except ImportError:
    raise ImportError(
        "LangChain core is required for the CrewAI integration. "
        "Install it with: pip install dprovenancekit[crewai]"
    )

from dprovenancekit.kit import DProvenanceKit
from dprovenancekit.event import TraceableEvent


class CrewAITracer(BaseCallbackHandler):
    """A callback handler specifically designed to trace CrewAI agents and tasks.
    
    It maps CrewAI's execution graph into DProvenanceKit semantic engines,
    automatically segmenting traces by the specific Agent performing the task.
    """

    def __init__(
        self,
        kit: DProvenanceKit,
        start_event_factory: Callable[[str, str], TraceableEvent],
        end_event_factory: Callable[[str, str], TraceableEvent],
    ):
        self.kit = kit
        self.start_event_factory = start_event_factory
        self.end_event_factory = end_event_factory

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        # CrewAI often tags its chains with the Agent's role or name
        agent_name = "CrewAIAgent"
        if metadata and "agent_role" in metadata:
            agent_name = metadata["agent_role"]
            
        task_input = json.dumps(inputs)
        
        # We start an engine scope if possible, or just emit an event
        with self.kit.with_engine(agent_name):
            self.kit.record(self.start_event_factory(agent_name, task_input))

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        output_str = json.dumps(outputs)
        self.kit.record(self.end_event_factory("CrewAIAgent", output_str))
