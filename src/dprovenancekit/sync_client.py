"""Cloud Sync Client for DProvenanceKit.

Provides synchronization of local traces to a centralized SaaS backend.
"""

import json
import os
import urllib.request
import urllib.error
import uuid
from typing import Dict, Any, List

from .event import AnyTraceableEvent, TraceEvent
from .sqlite_store import SQLiteTraceStore
from .edge import TraceEdgeType


class CloudSyncClient:
    def __init__(self, api_url: str = None, api_key: str = None):
        self.api_url = (api_url or os.environ.get("DPROV_API_URL", "https://api.dprovenance.dev")).rstrip("/")
        self.api_key = api_key or os.environ.get("DPROV_API_KEY", "")

    def _request(self, method: str, endpoint: str, payload: dict = None) -> dict:
        url = f"{self.api_url}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        data = json.dumps(payload).encode("utf-8") if payload else None
        
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
            raise Exception(f"API Error {e.code}: {err_msg}")
        except Exception as e:
            raise Exception(f"Network Error: {e}")

    def push_run(self, run_id: uuid.UUID, store: SQLiteTraceStore) -> None:
        """Extract a run from the local store and push it to the remote backend."""
        run = store.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found in local store.")
            
        # Serialize events
        events_data = []
        for event in run.events:
            events_data.append({
                "id": str(event.id),
                "sequence": event.sequence,
                "timestamp": event.timestamp,
                "span_id": event.span_id,
                "parent_span_id": event.parent_span_id,
                "engine_name": event.engine_name,
                "payload": event.payload.to_dict()
            })
            
        # Extract edges directly via SQL since the store doesn't expose a run-level edge query
        edges_data = []
        try:
            rows = store.query(
                "SELECT source_id, target_id, edge_type FROM provenance_edges WHERE run_id = ?", 
                (str(run_id),)
            )
            for row in rows:
                edges_data.append({
                    "source_id": row[0],
                    "target_id": row[1],
                    "type": row[2]
                })
        except Exception:
            pass # Edges might not exist or schema might be different

        payload = {
            "run_id": str(run.run_id),
            "context_id": run.context_id,
            "start_time": run.events[0].timestamp if run.events else 0.0,
            "events": events_data,
            "edges": edges_data
        }
        
        self._request("POST", "/api/v1/sync/push", payload)

    def pull_run(self, run_id: uuid.UUID, store: SQLiteTraceStore) -> None:
        """Fetch a run from the remote backend and insert it into the local store."""
        resp = self._request("GET", f"/api/v1/sync/runs/{run_id}")
        
        # We need to construct TraceEvent instances and insert them
        for ev_data in resp.get("events", []):
            payload_data = ev_data["payload"]
            type_id = payload_data.get("type", payload_data.get("type_identifier_value", "unknown"))
            priority = payload_data.get("priority", payload_data.get("priority_value", 1))
            raw_json = json.dumps(payload_data)
            
            payload = AnyTraceableEvent(
                type_identifier_value=type_id,
                priority_value=priority,
                raw_json=raw_json
            )
            event = TraceEvent(
                id=uuid.UUID(ev_data["id"]),
                run_id=run_id,
                context_id=resp.get("context_id", ""),
                engine_name=ev_data["engine_name"],
                schema_version=1,
                sequence=ev_data["sequence"],
                span_id=ev_data.get("span_id"),
                parent_span_id=ev_data.get("parent_span_id"),
                payload=payload,
                timestamp=ev_data["timestamp"]
            )
            store.record(event)
            
        store.flush()
        
        # Link edges
        for edge_data in resp.get("edges", []):
            store.link(
                source=uuid.UUID(edge_data["source_id"]),
                target=uuid.UUID(edge_data["target_id"]),
                type=TraceEdgeType(edge_data["type"])
            )

# git-blob-rewrite
