#!/usr/bin/env python3
"""Fail if version markers disagree across pyproject, __init__, README, VERSION_SURFACE."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("error: version not found in pyproject.toml")
    return m.group(1)


def init_fallback_version() -> str:
    text = (ROOT / "dprovenancekit" / "__init__.py").read_text()
    m = re.search(r'^    __version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        # alternate indentation
        m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("error: __version__ fallback not found")
    return m.group(1)


def main() -> int:
    errors: list[str] = []
    ver = pyproject_version()
    init_ver = init_fallback_version()
    if ver != init_ver:
        errors.append(f"pyproject ({ver}) != __init__ fallback ({init_ver})")

    readme = (ROOT / "README.md").read_text()
    surface = (ROOT / "docs" / "VERSION_SURFACE.md").read_text()

    if ver not in surface:
        errors.append(f"docs/VERSION_SURFACE.md does not mention {ver}")

    # Status line should mention current version
    if f"]" not in readme or ver not in readme:
        # require version string somewhere in README status/matrix
        if ver not in readme:
            errors.append(f"README.md does not mention version {ver}")

    # Attestation honesty: if version >= 0.7.0, matrix must not say Not yet for attestation
    parts = [int(x) for x in ver.split(".")[:3]]
    if parts >= [0, 7, 0]:
        # look for attestation row claiming Not yet on Python column
        for line in readme.splitlines():
            if "attestation" in line.lower() and "Not yet" in line and "Proof packs" not in line:
                errors.append(
                    f"README attestation row still says Not yet but package is {ver}: {line}"
                )

    if errors:
        print("version surface check FAILED:")
        for e in errors:
            print(" -", e)
        return 1

    print(f"version surface ok: {ver}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
