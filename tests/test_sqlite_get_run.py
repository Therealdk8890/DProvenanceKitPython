"""SQLiteTraceStore.get_run — single-run fetch by id, indexed on run_id (no full scan)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from dprovenancekit import DProvenanceKit, SQLiteTraceStore, TraceableEvent, TracePriority


@dataclass(frozen=True)
class _E(TraceableEvent):
    kind: str

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        return TracePriority.CRITICAL

    def to_dict(self) -> dict:
        return {"kind": self.kind}

    @classmethod
    def from_dict(cls, data: dict) -> "_E":
        return cls(kind=data["kind"])


def test_get_run_returns_run_by_id(tmp_path):
    store = SQLiteTraceStore(_E, str(tmp_path / "t.sqlite"), start_writer=False)
    kit = DProvenanceKit(_E)
    with kit.run(context_id="c", store=store) as run:
        kit.record(_E("a"))
        kit.record(_E("b"))

    got = store.get_run(run.run_id)  # flushes, then indexed fetch
    assert got is not None
    assert got.context_id == "c"
    assert [e.payload.type_identifier for e in got.events] == ["a", "b"]


def test_get_run_missing_is_none(tmp_path):
    store = SQLiteTraceStore(_E, str(tmp_path / "t.sqlite"), start_writer=False)
    assert store.get_run(uuid.uuid4()) is None

# git-blob-rewrite
