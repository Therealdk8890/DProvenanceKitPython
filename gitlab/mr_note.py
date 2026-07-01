#!/usr/bin/env python3
"""Post (or update) a sticky GitLab Merge Request note summarizing the regression gate.

Reads the gate report JSON from ``$DPROV_REPORT_JSON`` or stdin, renders markdown, and upserts
a single MR note (found via a hidden marker) using the GitLab API. Re-runs edit the same note
in place instead of stacking new ones.

Requires ``$DPROV_GITLAB_TOKEN`` — a project or personal access token with ``api`` scope.
``CI_JOB_TOKEN`` generally cannot create MR notes, so a dedicated token is needed. Uses the
predefined ``CI_API_V4_URL`` / ``CI_PROJECT_ID`` / ``CI_MERGE_REQUEST_IID``. When the token or
MR context is missing, it skips gracefully (prints the body) rather than failing the job.

Standard library only — the GitLab analogue of ``action/pr_comment.py``.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# Hidden marker that identifies our note so re-runs update it in place.
_MARKER = "<!-- dprovenancekit-regression-gate -->"

# Per-step change kinds worth surfacing, in a stable display order.
_CHANGE_ORDER = ("removed", "added", "reordered", "ambiguous")


def render_note(report):
    """Render the gate report dict as a sticky MR-note markdown body (pure function)."""
    passed = bool(report.get("passed"))
    badge = "✅ **Regression gate passed**" if passed else "❌ **Regression gate failed**"
    level = report.get("regression_level", "none")
    strength = float(report.get("strength", 0.0) or 0.0)

    lines = [
        _MARKER,
        "## DProvenanceKit",
        "",
        badge,
        "",
        f"- **Severity:** {level} (strength {strength:.2f}); "
        f"max allowed: {report.get('max_regression_level', 'none')}",
        f"- **Fingerprint:** {'match' if report.get('fingerprint_match') else 'differs'}",
    ]

    changes = report.get("steps_by_change") or {}
    rows = [(kind, changes[kind]) for kind in _CHANGE_ORDER if changes.get(kind)]
    if rows:
        lines += ["", "| change | steps |", "| --- | --- |"]
        lines += [f"| {kind} | {', '.join(steps)} |" for kind, steps in rows]
    else:
        lines.append("- No per-step changes (all exact matches).")

    reasoning = report.get("reasoning")
    if reasoning:
        lines += ["", f"_{reasoning}_"]
    return "\n".join(lines)


def _api(method, url, token, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "PRIVATE-TOKEN": token,
            "Content-Type": "application/json",
            "User-Agent": "dprovenancekit-regression-gate",
        },
    )
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
        return resp.status, (json.loads(body) if body else None)


def post_note(report, env, api=_api):
    """Upsert the sticky note on the MR named by the GitLab CI environment.

    Returns the MR iid on success, or ``None`` when posting was skipped (no token, not an MR
    pipeline, or insufficient permissions).
    """
    body = render_note(report)
    token = env.get("DPROV_GITLAB_TOKEN")
    api_url = env.get("CI_API_V4_URL")
    project = env.get("CI_PROJECT_ID")
    mr_iid = env.get("CI_MERGE_REQUEST_IID")

    if not token or not api_url or not project or not mr_iid:
        print("dprovenancekit: no token / not an MR pipeline; note body follows:\n" + body)
        return None

    base = f"{api_url}/projects/{urllib.parse.quote_plus(str(project))}/merge_requests/{mr_iid}/notes"
    try:
        _, notes = api("GET", base + "?per_page=100", token)
        existing = next((n for n in (notes or []) if _MARKER in (n.get("body") or "")), None)
        if existing:
            api("PUT", f"{base}/{existing['id']}", token, {"body": body})
        else:
            api("POST", base, token, {"body": body})
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            print(
                "dprovenancekit: insufficient permissions to post the MR note "
                "(does DPROV_GITLAB_TOKEN have 'api' scope?); skipping",
                file=sys.stderr,
            )
            return None
        raise
    return mr_iid


def main(env=None):
    env = dict(os.environ if env is None else env)
    raw = env.get("DPROV_REPORT_JSON") or sys.stdin.read()
    if not raw.strip():
        print("error: no gate report JSON provided (stdin or $DPROV_REPORT_JSON)", file=sys.stderr)
        return 1
    post_note(json.loads(raw), env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

