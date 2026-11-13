# Local reasoning history (opt-in v1 candidate)

DPK can preserve a workflow snapshot and its recorded execution as an immutable
commit, branch experiments, compare decision paths, and export portable evidence.
This is source-checkout candidate functionality, not a claim about the published
0.9.2 wheel. Existing trace commands and formats remain available.

## Run the complete offline example

From this checkout, after `python -m pip install -e .`:

```console
dpk repo demo --output-dir history-demo
dpk ui --repo history-demo/repository.sqlite
```

Use an empty output directory. The example records baseline A, an experiment B
that skips verification while returning the same answer, and a fresh matching
execution C. B fails; C passes with its own execution identity. It exports a
full bundle, imports into a second repository and reproduces the same evaluation
ID and verdict. No model, API key, Git executable, or network is used.

## Bind the workflow before recording

```python
from pathlib import Path
from dprovenancekit import ReasoningRepository
from dprovenancekit.instrument import record_event, traced_run

with ReasoningRepository() as repo:
    workflow = repo.snapshot_workflow(
        {"prompt.txt": Path("prompt.txt").read_bytes()},
        config={"model": "your-model", "temperature": "0"},
        source={"git_commit": "the-source-revision-you-selected"},
    )
    with traced_run(repository=repo, workflow_snapshot=workflow) as run:
        record_event("retrieve", logical_step_id="retrieve")
        record_event("verify", {"passed": True}, logical_step_id="verify")
        record_event("answer", {"text": "the recorded result"}, logical_step_id="answer")
    commit = repo.commit(run.repository_evidence_id, "Preserve verified workflow")
    result = repo.compare(commit, commit)
    repo.promote(commit, result["evaluation_id"])  # Initial, complete self-comparison.
    print(commit)
```

Use this around the actual workflow and instrument real tool/model activity.
`record_event("verify")` is a caller assertion, not independent proof that a
verification tool ran or that its conclusion is correct.

Only selected asset bytes are captured. Configuration/source/capture-policy
metadata are explicit caller declarations. DPK does not collect your environment,
credentials, source tree or Git state automatically. Supply an allowlisted source
snapshot and dirty-file content if those are needed to reconstruct the workflow.
Redact sensitive data **before** recording and snapshotting; capture-policy
metadata does not apply an automatic redactor. Validate your redaction rules in
the capture adapter. Later redaction creates new objects; v1 does not delete
historical copies. Hashes alone do not hide low-entropy secrets.

Canonical metadata supports null, booleans, signed 64-bit integers, strings,
bytes, lists, and string-keyed maps. Use decimal strings for model settings such
as temperature. Arbitrary payload bytes remain exact; the comparison normalizer
works on strict JSON values without rewriting archival bytes.

## Inspect, branch, compare, and promote

```console
dpk repo init
dpk repo snapshot --asset prompt=prompt.txt
dpk repo commit --run RUN_UUID -m "Preserve the recorded execution"
dpk repo branch experiment --from main
dpk repo switch experiment
dpk repo log --limit 20
dpk repo show HEAD
dpk repo diff --baseline main --candidate experiment --json
dpk repo gate --baseline refs/baselines/default --candidate experiment --json
dpk repo fsck
```

The default repository is `.dprovenance/repository.sqlite`, separate from
`.dprovenance/traces.sqlite`. Select another with `--repo PATH` before or after
the subcommand. A `snapshot` ID is supplied to `traced_run`; it does not itself
record an execution. Each commit binds exactly one workflow and one run.
Branch switching selects reasoning history and never edits source files.

The built-in `repository-strict-v1` policy uses the existing strict alignment
engine, exact normalized payload equality, and independent hierarchy/order/edge
checks. Experimental Structural Alignment v1 is not silently selected. Workflow
asset/configuration changes appear separately from execution findings.

Outcomes are `accepted`, `regressed`, `incomplete`, or `invalid`. Gate exit codes:
0 accepted, 1 regression, 2 incomplete/invalid/error. `diff` exits 0 for a valid
regression comparison, while still returning 2 for incomplete/invalid evidence.
The event comparison budget defaults to 1,000, configurable up to 10,000 using
an explicit policy JSON. Exceeding the budget never produces acceptance.

Promotion needs the exact evaluation ID printed in the gate report:

```console
dpk repo baseline CANDIDATE_ID --evaluation EVALUATION_ID --expected-old BASELINE_ID
```

Promotion recomputes the result and conditionally updates the baseline from the
exact previous commit. For an initial baseline omit `--expected-old` and supply
an accepted self-comparison. Existing refs cannot be silently replaced through
initial creation or bundle import.

## Portable evidence and optional signing

```console
dpk repo bundle export history.dpkbundle
dpk repo --repo imported.sqlite init
dpk repo --repo imported.sqlite bundle import history.dpkbundle --map-ref refs/heads/main=refs/heads/imported
dpk repo --repo imported.sqlite fsck
```

Export includes the full reachable closure and attached signatures. To include
detached evaluation objects, pass their IDs as additional repeated `--root`
arguments. Import verifies inventory, hashes, schemas and typed references before
one atomic transaction. It performs no extraction, package installs, hooks, model
execution, or network calls. Refs only change through explicit create-only maps;
baseline promotion cannot be performed by importing a ref.

Install `python -m pip install -e ".[crypto]"` for P-256 signing/verification:

```console
dpk repo sign COMMIT_ID --key private-p256.pem
dpk repo verify COMMIT_ID --trusted-key EXPECTED_KEY_ID
```

Private keys are never placed in repository objects or bundles. An embedded key
can establish signature integrity; signer trust requires an independently
provisioned key ID. Unsigned objects can still have verified byte integrity.

## Boundaries and compatibility

- Finalization closes admission, flushes recording, and seals evidence from one
  read snapshot. Late event/edge writes are rejected.
- Capture quality records per-run attempts/loss, schema provenance and lifecycle.
  Empty, failed, interrupted, lossy, unknown, or post-hoc-bound captures cannot
  pass strict repository gating. Legacy bytes remain archivable with unknown
  quality instead of receiving invented completeness.
- The read-only viewer uses paging and lazy object loading; it has no login or
  authorization. Keep the default loopback bind.
- Hash integrity, signer trust, capture quality, and policy acceptance are
  separate. Neither a signature nor matching trace proves a claim is true.
- SQLite/API immutability and the reflog do not prevent a local administrator
  from replacing the entire database with an older valid snapshot.
- There is no trace-splicing merge, live-model replay, hosted synchronization,
  garbage collection, or history rewriting in this v1 candidate.
- Python remains dependency-free in its core. Swift has an independent reader
  and verifier for the same object/bundle bytes; Apple CI must validate it before
  a coordinated release. The commercial server/native application stages remain
  separate from these SDK changes.

See [the repository protocol](../conformance/REPOSITORY_SPEC_v1.md) for exact
encoding and trust contracts. No legacy trace fingerprint or attestation ID was
redefined for this feature.

For informative capture/sealing/comparison timings, run
`python scripts/benchmark_repository.py` in the installed checkout. Exact ordered
paths use a linear comparison fast path; changed paths retain strict alignment.
Timing results depend on the machine and are not correctness thresholds.
