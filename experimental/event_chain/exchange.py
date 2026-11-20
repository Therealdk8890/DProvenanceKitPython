"""Review-only fixture exchange; not a production wire parser or trust store."""
import base64
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .auth import Edge, SignedClose, Trace, sign_close, verify_authentication
from .core import Chain, ChainError, Entry, Event, RunClose, build

# PUBLIC TEST SCALARS ONLY. Never use these keys for actual evidence.
PYTHON_SCALAR = 1
SWIFT_SCALAR = 2
CASES = ("empty", "sequence_gap_and_unicode")


class TestKey:
    __test__ = False

    def __init__(self, scalar: int) -> None:
        self._key = ec.derive_private_key(scalar, ec.SECP256R1())

    @property
    def public_key_x963_representation(self) -> bytes:
        return self._key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )

    def signature_der(self, data: bytes) -> bytes:
        return self._key.sign(data, ec.ECDSA(hashes.SHA256()))


def pin(scalar: int) -> bytes:
    return sha256(TestKey(scalar).public_key_x963_representation).digest()


def fixture_case(path: Path, name: str) -> Tuple[Chain, Trace]:
    fixtures = json.loads(path.read_text(encoding="utf-8"))
    case = next(item for item in fixtures["positiveCases"] if item["name"] == name)
    events = tuple(Event(**item["event"]) for item in case["entries"])
    run = case["runID"]
    edges = (Edge(events[1].eventID, events[2].eventID, "informed"),
             Edge(events[0].eventID, events[1].eventID, "derivedFrom")) if len(events) >= 3 else ()
    return build(run, events), Trace(run, events[0].contextID if events else "empty", events, edges)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def raw(value: str) -> bytes:
    result = base64.b64decode(value, validate=True)
    if b64(result) != value:
        raise ValueError("noncanonical base64")
    return result


def emit(chain: Chain, trace: Trace, signed: SignedClose, output: Path) -> None:
    commitment = asdict(signed.close)
    for field in ("finalChainHead", "traceDigest", "keyID"):
        commitment[field] = b64(commitment[field])
    document = {
        "format": "DPK-CANDIDATE-EXCHANGE-TEST-V1",
        "chain": {
            "runID": chain.runID, "version": chain.version, "head": b64(chain.head),
            "entries": [{"chainIndex": e.chainIndex, "event": asdict(e.event),
                         "previousHash": b64(e.previousHash), "entryHash": b64(e.entryHash)}
                        for e in chain.entries],
        },
        "trace": asdict(trace),
        "signed": {"close": commitment, "publicKeyX963": b64(signed.publicKeyX963),
                   "signatureDER": b64(signed.signatureDER)},
    }
    output.write_text(json.dumps(document, ensure_ascii=True, sort_keys=True), encoding="utf-8")


def event_from_wire(value: dict[str, Any]) -> Event:
    fields = dict(value)
    fields.setdefault("spanID", None)
    fields.setdefault("parentSpanID", None)
    return Event(**fields)


def read(path: Path) -> Tuple[Chain, Trace, SignedClose]:
    if path.stat().st_size > 4_194_304:
        raise ValueError("test artifact too large")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document["format"] != "DPK-CANDIDATE-EXCHANGE-TEST-V1":
        raise ValueError("wrong test envelope")
    chain = document["chain"]
    trace = document["trace"]
    signed = document["signed"]
    close = dict(signed["close"])
    close.setdefault("finalChainIndex", None)
    for field in ("finalChainHead", "traceDigest", "keyID"):
        close[field] = raw(close[field])
    return (
        Chain(chain["runID"], raw(chain["head"]),
              tuple(Entry(e["chainIndex"], event_from_wire(e["event"]),
                          raw(e["previousHash"]), raw(e["entryHash"])) for e in chain["entries"]),
              chain["version"]),
        Trace(trace["runID"], trace["contextID"],
              tuple(event_from_wire(e) for e in trace["events"]),
              tuple(Edge(**e) for e in trace["edges"])),
        SignedClose(RunClose(**close), raw(signed["publicKeyX963"]), raw(signed["signatureDER"])),
    )


def reject(code: str, chain: Chain, trace: Trace, signed: SignedClose,
           run: str, trusted: bytes, *, require_evidence: bool = False) -> None:
    try:
        verify_authentication(chain, trace, signed, run, trusted_key_id=trusted,
                              require_evidence=require_evidence)
    except ChainError as exc:
        if str(exc) != code:
            raise AssertionError(f"expected {code}, got {exc}") from exc
    else:
        raise AssertionError(f"accepted invalid {code}")


def verify_received(path: Path, run: str, trusted: bytes, other: bytes) -> None:
    chain, trace, signed = read(path)
    verify_authentication(chain, trace, signed, run, trusted_key_id=trusted)
    reject("UNTRUSTED_KEY", chain, trace, signed, run, other)
    reject("INVALID_SIGNATURE", chain, trace, replace(signed, signatureDER=b"bad"), run, trusted)
    changed = replace(signed, close=replace(signed.close, issuedAtUnixMicroseconds=124))
    reject("INVALID_SIGNATURE", chain, trace, changed, run, trusted)
    if chain.entries:
        reject("TRACE_MISMATCH", chain, replace(trace, edges=()), signed, run, trusted)
        truncated = build(run, trace.events[:-1])
        reject("CLOSURE_MISMATCH", truncated, trace, signed, run, trusted)
        changed_events = (replace(trace.events[0], payloadJSON="{}"), *trace.events[1:])
        replacement = build(run, changed_events)
        reject("CLOSURE_MISMATCH", replacement, trace, signed, run, trusted)
    else:
        reject("EMPTY_CHAIN", chain, trace, signed, run, trusted, require_evidence=True)


def run_exchange(swift: Path, fixtures: Path) -> None:
    # Pins are computed from independent, known test identities before reading any artifact.
    python_pin, swift_pin = pin(PYTHON_SCALAR), pin(SWIFT_SCALAR)
    with tempfile.TemporaryDirectory(prefix="dpk-exchange-") as temp:
        for name in CASES:
            chain, trace = fixture_case(fixtures, name)
            run = chain.runID
            python_file = Path(temp) / f"python-{name}.json"
            swift_file = Path(temp) / f"swift-{name}.json"
            emit(chain, trace, sign_close(chain, trace, run, 123, TestKey(PYTHON_SCALAR)), python_file)
            subprocess.run([str(swift), "exchange-verify", str(python_file),
                            run, python_pin.hex(), swift_pin.hex()], check=True)
            subprocess.run([str(swift), "exchange-sign", str(fixtures), name, str(swift_file)], check=True)
            verify_received(swift_file, run, swift_pin, python_pin)
            print(f"PASS: Swift -> Python {name}", flush=True)
    print("PASS: reciprocal Python/Swift signed artifacts, both cases", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m experimental.event_chain.exchange SWIFT_BINARY FIXTURES")
    run_exchange(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
