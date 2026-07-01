"""Model Context Protocol (MCP) integration for DProvenanceKit.

Provides decorators and middleware to seamlessly trace MCP server tool 
executions into a DProvenanceKit timeline.
"""

from __future__ import annotations

import functools
import inspect
from typing import Callable, Any, Dict

from dprovenancekit.kit import DProvenanceKit
from dprovenancekit.event import TraceableEvent


def traced_mcp_tool(
    kit: DProvenanceKit,
    start_event_factory: Callable[[str, Dict[str, Any]], TraceableEvent],
    end_event_factory: Callable[[str, Any], TraceableEvent],
    error_event_factory: Callable[[str, Exception], TraceableEvent],
):
    """Decorator to automatically trace MCP tool executions.
    
    This wraps an asynchronous tool function (like those decorated with 
    `@server.tool()` in the official MCP Python SDK) and emits events to 
    an active trace run.
    
    Args:
        kit: The DProvenanceKit instance.
        start_event_factory: A callable that takes (tool_name, arguments_dict) and 
            returns a TraceableEvent to record before execution.
        end_event_factory: A callable that takes (tool_name, result) and returns a 
            TraceableEvent to record after successful execution.
        error_event_factory: A callable that takes (tool_name, exception) and 
            returns a TraceableEvent to record on failure.
            
    Usage:
        @server.tool()
        @traced_mcp_tool(
            kit=kit,
            start_event_factory=lambda n, args: MyEvent.tool_start(n, json.dumps(args)),
            end_event_factory=lambda n, res: MyEvent.tool_end(n, str(res)),
            error_event_factory=lambda n, err: MyEvent.tool_error(n, str(err))
        )
        async def fetch_weather(location: str):
            return "Sunny"
    """

    def decorator(func: Callable):
        tool_name = func.__name__

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            # Record start
            bound_args = inspect.signature(func).bind(*args, **kwargs)
            bound_args.apply_defaults()
            kit.record(start_event_factory(tool_name, bound_args.arguments))
            
            try:
                result = await func(*args, **kwargs)
                kit.record(end_event_factory(tool_name, result))
                return result
            except Exception as e:
                kit.record(error_event_factory(tool_name, e))
                raise

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            bound_args = inspect.signature(func).bind(*args, **kwargs)
            bound_args.apply_defaults()
            kit.record(start_event_factory(tool_name, bound_args.arguments))
            
            try:
                result = func(*args, **kwargs)
                kit.record(end_event_factory(tool_name, result))
                return result
            except Exception as e:
                kit.record(error_event_factory(tool_name, e))
                raise

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
