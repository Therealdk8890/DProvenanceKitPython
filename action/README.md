# DProvenanceKit regression gate — GitHub Action

A composite action that fails a pull request when an agent's reasoning regresses against a
golden run, and posts a sticky summary comment with the diff. It wraps the server-less
`dprovenancekit gate` CLI, so it runs fully local — no hosted backend required.

## Usage

```yaml
name: reasoning-regression
on: pull_request

permissions:
  contents: read
  pull-requests: write   # required for the PR comment

jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      # Produce a SQLite trace database with your golden + candidate runs.
      # (Record the candidate from this PR; restore/fetch the golden baseline.)
      - name: Record traces
        run: python scripts/record_traces.py   # your script; writes traces.sqlite

      - name: Regression gate
        uses: Therealdk8890/DProvenanceKitPython/action@v1
        with:
          db-path: traces.sqlite
          golden-run-id: ${{ env.GOLDEN_RUN_ID }}
          candidate-run-id: ${{ env.CANDIDATE_RUN_ID }}
          max-level: none          # strict: any divergence fails
          # allow-divergent: true  # tolerate per-step changes, gate only on severity
```

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `db-path` | — (required) | SQLite trace database holding both runs. |
| `golden-run-id` | — (required) | Run id of the golden (known-good) trace. |
| `candidate-run-id` | — (required) | Run id of the candidate trace to gate. |
| `max-level` | `none` | Worst severity that still passes: `none` \| `low` \| `medium` \| `high`. |
| `allow-divergent` | `false` | Tolerate per-step changes; gate only on severity. |
| `fail-on-regression` | `true` | Fail the job when a regression is detected. |
| `comment` | `true` | Post a sticky summary comment on the PR. |
| `anomaly-rules` | `""` | Path to a JSON rules config. When set, runs the out-of-the-box anomaly rules over the candidate run. |
| `fail-on-anomaly` | `false` | Fail the job when an anomaly rule fires. |
| `install-spec` | `dprovenancekit` | pip requirement to install the gate from (pin a version or point at a VCS URL). |
| `python-version` | `3.x` | Python to set up. |
| `github-token` | `${{ github.token }}` | Token used to post the comment. |

## Outputs

| Output | Description |
| --- | --- |
| `passed` | `true` when no regression was detected. |
| `regression-level` | Engine-assessed severity (`none` \| `low` \| `medium` \| `high`). |
| `summary` | Human-readable gate summary. |
| `report-json` | Full gate report as a JSON string. |
| `anomaly-count` | Number of anomalies the rules fired on (`0` when `anomaly-rules` is unset). |
| `anomalies-json` | Anomaly findings as a JSON string (`{}` when `anomaly-rules` is unset). |

## Anomaly rules

Set `anomaly-rules` to a JSON config to also run the out-of-the-box rule library over the
candidate run. Findings surface as inline `::warning::` annotations and a job summary; set
`fail-on-anomaly: true` to also fail the job.

```json
{
  "rules": [
    { "type": "tool_drop", "required_step": "safety_check" },
    { "type": "looping", "step": "web_search", "max_repeats": 5 }
  ]
}
```

```yaml
      - uses: Therealdk8890/DProvenanceKitPython/action@v1
        with:
          db-path: traces.sqlite
          golden-run-id: ${{ env.GOLDEN_RUN_ID }}
          candidate-run-id: ${{ env.CANDIDATE_RUN_ID }}
          anomaly-rules: .github/dprov-rules.json
          fail-on-anomaly: true
```

The same rules run anywhere without the action via `dprovenancekit anomalies --db traces.sqlite --rules rules.json [--run <id>] [--json]`.

## Notes

- **Baseline selection** (how the golden run is produced/located for a PR) is left to your
  workflow today — provide the run ids and a database that contains both. Convenience for
  selecting the golden run from `main` is a planned follow-up.
- **Fork PRs:** `GITHUB_TOKEN` is read-only on pull requests from forks, so the comment step
  is skipped with a notice rather than failing the job. The gate verdict (and job
  pass/fail) is unaffected.
- The action reads databases in the standard type-erased format the library's stores and the
  hosted backend produce.
