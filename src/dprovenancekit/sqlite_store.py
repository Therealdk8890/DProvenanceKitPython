"""Durable, WAL-mode SQLite trace store with a non-blocking background writer.

Writes land in the in-memory :class:`~dprovenancekit.write_buffer.TraceWriteBuffer` and
are drained in adaptive batches by a background thread into WAL-mode SQLite. ``record``
never blocks; ``flush`` is a true barrier. A failed batch insert and an unencodable
payload are both counted by tier (via :class:`~dprovenancekit.drop_stats.TraceDropTally`)
so neither thins a run silently.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Type

from .drop_stats import TraceDropStats, TraceDropTally
from .edge import TraceEdge, TraceEdgeType
from .event import RunRow, TraceableEvent, TraceEvent, TraceEventRow
from .priority import TracePriority
from .query import TraceQueryCompiler, TraceQueryDSL, TraceRun
from .store import TraceStore
from .write_buffer import TraceWriteBuffer


class SQLiteConnection:
    """Thread-safe wrapper over a single sqlite3 connection.

    All access is serialized through a re-entrant lock, the Python analogue of opening
    the Swift connection with ``SQLITE_OPEN_FULLMUTEX`` and a serial queue. WAL mode with
    ``synchronous=NORMAL`` and ``temp_store=MEMORY`` matches the Swift pragmas.
    """

    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.execute("PRAGMA journal_mode=WAL;")
        self.execute("PRAGMA synchronous=NORMAL;")
        self.execute("PRAGMA temp_store=MEMORY;")

    def execute(self, sql: str, params=()) -> None:
        with self._lock:
            self._db.execute(sql, params)

    def query(self, sql: str, params=()) -> List[tuple]:
        with self._lock:
            cur = self._db.execute(sql, params)
            return cur.fetchall()

    def executemany(self, sql: str, seq_of_params) -> None:
        with self._lock:
            self._db.executemany(sql, seq_of_params)

    @contextmanager
    def transaction(self):
        with self._lock:
            self._db.execute("BEGIN;")
            try:
                yield
                self._db.execute("COMMIT;")
            except Exception:
                self._db.execute("ROLLBACK;")
                raise

    @property
    def user_version(self) -> int:
        rows = self.query("PRAGMA user_version;")
        return int(rows[0][0]) if rows else 0

    @user_version.setter
    def user_version(self, value: int) -> None:
        self.execute(f"PRAGMA user_version = {int(value)};")

    def close(self) -> None:
        with self._lock:
            self._db.close()


@dataclass
class _RunState:
    context_id: str
    start_time: int
    latest_time: int
    event_count: int
    fingerprint_hash: "hashlib._Hash"
    is_dirty: bool


class SQLiteWriter:
    """Background writer that drains the buffer and executes batched INSERTs.

    Serialized through a re-entrant lock (the Python analogue of the Swift actor), so
    ``flush`` and the background tick never corrupt the run-state cache. Run metadata is
    folded in only *after* a successful commit, so a rolled-back batch never inflates
    ``event_count`` or the fingerprint.
    """

    def __init__(self, db: SQLiteConnection, buffer: TraceWriteBuffer, drop_tally: Optional[TraceDropTally] = None):
        self._db = db
        self._buffer = buffer
        self._drop_tally = drop_tally if drop_tally is not None else TraceDropTally()
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._shutting_down = threading.Event()

        # EMA smoothing state.
        self._smoothed_load = 0.0
        self._alpha = 0.2

        # Adaptive idle cadence.
        self._base_idle_sleep_ms = 50
        self._max_idle_sleep_ms = 500
        self._idle_sleep_ms = 50

        self._last_run_flush_time = time.time()
        self._active_runs: Dict[str, _RunState] = {}

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._loop, name="dprov-sqlite-writer", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._shutting_down.is_set():
            self._tick()

    def flush(self) -> None:
        with self._lock:
            self._process_batch(drain_all=True)
            try:
                with self._db.transaction():
                    staged = self._flush_runs_table(force=True)
                self._mark_runs_clean(staged)
            except Exception:  # pragma: no cover - defensive
                pass

    def shutdown(self) -> None:
        self._shutting_down.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self.flush()

    def _tick(self) -> None:
        depth = self._buffer.current_depth
        with self._lock:
            self._smoothed_load = (self._alpha * depth) + ((1.0 - self._alpha) * self._smoothed_load)
            smoothed = self._smoothed_load

            if smoothed > 5_000:
                batch_size = 5_000
                sleep_ms = random.randint(0, 5)
                self._idle_sleep_ms = self._base_idle_sleep_ms
            elif smoothed > 500:
                batch_size = 1_000
                sleep_ms = random.randint(10, 20)
                self._idle_sleep_ms = self._base_idle_sleep_ms
            else:
                batch_size = 500
                if depth > 0:
                    sleep_ms = self._base_idle_sleep_ms
                    self._idle_sleep_ms = self._base_idle_sleep_ms
                else:
                    sleep_ms = self._idle_sleep_ms
                    self._idle_sleep_ms = min(self._idle_sleep_ms * 2, self._max_idle_sleep_ms)

            self._process_batch(max_batch=batch_size)

            now = time.time()
            if now - self._last_run_flush_time > 1.0:
                try:
                    with self._db.transaction():
                        staged = self._flush_runs_table()
                    self._mark_runs_clean(staged)
                    self._last_run_flush_time = now
                except Exception as err:  # pragma: no cover
                    print(f"🚨 [DProvenanceKit] SQLiteWriter failed to flush runs: {err}")

        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)

    def _process_batch(self, drain_all: bool = False, max_batch: int = 1000) -> None:
        batch = self._buffer.flush_all() if drain_all else self._buffer.drain(max_batch)
        edges_batch = self._buffer.drain_edges()

        if not batch and not edges_batch:
            return

        try:
            with self._db.transaction():
                if batch:
                    self._insert(batch)
                if edges_batch:
                    self._insert_edges(edges_batch)
            # Durably committed: now safe to fold into in-memory run metadata.
            for event in batch:
                self._update_run_state(event)
        except Exception as err:
            # The transaction rolled back: these rows were already drained and are gone.
            # Count the loss per tier so it surfaces in dropStats / preserved_integrity.
            for event in batch:
                self._drop_tally.record(priority=event.priority)
            print(f"🚨 [DProvenanceKit] SQLiteWriter failed to insert batch: {err}")

    def _insert_edges(self, edges: List[TraceEdge]) -> None:
        sql = "INSERT INTO trace_edges (source_id, target_id, edge_type) VALUES (?, ?, ?);"
        self._db.executemany(
            sql,
            [(str(e.source_id), str(e.target_id), e.type.value) for e in edges],
        )

    def _insert(self, events: List[TraceEventRow]) -> None:
        sql = (
            "INSERT INTO trace_events "
            "(id, run_id, context_id, priority, sequence, engine, span_id, parent_span_id, type, payload, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"
        )
        rows = [
            (
                e.id,
                e.run_id,
                e.context_id,
                int(e.priority),
                int(e.sequence),
                e.engine,
                e.span_id,
                e.parent_span_id,
                e.type,
                e.payload,
                int(e.timestamp),
            )
            for e in events
        ]
        self._db.executemany(sql, rows)

    def _update_run_state(self, event: TraceEventRow) -> None:
        run_id = event.run_id
        state = self._active_runs.get(run_id)
        if state is None:
            state = _RunState(
                context_id=event.context_id,
                start_time=event.timestamp,
                latest_time=event.timestamp,
                event_count=0,
                fingerprint_hash=hashlib.sha1(),
                is_dirty=False,
            )
        state.latest_time = event.timestamp
        state.event_count += 1
        state.is_dirty = True
        signature = f"{event.type}:{event.engine or ''}|"
        state.fingerprint_hash.update(signature.encode("utf-8"))
        self._active_runs[run_id] = state

    def _flush_runs_table(self, force: bool = False) -> List[str]:
        sql = (
            "INSERT INTO runs (run_id, context_id, start_time, end_time, event_count, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "end_time = excluded.end_time, "
            "event_count = excluded.event_count, "
            "fingerprint = excluded.fingerprint;"
        )
        staged: List[str] = []
        for run_id, state in self._active_runs.items():
            if state.is_dirty or force:
                fingerprint = state.fingerprint_hash.hexdigest()
                self._db.execute(
                    sql,
                    (
                        run_id,
                        state.context_id,
                        int(state.start_time),
                        int(state.latest_time),
                        int(state.event_count),
                        fingerprint,
                    ),
                )
                staged.append(run_id)
        return staged

    def _mark_runs_clean(self, run_ids: List[str]) -> None:
        for run_id in run_ids:
            state = self._active_runs.get(run_id)
            if state is not None:
                state.is_dirty = False


class SQLiteTraceStore(TraceStore):
    """Non-blocking, durable trace store backed by WAL-mode SQLite."""

    def __init__(
        self,
        event_type: Type[TraceableEvent],
        path: str,
        max_global_buffer: int = 50_000,
        max_per_run_buffer: int = 5_000,
        start_writer: bool = True,
    ):
        self._event_type = event_type
        self._db = SQLiteConnection(path)
        self._buffer = TraceWriteBuffer(
            max_global_buffer=max_global_buffer, max_per_run_buffer=max_per_run_buffer
        )
        self._drop_tally = TraceDropTally()
        self._writer = SQLiteWriter(self._db, self._buffer, self._drop_tally)

        self._create_schema()

        if start_writer:
            self._writer.start()

    def _create_schema(self) -> None:
        db = self._db
        with db.transaction():
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    context_id TEXT,
                    start_time INTEGER,
                    end_time INTEGER,
                    event_count INTEGER,
                    fingerprint TEXT
                );
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS trace_events (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    engine TEXT,
                    span_id TEXT,
                    parent_span_id TEXT,
                    type TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    timestamp INTEGER NOT NULL
                );
                """
            )
            # Backwards compatibility for existing databases.
            for alter in (
                "ALTER TABLE trace_events ADD COLUMN span_id TEXT;",
                "ALTER TABLE trace_events ADD COLUMN parent_span_id TEXT;",
            ):
                try:
                    db.execute(alter)
                except sqlite3.OperationalError:
                    pass

            db.execute("CREATE INDEX IF NOT EXISTS idx_run_id ON trace_events(run_id);")
            db.execute("CREATE INDEX IF NOT EXISTS idx_type ON trace_events(type);")
            db.execute("CREATE INDEX IF NOT EXISTS idx_run_type ON trace_events(run_id, type);")
            db.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON trace_events(timestamp);")
            db.execute("CREATE INDEX IF NOT EXISTS idx_run_sequence ON trace_events(run_id, sequence);")
            db.execute("CREATE INDEX IF NOT EXISTS idx_priority ON trace_events(priority);")

            if db.user_version < 2:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS trace_edges (
                        source_id TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        edge_type TEXT NOT NULL
                    );
                    """
                )
                db.execute("CREATE INDEX IF NOT EXISTS idx_edge_source ON trace_edges(source_id, edge_type);")
                db.execute("CREATE INDEX IF NOT EXISTS idx_edge_target ON trace_edges(target_id, edge_type);")
                db.user_version = 2

            # Write-behind reconciliation: rebuild any runs interrupted during a crash.
            db.execute(
                """
                INSERT INTO runs (run_id, context_id, start_time, end_time, event_count, fingerprint)
                SELECT
                    run_id, MAX(context_id), MIN(timestamp), MAX(timestamp), COUNT(*), ''
                FROM trace_events
                GROUP BY run_id
                HAVING COUNT(*) > (
                    SELECT COALESCE(MAX(event_count), 0) FROM runs WHERE runs.run_id = trace_events.run_id
                )
                ON CONFLICT(run_id) DO UPDATE SET
                    end_time = excluded.end_time,
                    event_count = excluded.event_count;
                """
            )

    def record(self, event: TraceEvent) -> None:
        try:
            payload_data = event.payload.encode()
        except Exception:
            # An unencodable payload can't be persisted — count it in its own tier so the
            # loss shows up in dropStats rather than vanishing silently.
            self._drop_tally.record(priority=int(event.payload.priority))
            return

        row = TraceEventRow(
            id=str(event.id),
            run_id=str(event.run_id),
            context_id=event.context_id,
            priority=int(event.payload.priority),
            sequence=int(event.sequence),
            engine=event.engine_name,
            span_id=event.span_id,
            parent_span_id=event.parent_span_id,
            type=event.payload.type_identifier,
            payload=payload_data,
            timestamp=int(event.timestamp * 1_000_000),
        )
        self._buffer.enqueue(row)

    def link(self, source: uuid.UUID, target: uuid.UUID, type: TraceEdgeType) -> None:
        self._buffer.enqueue_edge(TraceEdge(source_id=source, target_id=target, type=type))

    def flush(self) -> None:
        self._writer.flush()

    def close(self) -> None:
        """Stop the background writer (if running) and close the database connection.

        Safe to call when ``start_writer=False`` (no thread was started) — it still flushes
        any buffered events and releases the SQLite file handle.
        """
        self._writer.shutdown()
        self._db.close()

    @property
    def drop_stats(self) -> TraceDropStats:
        return self._buffer.drop_stats + self._drop_tally.snapshot

    def query_runs(self, dsl: TraceQueryDSL) -> List[TraceRun]:
        self.flush()
        compiled = TraceQueryCompiler.compile(dsl.ast)
        rows = self._db.query(compiled.sql, tuple(compiled.bindings))
        run_ids = [r[0] for r in rows if r[0] is not None]

        runs: List[TraceRun] = []
        for id_string in run_ids:
            try:
                run_uuid = uuid.UUID(id_string)
            except (ValueError, AttributeError):
                continue
            run = self._fetch_run(run_uuid)
            if run is not None:
                runs.append(run)
        return runs

    def list_run_metadata(self) -> List[RunRow]:
        """Run metadata (no events) from the ``runs`` table, newest first by start time.

        Cheap and event-free — for picking a baseline run (e.g. the latest known-good run for
        a context) without materializing every run. Flushes pending writes first, like
        :meth:`query_runs`.
        """
        self.flush()
        rows = self._db.query(
            "SELECT run_id, context_id, start_time, end_time, event_count, fingerprint "
            "FROM runs ORDER BY start_time DESC, run_id DESC"
        )
        return [
            RunRow(
                run_id=r[0],
                context_id=r[1],
                start_time=int(r[2] or 0),
                end_time=int(r[3] or 0),
                event_count=int(r[4] or 0),
                fingerprint=r[5] or "",
            )
            for r in rows
            if r[0] is not None
        ]

    def get_run(self, id: uuid.UUID) -> Optional[TraceRun]:
        """Fetch a single run by id, indexed on ``run_id`` (no full scan). Flushes pending
        events first, mirroring :meth:`query_runs`, and parallels ``InMemoryTraceStore``."""
        self.flush()
        return self._fetch_run(id)

    def _fetch_run(self, id: uuid.UUID) -> Optional[TraceRun]:
        ctx_rows = self._db.query("SELECT context_id FROM runs WHERE run_id = ?", (str(id),))
        if not ctx_rows or ctx_rows[0][0] is None:
            return None
        context_id = ctx_rows[0][0]

        rows = self._db.query(
            "SELECT engine, span_id, parent_span_id, type, payload, timestamp, sequence "
            "FROM trace_events WHERE run_id = ? ORDER BY sequence ASC",
            (str(id),),
        )

        events: List[TraceEvent] = []
        for engine, span_id, parent_span_id, _type, payload_data, timestamp_us, sequence in rows:
            try:
                payload = self._event_type.decode(bytes(payload_data))
            except Exception:
                continue
            events.append(
                TraceEvent(
                    run_id=id,
                    context_id=context_id,
                    engine_name=engine if engine is not None else "Unknown",
                    schema_version=1,
                    sequence=int(sequence),
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    payload=payload,
                    timestamp=float(timestamp_us) / 1_000_000.0,
                )
            )

        if not events:
            return None
        return TraceRun(run_id=id, context_id=context_id, events=events)

    def lineage_edges(self, id: uuid.UUID) -> List[TraceEdge]:
        self.flush()
        sql = """
        WITH RECURSIVE lineage_cte(source_id, target_id, edge_type) AS (
            SELECT source_id, target_id, edge_type FROM trace_edges WHERE target_id = ?
            UNION
            SELECT e.source_id, e.target_id, e.edge_type
            FROM trace_edges e JOIN lineage_cte l ON e.target_id = l.source_id
        )
        SELECT source_id, target_id, edge_type FROM lineage_cte;
        """
        return self._read_edges(sql, (str(id),))

    def impact_edges(self, id: uuid.UUID) -> List[TraceEdge]:
        self.flush()
        sql = """
        WITH RECURSIVE impact_cte(source_id, target_id, edge_type) AS (
            SELECT source_id, target_id, edge_type FROM trace_edges WHERE source_id = ?
            UNION
            SELECT e.source_id, e.target_id, e.edge_type
            FROM trace_edges e JOIN impact_cte l ON e.source_id = l.target_id
        )
        SELECT source_id, target_id, edge_type FROM impact_cte;
        """
        return self._read_edges(sql, (str(id),))

    def _read_edges(self, sql: str, params) -> List[TraceEdge]:
        edges: List[TraceEdge] = []
        for source_str, target_str, type_str in self._db.query(sql, params):
            try:
                source = uuid.UUID(source_str)
                target = uuid.UUID(target_str)
            except (ValueError, AttributeError):
                continue
            try:
                edge_type = TraceEdgeType(type_str)
            except ValueError:
                edge_type = TraceEdgeType.INFORMED
            edges.append(TraceEdge(source_id=source, target_id=target, type=edge_type))
        return edges

    def get_events(self, ids: Set[uuid.UUID]) -> Dict[uuid.UUID, TraceEvent]:
        if not ids:
            return {}
        self.flush()
        id_strings = [str(i) for i in ids]
        placeholders = ", ".join("?" for _ in id_strings)
        sql = (
            "SELECT e.id, e.run_id, r.context_id, e.engine, e.span_id, e.parent_span_id, "
            "e.payload, e.timestamp, e.sequence "
            "FROM trace_events e JOIN runs r ON e.run_id = r.run_id "
            f"WHERE e.id IN ({placeholders})"
        )
        events: Dict[uuid.UUID, TraceEvent] = {}
        for row in self._db.query(sql, tuple(id_strings)):
            (id_str, run_id_str, context_id, engine, span_id, parent_span_id,
             payload_data, timestamp_us, sequence) = row
            try:
                event_id = uuid.UUID(id_str)
                run_id = uuid.UUID(run_id_str)
            except (ValueError, AttributeError):
                continue
            if engine is None:
                continue
            try:
                payload = self._event_type.decode(bytes(payload_data))
            except Exception:
                continue
            events[event_id] = TraceEvent(
                id=event_id,
                run_id=run_id,
                context_id=context_id,
                engine_name=engine,
                schema_version=1,
                sequence=int(sequence),
                span_id=span_id,
                parent_span_id=parent_span_id,
                payload=payload,
                timestamp=float(timestamp_us) / 1_000_000.0,
            )
        return events
