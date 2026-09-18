# End-to-end decision-path demo

One-shot walkthrough of the DProvenanceKit product loop with **real CLI output**:

```
Instrument → Record → Baseline → Compare → Gate → Attest → Verify
```

The demo records a clean baseline path, pins it with `dpk record`, then records a
candidate that **drops the CRITICAL `claimVerified` step**. `dpk gate` exits non-zero
with severity **HIGH**. Separately, the baseline run is exported as an attestable trace,
signed with `dpk attest sign`, and verified with `dpk attest verify`.

**Honest scope:** tamper-evident record of an *instrumented* decision path, a CI gate on
path regression, and a software `DPK-BINARY-V1` attestation of that record. This does
**not** prove that the decision was sound, compliant, or correct — only that the recorded
path is comparable and (when attested) integrity-checkable.

Swift counterpart (CryptoKit / proof packs / Secure Enclave):  
[Therealdk8890/DProvenanceKit](https://github.com/Therealdk8890/DProvenanceKit) — see
[`docs/ATTESTATION.md`](https://github.com/Therealdk8890/DProvenanceKit/blob/main/docs/ATTESTATION.md).

## Fixtures

| File | Role |
|------|------|
| `fixtures/baseline_path.json` | Clean path: retrieve → **verify** → decide |
| `fixtures/candidate_regressed.json` | Same path with CRITICAL verify removed |

## Run (one shot)

From the repository root:

```bash
pip install -e ".[crypto]"
python -m examples.e2e_decision_path
```

Keep artifacts on disk:

```bash
python -m examples.e2e_decision_path --output-dir /tmp/dpk-e2e --keep
```

Equivalent manual CLI after the demo has written `traces.sqlite` / baseline JSON
(under `--output-dir`):

```bash
dpk record --db traces.sqlite --baseline baseline.sqlite --run <baseline-run-id>
dpk gate --db traces.sqlite --golden <baseline-run-id> --candidate <candidate-run-id>
dpk attest sign --in baseline_attestable.json --out baseline_attestation.json
dpk attest verify --in baseline_attestation.json
```

## Expected outcome

- Gate: non-zero exit, `regression_level=high`, demo prints `GATE_RESULT=FAIL` / `GATE_LEVEL=high`
- Attest+verify on baseline: exit 0, demo prints `ATTEST_VERIFY=valid`
- Overall demo exit 0 when that story holds (`DEMO_OK`)

## What this is not

- Not ClaimProofKit / commercial packaging
- Not a proof of sound reasoning
- Not Secure Enclave or proof-pack attestation (Swift-only)
