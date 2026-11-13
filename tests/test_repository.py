"""Repository invariants: exact history, explicit trust and transactional refs."""

import json
import base64
import sqlite3
import struct
import zipfile
from pathlib import Path

import pytest

from dprovenancekit.testing import RegressionGate

from dprovenancekit.instrument import TracedEvent, record_event, traced_run
from dprovenancekit.repository import (
    ReasoningRepository, RefConflictError, RepositoryError,
)
from dprovenancekit.repository_codec import (
    RepositoryFormatError, decode_object, encode_object, object_id,
)
from dprovenancekit.sqlite_store import SQLiteTraceStore


def capture(repo, store, workflow, steps=("retrieve", "plan", "verify", "answer")):
    with traced_run(store, context_id="loan-decision", repository=repo, workflow_snapshot=workflow) as run:
        for step in steps:
            record_event(step, {"answer": "approved"} if step == "answer" else {})
    return repo.seal_run(store, run.run_id)


def test_exact_ordered_fast_path_agrees_with_released_gate_for_repeated_types(environment):
    repo, store, workflow = environment
    runs = []
    commits = []
    for values in ((1, 2, 1, 3), (1, 2, 1, 3), (2, 1, 1, 3)):
        with traced_run(store, repository=repo, workflow_snapshot=workflow) as run:
            for value in values:
                record_event("check", {"value": value})
        runs.append(store.get_run(run.run_id))
        commits.append(repo.commit(run.repository_evidence_id, "repeated step fixture"))
    for index in (1, 2):
        reference = RegressionGate().check(runs[0], runs[index])
        actual = repo.compare(commits[0], commits[index])
        assert (actual["outcome"] == "accepted") == reference.passed
    assert repo.compare(commits[0], commits[1])["metrics"] == {"tp": 4, "fp": 0, "fn": 0}


@pytest.fixture
def environment(tmp_path):
    with ReasoningRepository(tmp_path / "history.sqlite") as repo:
        with SQLiteTraceStore(TracedEvent, str(tmp_path / "capture.sqlite")) as store:
            workflow = repo.snapshot_workflow({"prompt.txt": b"Retrieve, plan, verify, answer."},
                                               config={"model": "deterministic-demo", "temperature": "0"})
            yield repo, store, workflow


def test_missing_verify_history_gate_and_reopen(environment):
    repo, store, workflow = environment
    baseline = repo.commit(capture(repo, store, workflow), "known good")
    same = repo.commit(capture(repo, store, workflow), "fresh matching execution")
    assert baseline != same
    assert repo.compare(baseline, same)["outcome"] == "accepted"
    repo.create_branch("skip-verification", baseline)
    repo.switch("skip-verification")
    bad = repo.commit(capture(repo, store, workflow, ("retrieve", "answer")), "skip verify")
    report = repo.compare(baseline, bad)
    assert report["outcome"] == "regressed"
    assert report["metrics"]["fn"] == 2
    assert report["capture_complete"] is True
    assert repo.get_object(baseline)["payload"]["parents"] == []
    assert repo.get_object(bad)["payload"]["parents"] == [baseline]
    assert repo.log("skip-verification", limit=1, offset=1)[0]["id"] == baseline
    with ReasoningRepository(repo.path, read_only=True) as reopened:
        assert reopened.resolve("main") == same
        assert reopened.resolve("skip-verification") == bad
        assert reopened.fsck()["valid"]
        assert reopened.compare(baseline, bad, persist=False)["outcome"] == "regressed"
        with pytest.raises(RepositoryError, match="read-only"):
            reopened.create_branch("cannot-write", baseline)


def test_ref_compare_and_swap_across_connections(environment):
    repo, store, workflow = environment
    first = repo.commit(capture(repo, store, workflow), "first")
    run = capture(repo, store, workflow)
    with ReasoningRepository(repo.path) as other:
        second = other.commit(run, "second", expected_parent=first)
        count = repo.status()["object_count"]
        with pytest.raises(RefConflictError):
            repo.commit(run, "stale update", expected_parent=first)
        assert repo.resolve("main") == second
        assert repo.status()["object_count"] == count
    assert len(repo.reflog()) == 2


def test_baseline_promotion_recomputes_policy_and_uses_cas(environment):
    repo, store, workflow = environment
    first = repo.commit(capture(repo, store, workflow), "first")
    evaluation = repo.compare(first, first)
    repo.promote(first, evaluation["evaluation_id"])
    second = repo.commit(capture(repo, store, workflow), "second")
    accepted = repo.compare(first, second)
    repo.promote(second, accepted["evaluation_id"], expected_old=first)
    with pytest.raises(RefConflictError):
        repo.promote(second, accepted["evaluation_id"], expected_old=first)
    bad = repo.commit(capture(repo, store, workflow, ("retrieve", "answer")), "bad")
    rejected = repo.compare(second, bad)
    with pytest.raises(RepositoryError, match="passing"):
        repo.promote(bad, rejected["evaluation_id"], expected_old=second)
    assert repo.resolve("refs/baselines/default") == second


def test_binding_is_persistent_and_cannot_change(environment):
    repo, store, workflow = environment
    with traced_run(store) as run:
        repo.bind_run(run.run_id, workflow)
        store.bind_workflow(run.run_id, workflow)
        record_event("verify")
        with pytest.raises(ValueError):
            repo.seal_run(store, run.run_id)
    other_workflow = repo.snapshot_workflow({"prompt": b"another"})
    with ReasoningRepository(repo.path) as reopened:
        evidence = reopened.seal_run(store, run.run_id)
        assert repo.lookup_run(run.run_id) == evidence
        assert repo.seal_run(store, run.run_id) == evidence
        with pytest.raises(RepositoryError, match="another"):
            reopened.bind_run(run.run_id, other_workflow)


def test_posthoc_and_failed_capture_are_not_complete(environment):
    repo, store, workflow = environment
    with traced_run(store) as unbound:
        record_event("verify")
    legacy = repo.commit(repo.seal_run(store, unbound.run_id, workflow), "post hoc")
    assert repo.compare(legacy, legacy)["outcome"] == "incomplete"
    with pytest.raises(RuntimeError):
        with traced_run(store) as failed:
            repo.bind_run(failed.run_id, workflow)
            store.bind_workflow(failed.run_id, workflow)
            record_event("verify")
            raise RuntimeError("tool failed")
    failed_commit = repo.commit(repo.seal_run(store, failed.run_id), "failed capture")
    assert repo.compare(failed_commit, failed_commit)["outcome"] == "incomplete"


def test_budget_and_empty_capture_do_not_pass(environment):
    repo, store, workflow = environment
    full = repo.commit(capture(repo, store, workflow), "full")
    report = repo.compare(full, full, {"max_events": 1})
    assert report["outcome"] == "incomplete"
    assert "budget" in report["reason"]
    empty = repo.commit(capture(repo, store, workflow, ()), "empty")
    assert repo.compare(empty, empty)["outcome"] == "incomplete"


def test_structural_reordering_fails_independent_order_gate(environment):
    repo, store, workflow = environment
    first = repo.commit(capture(repo, store, workflow), "first")
    swapped = repo.commit(capture(repo, store, workflow, ("retrieve", "verify", "plan", "answer")), "swapped")
    report = repo.compare(first, swapped)
    assert report["outcome"] == "regressed"
    assert report["gates"]["order"] is False
    assert any(f["reason"] == "REORDERED_REQUIRED_EVENTS" for f in report["findings"])


def test_full_bundle_portable_closure_and_explicit_ref_mapping(environment, tmp_path):
    repo, store, workflow = environment
    base = repo.commit(capture(repo, store, workflow), "first")
    repo.create_branch("experiment", base)
    bad = repo.commit(capture(repo, store, workflow, ("retrieve", "answer")), "bad", branch="experiment")
    evaluation = repo.compare(base, bad)
    bundle = tmp_path / "history.dpk"
    repo.export_bundle(bundle, roots=[bad, evaluation["evaluation_id"]], refs={"refs/heads/experiment": bad})
    with ReasoningRepository(tmp_path / "imported.sqlite") as imported:
        result = imported.import_bundle(bundle)
        assert result["refs"] == {}
        assert imported.refs() == {}
        assert imported.compare(base, bad)["outcome"] == "regressed"
        imported.import_bundle(bundle, {"refs/heads/experiment": "received"})
        assert imported.resolve("received") == bad
        count = imported.status()["object_count"]
        with pytest.raises(RefConflictError):
            imported.import_bundle(bundle, {"refs/heads/experiment": "received"})
        assert imported.status()["object_count"] == count
        assert imported.fsck()["valid"]


def rewrite_bundle(source, destination, transform):
    with zipfile.ZipFile(source) as archive:
        entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    transform(entries)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


@pytest.mark.parametrize("attack", ["tamper", "missing", "traversal", "duplicate", "compressed"])
def test_bad_bundle_is_rejected_without_partial_import(environment, tmp_path, attack):
    repo, store, workflow = environment
    commit = repo.commit(capture(repo, store, workflow), "first")
    source, damaged = tmp_path / "good.dpk", tmp_path / "bad.dpk"
    repo.export_bundle(source)

    def mutate(entries):
        entry = next(n for n in entries if n.startswith("objects/"))
        if attack == "tamper":
            entries[entry] = entries[entry] + b"!"
        elif attack == "missing":
            del entries[entry]
        elif attack == "traversal":
            entries["../escaped"] = b"never extract"

    rewrite_bundle(source, damaged, mutate)
    if attack == "duplicate":
        with zipfile.ZipFile(damaged, "a") as archive:
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("manifest.json", b"{}")
    elif attack == "compressed":
        with zipfile.ZipFile(damaged, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", b"{}")
    with ReasoningRepository(tmp_path / (attack + ".sqlite")) as destination:
        with pytest.raises(ValueError):
            destination.import_bundle(damaged)
        assert destination.status()["object_count"] == 0
        assert destination.refs() == {}
    assert not (tmp_path / "escaped").exists()
    assert repo.resolve("main") == commit


def test_hash_tampering_detected_even_for_stored_objects(environment):
    repo, store, workflow = environment
    commit = repo.commit(capture(repo, store, workflow), "first")
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE objects SET data=? WHERE id=?", (b"corrupt", commit))
    assert not repo.fsck()["valid"]
    with pytest.raises(RepositoryFormatError, match="hash"):
        repo.get_object(commit)
    assert repo.compare(commit, commit)["outcome"] == "invalid"


def test_workflow_assets_are_diffed_separately_from_execution(environment):
    repo, store, workflow = environment
    first = repo.commit(capture(repo, store, workflow), "first")
    updated = repo.snapshot_workflow({"prompt.txt": b"A different prompt."}, config={"model": "updated"})
    second = repo.commit(capture(repo, store, updated), "same execution, changed workflow")
    report = repo.compare(first, second)
    assert report["outcome"] == "accepted"
    assert [(c["kind"], c["name"]) for c in report["workflow_changes"]] == [
        ("asset", "prompt.txt"), ("metadata", "config")]
    assert repo.get_object(report["evaluation_id"])["payload"]["report"]["workflow_changes"] == report["workflow_changes"]


def test_binding_after_recording_cannot_retroactively_claim_prebound(environment):
    repo, store, workflow = environment
    with traced_run(store) as run:
        record_event("verify")
        repo.bind_run(run.run_id, workflow)
        with pytest.raises(ValueError):
            store.bind_workflow(run.run_id, workflow)
    evidence = repo.seal_run(store, run.run_id)
    assert repo.get_object(evidence)["payload"]["workflow_binding"] == "post_hoc"
    commit = repo.commit(evidence, "post-hoc binding")
    assert repo.compare(commit, commit)["outcome"] == "incomplete"


def test_optional_detached_signature_survives_portable_bundle(environment, tmp_path):
    pytest.importorskip("cryptography")
    from dprovenancekit.attestation import SoftwareTraceAttestationKey

    repo, store, workflow = environment
    commit = repo.commit(capture(repo, store, workflow), "signed")
    key = SoftwareTraceAttestationKey()
    signature = repo.sign(commit, key)
    key_id = repo.get_object(signature)["payload"]["key_id"]
    assert repo.verify_signatures(commit)["integrity_valid"]
    assert not repo.verify_signatures(commit)["trusted_signer"]
    assert repo.verify_signatures(commit, [key_id])["trusted_signer"]
    bundle = tmp_path / "signed.dpk"
    repo.export_bundle(bundle)
    with ReasoningRepository(tmp_path / "signed.sqlite") as imported:
        imported.import_bundle(bundle)
        assert imported.verify_signatures(commit, [key_id])["trusted_signer"]


def test_codec_rejects_float_trailing_bytes_and_noncanonical_maps():
    with pytest.raises(RepositoryFormatError, match="floats"):
        encode_object("workflow", {"temperature": 0.1})
    raw = encode_object("blob", {"data": b"\x00\xff"})
    assert decode_object(raw) == ("blob", {"data": b"\x00\xff"})
    with pytest.raises(RepositoryFormatError, match="trailing"):
        decode_object(raw + b"\x00")
    prefix = raw[:raw.index(b"\x07")]
    malformed = prefix + b"\x07" + struct.pack(">Q", 2)
    malformed += struct.pack(">Q", 1) + b"z" + b"\x00"
    malformed += struct.pack(">Q", 1) + b"a" + b"\x00"
    with pytest.raises(RepositoryFormatError, match="sorted"):
        decode_object(malformed)


def _fixture_value(value):
    if isinstance(value, dict):
        if set(value) == {"$bytes_hex"}:
            return bytes.fromhex(value["$bytes_hex"])
        return {k: _fixture_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_fixture_value(v) for v in value]
    return value


def test_frozen_repository_codec_vectors():
    path = Path(__file__).resolve().parents[1] / "conformance" / "vectors" / "repository_v1.json"
    vectors = json.loads(path.read_text(encoding="utf-8"))
    for case in vectors["cases"]:
        payload = _fixture_value(case["payload"])
        encoded = encode_object(case["type"], payload)
        assert encoded.hex() == case["canonical_hex"], case["id"]
        assert object_id(encoded) == case["object_id"], case["id"]
        assert decode_object(encoded) == (case["type"], payload)


@pytest.mark.parametrize("name", ["unsigned", "signed"])
def test_frozen_portable_bundles(name, tmp_path):
    if name == "signed":
        pytest.importorskip("cryptography")
    path = Path(__file__).resolve().parents[1] / "conformance" / "vectors" / "repository_v1.json"
    vectors = json.loads(path.read_text(encoding="utf-8"))
    fixture = next(v for v in vectors["bundles"] if v["id"] == name)
    raw = base64.b64decode(fixture["base64"], validate=True)
    import hashlib
    assert hashlib.sha256(raw).hexdigest() == fixture["sha256"]
    bundle = tmp_path / "fixture.dpk"
    bundle.write_bytes(raw)
    with ReasoningRepository(tmp_path / "fixture.sqlite") as repo:
        repo.import_bundle(bundle)
        assert repo.compare(fixture["commit"], fixture["commit"])["outcome"] == "accepted"
        assert repo.fsck()["valid"]


def test_invalid_detached_signature_rejected_atomically(environment, tmp_path):
    pytest.importorskip("cryptography")
    from dprovenancekit.attestation import SoftwareTraceAttestationKey
    repo, store, workflow = environment
    commit = repo.commit(capture(repo, store, workflow), "signed")
    signature = repo.sign(commit, SoftwareTraceAttestationKey())
    source, bad = tmp_path / "signed.dpk", tmp_path / "bad-signature.dpk"
    repo.export_bundle(source)

    def tamper(entries):
        signature_path = "objects/" + signature + ".bin"
        kind, payload = decode_object(entries.pop(signature_path))
        payload["signature"] = b"\x00" * len(payload["signature"])
        raw = encode_object(kind, payload)
        changed_id = object_id(raw)
        entries["objects/" + changed_id + ".bin"] = raw
        manifest = json.loads(entries["manifest.json"])
        manifest["objects"] = sorted(changed_id if x == signature else x for x in manifest["objects"])
        entries["manifest.json"] = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()

    rewrite_bundle(source, bad, tamper)
    with ReasoningRepository(tmp_path / "destination.sqlite") as destination:
        with pytest.raises(RepositoryFormatError, match="signature"):
            destination.import_bundle(bad)
        assert destination.status()["object_count"] == 0


def test_raw_unreadable_payload_is_archived_without_reencoding(environment):
    repo, store, workflow = environment
    with traced_run(store) as run:
        repo.bind_run(run.run_id, workflow)
        store.bind_workflow(run.run_id, workflow)
        record_event("verify")
    snapshot = store.repository_run_snapshot(run.run_id)
    snapshot["events"][0]["payload_bytes"] = b"\xff\x00"
    snapshot["events"][0]["payload_json"] = None
    snapshot["capture_quality"]["undecoded_events"] = 1

    class Archived:
        def repository_run_snapshot(self, run_id):
            return snapshot

    evidence = repo.seal_run(Archived(), run.run_id)
    run_object = repo.get_object(evidence)["payload"]
    assert repo.get_object(run_object["events"][0]["payload"])["payload"]["data"] == b"\xff\x00"
    commit = repo.commit(evidence, "unreadable archive")
    assert repo.compare(commit, commit)["outcome"] == "incomplete"


def test_graph_damage_is_archived_but_cannot_pass_or_hide_in_fsck(environment):
    repo, store, workflow = environment
    with traced_run(store) as run:
        repo.bind_run(run.run_id, workflow)
        store.bind_workflow(run.run_id, workflow)
        record_event("retrieve")
        record_event("verify")
    snapshot = store.repository_run_snapshot(run.run_id)
    snapshot["events"][0].update(span_id="a", parent_span_id="b")
    snapshot["events"][1].update(span_id="b", parent_span_id="a")

    class Damaged:
        def repository_run_snapshot(self, run_id):
            return snapshot

    evidence = repo.seal_run(Damaged(), run.run_id)
    assert "SPAN_CYCLE" in repo.get_object(evidence)["payload"]["graph_health"]["reasons"]
    commit = repo.commit(evidence, "damaged graph")
    assert repo.compare(commit, commit)["outcome"] == "invalid"
    check = repo.fsck()
    assert check["integrity_valid"] and not check["graph_valid"] and not check["valid"]


def test_forged_accepted_evaluation_cannot_promote(environment):
    repo, store, workflow = environment
    good = repo.commit(capture(repo, store, workflow), "good")
    bad = repo.commit(capture(repo, store, workflow, ("retrieve", "answer")), "bad")
    rejected = repo.compare(good, bad)
    payload = repo.get_object(rejected["evaluation_id"])["payload"]
    payload["report"]["outcome"] = "accepted"
    payload["report"]["policy_accepted"] = True
    with repo._transaction():
        forged = repo._put("evaluation", payload)
    with pytest.raises(RepositoryError, match="passing"):
        repo.promote(bad, forged, expected_old=good)
