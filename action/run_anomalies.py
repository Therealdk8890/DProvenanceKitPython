#!/usr/bin/env python3
"""Run DProvenanceKit anomaly rules over the candidate run and publish Action outputs.

Invoked by ``action.yml`` when ``anomaly-rules`` is set. Runs
``python -m dprovenancekit.cli anomalies --json`` scoped to the candidate run, writes
``anomaly-count`` / ``anomalies-json`` to ``$GITHUB_OUTPUT``, appends a table to
``$GITHUB_STEP_SUMMARY``, and prints one ``::warning::`` annotation per anomaly.

It does NOT fail the job — a later ``action.yml`` step enforces that from ``anomaly-count``
when ``fail-on-anomaly`` is true. Returns non-zero only on a real error (bad config / run not
found), mirroring the CLI's exit code 2. Standard library only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_DELIM = "__DPROV_OUTPUT_EOF__"


def build_argv(env):
    """Build the ``python -m dprovenancekit.cli anomalies`` command from the environment."""
    argv = [
        sys.executable, "-m", "dprovenancekit.cli", "anomalies",
        "--db", env["DPROV_DB"],
        "--rules", env["DPROV_ANOMALY_RULES"],
        "--json",
    ]
    candidate = env.get("DPROV_CANDIDATE")
    if candidate:
        argv += ["--run", candidate]
    return argv


def write_outputs(pairs, path):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in pairs:
            fh.write(f"{key}<<{_DELIM}\n{value}\n{_DELIM}\n")


def _one_line(value):
    """Collapse newlines/carriage returns so trace-derived text can't inject a workflow
    command (``::error::``) into the job log or break out of a markdown table cell."""
    return str(value).replace("\r", " ").replace("\n", " ")


def _cell(value):
    """One-line, table-safe text for a markdown cell (escapes the column separator)."""
    return _one_line(value).replace("|", "\\|")


def render_annotations(report):
    """One GitHub ``::warning::`` annotation line per anomaly."""
    return [
        f"::warning title=DProvenanceKit anomaly ({_one_line(a.get('rule'))})::"
        f"{_one_line(a.get('description'))}"
        for a in report.get("anomalies", [])
    ]


def render_summary(report):
    """A markdown section for ``$GITHUB_STEP_SUMMARY``."""
    count = report.get("count", 0)
    if not count:
        return "### DProvenanceKit anomaly rules\n\nNo anomalies detected.\n"
    rows = "\n".join(
        f"| `{_one_line(a.get('rule'))}` | {_cell(a.get('description'))} |"
        for a in report.get("anomalies", [])
    )
    return (
        "### DProvenanceKit anomaly rules\n\n"
        f"{count} anomaly(ies) detected.\n\n"
        "| rule | detail |\n| --- | --- |\n" + rows + "\n"
    )


def main(env=None):
    env = dict(os.environ if env is None else env)
    try:
        argv = build_argv(env)
    except KeyError as exc:
        print(f"error: missing required input {exc}", file=sys.stderr)
        return 2

    proc = subprocess.run(argv, capture_output=True, text=True)
    # anomalies exit codes: 0 none, 1 found, 2 usage / config / not-found.
    if proc.returncode == 2:
        sys.stderr.write(proc.stderr)
        return 2
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(proc.stderr or proc.stdout)
        print("error: anomalies command did not emit a JSON report", file=sys.stderr)
        return 2

    write_outputs(
        [
            ("anomaly-count", str(report.get("count", 0))),
            ("anomalies-json", json.dumps(report)),
        ],
        env.get("GITHUB_OUTPUT"),
    )

    for line in render_annotations(report):
        print(line)
    step_summary = env.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(render_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
