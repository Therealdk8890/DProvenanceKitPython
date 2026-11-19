"""Fail-closed quality gate for the isolated candidate, with no automatic installs."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = (
    ("lint", ("-m", "ruff", "check", "--target-version", "py39", "--select", "E,F", "--ignore", "E501", "experimental", "tests/test_event_chain_candidate.py", "scripts/event_chain_gate.py")),
    ("strict types", ("-m", "mypy", "--config-file", "event_chain_mypy.ini", "experimental")),
    ("tests", ("-m", "pytest", "-q", "tests/test_event_chain_candidate.py")),
)


def main() -> int:
    failed = False
    for label, args in COMMANDS:
        print(f"=== {label} ===", flush=True)
        result = subprocess.run((sys.executable, *args), cwd=ROOT, check=False)
        if result.returncode != 0:
            failed = True
            print(f"FAIL: {label} (exit {result.returncode})", flush=True)
        else:
            print(f"PASS: {label}", flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
