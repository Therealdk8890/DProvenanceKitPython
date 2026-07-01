"""Tests for the ``dprovenancekit anomalies`` CLI subcommand."""

from __future__ import annotations

import json
import uuid

import pytest

from dprovenancekit import DProvenanceKit, SQLiteTraceStore, TracePriority
from dprovenancekit.cli import main
from dprovenancekit.event import AnyTraceableEvent


def _event(kind):
    return AnyTraceableEvent(
        type_identifier_value=kind,
        priority_value=int(TracePriority.STRUCTURAL),
        raw_json="{}",
    )


def _record(store, context_id, steps):
    kit = DProvenanceKit(AnyTraceableEvent)
    with kit.run(context_id=context_id, store=store) as run:
        for step in steps:
            kit.record(_event(step))
        return run.run_id


@pytest.fixture
def db_and_rules(tmp_path):
    db = str(tmp_path / "t.sqlite")
    store = SQLiteTraceStore(AnyTraceableEvent, db, start_writer=False)
    ids = {
        "clean": _record(store, "clean", ["plan", "safety_check", "act"]),
        # Missing safety_check AND web_search repeated 6 times (> 5).
        "bad": _record(store, "bad", ["plan"] + ["web_search"] * 6),
    }
    store.flush()
    store._db.close()

    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            {
                "rules": [
                    {"type": "tool_drop", "required_step": "safety_check"},
                    {"type": "looping", "step": "web_search", "max_repeats": 5},
                ]
            }
        ),
        encoding="utf-8",
    )
    return db, str(rules), ids


def test_anomalies_found_for_single_run(db_and_rules, capsys):
    db, rules, ids = db_and_rules
    code = main(
        ["anomalies", "--db", db, "--rules", rules, "--run", str(ids["bad"]), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["count"] == 2
    fired = {a["rule"] for a in payload["anomalies"]}
    assert fired == {"tool_drop:safety_check", "looping:web_search"}


def test_anomalies_none_for_clean_run(db_and_rules, capsys):
    db, rules, ids = db_and_rules
    code = main(
        [
            "anomalies",
            "--db",
            db,
            "--rules",
            rules,
            "--run",
            str(ids["clean"]),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["count"] == 0


def test_anomalies_over_all_runs(db_and_rules, capsys):
    db, rules, _ = db_and_rules
    code = main(["anomalies", "--db", db, "--rules", rules])
    out = capsys.readouterr().out
    assert code == 1
    assert "2 anomaly" in out
    assert "web_search" in out


def test_anomalies_clean_run_text_output(db_and_rules, capsys):
    db, rules, ids = db_and_rules
    code = main(["anomalies", "--db", db, "--rules", rules, "--run", str(ids["clean"])])
    assert code == 0
    assert "No anomalies detected." in capsys.readouterr().out


def test_anomalies_bad_config_is_usage_error(db_and_rules, tmp_path, capsys):
    db, _, _ = db_and_rules
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"rules": [{"type": "does_not_exist"}]}), encoding="utf-8"
    )
    code = main(["anomalies", "--db", db, "--rules", str(bad)])
    assert code == 2
    assert "unknown rule type" in capsys.readouterr().err


def test_anomalies_missing_run_is_usage_error(db_and_rules, capsys):
    db, rules, _ = db_and_rules
    code = main(["anomalies", "--db", db, "--rules", rules, "--run", str(uuid.uuid4())])
    assert code == 2
    assert "not found" in capsys.readouterr().err


def test_anomalies_bad_run_uuid_is_usage_error(db_and_rules, capsys):
    db, rules, _ = db_and_rules
    code = main(["anomalies", "--db", db, "--rules", rules, "--run", "not-a-uuid"])
    assert code == 2
    assert "valid run id" in capsys.readouterr().err


def test_anomalies_unopenable_db_exits_2(tmp_path, db_and_rules, capsys):
    _, rules, _ = db_and_rules
    # A directory is not a valid SQLite file; the CLI must report exit 2, not crash.
    code = main(["anomalies", "--db", str(tmp_path), "--rules", rules])
    assert code == 2
    assert "could not open database" in capsys.readouterr().err


def test_anomalies_bad_run_uuid_does_not_create_db_file(tmp_path, db_and_rules):
    db, rules, _ = db_and_rules
    fresh = tmp_path / "should-not-exist.sqlite"
    code = main(
        ["anomalies", "--db", str(fresh), "--rules", rules, "--run", "not-a-uuid"]
    )
    assert code == 2
    # The bad-UUID error is reported before the store is opened, so no DB file is created.
    assert not fresh.exists()
