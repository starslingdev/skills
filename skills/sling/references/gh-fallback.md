# `gh` fallback: everything `sling` cannot do

`sling` is **read-only** in this release. Anything that changes CI state
goes to the GitHub CLI. Syntax below was checked against **`gh` v2.93.0**.

Before the first `gh` call, confirm `gh auth status` succeeds. `gh` and
`sling` authenticate separately: a working `sling` says nothing about
whether `gh` is signed in. If `gh` is unauthenticated, say so — never
report a state change as done when the command did not run.

Add `-R <owner>/<repo>` when the working directory is not the target repo.

## State changes

| Ask | Command |
|---|---|
| Re-run a whole run | `gh run rerun <run-id>` |
| Re-run only the failed jobs (and their dependencies) | `gh run rerun <run-id> --failed` |
| Re-run one job | `gh run rerun <run-id> --job <job-id>` |
| Re-run with debug logging on | `gh run rerun <run-id> --debug` |
| Cancel an in-progress run | `gh run cancel <run-id>` |
| Delete a run | `gh run delete <run-id>` |
| Trigger a `workflow_dispatch` workflow | `gh workflow run <workflow> [--ref <branch>] [-f key=value]` |
| Enable / disable a workflow | `gh workflow enable <workflow>` / `gh workflow disable <workflow>` |
| Download a run's artifacts | `gh run download <run-id> [-n <name>] [-D <dir>]` |
| Set or delete a secret | `gh secret set <NAME>` / `gh secret delete <NAME>` |
| Set or delete a variable | `gh variable set <NAME>` / `gh variable delete <NAME>` |

**Announce every one of these before running it.** They change something in
the user's repo, and a re-run costs runner-minutes. Diagnosis first, action
second, and say which you are doing.

## Reads `gh` has and `sling` does not

| Ask | Command |
|---|---|
| The checks on a pull request | `gh pr checks <pr>` |
| Watch a run to completion in the terminal | `gh run watch <run-id>` |
| List workflows defined in the repo | `gh workflow list` |
| A workflow's summary | `gh workflow view <workflow>` |

`gh pr checks` is the bridge from a PR to a run: take the failing run or
job id out of its output and go back to `sling why` / `sling time` for the
diagnosis.

## Approving or rejecting a pending deployment

`gh` v2.93.0 has **no first-class subcommand** for this — the surface is
`gh run` (cancel, delete, download, list, rerun, view, watch) and `gh
workflow` (list, run, view, enable, disable), neither of which covers
deployment gates. Use the REST API through `gh api`.

Read which environments are waiting:

```bash
gh api repos/<owner>/<repo>/actions/runs/<run-id>/pending_deployments
```

Approve or reject (`state` is `approved` or `rejected`):

```bash
gh api --method POST \
  repos/<owner>/<repo>/actions/runs/<run-id>/pending_deployments \
  -f state=approved -f comment="<why>" -F 'environment_ids[]=<env-id>'
```

The `environment_ids` come from the GET above. This is an approval on
someone's deployment gate — never send it without the user explicitly
asking for that specific environment.

## Degraded read path when `sling` is unavailable

If `sling` cannot be installed, authenticated, or reached, these are the
closest `gh` equivalents. They are strictly weaker — no classification, no
phase breakdown, no cost attribution — so **name what is missing** when you
fall back:

| Instead of | Use | What is lost |
|---|---|---|
| `sling why <job>` | `gh run view <run-id> --log-failed` | The raw failed-step log, with no classification, evidence refs, or suggested next command |
| `sling time <run>` | `gh run view <run-id>` | Total duration only — no queue-wait / provision / steps / teardown split |
| `sling runs list` | `gh run list [--branch --status --workflow]` | Roughly equivalent for listing |
| `sling logs --grep` | `gh run view <run-id> --log` piped to `grep` | Downloads the whole transcript to filter locally |
| `sling usage` / `top` / `bill` | — | No `gh` equivalent. Runner-minute and cost attribution is unavailable |
