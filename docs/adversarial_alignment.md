# Adversarial Alignment Suite

## Purpose

Measure how often the **production** trace matcher (greedy, highest-score-first
bipartite assignment in `DefaultTraceMatcher`) disagrees with a **mathematically
optimal** maximum-weight assignment under the **same** scoring function,
thresholds, and type/priority filtering — and whether disagreement can flip the
regression verdict (especially `HIGH` for critical remove / reorder / changed).

Production engines are **not** rewritten. Optimal assignment lives only in tests.

## Production matcher (both languages)

| Surface | Path | Algorithm |
|---------|------|-----------|
| Python | `dprovenancekit/alignment_matcher.py` | Score all pairs ≥ ambiguity threshold; sort by score desc, base idx, comp idx; greedy claim |
| Swift | `Sources/DProvenanceKit/TraceMatching/DefaultTraceMatcher.swift` | Same greedy policy (with concurrent scan optimizations that preserve exactness) |

Scoring comes from `AlignmentConfiguration.score_match` / Swift `combinedScore`
(type, payload/evaluator, structural, temporal weights from the active profile).

Default bind floor: evaluator `ambiguity_threshold` (typically `0.4`).
Equivalence / “changed beyond” uses `profile.semantic_threshold`
(`0.99` strict_audit, `0.75` developer_debug).

## Optimal reference

Pure-Python Hungarian / Munkres maximizing Σ scores over eligible pairs
(`tests/adversarial_alignment/optimal_assignment.py`). No scipy dependency.

Verdict under optimal pairing reuses `DefaultAlignmentInterpreter` plus a
test-local copy of the engine’s regression-risk derivation (removed / reordered /
changed criticals) — still without patching production sources.

## Pathological generators (v2)

Covered in `tests/adversarial_alignment/generators.py` (~58 cases):

| Category | Coverage |
|----------|----------|
| `duplicates` | duplicate types, nested/interleaved same-type blocks |
| `near_identical` | trailing whitespace/tab, double space, prefix drift + decoy exact |
| `insert_delete` | decoys, deletes, bulk noise inserts, every-other delete |
| `reorder` | triple swap, full reverse, adjacent swap cascade |
| `ties` | 1×8 … 8×12 equal-score grids, ties with exact anchors |
| `collisions` | one-to-many, rotate-3, many-to-one, chain-5, graded greedy traps (2×2 / 3×3 / 4×4 / steal) |
| `threshold` | below/at/above semantic threshold, near-miss extras, mixed pair |
| `priority_mix` | critical↔structural shuffle, critical reorder with noise, structural lookalike, all-structural |
| `long` | legacy ~30 + **50 / 100 / 150 / 200** repeated patterns |
| `semantic_hook` | `SemanticLabel_v1` disagree with payload equality (2- and 3-cluster) |
| `fuzz` | seeded deterministic families (`seeds=1,2,3,7,11,42,99,123`), n∈[50,200] |

## Metrics (local run against main package sources)

Command: `python -m pytest tests/test_adversarial_alignment.py -v`

| Metric | v1 (~15 cases) | **v2 (58 cases)** |
|--------|----------------|-------------------|
| **disagreement_rate** | 0.067 | **0.207** |
| **verdict_flip_rate** | 0.000 | **0.000** |
| **high_none_flip_rate** | 0.000 | **0.000** |

Pairing disagreements observed on graded traps, some long (150/200) traces, and
most fuzz seeds — informative, not failures. No ExactEquality `HIGH`↔`none` flips.

Emitted in pytest output and `adversarial_alignment_report.json`:

- **disagreement_rate** — fraction of cases where pair sets differ
- **verdict_flip_rate** — risk level differs (any levels)
- **high_none_flip_rate** — `HIGH` ↔ `none` specifically
- **categorized_failures** — by generator category

### Pass / fail policy

- Pairing disagreements alone: **PASS** (metrics asserted under budget).
- Optimal score must be ≥ production score (sanity).
- Unexpected `HIGH`↔`none` flips on **ExactEquality_v1** cases: **FAIL**.
- Graded / semantic-hook cases are separate evaluators and do not trip the
  ExactEquality hard invariant.
- Suite size must be ≥ 40 cases (`MIN_SUITE_CASES`).

## Swift port

XCTest port lives in `Therealdk8890/DProvenanceKit` under
`Tests/DProvenanceKitTests/AdversarialAlignment*` (branch
`test/adversarial-alignment-xctest`). Generators mirror this catalog for
cross-language disagreement-rate comparison. Production `DefaultTraceMatcher`
remains the default; optimal assignment is test-only.

## Running

```bash
python -m pytest tests/test_adversarial_alignment.py -v
```
