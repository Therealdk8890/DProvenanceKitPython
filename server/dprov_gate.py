#!/usr/bin/env python3
"""CI regression gate — fail the build when an agent run regresses against a golden run.

Standalone (Python standard library only); drop it into any CI. It POSTs to the hosted
backend's ``/api/gate`` and sets the exit code from the verdict:

    0  no regression (gate passed)
    1  regression detected
    2  usage / request error

Usage:

    python dprov_gate.py --url "$DPROV_URL" --key "$DPROV_KEY" \
        --golden "$GOLDEN_RUN_ID" --candidate "$CANDIDATE_RUN_ID" \
        [--max-level none|low|medium|high] [--allow-divergent]

``--url`` / ``--key`` default to ``$DPROV_URL`` / ``$DPROV_KEY``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _urllib_request(method: str, url: str, headers: dict, body: bytes):
    req = urllib.request.Request(url=url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main(argv=None, request_fn=_urllib_request) -> int:
    ap = argparse.ArgumentParser(prog="dprov-gate", description="CI regression gate")
    ap.add_argument("--url", default=os.environ.get("DPROV_URL"))
    ap.add_argument("--key", default=os.environ.get("DPROV_KEY"))
    ap.add_argument("--golden", required=True, help="golden (known-good) run id")
    ap.add_argument("--candidate", required=True, help="candidate run id to gate")
    ap.add_argument("--max-level", default="none", choices=["none", "low", "medium", "high"],
                    help="worst severity that still passes (default: none = strict)")
    ap.add_argument("--allow-divergent", action="store_true",
                    help="tolerate per-step changes; gate only on severity")
    args = ap.parse_args(argv)

    if not args.url or not args.key:
        print("error: set --url/--key (or DPROV_URL/DPROV_KEY)", file=sys.stderr)
        return 2

    body = json.dumps({
        "golden_run_id": args.golden,
        "candidate_run_id": args.candidate,
        "max_regression_level": args.max_level,
        "allow_divergent_steps": args.allow_divergent,
    }).encode("utf-8")
    headers = {"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"}

    try:
        status, data = request_fn("POST", args.url.rstrip("/") + "/api/gate", headers, body)
    except Exception as e:  # noqa: BLE001 - network/connection failure
        print(f"error: gate request failed: {e}", file=sys.stderr)
        return 2

    try:
        report = json.loads(data.decode("utf-8"))
    except Exception:
        print(f"error: bad response (HTTP {status})", file=sys.stderr)
        return 2

    if status != 200:
        print(f"error: gate returned HTTP {status}: {report.get('error', report)}", file=sys.stderr)
        return 2

    print(report.get("summary", ""))
    if report.get("passed"):
        print("✓ no regression")
        return 0
    print("✗ regression detected", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
