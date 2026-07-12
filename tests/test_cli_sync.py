"""Tests for the ``dprovenancekit sync`` subcommand's db handling.

The sync client itself lives in the separate (closed-source) ``dprovenancekit_cloud``
distribution, so these tests install a stub for it in ``sys.modules`` to reach the
open-source db-opening path — the part this package owns and gates.
"""

from __future__ import annotations

import sys
import types
import uuid

from dprovenancekit.cli import main


def _install_fake_cloud(monkeypatch):
    """Register a stub ``dprovenancekit_cloud.sync_client.CloudSyncClient`` so ``_run_sync``
    gets past the ImportError guard and exercises the db-opening logic under test."""
    module = types.ModuleType("dprovenancekit_cloud")
    sync_client = types.ModuleType("dprovenancekit_cloud.sync_client")

    class CloudSyncClient:  # pragma: no cover - only the db guard is exercised here
        def push_run(self, *args, **kwargs):
            raise AssertionError("push_run should not be reached in these tests")

        def pull_run(self, *args, **kwargs):
            raise AssertionError("pull_run should not be reached in these tests")

    sync_client.CloudSyncClient = CloudSyncClient
    module.sync_client = sync_client
    monkeypatch.setitem(sys.modules, "dprovenancekit_cloud", module)
    monkeypatch.setitem(sys.modules, "dprovenancekit_cloud.sync_client", sync_client)


def test_sync_nonexistent_db_exits_2_without_creating_file(tmp_path, capsys, monkeypatch):
    # A typo'd --db path must error (exit 2) rather than have sqlite3.connect() create an
    # empty database behind the sync client's back.
    _install_fake_cloud(monkeypatch)
    missing = tmp_path / "typo.sqlite"
    code = main(["sync", "push", "--run", str(uuid.uuid4()), "--db", str(missing)])
    assert code == 2
    assert "no such database" in capsys.readouterr().err
    assert not missing.exists()  # guard must not leave a stray empty db


def test_sync_unopenable_db_exits_2(tmp_path, capsys, monkeypatch):
    # A directory exists but is not a usable SQLite file; previously this raised a raw
    # traceback — the sync command must now report a clean exit-2 error like its siblings.
    _install_fake_cloud(monkeypatch)
    code = main(["sync", "push", "--run", str(uuid.uuid4()), "--db", str(tmp_path)])
    assert code == 2
    assert "could not open database" in capsys.readouterr().err
