"""Pytest plugin for DProvenanceKit.

Provides fixtures and rich formatting for regression testing.
To use, ensure the plugin is loaded (which happens automatically if installed).

The headline fixture is :func:`golden_trace` — snapshot testing for reasoning traces::

    def test_research_agent(golden_trace):
        with golden_trace("research-agent"):
            run_my_agent()   # anything using @traced / record_event / an adapter

    # First time (and whenever the change is intentional):
    #   pytest --dprov-update-golden     -> records tests/goldens/research-agent.sqlite
    # Every run after that gates the recorded candidate against that golden baseline
    # and fails the test if the reasoning drifted (dropped/added/reordered steps).
"""

import hashlib
import os
import re
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from .alignment_models import RegressionLevel
from .event import AnyTraceableEvent
from .instrument import traced_run, TracedEvent
from .sqlite_store import SQLiteTraceStore
from .testing import RegressionGate, RegressionError


def pytest_addoption(parser):
    group = parser.getgroup("dprovenancekit")
    group.addoption(
        "--dprov-update-golden",
        action="store_true",
        default=False,
        help="(re)record golden baselines for golden_trace tests instead of gating",
    )
    parser.addini(
        "dprov_golden_dir",
        default="tests/goldens",
        help="directory (relative to rootdir) where golden_trace baselines are stored",
    )


def pytest_exception_interact(node, call, report):
    """Custom exception formatting for RegressionError."""
    if call.excinfo and issubclass(call.excinfo.type, RegressionError):
        # We could format it further if we want, but RegressionError
        # already provides a nice summary().
        pass


_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# Session-wide registry of claimed baseline files: resolved path -> (name, nodeid).
# Two golden_trace uses mapping to the same file would silently record
# last-writer-wins baselines (and then gate one block against the other's run),
# so any second claim fails loudly instead.
_CLAIMED_BASELINES = pytest.StashKey()


class GoldenTrace:
    """Records the ``with`` block into a run, then gates it against a pinned baseline.

    The block always records into a temporary SQLite file. In update mode
    (``--dprov-update-golden``) a cleanly-recorded run is then atomically promoted to
    the baseline file — a block that raises leaves the committed baseline untouched.
    Otherwise the recording is the *candidate*: both it and the baseline are loaded
    back from disk identically (as canonical runs) and :class:`RegressionGate` fails
    the test on drift.
    """

    def __init__(self, name, golden_path, update, gate, notify):
        self.name = name
        self.golden_path = Path(golden_path)
        self.update = update
        self._gate = gate
        self._notify = notify
        self.run = None  # the ActiveTraceRun, for wiring framework adapters
        self._store = None
        self._run_cm = None
        self._tmpdir = None

    def __enter__(self):
        # Recording always targets a temp file. Loading golden and candidate back
        # through the same SQLite path keeps their representations symmetric (an
        # in-memory typed run vs. a from-disk run makes every step look "changed"),
        # and update mode only touches the committed baseline after a clean exit.
        self._tmpdir = tempfile.TemporaryDirectory(prefix="dprov-golden-")
        self._record_path = Path(self._tmpdir.name) / "candidate.sqlite"
        self._store = SQLiteTraceStore(TracedEvent, str(self._record_path))
        self._run_cm = traced_run(self._store, context_id=self.name)
        self.run = self._run_cm.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._run_cm.__exit__(exc_type, exc, tb)
        self._store.close()  # final durability for the just-recorded run
        try:
            if exc_type is not None:
                return False  # the block itself failed; report that, not the gate
            if self.update:
                self._promote_to_baseline()
                self._notify(
                    f"golden baseline for '{self.name}' recorded at "
                    f"{self.golden_path} — commit this file"
                )
                return False
            if not self.golden_path.exists():
                pytest.fail(
                    f"no golden baseline for '{self.name}' at {self.golden_path}.\n"
                    f"Record one with: pytest --dprov-update-golden",
                    pytrace=False,
                )
            candidate = self._load_run(
                self._record_path,
                f"golden_trace('{self.name}') recorded no run — did the block "
                f"execute any traced code?",
            )
            golden = self._load_run(
                self.golden_path,
                f"golden file {self.golden_path} holds no run for context "
                f"'{self.name}' — re-record with: pytest --dprov-update-golden",
            )
            self._gate.assert_no_regression(golden, candidate)
        finally:
            if self._tmpdir is not None:
                self._tmpdir.cleanup()
        return False

    def _promote_to_baseline(self):
        self.golden_path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            stale = Path(str(self.golden_path) + suffix)
            if stale.exists():
                stale.unlink()
        os.replace(self._record_path, self.golden_path)

    def _load_run(self, path, missing_message):
        try:
            store = SQLiteTraceStore(AnyTraceableEvent, str(path), start_writer=False)
        except (sqlite3.Error, OSError) as exc:
            pytest.fail(
                f"could not open trace db {path}: {exc}\n"
                f"If this is the golden baseline for '{self.name}', re-record it "
                f"with: pytest --dprov-update-golden",
                pytrace=False,
            )
        try:
            # list_run_metadata is newest-first; a well-formed file has one run.
            # Skip rows whose run_id fails to parse (foreign or corrupted db).
            for row in store.list_run_metadata():
                if row.context_id != self.name:
                    continue
                try:
                    run_id = uuid.UUID(row.run_id)
                except ValueError:
                    continue
                return store.get_run(run_id)
            pytest.fail(missing_message, pytrace=False)
        finally:
            store.close()


@pytest.fixture
def golden_trace(request):
    """Snapshot testing for reasoning traces: record a block, gate it against a baseline.

    Usage::

        def test_agent(golden_trace):
            with golden_trace("research-agent"):
                run_my_agent()

    Baselines live under the ``dprov_golden_dir`` ini directory (default
    ``tests/goldens``), one SQLite file per name — names must be unique across the
    test session. Record or intentionally update a baseline with
    ``pytest --dprov-update-golden``; normal runs gate against it and fail on
    reasoning drift. The context manager exposes ``.run`` (the active recording run)
    for wiring framework adapters inside the block.

    Keyword arguments are forwarded to :class:`~dprovenancekit.testing.RegressionGate`
    (``max_regression_level`` also accepts its string form, e.g. ``"high"``).
    """
    config = request.config
    update = config.getoption("--dprov-update-golden")
    base = Path(config.rootpath) / config.getini("dprov_golden_dir")
    reporter = config.pluginmanager.get_plugin("terminalreporter")

    def notify(message):
        if reporter is not None:
            reporter.write_line(f"[dprovenancekit] {message}")
        else:  # pragma: no cover - terminal reporter is present in normal runs
            print(f"[dprovenancekit] {message}")

    def factory(name, **gate_kwargs):
        level = gate_kwargs.get("max_regression_level")
        if isinstance(level, str):
            gate_kwargs["max_regression_level"] = RegressionLevel(level)
        safe = _UNSAFE_NAME_CHARS.sub("-", name)
        if safe != name:
            # Keep sanitized filenames injective: distinct names must never share
            # a baseline file.
            digest = hashlib.sha256(name.encode()).hexdigest()[:8]
            safe = f"{safe}-{digest}"
        path = base / f"{safe}.sqlite"

        claimed = config.stash.setdefault(_CLAIMED_BASELINES, {})
        holder = claimed.get(path)
        if holder is not None:
            pytest.fail(
                f"golden_trace name collision: '{name}' ({request.node.nodeid}) and "
                f"'{holder[0]}' ({holder[1]}) both map to {path}.\n"
                f"Golden names must be unique per test session.",
                pytrace=False,
            )
        claimed[path] = (name, request.node.nodeid)

        return GoldenTrace(
            name, path, update, RegressionGate(**gate_kwargs), notify
        )

    return factory


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
