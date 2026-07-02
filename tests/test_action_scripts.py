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
    spec = importlib.util.spec_from_file_location(
        f"action_{name}", _ACTION_DIR / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_gate = _load("run_gate")
pr_comment = _load("pr_comment")
run_anomalies = _load("run_anomalies")


# ── fixtures ─────────────────────────────────────────────────────────────────────


def _event(kind, priority, raw="{}"):
    return AnyTraceableEvent(
        type_identifier_value=kind, priority_value=int(priority), raw_json=raw
    )


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
        "regressed": _record(
            store, "b", [_GOLDEN[0]]
        ),  # dropped the CRITICAL verify step
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


def test_build_gate_argv_passes_separate_dbs():
    # When golden/candidate dbs are unset, both default to DPROV_DB.
    argv = run_gate.build_gate_argv(
        {"DPROV_DB": "b.sqlite", "DPROV_GOLDEN": "g", "DPROV_CANDIDATE": "c"}
    )
    assert argv[argv.index("--golden-db") + 1] == "b.sqlite"
    assert argv[argv.index("--candidate-db") + 1] == "b.sqlite"

    # Separate dbs override the shared default.
    argv2 = run_gate.build_gate_argv(
        {
            "DPROV_DB": "b.sqlite",
            "DPROV_GOLDEN_DB": "base.sqlite",
            "DPROV_CANDIDATE_DB": "pr.sqlite",
            "DPROV_GOLDEN": "g",
            "DPROV_CANDIDATE": "c",
        }
    )
    assert argv2[argv2.index("--golden-db") + 1] == "base.sqlite"
    assert argv2[argv2.index("--candidate-db") + 1] == "pr.sqlite"


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
    path.write_text(
        json.dumps({"pull_request": {"number": pr_number}}), encoding="utf-8"
    )
    return str(path)


def test_post_comment_updates_existing_sticky(tmp_path):
    calls = []

    def fake_api(method, url, token, payload=None):
        calls.append((method, url))
        if method == "GET":
            return 200, [{"id": 7, "body": "stale " + pr_comment._MARKER}]
        return 200, {}

    pr = pr_comment.post_comment(
        {
            "passed": False,
            "regression_level": "high",
            "steps_by_change": {"removed": ["verify"]},
        },
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


# ── run_anomalies ────────────────────────────────────────────────────────────────


def test_run_anomalies_render_annotations_and_summary():
    report = {
        "count": 1,
        "anomalies": [
            {
                "rule": "looping:web_search",
                "run_id": "r",
                "description": "repeated 6 times",
            }
        ],
    }
    anns = run_anomalies.render_annotations(report)
    assert len(anns) == 1
    assert anns[0].startswith("::warning") and "web_search" in anns[0]

    summary = run_anomalies.render_summary(report)
    assert "1 anomaly" in summary and "looping:web_search" in summary
    assert "No anomalies" in run_anomalies.render_summary({"count": 0, "anomalies": []})


def test_run_anomalies_sanitizes_log_injection():
    # Trace-derived text (e.g. context_id) must not be able to inject a workflow command
    # into the job log or break out of the markdown table.
    report = {
        "count": 1,
        "anomalies": [
            {
                "rule": "tool_drop:x",
                "run_id": "r",
                "description": "ctx\n::error::pwned | x",
            }
        ],
    }
    ann = run_anomalies.render_annotations(report)[0]
    assert "\n" not in ann and "\r" not in ann  # cannot start a new ::error:: command

    summary = run_anomalies.render_summary(report)
    assert "\\|" in summary  # the injected pipe is escaped
    # The injected newline did not split the description across rows.
    data_rows = [r for r in summary.splitlines() if r.startswith("| `tool_drop:x`")]
    assert len(data_rows) == 1
    assert (
        "::error::pwned" in data_rows[0]
    )  # present but inert (single line, escaped pipe)


def test_run_anomalies_publishes_outputs(tmp_path, capsys):
    db = str(tmp_path / "a.sqlite")
    store = SQLiteTraceStore(AnyTraceableEvent, db, start_writer=False)
    candidate = _record(
        store, "cand", [("web_search", TracePriority.STRUCTURAL, "{}")] * 6
    )
    store.flush()
    store._db.close()

    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            {"rules": [{"type": "looping", "step": "web_search", "max_repeats": 5}]}
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.txt"
    rc = run_anomalies.main(
        {
            "DPROV_DB": db,
            "DPROV_CANDIDATE": str(candidate),
            "DPROV_ANOMALY_RULES": str(rules),
            "GITHUB_OUTPUT": str(out),
        }
    )
    assert rc == 0
    parsed = _parse_github_output(out)
    assert parsed["anomaly-count"] == "1"
    assert json.loads(parsed["anomalies-json"])["count"] == 1
    # The warning annotation is emitted to the log.
    assert "::warning" in capsys.readouterr().out


# ── context-based run selection (golden-context / candidate-context inputs) ──────


def test_gate_argv_run_id_wins_over_context():
    # action.yml documents setting candidate-run-id alongside candidate-context (to
    # scope anomaly rules); the gate argv must forward only ONE selector per side or
    # the CLI rejects the combination with exit 2.
    env = {
        "DPROV_DB": "traces.sqlite",
        "DPROV_GOLDEN": "aaaa",
        "DPROV_GOLDEN_CONTEXT": "golden-ctx",
        "DPROV_CANDIDATE": "bbbb",
        "DPROV_CANDIDATE_CONTEXT": "candidate-ctx",
    }
    argv = run_gate.build_gate_argv(env)
    assert "--golden" in argv and "--golden-context" not in argv
    assert "--candidate" in argv and "--candidate-context" not in argv


def test_gate_argv_falls_back_to_context():
    env = {
        "DPROV_DB": "traces.sqlite",
        "DPROV_GOLDEN_CONTEXT": "golden-ctx",
        "DPROV_CANDIDATE_CONTEXT": "candidate-ctx",
    }
    argv = run_gate.build_gate_argv(env)
    assert argv[argv.index("--golden-context") + 1] == "golden-ctx"
    assert argv[argv.index("--candidate-context") + 1] == "candidate-ctx"
    assert "--golden" not in argv and "--candidate" not in argv


def test_anomalies_resolves_candidate_context():
    calls = []

    def fake_run(argv, capture_output, text):
        calls.append(argv)

        class Proc:
            returncode = 0
            stdout = "1234-run-id\n"
            stderr = ""

        return Proc()

    env = {"DPROV_DB": "traces.sqlite", "DPROV_CANDIDATE_CONTEXT": "candidate"}
    run_id, error = run_anomalies.resolve_candidate(env, run=fake_run)
    assert error is None
    assert run_id == "1234-run-id"
    assert calls and "--latest" in calls[0] and "--context" in calls[0]


def test_anomalies_context_resolution_failure_is_an_error():
    def fake_run(argv, capture_output, text):
        class Proc:
            returncode = 1
            stdout = ""
            stderr = "error: no run found for context 'candidate'"

        return Proc()

    env = {"DPROV_DB": "traces.sqlite", "DPROV_CANDIDATE_CONTEXT": "candidate"}
    run_id, error = run_anomalies.resolve_candidate(env, run=fake_run)
    assert run_id is None
    assert "no run found" in error


def test_anomalies_explicit_run_id_skips_resolution():
    def fake_run(argv, capture_output, text):  # pragma: no cover - must not be called
        raise AssertionError("resolution subprocess should not run")

    env = {
        "DPROV_DB": "traces.sqlite",
        "DPROV_CANDIDATE": "abcd",
        "DPROV_CANDIDATE_CONTEXT": "candidate",
    }
    run_id, error = run_anomalies.resolve_candidate(env, run=fake_run)
    assert (run_id, error) == ("abcd", None)
