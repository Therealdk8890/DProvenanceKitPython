"""CI-friendly guard: README / docs must not drift from pyproject version markers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_version_surface_script() -> None:
    script = ROOT / "scripts" / "check_version_surface.py"
    assert script.is_file(), f"missing {script}"
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
