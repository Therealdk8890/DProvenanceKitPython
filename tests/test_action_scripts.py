"""Tests for the GitHub Action helper scripts under ``action/``.

The scripts are standalone (stdlib only) and live outside the package, so they are loaded by
path. ``run_gate`` is exercised end-to-end against a real SQLite database; ``pr_comment``'s
rendering is a pure function and its posting logic is tested with an injected fake API.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from dprovenancekit import DProvenanceKit, SQLiteTraceStore, TracePriority
from dprovenancekit.event import AnyTraceableEvent

_ACTION_DIR = Path(__file__).resolve().parents[1] / "action"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"action_{name}", _ACTION_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_gate = _load("run_gate")
pr_comment = _load("pr_comment")


# ── fixtures ─────────────────────────────────────────────────────────────────────


def _event(kind, priority, raw="{}"):
    return AnyTraceableEvent(type_identifier_value=kind, priority_value=int(priority), raw_json=raw)


def _record(store, context_id, steps):
    kit = DProvenanceKit(AnyTraceableEvent)
    with kit.run(context_id=context_id, store=store) as run:
        for kind, priority, raw in steps:
            kit.record(_event(kind, priority, raw))
        return run.run_id


_GOLDEN = [
    ("retrieved", TracePriority.STRUCTURAL, '{"d": "3 sources"}'),
    ("verify", TracePriority.CRITICAL, '{"d": "ok"}'),
]


@pytest.fixture
def trace_db(tmp_path):
    db = str(tmp_path / "t.sqlite")
    store = SQLiteTraceStore(AnyTraceableEvent, db, start_writer=False)
    ids = {
        "golden": _record(store, "g", _GOLDEN),
        "pass": _record(store, "ok", _GOLDEN),
        "regressed": _record(store, "b", [_GOLDEN[0]]),  # dropped the CRITICAL verify step
    }
    store.flush()
    store._db.close()
    return db, ids


def _parse_github_output(path):
    """Parse the ``key<<DELIM\\n...\\nDELIM`` multiline format back into a dict."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    out, i = {}, 0
    while i < len(lines):
        if "<<" in lines[i]:
            key, delim = lines[i].split("<<", 1)
            i += 1
            buf = []
            while i < len(lines) and lines[i] != delim:
                buf.append(lines[i])
                i += 1
            out[key] = "\n".join(buf)
        i += 1
    return out


# ── run_gate ─────────────────────────────────────────────────────────────────────


def test_run_gate_publishes_pass_outputs(trace_db, tmp_path):
    db, ids = trace_db
    out = tmp_path / "out.txt"
    rc = run_gate.main(
        {
            "DPROV_DB": db,
            "DPROV_GOLDEN": str(ids["golden"]),
            "DPROV_CANDIDATE": str(ids["pass"]),
            "GITHUB_OUTPUT": str(out),
        }
    )
    assert rc == 0
    parsed = _parse_github_output(out)
    assert parsed["passed"] == "true"
    assert parsed["regression-level"] == "none"
    assert json.loads(parsed["report-json"])["passed"] is True


def test_run_gate_publishes_regression_without_failing_wrapper(trace_db, tmp_path):
    db, ids = trace_db
    out = tmp_path / "out.txt"
    # The wrapper succeeds (rc 0) even on a regression — enforcement is a separate step.
    rc = run_gate.main(
        {
            "DPROV_DB": db,
            "DPROV_GOLDEN": str(ids["golden"]),
            "DPROV_CANDIDATE": str(ids["regressed"]),
            "GITHUB_OUTPUT": str(out),
        }
    )
    assert rc == 0
    parsed = _parse_github_output(out)
    assert parsed["passed"] == "false"
    assert parsed["regression-level"] == "high"


def test_run_gate_reports_usage_error_on_missing_run(trace_db, tmp_path, capsys):
    db, ids = trace_db
    rc = run_gate.main(
        {
            "DPROV_DB": db,
            "DPROV_GOLDEN": str(ids["golden"]),
            "DPROV_CANDIDATE": "00000000-0000-0000-0000-000000000000",
            "GITHUB_OUTPUT": str(tmp_path / "out.txt"),
        }
    )
    assert rc == 2
    assert "not found" in capsys.readouterr().err


# ── pr_comment.render_comment ────────────────────────────────────────────────────


def test_render_comment_pass():
    md = pr_comment.render_comment(
        {
            "passed": True,
            "regression_level": "none",
            "strength": 1.0,
            "fingerprint_match": True,
            "max_regression_level": "none",
            "steps_by_change": {},
        }
    )
    assert pr_comment._MARKER in md
    assert "passed" in md
    assert "No per-step changes" in md


def test_render_comment_fail_lists_changes():
    md = pr_comment.render_comment(
        {
            "passed": False,
            "regression_level": "high",
            "strength": 0.95,
            "fingerprint_match": False,
            "max_regression_level": "none",
            "steps_by_change": {"removed": ["verify"]},
            "reasoning": "Critical reasoning steps removed: verify",
        }
    )
    assert "failed" in md
    assert "| removed | verify |" in md
    assert "Critical reasoning steps removed" in md


# ── pr_comment.post_comment (injected fake API) ──────────────────────────────────


def _event_file(tmp_path, pr_number):
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"pull_request": {"number": pr_number}}), encoding="utf-8")
    return str(path)


def test_post_comment_updates_existing_sticky(tmp_path):
    calls = []

    def fake_api(method, url, token, payload=None):
        calls.append((method, url))
        if method == "GET":
            return 200, [{"id": 7, "body": "stale " + pr_comment._MARKER}]
        return 200, {}

    pr = pr_comment.post_comment(
        {"passed": False, "regression_level": "high", "steps_by_change": {"removed": ["verify"]}},
        {
            "GITHUB_TOKEN": "x",
            "GITHUB_EVENT_PATH": _event_file(tmp_path, 42),
            "GITHUB_REPOSITORY": "o/r",
        },
        api=fake_api,
    )
    assert pr == 42
    methods = [m for m, _ in calls]
    assert "PATCH" in methods and "POST" not in methods


def test_post_comment_creates_when_absent(tmp_path):
    calls = []

    def fake_api(method, url, token, payload=None):
        calls.append(method)
        return (200, []) if method == "GET" else (201, {"id": 1})

    pr_comment.post_comment(
        {"passed": True, "steps_by_change": {}},
        {
            "GITHUB_TOKEN": "x",
            "GITHUB_EVENT_PATH": _event_file(tmp_path, 5),
            "GITHUB_REPOSITORY": "o/r",
        },
        api=fake_api,
    )
    assert "POST" in calls and "PATCH" not in calls


def test_post_comment_dry_run_without_token(capsys):
    result = pr_comment.post_comment({"passed": True, "steps_by_change": {}}, {})
    assert result is None
    assert pr_comment._MARKER in capsys.readouterr().out
