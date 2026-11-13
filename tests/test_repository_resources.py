"""Opening incompatible data must not leave handles or rewrite foreign databases."""

import hashlib
import sqlite3

import pytest

from dprovenancekit import ReasoningRepository


@pytest.mark.parametrize("read_only", [True, False])
def test_invalid_database_closes_connection_before_constructor_raises(tmp_path, read_only):
    path = tmp_path / "invalid.sqlite"
    path.write_bytes(b"This is not a SQLite file.")
    with pytest.raises(sqlite3.DatabaseError):
        ReasoningRepository(path, read_only=read_only)
    # Windows refuses unlink while a connection leaked by __init__ remains open.
    path.unlink()


def test_foreign_sqlite_database_is_not_adopted_or_migrated(tmp_path):
    path = tmp_path / "application.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE business_data (value TEXT)")
    connection.commit()
    connection.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="nonempty database"):
        ReasoningRepository(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
