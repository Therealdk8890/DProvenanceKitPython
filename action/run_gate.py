#!/usr/bin/env python3
"""Run the DProvenanceKit regression gate and publish GitHub Action step outputs.

Invoked by ``action.yml`` (composite). Runs ``python -m dprovenancekit.cli gate --json``
against a local SQLite database, then writes step outputs (``passed``, ``regression-level``,
``summary``, ``report-json``) to ``$GITHUB_OUTPUT`` and a short report to
``$GITHUB_STEP_SUMMARY``.

It deliberately does NOT fail the job when a regression is found — a later ``action.yml``
step enforces that from the ``passed`` output — so the PR-comment step still runs on a
failing gate. It returns non-zero only on a real error (bad input / run not found), mirroring
the gate's own exit code 2.

Standard library only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# An unlikely delimiter for the $GITHUB_OUTPUT multiline (heredoc) format.
_DELIM = "__DPROV_OUTPUT_EOF__"


def resolve_run(env, run_key, context_key, db, run=subprocess.run):
    """The run id for one side: explicit, or the newest run for its context id.

    Resolution shells out to ``dprovenancekit runs --latest --format id`` (available
    since 0.3.x) rather than the gate's own ``--golden-context`` flags, so the action
    works with whatever SDK version ``install-spec`` installed. An explicit run id
    wins — both may legitimately be set (e.g. candidate-run-id to scope anomaly rules
    alongside candidate-context). Returns ``(run_id_or_none, error_or_none)``.
    """
    if env.get(run_key):
        return env[run_key], None
    context = env.get(context_key)
    if not context:
        side = run_key.removeprefix("DPROV_").lower()
        return None, (
            f"no {side} run selected — set the {side}-run-id or "
            f"{side}-context input"
        )
    proc = run(
        [
            sys.executable, "-m", "dprovenancekit.cli", "runs",
            "--db", db,
            "--context", context,
            "--latest", "--format", "id",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, proc.stderr.strip() or f"no run found for context '{context}'"
    return proc.stdout.strip().splitlines()[0], None


def build_gate_argv(env, golden=None, candidate=None):
    """Build the ``python -m dprovenancekit.cli gate`` command from the step environment."""
    golden_db = env.get("DPROV_GOLDEN_DB") or env["DPROV_DB"]
    candidate_db = env.get("DPROV_CANDIDATE_DB") or env["DPROV_DB"]
    argv = [
        sys.executable, "-m", "dprovenancekit.cli", "gate",
        "--golden-db", golden_db,
        "--candidate-db", candidate_db,
        "--golden", golden if golden is not None else env["DPROV_GOLDEN"],
        "--candidate", candidate if candidate is not None else env["DPROV_CANDIDATE"],
        "--max-level", (env.get("DPROV_MAX_LEVEL") or "none"),
        "--json",
    ]
    if str(env.get("DPROV_ALLOW_DIVERGENT", "")).strip().lower() == "true":
        argv.append("--allow-divergent")
    return argv


def render_outputs(report):
    """Map a gate report dict to ordered ``(key, value)`` GitHub output pairs."""
    return [
        ("passed", "true" if report.get("passed") else "false"),
        ("regression-level", str(report.get("regression_level", ""))),
        ("summary", report.get("summary", "")),
        ("report-json", json.dumps(report)),
    ]


def write_outputs(pairs, path):
    """Append ``pairs`` to ``$GITHUB_OUTPUT`` using the multiline heredoc format."""
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in pairs:
            fh.write(f"{key}<<{_DELIM}\n{value}\n{_DELIM}\n")


def main(env=None):
    env = dict(os.environ if env is None else env)
    try:
        golden_db = env.get("DPROV_GOLDEN_DB") or env["DPROV_DB"]
        candidate_db = env.get("DPROV_CANDIDATE_DB") or env["DPROV_DB"]
        golden, golden_error = resolve_run(
            env, "DPROV_GOLDEN", "DPROV_GOLDEN_CONTEXT", golden_db
        )
        candidate, candidate_error = resolve_run(
            env, "DPROV_CANDIDATE", "DPROV_CANDIDATE_CONTEXT", candidate_db
        )
        for error in (golden_error, candidate_error):
            if error is not None:
                print(f"error: {error}", file=sys.stderr)
        if golden is None or candidate is None:
            return 2
        argv = build_gate_argv(env, golden, candidate)
    except KeyError as exc:
        print(f"error: missing required input {exc}", file=sys.stderr)
        return 2

    proc = subprocess.run(argv, capture_output=True, text=True)
    # Gate exit codes: 0 pass, 1 regression, 2 usage / run-not-found.
    if proc.returncode == 2:
        sys.stderr.write(proc.stderr)
        return 2
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(proc.stderr or proc.stdout)
        print("error: gate did not emit a JSON report", file=sys.stderr)
        return 2

    write_outputs(render_outputs(report), env.get("GITHUB_OUTPUT"))

    summary = report.get("summary", "")
    print(summary)
    step_summary = env.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("### DProvenanceKit regression gate\n\n```\n" + summary + "\n```\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

