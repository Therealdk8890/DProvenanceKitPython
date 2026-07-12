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


def test_scoped_run_applies_is_anomalous_refinement(tmp_path, capsys):
    """Regression: ``anomalies --run <id>`` must apply ``is_anomalous()``, not just
    the pre-filter query. ``unused_tool_result``'s ``anomaly_query`` is a bare
    ``requiring_step()`` filter, so a run that contains web_search but *does* follow
    it with summarize must NOT be flagged. (This is the exact path the GitHub Action
    uses when it scopes anomaly rules to the candidate run.)"""
    db = str(tmp_path / "u.sqlite")
    store = SQLiteTraceStore(AnyTraceableEvent, db, start_writer=False)
    ids = {
        # web_search IS consumed by a later summarize -> passes the pre-filter but
        # is NOT anomalous.
        "consumed": _record(store, "consumed", ["web_search", "verify", "summarize"]),
        # web_search never followed by summarize -> genuinely anomalous.
        "dangling": _record(store, "dangling", ["web_search", "decide"]),
    }
    store.flush()
    store._db.close()

    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "type": "unused_tool_result",
                        "step": "web_search",
                        "required_followup_step": "summarize",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    # Consumed run: passes requiring_step() but is_anomalous() is False -> no anomaly.
    code = main(
        ["anomalies", "--db", db, "--rules", str(rules),
         "--run", str(ids["consumed"]), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["count"] == 0

    # Dangling run: the rule still fires when it genuinely should.
    code = main(
        ["anomalies", "--db", db, "--rules", str(rules),
         "--run", str(ids["dangling"]), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["count"] == 1
    assert payload["anomalies"][0]["rule"] == "unused_tool_result:web_search"


def test_anomalies_json_includes_severity_and_message(tmp_path, capsys):
    """The JSON output surfaces the presentation metadata build_rule carries onto each
    anomaly: an explicit severity/message from the spec, and the defaults
    (severity 'warning', message null) when the spec omits them."""
    db = str(tmp_path / "s.sqlite")
    store = SQLiteTraceStore(AnyTraceableEvent, db, start_writer=False)
    bad = _record(store, "bad", ["plan"] + ["web_search"] * 6)
    store.flush()
    store._db.close()

    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "type": "tool_drop",
                        "required_step": "safety_check",
                        "severity": "critical",
                        "message": "Agent skipped the safety check.",
                    },
                    {"type": "looping", "step": "web_search", "max_repeats": 5},
                ]
            }
        ),
        encoding="utf-8",
    )

    code = main(
        ["anomalies", "--db", db, "--rules", str(rules), "--run", str(bad), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    by_rule = {a["rule"]: a for a in payload["anomalies"]}
    assert by_rule["tool_drop:safety_check"]["severity"] == "critical"
    assert (
        by_rule["tool_drop:safety_check"]["message"]
        == "Agent skipped the safety check."
    )
    # Spec without severity/message keeps the defaults.
    assert by_rule["looping:web_search"]["severity"] == "warning"
    assert by_rule["looping:web_search"]["message"] is None


def test_anomalies_text_includes_severity_and_message(tmp_path, capsys):
    db = str(tmp_path / "s.sqlite")
    store = SQLiteTraceStore(AnyTraceableEvent, db, start_writer=False)
    bad = _record(store, "bad", ["plan", "act"])
    store.flush()
    store._db.close()

    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "type": "tool_drop",
                        "required_step": "safety_check",
                        "severity": "critical",
                        "message": "Agent skipped the safety check.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    code = main(["anomalies", "--db", db, "--rules", str(rules), "--run", str(bad)])
    out = capsys.readouterr().out
    assert code == 1
    assert "[tool_drop:safety_check] critical:" in out
    assert "(Agent skipped the safety check.)" in out


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


def test_anomalies_nonexistent_db_exits_2_without_creating_file(tmp_path, db_and_rules, capsys):
    # A typo'd --db path (rules load fine) must error rather than have sqlite3.connect()
    # create an empty database and report "No anomalies detected." with exit 0.
    _, rules, _ = db_and_rules
    missing = tmp_path / "typo.sqlite"
    code = main(["anomalies", "--db", str(missing), "--rules", rules])
    assert code == 2
    assert "no such database" in capsys.readouterr().err
    assert not missing.exists()  # guard must not leave a stray empty db
