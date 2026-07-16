"""Execute the end-to-end demo so it can't rot.

``examples/end_to_end_demo.py`` self-asserts its full story (record → query → gate →
anomalies → diff → report) under ``__main__``. Running it here means any drift in the
public API breaks this test rather than silently breaking the showcase. Artifacts are
written to a temp dir so the repo stays clean.
"""

from __future__ import annotations

import os
import runpy
import sys

from dprovenancekit.cli import main


def test_end_to_end_demo_runs_and_self_asserts(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DPROV_DEMO_OUT", str(tmp_path))
    path = os.path.join(
        os.path.dirname(__file__), "..", "examples", "end_to_end_demo.py"
    )
    monkeypatch.setattr(sys, "argv", [path])

    runpy.run_path(path, run_name="__main__")

    out = capsys.readouterr().out
    assert "REGRESSION" in out
    assert "tool_drop:verify" in out and "looping:search" in out
    assert (tmp_path / "demo-report.html").exists()
    assert (tmp_path / "demo-rules.json").exists()
    assert (tmp_path / "demo-traces.sqlite").exists()


def test_installed_demo_defaults_to_current_working_directory(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)

    code = main(["demo"])

    out = capsys.readouterr().out
    assert code == 0
    assert "REGRESSION" in out
    assert "dprovenancekit gate --db demo-traces.sqlite" in out
    assert "dprovenancekit anomalies --db demo-traces.sqlite --rules demo-rules.json" in out
    assert "dprovenancekit ui --db demo-traces.sqlite" in out
    assert (tmp_path / "demo-report.html").exists()
    assert (tmp_path / "demo-rules.json").exists()
    assert (tmp_path / "demo-traces.sqlite").exists()
