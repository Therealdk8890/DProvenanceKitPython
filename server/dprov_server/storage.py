"""Pluggable per-project storage.

The server is store-agnostic: it only needs ``record`` / ``flush`` / ``query_runs`` and a
way to fetch one run by id. ``DPROV_STORAGE=memory`` (default) keeps everything in process;
``DPROV_STORAGE=sqlite`` persists one WAL SQLite file per project under ``DPROV_DATA_DIR``.
The two backends are held at **parity** by the library's own test suite, so the query and
regression-gate code is identical regardless of which is used.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Optional

from dprovenancekit import (
    AnyTraceableEvent,
    InMemoryTraceStore,
    SQLiteTraceStore,
    TraceQueryDSL,
)
from dprovenancekit.query import AndNode

#: An empty AND matches every run — the "list all runs" query.
ALL_RUNS = TraceQueryDSL(_root=AndNode(nodes=()))

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def make_store(name: str, *, storage: Optional[str] = None, data_dir: Optional[str] = None):
    """Build a store for a project. ``storage`` defaults to ``$DPROV_STORAGE`` or ``memory``."""
    storage = (storage or os.environ.get("DPROV_STORAGE", "memory")).lower()
    if storage == "sqlite":
        data_dir = data_dir or os.environ.get("DPROV_DATA_DIR", "./dprov-data")
        os.makedirs(data_dir, exist_ok=True)
        safe = _SAFE.sub("_", name) or "default"
        path = os.path.join(data_dir, f"{safe}.sqlite")
        # start_writer=False: the server flushes synchronously after each ingest, so no
        # per-project background thread is needed.
        return SQLiteTraceStore(AnyTraceableEvent, path, start_writer=False)
    return InMemoryTraceStore()


def flush(store) -> None:
    f = getattr(store, "flush", None)
    if callable(f):
        f()


def fetch_run(store, run_id: uuid.UUID):
    """Fetch one run by id, uniformly across backends.

    InMemoryTraceStore has a direct ``get_run``; SQLiteTraceStore does not (and there is no
    run-id query node), so fall back to scanning all runs. Fine for the MVP; a production
    store would index by run id.
    """
    get_run = getattr(store, "get_run", None)
    if callable(get_run):
        return get_run(run_id)
    for run in store.query_runs(ALL_RUNS):
        if run.run_id == run_id:
            return run
    return None
