"""Top-level CLI dispatch: unknown commands must fail loudly.

This CLI gates CI builds; a mistyped subcommand that exits 0 silently passes the
pipeline forever, so unknown commands exit 2 with usage on stderr.
"""

from __future__ import annotations

from dprovenancekit import __version__
from dprovenancekit.cli import main


def test_unknown_subcommand_exits_2_with_usage_on_stderr(capsys):
    code = main(["gaet"])  # the classic typo of "gate"
    captured = capsys.readouterr()
    assert code == 2
    assert "unknown command 'gaet'" in captured.err
    assert "Usage: dprovenancekit" in captured.err
    assert captured.out == ""  # nothing on stdout that a pipeline could mistake


def test_help_exits_0_and_lists_commands(capsys):
    code = main(["--help"])
    captured = capsys.readouterr()
    assert code == 0
    for command in ("demo", "gate", "anomalies", "runs", "ui", "ingest", "export", "sync"):
        assert command in captured.out


def test_version_exits_0_and_prints_version(capsys):
    code = main(["--version"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == __version__


def test_short_version_exits_0_and_prints_version(capsys):
    code = main(["-V"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == __version__
