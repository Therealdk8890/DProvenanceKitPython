"""Pytest plugin for DProvenanceKit.

Provides fixtures and rich formatting for regression testing.
To use, ensure the plugin is loaded (which happens automatically if installed).
"""

import pytest

from .testing import RegressionGate, RegressionError


def pytest_exception_interact(node, call, report):
    """Custom exception formatting for RegressionError."""
    if call.excinfo and issubclass(call.excinfo.type, RegressionError):
        # We could format it further if we want, but RegressionError
        # already provides a nice summary().
        pass


@pytest.fixture
def dprovenance_gate():
    """Returns a fresh RegressionGate configured with default strictness.
    
    Usage:
        def test_agent_regression(dprovenance_gate, golden_run, candidate_run):
            dprovenance_gate.assert_no_regression(golden_run, candidate_run)
    """
    return RegressionGate()


@pytest.fixture
def dprovenance_strict_gate():
    """Returns a RegressionGate that strictly fails on any divergence."""
    return RegressionGate(allow_divergent_steps=False)


@pytest.fixture
def dprovenance_relaxed_gate():
    """Returns a RegressionGate that allows benign divergent steps as long as the severity level is acceptable."""
    return RegressionGate(allow_divergent_steps=True)

# git-blob-rewrite
