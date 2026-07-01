#!/usr/bin/env python3
"""Post (or update) a sticky pull-request comment summarizing the regression gate.

Reads the gate report JSON from ``$DPROV_REPORT_JSON``, renders markdown, and upserts a
single comment on the PR (found via a hidden marker) using ``$GITHUB_TOKEN``. Re-runs edit
the same comment in place instead of stacking new ones.

On a fork PR the ``GITHUB_TOKEN`` is read-only, so posting is skipped with a notice rather
than failing the job. Standard library only (``urllib``), matching ``server/dprov_gate.py``.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# Hidden HTML marker that identifies our comment so re-runs update it in place.
_MARKER = "<!-- dprovenancekit-regression-gate -->"

# Per-step change kinds worth surfacing, in a stable display order.
_CHANGE_ORDER = ("removed", "added", "reordered", "ambiguous")


def render_comment(report):
    """Render the gate report dict as a sticky PR-comment markdown body (pure function)."""
    passed = bool(report.get("passed"))
    badge = "✅ **Regression gate passed**" if passed else "❌ **Regression gate failed**"
    level = report.get("regression_level", "none")
    strength = float(report.get("strength", 0.0) or 0.0)
    fp_match = report.get("fingerprint_match")

    lines = [
        _MARKER,
        "## DProvenanceKit",
        "",
        badge,
        "",
        f"- **Severity:** {level} (strength {strength:.2f}); "
        f"max allowed: {report.get('max_regression_level', 'none')}",
        f"- **Fingerprint:** {'match' if fp_match else 'differs'}",
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
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "dprovenancekit-regression-gate",
        },
    )
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
        return resp.status, (json.loads(body) if body else None)


def post_comment(report, env, api=_api):
    """Upsert the sticky comment on the PR named by the GitHub event payload.

    Returns the PR number on success, or ``None`` when posting was skipped (no token, not a
    pull_request event, or insufficient permissions).
    """
    body = render_comment(report)
    token = env.get("GITHUB_TOKEN")
    event_path = env.get("GITHUB_EVENT_PATH")
    repo = env.get("GITHUB_REPOSITORY")
    api_url = env.get("GITHUB_API_URL", "https://api.github.com")

    if not token or not event_path or not repo:
        print("dprovenancekit: no token / event context; comment body follows:\n" + body)
        return None

    with open(event_path, encoding="utf-8") as fh:
        event = json.load(fh)
    pr = (event.get("pull_request") or {}).get("number") or event.get("number")
    if not pr:
        print("dprovenancekit: not a pull_request event; skipping comment")
        return None

    comments_url = f"{api_url}/repos/{repo}/issues/{pr}/comments?per_page=100"
    post_url = f"{api_url}/repos/{repo}/issues/{pr}/comments"
    try:
        _, comments = api("GET", comments_url, token)
        existing = next(
            (c for c in (comments or []) if _MARKER in (c.get("body") or "")), None
        )
        if existing:
            api("PATCH", f"{api_url}/repos/{repo}/issues/comments/{existing['id']}", token, {"body": body})
        else:
            api("POST", post_url, token, {"body": body})
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            print(
                "dprovenancekit: insufficient permissions to comment "
                "(fork PR token is read-only?); skipping",
                file=sys.stderr,
            )
            return None
        raise
    return pr


def main(env=None):
    env = dict(os.environ if env is None else env)
    raw = env.get("DPROV_REPORT_JSON")
    if not raw:
        print("error: DPROV_REPORT_JSON is empty", file=sys.stderr)
        return 1
    post_comment(json.loads(raw), env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# git-blob-rewrite
