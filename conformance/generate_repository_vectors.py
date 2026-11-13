"""Deliberately regenerate the independently versioned repository codec corpus."""

import hashlib
import base64
import io
import json
import sys
import zipfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dprovenancekit.repository_codec import encode_object, object_id  # noqa: E402
from dprovenancekit.repository import ReasoningRepository, BUNDLE_FORMAT, _references  # noqa: E402


def json_value(value):
    if isinstance(value, bytes):
        return {"$bytes_hex": value.hex()}
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_value(v) for v in value]
    return value


def main():
    cases = []
    objects = {}

    def add(name, kind, payload):
        raw = encode_object(kind, payload)
        oid = object_id(raw)
        cases.append({"id": name, "type": kind, "payload": json_value(payload),
                      "canonical_hex": raw.hex(), "object_id": oid})
        objects[oid] = (kind, payload, raw)
        return oid

    blob = add("json-payload", "blob", {"data": b"{}"})
    add("binary-blob", "blob", {"data": bytes(range(256))})
    add("empty-blob", "blob", {"data": b""})
    workflow = add("unicode-workflow", "workflow", {
        "assets": {"prompt.txt": blob},
        "config": {"\u00e9": "composed", "e\u0301": "decomposed", "emoji": "\U0001f512",
                   "temperature": "0.1", "enabled": True, "disabled": False,
                   "optional": None, "limits": [-(2**63), 2**63 - 1]},
        "source": {"git_revision": "fixture"}, "capture_policy": {"payloads": "explicit"},
    })
    run_id = "00000000-0000-0000-0000-000000000001"
    events = [{"id": "00000000-0000-0000-0001-%012d" % i, "run_id": run_id,
               "context_id": "fixture", "engine": "Planner", "schema_version": 1,
               "sequence": i, "span_id": None, "parent_span_id": None, "type": step,
               "priority": 2, "payload": blob, "timestamp_us": 100 + i,
               "schema_provenance": "recorded"}
              for i, step in enumerate(("retrieve", "plan", "verify", "answer"))]
    run = add("complete-run", "run", {"run_id": run_id, "context_id": "fixture",
        "workflow": workflow, "workflow_binding": "prebound", "events": events, "edges": [],
        "capture_quality": {"lifecycle": "completed", "attempted_events": 4,
            "dropped_events": 0, "attempted_edges": 0, "dropped_edges": 0,
            "undecoded_events": 0, "schema_provenance": "recorded", "sealed": True,
            "workflow_id": workflow},
        "graph_health": {"valid": True, "reasons": [], "event_count": 4, "edge_count": 0}})
    commit = add("root-commit", "commit", {"parents": [], "workflow": workflow,
        "run": run, "message": "Verified retrieval path", "author": "fixture", "created_at_us": 123})
    with tempfile.TemporaryDirectory() as temporary:
        with ReasoningRepository(Path(temporary) / "fixtures.sqlite") as repository:
            with repository._transaction():
                for kind, payload, _ in objects.values():
                    repository._put(kind, payload)
            report = repository.compare(commit, commit)
            evaluation_payload = repository.get_object(report["evaluation_id"])["payload"]
    evaluation = add("detached-evaluation", "evaluation", evaluation_payload)
    from dprovenancekit.attestation import SoftwareTraceAttestationKey
    key = SoftwareTraceAttestationKey.from_raw_representation((1).to_bytes(32, "big"))
    signature_payload = {"subject": commit, "algorithm": "P256-SHA256",
        "public_key": key.public_key_x963_representation,
        "key_id": hashlib.sha256(key.public_key_x963_representation).hexdigest(),
        "signature": key.signature_der(b"DPK-REPOSITORY-SIGNATURE-V1\x00" + bytes.fromhex(commit))}
    # Keep the frozen valid ECDSA signature stable on a no-change regeneration.
    path = ROOT / "conformance" / "vectors" / "repository_v1.json"
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        for case in previous["cases"]:
            if case["id"] == "detached-signature" and case["payload"]["subject"] == commit:
                signature_payload["signature"] = bytes.fromhex(case["payload"]["signature"]["$bytes_hex"])
    signature = add("detached-signature", "signature", signature_payload)
    closure = set()
    pending = [commit, evaluation]
    while pending:
        oid = pending.pop()
        if oid in closure:
            continue
        closure.add(oid)
        pending.extend(_references(objects[oid][0], objects[oid][1]))
    bundles = []
    for name, included in (("unsigned", closure), ("signed", closure | {signature})):
        manifest = {"format": BUNDLE_FORMAT, "version": 1, "roots": sorted([commit, evaluation]),
            "objects": sorted(included), "refs": {"refs/heads/main": commit}}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
            ReasoningRepository._zip_write(archive, "manifest.json", json.dumps(
                manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            for oid in sorted(included):
                ReasoningRepository._zip_write(archive, "objects/" + oid + ".bin", objects[oid][2])
        raw = output.getvalue()
        bundles.append({"id": name, "commit": commit, "object_ids": sorted(included),
            "sha256": hashlib.sha256(raw).hexdigest(), "base64": base64.b64encode(raw).decode("ascii")})
        fixtures = ROOT / "conformance" / "fixtures"
        fixtures.mkdir(exist_ok=True)
        (fixtures / ("repository_" + name + "_v1.dpk")).write_bytes(raw)
    result = {"spec_version": "DPK-REPOSITORY-V1", "description":
              "Byte-exact object encoding; Unicode keys are compared by UTF-8 bytes without normalization.",
              "cases": cases, "bundles": bundles}
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
