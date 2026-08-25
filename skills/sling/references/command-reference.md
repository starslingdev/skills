# `sling` command reference (verified)

Every flag list and JSON shape below was **read off the binary**, not off
documentation: each command was run with `--agent` against a live control
plane on **`sling` v0.1.2**, 2026-08-25. Where the published docs and the
binary disagree, the binary wins and the disagreement is noted.

Field names are `snake_case` everywhere **except `whoami`**. Objects are
abridged to the keys an agent routes on; the control-plane response is
passed through verbatim inside a small local envelope, so extra keys may
appear and a parser must tolerate them.

The `local` object that most responses carry is the CLI's own record of
what it resolved (`org`, and the id or input it acted on). It is client
context, not data — never report it as a finding.

## Contents

- **Global flags** — `--agent`, `--org`, `--repo`, `SLING_HOST`, and the
  unknown-flag trap
- **Auth & setup** — `doctor`, `whoami`, `login`, `logout`, `org switch`
- **Inspect** — `runs list`, `runs show`, `jobs list`, `jobs show`, `logs`,
  `resolve`
- **Diagnose** — `why`, `time` (one run), `time --repo` (percentiles)
- **Cost & capacity** — `usage`, `top`, `bill`, `bill history`, `labels list`

Each entry gives the flags and the verified JSON response shape.

## Global flags

Available on the root command and every subcommand.

| Flag | Meaning |
|---|---|
| `--agent` | Machine mode: exactly `--json --compact --no-input --no-color --yes`. Pass this on every invocation |
| `--json` | JSON on stdout (implied by `--agent`) |
| `--org <slug>` | Org context. Auto-resolved when unambiguous; default set by `sling org switch` |
| `--repo <owner/name>` | Repo context. Defaults to the git remote of the current directory |
| `--version`, `-v` / `--help`, `-h` | Version / help |

`SLING_HOST` (env only, no flag) overrides the control-plane base URL for
self-hosted or development control planes. It must be `https://` — or
`http://` on a loopback host.

**Unknown flags are ignored rather than rejected.** `sling runs list
--bogus` exits `0` and returns unfiltered rows. Do not read a `0` as proof
that a filter was applied; check the returned rows.

## Auth & setup

### `sling doctor [--agent]`

Exits `0` healthy, **`10` unhealthy**.

```json
{"checks": [{"key": "token", "ok": true, "detail": "valid session (expires …)"},
            {"key": "control_plane", "ok": true, "detail": "reachable (…)"},
            {"key": "clock_skew", "ok": true, "detail": "1s vs server"},
            {"key": "git_remote", "ok": true, "detail": "origin → …"},
            {"key": "patch_tooling", "ok": true, "detail": "git found (/usr/bin/git)"},
            {"key": "version", "ok": false, "skipped": true, "detail": "on 0.1.2",
             "fix_command": "sling update"},
            {"key": "org", "ok": true, "detail": "starslingdev (paid)"}]}
```

A check with `"skipped": true` is advisory (the `version` check reports an
available upgrade this way) and does not make the environment unhealthy.

**The `version` check's `fix_command` is not a real subcommand.** On v0.1.2
`doctor` emits `"fix_command": "sling update"`, and `sling update` **does not
exist** — the binary rejects it as an unknown command. Never run a
`fix_command` unchecked; to upgrade, re-run the installer from the
installation page. (Reported upstream; if a later release adds the
subcommand, add it to `command-surface.json` and drop this note.)

### `sling whoami [--agent]`

**The one camelCase response.**

```json
{"identity": {"userId": "...", "name": "...", "email": "...", "githubLogin": "..."},
 "credential": {"type": "session", "expiresAt": "2026-08-31T20:38:25.617Z"},
 "local": {"org": {"kind": "set", "slug": "starslingdev", "plan": "paid"}}}
```

### `sling login [--force] [--clear]`

GitHub device-code flow. Blocking and interactive — surface the code and
URL to the user and wait. `--force` re-authenticates an existing session;
`--clear` is an alias for `logout --yes`. Credential is written to
`~/.config/sling/credentials`, per user, owner-only.

### `sling logout`, `sling org switch [slug] [--no-input]`

`org switch` with no slug opens a picker; under `--agent` (which implies
`--no-input`) that is refused with exit `2`, so pass the slug.

## Inspect

### `sling runs list [filters] [--limit <n>] [--cursor <c>]`

Filters, all server-side and freely combinable: `--branch`, `--status`
(`queued`, `in_progress`, `completed`), `--conclusion` (`success`,
`failure`, `cancelled`, …), `--trigger`, `--workflow-path`, `--label`.
Time window: `--window <n>d`, `--month <YYYY-MM>`, or `--from`/`--to` —
mutually exclusive; two of them is a usage error.

`sling <cmd> --help` lists an abridged flag set (it omits `--trigger`,
`--workflow-path`, `--label` and the window flags for `runs list`); all of
them were confirmed to filter for real.

```json
{"runs": [{"run_id": "32881984759", "run_url": "https://github.com/…/actions/runs/…",
           "workflow_path": ".github/workflows/ci.yml", "branch": "main",
           "trigger": "schedule", "status": "completed", "conclusion": "skipped",
           "created_at": "2026-08-25T18:08:11.000Z", "duration_ms": 1000,
           "jobs_total": 1, "jobs_failed": 0}],
 "has_more": false, "next_cursor": null, "local": {"org": "starslingdev"}}
```

Page with `--cursor` while `has_more` is true.

### `sling runs show <run id | URL> [--wait] [--fail-fast] [--wait-timeout <d>]`

```json
{"run": {"run_id": "…", "run_url": "…", "workflow_path": "…", "branch": "main",
         "trigger": "schedule", "status": "completed", "conclusion": "failure",
         "created_at": "…", "duration_ms": 11000},
 "jobs": [{"job_name": "discord-triage / execute-workflow",
           "attempts": [{"attempt_id": "att_97912608061.1", "attempt": 1,
                         "status": "completed", "conclusion": "failure",
                         "runner_id": "1000520625", "duration_ms": 3000}]}],
 "local": {"org": "…", "run_id": "…"}}
```

`--wait` blocks until the run concludes and **exits `10` when the run did
not succeed** — that is the answer, not an error. `--fail-fast` returns as
soon as any job fails; `--wait-timeout` caps the wait.

Note the shape: `jobs[]` here carries `job_name` and `attempts[]` but no
`job_id`. To get job ids, use `jobs list --run <id>`, or `resolve`.

### `sling jobs list --run <id> | --repo <owner/name> [window] [--conclusion <c>]`

```json
{"jobs": [{"job_id": "97912608061", "run_id": "32881736257",
           "name": "discord-triage / execute-workflow", "status": "completed",
           "conclusion": "failure", "runner_id": "1000520625", "attempt": 1,
           "duration_ms": 3000, "created_at": "…"}],
 "has_more": false, "local": {"org": "…", "run_id": "…"}}
```

### `sling jobs show <job id | URL>`

Per-attempt step timings — the fastest way to see which step failed.

```json
{"run_id": "…", "run_url": "…", "job_name": "…",
 "attempts": [{"attempt_id": "att_97912608061.1", "attempt": 1,
               "status": "completed", "conclusion": "failure", "runner_id": "…",
               "duration_ms": 3000,
               "steps": [{"name": "Set up job", "number": 1, "status": "completed",
                          "conclusion": "success", "duration_ms": 1000}]}],
 "local": {"org": "…", "job_id": "…"}}
```

### `sling logs <run|job|attempt id | URL> [flags]`

Flags: `--job <name>` (one job leg of a run), `--grep <re>` (server-side),
`--since <dur>`, `--timestamps`, `--limit <n>`, `--cursor <c>`,
`--output-file <path>`.

**Under `--agent` this returns JSON**, verified against the binary:

```json
{"lines": [{"job_id": "…", "job_name": "…", "line_number": 1,
            "log_data": "Current runner version: '2.336.0'"}],
 "has_more": false, "local": {…}}
```

The published docs call `logs` the exception that streams raw text in both
modes. That describes **human** mode. Under `--agent`, hand its stdout to
the same JSON parser as every other command.

A real job that stores no logs exits `3` — branch on the exit code, not on
whether stdout looks empty.

### `sling resolve <id | URL> [--target run|job|attempt]`

Turns a pasted Actions URL or a bare id into the ids everything else takes.
The first move when an id does not resolve.

```json
{"resolved": {"id": "att_97912608061.1", "kind": "attempt", "run_id": "…",
              "org": "starslingdev", "repo": "agent-ci-mastra-test",
              "job_id": "97912608061", "attempt": 1, "job_name": "…",
              "runner_id": "…", "runner_name": "GitHub Actions …"},
 "local": {"input": "32881736257"}}
```

Id shapes accepted across commands: a run id, a job id, `att_<jobid>.<n>`,
a runner id, a bare id, or a GitHub Actions URL.

## Diagnose

### `sling why <run|job|attempt|runner id | URL>`

Classified server-side, with no LLM in the loop.

```json
{"job_id": "…", "job_name": "…", "conclusion": "failure",
 "classification": "step_failure",
 "summary": "The step 2 \"Create and execute workflow run\" step failed (exit code 3).",
 "evidence": [{"kind": "step", "ref": "step 2 \"…\""},
              {"kind": "log_line", "ref": "job_log_lines:110"}],
 "logs": "##[error]Process completed with exit code 3.",
 "log_window": "Creating workflow run for: …\n##[error]Process completed with exit code 3.",
 "suggested_actions": [{"title": "Verify the logs", "command": "sling logs … --grep '…'"}],
 "prompt": "CI job … was classified as \"step_failure\". … Investigate and, if it's a code failure, propose a fix; if it's infra/network/terminated/cancelled, decide whether a re-run is warranted before changing code.",
 "meta": {"truncated": false}, "local": {…}}
```

- `suggested_actions[].command` is a ready-to-run follow-up — prefer it
  over improvising a `--grep` pattern.
- `prompt` is written to be acted on directly by an agent.
- `meta.truncated` true means the evidence window was clipped.
- May exit `6` (partial telemetry) with a usable result.

### `sling time <run|job|attempt id | URL>` — one run

```json
{"level": "attempt", "id": "att_…", "run_id": "…", "job_id": "…", "attempt": 1,
 "job_name": "…", "wall_clock_ms": 9000,
 "phases": [{"key": "queue_wait", "ms": 6000, "pct": 66.7},
            {"key": "provision", "ms": 1000, "pct": 11.1,
             "detail": {"note": "bundles image_pull + cold_start (v1: not separable …)"}},
            {"key": "image_pull", "ms": 0, "pct": 0},
            {"key": "cache_restore", "ms": 0, "pct": 0},
            {"key": "checkout+patch", "ms": 0, "pct": 0},
            {"key": "steps", "ms": 0, "pct": 0},
            {"key": "cache_save", "ms": 0, "pct": 0},
            {"key": "teardown", "ms": 2000, "pct": 22.2}],
 "steps": [{"key": "…", "ms": 0, "conclusion": "failure"}],
 "meta": {"source": "steps",
          "truncated": [{"key": "image_pull", "reason": "bundled in provision — …"}]},
 "local": {…}}
```

**Read `meta.truncated` before reporting a phase as zero.** In v1,
`image_pull` and `cold_start` are bundled into `provision` and reported as
`0` with a reason — a zero there means *not separately measured*, not
*instant*. Saying otherwise turns a measurement gap into a false finding.

### `sling time --repo <owner/name> [--window <n>d | --month | --from/--to]`

Percentiles per phase per runner label across a repo:

```json
{"level": "repo", "repo": "owner/name", "window": {"from": "…", "to": "…"},
 "phases": [{"phase": "steps", "label": "starsling-ubuntu-24.04",
             "p50_ms": 46000, "p95_ms": 506900, "p99_ms": 586240, "jobs": 199}],
 "meta": {"source": "steps", "truncated": [...]}}
```

This is the one `sling` command that spans many runs. It is still
*measurement*, not an audit: it says which phase is slow, never why the
workflow is written the way it is. That second question is `ci-speedup`.

## Cost & capacity

### `sling usage [--group-by label|workflow|job|repo|day] [--order-by cost|minutes|jobs|key] [--org | --repo <r>] [--window <n>d | --month]`

```json
{"group_by": "repo", "window": {"from": "…", "to": "…"},
 "plan": {"status": "paid", "period_source": "install_cycle"},
 "rows": [{"key": "owner/repo", "runner_minutes": 53206, "jobs": 23812,
           "cost_usd": 361.98, "pct_of_total": 49.46}]}
```

### `sling top [--by workflow|job|label|repo|branch] [--metric runner-minutes|cost|jobs|p95-duration] [-n <count>] [--window <n>d | --month]`

```json
{"by": "workflow", "metric": "runner-minutes", "window": {"from": "…", "to": "…"},
 "rows": [{"key": "ci", "repo": "blazar", "runner_minutes": 70145, "cost_usd": 561.16,
           "runs": 14852, "jobs": 31983, "p50_ms": 70000, "p95_ms": 184000,
           "p99_ms": 354810, "queue_wait_ms": 14000, "trend_pct": 154.6}]}
```

`repo` can be the literal string `"(multiple)"` when one key spans repos —
do not print that as a repo name without saying what it means. `trend_pct`
is a change against the prior comparable window.

### `sling bill [--month YYYY-MM] [--select <a,b>]` and `sling bill history`

```json
{"period": {"from": "…", "to": "…"}, "status": "open",
 "period_source": "install_cycle", "runner_minutes": 94655,
 "amount_usd": 731.81, "credits_usd": 0, "amount_due_usd": 731.81,
 "line_items": [{"label": "starsling-ubuntu-24.04", "minutes": 40227, "usd": 321.816}]}
```

`bill history` returns `{"invoices": [ …the same shape plus "invoice_id"… ]}`.
Report `amount_due_usd`, not `amount_usd`, when the user asks what is owed —
they differ by `credits_usd`. `status: "open"` means the period is still
accruing; it is not a final number.

### `sling labels list`

```json
{"labels": [{"label": "starsling-ubuntu-24.04", "cpu": 4, "memory_gb": 16,
             "arch": "x64", "price_per_min_usd": 0.008}]}
```
