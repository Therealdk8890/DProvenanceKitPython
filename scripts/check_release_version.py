#!/usr/bin/env python3
"""Reject release tags that do not match the package's declared and fallback versions."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def _single_version(pattern: str, content: str, source: str) -> str:
    matches = re.findall(pattern, content, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(
            f"{source}: expected exactly one version declaration, found {len(matches)}"
        )
    return matches[0]


def check_release_version(tag: str, root: Path) -> list[str]:
    errors: list[str] = []
    if not tag.startswith("v") or len(tag) == 1:
        return ["release tag must use the vX.Y.Z form"]

    try:
        project_text = (root / "pyproject.toml").read_text(encoding="utf-8")
        project_version = _single_version(
            r'^version\s*=\s*"([^"]+)"\s*$',
            project_text,
            "pyproject.toml",
        )
        init_text = (root / "dprovenancekit" / "__init__.py").read_text(
            encoding="utf-8"
        )
        fallback_version = _single_version(
            r'^\s*__version__\s*=\s*"([^"]+)"\s*$',
            init_text,
            "dprovenancekit/__init__.py",
        )
    except (OSError, ValueError) as exc:
        return [str(exc)]

    tag_version = tag[1:]
    if project_version != fallback_version:
        errors.append(
            "pyproject.toml version "
            f"({project_version}) does not match __version__ fallback "
            f"({fallback_version})"
        )
    if tag_version != project_version:
        errors.append(
            f"release tag ({tag_version}) does not match package version "
            f"({project_version})"
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="GitHub release tag, for example v0.9.2")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    errors = check_release_version(args.tag, args.root)
    if errors:
        print("release version check FAILED:")
        for error in errors:
            print(f" - {error}")
        return 1

    print(f"release version check passed: {args.tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
