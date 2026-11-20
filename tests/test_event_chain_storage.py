"""Persistence acceptance tests against real SQLite, separate connections and processes."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from experimental.event_chain.core import ChainError, Event, build
from experimental.event_chain.storage import SQLiteChainStore

FIXTURE = Path(__file__).resolve().parents[1] / "conformance/vectors/event_chain_v1_candidate.json"
CASE = json.loads(FIXTURE.read_text(encoding="utf-8"))["positiveCases"][2]
RUN = CASE["runID"]
EVENTS = tuple(Event(**entry["event"]) for entry in CASE["entries"])


def test_roundtrip_and_explicit_creation(tmp_path):
    path = tmp_path / "run.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        SQLiteChainStore(path, RUN).load()
    assert not path.exists()
    store = SQLiteChainStore.create(path, RUN)
    assert store.load() == build(RUN, [])
    for event in EVENTS:
        store.append(event)
    expected = build(RUN, EVENTS)
    assert SQLiteChainStore(path, RUN.upper()).load(expected_head=expected.head, expected_count=3) == expected
    with pytest.raises(FileExistsError):
        SQLiteChainStore.create(path, RUN)
    assert store.load() == expected


def test_full_unsigned_sequence_range(tmp_path):
    store = SQLiteChainStore.create(tmp_path / "run.sqlite", RUN)
    event = replace(EVENTS[0], eventSequence=2**64 - 1)
    store.append(event)
    assert store.load().entries[0].event == event


def test_invalid_and_stale_append_do_not_mutate(tmp_path):
    store = SQLiteChainStore.create(tmp_path / "run.sqlite", RUN)
    old = store.load()
    store.append(EVENTS[0])
    expected = store.load()
    with pytest.raises(ChainError, match="CLOSURE_MISMATCH"):
        store.append(EVENTS[1], expected_head=old.head)
    with pytest.raises(ChainError, match="DUPLICATE_EVENT_ID"):
        store.append(EVENTS[0])
    assert store.load() == expected
    store.append(EVENTS[1], expected_head=expected.head)


def test_sqlite_failure_rolls_back_insert(tmp_path, monkeypatch):
    path = tmp_path / "run.sqlite"
    store = SQLiteChainStore.create(path, RUN)
    store.append(EVENTS[0])
    prefix = store.load()

    def fail(*args):
        raise sqlite3.OperationalError("injected write failure after insert")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_advance", fail)
        with pytest.raises(sqlite3.OperationalError):
            store.append(EVENTS[1])
    assert store.load() == prefix
    store.append(EVENTS[1])
    assert store.load() == build(RUN, EVENTS[:2])


@pytest.mark.parametrize("phase", ["before_head", "after_head"])
def test_process_exit_during_transaction(tmp_path, phase):
    path = tmp_path / "run.sqlite"
    store = SQLiteChainStore.create(path, RUN)
    store.append(EVENTS[0])
    before = store.load()
    code = """
import json
import os
from pathlib import Path
import sys
from experimental.event_chain.core import Event
from experimental.event_chain.storage import SQLiteChainStore
store = SQLiteChainStore(Path(sys.argv[1]), sys.argv[2])
original = store._advance
def interrupted(*args):
    if sys.argv[4] == 'after_head':
        original(*args)
    os._exit(91)
store._advance = interrupted
store.append(Event(**json.loads(sys.argv[3])))
raise SystemExit(92)
"""
    result = subprocess.run([sys.executable, "-c", code, str(path), RUN,
                             json.dumps(CASE["entries"][1]["event"]), phase],
                            cwd=FIXTURE.parents[2], timeout=30, check=False)
    assert result.returncode == 91
    assert SQLiteChainStore(path, RUN).load(expected_head=before.head, expected_count=1) == before
    store.append(EVENTS[1])
    assert store.load() == build(RUN, EVENTS[:2])


def test_concurrent_writers_same_sequence(tmp_path):
    path = tmp_path / "run.sqlite"
    store = SQLiteChainStore.create(path, RUN)
    store.append(EVENTS[0])
    barrier = Barrier(2)
    a = EVENTS[1]
    b = replace(a, eventID="00000000-0000-4000-8000-000000000099")

    def append(event):
        barrier.wait(timeout=10)
        try:
            SQLiteChainStore(path, RUN).append(event)
            return "accepted"
        except ChainError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, [a, b]))
    assert sorted(results) == ["SEQUENCE_NONMONOTONIC", "accepted"]
    chain = store.load()
    assert len(chain.entries) == 2
    assert chain.entries[-1].event in (a, b)
    store.append(EVENTS[2])
    assert len(store.load().entries) == 3


@pytest.mark.parametrize("mutation", [
    "DELETE FROM chain_entry WHERE chain_index=0",
    "UPDATE chain_entry SET entry_hash=zeroblob(32) WHERE chain_index=0",
    "UPDATE chain_entry SET event_id='bad' WHERE chain_index=0",
    "UPDATE chain_run SET head=zeroblob(32)",
    "UPDATE chain_run SET event_count=event_count+1",
    "UPDATE chain_entry SET event_json=x'ff' WHERE chain_index=0",
])
def test_corrupt_storage_rejects_read_and_append(tmp_path, mutation):
    path = tmp_path / "run.sqlite"
    store = SQLiteChainStore.create(path, RUN)
    store.append(EVENTS[0])
    with sqlite3.connect(path) as con:
        con.execute(mutation)
    with pytest.raises(ChainError):
        store.load()
    with pytest.raises(ChainError):
        store.append(EVENTS[1])


def test_consistent_replacement_requires_external_commitment(tmp_path):
    path = tmp_path / "run.sqlite"
    store = SQLiteChainStore.create(path, RUN)
    for event in EVENTS:
        store.append(event)
    original = store.load()
    other = SQLiteChainStore.create(tmp_path / "replacement.sqlite", RUN)
    for event in (replace(EVENTS[0], payloadJSON="{}"), *EVENTS[1:]):
        other.append(event)
    assert other.load().head != original.head
    with pytest.raises(ChainError, match="CLOSURE_MISMATCH"):
        other.load(expected_head=original.head, expected_count=3)


def test_provisional_storage_caps(tmp_path, monkeypatch):
    from experimental.event_chain import storage

    store = SQLiteChainStore.create(tmp_path / "run.sqlite", RUN)
    before = store.load()
    with monkeypatch.context() as patch:
        patch.setattr(storage, "MAX_EVENT_BYTES", 8)
        with pytest.raises(ChainError, match="STORAGE_LIMIT"):
            store.append(EVENTS[0])
    assert store.load() == before
    with monkeypatch.context() as patch:
        patch.setattr(storage, "MAX_TOTAL_BYTES", 8)
        with pytest.raises(ChainError, match="STORAGE_LIMIT"):
            store.append(EVENTS[0])
    assert store.load() == before
    store.append(EVENTS[0])
    with monkeypatch.context() as patch:
        patch.setattr(storage, "MAX_EVENTS", 1)
        with pytest.raises(ChainError, match="STORAGE_LIMIT"):
            store.append(EVENTS[1])
    assert store.load() == build(RUN, EVENTS[:1])


def signed_prefix(store, key):
    from hashlib import sha256
    from experimental.event_chain.auth import Trace, Edge, sign_close

    chain = store.load()
    events = tuple(entry.event for entry in chain.entries)
    edges = (Edge(events[0].eventID, events[1].eventID, "derivedFrom"),) if len(events) > 1 else ()
    trace = Trace(RUN, events[0].contextID if events else "empty", events, edges)
    return chain, trace, sign_close(chain, trace, RUN, 123, key), sha256(key.public_key_x963_representation).digest()


def test_persistent_signed_close(tmp_path):
    from experimental.event_chain.exchange import TestKey, pin

    path = tmp_path / "run.sqlite"
    store = SQLiteChainStore.create(path, RUN)
    for event in EVENTS[:2]:
        store.append(event)
    chain, trace, signed, trusted = signed_prefix(store, TestKey(1))
    with pytest.raises(ChainError, match="RUN_NOT_CLOSED"):
        store.load_closed(trusted_key_id=trusted)
    store.seal(trace, signed, trusted_key_id=trusted, require_evidence=True)
    reopened = SQLiteChainStore(path, RUN)
    assert reopened.load_closed(trusted_key_id=trusted, expected_head=chain.head) == (chain, trace, signed)
    assert reopened.load() == chain
    with pytest.raises(ChainError, match="UNTRUSTED_KEY"):
        reopened.load_closed(trusted_key_id=pin(2))
    with pytest.raises(ChainError, match="RUN_CLOSED"):
        reopened.append(EVENTS[2])
    with pytest.raises(ChainError, match="RUN_CLOSED"):
        reopened.seal(trace, signed, trusted_key_id=trusted)


def test_seal_stale_or_untrusted_does_not_close(tmp_path):
    from experimental.event_chain.exchange import TestKey, pin

    store = SQLiteChainStore.create(tmp_path / "run.sqlite", RUN)
    store.append(EVENTS[0])
    _, trace, signed, trusted = signed_prefix(store, TestKey(1))
    with pytest.raises(ChainError, match="UNTRUSTED_KEY"):
        store.seal(trace, signed, trusted_key_id=pin(2))
    store.append(EVENTS[1])
    with pytest.raises(ChainError, match="CLOSURE_MISMATCH"):
        store.seal(trace, signed, trusted_key_id=trusted)
    store.append(EVENTS[2])


def test_empty_close_policy(tmp_path):
    from experimental.event_chain.exchange import TestKey

    store = SQLiteChainStore.create(tmp_path / "run.sqlite", RUN)
    _, trace, signed, trusted = signed_prefix(store, TestKey(1))
    with pytest.raises(ChainError, match="EMPTY_CHAIN"):
        store.seal(trace, signed, trusted_key_id=trusted, require_evidence=True)
    store.seal(trace, signed, trusted_key_id=trusted)
    with pytest.raises(ChainError, match="EMPTY_CHAIN"):
        store.load_closed(trusted_key_id=trusted, require_evidence=True)


def test_seal_append_race(tmp_path):
    from experimental.event_chain.exchange import TestKey

    path = tmp_path / "run.sqlite"
    store = SQLiteChainStore.create(path, RUN)
    store.append(EVENTS[0])
    chain, trace, signed, trusted = signed_prefix(store, TestKey(1))
    barrier = Barrier(2)

    def close():
        barrier.wait(timeout=10)
        try:
            SQLiteChainStore(path, RUN).seal(trace, signed, trusted_key_id=trusted)
            return "closed"
        except ChainError as exc:
            return str(exc)

    def append_new():
        barrier.wait(timeout=10)
        try:
            SQLiteChainStore(path, RUN).append(EVENTS[1])
            return "appended"
        except ChainError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = (pool.submit(close), pool.submit(append_new))
        closed, appended = (item.result(timeout=20) for item in results)
    if closed == "closed":
        assert appended == "RUN_CLOSED"
        assert store.load_closed(trusted_key_id=trusted)[0] == chain
    else:
        assert (closed, appended) == ("CLOSURE_MISMATCH", "appended")
        assert len(store.load().entries) == 2
        with pytest.raises(ChainError, match="RUN_NOT_CLOSED"):
            store.load_closed(trusted_key_id=trusted)


@pytest.mark.parametrize("phase", ["before_marker", "after_marker"])
def test_process_exit_during_seal(tmp_path, phase):
    from experimental.event_chain.exchange import TestKey

    path = tmp_path / "run.sqlite"
    store = SQLiteChainStore.create(path, RUN)
    store.append(EVENTS[0])
    chain, trace, signed, trusted = signed_prefix(store, TestKey(1))
    code = """
from pathlib import Path
import os
import sys
from experimental.event_chain.auth import Trace, sign_close
from experimental.event_chain.exchange import TestKey, pin
from experimental.event_chain.storage import SQLiteChainStore
store = SQLiteChainStore(Path(sys.argv[1]), sys.argv[2])
chain = store.load()
events = tuple(e.event for e in chain.entries)
trace = Trace(sys.argv[2], events[0].contextID, events)
signed = sign_close(chain, trace, sys.argv[2], 123, TestKey(1))
original = store._mark_closed
def interrupted(*args):
    if sys.argv[3] == "after_marker":
        original(*args)
    os._exit(93)
store._mark_closed = interrupted
store.seal(trace, signed, trusted_key_id=pin(1))
raise SystemExit(94)
"""
    result = subprocess.run([sys.executable, "-c", code, str(path), RUN, phase],
                            cwd=FIXTURE.parents[2], timeout=30, check=False)
    assert result.returncode == 93
    assert store.load() == chain
    with pytest.raises(ChainError, match="RUN_NOT_CLOSED"):
        store.load_closed(trusted_key_id=trusted)
    store.seal(trace, signed, trusted_key_id=trusted)
    assert store.load_closed(trusted_key_id=trusted)[2] == signed


@pytest.mark.parametrize("mutation", [
    "DELETE FROM chain_close",
    "UPDATE chain_run SET state='OPEN'",
    "UPDATE chain_close SET signature=zeroblob(64)",
    "UPDATE chain_close SET trace_digest=zeroblob(32)",
    "UPDATE chain_close SET trace_metadata=x'ff'",
])
def test_closed_storage_corruption(tmp_path, mutation):
    from experimental.event_chain.exchange import TestKey

    path = tmp_path / "run.sqlite"
    store = SQLiteChainStore.create(path, RUN)
    store.append(EVENTS[0])
    _, trace, signed, trusted = signed_prefix(store, TestKey(1))
    store.seal(trace, signed, trusted_key_id=trusted)
    with sqlite3.connect(path) as con:
        con.execute(mutation)
    with pytest.raises(ChainError):
        store.load_closed(trusted_key_id=trusted)
    with pytest.raises(ChainError):
        store.load()
    with pytest.raises(ChainError):
        store.append(EVENTS[1])
