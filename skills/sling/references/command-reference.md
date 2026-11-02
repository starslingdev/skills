# `sling` command reference (verified)

Every flag list and JSON shape below was **read off the binary**, not off
documentation, except where a section says otherwise: each command was run
with `--agent` against a live control plane on **`sling` v0.1.124**,
2026-10-27. Where the published docs and the
binary disagree, the binary wins and the disagreement is noted.

Field names are `snake_case` everywhere **except `whoami`**. Objects are
abridged to the keys an agent routes on; the control-plane response is
passed through verbatim inside a small local envelope, so extra keys may
appear and a parser must tolerate them.

The `local` object that most responses carry is the CLI's own record of
what it resolved (`org`, and the id or input it acted on). It is client
context, not data — never report it as a finding.

## Contents

- **Global flags** — `--agent`, the per-command `--org` / `--repo` rules,
  `SLING_HOST`, and the unknown-flag trap
- **Auth & setup** — `doctor`, `whoami`, `login`, `update` (and background
  self-update), `logout`, `org switch`
- **Inspect** — `runs list`, `runs show`, `jobs list`, `jobs show`, `logs`,
  `resolve`
- **Diagnose** — `why`, `time` (one run), `time --repo` (percentiles,
  `--workflow` critical path, `--group-by` splits)
- **Cost & capacity** — `usage`, `top`, `bill`, `bill history`, `labels list`

Each entry gives the flags and the verified JSON response shape.
Examples use illustrative values, not live measurements or identifiers.

## Global flags

`--agent`, `--json`, `--version` and `--help` work everywhere. The two scope
flags, `--org <slug>` and `--repo <owner/name>`, are **per-command**, and
every rejection below exits `2` with nothing on stdout:

| Command | `--org` | `--repo` |
|---|---|---|
| `sling logs` | rejected (`Unknown flag "--org"`) | rejected |
| `sling time` | **rejected outright**, even beside `--repo` | **required** for the repo view |
| `sling runs list`, `jobs list`, `usage`, `top` | accepted | accepted — but **not both together** (`Use --org or --repo, not both.`) |

For `logs` and `time`, switch the default org (`sling org switch`) instead
of flagging the call. **No command reads `--repo` from the git remote** of
the current directory: without it, `runs list`, `usage` and `top` cover the
whole org (verified from inside a checkout, which returned another repo's
runs), `jobs list` exits `2` (`Narrow the listing: pass --run <id>, --repo
<owner/name>, or a time window.`), and `sling time` with no target and no
`--repo` is a usage error. Only `doctor`'s `git_remote` check reads the
remote, and it never feeds `--repo`. With both flags, keep `--repo` (it names
the org) and drop `--org`.

| Flag | Meaning |
|---|---|
| `--agent` | Machine mode. The PUBLISHED DOCS call it equivalent to `--json --compact --no-input --no-color --yes`; no help page says that, and `--compact` is not a flag this binary has (0.1.124) — it changes nothing because unknown flags are ignored. Pass it on every **data** command — never on `sling login`, and give `sling org switch` an explicit slug |
| `--json` | JSON on stdout (implied by `--agent`) |
| `--org <slug>` | Org context, where accepted. Auto-resolved when unambiguous; default set by `sling org switch` |
| `--repo <owner/name>` | Repo context, where accepted. Never defaulted from git; a bare repo name is rejected |
| `--version`, `-v` / `--help`, `-h` | Version / help |

`SLING_HOST` (env only, no flag) overrides the control-plane base URL for
self-hosted or development control planes. It must be `https://` — or
`http://` on a loopback host.

**Unknown flags are ignored rather than rejected — except by `sling logs` and `sling update`, the strict parsers.** `sling runs list
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
             "detail": "version 0.1.123\n0.1.124 is available, to update:",
             "fix_command": "sling update"},
            {"key": "auto_update", "ok": true,
             "detail": "on; not checked yet"},
            {"key": "org", "ok": true, "detail": "starslingdev (paid)"},
            {"key": "agent_skill", "ok": true,
             "detail": "sling skill installed for Claude Code"}]}
```

Nine checks. The `version` row above is shown deliberately in its OUTDATED
shape, so the upgrade fields are visible; a current binary reports
`{"key": "version", "ok": true, "detail": "up to date (0.1.124)"}` with no
`warn` and no `fix_command`. The `auto_update` row is the installer-placed,
updates-on shape. Every other row is a live 0.1.124 capture.

**The `version` row's `fix_command` depends on how `sling` was installed.**
On a normal install (placed by the StarSling installer) it is `sling update`.
On a binary the installer did not place, it is the installer one-liner — or,
for a copy sitting where the installer would write, a longer shell line that
sets the copy aside and runs the installer — and for a copy with no safe
one-line fix it is absent, with the detail ending "this copy has no safe
one-line update (`sling update` says why)". Every form is a command that
changes the user's machine: surface it, never run it.

`"skipped": true` means the check produced NO verdict — a check that could
not run at all (`clock_skew` and `org` under an unreachable control plane
read `ok: false, skipped: true, "not checked"`). Read `detail`; a skipped
check is never a pass. An outdated-but-working version reads `ok: true,
warn: true` with the update command in `fix_command` (`sling update` on an
installer-placed copy, as above) — advisory, not a failure (up to date it reads `ok: true, "up to date (<version>)"`).

**`auto_update` is advisory and can never fail the run.** It is always
`ok: true` except for a copy with no installer record at all (the last row
below), which reads `ok: false, skipped: true` — a non-verdict, so it never
contributes to exit `10`. Its shapes:

| Shape | Row |
|---|---|
| Installer-placed, updates on | `ok: true`, `"on; last checked <time> UTC: <outcome>"` (or `"on; not checked yet"`) |
| Installer-placed, turned off | `ok: true`, `"off (<why>); …"` — `<why>` is e.g. `CI is set`, `SLING_NO_AUTO_UPDATE is set`, `sling update --off`, or that the environment could not be read |
| Installer-placed, last background check failed | `ok: true, warn: true`, `"on; … To retry now:"`, `fix_command: "sling update"` |
| A copy the installer did not place, with a safe swap | `ok: true, warn: true`, `"off — the StarSling installer didn't place this sling; to swap it for one that updates itself:"`, with the swap line in `fix_command` |
| A copy with no safe one-line swap | `ok: true, warn: true`, `"off — … and it has no safe one-line swap …"` (the detail points at `sling update` for why), no `fix_command` |
| No installer record at all | `ok: false, skipped: true`, `"off — this sling wasn't placed by the StarSling installer, so it can't update itself"` |

Every `fix_command` here changes the user's machine — **surface it, never
run it**, and never turn background updates off or on yourself.

**`agent_skill` is advisory and can never fail the run.**
`doctor` is unhealthy only when a check is `ok: false` AND not `skipped`, and
this check's only `ok: false` shape is also `skipped: true` — so it never
contributes to exit `10`. It reports whether this skill is installed for a
coding agent on the machine, in four shapes:

| Shape | Row |
|---|---|
| Installed under a per-agent home (`.claude` / `.codex` / `.cursor`) | `ok: true`, `"sling skill installed for <agents>"` |
| Installed under the shared `.agents` home | `ok: true`, `"sling skill installed"` — no agent named |
| A coding agent is present but the skill is not | `ok: true, warn: true`, `"not installed — …, to install:"`, with the install command in `fix_command` |
| No coding agent found | `ok: false, skipped: true`, `"no supported coding agent detected"` (the detail closes "— sling is usable directly") — per the rule above a non-verdict, not a failure |

**Never run this row's `fix_command` yourself** — it is a mutating install,
and the machine's own agent configuration is the user's to change.

**Do not read a `warn` row as proof your own copy is missing.** The check
only looks under `<root>/{.claude,.codex,.cursor,.agents}/skills/sling/`, for
`$HOME` and for the current directory walked up to the first `.git` — so a
skill delivered any other way (a plugin or marketplace directory, a repo
checkout, an outer repo above a nested `.git`, a different `HOME` than the
one `sling` sees) reads as "not installed" while you are demonstrably
reading it. That row is still worth surfacing: it means the user has no
installed copy for their agent, and the `fix_command` is what fixes it. Report
it with its detail; do not act on it, and do not treat it as a failure.

### `sling whoami [--agent]`

**The one camelCase response.**

```json
{"identity": {"userId": "...", "name": "...", "email": "...", "githubLogin": "..."},
 "credential": {"type": "session", "expiresAt": "2026-08-31T20:38:25.617Z"},
 "local": {"org": {"kind": "set", "slug": "starslingdev", "plan": "paid"}}}
```

### `sling login [--force] [--clear]`

GitHub device-code flow, and **a command an agent must never run** (like
`sling update` and `sling logout`):
it prints a code, opens a browser approval page, and blocks until a human
approves it. Ask the user to run it in their own terminal. Never pass
`--agent` — that carries `--no-input`, so the prompt is refused with exit
`2` rather than shown.

`--force` re-authenticates an existing session; `--clear` is an alias for
`logout --yes`. The credential is written to `~/.config/sling/credentials`,
per user, owner-only — so one sign-in covers every agent running as that
user.

### `sling update [--off | --on] [--json]`

Reinstalls `sling` on the user's machine through the same installer, when a
newer release is served. It changes **no GitHub or CI state** — only the
installed binary — and it is, like `login`, **a command the user runs, not
the agent**: surface it when `doctor` suggests it, never run it yourself.
Present since 0.1.84; the parser is strict (an unknown argument, or `--off`
with `--on`, exits `2`).

Under `--agent` it prints one object on stdout (the update itself was
deliberately not run while capturing, since it changes the machine; these are
the CLI's defined outcomes, not a captured run):

| Outcome | stdout | Exit |
|---|---|---|
| Already current | `{"result": "current", "version": "…"}` | `0` |
| Updated | `{"result": "updated", "from": "…", "to": "…"}` | `0` |
| The check failed (installer URL unreachable or unreadable) | `{"result": "failed", "stage": "check", "detail": "…"}` | `5` |
| The install failed (previous version stays active) | `{"result": "failed", "stage": "install", "detail": "…"}` | `1` |
| A binary the installer did not place | `{"result": "unmanaged", "reason": "…", "install": "…"}` — `install` is the replacement line, absent when there is no safe one | `2` |

`--off` / `--on` turn background updates off and on for this install
(`{"auto_update": "off"}` / `"on"` under `--agent`; a copy the installer did
not place gets the `unmanaged` object and exit `2` instead); `sling update` still
works on demand while they are off. Opting out is the user's decision.

**Background self-update.** About once a day, after most commands finish
(not `--version`, `--help`, a bare `sling` or `sling update` itself), an
installer-placed `sling` checks for a newer release and installs it in a
detached process. Nothing is written to stdout and the exit code is never
touched; under `--agent` / `--json` it is silent (at most one stderr line, in
human mode on a terminal). It is off when `CI` or `SLING_NO_AUTO_UPDATE` is
set to anything but empty, `0` or `false`, and after `sling update --off`.
So **the version can change mid-session**: check `sling --version` (local, no
network) when the version matters, rather than trusting one seen earlier.

### `sling logout`, `sling org switch [slug] [--no-input]`

`logout` deletes the saved credential, and only the user can sign back in,
so the user runs it, never the agent. `org switch <slug>` is the agent's to
run.

`org switch` with no slug opens a picker; under `--agent` (which implies
`--no-input`) that is refused with exit `2`, so pass the slug.

## Inspect

### `sling runs list [filters] [--org <slug> | --repo <owner/name>] [--limit <n>] [--cursor <c>]`

Filters, all server-side and freely combinable: `--branch`, `--status`
(`queued`, `in_progress`, `completed`), `--conclusion` (`success`,
`failure`, `cancelled`, …), `--trigger`, `--workflow-path`, `--label`.
Time window: `--window <n>d`, `--month <YYYY-MM>`, or `--from`/`--to` —
mutually exclusive; two of them is a usage error.

`sling <cmd> --help` lists an abridged flag set (it omits `--trigger`,
`--workflow-path`, `--label` and the window flags for `runs list`); all of
them were confirmed to filter for real.

```json
{"runs": [{"run_id": "1000", "run_url": "https://github.com/…/actions/runs/…",
           "workflow_path": ".github/workflows/ci.yml", "branch": "main",
           "trigger": "schedule", "status": "completed", "conclusion": "skipped",
           "created_at": "2026-08-25T18:08:11.000Z", "duration_ms": 1000,
           "jobs_total": 1, "jobs_failed": 0}],
 "has_more": false, "next_cursor": null, "local": {"org": "starslingdev"}}
```

Page with `--cursor` while `has_more` is true. `--org` and `--repo` together
exit `2` (`Use --org or --repo, not both.`).

### `sling runs show <run id | URL> [--wait] [--fail-fast] [--wait-timeout <d>]`

```json
{"run": {"run_id": "…", "run_url": "…", "workflow_path": "…", "branch": "main",
         "trigger": "schedule", "status": "completed", "conclusion": "failure",
         "created_at": "…", "duration_ms": 11000},
 "jobs": [{"job_name": "<job name>",
           "attempts": [{"attempt_id": "att_2000.1", "attempt": 1,
                         "status": "completed", "conclusion": "failure",
                         "runner_id": "3000", "duration_ms": 3000}]}],
 "local": {"org": "…", "run_id": "…"}}
```

`--wait` blocks until the run concludes and **exits `10` when the run did
not succeed** — that is the answer, not an error. `--fail-fast` returns as
soon as any job fails; `--wait-timeout` caps the wait.

Note the shape: `jobs[]` here carries `job_name` and `attempts[]` but no
`job_id`. To get job ids, use `jobs list --run <id>`, or `resolve`.

### `sling jobs list --run <id> | --repo <owner/name> [window] [--conclusion <c>] [--afterburner engaged|not-engaged]`

```json
{"jobs": [{"job_id": "2000", "run_id": "1000",
           "name": "<job name>", "status": "completed",
           "conclusion": "failure", "label": "<runner label>",
           "runner_id": "3000", "attempt": 1,
           "duration_ms": 3000, "created_at": "…"}],
 "has_more": false, "local": {"org": "…", "run_id": "…"}}
```

`--afterburner engaged` keeps only jobs where Afterburner engaged (the job
went over its billed size at any point); `not-engaged` the rest; any other
value exits `2`. Rows on runners that report Afterburner carry optional
fields — `afterburner_engaged`, `afterburner_coverage`,
`afterburner_complete`, `afterburner_rate` — absent elsewhere, so a parser
must not require them; `label`, `runner_id` and `duration_ms` are optional too. `--org` together with `--repo` exits `2`.

### `sling jobs show <job id | URL>`

Per-attempt step timings — the fastest way to see which step failed.

```json
{"run_id": "…", "run_url": "…", "job_name": "…",
 "attempts": [{"attempt_id": "att_2000.1", "attempt": 1,
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
{"resolved": {"id": "att_2000.1", "kind": "attempt", "run_id": "…",
              "org": "starslingdev", "repo": "<repo>",
              "job_id": "2000", "attempt": 1, "job_name": "…",
              "runner_id": "…", "runner_name": "GitHub Actions …"},
 "local": {"input": "1000"}}
```

**An ambiguous input exits `2` and still returns JSON.** A run id covering
more than one job cannot resolve to a single attempt, so `resolve` lists
what it found rather than guessing:

```json
{"candidates": [{"id": "att_2001.1", "kind": "attempt",
                 "run_id": "…", "job_id": "2001", "attempt": 1,
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

### `sling time --repo <owner/name> [--workflow <name>] [--group-by afterburner|provider|tier] [--window <n>d | --month | --from/--to]`

**`--repo` is required for this view and is never read from the git
remote**: `sling time` with no target and no `--repo` exits `2`, even inside
a checkout. **`--org` is rejected outright** (exit `2`, "`sling time`
decomposes one repo or one target, so `--org` … doesn't apply"), with or
without `--repo` — switch the default org instead.

Percentiles per phase per runner label across a repo:

```json
{"level": "repo", "repo": "owner/name", "window": {"from": "…", "to": "…"},
 "phases": [{"phase": "steps", "label": "starsling-ubuntu-24.04",
             "p50_ms": 60000, "p95_ms": 300000, "p99_ms": 600000, "jobs": 200}],
 "meta": {"source": "steps", "truncated": [...]}}
```

**`--workflow <name>`** narrows to one workflow and adds its dominant
critical path, hop by hop — the answer to "which part of this workflow is
slow". Shown here as `--workflow <name> --group-by afterburner` output, so
each hop's `cells[]` carries one entry per group, with its `delta` against
the baseline group (values synthetic; trimmed here to the `on` group):

```json
{"level": "repo", "workflow": "<workflow name>", "phases": [...],
 "critical_path": {"path": ["setup", "build", "test"], "runs": 1000,
                   "path_runs": 500, "min_sample": 20,
                   "hops": [{"job_name": "build", "label": "<runner label>",
                             "cells": [{"group": "on", "p50_ms": 120000, "n": 50,
                                        "delta": {"kind": "estimate", "ms": -20000,
                                                  "pct": -14.3, "error_ms": 10000,
                                                  "baseline_p50_ms": 140000}}]}],
                   "total": [{"group": "on", "p50_ms": 1200000}]}}
```

Hops are started→completed of successful jobs on each hop's runner label.
A `delta` of `{"kind": "insufficient_sample"}` means fewer than `min_sample`
jobs on one side — **report that as "not enough runs to compare", never as
zero**.

**`--group-by afterburner|provider|tier`** splits the repo view by where
each job ran, adding a `grouping` object: per-group job counts (and, for
Afterburner, how many were captured and engaged) and p50/p95/p99 per phase
per group (values synthetic):

```json
{"grouping": {"group_by": "afterburner", "baseline": "off",
              "groups": [{"key": "on", "jobs": 200, "captured": 200, "engaged": 50},
                         {"key": "off", "jobs": 500, "captured": 0, "engaged": 0},
                         {"key": "unknown", "jobs": 100}],
              "phases": [{"phase": "steps", "group": "on", "p50_ms": 60000,
                          "p95_ms": 300000, "p99_ms": 600000, "jobs": 200}]}}
```

The grouped phase table carries **no deltas**: each group pools whatever jobs
ran in it, so a phase difference measures the job mix, not the runner. The
like-for-like comparison against the `baseline` group is the per-hop `delta`
of `--workflow` combined with `--group-by`. `unknown` is jobs with no
placement record, and never carries a delta.

This is the one `sling` command that spans many runs. It is still
*measurement*, not an audit: it says which phase is slow, never why the
workflow is written the way it is. That second question is `ci-speedup`.

## Cost & capacity

### `sling usage [--group-by label|workflow|job|repo|day] [--order-by cost|minutes|jobs|key] [--org <slug> | --repo <owner/name>] [--window <n>d | --month]`

```json
{"group_by": "repo", "window": {"from": "…", "to": "…"},
 "plan": {"status": "paid", "period_source": "install_cycle"},
 "rows": [{"key": "owner/repo", "runner_minutes": 5000, "jobs": 2000,
           "cost_usd": 40, "pct_of_total": 100}]}
```

`--org` and `--repo` are mutually exclusive: both together exit `2` (`Use
--org or --repo, not both.`). The same holds for `sling top`. There is no
Afterburner axis here; compare timings with and without Afterburner with `sling
time --repo <owner/name> --workflow <name> --group-by afterburner` (the per-hop
`delta`).

### `sling top [--by workflow|job|label|repo|branch] [--metric runner-minutes|cost|jobs|p95-duration] [-n <count>] [--org <slug> | --repo <owner/name>] [--window <n>d | --month]`

```json
{"by": "workflow", "metric": "runner-minutes", "window": {"from": "…", "to": "…"},
 "rows": [{"key": "ci", "repo": "<repo>", "runner_minutes": 10000, "cost_usd": 80,
           "runs": 1000, "jobs": 2000, "p50_ms": 60000, "p95_ms": 180000,
           "p99_ms": 360000, "queue_wait_ms": 10000, "trend_pct": 50}]}
```

`repo` can be the literal string `"(multiple)"` when one key spans repos —
do not print that as a repo name without saying what it means. `trend_pct`
is a change against the prior comparable window.

### `sling bill [--month YYYY-MM] [--select <a,b>]` and `sling bill history [-n <count>] [--after <invoice_id>]`

```json
{"period": {"from": "…", "to": "…"}, "status": "open",
 "period_source": "install_cycle", "runner_minutes": 12345,
 "amount_usd": 98.76, "credits_usd": 8, "amount_due_usd": 90.76,
 "free_credit_total_usd": 8, "free_credit_remaining_usd": 0,
 "line_items": [{"label": "starsling-ubuntu-24.04", "minutes": 12345, "usd": 98.76,
                 "afterburner_rate": 0.5, "afterburner_engaged_jobs": 100}],
 "projected_month_end_usd": 192.4}
```

`bill history` returns `{"invoices": [ …the same shape plus "invoice_id"… ]}`.
Report `amount_due_usd`, not `amount_usd`, when the user asks what is owed —
they differ by `credits_usd`. `free_credit_total_usd` /
`free_credit_remaining_usd` (optional) describe a free-credit grant; they
are context, not the amount owed — still report `amount_due_usd`.
`projected_month_end_usd` is a projection, not a bill. The values in
the example above are illustrative, not a capture; the field names are the
binary's. Line items may
carry optional Afterburner counts (`afterburner_rate`,
`afterburner_total_jobs`, `afterburner_known_jobs`,
`afterburner_measured_jobs`, `afterburner_engaged_jobs`). `status: "open"`
means the period is still accruing; it is not a final number.

**Both scope flags take a value.** `sling <cmd> --help` renders them as
`[--org | --repo <r>]`, which reads as though `--org` were a bare switch — it
is not. A scope flag with no value is rejected rather than swallowed:
`sling usage --org --window 7d` exits `2` with `--org needs a slug (got an
empty value).` on stderr. Loud rather than silent — but it is still a bad
invocation, so pass the slug.

### `sling labels list [--org <slug>]`

```json
{"labels": [{"label": "starsling-ubuntu-24.04", "cpu": 4, "memory_gb": 16,
             "arch": "x64", "price_per_min_usd": 0.008},
            {"label": "<private-beta label>", "cpu": 16, "memory_gb": 64,
             "arch": "x64", "price_per_min_usd": 0.032, "private_beta": true}]}
```

Rows with `private_beta: true` are that org's private-beta labels — the
default org's, or those of the org named by `--org`. Say so before
suggesting one for `runs-on`, and never present one as generally
available.
