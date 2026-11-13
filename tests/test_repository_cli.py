"""Exercise public CLI workflows, independent of storage implementation details."""

import json
import hashlib

import pytest

from dprovenancekit.cli import main
from dprovenancekit import ReasoningRepository, SQLiteTraceStore
from dprovenancekit.instrument import TracedEvent, record_event, traced_run


def _capture(repo, store, workflow, verify=True):
    with traced_run(store, repository=repo, workflow_snapshot=workflow) as active:
        record_event("retrieve")
        if verify:
            record_event("verify")
        record_event("answer", {"result": "same"})
    return active


def test_cli_commits_can_advance_existing_branch_and_gate(tmp_path, capsys):
    path = tmp_path / "history.sqlite"
    assert main(["repo", "init", "--repo", str(path)]) == 0
    capsys.readouterr()
    with ReasoningRepository(path) as repo, SQLiteTraceStore(TracedEvent, str(tmp_path / "trace.sqlite")) as store:
        workflow = repo.snapshot_workflow({"prompt.txt": b"verify"})
        first = _capture(repo, store, workflow)
        second = _capture(repo, store, workflow, verify=False)
    for active in (first, second):
        assert main(["repo", "--repo", str(path), "commit", "--run", str(active.run_id), "-m", "recorded"]) == 0
        output = capsys.readouterr().out.strip()
        assert len(output) == 64
        if active is first:
            baseline = output
        else:
            candidate = output
    args = ["repo", "--repo", str(path), "gate", "--baseline", baseline, "--candidate", candidate, "--json"]
    assert main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "regressed"
    args[3] = "diff"
    assert main(args) == 0
    capsys.readouterr()
    assert main(["repo", "status", "--repo", str(path)]) == 0
    assert "refs/heads/main" in json.loads(capsys.readouterr().out)["refs"]


def test_cli_snapshot_only_includes_selected_files(tmp_path, capsys):
    path = tmp_path / "history.sqlite"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Use the verification tool.", encoding="utf-8")
    (tmp_path / "private.env").write_text("SECRET=not-an-asset", encoding="utf-8")
    assert main(["repo", "init", "--repo", str(path)]) == 0
    capsys.readouterr()
    assert main(["repo", "snapshot", "--repo", str(path), "--asset", "prompt=" + str(prompt)]) == 0
    workflow = capsys.readouterr().out.strip()
    with ReasoningRepository(path, read_only=True) as repo:
        assert set(repo.get_object(workflow)["payload"]["assets"]) == {"prompt"}


def test_missing_repository_fails_without_creating_a_file(tmp_path, capsys):
    path = tmp_path / "missing.sqlite"
    assert main(["repo", "log", "--repo", str(path)]) == 2
    assert not path.exists()
    assert "does not exist" in capsys.readouterr().err


def test_offline_history_demo_reproduces_gate_after_bundle_import(tmp_path, capsys):
    output = tmp_path / "demo"
    assert main(["repo", "demo", "--output-dir", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["offline_roundtrip_verified"]
    assert report["regression"]["outcome"] == "regressed"
    assert report["matching_evaluation"]["outcome"] == "accepted"
    assert main(["repo", "demo", "--output-dir", str(output)]) == 2
    assert "never replaces" in capsys.readouterr().err


def test_cli_signing_distinguishes_signature_integrity_from_pinned_trust(tmp_path, capsys):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    path = tmp_path / "history.sqlite"
    key = ec.generate_private_key(ec.SECP256R1())
    private = tmp_path / "test-key.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    public = key.public_key().public_bytes(serialization.Encoding.X962,
                                          serialization.PublicFormat.UncompressedPoint)
    with ReasoningRepository(path) as repo, SQLiteTraceStore(TracedEvent, str(tmp_path / "trace.sqlite")) as store:
        workflow = repo.snapshot_workflow({"prompt.txt": b"verify"})
        run = _capture(repo, store, workflow)
        commit = repo.commit(run.repository_evidence_id, "signed")
    assert main(["repo", "--repo", str(path), "verify", commit]) == 2
    capsys.readouterr()
    assert main(["repo", "--repo", str(path), "sign", commit, "--key", str(private)]) == 0
    assert "PRIVATE KEY" not in capsys.readouterr().out
    args = ["repo", "--repo", str(path), "verify", commit]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["integrity_valid"] and not report["trusted_signer"]
    assert main(args + ["--trusted-key", "f" * 64]) == 2
    capsys.readouterr()
    assert main(args + ["--trusted-key", hashlib.sha256(public).hexdigest()]) == 0
