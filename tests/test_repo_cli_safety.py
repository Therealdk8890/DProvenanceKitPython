"""Fail-closed regression tests for the experimental reasoning repository CLI."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from dprovenancekit import repo_cli


class FakeRepository:
    def __init__(self, *, outcome=None, tip="parent", evidence=None):
        self.outcome = outcome
        self.tip = tip
        self.evidence = evidence
        self.commit_called = False
        self.commit_kwargs = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def resolve(self, ref):
        if self.tip is None:
            raise KeyError(ref)
        return self.tip

    def refs(self):
        return {}

    def branches(self):
        return {}

    def fsck(self):
        return self.outcome

    def verify_signatures(self, subject, trusted_keys):
        assert subject == self.tip
        assert trusted_keys == ["trusted-1"]
        return self.outcome

    def lookup_run(self, run_id):
        return self.evidence

    def commit(self, evidence, message, *, branch, expected_parent, author):
        self.commit_called = True
        self.commit_kwargs = {
            "evidence": evidence, "message": message,
            "branch": branch, "expected_parent": expected_parent, "author": author,
        }
        return "commit-id"

    def log(self, ref, *, limit, offset):
        return []


@pytest.mark.parametrize("outcome", [
    None, [], True, "verified", {},
    {"valid": "true"}, {"verified": 1}, {"valid": False},
    {"valid": True, "verified": False},
])
def test_verification_rejects_nonboolean_or_conflicting_success(tmp_path, outcome):
    path = tmp_path / "repository.sqlite"
    path.touch()
    fake = FakeRepository(outcome=outcome)
    result = repo_cli.main(
        ["--repo", str(path), "verify", "HEAD", "--trusted-key", "trusted-1"],
        repository_factory=lambda _: fake,
    )
    assert result == 2


@pytest.mark.parametrize("outcome", [{"valid": True}, {"verified": True},
                                    {"valid": True, "verified": True}])
def test_verification_accepts_explicit_valid_boolean(tmp_path, outcome):
    path = tmp_path / "repository.sqlite"
    path.touch()
    fake = FakeRepository(outcome=outcome)
    assert repo_cli.main(
        ["--repo", str(path), "verify", "HEAD", "--trusted-key", "trusted-1"],
        repository_factory=lambda _: fake,
    ) == 0


@pytest.mark.parametrize("outcome", [{}, {"ok": 1}, {"valid": "false"},
                                    {"valid": False}, {"valid": True, "ok": False}, []])
def test_fsck_fails_closed(tmp_path, outcome):
    path = tmp_path / "repository.sqlite"
    path.touch()
    assert repo_cli.main(
        ["--repo", str(path), "fsck"],
        repository_factory=lambda _: FakeRepository(outcome=outcome),
    ) == 2


def test_fsck_accepts_explicit_boolean(tmp_path):
    path = tmp_path / "repository.sqlite"
    path.touch()
    assert repo_cli.main(
        ["--repo", str(path), "fsck"],
        repository_factory=lambda _: FakeRepository(outcome={"valid": True}),
    ) == 0


def test_existing_branch_requires_expected_parent_before_commit(tmp_path, capsys):
    path = tmp_path / "repository.sqlite"
    path.touch()
    fake = FakeRepository(tip="parent", evidence="sealed-evidence")
    result = repo_cli.main(
        ["--repo", str(path), "commit", "--evidence", "sealed-evidence", "-m", "test"],
        repository_factory=lambda _: fake,
    )
    assert result == 2
    assert not fake.commit_called
    assert "--expected-parent" in capsys.readouterr().err


def test_stale_parent_does_not_commit(tmp_path, capsys):
    path = tmp_path / "repository.sqlite"
    path.touch()
    fake = FakeRepository(tip="changed", evidence="sealed-evidence")
    result = repo_cli.main(
        ["--repo", str(path), "commit", "--evidence", "sealed-evidence",
         "-m", "test", "--expected-parent", "stale"],
        repository_factory=lambda _: fake,
    )
    assert result == 2
    assert not fake.commit_called
    assert "branch changed" in capsys.readouterr().err


def test_existing_branch_passes_compare_and_swap_precondition(tmp_path):
    path = tmp_path / "repository.sqlite"
    path.touch()
    fake = FakeRepository(tip="parent", evidence="sealed-evidence")
    result = repo_cli.main(
        ["--repo", str(path), "commit", "--evidence", "sealed-evidence",
         "-m", "test", "--expected-parent", "parent"],
        repository_factory=lambda _: fake,
    )
    assert result == 0
    assert fake.commit_kwargs["expected_parent"] == "parent"


def test_missing_readonly_backend_fails_without_mutating_source(tmp_path, capsys):
    path = tmp_path / "repository.sqlite"
    path.touch()
    source = tmp_path / "trace.sqlite"
    con = sqlite3.connect(str(source))
    con.execute("CREATE TABLE sentinel (value TEXT)")
    con.execute("INSERT INTO sentinel VALUES ('immutable')")
    con.commit()
    con.close()
    before = source.read_bytes()

    fake = FakeRepository(tip="parent")
    result = repo_cli.main(
        ["--repo", str(path), "commit", "--run", "run-id", "--db", str(source),
         "-m", "test", "--expected-parent", "parent"],
        repository_factory=lambda _: fake,
    )
    assert result == 2
    assert "read-only capture sealing" in capsys.readouterr().err
    assert source.read_bytes() == before
    assert not fake.commit_called


@pytest.mark.parametrize("limit,offset", [(0, 0), (-1, 0), (3, -1)])
def test_log_rejects_invalid_pagination(tmp_path, limit, offset):
    path = tmp_path / "repository.sqlite"
    path.touch()
    assert repo_cli.main(
        ["--repo", str(path), "log", "--limit", str(limit), "--offset", str(offset)],
        repository_factory=lambda _: FakeRepository(),
    ) == 2


def test_mapping_file_requires_object(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        repo_cli._mapping_file(str(config))


def test_cli_is_not_wired_into_legacy_dispatch():
    from dprovenancekit import cli
    # This preparatory safety patch intentionally leaves legacy routing untouched.
    assert cli.main(["--version"]) == 0
