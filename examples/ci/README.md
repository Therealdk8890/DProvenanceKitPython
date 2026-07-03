# CI regression-gating examples

Two ways to gate a pull request when an agent's reasoning regresses against a
known-good ("golden") run. Pick the one that matches how you store baselines.

| File | Baseline source | Needs |
| --- | --- | --- |
| [`github-workflow.yml`](github-workflow.yml) | **Cloud sync** — pulls the golden by run id from the DProvenanceKit SaaS | `DPROV_API_KEY`, a `golden-run-id` |
| [`record-baseline.yml`](record-baseline.yml) + [`agent-regression-gate.yml`](agent-regression-gate.yml) | **Artifact** — record the golden on `main`, restore it in the PR | Nothing external; uses GitHub artifacts |
| [`gitlab-workflow.yml`](gitlab-workflow.yml) | GitLab CI parity | — |

The rest of this guide covers the **artifact-baseline** pattern, which is the
zero-dependency default.

## How the two workflows fit together

1. **`record-baseline.yml`** runs on every push to `main`. It records one
   known-good agent run into `baseline.sqlite` (with `context_id="production-agent"`)
   and uploads it as the `agent-baseline-db` artifact.
2. **`agent-regression-gate.yml`** runs on every pull request. It records the
   candidate run into `candidate.sqlite`, restores the latest successful `main`
   baseline via the GitHub CLI, then runs the action to diff them, apply the
   anomaly rules, and post a sticky PR comment.

Both jobs run your own recording script (shown as `record_agent.py`). The
`context_id` is set **in that code** at record time — there is no CLI flag or
env var for it — so both jobs must use the **same** context id.

## The `db-path` routing gotcha (read this)

The action runs two checks with different database wiring, verified against the
action source:

- The **gate** step reads `golden-db` and `candidate-db` separately.
- The **anomaly** step reads **only `db-path`** and resolves `candidate-context`
  against it — it never looks at `candidate-db`.

So the candidate run must live in `db-path`, or the anomaly rules silently find
no candidate:

```yaml
db-path: candidate.sqlite    # the candidate db — anomaly rules read this
golden-db: baseline.sqlite   # the restored baseline — the gate reads golden here
# candidate-db: (unset)      # defaults to db-path = candidate.sqlite
golden-context: production-agent
candidate-context: production-agent
```

Do **not** set `dprov-api-key` in this pattern. If the key is set, the action
switches to the cloud-sync path, which requires `golden-run-id` and hard-errors
when you rely on `golden-context`. Local baseline = no API key.

## Tuning the gate: permissive → strict

Start lenient so the gate earns trust, then tighten:

- **`max-level`** (`none` → `low` → `medium` → `high`) is the worst severity that
  still passes. Begin at `low`; move toward `none` as the baseline stabilizes.
- **`allow-divergent: "true"`** gates on severity only and ignores per-step churn
  — useful for a nondeterministic agent. Flip to `"false"` to also fail on any
  structural divergence once the agent's step shape is stable.

These inputs are strings — quote `"true"` / `"false"`.

## Anomaly rules complement the severity gate

The severity gate is **relative** (golden vs candidate diff). Anomaly rules in
[`dprov-rules.json`](dprov-rules.json) are **absolute**, single-run invariants
that catch failures a diff can miss — a dropped safety step, a search loop — even
when the two runs drift together. Set `fail-on-anomaly: "true"` to block on them.

Copy this file to **`.github/dprov-rules.json`** — the path the PR workflow's
`anomaly-rules` input points at — and put the two `.yml` workflows in
`.github/workflows/`.

The `required_step` / `step` / `required_followup_step` values in the ruleset are
your recorded step **`type_identifier`s** — change `verify_claims`, `web_search`,
and `summarize` to match your agent's actual steps. Supported rule types:
`tool_drop`, `looping`, `unregistered_tool`, `unused_tool_result`.

## Notes

- **First PR / no baseline yet.** Until `record-baseline.yml` has succeeded on
  `main` once, the restore step warns and the gate step is skipped, so the PR is
  not blocked. Merge it (or run the baseline workflow manually) to seed the first
  baseline; subsequent PRs are gated.
- **Fork PRs.** GitHub downgrades `GITHUB_TOKEN` to read-only on fork PRs, so the
  sticky comment may be skipped (the action handles this gracefully). The cross-run
  artifact restore is a read and still works.
- **Supply-chain hygiene.** These examples pin GitHub-official actions to major
  tags (`@v4` / `@v5`) for readability. For stricter provenance, pin them to full
  commit SHAs. The DProvenanceKit action is pinned to the release tag `@v0.5.0`.
