"""Tests for the visualizer data endpoints: GET /api/runs/{id}/replay and POST /api/diff."""

from __future__ import annotations

import json
import uuid

from dprov_server import Project, Server

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


def ev(run_id, seq, typ, *, ctx="c", engine="E", priority=2, span_id=None, parent=None, payload=None):
    return {
        "id": str(uuid.uuid4()), "run_id": run_id, "context_id": ctx,
        "priority": priority, "sequence": seq, "engine": engine,
        "span_id": span_id, "parent_span_id": parent, "type": typ,
        "payload": payload if payload is not None else {"t": typ}, "timestamp": seq * 1000,
    }


# ── replay ───────────────────────────────────────────────────────────────────────


def test_replay_returns_span_tree_and_payloads():
    srv = server()
    rid = str(uuid.uuid4())
    call(srv, "POST", "/ingest", [
        ev(rid, 0, "root", span_id="s_root"),
        ev(rid, 1, "child", span_id="s_child", parent="s_root", payload={"detail": "hello"}),
    ])

    s, body = call(srv, "GET", f"/api/runs/{rid}/replay")
    assert s == 200
    assert body["run_id"] == rid

    snap = body["snapshot"]
    assert len(snap["roots"]) == 1
    root = snap["roots"][0]
    assert root["span_id"] == "s_root"
    assert len(root["children"]) == 1

    child = root["children"][0]
    assert child["span_id"] == "s_child"
    # The JSON inspector gets the actual recorded payload (not just the type).
    assert child["events"][0]["payload"] == {"detail": "hello"}
    assert snap["manifest"]["reconstructed_spans"] == 2
    assert snap["manifest"]["total_events"] == 2


def test_replay_missing_run_is_404():
    srv = server()
    s, body = call(srv, "GET", f"/api/runs/{uuid.uuid4()}/replay")
    assert s == 404 and body["error"] == "RUN_NOT_FOUND"


def test_run_detail_still_works_alongside_replay_subpath():
    # The new sub-path routing must not break GET /api/runs/{id}.
    srv = server()
    rid = str(uuid.uuid4())
    call(srv, "POST", "/ingest", [ev(rid, 0, "a"), ev(rid, 1, "b")])
    s, detail = call(srv, "GET", f"/api/runs/{rid}")
    assert s == 200 and [e["type"] for e in detail["events"]] == ["a", "b"]


def test_unknown_run_subpath_is_404():
    srv = server()
    rid = str(uuid.uuid4())
    call(srv, "POST", "/ingest", [ev(rid, 0, "a")])
    assert call(srv, "GET", f"/api/runs/{rid}/bogus")[0] == 404


# ── diff ─────────────────────────────────────────────────────────────────────────


def test_diff_reports_removed_events_and_divergence():
    srv = server()
    g, c = str(uuid.uuid4()), str(uuid.uuid4())
    call(srv, "POST", "/ingest", [ev(g, 0, "retrieved"), ev(g, 1, "verify"), ev(g, 2, "decide")])
    call(srv, "POST", "/ingest", [ev(c, 0, "retrieved"), ev(c, 1, "decide")])  # dropped verify

    s, body = call(srv, "POST", "/api/diff", {"base_run_id": g, "comparison_run_id": c})
    assert s == 200
    assert body["base_run_id"] == g and body["comparison_run_id"] == c
    assert body["is_identical"] is False
    assert body["summary"]["removed_events"] >= 1
    assert body["summary"]["divergence_points"] >= 1


def test_diff_identical_runs():
    srv = server()
    g, c = str(uuid.uuid4()), str(uuid.uuid4())
    steps = [("retrieved", 2), ("decided", 3)]
    for rid in (g, c):
        call(srv, "POST", "/ingest", [ev(rid, i, t, priority=p) for i, (t, p) in enumerate(steps)])

    s, body = call(srv, "POST", "/api/diff", {"base_run_id": g, "comparison_run_id": c})
    assert s == 200
    assert body["is_identical"] is True
    assert body["summary"]["divergence_points"] == 0


def test_diff_missing_run_is_404():
    srv = server()
    g = str(uuid.uuid4())
    call(srv, "POST", "/ingest", [ev(g, 0, "a")])
    s, body = call(srv, "POST", "/api/diff", {"base_run_id": g, "comparison_run_id": str(uuid.uuid4())})
    assert s == 404 and body["error"] == "RUN_NOT_FOUND"


# ── auth ─────────────────────────────────────────────────────────────────────────


def test_visualizer_endpoints_require_auth():
    srv = server()
    rid = str(uuid.uuid4())
    call(srv, "POST", "/ingest", [ev(rid, 0, "a")])
    assert call(srv, "GET", f"/api/runs/{rid}/replay", key=None)[0] == 401
    assert call(srv, "POST", "/api/diff", {"base_run_id": rid, "comparison_run_id": rid}, key=None)[0] == 401


# ── report (HTML export) ─────────────────────────────────────────────────────────


def test_report_returns_standalone_html():
    srv = server()
    g, c = str(uuid.uuid4()), str(uuid.uuid4())
    call(srv, "POST", "/ingest", [ev(g, 0, "retrieved"), ev(g, 1, "verify", priority=3), ev(g, 2, "decide", priority=3)])
    call(srv, "POST", "/ingest", [ev(c, 0, "retrieved"), ev(c, 1, "decide", priority=3)])  # dropped a critical step

    status, headers, out = srv.handle(
        "POST", "/api/report",
        {"Authorization": f"Bearer {API_KEY}"},
        json.dumps({"golden_run_id": g, "candidate_run_id": c}).encode(),
    )
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    body = out.decode()
    assert body.startswith("<!doctype html>")
    assert "REGRESSION" in body
    assert "verify" in body


def test_report_missing_run_is_404():
    srv = server()
    g = str(uuid.uuid4())
    call(srv, "POST", "/ingest", [ev(g, 0, "a")])
    s, body = call(srv, "POST", "/api/report", {"golden_run_id": g, "candidate_run_id": str(uuid.uuid4())})
    assert s == 404 and body["error"] == "RUN_NOT_FOUND"


def test_report_requires_auth():
    srv = server()
    assert call(srv, "POST", "/api/report", {"golden_run_id": "x", "candidate_run_id": "y"}, key=None)[0] == 401
