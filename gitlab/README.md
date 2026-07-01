# DProvenanceKit regression gate — GitLab CI

The GitLab analogue of the [GitHub Action](../action/README.md): fail a merge request when an
agent's reasoning regresses against a golden run, and post a sticky MR note with the diff. It
wraps the server-less `dprovenancekit gate` CLI, so it runs fully local — no hosted backend.

## Usage

Include the template and set the run variables:

```yaml
include:
  - remote: 'https://raw.githubusercontent.com/Therealdk8890/DProvenanceKitPython/main/gitlab/dprovenancekit.gitlab-ci.yml'

# Produce traces.sqlite with your golden + candidate runs in an earlier job, then:
dprovenancekit-gate:
  variables:
    DPROV_DB: traces.sqlite
    DPROV_GOLDEN: "$GOLDEN_RUN_ID"
    DPROV_CANDIDATE: "$CANDIDATE_RUN_ID"
    # DPROV_MAX_LEVEL: low          # tolerate up to low severity
    # DPROV_ALLOW_DIVERGENT: "true" # gate only on severity
```

## Variables

| Variable | Default | Description |
| --- | --- | --- |
| `DPROV_DB` | — (required) | SQLite trace database holding both runs. |
| `DPROV_GOLDEN` | — (required) | Golden (known-good) run id. |
| `DPROV_CANDIDATE` | — (required) | Candidate run id to gate. |
| `DPROV_GOLDEN_DB` / `DPROV_CANDIDATE_DB` | `DPROV_DB` | Separate dbs (e.g. a restored baseline vs. this MR's run). |
| `DPROV_MAX_LEVEL` | `none` | Worst severity that still passes: `none` \| `low` \| `medium` \| `high`. |
| `DPROV_ALLOW_DIVERGENT` | `false` | Tolerate per-step changes; gate only on severity. |
| `DPROV_FAIL_ON_REGRESSION` | `true` | Fail the pipeline when a regression is detected. |
| `DPROV_INSTALL_SPEC` | `dprovenancekit` | pip requirement to install the gate from. |
| `DPROV_GITLAB_TOKEN` | — | Token with `api` scope, to post the MR note. |

## Notes

- **MR note token:** `CI_JOB_TOKEN` generally cannot create MR notes, so set `DPROV_GITLAB_TOKEN`
  to a project or personal access token with `api` scope (a masked CI/CD variable). Without it,
  the gate still runs and fails the pipeline on a regression — only the note is skipped.
- **Baseline selection:** resolve the golden run id from a restored baseline with
  `dprovenancekit runs --db baseline.sqlite --context my-agent --latest --format id`, then pass
  `DPROV_GOLDEN_DB` / `DPROV_CANDIDATE_DB` (see the [action README](../action/README.md#baseline-selection)).
- The job runs only on merge-request pipelines (`$CI_PIPELINE_SOURCE == "merge_request_event"`).

# git-blob-rewrite
