"""DProvenanceKit hosted backend — the managed service the ``CloudTraceStore`` SDK targets.

The open-source library (Apache 2.0) is the client; this is the service. It speaks the Trace
Specification v1 cloud wire format (§7) so the existing ``CloudTraceStore`` works against it
unchanged — ``POST /ingest``, ``POST /query``, ``GET /capabilities`` — and adds the
monetizable layer on top: a **regression gate** API (``POST /api/gate``) and the data a
viewer renders — run list/detail (``GET /api/runs``, ``GET /api/runs/{id}``), the reconstructed
span tree + payloads (``GET /api/runs/{id}/replay``), and a span-aware semantic diff of two
runs (``POST /api/diff``).

It is generic over any consumer payload: ingested events are stored type-erased as
``AnyTraceableEvent`` (carrying ``type``, ``priority``, and the canonical payload JSON),
which is all the query engine, the run fingerprint, and exact-equality alignment need. The
whole reasoning layer — query DSL, fingerprint, alignment, the regression gate — is reused
verbatim from the library, so the service and the SDK can never drift.

The HTTP core is a single pure ``Server.handle(method, path, headers, body) -> (status,
headers, body)`` so it can be driven both by a real socket server (``http_app.py``) and, in
tests, directly by the SDK's pluggable ``transport`` — no sockets required.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Make the library importable when run from a checkout (src/ layout).
_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from dprovenancekit import (  # noqa: E402
    AnyTraceableEvent,
    InMemoryTraceStore,
    RegressionGate,
    RegressionLevel,
    SnapshotDiffEngine,
    TraceEvent,
    TraceQueryDSL,
    TraceReplayEngine,
    run_fingerprint,
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
)

from .billing import parse_price_plans, plan_for_event, verify_signature  # noqa: E402
from .storage import ALL_RUNS, fetch_run, flush as flush_store, make_store  # noqa: E402

SCHEMA_VERSION = "1.0"
_DASHBOARD = os.path.join(os.path.dirname(__file__), "dashboard.html")


# ── Query wire-form deserializer (inverse of the SDK's _serialize_node) ─────────


def node_from_wire(node: Dict[str, Any]):
    kind = node["type"]
    if kind == "and":
        return AndNode(nodes=tuple(node_from_wire(n) for n in node["nodes"]))
    if kind == "or":
        return OrNode(nodes=tuple(node_from_wire(n) for n in node["nodes"]))
    if kind == "not":
        return NotNode(node=node_from_wire(node["node"]))
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


# ── Per-project state ────────────────────────────────────────────────────────────


# Role hierarchy and per-plan quotas (quotas enforced only in tenancy mode).
_ROLE_RANK = {"read": 1, "write": 2, "admin": 3}
PLAN_LIMITS = {
    "free": {"events": 10_000, "gate_calls": 500},
    "pro": {"events": 10_000_000, "gate_calls": 100_000},
}


@dataclass
class Project:
    name: str
    store: Any = field(default_factory=InMemoryTraceStore)
    role: str = "admin"          # static/dev keys are unrestricted; tenancy sets the real role
    id: Optional[str] = None     # set in tenancy mode; enables usage metering + quotas


def make_project(name: str) -> Project:
    """A project with a store built from the configured backend (memory or sqlite)."""
    return Project(name=name, store=make_store(name))


def _load_api_keys() -> Dict[str, Project]:
    """``DPROV_API_KEYS="key1:projectA,key2:projectB"``; defaults to a demo key."""
    raw = os.environ.get("DPROV_API_KEYS", "demo-key:demo")
    keys: Dict[str, Project] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, name = pair.partition(":")
        keys[key.strip()] = make_project(name.strip() or "default")
    return keys


class HTTPError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body


# ── The server ─────────────────────────────────────────────────────────────────


class Server:
    """Pure request handler. Reuses the library for all reasoning; no sockets here.

    Auth has two modes:
      * **static** — an explicit ``{api_key: Project}`` map (tests, or ``DPROV_API_KEYS``).
      * **tenancy** — a :class:`~dprov_server.tenancy.Tenancy` (durable projects + hashed
        keys); each project's store is built lazily and cached.
    """

    def __init__(self, projects: Optional[Dict[str, Project]] = None, tenancy=None,
                 stripe_secret: Optional[str] = None, price_plans: Optional[Dict[str, str]] = None):
        self._static: Optional[Dict[str, Project]] = None
        self.tenancy = None
        self._stores: Dict[str, Any] = {}
        self._stripe_secret = (
            stripe_secret if stripe_secret is not None else os.environ.get("DPROV_STRIPE_WEBHOOK_SECRET")
        )
        self._price_plans = (
            price_plans if price_plans is not None else parse_price_plans(os.environ.get("DPROV_STRIPE_PRICE_PLANS"))
        )
        if projects is not None:
            self._static = projects
        elif tenancy is not None:
            self.tenancy = tenancy
        elif os.environ.get("DPROV_API_KEYS"):
            self._static = _load_api_keys()
        else:
            from .tenancy import Tenancy

            self.tenancy = Tenancy.default()

    def describe(self) -> str:
        if self._static is not None:
            names = sorted({p.name for p in self._static.values()})
            return f"static auth · projects: {', '.join(names) or '—'}"
        return f"tenancy auth · {len(self.tenancy.list_projects())} project(s)"

    def _store_for(self, project_id: str):
        store = self._stores.get(project_id)
        if store is None:
            store = make_store(project_id)
            self._stores[project_id] = store
        return store

    # -- routing -----------------------------------------------------------------

    def handle(self, method: str, path: str, headers: Dict[str, str], body: bytes) -> Tuple[int, Dict[str, str], bytes]:
        path = path.split("?", 1)[0].rstrip("/") or "/"
        try:
            # Public routes (no auth).
            if method == "GET" and path == "/":
                return self._html(self._dashboard())
            if method == "GET" and path == "/api/health":
                return self._json(200, {"status": "ok", "schemaVersions": [SCHEMA_VERSION]})
            if method == "POST" and path == "/webhooks/stripe":
                return self._stripe_webhook(headers, body)  # authed by signature, not Bearer

            # Everything else is authenticated and scoped to a project.
            project = self._auth(headers)

            if method == "GET" and path == "/capabilities":
                self._require(project, "read")
                return self._json(200, {"schemaVersions": [SCHEMA_VERSION], "features": ["ingest", "query", "gate"]})
            if method == "POST" and path == "/ingest":
                self._require(project, "write")
                return self._ingest(project, body)
            if method == "POST" and path == "/query":
                self._require(project, "read")
                return self._query(project, body)
            if method == "GET" and path == "/api/runs":
                self._require(project, "read")
                return self._list_runs(project)
            if method == "GET" and path.startswith("/api/runs/"):
                self._require(project, "read")
                parts = path[len("/api/runs/"):].split("/")
                if len(parts) == 1 and parts[0]:
                    return self._run_detail(project, parts[0])
                if len(parts) == 2 and parts[1] == "replay":
                    return self._run_replay(project, parts[0])
                return self._json(404, {"error": "NOT_FOUND", "path": path})
            if method == "POST" and path == "/api/diff":
                self._require(project, "read")
                return self._diff(project, body)
            if method == "POST" and path == "/api/gate":
                self._require(project, "read")
                return self._gate(project, body)
            if method == "GET" and path == "/api/usage":
                self._require(project, "read")
                return self._usage(project)

            return self._json(404, {"error": "NOT_FOUND", "path": path})
        except HTTPError as e:
            return self._json(e.status, e.body if isinstance(e.body, dict) else {"error": str(e.body)})
        except Exception as e:  # noqa: BLE001 - never leak a stack trace to the wire
            return self._json(500, {"error": "INTERNAL", "detail": str(e)})

    # -- auth --------------------------------------------------------------------

    def _auth(self, headers: Dict[str, str]) -> Project:
        auth = _header(headers, "authorization")
        if not auth or not auth.startswith("Bearer "):
            raise HTTPError(401, {"error": "MISSING_BEARER"})
        key = auth[len("Bearer "):].strip()
        if self._static is not None:
            project = self._static.get(key)
            if project is None:
                raise HTTPError(401, {"error": "INVALID_API_KEY"})
            return project
        resolved = self.tenancy.resolve(key)
        if resolved is None:
            raise HTTPError(401, {"error": "INVALID_API_KEY"})
        project_id, name, role = resolved
        return Project(name=name, store=self._store_for(project_id), role=role, id=project_id)

    def _require(self, project: Project, needed: str) -> None:
        if _ROLE_RANK.get(project.role, 0) < _ROLE_RANK[needed]:
            raise HTTPError(403, {"error": "FORBIDDEN", "need": needed, "have": project.role})

    # -- billing webhook ---------------------------------------------------------

    def _stripe_webhook(self, headers: Dict[str, str], body: bytes) -> Tuple[int, Dict[str, str], bytes]:
        if not self._stripe_secret:
            raise HTTPError(503, {"error": "BILLING_NOT_CONFIGURED"})
        sig = _header(headers, "stripe-signature") or ""
        if not verify_signature(body, sig, self._stripe_secret):
            raise HTTPError(400, {"error": "BAD_SIGNATURE"})
        try:
            event = json.loads(body.decode("utf-8"))
        except Exception:
            raise HTTPError(400, {"error": "BAD_PAYLOAD"})
        mapping = plan_for_event(event, self._price_plans)
        if mapping is None or self.tenancy is None:
            return self._json(200, {"received": True, "ignored": True})
        project_id, plan = mapping
        updated = self.tenancy.set_plan(project_id, plan)
        return self._json(200, {"received": True, "project": project_id, "plan": plan, "updated": updated})

    # -- usage metering + quotas (tenancy mode only) -----------------------------

    def _metered(self, project: Project) -> bool:
        return self.tenancy is not None and project.id is not None

    def _check_quota(self, project: Project, *, events: int = 0, gate_calls: int = 0) -> None:
        if not self._metered(project):
            return
        plan = self.tenancy.get_plan(project.id) or "free"
        limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
        used_events, used_gate = self.tenancy.get_usage(project.id)
        if events and used_events + events > limits["events"]:
            raise HTTPError(429, {"error": "QUOTA_EXCEEDED", "resource": "events",
                                  "plan": plan, "limit": limits["events"], "used": used_events})
        if gate_calls and used_gate + gate_calls > limits["gate_calls"]:
            raise HTTPError(429, {"error": "QUOTA_EXCEEDED", "resource": "gate_calls",
                                  "plan": plan, "limit": limits["gate_calls"], "used": used_gate})

    def _meter(self, project: Project, *, events: int = 0, gate_calls: int = 0) -> None:
        if self._metered(project):
            self.tenancy.record_usage(project.id, events=events, gate_calls=gate_calls)

    def _usage(self, project: Project) -> Tuple[int, Dict[str, str], bytes]:
        if not self._metered(project):
            return self._json(200, {"metered": False})
        plan = self.tenancy.get_plan(project.id) or "free"
        used_events, used_gate = self.tenancy.get_usage(project.id)
        return self._json(200, {
            "metered": True,
            "plan": plan,
            "usage": {"events": used_events, "gate_calls": used_gate},
            "limits": PLAN_LIMITS.get(plan, PLAN_LIMITS["free"]),
        })

    # -- ingest (§7) -------------------------------------------------------------

    def _ingest(self, project: Project, body: bytes) -> Tuple[int, Dict[str, str], bytes]:
        try:
            events = json.loads(body.decode("utf-8"))
            assert isinstance(events, list)
        except Exception:
            # A malformed batch is a "poison batch": 400 tells the SDK to quarantine it.
            raise HTTPError(400, {"error": "BAD_BATCH"})
        self._check_quota(project, events=len(events))
        accepted = 0
        for ev in events:
            try:
                project.store.record(_event_from_wire(ev))
                accepted += 1
            except Exception:
                raise HTTPError(400, {"error": "BAD_EVENT", "accepted": accepted})
        flush_store(project.store)  # persist the batch (no-op for the in-memory backend)
        self._meter(project, events=accepted)
        return self._json(200, {"accepted": accepted})

    # -- query (§7): the SDK only checks status; the body powers the dashboard ---

    def _query(self, project: Project, body: bytes) -> Tuple[int, Dict[str, str], bytes]:
        req = json.loads(body.decode("utf-8")) if body else {}
        received = str(req.get("schemaVersion", ""))
        if received != SCHEMA_VERSION:
            raise HTTPError(422, {"error": "UNSUPPORTED_SCHEMA", "expected": SCHEMA_VERSION, "received": received})
        dsl = TraceQueryDSL(_root=node_from_wire(req["dsl"])) if req.get("dsl") else ALL_RUNS
        runs = project.store.query_runs(dsl)
        limit = int(req.get("limit", 100))
        return self._json(200, {"runs": [_run_summary(r) for r in runs[:limit]]})

    # -- dashboard data ----------------------------------------------------------

    def _list_runs(self, project: Project) -> Tuple[int, Dict[str, str], bytes]:
        runs = project.store.query_runs(ALL_RUNS)
        runs = sorted(runs, key=lambda r: r.events[0].timestamp if r.events else 0, reverse=True)
        return self._json(200, {"project": project.name, "runs": [_run_summary(r) for r in runs]})

    def _run_detail(self, project: Project, run_id: str) -> Tuple[int, Dict[str, str], bytes]:
        run = _get_run(project, run_id)
        return self._json(200, {
            **_run_summary(run),
            "events": [
                {
                    "sequence": e.sequence,
                    "type": e.payload.type_identifier,
                    "engine": e.engine_name,
                    "priority": int(e.payload.priority),
                    "span_id": e.span_id,
                    "parent_span_id": e.parent_span_id,
                }
                for e in run.events
            ],
        })

    def _run_replay(self, project: Project, run_id: str) -> Tuple[int, Dict[str, str], bytes]:
        """The reconstructed span tree + full event payloads — the data a viewer renders
        (span-tree pane + JSON payload inspector), which ``_run_detail`` deliberately omits."""
        run = _get_run(project, run_id)
        snapshot = TraceReplayEngine(run.events).snapshot()
        return self._json(200, {**_run_summary(run), "snapshot": _snapshot_json(snapshot)})

    def _diff(self, project: Project, body: bytes) -> Tuple[int, Dict[str, str], bytes]:
        """Span-aware semantic diff of two runs (``SnapshotDiffEngine``) — span add/remove/
        reparent/contamination, per-event add/remove/modify, and first divergence."""
        req = json.loads(body.decode("utf-8")) if body else {}
        base = _get_run(project, str(req.get("base_run_id", "")))
        comparison = _get_run(project, str(req.get("comparison_run_id", "")))
        base_snap = TraceReplayEngine(base.events).snapshot()
        comp_snap = TraceReplayEngine(comparison.events).snapshot()
        result = SnapshotDiffEngine().diff(base_snap, comp_snap)
        s = result.summary
        return self._json(200, {
            "base_run_id": str(base.run_id),
            "comparison_run_id": str(comparison.run_id),
            "is_identical": result.is_identical,
            "summary": {
                "added_spans": s.added_spans,
                "removed_spans": s.removed_spans,
                "added_events": s.added_events,
                "removed_events": s.removed_events,
                "modified_events": s.modified_events,
                "contaminated_spans": s.contaminated_spans,
                "divergence_points": s.divergence_points,
            },
            "span_changes": [_span_change_json(c) for c in result.span_changes],
            "event_changes": [_event_change_json(c) for c in result.event_changes],
            "divergences": [_divergence_json(d) for d in result.divergences],
        })

    # -- the regression gate: the paid differentiator ----------------------------

    def _gate(self, project: Project, body: bytes) -> Tuple[int, Dict[str, str], bytes]:
        req = json.loads(body.decode("utf-8")) if body else {}
        self._check_quota(project, gate_calls=1)
        golden = _get_run(project, str(req.get("golden_run_id", "")))
        candidate = _get_run(project, str(req.get("candidate_run_id", "")))
        gate = RegressionGate(
            max_regression_level=RegressionLevel(req.get("max_regression_level", "none")),
            allow_divergent_steps=bool(req.get("allow_divergent_steps", False)),
        )
        report = gate.check(golden, candidate)
        self._meter(project, gate_calls=1)
        return self._json(200, {
            "passed": report.passed,
            "regression_level": report.regression_level.value,
            "strength": report.strength,
            "fingerprint_match": report.fingerprint_match,
            "golden_fingerprint": report.golden_fingerprint,
            "candidate_fingerprint": report.candidate_fingerprint,
            "removed_steps": report.removed_steps,
            "added_steps": report.added_steps,
            "divergent_steps": report.divergent_steps,
            "steps_by_change": report.steps_by_change,
            "summary": report.summary(),
        })

    # -- response helpers --------------------------------------------------------

    def _json(self, status: int, obj: Any) -> Tuple[int, Dict[str, str], bytes]:
        return status, {"Content-Type": "application/json"}, json.dumps(obj).encode("utf-8")

    def _html(self, html: str) -> Tuple[int, Dict[str, str], bytes]:
        return 200, {"Content-Type": "text/html; charset=utf-8"}, html.encode("utf-8")

    def _dashboard(self) -> str:
        try:
            with open(_DASHBOARD, encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return "<h1>DProvenanceKit</h1><p>dashboard.html not found.</p>"


# ── helpers ──────────────────────────────────────────────────────────────────────


def _header(headers: Dict[str, str], name: str) -> Optional[str]:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def _event_from_wire(ev: Dict[str, Any]) -> TraceEvent:
    payload = ev.get("payload")
    raw_json = json.dumps(payload, sort_keys=True) if not isinstance(payload, str) else payload
    return TraceEvent(
        run_id=uuid.UUID(str(ev["run_id"])),
        context_id=str(ev.get("context_id", "")),
        engine_name=str(ev.get("engine") or "Unknown"),
        schema_version=1,
        sequence=int(ev.get("sequence", 0)),
        span_id=ev.get("span_id"),
        parent_span_id=ev.get("parent_span_id"),
        payload=AnyTraceableEvent(
            type_identifier_value=str(ev["type"]),
            priority_value=int(ev.get("priority", 3)),
            raw_json=raw_json,
        ),
        id=uuid.UUID(str(ev["id"])) if ev.get("id") else uuid.uuid4(),
        timestamp=float(ev.get("timestamp", 0)) / 1_000_000.0,
    )


def _run_summary(run) -> Dict[str, Any]:
    types: List[str] = [e.payload.type_identifier for e in run.events]
    return {
        "run_id": str(run.run_id),
        "context_id": run.context_id,
        "event_count": len(run.events),
        "fingerprint": run_fingerprint(run),
        "steps": types,
        "engines": sorted({e.engine_name for e in run.events}),
    }


def _get_run(project: Project, run_id: str):
    try:
        run = fetch_run(project.store, uuid.UUID(run_id))
    except (ValueError, AttributeError):
        run = None
    if run is None:
        raise HTTPError(404, {"error": "RUN_NOT_FOUND", "run_id": run_id})
    return run


# ── replay / diff serialization (the visualizer data contract) ────────────────────


def _payload_json(payload) -> Any:
    """The event's payload for the JSON inspector. Prefer the original raw JSON carried by
    ``AnyTraceableEvent`` (what was actually recorded); fall back to a concrete event's dict."""
    raw = getattr(payload, "raw_json", None)
    if raw is not None:
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw
    try:
        return payload.to_dict()
    except Exception:  # noqa: BLE001 - a payload that refuses to serialize is non-fatal
        return None


def _replay_event_json(re) -> Dict[str, Any]:
    e = re.event
    return {
        "source": re.source.value,
        "replay_order": re.replay_order,
        "sequence": e.sequence,
        "type": e.payload.type_identifier,
        "engine": e.engine_name,
        "priority": int(e.payload.priority),
        "span_id": e.span_id,
        "parent_span_id": e.parent_span_id,
        "timestamp": e.timestamp,
        "payload": _payload_json(e.payload),
    }


def _span_node_json(node) -> Dict[str, Any]:
    return {
        "span_id": node.span_id,
        "start_sequence": node.start_sequence,
        "end_sequence": node.end_sequence,
        "contains_quarantined_events": node.contains_quarantined_events,
        "events": [_replay_event_json(re) for re in node.events],
        "children": [_span_node_json(c) for c in node.children],
    }


def _snapshot_json(snapshot) -> Dict[str, Any]:
    m = snapshot.manifest
    md = snapshot.metadata
    return {
        "roots": [_span_node_json(r) for r in snapshot.roots],
        "orphaned_events": [_replay_event_json(re) for re in snapshot.orphaned_events],
        "manifest": {
            "total_events": m.total_events,
            "committed_events": m.committed_events,
            "quarantined_events": m.quarantined_events,
            "orphaned_events": m.orphaned_events,
            "duplicate_event_ids": m.duplicate_event_ids,
            "reconstructed_spans": m.reconstructed_spans,
            "contaminated_spans": m.contaminated_spans,
            "sequence_gaps": [
                {"lower_bound": g.lower_bound, "upper_bound": g.upper_bound} for g in m.sequence_gaps
            ],
        },
        "metadata": {
            "generated_at": md.generated_at,
            "max_sequence_included": md.max_sequence_included,
            "source_counts": {src.value: cnt for src, cnt in md.source_counts.items()},
        },
    }


def _span_change_json(c) -> Dict[str, Any]:
    return {
        "kind": c.kind.value,
        "span_id": c.span_id,
        "parent_span_id": c.parent_span_id,
        "from_parent": c.from_parent,
        "to_parent": c.to_parent,
        "from_contaminated": c.from_contaminated,
        "to_contaminated": c.to_contaminated,
    }


def _event_change_json(c) -> Dict[str, Any]:
    return {
        "kind": c.kind.value,
        "span_id": c.span_id,
        "event": _replay_event_json(c.event) if c.event is not None else None,
        "before": _replay_event_json(c.before) if c.before is not None else None,
        "after": _replay_event_json(c.after) if c.after is not None else None,
    }


def _divergence_json(d) -> Dict[str, Any]:
    return {
        "span_id": d.span_id,
        "common_prefix_length": d.common_prefix_length,
        "divergence_sequence": d.divergence_sequence,
        "left_event": _replay_event_json(d.left_event) if d.left_event is not None else None,
        "right_event": _replay_event_json(d.right_event) if d.right_event is not None else None,
    }
