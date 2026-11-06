"""Tests for the release tag/package version consistency gate."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_release_version.py"


def _run_check(tmp_path: Path, tag: str, project: str = "0.9.2", fallback: str = "0.9.2"):
    package = tmp_path / "dprovenancekit"
    package.mkdir(exist_ok=True)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "dprovenancekit"\nversion = "{project}"\n',
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        f'__version__ = "{fallback}"\n',
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), tag, "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_matching_release_tag_and_package_versions_pass(tmp_path):
    result = _run_check(tmp_path, "v0.9.2")
    assert result.returncode == 0
    assert "release version check passed: v0.9.2" in result.stdout


def test_release_tag_mismatch_fails(tmp_path):
    result = _run_check(tmp_path, "v0.9.1")
    assert result.returncode == 1
    assert "release tag (0.9.1) does not match package version (0.9.2)" in result.stdout


def test_metadata_and_source_fallback_mismatch_fails(tmp_path):
    result = _run_check(tmp_path, "v0.9.2", fallback="0.7.1")
    assert result.returncode == 1
    assert "does not match __version__ fallback (0.7.1)" in result.stdout


def test_non_version_tag_format_fails(tmp_path):
    result = _run_check(tmp_path, "release-0.9.2")
    assert result.returncode == 1
    assert "release tag must use the vX.Y.Z form" in result.stdout
