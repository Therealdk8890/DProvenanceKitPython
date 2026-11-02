"""Regression tests for the safe, public-API-first project scaffold."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from dprovenancekit.cli import main


def test_init_scaffold_uses_instrumentation_api_and_preserves_existing_database(
    tmp_path, capsys
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

    scaffold = (project / "agent_stub.py").read_text(encoding="utf-8")
    assert "from dprovenancekit import TracePriority, record_event, traced, traced_run" in scaffold
    assert "SQLiteTraceStore" not in scaffold
    assert ".unlink()" not in scaffold
