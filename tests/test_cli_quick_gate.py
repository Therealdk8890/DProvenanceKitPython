"""Zero-configuration ``dpk record`` / ``compare`` / ``gate`` workflow."""

from __future__ import annotations

import sqlite3
import time

from dprovenancekit import SQLiteTraceStore, TracedEvent, traced, traced_run
from dprovenancekit.cli import main


@traced
def _retrieve():
    return ["source"]


@traced
def _verify():
    return True


def _run_agent(include_verify=True, context_id="research-agent"):
    with traced_run(context_id=context_id) as run:
        _retrieve()
        if include_verify:
            _verify()
    return run.run_id


def test_quick_workflow_records_compares_and_gates(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    golden_id = _run_agent()
    assert main(["record"]) == 0
    recorded = capsys.readouterr().out
    baseline = tmp_path / ".dprovenance" / "baseline.sqlite"
    assert baseline.exists()
    assert str(golden_id) in recorded

    # The pinned baseline is a single, self-contained run and keeps its provenance edges.
    with SQLiteTraceStore(TracedEvent, str(baseline), start_writer=False) as store:
        metadata = store.list_run_metadata()
        assert len(metadata) == 1
        assert metadata[0].run_id == str(golden_id)
    conn = sqlite3.connect(str(baseline))
    try:
        assert conn.execute("SELECT COUNT(*) FROM trace_edges").fetchone()[0] == 2
    finally:
        conn.close()

    time.sleep(0.002)
    _run_agent()
    assert main(["compare"]) == 0
    assert "PASS" in capsys.readouterr().out
    assert main(["gate"]) == 0
    assert "PASS" in capsys.readouterr().out

    time.sleep(0.002)
    _run_agent(include_verify=False)
    # compare is exploratory: it reports the regression without failing the shell.
    assert main(["compare"]) == 0
    compared = capsys.readouterr().out
    assert "FAIL" in compared
    assert "verify" in compared
    # gate applies the same report as a CI-safe exit status.
    assert main(["gate"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_record_context_selects_the_intended_run(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    wanted = _run_agent(context_id="wanted")
    time.sleep(0.002)
    _run_agent(context_id="newer-but-unrelated")

    assert main(["record", "--context", "wanted"]) == 0
    assert str(wanted) in capsys.readouterr().out

    baseline = tmp_path / ".dprovenance" / "baseline.sqlite"
    with SQLiteTraceStore(TracedEvent, str(baseline), start_writer=False) as store:
        metadata = store.list_run_metadata()
    assert len(metadata) == 1
    assert metadata[0].context_id == "wanted"

    time.sleep(0.002)
    _run_agent(context_id="wanted")
    time.sleep(0.002)
    _run_agent(include_verify=False, context_id="newer-but-unrelated")
    assert main(["gate", "--context", "wanted"]) == 0
    assert "PASS" in capsys.readouterr().out


def test_record_without_a_trace_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["record"]) == 2
    captured = capsys.readouterr()
    assert "Run code inside traced_run" in captured.err
    assert not (tmp_path / ".dprovenance" / "traces.sqlite").exists()


def test_quick_gate_without_a_baseline_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _run_agent()
    assert main(["gate"]) == 2
    captured = capsys.readouterr()
    assert "dpk record" in captured.err
    assert not (tmp_path / ".dprovenance" / "baseline.sqlite").exists()
