"""Isolated candidate SQLite store; separate file per run, not the SDK trace database."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Iterator, Optional

from .core import Chain, ChainBuilder, ChainError, Entry, Event, RunClose, _hash, _pairs, _uuid, genesis, verify

if TYPE_CHECKING:
    from .auth import SignedClose, Trace

# Provisional storage-specific bounds; not a change to the binary event format.
MAX_EVENTS = 4096
MAX_EVENT_BYTES = 2_097_152
MAX_TOTAL_BYTES = 67_108_864
MAX_CLOSE_METADATA_BYTES = 2_097_152
MAX_EDGES = 8192

_RUN_SQL = """
CREATE TABLE chain_run (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version = 1),
    head BLOB NOT NULL CHECK(typeof(head) = 'blob' AND length(head) = 32),
    event_count INTEGER NOT NULL CHECK(event_count >= 0),
    stored_bytes INTEGER NOT NULL CHECK(stored_bytes >= 0),
    state TEXT NOT NULL CHECK(state IN ('OPEN', 'CLOSED'))
)
"""
_ENTRY_SQL = """
CREATE TABLE chain_entry (
    chain_index INTEGER PRIMARY KEY CHECK(chain_index >= 0),
    event_id TEXT NOT NULL UNIQUE,
    event_json BLOB NOT NULL CHECK(typeof(event_json) = 'blob'),
    previous_hash BLOB NOT NULL CHECK(typeof(previous_hash) = 'blob' AND length(previous_hash) = 32),
    entry_hash BLOB NOT NULL CHECK(typeof(entry_hash) = 'blob' AND length(entry_hash) = 32)
)
"""
_CLOSE_SQL = """
CREATE TABLE chain_close (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    final_index INTEGER,
    final_head BLOB NOT NULL CHECK(typeof(final_head) = 'blob' AND length(final_head) = 32),
    event_count INTEGER NOT NULL CHECK(event_count >= 0),
    trace_digest BLOB NOT NULL CHECK(typeof(trace_digest) = 'blob' AND length(trace_digest) = 32),
    key_id BLOB NOT NULL CHECK(typeof(key_id) = 'blob' AND length(key_id) = 32),
    issued_at INTEGER NOT NULL,
    public_key BLOB NOT NULL CHECK(typeof(public_key) = 'blob' AND length(public_key) = 65),
    signature BLOB NOT NULL CHECK(typeof(signature) = 'blob' AND length(signature) BETWEEN 8 AND 72),
    trace_metadata BLOB NOT NULL CHECK(typeof(trace_metadata) = 'blob')
)
"""


def _metadata(trace: Trace) -> bytes:
    if len(trace.edges) > MAX_EDGES:
        raise ChainError("STORAGE_LIMIT")
    raw = json.dumps({"contextID": trace.contextID, "edges": [asdict(e) for e in trace.edges]},
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_CLOSE_METADATA_BYTES:
        raise ChainError("STORAGE_LIMIT")
    return raw


def _encoded(event: Event) -> bytes:
    # Envelope is only storage. The core hashes the exact payloadJSON UTF-8,
    # not this JSON representation. U64 event sequences remain JSON integers.
    raw = json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_EVENT_BYTES:
        raise ChainError("STORAGE_LIMIT")
    return raw


def _decoded(raw: bytes) -> Event:
    if not isinstance(raw, bytes) or len(raw) > MAX_EVENT_BYTES:
        raise ChainError("STORAGE_CORRUPT")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    if not isinstance(value, dict):
        raise ChainError("STORAGE_CORRUPT")
    event = Event(**value)
    if _encoded(event) != raw:
        raise ChainError("STORAGE_CORRUPT")
    return event


class SQLiteChainStore:
    """One explicitly initialized candidate chain; connection per operation.

    Writers serialize via BEGIN IMMEDIATE. Reverify the prefix on every append,
    not just on open; this is intentionally O(n) per append. A mutable local
    database cannot detect wholesale consistent replacement without a trusted
    external commitment. No capture pipeline or migration yet.
    """

    def __init__(self, path: Path, expected_run_id: str) -> None:
        self.path = path.resolve()
        self.run_id = str(_uuid(expected_run_id))

    @classmethod
    def create(cls, path: Path, run_id: str) -> "SQLiteChainStore":
        store = cls(path, run_id)
        # Exclusive file creation: never silently initialize an existing DB.
        fd = os.open(store.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with store._connection() as con:
            mode = con.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or mode[0] != "wal":
                raise ChainError("STORAGE_MODE")
            con.execute("BEGIN IMMEDIATE")
            con.execute(_RUN_SQL)
            con.execute(_ENTRY_SQL)
            con.execute(_CLOSE_SQL)
            con.execute("INSERT INTO chain_run VALUES (1, ?, 1, ?, 0, 0, 'OPEN')",
                        (store.run_id, genesis(store.run_id)))
            con.commit()
        return store

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        # mode=rw rejects missing paths. Opening is not initialization or repair.
        con = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                              isolation_level=None, timeout=10.0)
        try:
            con.execute("PRAGMA synchronous=FULL")
            con.execute("PRAGMA trusted_schema=OFF")
            yield con
        finally:
            if con.in_transaction:
                con.rollback()
            con.close()

    def _load(self, con: sqlite3.Connection) -> tuple[Chain, int, str]:
        try:
            mode = con.execute("PRAGMA journal_mode").fetchone()
            if mode is None or mode[0] != "wal":
                raise ChainError("STORAGE_MODE")
            rows = con.execute(
                "SELECT singleton, run_id, version, head, event_count, stored_bytes, state FROM chain_run"
            ).fetchmany(2)
            if len(rows) != 1 or rows[0][0] != 1:
                raise ChainError("STORAGE_CORRUPT")
            _, run_id, version, head, count, stored_bytes, state = rows[0]
            if state not in ("OPEN", "CLOSED"):
                raise ChainError("STORAGE_CORRUPT")
            close_rows = con.execute("SELECT count(*) FROM chain_close").fetchone()
            if close_rows is None or close_rows[0] != int(state == "CLOSED"):
                raise ChainError("STORAGE_CORRUPT")
            if run_id != self.run_id:
                raise ChainError("RUN_ID_MISMATCH")
            if (type(count) is not int or not 0 <= count <= MAX_EVENTS
                    or type(stored_bytes) is not int or not 0 <= stored_bytes <= MAX_TOTAL_BYTES):
                raise ChainError("STORAGE_CORRUPT")
            aggregate = con.execute(
                "SELECT count(*), coalesce(sum(length(event_json)), 0), "
                "coalesce(max(length(event_json)), 0) FROM chain_entry"
            ).fetchone()
            if aggregate is None or aggregate[:2] != (count, stored_bytes) or aggregate[2] > MAX_EVENT_BYTES:
                raise ChainError("STORAGE_CORRUPT")
            entries = []
            for index, identity, raw, previous, digest in con.execute(
                "SELECT chain_index, event_id, event_json, previous_hash, entry_hash "
                "FROM chain_entry ORDER BY chain_index"
            ):
                event = _decoded(raw)
                if str(_uuid(event.eventID)) != identity:
                    raise ChainError("STORAGE_CORRUPT")
                entries.append(Entry(index, event, previous, digest))
            chain = Chain(run_id, head, tuple(entries), version)
            verify(chain, self.run_id, expected_count=count)
            return chain, stored_bytes, state
        except ChainError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, RecursionError, OverflowError) as exc:
            raise ChainError("STORAGE_CORRUPT") from exc

    def load(self, *, expected_head: Optional[bytes] = None,
             expected_count: Optional[int] = None) -> Chain:
        """Read a consistent snapshot; external commitments must be independently trusted."""
        with self._connection() as con:
            con.execute("BEGIN")
            chain, _, state = self._load(con)
            if state == "CLOSED":
                # Internal consistency only. Use load_closed with a trusted pin for authentication.
                self._read_closed(con, chain, trusted_key_id=None)
            if expected_head is not None or expected_count is not None:
                verify(chain, self.run_id, expected_head=expected_head, expected_count=expected_count)
            con.commit()
            return chain

    def _advance(self, con: sqlite3.Connection, previous: Chain, entry: Entry,
                 old_bytes: int, new_bytes: int) -> None:
        result = con.execute(
            "UPDATE chain_run SET head=?, event_count=?, stored_bytes=? "
            "WHERE singleton=1 AND run_id=? AND version=1 AND head=? AND event_count=? AND stored_bytes=? AND state='OPEN'",
            (entry.entryHash, entry.chainIndex + 1, new_bytes, self.run_id,
             previous.head, len(previous.entries), old_bytes),
        )
        if result.rowcount != 1:
            raise ChainError("STORAGE_CAS_MISMATCH")

    def append(self, event: Event, *, expected_head: Optional[bytes] = None) -> Entry:
        with self._connection() as con:
            con.execute("BEGIN IMMEDIATE")
            chain, old_bytes, state = self._load(con)
            if state != "OPEN":
                raise ChainError("RUN_CLOSED")
            if expected_head is not None:
                verify(chain, self.run_id, expected_head=expected_head)
            if len(chain.entries) >= MAX_EVENTS:
                raise ChainError("STORAGE_LIMIT")
            writer = ChainBuilder.from_chain(chain, self.run_id)
            entry = writer.append(event)
            raw = _encoded(event)
            new_bytes = old_bytes + len(raw)
            if new_bytes > MAX_TOTAL_BYTES:
                raise ChainError("STORAGE_LIMIT")
            con.execute("INSERT INTO chain_entry VALUES (?, ?, ?, ?, ?)",
                        (entry.chainIndex, str(_uuid(event.eventID)), raw,
                         entry.previousHash, entry.entryHash))
            self._advance(con, chain, entry, old_bytes, new_bytes)
            con.commit()
            return entry


    def _mark_closed(self, con: sqlite3.Connection, chain: Chain) -> None:
        result = con.execute(
            "UPDATE chain_run SET state='CLOSED' WHERE singleton=1 AND run_id=? "
            "AND version=1 AND state='OPEN' AND head=? AND event_count=?",
            (self.run_id, chain.head, len(chain.entries)),
        )
        if result.rowcount != 1:
            raise ChainError("STORAGE_CAS_MISMATCH")

    def seal(self, trace: Trace, signed: SignedClose, *, trusted_key_id: bytes,
             require_evidence: bool = False) -> None:
        """Persist an externally signed close; verify current state under the write lock.

        Sign outside this transaction. A concurrent append invalidates the stale
        commitment. Never infer trust from the embedded key or sign under a DB lock.
        """
        from .auth import verify_authentication

        _hash(trusted_key_id)
        metadata = _metadata(trace)
        with self._connection() as con:
            con.execute("BEGIN IMMEDIATE")
            chain, _, state = self._load(con)
            if state != "OPEN":
                raise ChainError("RUN_CLOSED")
            verify_authentication(chain, trace, signed, self.run_id, trusted_key_id=trusted_key_id,
                                  require_evidence=require_evidence)
            close = signed.close
            con.execute(
                "INSERT INTO chain_close VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (close.finalChainIndex, close.finalChainHead, close.eventCount, close.traceDigest,
                 close.keyID, close.issuedAtUnixMicroseconds, signed.publicKeyX963,
                 signed.signatureDER, metadata),
            )
            self._mark_closed(con, chain)
            con.commit()

    def _read_closed(self, con: sqlite3.Connection, chain: Chain, *,
                     trusted_key_id: Optional[bytes], require_evidence: bool = False
                     ) -> tuple[Trace, SignedClose]:
        from .auth import Edge, SignedClose, Trace, verify_authentication

        try:
            lengths = con.execute(
                "SELECT length(trace_metadata) FROM chain_close WHERE singleton=1"
            ).fetchone()
            if lengths is None or type(lengths[0]) is not int or not 0 <= lengths[0] <= MAX_CLOSE_METADATA_BYTES:
                raise ChainError("STORAGE_CORRUPT")
            row = con.execute(
                "SELECT final_index, final_head, event_count, trace_digest, key_id, issued_at, "
                "public_key, signature, trace_metadata FROM chain_close WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise ChainError("STORAGE_CORRUPT")
            index, head, count, digest, key, issued, public, signature, raw = row
            if not isinstance(raw, bytes):
                raise ChainError("STORAGE_CORRUPT")
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
            if (not isinstance(value, dict) or set(value) != {"contextID", "edges"}
                    or not isinstance(value["contextID"], str) or not isinstance(value["edges"], list)
                    or len(value["edges"]) > MAX_EDGES):
                raise ChainError("STORAGE_CORRUPT")
            trace = Trace(self.run_id, value["contextID"], tuple(e.event for e in chain.entries),
                          tuple(Edge(**edge) for edge in value["edges"]))
            if _metadata(trace) != raw:
                raise ChainError("STORAGE_CORRUPT")
            signed = SignedClose(RunClose(self.run_id, 1, index, head, count, digest, key, issued),
                                 public, signature)
            # Embedded key ID is permissible ONLY for internal consistency in load().
            # load_closed requires an external pin and returns an authenticated result.
            pin = _hash(key) if trusted_key_id is None else _hash(trusted_key_id)
            verify_authentication(chain, trace, signed, self.run_id, trusted_key_id=pin,
                                  require_evidence=require_evidence)
            return trace, signed
        except ChainError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, RecursionError, OverflowError) as exc:
            raise ChainError("STORAGE_CORRUPT") from exc

    def load_closed(self, *, trusted_key_id: bytes, require_evidence: bool = False,
                    expected_head: Optional[bytes] = None
                    ) -> tuple[Chain, Trace, SignedClose]:
        """Reopen only an authenticated closed run. Pin is independently provisioned."""
        _hash(trusted_key_id)
        with self._connection() as con:
            con.execute("BEGIN")
            chain, _, state = self._load(con)
            if state != "CLOSED":
                raise ChainError("RUN_NOT_CLOSED")
            trace, signed = self._read_closed(con, chain, trusted_key_id=trusted_key_id,
                                              require_evidence=require_evidence)
            if expected_head is not None:
                verify(chain, self.run_id, expected_head=expected_head)
            con.commit()
            return chain, trace, signed
