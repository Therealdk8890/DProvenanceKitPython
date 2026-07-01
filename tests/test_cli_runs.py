"""Tests for the ``dprovenancekit runs`` subcommand (baseline selection)."""

from __future__ import annotations

import json

from dprovenancekit import DProvenanceKit, SQLiteTraceStore, TracePriority
from dprovenancekit.cli import main
from dprovenancekit.event import AnyTraceableEvent


def _record(store, context_id, steps=("a",)):
    kit = DProvenanceKit(AnyTraceableEvent)
    with kit.run(context_id=context_id, store=store) as run:
        for step in steps:
            kit.record(
                AnyTraceableEvent(
                    type_identifier_value=step, priority_value=int(TracePriority.STRUCTURAL), raw_json="{}"
                )
            )
        return run.run_id


def _db(tmp_path):
    path = str(tmp_path / "t.sqlite")
    store = SQLiteTraceStore(AnyTraceableEvent, path, start_writer=False)
    ids = {
        "old": _record(store, "agent", ["a", "b"]),
        "new": _record(store, "agent", ["a", "b", "c"]),
        "other": _record(store, "other-agent", ["x"]),
    }
    store.close()
    return path, ids


def test_runs_lists_all(tmp_path, capsys):
    db, ids = _db(tmp_path)
    code = main(["runs", "--db", db, "--format", "id"])
    out = capsys.readouterr().out
    assert code == 0
    listed = set(out.split())
    assert listed == {str(v) for v in ids.values()}


def test_runs_filter_by_context(tmp_path, capsys):
    db, ids = _db(tmp_path)
    code = main(["runs", "--db", db, "--context", "agent", "--format", "id"])
    out = capsys.readouterr().out
    assert code == 0
    assert set(out.split()) == {str(ids["old"]), str(ids["new"])}
    assert str(ids["other"]) not in out


def test_runs_latest_by_context_is_deterministic(tmp_path, capsys):
    db, ids = _db(tmp_path)
    code = main(["runs", "--db", db, "--context", "other-agent", "--latest", "--format", "id"])
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == str(ids["other"])


def test_runs_latest_no_match_exits_1(tmp_path, capsys):
    db, _ = _db(tmp_path)
    code = main(["runs", "--db", db, "--context", "does-not-exist", "--latest", "--format", "id"])
    assert code == 1
    assert "no run found" in capsys.readouterr().err


def test_runs_json_output(tmp_path, capsys):
    db, ids = _db(tmp_path)
    code = main(["runs", "--db", db, "--context", "agent", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert {r["run_id"] for r in payload} == {str(ids["old"]), str(ids["new"])}
    assert all(r["context_id"] == "agent" for r in payload)


def test_runs_unopenable_db_exits_2(tmp_path, capsys):
    code = main(["runs", "--db", str(tmp_path), "--format", "id"])  # a directory
    assert code == 2
    assert "could not open database" in capsys.readouterr().err

