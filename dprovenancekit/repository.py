"""Local immutable history for recorded AI workflows (DPK Repository v1).

Live trace databases remain separate. Objects are immutable through this API;
refs are deliberately mutable and updated with SQLite transactions/CAS. Hashes
verify exact bytes, not factual truth, capture completeness, or protection from
rolling an entire repository back to an older valid root.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import struct
import threading
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Union, cast

from .repository_codec import (
    MAX_OBJECT_BYTES, RepositoryFormatError, decode_object, encode_object, object_id,
)

DEFAULT_REPOSITORY = ".dprovenance/repository.sqlite"
BUNDLE_FORMAT = "DPK-REPOSITORY-BUNDLE-V1"
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_OBJECTS = 10000
_ID = re.compile(r"^[0-9a-f]{64}$")
_UNSET = object()
DEFAULT_POLICY: Dict[str, Any] = {
    "name": "repository-strict-v1", "profile": "strict_audit_v1",
    "minimum_priority": 2, "max_events": 1000,
    "normalizer": "json-value-v1", "evaluator": "TraceAlignmentEngine/1.0.0",
}


class RepositoryError(ValueError):
    """Repository operation cannot be completed safely."""


class RefConflictError(RepositoryError):
    """A ref changed since its expected value was read."""


def _now() -> int:
    return time.time_ns() // 1000


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RepositoryFormatError(message)


def _ref_name(name: str, kind: str = "heads") -> str:
    if not isinstance(name, str) or not name or len(name) > 255:
        raise RepositoryError("invalid ref name")
    full = name if name.startswith("refs/") else "refs/" + kind + "/" + name
    parts = full.split("/")
    if len(parts) < 3 or parts[1] not in {"heads", "tags", "baselines"}:
        raise RepositoryError("unsupported ref namespace")
    if any(not p or p in {".", ".."} or p.endswith(".lock") for p in parts):
        raise RepositoryError("invalid ref name")
    if any(ord(c) < 33 or c in "~^:?*[\\" for c in full) or "@{" in full:
        raise RepositoryError("invalid ref name")
    return full


def _json_object_pairs(pairs: Sequence[Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RepositoryFormatError("duplicate JSON key")
        result[key] = value
    return result


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _validate_zip_layout(raw: bytes) -> None:
    """Accept the small stored-ZIP subset independently readable by every SDK."""
    if len(raw) < 22:
        raise RepositoryFormatError("truncated ZIP")
    end = struct.unpack("<4s4H2IH", raw[-22:])
    if end[0] != b"PK\x05\x06" or end[1] or end[2] or end[3] != end[4] or end[7]:
        raise RepositoryFormatError("ZIP comments, spanning and ZIP64 are unsupported")
    count, central_size, central_start = end[4], end[5], end[6]
    if count > MAX_BUNDLE_OBJECTS + 1 or central_start + central_size != len(raw) - 22:
        raise RepositoryFormatError("invalid ZIP central directory")
    local = {}
    position = 0
    while position < central_start:
        if position + 30 > central_start:
            raise RepositoryFormatError("truncated ZIP local header")
        h = struct.unpack("<4s5H3I2H", raw[position:position + 30])
        if h[0] != b"PK\x03\x04" or h[1] > 20 or h[2] & ~0x800 or h[3] or h[10] or h[7] != h[8]:
            raise RepositoryFormatError("unsupported ZIP local feature")
        name_start = position + 30
        name_end = name_start + h[9]
        data_end = name_end + h[7]
        if data_end > central_start or h[7] > MAX_OBJECT_BYTES:
            raise RepositoryFormatError("invalid ZIP entry bounds")
        name = raw[name_start:name_end]
        if name in local:
            raise RepositoryFormatError("duplicate ZIP entry")
        local[name] = (position, h[2], h[6], h[7], h[8])
        position = data_end
    if position != central_start or len(local) != count:
        raise RepositoryFormatError("ZIP local entry count mismatch")
    seen = set()
    for _ in range(count):
        if position + 46 > len(raw) - 22:
            raise RepositoryFormatError("truncated ZIP central header")
        h = struct.unpack("<4s6H3I5H2I", raw[position:position + 46])
        if h[0] != b"PK\x01\x02" or h[2] > 20 or h[3] & ~0x800 or h[4] or h[11] or h[12] or h[13]:
            raise RepositoryFormatError("unsupported ZIP central feature")
        name = raw[position + 46:position + 46 + h[10]]
        if name in seen or local.get(name) != (h[16], h[3], h[7], h[8], h[9]):
            raise RepositoryFormatError("ZIP local/central entry mismatch")
        seen.add(name)
        position += 46 + h[10]
    if position != len(raw) - 22:
        raise RepositoryFormatError("trailing ZIP directory bytes")


def _verify_signature_payload(payload: Mapping[str, Any]) -> bool:
    from .attestation import _require_crypto

    _require_crypto()
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), payload["public_key"])
        key.verify(payload["signature"], b"DPK-REPOSITORY-SIGNATURE-V1\x00" + bytes.fromhex(payload["subject"]),
                   ec.ECDSA(hashes.SHA256()))
        return True
    except (ValueError, InvalidSignature):
        return False


def _payload_signature(raw: bytes) -> str:
    """JSON value equality with exact decimal numbers and duplicate-key rejection."""
    def invalid(value: str) -> Any:
        raise RepositoryFormatError("non-finite JSON number")

    parsed = json.loads(raw.decode("utf-8"), parse_float=Decimal, parse_int=Decimal,
                        parse_constant=invalid, object_pairs_hook=_json_object_pairs)

    def render(value: Any, depth: int = 0) -> str:
        if depth > 64:
            raise RepositoryFormatError("payload nesting limit exceeded")
        if value is None:
            return "null"
        if type(value) is bool:
            return "true" if value else "false"
        if isinstance(value, Decimal):
            if not value.is_finite() or abs(value.adjusted()) > 1000:
                raise RepositoryFormatError("payload number outside normalizer limits")
            if not value:
                return "0"
            text = format(value, "f")
            return text.rstrip("0").rstrip(".") if "." in text else text
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, list):
            return "[" + ",".join(render(v, depth + 1) for v in value) + "]"
        if isinstance(value, dict):
            return "{" + ",".join(
                json.dumps(k, ensure_ascii=False) + ":" + render(value[k], depth + 1)
                for k in sorted(value, key=lambda x: x.encode("utf-8"))
            ) + "}"
        raise RepositoryFormatError("unsupported JSON payload")

    return hashlib.sha256(render(parsed).encode("utf-8")).hexdigest()


def _graph_health(events: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]],
                  run_id: str) -> Dict[str, Any]:
    reasons: Set[str] = set()
    ids = [str(event.get("id", "")) for event in events]
    sequences = [event.get("sequence") for event in events]
    if len(set(ids)) != len(ids):
        reasons.add("DUPLICATE_EVENT_ID")
    if sequences != list(range(len(events))):
        reasons.add("SEQUENCE_GAP_OR_DUPLICATE")
    if any(event.get("run_id") != run_id for event in events):
        reasons.add("MIXED_RUN_EVENTS")
    parents: Dict[str, Optional[str]] = {}
    for event in events:
        span, parent = event.get("span_id"), event.get("parent_span_id")
        if span is not None:
            if span in parents and parents[span] != parent:
                reasons.add("INCONSISTENT_SPAN_PARENT")
            parents[span] = parent
        elif parent is not None:
            reasons.add("PARENT_WITHOUT_SPAN")
    resolved_spans: Set[str] = set()
    for span in parents:
        if span in resolved_spans:
            continue
        seen: Set[str] = set()
        current: Optional[str] = span
        while current is not None and current not in resolved_spans:
            if current in seen:
                reasons.add("SPAN_CYCLE")
                break
            seen.add(current)
            if current not in parents:
                reasons.add("MISSING_PARENT_SPAN")
                break
            current = parents[current]
        resolved_spans.update(seen)
    id_set = set(ids)
    adjacency: Dict[str, List[str]] = {}
    seen_edges: Set[Any] = set()
    for edge in edges:
        source, target, kind = str(edge.get("source_id", "")), str(edge.get("target_id", "")), edge.get("type")
        key = (source, target, kind)
        if key in seen_edges:
            reasons.add("DUPLICATE_EDGE")
        seen_edges.add(key)
        if source not in id_set or target not in id_set:
            reasons.add("EXTERNAL_OR_MISSING_EDGE_ENDPOINT")
        if source == target:
            reasons.add("SELF_EDGE")
        adjacency.setdefault(source, []).append(target)
    # Iterative DFS avoids Python recursion dependence on trace size.
    visited: Set[str] = set()
    active: Set[str] = set()
    for start in id_set:
        if start in visited:
            continue
        stack = [(start, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                active.discard(node)
                visited.add(node)
            elif node in active:
                reasons.add("PROVENANCE_CYCLE")
            elif node not in visited:
                active.add(node)
                stack.append((node, True))
                stack.extend((child, False) for child in adjacency.get(node, []))
    return {"valid": not reasons, "reasons": sorted(reasons),
            "event_count": len(events), "edge_count": len(edges)}


def _capture_complete(run: Mapping[str, Any]) -> bool:
    quality = run["capture_quality"]
    return (
        run.get("workflow_binding") == "prebound"
        and quality.get("workflow_id") == run["workflow"]
        and quality.get("lifecycle") == "completed" and quality.get("sealed") is True
        and type(quality.get("attempted_events")) is int
        and type(quality.get("attempted_edges")) is int
        and quality["attempted_events"] == len(run["events"])
        and quality["attempted_edges"] == len(run["edges"])
        and quality.get("dropped_events", 0) == 0
        and quality.get("dropped_edges", 0) == 0
        and quality.get("undecoded_events", 0) == 0
        and quality.get("schema_provenance") == "recorded"
        and all(e["schema_provenance"] == "recorded" for e in run["events"])
        and run["graph_health"]["valid"] is True and bool(run["events"])
    )


def _references(kind: str, payload: Mapping[str, Any]) -> Dict[str, str]:
    if kind == "workflow":
        return {v: "blob" for v in payload["assets"].values()}
    if kind == "run":
        result = {payload["workflow"]: "workflow"}
        result.update({e["payload"]: "blob" for e in payload["events"]})
        return result
    if kind == "commit":
        result = {v: "commit" for v in payload["parents"]}
        result[payload["workflow"]] = "workflow"
        result[payload["run"]] = "run"
        return result
    if kind == "evaluation":
        return {payload["base"]: "commit", payload["candidate"]: "commit"}
    if kind == "signature":
        return {payload["subject"]: "commit"}
    return {}


def _shape(kind: str, p: Any) -> None:
    _require(isinstance(p, dict), "object payload must be a map")
    required = {
        "blob": {"data"},
        "workflow": {"assets", "config", "source", "capture_policy"},
        "run": {"run_id", "context_id", "workflow", "workflow_binding", "events", "edges",
                "capture_quality", "graph_health"},
        "commit": {"parents", "workflow", "run", "message", "author", "created_at_us"},
        "evaluation": {"base", "candidate", "policy", "policy_digest", "report"},
        "signature": {"subject", "algorithm", "key_id", "public_key", "signature"},
    }[kind]
    _require(set(p) == required, "unexpected or missing " + kind + " fields")
    if kind == "blob":
        _require(isinstance(p["data"], bytes), "blob data must be bytes")
    elif kind == "workflow":
        _require(all(isinstance(p[k], dict) for k in required), "workflow fields must be maps")
        _require(all(isinstance(k, str) and k and _is_id(v) for k, v in p["assets"].items()),
                 "invalid workflow assets")
    elif kind == "run":
        _require(_is_id(p["workflow"]), "invalid workflow ID")
        _require(isinstance(p["run_id"], str) and bool(p["run_id"])
                 and isinstance(p["context_id"], str), "invalid run identity")
        _require(p["workflow_binding"] in {"prebound", "post_hoc"}, "invalid workflow binding")
        _require(isinstance(p["events"], list) and isinstance(p["edges"], list), "invalid run arrays")
        _require(isinstance(p["capture_quality"], dict) and isinstance(p["graph_health"], dict),
                 "invalid capture quality")
        health = p["graph_health"]
        _require(set(health) == {"valid", "reasons", "event_count", "edge_count"}
                 and type(health["valid"]) is bool and isinstance(health["reasons"], list)
                 and all(isinstance(reason, str) for reason in health["reasons"])
                 and type(health["event_count"]) is int and type(health["edge_count"]) is int,
                 "invalid graph health fields")
        event_keys = {"id", "run_id", "context_id", "engine", "schema_version", "sequence",
                      "span_id", "parent_span_id", "type", "priority", "payload", "timestamp_us",
                      "schema_provenance"}
        for e in p["events"]:
            _require(isinstance(e, dict) and set(e) == event_keys, "invalid event fields")
            _require(all(isinstance(e[k], str) for k in ("id", "run_id", "context_id", "engine", "type")),
                     "invalid event strings")
            _require(bool(e["id"]) and bool(e["type"]) and _is_id(e["payload"]), "invalid event identity/payload")
            _require(all(type(e[k]) is int for k in ("schema_version", "sequence", "priority", "timestamp_us")),
                     "invalid event integer metadata")
            _require(e["schema_version"] >= 1 and e["sequence"] >= 0 and 0 <= e["priority"] <= 3,
                     "invalid event numeric range")
            _require(all(e[k] is None or isinstance(e[k], str) for k in ("span_id", "parent_span_id")),
                     "invalid span identity")
            _require(e["schema_provenance"] in {"recorded", "legacy_assumed_v1"}, "unknown schema provenance")
        for e in p["edges"]:
            _require(isinstance(e, dict) and set(e) == {"source_id", "target_id", "type"}, "invalid edge")
            _require(all(isinstance(v, str) for v in e.values()), "invalid edge strings")
            _require(e["type"] in {"derivedFrom", "influencedBy", "generatedFrom", "verifiedBy", "correctedBy", "informed"},
                     "unknown edge type")
        _require(p["graph_health"] == _graph_health(p["events"], p["edges"], p["run_id"]),
                 "graph health assertion does not match captured graph")
        q = p["capture_quality"]
        quality_keys = {"lifecycle", "attempted_events", "dropped_events", "attempted_edges",
                        "dropped_edges", "undecoded_events", "schema_provenance", "sealed"}
        _require(quality_keys <= set(q) <= quality_keys | {"workflow_id"}, "invalid capture quality fields")
        _require(q.get("lifecycle") in {"recording", "completed", "failed", "interrupted", "legacy_unknown"},
                 "unknown capture lifecycle")
        _require(type(q.get("sealed")) is bool, "sealed must be boolean")
        for key in ("attempted_events", "attempted_edges"):
            _require(q.get(key) is None or type(q[key]) is int and q[key] >= 0, "invalid capture count")
        for key in ("dropped_events", "dropped_edges", "undecoded_events"):
            _require(type(q.get(key)) is int and q[key] >= 0, "invalid drop count")
        _require(q.get("schema_provenance") in {"recorded", "legacy_assumed_v1"}, "invalid capture provenance")
        _require(q.get("workflow_id") is None or _is_id(q["workflow_id"]), "invalid captured workflow ID")
        _require(p["workflow_binding"] != "prebound" or q.get("workflow_id") == p["workflow"],
                 "prebound workflow must match the capture store binding")
        _require(q["lifecycle"] != "recording", "active runs cannot be sealed as evidence")
    elif kind == "commit":
        _require(isinstance(p["parents"], list) and len(p["parents"]) <= 1
                 and all(_is_id(v) for v in p["parents"]), "v1 supports at most one commit parent")
        _require(_is_id(p["workflow"]) and _is_id(p["run"]), "invalid commit references")
        _require(isinstance(p["message"], str) and isinstance(p["author"], str)
                 and type(p["created_at_us"]) is int, "invalid commit metadata")
    elif kind == "evaluation":
        _require(_is_id(p["base"]) and _is_id(p["candidate"]), "invalid evaluation input")
        _require(isinstance(p["policy"], dict) and isinstance(p["report"], dict), "invalid evaluation")
        try:
            digest = hashlib.sha256(_json_bytes(p["policy"])).hexdigest()
        except (TypeError, ValueError) as exc:
            raise RepositoryFormatError("policy must be canonical JSON metadata") from exc
        _require(p["policy_digest"] == digest, "policy digest mismatch")
    else:
        _require(_is_id(p["subject"]) and p["algorithm"] == "P256-SHA256", "invalid signature subject/algorithm")
        _require(isinstance(p["public_key"], bytes) and isinstance(p["signature"], bytes), "invalid signature bytes")
        _require(len(p["public_key"]) == 65 and p["public_key"][0] == 4,
                 "public key must be an uncompressed X9.63 P-256 point")
        _require(p["key_id"] == hashlib.sha256(p["public_key"]).hexdigest(), "signature key ID mismatch")


class ReasoningRepository:
    """Persistent workflow/run history. Source checkout files are never changed."""

    def __init__(self, path: Union[str, Path] = DEFAULT_REPOSITORY, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self._lock = threading.RLock()
        self._closed = False
        if read_only:
            self._db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True,
                                       check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        try:
            self._initialize()
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    def _initialize(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version not in (0, 1) or (self.read_only and version != 1):
            raise RepositoryFormatError("unsupported repository schema")
        if version == 0 and tables:
            raise RepositoryFormatError("refusing to initialize a nonempty database as a reasoning repository")
        if version == 1 and not {"objects", "refs", "reflog", "settings", "bindings"} <= tables:
            raise RepositoryFormatError("incomplete repository schema")
        if not self.read_only:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY, type TEXT NOT NULL, data BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS refs (name TEXT PRIMARY KEY, target TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reflog (sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL, old_target TEXT, new_target TEXT NOT NULL, timestamp_us INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bindings (run_id TEXT PRIMARY KEY, workflow TEXT NOT NULL,
                    evidence TEXT, bound_at_us INTEGER NOT NULL);
                INSERT OR IGNORE INTO settings VALUES ('head','refs/heads/main');
                PRAGMA user_version=1;
            """)
            self._db.commit()

    def __enter__(self) -> "ReasoningRepository":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def _open(self) -> None:
        if self._closed:
            raise RepositoryError("repository is closed")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._open()
            if self.read_only:
                raise RepositoryError("repository is read-only")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _put(self, kind: str, payload: Any) -> str:
        _shape(kind, payload)
        data = encode_object(kind, payload)
        oid = object_id(data)
        existing = self._db.execute("SELECT data FROM objects WHERE id=?", (oid,)).fetchone()
        if existing is not None and bytes(existing[0]) != data:
            raise RepositoryFormatError("object ID collision or repository corruption")
        self._db.execute("INSERT OR IGNORE INTO objects(id,type,data) VALUES(?,?,?)", (oid, kind, data))
        return oid

    def get_object(self, oid: str) -> Dict[str, Any]:
        with self._lock:
            self._open()
            if not _is_id(oid):
                raise RepositoryError("invalid object ID")
            row = self._db.execute("SELECT type,data FROM objects WHERE id=?", (oid,)).fetchone()
            if row is None:
                raise RepositoryError("object not found: " + oid)
            data = bytes(row[1])
            if object_id(data) != oid:
                raise RepositoryFormatError("object hash mismatch: " + oid)
            kind, payload = decode_object(data)
            if kind != row[0]:
                raise RepositoryFormatError("object type index mismatch")
            _shape(kind, payload)
            return {"id": oid, "type": kind, "payload": payload}

    def _payload(self, oid: str, expected: str) -> Dict[str, Any]:
        obj = self.get_object(oid)
        if obj["type"] != expected:
            raise RepositoryFormatError("expected " + expected + " object")
        return obj["payload"]

    def refs(self) -> Dict[str, str]:
        with self._lock:
            self._open()
            return dict(self._db.execute("SELECT name,target FROM refs ORDER BY name"))

    def resolve(self, name: str = "HEAD") -> str:
        with self._lock:
            self._open()
            if _is_id(name):
                self.get_object(name)
                return name
            if name == "HEAD":
                name = self._db.execute("SELECT value FROM settings WHERE key='head'").fetchone()[0]
            refs = self.refs()
            candidates = [name] if name.startswith("refs/") else [
                _ref_name(name), _ref_name(name, "tags"), _ref_name(name, "baselines")]
            matches = [refs[n] for n in candidates if n in refs]
            if len(matches) != 1:
                raise RepositoryError("ref is missing or ambiguous: " + name)
            return matches[0]

    def status(self) -> Dict[str, Any]:
        with self._lock:
            self._open()
            head = self._db.execute("SELECT value FROM settings WHERE key='head'").fetchone()[0]
            return {"path": str(self.path), "head": head, "refs": self.refs(),
                    "object_count": self._db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]}

    def _update_ref(self, name: str, target: str, expected: Any = _UNSET) -> None:
        self._payload(target, "commit")
        row = self._db.execute("SELECT target FROM refs WHERE name=?", (name,)).fetchone()
        old = row[0] if row else None
        if expected is not _UNSET and old != expected:
            raise RefConflictError("ref changed: " + name)
        self._db.execute("INSERT INTO refs(name,target) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET target=excluded.target",
                         (name, target))
        self._db.execute("INSERT INTO reflog(name,old_target,new_target,timestamp_us) VALUES(?,?,?,?)",
                         (name, old, target, _now()))

    def snapshot_workflow(self, assets: Mapping[str, bytes], config: Optional[dict] = None,
                          source: Optional[dict] = None, capture_policy: Optional[dict] = None) -> str:
        with self._transaction():
            references = {}
            for name, data in sorted(assets.items()):
                if not isinstance(name, str) or not name or not isinstance(data, bytes):
                    raise RepositoryError("assets must map nonempty names to exact bytes")
                references[name] = self._put("blob", {"data": data})
            return self._put("workflow", {"assets": references, "config": config if config is not None else {},
                                          "source": source if source is not None else {},
                                          "capture_policy": capture_policy if capture_policy is not None else {}})

    def bind_run(self, run_id: Any, workflow_id: str) -> None:
        with self._transaction():
            self._payload(workflow_id, "workflow")
            key = str(run_id)
            row = self._db.execute("SELECT workflow FROM bindings WHERE run_id=?", (key,)).fetchone()
            if row and row[0] != workflow_id:
                raise RepositoryError("run is already bound to another workflow")
            self._db.execute("INSERT OR IGNORE INTO bindings(run_id,workflow,bound_at_us) VALUES(?,?,?)",
                             (key, workflow_id, _now()))

    def lookup_run(self, run_id: Any) -> Optional[str]:
        with self._lock:
            self._open()
            row = self._db.execute("SELECT evidence FROM bindings WHERE run_id=?", (str(run_id),)).fetchone()
            return row[0] if row else None

    def seal_run_from_readonly_database(self, db_path: str, run_id: Any) -> str:
        """Seal one completed capture without opening a writable trace store."""
        from .repository_readonly import seal_readonly_run

        return seal_readonly_run(self, db_path, run_id)

    def seal_run(self, store: Any, run_id: Any, workflow_id: Optional[str] = None) -> str:
        key = str(run_id)
        with self._lock:
            self._open()
            binding = self._db.execute("SELECT workflow,evidence FROM bindings WHERE run_id=?", (key,)).fetchone()
            if binding and binding[1]:
                self._payload(binding[1], "run")
                if workflow_id is not None and workflow_id != binding[0]:
                    raise RepositoryError("workflow does not match run binding")
                return binding[1]
        workflow = workflow_id or (binding[0] if binding else None)
        if workflow is None:
            raise RepositoryError("snapshot and bind a workflow before recording, or supply workflow_id for a legacy import")
        if binding and binding[0] != workflow:
            raise RepositoryError("workflow does not match run binding")
        if not hasattr(store, "repository_run_snapshot"):
            raise RepositoryError("store does not support lossless repository snapshots")
        snapshot = store.repository_run_snapshot(uuid.UUID(key))
        quality = snapshot["capture_quality"]
        if is_dataclass(quality):
            quality = asdict(cast(Any, quality))
        quality = dict(quality)
        quality.setdefault("lifecycle", snapshot.get("lifecycle", "legacy_unknown"))
        quality.setdefault("sealed", False)
        quality.setdefault("attempted_events", None)
        quality.setdefault("attempted_edges", None)
        quality.setdefault("dropped_events", 0)
        quality.setdefault("dropped_edges", 0)
        quality.setdefault("undecoded_events", 0)
        quality.setdefault("schema_provenance", "legacy_assumed_v1")
        if quality["lifecycle"] == "recording":
            raise RepositoryError("cannot seal an active run")
        with self._transaction():
            self._payload(workflow, "workflow")
            events = []
            for e in snapshot["events"]:
                raw = e.get("payload_bytes", e.get("payload_json"))
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                blob = self._put("blob", {"data": raw})
                events.append({
                    "id": str(e["id"]), "run_id": str(e["run_id"]), "context_id": e["context_id"],
                    "engine": e.get("engine_name") or "", "schema_version": e.get("schema_version", 1),
                    "sequence": e["sequence"], "span_id": e.get("span_id"),
                    "parent_span_id": e.get("parent_span_id"), "type": e["type_identifier"],
                    "priority": int(e["priority"]), "payload": blob, "timestamp_us": e["timestamp_us"],
                    "schema_provenance": e.get("schema_provenance", "legacy_assumed_v1"),
                })
            events.sort(key=lambda e: (e["sequence"], e["id"]))
            edges = sorted([dict(e) for e in snapshot["edges"]],
                           key=lambda e: (e["source_id"], e["target_id"], e["type"]))
            run = snapshot["run"]
            payload = {"run_id": key, "context_id": run.context_id, "workflow": workflow,
                       "workflow_binding": "prebound" if binding and quality.get("workflow_id") == workflow else "post_hoc", "events": events,
                       "edges": edges, "capture_quality": quality,
                       "graph_health": _graph_health(events, edges, key)}
            evidence = self._put("run", payload)
            self._db.execute("INSERT INTO bindings(run_id,workflow,evidence,bound_at_us) VALUES(?,?,?,?) "
                             "ON CONFLICT(run_id) DO UPDATE SET evidence=excluded.evidence",
                             (key, workflow, evidence, _now()))
            return evidence

    def commit(self, run_object_id: str, message: str, branch: Optional[str] = None,
               expected_parent: Any = _UNSET, author: str = "") -> str:
        with self._transaction():
            run = self._payload(run_object_id, "run")
            name = _ref_name(branch) if branch is not None else self._db.execute(
                "SELECT value FROM settings WHERE key='head'").fetchone()[0]
            if not name.startswith("refs/heads/"):
                raise RepositoryError("commits can advance only branch refs")
            row = self._db.execute("SELECT target FROM refs WHERE name=?", (name,)).fetchone()
            parent = row[0] if row else None
            if expected_parent is not _UNSET and expected_parent != parent:
                raise RefConflictError("branch changed before commit")
            oid = self._put("commit", {"parents": [parent] if parent else [], "workflow": run["workflow"],
                                       "run": run_object_id, "message": message, "author": author,
                                       "created_at_us": _now()})
            self._closure([oid])
            self._update_ref(name, oid, parent)
            return oid

    def branches(self) -> Dict[str, str]:
        return {k[len("refs/heads/"):]: v for k, v in self.refs().items() if k.startswith("refs/heads/")}

    def create_branch(self, name: str, start: str = "HEAD") -> str:
        with self._transaction():
            ref = _ref_name(name)
            if not ref.startswith("refs/heads/"):
                raise RepositoryError("branch requires heads namespace")
            target = self.resolve(start)
            self._update_ref(ref, target, None)
            return target

    def switch(self, name: str) -> None:
        with self._transaction():
            ref = _ref_name(name)
            if not ref.startswith("refs/heads/") or ref not in self.refs():
                raise RepositoryError("branch not found")
            self._db.execute("UPDATE settings SET value=? WHERE key='head'", (ref,))

    def tag(self, name: str, target: str = "HEAD") -> str:
        with self._transaction():
            ref = _ref_name(name, "tags")
            if not ref.startswith("refs/tags/"):
                raise RepositoryError("tag requires tags namespace")
            oid = self.resolve(target)
            self._update_ref(ref, oid, None)
            return oid

    def log(self, ref: str = "HEAD", limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 1000 or offset < 0:
            raise RepositoryError("invalid page bounds")
        result: List[Dict[str, Any]] = []
        with self._lock:
            oid: Optional[str] = self.resolve(ref)
            seen: Set[str] = set()
            index = 0
            while oid is not None and len(result) < limit:
                if oid in seen:
                    raise RepositoryFormatError("commit parent cycle")
                seen.add(oid)
                p = self._payload(oid, "commit")
                if index >= offset:
                    result.append({"id": oid, **p})
                index += 1
                oid = p["parents"][0] if p["parents"] else None
        return result

    def reflog(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        if not 1 <= limit <= 1000 or offset < 0:
            raise RepositoryError("invalid page bounds")
        with self._lock:
            self._open()
            rows = self._db.execute("SELECT sequence,name,old_target,new_target,timestamp_us FROM reflog "
                                    "ORDER BY sequence DESC LIMIT ? OFFSET ?", (limit, offset))
            return [dict(zip(("sequence", "name", "old_target", "new_target", "timestamp_us"), r)) for r in rows]

    def _closure(self, roots: Iterable[str], max_objects: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
        objects: Dict[str, Dict[str, Any]] = {}
        checked: Dict[str, Dict[str, Any]] = {}

        def checked_object(oid: str) -> Dict[str, Any]:
            if oid not in checked:
                checked[oid] = self.get_object(oid)
            return checked[oid]

        pending = list(roots)
        while pending:
            oid = pending.pop()
            if oid in objects:
                continue
            obj = checked_object(oid)
            objects[oid] = obj
            if max_objects is not None and len(objects) > max_objects:
                raise RepositoryFormatError("object closure limit exceeded")
            for ref, expected in _references(obj["type"], obj["payload"]).items():
                referenced = checked_object(ref)
                if referenced["type"] != expected:
                    raise RepositoryFormatError("referenced object type mismatch")
                pending.append(ref)
            if obj["type"] == "commit":
                p = obj["payload"]
                if checked_object(p["run"])["payload"]["workflow"] != p["workflow"]:
                    raise RepositoryFormatError("commit/run workflow mismatch")
            elif obj["type"] == "signature" and not _verify_signature_payload(obj["payload"]):
                raise RepositoryFormatError("invalid detached signature")
        return objects

    def compare(self, base: str, candidate: str, policy: Optional[dict] = None,
                persist: bool = True) -> Dict[str, Any]:
        base_id = base if _is_id(base) else self.resolve(base)
        candidate_id = candidate if _is_id(candidate) else self.resolve(candidate)
        selected = dict(DEFAULT_POLICY)
        if policy:
            if set(policy) - set(selected):
                raise RepositoryError("unknown policy field")
            selected.update(policy)
        if selected["name"] != "repository-strict-v1" or selected["normalizer"] != "json-value-v1" \
                or selected["evaluator"] != "TraceAlignmentEngine/1.0.0" \
                or selected["profile"] != "strict_audit_v1":
            raise RepositoryError("unsupported policy, evaluator, or normalizer")
        for key in ("minimum_priority", "max_events"):
            if type(selected[key]) is not int:
                raise RepositoryError("policy numeric fields must be integers")
        if not 0 <= selected["minimum_priority"] <= 3 or not 1 <= selected["max_events"] <= 10000:
            raise RepositoryError("invalid policy priority or event budget")
        report: Dict[str, Any] = {"schema_version": "dpk.repository-evaluation.v1", "base": base_id,
                                  "candidate": candidate_id, "outcome": "invalid", "integrity_valid": False,
                                  "capture_complete": False, "policy_accepted": False, "findings": [],
                                  "workflow_changes": [],
                                  "policy": selected, "reason": ""}
        try:
            self._closure([base_id, candidate_id])
            base_commit = self._payload(base_id, "commit")
            candidate_commit = self._payload(candidate_id, "commit")
            left = self._payload(base_commit["run"], "run")
            right = self._payload(candidate_commit["run"], "run")
            left_workflow = self._payload(base_commit["workflow"], "workflow")
            right_workflow = self._payload(candidate_commit["workflow"], "workflow")
            report["workflow_changes"] = self._workflow_changes(left_workflow, right_workflow)
            report["integrity_valid"] = True
            if not left["graph_health"]["valid"] or not right["graph_health"]["valid"]:
                report["reason"] = "invalid recorded graph"
            elif not _capture_complete(left) or not _capture_complete(right):
                report["outcome"] = "incomplete"
                report["reason"] = "capture is empty, lossy, unbound, unfinished, or has unknown provenance"
            else:
                report["capture_complete"] = True
                minimum = selected["minimum_priority"]
                left_events = [e for e in left["events"] if e["priority"] >= minimum]
                right_events = [e for e in right["events"] if e["priority"] >= minimum]
                if max(len(left_events), len(right_events)) > selected["max_events"]:
                    report["outcome"] = "incomplete"
                    report["reason"] = "comparison event budget exceeded"
                elif not left_events or not right_events:
                    report["outcome"] = "incomplete"
                    report["reason"] = "no events meet the comparison priority floor"
                else:
                    result = self._strict_alignment(left_events, right_events)
                    mapping = {v.target_id: v.candidate_id for v in result.verdicts if v.candidate_id}
                    left_ids, right_ids = {e["id"] for e in left_events}, {e["id"] for e in right_events}
                    left_edges = {(mapping.get(e["source_id"]), mapping.get(e["target_id"]), e["type"])
                                  for e in left["edges"] if e["source_id"] in left_ids and e["target_id"] in left_ids}
                    right_edges = {(e["source_id"], e["target_id"], e["type"]) for e in right["edges"]
                                   if e["source_id"] in right_ids and e["target_id"] in right_ids}
                    provenance_ok = left_edges == right_edges
                    accepted = result.conforming and provenance_ok
                    report.update({"outcome": "accepted" if accepted else "regressed", "policy_accepted": accepted,
                                   "reason": "configured path policy passed" if accepted else "recorded decision path changed",
                                   "metrics": {"tp": result.tp, "fp": result.fp, "fn": result.fn},
                                   "gates": {"path": result.path_gate_passed, "hierarchy": result.hierarchy_gate_passed,
                                             "order": result.order_gate_passed, "provenance": provenance_ok}})
                    report["findings"] = [
                        {"reason": v.reason, "base_event": v.target_id, "candidate_event": v.candidate_id,
                         "suggested_candidate": v.suggested_candidate_id, "matched": v.matched,
                         "gate_passed": v.gate_passed}
                        for v in result.verdicts if not v.gate_passed
                    ]
                    assigned = set(mapping.values())
                    report["findings"].extend({"reason": "EXTRA_CANDIDATE_EVENT", "candidate_event": e["id"]}
                                              for e in right_events if e["id"] not in assigned)
                    if not provenance_ok:
                        report["findings"].append({"reason": "PROVENANCE_EDGE_CHANGED"})
        except (RepositoryError, RepositoryFormatError, ValueError, TypeError, RecursionError) as exc:
            report["reason"] = str(exc)
        if not report["integrity_valid"]:
            return {**report, "evaluation_id": None}
        payload = {"base": base_id, "candidate": candidate_id, "policy": selected,
                   "policy_digest": hashlib.sha256(_json_bytes(selected)).hexdigest(), "report": report}
        evaluation_id = object_id(encode_object("evaluation", payload))
        if persist:
            with self._transaction():
                self._put("evaluation", payload)
        return {**report, "evaluation_id": evaluation_id}

    @staticmethod
    def _workflow_changes(left: Mapping[str, Any], right: Mapping[str, Any]) -> List[Dict[str, Any]]:
        changes = []
        for name in sorted(set(left["assets"]) | set(right["assets"]), key=lambda x: x.encode("utf-8")):
            before, after = left["assets"].get(name), right["assets"].get(name)
            if before != after:
                changes.append({"kind": "asset", "name": name, "before": before, "after": after})
        for name in ("config", "source", "capture_policy"):
            if left[name] != right[name]:
                changes.append({"kind": "metadata", "name": name, "before": left[name], "after": right[name]})
        return changes

    def _nodes(self, events: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        first_by_span: Dict[str, str] = {}
        for e in events:
            if e["span_id"] is not None:
                first_by_span.setdefault(e["span_id"], e["id"])
        result = []
        for e in events:
            raw = self._payload(e["payload"], "blob")["data"]
            signature = _payload_signature(raw)
            parsed = json.loads(raw.decode("utf-8"))
            logical_id = parsed.get("dpk.logical_step_id") if isinstance(parsed, dict) else None
            if logical_id is not None and (not isinstance(logical_id, str) or not logical_id):
                raise RepositoryFormatError("logical step ID must be a nonempty string")
            result.append({"id": e["id"], "parent_id": first_by_span.get(e["parent_span_id"]),
                           "type": json.dumps([e["type"], e["engine"], logical_id], separators=(",", ":")),
                           "payload_signature": signature})
        return result

    def _strict_alignment(self, left: Sequence[Mapping[str, Any]],
                          right: Sequence[Mapping[str, Any]]) -> Any:
        """Reuse the released strict gate; add independent span/order topology checks."""
        from .alignment_config import AlignmentConfiguration, AlignmentProfile
        from .alignment_engine import TraceAlignmentEngine
        from .event import AnyTraceableEvent, TraceEvent
        from .priority import TracePriority
        from .query import TraceRun
        from .testing import RegressionGate, exact_equality_evaluator

        left_nodes, right_nodes = self._nodes(left), self._nodes(right)

        # The common case is a fresh execution of the same ordered path. Exact
        # normalized content plus the same positional parent topology proves
        # the strict one-to-one mapping without allocating a quadratic matcher.
        # Provenance edges are still checked independently by compare().
        if len(left_nodes) == len(right_nodes):
            left_positions = {node["id"]: i for i, node in enumerate(left_nodes)}
            right_positions = {node["id"]: i for i, node in enumerate(right_nodes)}
            def signature(nodes, events, positions):
                return [(node["type"], node["payload_signature"], event["priority"],
                         positions.get(node["parent_id"]) if node["parent_id"] else None)
                        for node, event in zip(nodes, events)]
            if signature(left_nodes, left, left_positions) == signature(right_nodes, right, right_positions):
                verdicts = [SimpleNamespace(
                    target_id=a["id"], candidate_id=b["id"], suggested_candidate_id=None,
                    matched=True, gate_passed=True, reason="CONFORMANT",
                ) for a, b in zip(left_nodes, right_nodes)]
                return SimpleNamespace(
                    verdicts=verdicts, tp=len(left), fp=0, fn=0, conforming=True,
                    path_gate_passed=True, hierarchy_gate_passed=True, order_gate_passed=True,
                )

        def run(events: Sequence[Mapping[str, Any]], nodes: Sequence[Mapping[str, Any]]) -> TraceRun:
            return TraceRun(uuid.UUID(events[0]["run_id"]), events[0]["context_id"], [
                TraceEvent(id=uuid.UUID(e["id"]), run_id=uuid.UUID(e["run_id"]),
                           context_id=e["context_id"], engine_name=e["engine"],
                           schema_version=e["schema_version"], sequence=e["sequence"],
                           span_id=e["span_id"], parent_span_id=e["parent_span_id"],
                           payload=AnyTraceableEvent(node["type"], e["priority"], node["payload_signature"]),
                           timestamp=e["timestamp_us"] / 1000000)
                for e, node in zip(events, nodes)])

        a, b = run(left, left_nodes), run(right, right_nodes)
        gate = RegressionGate(minimum_priority=TracePriority.TELEMETRY).check(a, b)
        result = TraceAlignmentEngine(AlignmentConfiguration(
            profile=AlignmentProfile.strict_audit_v1,
            equivalence_evaluator=exact_equality_evaluator())).align(
                a, b, minimum_priority=TracePriority.TELEMETRY)
        matched = [v for v in result.alignments if v.base_event is not None and v.comparison_event is not None]
        mapping = {}
        for aligned in matched:
            assert aligned.base_event is not None and aligned.comparison_event is not None
            mapping[str(aligned.base_event.id)] = str(aligned.comparison_event.id)
        parent_left = {n["id"]: n["parent_id"] for n in left_nodes}
        parent_right = {n["id"]: n["parent_id"] for n in right_nodes}
        hierarchy = all(parent_right[candidate] == (mapping.get(parent_left[target])
                        if parent_left[target] is not None else None)
                        and (parent_left[target] is None or parent_left[target] in mapping)
                        for target, candidate in mapping.items())
        candidate_positions = {node["id"]: index for index, node in enumerate(right_nodes)}
        mapped_order = [(node["id"], candidate_positions[mapping[node["id"]]])
                        for node in left_nodes if node["id"] in mapping]
        reordered: Set[str] = set()
        largest = -1
        for target, position in mapped_order:
            if position < largest:
                reordered.add(target)
            largest = max(largest, position)
        smallest = len(right_nodes)
        for target, position in reversed(mapped_order):
            if position > smallest:
                reordered.add(target)
            smallest = min(smallest, position)
        order = not reordered
        reasons = {"removed": "MISSING_REQUIRED_EVENT", "added": "EXTRA_CANDIDATE_EVENT",
                   "reordered": "REORDERED_REQUIRED_EVENTS", "ambiguous": "AMBIGUOUS_ALIGNMENT"}
        verdicts = []
        for v in result.alignments:
            if v.base_event is None:
                continue
            target = str(v.base_event.id)
            candidate = str(v.comparison_event.id) if v.comparison_event else None
            reason = reasons.get(v.state.kind.value, "CONFORMANT")
            if target in reordered:
                reason = "REORDERED_REQUIRED_EVENTS"
            if candidate and parent_right[candidate] != (mapping.get(parent_left[target])
                                                        if parent_left[target] else None):
                reason = "PARENT_HIERARCHY_MISMATCH"
            verdicts.append(SimpleNamespace(target_id=target, candidate_id=candidate,
                                            suggested_candidate_id=None, matched=candidate is not None,
                                            gate_passed=reason == "CONFORMANT", reason=reason))
        return SimpleNamespace(verdicts=verdicts, tp=len(matched), fp=len(right) - len(matched),
                               fn=len(left) - len(matched), conforming=gate.passed and hierarchy and order,
                               path_gate_passed=gate.passed, hierarchy_gate_passed=hierarchy,
                               order_gate_passed=order)

    def promote(self, candidate: str, evaluation_id: str, baseline: str = "default",
                expected_old: Optional[str] = None) -> str:
        with self._transaction():
            target = self.resolve(candidate)
            evaluation = self._payload(evaluation_id, "evaluation")
            if evaluation["candidate"] != target:
                raise RepositoryError("evaluation names a different candidate")
            if expected_old is None:
                if evaluation["base"] != target:
                    raise RepositoryError("initial baseline requires an accepted self-comparison")
            elif evaluation["base"] != expected_old:
                raise RepositoryError("evaluation does not compare the expected baseline")
            # Recompute from immutable inputs, never trust a stored report's assertion.
            current = self.compare(evaluation["base"], target, evaluation["policy"], persist=False)
            if current["outcome"] != "accepted" or current["evaluation_id"] != evaluation_id:
                raise RepositoryError("baseline promotion requires a verified, complete passing evaluation")
            name = _ref_name(baseline, "baselines")
            if not name.startswith("refs/baselines/"):
                raise RepositoryError("baseline requires baselines namespace")
            self._update_ref(name, target, expected_old)
            return target

    def fsck(self) -> Dict[str, Any]:
        with self._lock:
            self._open()
            errors = []
            objects: Dict[str, Dict[str, Any]] = {}
            ids = [r[0] for r in self._db.execute("SELECT id FROM objects ORDER BY id")]
            try:
                objects = self._closure(ids)
            except (ValueError, KeyError, TypeError, ImportError) as exc:
                errors.append({"id": "closure", "reason": str(exc)})
            for name, target in self.refs().items():
                try:
                    _ref_name(name)
                    self._payload(target, "commit")
                except ValueError as exc:
                    errors.append({"ref": name, "reason": str(exc)})
            invalid_graphs = [oid for oid, obj in objects.items() if obj["type"] == "run"
                              and not obj["payload"]["graph_health"]["valid"]]
            incomplete_runs = [oid for oid, obj in objects.items() if obj["type"] == "run"
                               and not _capture_complete(obj["payload"])]
            return {"valid": not errors and not invalid_graphs, "integrity_valid": not errors,
                    "graph_valid": not invalid_graphs, "invalid_graph_runs": sorted(invalid_graphs),
                    "incomplete_runs": sorted(incomplete_runs), "object_count": len(ids),
                    "ref_count": len(self.refs()), "errors": errors}

    def export_bundle(self, path: Union[str, Path], roots: Optional[Iterable[str]] = None,
                      refs: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
        with self._lock:
            self._open()
            self._db.execute("BEGIN")
            try:
                chosen_refs = dict(refs) if refs is not None else self.refs()
                resolved_refs = {_ref_name(k): self.resolve(v) for k, v in chosen_refs.items()}
                root_ids = sorted(set(self.resolve(r) for r in roots)) if roots is not None else sorted(set(resolved_refs.values()))
                if not root_ids:
                    raise RepositoryError("bundle requires at least one root")
                objects = self._closure(root_ids, max_objects=MAX_BUNDLE_OBJECTS)
                for oid, in self._db.execute("SELECT id FROM objects WHERE type='signature'").fetchall():
                    signature = self._payload(oid, "signature")
                    if signature["subject"] in objects:
                        objects.update(self._closure([oid]))
                if len(objects) > MAX_BUNDLE_OBJECTS:
                    raise RepositoryError("bundle object count limit exceeded")
                if roots is not None and refs is None:
                    resolved_refs = {name: target for name, target in resolved_refs.items() if target in objects}
                if any(target not in objects for target in resolved_refs.values()):
                    raise RepositoryError("exported refs must point inside the selected root closure")
                manifest = {"format": BUNDLE_FORMAT, "version": 1, "roots": root_ids,
                            "objects": sorted(objects), "refs": resolved_refs}
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
                    self._zip_write(archive, "manifest.json", _json_bytes(manifest))
                    for oid in sorted(objects):
                        data = self._db.execute("SELECT data FROM objects WHERE id=?", (oid,)).fetchone()[0]
                        self._zip_write(archive, "objects/" + oid + ".bin", bytes(data))
                data = output.getvalue()
                if len(data) > MAX_BUNDLE_BYTES:
                    raise RepositoryError("bundle size limit exceeded")
            finally:
                self._db.rollback()
        destination = Path(path)
        if destination.resolve() in {self.path.resolve(), Path(str(self.path) + "-wal").resolve(),
                                      Path(str(self.path) + "-shm").resolve()}:
            raise RepositoryError("bundle destination cannot replace the repository database")
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(dir=str(destination.parent), prefix=".dpk-bundle-", delete=False) as handle:
                temporary_name = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        finally:
            if temporary_name is not None and os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return manifest

    @staticmethod
    def _zip_write(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, data)

    def import_bundle(self, path: Union[str, Path], ref_mapping: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
        bundle_source = Path(path)
        if bundle_source.stat().st_size > MAX_BUNDLE_BYTES:
            raise RepositoryFormatError("bundle size limit exceeded")
        raw = bundle_source.read_bytes()
        if len(raw) > MAX_BUNDLE_BYTES:
            raise RepositoryFormatError("bundle size limit exceeded")
        try:
            _validate_zip_layout(raw)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = archive.infolist()
                names = [i.filename for i in infos]
                if len(names) != len(set(names)) or len(names) > MAX_BUNDLE_OBJECTS + 1:
                    raise RepositoryFormatError("duplicate ZIP entry or object count limit")
                if any(i.compress_type != zipfile.ZIP_STORED or i.flag_bits & 1 for i in infos):
                    raise RepositoryFormatError("only unencrypted ZIP_STORED entries are supported")
                if "manifest.json" not in names or any(i.file_size > MAX_OBJECT_BYTES for i in infos):
                    raise RepositoryFormatError("missing manifest or oversized entry")
                manifest_bytes = archive.read("manifest.json")
                manifest = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=_json_object_pairs)
                _require(isinstance(manifest, dict) and set(manifest) == {"format", "version", "roots", "objects", "refs"},
                         "invalid bundle manifest")
                _require(manifest["format"] == BUNDLE_FORMAT and type(manifest["version"]) is int
                         and manifest["version"] == 1, "unsupported bundle format")
                _require(_json_bytes(manifest) == manifest_bytes, "bundle manifest must use canonical JSON")
                for field in ("objects", "roots"):
                    values = manifest[field]
                    _require(isinstance(values, list) and all(_is_id(x) for x in values)
                             and values == sorted(set(values)), "invalid or unsorted bundle IDs")
                _require(bool(manifest["roots"]) and set(manifest["roots"]) <= set(manifest["objects"]), "invalid bundle roots")
                _require(isinstance(manifest["refs"], dict), "invalid bundle refs")
                _require(set(names) == {"manifest.json"} | {"objects/" + x + ".bin" for x in manifest["objects"]},
                         "unexpected ZIP path or missing object")
                objects = {}
                for oid in manifest["objects"]:
                    data = archive.read("objects/" + oid + ".bin")
                    _require(object_id(data) == oid, "bundle object hash mismatch")
                    kind, payload = decode_object(data)
                    _shape(kind, payload)
                    if kind == "signature":
                        _require(_verify_signature_payload(payload), "bundle contains an invalid detached signature")
                    objects[oid] = (kind, payload, data)
                for oid, (kind, payload, _) in objects.items():
                    for ref, expected in _references(kind, payload).items():
                        _require(ref in objects and objects[ref][0] == expected, "bundle is not a complete typed closure")
                    if kind == "commit":
                        _require(objects[payload["run"]][1]["workflow"] == payload["workflow"], "commit/run workflow mismatch")
                for name, target in manifest["refs"].items():
                    _require(_ref_name(name) == name, "bundle refs must use full ref names")
                    _require(target in objects and objects[target][0] == "commit", "bundle ref must name included commit")
                reachable = set()
                pending = list(manifest["roots"])
                while pending:
                    oid = pending.pop()
                    if oid in reachable:
                        continue
                    reachable.add(oid)
                    pending.extend(_references(objects[oid][0], objects[oid][1]))
                for oid, (kind, payload, _) in objects.items():
                    if kind == "signature" and payload["subject"] in reachable:
                        reachable.add(oid)
                _require(reachable == set(objects), "bundle contains unreachable inventory objects")
        except (zipfile.BadZipFile, KeyError, UnicodeError, json.JSONDecodeError) as exc:
            raise RepositoryFormatError("invalid bundle: " + str(exc)) from exc
        mapping = dict(ref_mapping or {})
        destinations: Dict[str, str] = {}
        for source, destination in mapping.items():
            if source not in manifest["refs"]:
                raise RepositoryError("mapped source ref is missing from bundle")
            name = _ref_name(destination)
            if name.startswith("refs/baselines/"):
                raise RepositoryError("import cannot promote baseline refs")
            if name in destinations:
                raise RepositoryError("duplicate import destination")
            destinations[name] = manifest["refs"][source]
        with self._transaction():
            for name in destinations:
                if self._db.execute("SELECT 1 FROM refs WHERE name=?", (name,)).fetchone():
                    raise RefConflictError("import never overwrites an existing ref")
            for kind, payload, _ in objects.values():
                self._put(kind, payload)
            self._closure(manifest["roots"])
            for name, target in destinations.items():
                self._update_ref(name, target, None)
        return {"imported_objects": len(objects), "roots": manifest["roots"], "refs": destinations}

    def sign(self, subject: str, key: Any) -> str:
        """Attach a P-256 signature without changing the signed commit's ID."""
        with self._transaction():
            oid = self.resolve(subject)
            self._closure([oid])
            self._payload(oid, "commit")
            public = key.public_key_x963_representation
            payload = b"DPK-REPOSITORY-SIGNATURE-V1\x00" + bytes.fromhex(oid)
            return self._put("signature", {"subject": oid, "algorithm": "P256-SHA256",
                                           "key_id": hashlib.sha256(public).hexdigest(), "public_key": public,
                                           "signature": key.signature_der(payload)})

    def verify_signatures(self, subject: str, trusted_key_ids: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        from .attestation import _require_crypto

        _require_crypto()
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        with self._lock:
            oid = self.resolve(subject)
            self._closure([oid])
            self._payload(oid, "commit")
            trusted = set(trusted_key_ids) if trusted_key_ids is not None else None
            results = []
            for signature_id, in self._db.execute("SELECT id FROM objects WHERE type='signature' ORDER BY id").fetchall():
                p = self._payload(signature_id, "signature")
                if p["subject"] != oid:
                    continue
                valid = False
                try:
                    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p["public_key"])
                    public.verify(p["signature"], b"DPK-REPOSITORY-SIGNATURE-V1\x00" + bytes.fromhex(oid),
                                  ec.ECDSA(hashes.SHA256()))
                    valid = True
                except (ValueError, InvalidSignature):
                    pass
                results.append({"id": signature_id, "key_id": p["key_id"], "valid": valid,
                                "trusted": valid and trusted is not None and p["key_id"] in trusted})
            return {"subject": oid, "signatures": results,
                    "integrity_valid": any(r["valid"] for r in results),
                    "trusted_signer": any(r["trusted"] for r in results)}
