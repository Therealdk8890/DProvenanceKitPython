"""Execute the e2e decision-path demo so it can't rot.

Asserts the product story: gate fails HIGH on the regressed candidate, and
attestation of the baseline verifies. Requires ``cryptography`` (``[crypto]`` /
``[dev]``).
"""

from __future__ import annotations

import runpy
import sys

import pytest

cryptography = pytest.importorskip("cryptography")


def test_e2e_decision_path_demo_gate_fail_and_attest_ok(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["examples.e2e_decision_path", "--output-dir", str(tmp_path), "--keep"],
    )

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("examples.e2e_decision_path", run_name="__main__")
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "GATE_RESULT=FAIL" in out
    assert "GATE_LEVEL=high" in out
    assert "ATTEST_VERIFY=valid" in out
    assert "DEMO_OK" in out
    assert "claimVerified" in out
    assert (tmp_path / "traces.sqlite").exists()
    assert (tmp_path / "baseline.sqlite").exists()
    assert (tmp_path / "baseline_attestation.json").exists()


def test_e2e_decision_path_module_entry(tmp_path, capsys):
    from examples.e2e_decision_path.run_demo import main

    code = main(["--output-dir", str(tmp_path), "--keep"])
    out = capsys.readouterr().out
    assert code == 0
    assert "GATE_RESULT=FAIL" in out
    assert "ATTEST_VERIFY=valid" in out
