"""Projects and API keys — a durable, multi-tenant auth model.

Replaces the static ``DPROV_API_KEYS`` env map with a SQLite-backed tenancy: projects are
first-class, and API keys are stored **hashed** (only their SHA-256 is persisted; the raw
key is shown once at creation). Keys resolve to a project, can be named, and can be revoked.
Managed via the admin CLI (``python -m dprov_server.admin``).

Dependency-free (stdlib ``sqlite3``). Tenant metadata lives in its own database, separate
from the per-project trace stores.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
import uuid
from typing import List, Optional, Tuple


def _hash(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class Tenancy:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._init()

    @classmethod
    def default(cls) -> "Tenancy":
        path = os.environ.get("DPROV_TENANTS_DB") or os.path.join(
            os.environ.get("DPROV_DATA_DIR", "./dprov-data"), "tenants.sqlite"
        )
        return cls(path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS projects "
                "(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS api_keys "
                "(key_hash TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT, "
                " created_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_keys_project ON api_keys(project_id)")

    # -- projects ----------------------------------------------------------------

    def create_project(self, name: str) -> str:
        project_id = "proj_" + uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute("INSERT INTO projects VALUES (?,?,?)", (project_id, name, int(time.time())))
        return project_id

    def list_projects(self) -> List[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute("SELECT id, name, created_at FROM projects ORDER BY created_at"))

    def is_empty(self) -> bool:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 0

    # -- api keys ----------------------------------------------------------------

    def create_api_key(self, project_id: str, name: Optional[str] = None) -> str:
        """Mint a new API key for a project. Returns the raw key — shown ONCE; only its
        hash is stored, so it cannot be recovered later."""
        with self._conn() as c:
            if c.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise KeyError(f"no such project: {project_id}")
            raw = "dpk_" + secrets.token_urlsafe(24)
            c.execute(
                "INSERT INTO api_keys VALUES (?,?,?,?,0)",
                (_hash(raw), project_id, name, int(time.time())),
            )
        return raw

    def resolve(self, raw_key: str) -> Optional[Tuple[str, str]]:
        """Return ``(project_id, project_name)`` for a valid, non-revoked key, else ``None``."""
        with self._conn() as c:
            row = c.execute(
                "SELECT p.id AS id, p.name AS name FROM api_keys k "
                "JOIN projects p ON p.id = k.project_id "
                "WHERE k.key_hash = ? AND k.revoked = 0",
                (_hash(raw_key),),
            ).fetchone()
            return (row["id"], row["name"]) if row else None

    def list_keys(self, project_id: str) -> List[sqlite3.Row]:
        with self._conn() as c:
            return list(
                c.execute(
                    "SELECT key_hash, name, created_at, revoked FROM api_keys "
                    "WHERE project_id = ? ORDER BY created_at",
                    (project_id,),
                )
            )

    def revoke(self, raw_key: str) -> bool:
        with self._conn() as c:
            cur = c.execute("UPDATE api_keys SET revoked = 1 WHERE key_hash = ?", (_hash(raw_key),))
            return cur.rowcount > 0
