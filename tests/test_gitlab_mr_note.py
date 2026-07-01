"""Tests for the GitLab MR-note poster (``gitlab/mr_note.py``)."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

_GITLAB_DIR = Path(__file__).resolve().parents[1] / "gitlab"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"gitlab_{name}", _GITLAB_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mr_note = _load("mr_note")


def _env(**overrides):
    env = {
        "DPROV_GITLAB_TOKEN": "tok",
        "CI_API_V4_URL": "https://gitlab.example/api/v4",
        "CI_PROJECT_ID": "42",
        "CI_MERGE_REQUEST_IID": "7",
    }
    env.update(overrides)
    return env


# ── render_note ──────────────────────────────────────────────────────────────────


def test_render_note_pass():
    md = mr_note.render_note(
        {
            "passed": True,
            "regression_level": "none",
            "strength": 1.0,
            "fingerprint_match": True,
            "max_regression_level": "none",
            "steps_by_change": {},
        }
    )
    assert mr_note._MARKER in md
    assert "passed" in md
    assert "No per-step changes" in md


def test_render_note_fail_lists_changes():
    md = mr_note.render_note(
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


# ── post_note (injected fake API) ────────────────────────────────────────────────


def test_post_note_updates_existing_sticky():
    calls = []

    def fake_api(method, url, token, payload=None):
        calls.append((method, url))
        if method == "GET":
            return 200, [{"id": 9, "body": "stale " + mr_note._MARKER}]
        return 200, {}

    iid = mr_note.post_note(
        {"passed": False, "regression_level": "high", "steps_by_change": {"removed": ["v"]}},
        _env(),
        api=fake_api,
    )
    assert iid == "7"
    methods = [m for m, _ in calls]
    assert "PUT" in methods and "POST" not in methods


def test_post_note_creates_when_absent():
    calls = []

    def fake_api(method, url, token, payload=None):
        calls.append(method)
        return (200, []) if method == "GET" else (201, {"id": 1})

    mr_note.post_note({"passed": True, "steps_by_change": {}}, _env(), api=fake_api)
    assert "POST" in calls and "PUT" not in calls


def test_post_note_url_encodes_project_path():
    seen = {}

    def fake_api(method, url, token, payload=None):
        seen.setdefault("url", url)
        return (200, []) if method == "GET" else (201, {"id": 1})

    mr_note.post_note(
        {"passed": True, "steps_by_change": {}}, _env(CI_PROJECT_ID="group/proj"), api=fake_api
    )
    assert "group%2Fproj" in seen["url"]


def test_post_note_dry_run_without_token(capsys):
    result = mr_note.post_note({"passed": True, "steps_by_change": {}}, {})
    assert result is None
    assert mr_note._MARKER in capsys.readouterr().out


# ── main ─────────────────────────────────────────────────────────────────────────


def test_main_reads_report_from_env(capsys):
    rc = mr_note.main({"DPROV_REPORT_JSON": json.dumps({"passed": True, "steps_by_change": {}})})
    assert rc == 0
    assert mr_note._MARKER in capsys.readouterr().out


def test_main_errors_on_empty_input(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert mr_note.main({}) == 1

