"""Execute the regression-testing example so it can't rot.

The example in ``examples/regression_testing.py`` self-asserts its end-to-end story
(clean re-run passes; a skipped safety step is caught as a HIGH regression). Running it
under ``__main__`` here means any drift in the public API or alignment behavior fails
this test rather than silently breaking the example.
"""

from __future__ import annotations

import os
import runpy


def test_regression_example_runs_and_self_asserts(capsys):
    path = os.path.join(os.path.dirname(__file__), "..", "examples", "regression_testing.py")
    runpy.run_path(path, run_name="__main__")
    out = capsys.readouterr().out
    assert "HIGH regression" in out

# git-blob-rewrite
