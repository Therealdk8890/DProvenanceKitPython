"""Projects, API keys, roles, plans, and usage — a durable, multi-tenant control plane.

Replaces the static ``DPROV_API_KEYS`` env map with a SQLite-backed tenancy:

* **Projects** are first-class and carry a **plan** (``free`` / ``pro``).
* **API keys** are stored **hashed** (only their SHA-256 is persisted; the raw key is shown
  once at creation), carry a **role** (``read`` / ``write`` / ``admin``), and can be revoked.
* **Usage** (events ingested, gate checks) is metered per project for quota enforcement and
  billing.

Managed via the admin CLI (``python server/admin.py``). Dependency-free (stdlib ``sqlite3``);
tenant metadata lives in its own database, separate from the per-project trace stores.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
import uuid
from typing import List, Optional, Tuple

ROLES = ("read", "write", "admin")
PLANS = ("free", "pro")


def _hash(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class Tenancy:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
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
                "(id TEXT PRIMARY KEY, name TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'free', "
                " created_at INTEGER NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS api_keys "
                "(key_hash TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT, "
                " role TEXT NOT NULL DEFAULT 'write', created_at INTEGER NOT NULL, "
                " revoked INTEGER NOT NULL DEFAULT 0)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS usage "
                "(project_id TEXT PRIMARY KEY, events INTEGER NOT NULL DEFAULT 0, "
                " gate_calls INTEGER NOT NULL DEFAULT 0)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_keys_project ON api_keys(project_id)")
            # Forward migrations for databases created before these columns existed.
            for ddl in (
                "ALTER TABLE projects ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'",
                "ALTER TABLE api_keys ADD COLUMN role TEXT NOT NULL DEFAULT 'write'",
            ):
                try:
                    c.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # column already present

    # -- projects ----------------------------------------------------------------

    def create_project(self, name: str, plan: str = "free") -> str:
        if plan not in PLANS:
            raise ValueError(f"unknown plan: {plan}")
        project_id = "proj_" + uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT INTO projects (id, name, plan, created_at) VALUES (?,?,?,?)",
                (project_id, name, plan, int(time.time())),
            )
        return project_id

    def list_projects(self) -> List[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute("SELECT id, name, plan, created_at FROM projects ORDER BY created_at"))

    def is_empty(self) -> bool:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 0

    def get_plan(self, project_id: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT plan FROM projects WHERE id = ?", (project_id,)).fetchone()
            return row["plan"] if row else None

    def set_plan(self, project_id: str, plan: str) -> bool:
        """Set a project's plan. A Stripe webhook (checkout/subscription updated) would call
        this — the billing seam is intentionally this one method."""
        if plan not in PLANS:
            raise ValueError(f"unknown plan: {plan}")
        with self._conn() as c:
            cur = c.execute("UPDATE projects SET plan = ? WHERE id = ?", (plan, project_id))
            return cur.rowcount > 0

    # -- api keys ----------------------------------------------------------------

    def create_api_key(self, project_id: str, name: Optional[str] = None, role: str = "write") -> str:
        """Mint a key for a project (role ``read``/``write``/``admin``). Returns the raw key
        — shown ONCE; only its hash is stored, so it cannot be recovered later."""
        if role not in ROLES:
            raise ValueError(f"unknown role: {role}")
        with self._conn() as c:
            if c.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise KeyError(f"no such project: {project_id}")
            raw = "dpk_" + secrets.token_urlsafe(24)
            c.execute(
                "INSERT INTO api_keys (key_hash, project_id, name, role, created_at) VALUES (?,?,?,?,?)",
                (_hash(raw), project_id, name, role, int(time.time())),
            )
        return raw

    def resolve(self, raw_key: str) -> Optional[Tuple[str, str, str]]:
        """Return ``(project_id, project_name, role)`` for a valid, non-revoked key."""
        with self._conn() as c:
            row = c.execute(
                "SELECT p.id AS id, p.name AS name, k.role AS role FROM api_keys k "
                "JOIN projects p ON p.id = k.project_id "
                "WHERE k.key_hash = ? AND k.revoked = 0",
                (_hash(raw_key),),
            ).fetchone()
            return (row["id"], row["name"], row["role"]) if row else None

    def list_keys(self, project_id: str) -> List[sqlite3.Row]:
        with self._conn() as c:
            return list(
                c.execute(
                    "SELECT key_hash, name, role, created_at, revoked FROM api_keys "
                    "WHERE project_id = ? ORDER BY created_at",
                    (project_id,),
                )
            )

    def revoke(self, raw_key: str) -> bool:
        with self._conn() as c:
            cur = c.execute("UPDATE api_keys SET revoked = 1 WHERE key_hash = ?", (_hash(raw_key),))
            return cur.rowcount > 0

    # -- usage -------------------------------------------------------------------

    def get_usage(self, project_id: str) -> Tuple[int, int]:
        with self._conn() as c:
            row = c.execute("SELECT events, gate_calls FROM usage WHERE project_id = ?", (project_id,)).fetchone()
            return (row["events"], row["gate_calls"]) if row else (0, 0)

    def record_usage(self, project_id: str, *, events: int = 0, gate_calls: int = 0) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO usage (project_id, events, gate_calls) VALUES (?,?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "events = events + excluded.events, gate_calls = gate_calls + excluded.gate_calls",
                (project_id, events, gate_calls),
            )
