"""Tests for the hosted backend — wire compatibility + the regression-gate value."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

import pytest

from dprov_server import Project, Server
from dprov_server.storage import make_store
from dprov_server.tenancy import Tenancy

from dprovenancekit import CloudTraceStore, DProvenanceKit, TraceableEvent, TracePriority

API_KEY = "test-key"


def server() -> Server:
    return Server({API_KEY: Project("test")})


def call(srv, method, path, body=None, key=API_KEY):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    raw = json.dumps(body).encode() if body is not None else b""
    status, _h, out = srv.handle(method, path, headers, raw)
    try:
        return status, json.loads(out.decode())
    except Exception:
        return status, out


def ev(run_id, seq, typ, *, ctx="c", engine="E", priority=2, payload=None):
    return {
        "id": str(uuid.uuid4()), "run_id": run_id, "context_id": ctx,
        "priority": priority, "sequence": seq, "engine": engine,
        "span_id": None, "parent_span_id": None, "type": typ,
        "payload": payload if payload is not None else {"t": typ}, "timestamp": seq * 1000,
    }


# ── Wire surface ──────────────────────────────────────────────────────────────


def test_health_and_capabilities():
    srv = server()
    s, body = call(srv, "GET", "/api/health", key=None)
    assert s == 200 and body["schemaVersions"] == ["1.0"]
    s, body = call(srv, "GET", "/capabilities")
    assert s == 200 and "gate" in body["features"]


def test_auth_required():
    srv = server()
    assert call(srv, "GET", "/capabilities", key=None)[0] == 401
    assert call(srv, "GET", "/capabilities", key="bogus")[0] == 401


def test_ingest_then_list_and_detail():
    srv = server()
    rid = str(uuid.uuid4())
    s, body = call(srv, "POST", "/ingest", [
        ev(rid, 0, "retrieved", ctx="case-1"),
        ev(rid, 1, "decided", ctx="case-1", priority=3),
    ])
    assert s == 200 and body["accepted"] == 2

    s, body = call(srv, "GET", "/api/runs")
    assert s == 200 and len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["context_id"] == "case-1"
    assert run["steps"] == ["retrieved", "decided"]
    assert len(run["fingerprint"]) == 40  # sha1 hex

    s, detail = call(srv, "GET", f"/api/runs/{rid}")
    assert s == 200 and [e["type"] for e in detail["events"]] == ["retrieved", "decided"]


def test_malformed_batch_is_poison_400():
    srv = server()
    status, _h, _b = srv.handle("POST", "/ingest", {"Authorization": f"Bearer {API_KEY}"}, b"not json")
    assert status == 400  # tells the SDK to quarantine, not retry forever


def test_query_unsupported_schema():
    srv = server()
    s, body = call(srv, "POST", "/query", {"schemaVersion": "9.9", "dsl": {"type": "and", "nodes": []}})
    assert s == 422 and body["error"] == "UNSUPPORTED_SCHEMA"
    assert body["expected"] == "1.0" and body["received"] == "9.9"


def test_query_matches_via_wire_dsl():
    srv = server()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    call(srv, "POST", "/ingest", [ev(a, 0, "conflictDetected", ctx="run-a"), ev(a, 1, "finalDecisionMade", ctx="run-a")])
    call(srv, "POST", "/ingest", [ev(b, 0, "documentEvaluated", ctx="run-b"), ev(b, 1, "conflictDetected", ctx="run-b")])

    # conflictDetected AND no documentEvaluated -> only run-a
    dsl = {"type": "and", "nodes": [
        {"type": "containsStep", "step": "conflictDetected"},
        {"type": "missingStep", "step": "documentEvaluated"},
    ]}
    s, body = call(srv, "POST", "/query", {"schemaVersion": "1.0", "dsl": dsl})
    assert s == 200
    assert sorted(r["context_id"] for r in body["runs"]) == ["run-a"]


# ── The regression gate (the paid layer) ────────────────────────────────────────


def test_gate_catches_skipped_critical_step():
    srv = server()
    golden, cand = str(uuid.uuid4()), str(uuid.uuid4())
    # golden: retrieve -> verify(CRITICAL) -> decide(CRITICAL)
    call(srv, "POST", "/ingest", [
        ev(golden, 0, "retrieved"),
        ev(golden, 1, "verified", priority=3),
        ev(golden, 2, "decided", priority=3),
    ])
    # candidate skips verification
    call(srv, "POST", "/ingest", [
        ev(cand, 0, "retrieved"),
        ev(cand, 1, "decided", priority=3),
    ])

    s, report = call(srv, "POST", "/api/gate", {"golden_run_id": golden, "candidate_run_id": cand})
    assert s == 200
    assert report["passed"] is False
    assert report["regression_level"] == "high"
    assert "verified" in report["removed_steps"]
    assert report["fingerprint_match"] is False


def test_gate_passes_identical_runs():
    srv = server()
    g, c = str(uuid.uuid4()), str(uuid.uuid4())
    steps = [("retrieved", 2), ("verified", 3), ("decided", 3)]
    for rid in (g, c):
        call(srv, "POST", "/ingest", [ev(rid, i, t, priority=p) for i, (t, p) in enumerate(steps)])
    s, report = call(srv, "POST", "/api/gate", {"golden_run_id": g, "candidate_run_id": c})
    assert s == 200 and report["passed"] is True
    assert report["regression_level"] == "none"
    assert report["fingerprint_match"] is True


def test_gate_lenient_policy_tolerates_added_step():
    srv = server()
    g, c = str(uuid.uuid4()), str(uuid.uuid4())
    call(srv, "POST", "/ingest", [ev(g, 0, "retrieved"), ev(g, 1, "decided", priority=3)])
    call(srv, "POST", "/ingest", [ev(c, 0, "retrieved"), ev(c, 1, "extra"), ev(c, 2, "decided", priority=3)])

    strict = call(srv, "POST", "/api/gate", {"golden_run_id": g, "candidate_run_id": c})[1]
    assert strict["passed"] is False and "extra" in strict["added_steps"]

    lenient = call(srv, "POST", "/api/gate",
                   {"golden_run_id": g, "candidate_run_id": c, "allow_divergent_steps": True})[1]
    assert lenient["passed"] is True


def test_gate_missing_run_is_404():
    srv = server()
    s, body = call(srv, "POST", "/api/gate",
                   {"golden_run_id": str(uuid.uuid4()), "candidate_run_id": str(uuid.uuid4())})
    assert s == 404 and body["error"] == "RUN_NOT_FOUND"


# ── Durable (SQLite) storage ────────────────────────────────────────────────────


def test_sqlite_storage_persists_across_restart(tmp_path):
    data_dir = str(tmp_path)

    def fresh_server():  # a new process would build the store the same way
        return Server({API_KEY: Project("proj", make_store("proj", storage="sqlite", data_dir=data_dir))})

    g, c = str(uuid.uuid4()), str(uuid.uuid4())
    srv = fresh_server()
    call(srv, "POST", "/ingest", [ev(g, 0, "retrieved"), ev(g, 1, "verified", priority=3), ev(g, 2, "decided", priority=3)])
    call(srv, "POST", "/ingest", [ev(c, 0, "retrieved"), ev(c, 1, "decided", priority=3)])

    # A brand-new server over the same data dir still sees the runs — durability.
    srv2 = fresh_server()
    s, body = call(srv2, "GET", "/api/runs")
    assert s == 200 and len(body["runs"]) == 2

    # And the regression gate works across the "restart".
    s, rep = call(srv2, "POST", "/api/gate", {"golden_run_id": g, "candidate_run_id": c})
    assert s == 200 and rep["passed"] is False and "verified" in rep["removed_steps"]


# ── Multi-tenant auth (projects + hashed keys) ───────────────────────────────────


def test_tenancy_create_resolve_revoke(tmp_path):
    t = Tenancy(str(tmp_path / "tenants.sqlite"))
    pid = t.create_project("Team A")
    key = t.create_api_key(pid, name="ci")
    assert key.startswith("dpk_")
    assert t.resolve(key) == (pid, "Team A")
    assert t.resolve("dpk_wrong") is None
    # keys are stored hashed, never in the clear
    assert key not in open(str(tmp_path / "tenants.sqlite"), "rb").read().decode("latin-1")
    assert t.revoke(key) is True
    assert t.resolve(key) is None


def test_server_tenancy_mode_auth(tmp_path):
    t = Tenancy(str(tmp_path / "tenants.sqlite"))
    pid = t.create_project("acme")
    key = t.create_api_key(pid)
    srv = Server(tenancy=t)

    # a valid key authenticates and gets an isolated project store
    rid = str(uuid.uuid4())
    s, body = call(srv, "POST", "/ingest", [ev(rid, 0, "step")], key=key)
    assert s == 200 and body["accepted"] == 1
    s, body = call(srv, "GET", "/api/runs", key=key)
    assert s == 200 and len(body["runs"]) == 1

    # unknown and revoked keys are rejected
    assert call(srv, "GET", "/api/runs", key="dpk_nope")[0] == 401
    t.revoke(key)
    assert call(srv, "GET", "/api/runs", key=key)[0] == 401


# ── CI gate CLI (dprov_gate) ─────────────────────────────────────────────────────


def _gate_transport(srv):
    def request_fn(method, url, headers, body):
        status, _h, out = srv.handle(method, urlsplit(url).path, headers, body or b"")
        return status, out
    return request_fn


def test_gate_cli_exit_codes():
    import dprov_gate

    srv = server()
    g, c = str(uuid.uuid4()), str(uuid.uuid4())
    call(srv, "POST", "/ingest", [ev(g, 0, "retrieved"), ev(g, 1, "verified", priority=3), ev(g, 2, "decided", priority=3)])
    call(srv, "POST", "/ingest", [ev(c, 0, "retrieved"), ev(c, 1, "decided", priority=3)])
    rf = _gate_transport(srv)

    base = ["--url", "http://x", "--key", API_KEY]
    # regression -> exit 1
    assert dprov_gate.main(base + ["--golden", g, "--candidate", c], request_fn=rf) == 1
    # identical -> exit 0
    assert dprov_gate.main(base + ["--golden", g, "--candidate", g], request_fn=rf) == 0
    # fully lenient (tolerate divergences AND raise the severity ceiling) lets it pass
    assert dprov_gate.main(base + ["--golden", g, "--candidate", c, "--allow-divergent", "--max-level", "high"], request_fn=rf) == 0
    # missing creds -> exit 2
    assert dprov_gate.main(["--url", "", "--key", "", "--golden", g, "--candidate", c], request_fn=rf) == 2


# ── End-to-end through the real CloudTraceStore SDK (no sockets) ─────────────────


@dataclass(frozen=True)
class DemoEvent(TraceableEvent):
    kind: str

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        return TracePriority.CRITICAL if self.kind == "decided" else TracePriority.STRUCTURAL

    def to_dict(self) -> dict:
        return {"kind": self.kind}

    @classmethod
    def from_dict(cls, data: dict) -> "DemoEvent":
        return cls(kind=data["kind"])


def test_end_to_end_with_cloud_sdk():
    srv = server()

    def transport(method, url, headers, body):
        status, _h, out = srv.handle(method, urlsplit(url).path, headers, body or b"")
        return status, out

    store = CloudTraceStore(DemoEvent, "http://backend", API_KEY, transport=transport)
    kit = DProvenanceKit(DemoEvent)
    with kit.run(context_id="ticket-7", store=store):
        with kit.with_engine("Retriever"):
            kit.record(DemoEvent("retrieved"))
        with kit.with_engine("Decider"):
            kit.record(DemoEvent("decided"))
    store.flush(timeout=5)

    # negotiate_capabilities round-trips against the real server
    store.negotiate_capabilities()

    # the server received and stored the run
    s, body = call(srv, "GET", "/api/runs")
    assert s == 200 and len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["context_id"] == "ticket-7"
    assert run["steps"] == ["retrieved", "decided"]
    assert run["engines"] == ["Decider", "Retriever"]
