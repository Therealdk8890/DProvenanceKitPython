# Critical-step order intent v1 — design proposal (not implemented)

**Status: proposed, review required.** Addresses the specification gate of
[issue #110](https://github.com/Therealdk8890/DProvenanceKitPython/issues/110).
No production matcher or RegressionGate behavior changes in this proposal.

## Problem and evidence

The existing gate can classify an inversion of *matched CRITICAL steps* as HIGH
even when their ordering is not causally required. In the deterministic
10-case [synthetic policy benchmark](RELIABILITY_GATE_BENCHMARK.md), strict
gate policy has 3/5 labeled benign false alarms; the severity-only policy has
1/5. The remaining case is an explicitly independent critical-step reorder.
Both policies detect 5/5 labeled regressions. These are **synthetic case
counts**, not deployment false-positive or false-negative rates.

A global severity downgrade is not a valid fix: it would also allow required
dependency inversions.

## Opt-in policy (separate from frozen Trace Specification v1)

An external, versioned policy owned by the application declares required
partial ordering. It must be provided from trusted configuration, never
inferred from untrusted candidate traces. Proposed JSON shape:

```json
{
  "schema": "dpk.critical-order-intent/v1",
  "workflow": "checkout-v1",
  "steps": [
    {"key": "format_check", "type": "format-check", "priority": "CRITICAL"},
    {"key": "verify", "type": "verify", "priority": "CRITICAL"},
    {"key": "publish", "type": "publish", "priority": "CRITICAL"}
  ],
  "required_before": [
    {"before": "verify", "after": "publish"}
  ],
  "commutative_groups": [["format_check", "verify"]]
}
```

This is a **schema proposal**, not a CLI flag or accepted wire contract.
The default/no-policy behavior remains unchanged. A commutative declaration
permits an order swap **only when no required edge (direct or transitively
implied) orders those steps**. A declared dependency always wins. No
commutative group can authorize skipped steps, changed critical payloads,
unexpected capabilities, or mismatched execution identity.

### Stable step binding

- `key` is an application-defined stable logical step identifier, not a
  per-run event UUID or a type string assumed to be unique.
- The integration must define how baseline and candidate recorded events
  bind to the key under the same instrumentation profile.
- If a key matches zero or more than one eligible event in either trace, the
  order-intent override **does not apply**; ambiguity must fail closed or fall
  back to the existing strict gate, with an explicit reason code. The
  implementation must decide which of those two behavior classes is used
  before accepting this schema.
- For repeated step types or loops, unique scoped keys (such as a stable
  path plus occurrence discriminator) are mandatory. Never guess a match.

### Configuration validation

Reject unknown schema versions and fields, duplicate step keys, malformed
types/priorities, unknown edge endpoints, self-edges, cyclic required-edge
graphs, duplicate or conflicting constraints, invalid group overlaps, and
commutativity that conflicts with transitive required edges. Normalize
canonical policy bytes and bind a SHA-256 digest to the gate configuration
and emitted audit verdict. The expected digest must come from a trusted
repository or CI configuration, not the candidate trace.

Add explicit size/complexity limits to policy parsing before shipping. Avoid
quadratic explosions in dense dependency graphs or long traces. Policy parsing
and application must be deterministic across Python/Swift.

## Verdict semantics

1. Run the existing alignment engine and other structural/semantic checks
   without altering default scoring or match selection.
2. Evaluate the original critical-reorder signal.
3. **Only if** a trusted, validated opt-in policy resolves the exact logical
   steps uniquely, classify the reordered pair by its required dependency
   relation. Inverted required edges remain HIGH; explicitly independent,
   allowed reorderings may remove the order-only HIGH flag.
4. Preserve HIGH from missing steps, changed critical payloads, and any other
   independent failure. An order exception never clears another reason.
5. Emit machine-readable reason codes such as
   `required_order_inverted`, `allowed_independent_reorder`,
   `order_policy_invalid`, and `step_binding_ambiguous`, plus the policy
   digest and exact implicated keys. The final reason-code registry is subject
   to cross-language specification review.

## Threat model

An adversary may edit candidate traces, insert duplicate events, reorder
critical checks, tamper with policy files, forge a policy digest, or exploit
different Python/Swift parser behaviors. The gate cannot prove hidden model
reasoning, event completeness, or that a recorded check really ran; existing
attestation and external trust-anchor boundaries still apply.

**Required non-bypass rule:** missing, untrusted or malformed policy data
never weakens existing safety gates. No automatic learning or silent
commutativity inference from one candidate execution is allowed.

## Conformance matrix before implementation

| Case | Expected policy behavior |
| --- | --- |
| Independent `format_check` and `verify` swapped | Allowed only under valid explicit opt-in |
| `verify` and `publish` swapped | HIGH (required dependency inversion) |
| Identical traces, no policy | Existing verdict unchanged |
| Missing `verify` step | HIGH (absence cannot be waived by commutativity) |
| Modified CRITICAL payload | Existing HIGH remains |
| Duplicate logical step matches | No order waiver; auditable ambiguity |
| Unknown key, duplicate policy key, malformed graph | Reject policy / fail closed |
| Cyclic required dependencies | Reject policy |
| Disagreeing commutative group and required edge | Reject policy |
| Wrong digest, untrusted policy input | No order waiver |
| Nested, repeated, same-type steps | Require stable disambiguation |
| Swift/Python fixture parity | Same accept/reject and reason codes |

Maintain the existing synthetic benchmark and introduce versioned shared
positive/negative vectors before implementing changes in either language.
Require green legacy gates, Swift XCTest and Python pytest conformance, and
an independent review of the fail-closed policy trust boundary.

## Open decisions requiring review

1. Definition and source of stable logical step identity across instrumented
   integrations and repeated loops.
2. Whether ambiguous/unbound opt-in policy should be an explicit policy
   error or a fallback to strict order verdict. Neither may waive HIGH.
3. Canonical policy serialization/digest scheme and protected trust
   configuration for local and CI workflows.
4. Exact precedence between explicit commutative groups and required graph
   edges, with shared conformance fixtures.

No field reliability claim or default behavior change is authorized until
these questions are resolved and measured in real independently labeled traces.
