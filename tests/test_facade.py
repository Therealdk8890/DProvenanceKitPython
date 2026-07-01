"""Tests for the global ``trace`` facade — the README's 5-minute-wow API."""

import json

import pytest

from dprovenancekit import record_event, trace


def _golden_workflow():
    with trace("Agent Workflow"):
        with trace("Retrieve Documents"):
            pass
        with trace("Verify Claims"):
            pass


def _workflow_missing_verify():
    with trace("Agent Workflow"):
        with trace("Retrieve Documents"):
            pass


def test_facade_explain_walks_the_span_tree(capsys):
    _golden_workflow()
    trace.explain()
    out = capsys.readouterr().out
    assert "--- Execution Trace (" in out
    assert "▶ Started Agent Workflow" in out
    assert "  ▶ Started Retrieve Documents" in out
    assert "  ▶ Started Verify Claims" in out
    assert "✔ Finished Agent Workflow" in out


@pytest.mark.parametrize("suffix", [".sqlite", ".jsonl"])
def test_facade_identical_run_diffs_clean(tmp_path, suffix, capsys):
    _golden_workflow()
    golden = tmp_path / f"golden{suffix}"
    trace.save(golden)
    assert golden.exists()

    trace.diff(golden)
    out = capsys.readouterr().out
    assert "--- Trace Diff (Golden vs Current) ---" in out
    assert "structurally identical" in out


@pytest.mark.parametrize("suffix", [".sqlite", ".jsonl"])
def test_facade_diff_reports_step_dropped_since_golden(tmp_path, suffix, capsys):
    """The README's headline: the saved file is the baseline, the current run the
    candidate — a step present in the golden file but dropped from the current run
    must print as missing (not as added)."""
    _golden_workflow()
    golden = tmp_path / f"golden{suffix}"
    trace.save(golden)

    _workflow_missing_verify()
    trace.diff(golden)
    out = capsys.readouterr().out
    assert "❌ Missing step: Verify Claims" in out
    assert "Added" not in out


def test_facade_diff_reports_new_step_as_added(tmp_path, capsys):
    _workflow_missing_verify()
    golden = tmp_path / "golden.sqlite"
    trace.save(golden)

    _golden_workflow()
    trace.diff(golden)
    out = capsys.readouterr().out
    assert "➕ Added step: Verify Claims" in out
    assert "Missing" not in out


def test_facade_jsonl_save_is_one_event_per_line(tmp_path):
    _golden_workflow()
    path = tmp_path / "run.jsonl"
    trace.save(path)

    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    # Three spans, a .start/.end pair each.
    assert len(lines) == 6
    types = [l["type"] for l in lines]
    assert "Agent Workflow.start" in types
    assert "Verify Claims.end" in types
    # Every event carries the envelope fields the loader needs.
    for line in lines:
        for key in ("id", "run_id", "sequence", "engine", "type", "priority"):
            assert key in line


def test_facade_save_rejects_unknown_extension(tmp_path):
    _golden_workflow()
    with pytest.raises(ValueError):
        trace.save(tmp_path / "run.csv")


@pytest.mark.parametrize("suffix", [".sqlite", ".jsonl"])
def test_facade_reserved_attribute_names_do_not_corrupt_goldens(
    tmp_path, suffix, capsys
):
    """User attributes named "type" or "priority" must not rewrite an event's
    identity through the save/load roundtrip — an identical rerun must diff clean."""

    def workflow():
        with trace("Agent Workflow"):
            record_event("plan.chosen", {"type": "rag", "priority": "high"})

    workflow()
    golden = tmp_path / f"golden{suffix}"
    trace.save(golden)

    workflow()
    trace.diff(golden)
    out = capsys.readouterr().out
    assert "structurally identical" in out


def test_facade_jsonl_golden_with_multiple_runs_uses_newest(tmp_path, capsys):
    """A concatenated multi-run .jsonl golden must load as its newest run, not an
    interleaved merge of all runs."""
    _golden_workflow()
    first = tmp_path / "first.jsonl"
    trace.save(first)

    _workflow_missing_verify()
    second = tmp_path / "second.jsonl"
    trace.save(second)

    combined = tmp_path / "golden.jsonl"
    combined.write_text(first.read_text() + second.read_text())

    _workflow_missing_verify()
    trace.diff(combined)
    out = capsys.readouterr().out
    assert "structurally identical" in out


def test_facade_error_block_records_error_event(capsys):
    with pytest.raises(RuntimeError):
        with trace("Agent Workflow"):
            with trace("Explodes"):
                raise RuntimeError("boom")
    trace.explain()
    out = capsys.readouterr().out
    assert "✖ Error in Explodes" in out
