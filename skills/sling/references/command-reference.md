# `sling` command reference (verified)

Every flag list and JSON shape below was **read off the binary**, not off
documentation: each command was run with `--agent` against a live control
plane on **`sling` v0.1.4**, 2026-08-26. Where the published docs and the
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

Available on the root command and every subcommand **except `sling logs`**, whose strict parser rejects `--org` and `--repo` with exit `2` (`Unknown flag "--org"`) — switch the default org (`sling org switch`) instead of flagging a logs call.

| Flag | Meaning |
|---|---|
| `--agent` | Machine mode. The PUBLISHED DOCS call it equivalent to `--json --compact --no-input --no-color --yes`; no help page says that, and `--compact` is not a flag this binary has (0.1.4) — it changes nothing because unknown flags are ignored. Pass it on every **data** command — never on `sling login`, and give `sling org switch` an explicit slug |
| `--json` | JSON on stdout (implied by `--agent`) |
| `--org <slug>` | Org context. Auto-resolved when unambiguous; default set by `sling org switch` |
| `--repo <owner/name>` | Repo context. Defaults to the git remote of the current directory |
| `--version`, `-v` / `--help`, `-h` | Version / help |

`SLING_HOST` (env only, no flag) overrides the control-plane base URL for
self-hosted or development control planes. It must be `https://` — or
`http://` on a loopback host.

**Unknown flags are ignored rather than rejected — except by `sling logs`, the one strict parser.** `sling runs list
--bogus` exits `0` and returns unfiltered rows. Do not read a `0` as proof
that a filter was applied; check the returned rows.

## The precondition behind every command

`sling` reports on CI that the **StarSling GitHub App** collected. A valid
login is not sufficient: for an organization the app was never installed on,
every data command fails with exit `4` —

```
You don't have access to org "vercel". Run `sling login`.
```

— on stderr, with nothing on stdout. The trailing advice is wrong for this
case; re-authenticating changes nothing. The remedy is installing the app on
that organization (`https://github.com/apps/starslingdev`), or being added to
an org where it already is.

**Organizations only.** The app can be installed on a personal repository, but
StarSling will not pick up that repository's jobs, so there is never data to
report for one.

A connected org with a repo that has no runs in the window is the *other*
shape: exit `0` with `{"runs": [], "has_more": false, "local": {...}}`. Empty
and refused look nothing alike at the exit code, which is why branching on
the code rather than on emptiness matters here.

## Auth & setup

### `sling doctor [--agent]`

Exits `0` healthy, **`10` unhealthy**.

```json
{"checks": [{"key": "token", "ok": true, "detail": "valid session (expires …)"},
            {"key": "control_plane", "ok": true, "detail": "reachable (…)"},
            {"key": "clock_skew", "ok": true, "detail": "1s vs server"},
            {"key": "git_remote", "ok": true, "detail": "origin → …"},
            {"key": "patch_tooling", "ok": true, "detail": "git found (/usr/bin/git)"},
            {"key": "version", "ok": true, "warn": true,
             "detail": "version 0.1.3\n0.1.4 is available, to update:",
             "fix_command": "<the installer one-liner from the installation page>"},
            {"key": "org", "ok": true, "detail": "starslingdev (paid)"}]}
```

`"skipped": true` means the check produced NO verdict — a check that could
not run at all (`clock_skew` and `org` under an unreachable control plane
read `ok: false, skipped: true, "not checked"`). Read `detail`; a skipped
check is never a pass. An outdated-but-working version reads `ok: true,
warn: true` with the upgrade one-liner in `fix_command` — advisory, not a
failure (up to date it reads `ok: true, "up to date (<version>)"`).

### `sling whoami [--agent]`

**The one camelCase response.**

```json
{"identity": {"userId": "...", "name": "...", "email": "...", "githubLogin": "..."},
 "credential": {"type": "session", "expiresAt": "2026-08-31T20:38:25.617Z"},
 "local": {"org": {"kind": "set", "slug": "starslingdev", "plan": "paid"}}}
```

### `sling login [--force] [--clear]`

GitHub device-code flow, and **the one command an agent should not run**:
it prints a code, opens a browser approval page, and blocks until a human
approves it. Ask the user to run it in their own terminal. Never pass
`--agent` — that carries `--no-input`, so the prompt is refused with exit
`2` rather than shown.

`--force` re-authenticates an existing session; `--clear` is an alias for
`logout --yes`. The credential is written to `~/.config/sling/credentials`,
per user, owner-only — so one sign-in covers every agent running as that
user.

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
 "jobs": [{"job_name": "<job name>",
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
           "name": "<job name>", "status": "completed",
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

A log-less job also stamps `"local": {"empty": {"kind": "absent"}}` into the payload — a machine discriminator on stdout, not only a stderr explanation. Under `--agent`, hand its stdout to
the same JSON parser as every other command.

A real job that stores no logs exits `3` — branch on the exit code, not on
whether stdout looks empty.

### `sling resolve <id | URL> [--target run|job|attempt]`

Turns a pasted Actions URL or a bare id into the ids everything else takes.
The first move when an id does not resolve.

```json
{"resolved": {"id": "att_97912608061.1", "kind": "attempt", "run_id": "…",
              "org": "starslingdev", "repo": "<repo>",
              "job_id": "97912608061", "attempt": 1, "job_name": "…",
              "runner_id": "…", "runner_name": "GitHub Actions …"},
 "local": {"input": "32881736257"}}
```

**An ambiguous input exits `2` and still returns JSON.** A run id covering
more than one job cannot resolve to a single attempt, so `resolve` lists
what it found rather than guessing:

```json
{"candidates": [{"id": "att_98038364232.1", "kind": "attempt",
                 "run_id": "…", "job_id": "98038364232", "attempt": 1,
                 "job_name": "…"}, …]}
```

stderr carries the same list as `Ambiguous — N candidates; pass one:`.
Pass one of the returned candidate ids, or `--target run` when a run is wanted — `--target job`/`attempt` re-state the kind and return the same ambiguity. Otherwise put the candidates to the user — do not pick
one silently.

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

### `sling usage [--group-by label|workflow|job|repo|day] [--order-by cost|minutes|jobs|key] [--org <slug> | --repo <owner/name>] [--window <n>d | --month]`

```json
{"group_by": "repo", "window": {"from": "…", "to": "…"},
 "plan": {"status": "paid", "period_source": "install_cycle"},
 "rows": [{"key": "owner/repo", "runner_minutes": 5320, "jobs": 2381,
           "cost_usd": 43.21, "pct_of_total": 49.46}]}
```

### `sling top [--by workflow|job|label|repo|branch] [--metric runner-minutes|cost|jobs|p95-duration] [-n <count>] [--window <n>d | --month]`

```json
{"by": "workflow", "metric": "runner-minutes", "window": {"from": "…", "to": "…"},
 "rows": [{"key": "ci", "repo": "<repo>", "runner_minutes": 70145, "cost_usd": 56.12,
           "runs": 14852, "jobs": 31983, "p50_ms": 70000, "p95_ms": 184000,
           "p99_ms": 354810, "queue_wait_ms": 14000, "trend_pct": 154.6}]}
```

`repo` can be the literal string `"(multiple)"` when one key spans repos —
do not print that as a repo name without saying what it means. `trend_pct`
is a change against the prior comparable window.

### `sling bill [--month YYYY-MM] [--select <a,b>]` and `sling bill history`

```json
{"period": {"from": "…", "to": "…"}, "status": "open",
 "period_source": "install_cycle", "runner_minutes": 12345,
 "amount_usd": 98.76, "credits_usd": 0, "amount_due_usd": 98.76,
 "line_items": [{"label": "starsling-ubuntu-24.04", "minutes": 40227, "usd": 321.816}]}
```

`bill history` returns `{"invoices": [ …the same shape plus "invoice_id"… ]}`.
Report `amount_due_usd`, not `amount_usd`, when the user asks what is owed —
they differ by `credits_usd`. `status: "open"` means the period is still
accruing; it is not a final number.

**Both scope flags take a value.** `sling <cmd> --help` renders them as
`[--org | --repo <r>]`, which reads as though `--org` were a bare switch — it
is not. A scope flag with no value is rejected rather than swallowed:
`sling usage --org --window 7d` exits `2` with `--org needs a slug (got an
empty value).` on stderr. Loud rather than silent — but it is still a bad
invocation, so pass the slug.

### `sling labels list`

```json
{"labels": [{"label": "starsling-ubuntu-24.04", "cpu": 4, "memory_gb": 16,
             "arch": "x64", "price_per_min_usd": 0.008}]}
```
