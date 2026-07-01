"""FastAPI / Starlette integration for DProvenanceKit.

Provides a middleware that wraps every incoming HTTP request in a DProvenanceKit 
trace run, and injects the trace ID into the HTTP response headers.
"""

from __future__ import annotations

import typing
import uuid

try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response
except ImportError:
    raise ImportError(
        "Starlette/FastAPI is required for this integration. "
        "Install it with: pip install dprovenancekit[fastapi]"
    )

from dprovenancekit.kit import DProvenanceKit
from dprovenancekit.store import TraceStore


class DProvenanceMiddleware(BaseHTTPMiddleware):
    """ASGI middleware to trace HTTP requests.
    
    Automatically creates a `kit.run()` span for each incoming request, scoping any
    subsequent AI/LLM calls to this specific request's provenance timeline. 
    The resulting trace ID is returned to the client via an HTTP header.
    """

    def __init__(
        self, 
        app, 
        kit: DProvenanceKit, 
        store: TraceStore, 
        header_name: str = "X-DProvenance-Trace-Id"
    ):
        super().__init__(app)
        self.kit = kit
        self.store = store
        self.header_name = header_name

    async def dispatch(self, request: Request, call_next: typing.Callable) -> Response:
        context_id = f"{request.method} {request.url.path}"
        
        # We start the run scope. Any `kit.record()` or integration (like langchain)
        # called during this request will automatically attach to this run.
        with self.kit.run(context_id=context_id, store=self.store) as trace_run:
            response = await call_next(request)
            
            # Inject the trace ID so the API consumer can look up the provenance
            # of this specific execution.
            if self.header_name:
                response.headers[self.header_name] = str(trace_run.run_id)
                
            return response
