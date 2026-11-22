"""Regression tests for the safe, public-API-first project scaffold."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from dprovenancekit.cli import main


def test_init_scaffold_uses_instrumentation_api_and_preserves_existing_database(
    tmp_path, capsys, monkeypatch
):
    project = tmp_path / "agent-project"
    assert main(["init", str(project)]) == 0
    capsys.readouterr()

    database = project / ".dprovenance" / "traces.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE user_data (value TEXT NOT NULL)")
        connection.execute("INSERT INTO user_data VALUES ('keep-me')")

    repository_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repository_root), existing_pythonpath) if part
    )
    result = subprocess.run(
        [sys.executable, "agent_stub.py"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Decision: approved" in result.stdout
    assert "Recorded run:" in result.stdout

    with sqlite3.connect(database) as connection:
        preserved = connection.execute(
            "SELECT value FROM user_data"
        ).fetchone()
        assert preserved == ("keep-me",)
        recorded_runs = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert recorded_runs == 1

    scaffold_path = project / "agent_stub.py"
    scaffold = scaffold_path.read_text(encoding="utf-8")
    assert (
        "from dprovenancekit import TracePriority, record_event, traced, traced_run"
        in scaffold
    )
    assert "SQLiteTraceStore" not in scaffold
    assert ".unlink()" not in scaffold

    monkeypatch.chdir(project)
    assert main(["record"]) == 0

    second_run = subprocess.run(
        [sys.executable, "agent_stub.py"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert second_run.returncode == 0, second_run.stderr
    assert main(["gate"]) == 0

    # Removing the CRITICAL verification call must make the same gate fail.
    scaffold_path.write_text(
        scaffold.replace(
            "is_active = verify_account_status(account)",
            "is_active = True  # deliberately bypass verification",
        ),
        encoding="utf-8",
    )
    regressed_run = subprocess.run(
        [sys.executable, "agent_stub.py"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert regressed_run.returncode == 0, regressed_run.stderr
    assert main(["gate"]) == 1


def _python_blocks(markdown: str) -> list[str]:
    """Return the bodies of ``~~~python`` fenced blocks in generated markdown."""
    blocks = []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip() == "~~~python":
            body = []
            index += 1
            while index < len(lines) and lines[index].strip() != "~~~":
                body.append(lines[index])
                index += 1
            blocks.append("\n".join(body).strip())
        index += 1
    return blocks


def test_quickstart_failure_walkthrough_runs_as_documented(
    tmp_path, capsys, monkeypatch
):
    """Follow DPK_QUICKSTART.md's "see the gate fail" edit literally.

    The walkthrough once said to delete the ``verify_account_status(...)`` call, which
    leaves ``is_active`` undefined: the agent crashed with NameError before the gate
    ever ran. The documented edit must keep the agent runnable and make ``dpk gate``
    exit 1.
    """
    project = tmp_path / "walkthrough"
    assert main(["init", str(project)]) == 0
    capsys.readouterr()

    quickstart = (project / "DPK_QUICKSTART.md").read_text(encoding="utf-8")
    assert "change" in quickstart
    assert "remove the verify_account_status" not in quickstart
    edit = _python_blocks(quickstart)
    assert edit == ["is_active = verify_account_status(account)", "is_active = True"]
    before, after = edit

    scaffold_path = project / "agent_stub.py"
    scaffold = scaffold_path.read_text(encoding="utf-8")
    # The documented "before" line must exist exactly once in the generated stub.
    assert scaffold.count(before) == 1

    repository_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repository_root), existing_pythonpath) if part
    )

    def run_agent() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "agent_stub.py"],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    monkeypatch.chdir(project)
    baseline = run_agent()
    assert baseline.returncode == 0, baseline.stderr
    assert main(["record"]) == 0
    passing = run_agent()
    assert passing.returncode == 0, passing.stderr
    assert main(["gate"]) == 0
    capsys.readouterr()

    scaffold_path.write_text(scaffold.replace(before, after), encoding="utf-8")
    regressed = run_agent()
    assert regressed.returncode == 0, regressed.stderr
    assert "NameError" not in regressed.stderr
    assert "Decision: approved" in regressed.stdout
    assert main(["gate"]) == 1
    gate_output = capsys.readouterr().out
    assert "verify_account_status" in gate_output
