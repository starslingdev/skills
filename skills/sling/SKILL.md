---
name: sling
description: >-
  Answers live questions about one GitHub Actions run or job by driving
  `sling`, StarSling's read-only agent-first CLI, and routes anything
  `sling` cannot do to the `gh` CLI. Uses `sling why` for why a job failed,
  `sling time` for where a run's wall-clock went, `sling logs` for only the
  lines that matter, and `sling usage`, `sling top` and `sling bill` for
  runner-minutes and spend.
  Falls back to `gh` for everything that changes state, because `sling` is
  read-only: re-running, cancelling, triggering, enabling or disabling a
  workflow, downloading an artifact. Runs `sling doctor` as a preflight and
  walks the user through `sling login`. Use when the user asks
  about a specific CI run or job, or gives a live instruction to inspect,
  diagnose, re-run, cancel, or trigger GitHub Actions right now. Do NOT
  trigger for: a repo-wide CI audit, grade, speed sweep, or security scan of
  the workflow files, which belong to ci-score, ci-speedup and ci-secure;
  writing workflow YAML; or non-GitHub-Actions CI.
license: MIT
---

# sling CLI

`sling` is StarSling's agent-first CLI for GitHub Actions. It answers
questions about **live CI data** — one run, one job, one attempt, or an
account's runner-minutes — from StarSling's control plane, with structured
JSON on stdout and a stable exit-code table. In this release it is
**read-only**: it reads and analyzes, and there is no `sling` command that
changes anything in GitHub.

**This skill's job is picking the right tool per request.** Live question
about a run, a job, or spend → `sling`. Anything that *changes* CI state →
`gh`. A repo-wide audit of workflow *files* → hand off to the audit skill
that owns it and stop. Do not invent a `sling` subcommand for something
`sling` does not do; [the command
surface](references/command-surface.json) is the complete list.

Every fact in this skill's command reference was read off `sling` itself
(v0.1.2) rather than from documentation — see
[references/command-reference.md](references/command-reference.md) for the
verified per-command JSON shapes.

## Preflight: is `sling` installed and authenticated?

Do this once per session, before the first `sling` command.

1. **Is it on PATH?** `command -v sling`. If it is missing, `sling` is
   installed by a one-line installer published on the installation page at
   `https://docs.starsling.dev/sling-cli/installation` — point the user at
   that page and let them run it, rather than pasting an installer command
   from memory. It supports **Apple Silicon macOS and x64 glibc Linux
   only**; there is no Windows build, so on Windows say so plainly and use
   the `gh`-only path in step 6.

2. **Is the environment healthy?** `sling doctor --agent`. It emits
   `{"checks": [{"key", "ok", "detail", ...}]}` on stdout and **exits `10`
   when a real check fails** (`0` when healthy). The checks name the
   problem — `token`, `control_plane`, `clock_skew`, `git_remote`,
   `patch_tooling`, `version`, `org` — so act on the one that is `ok:
   false` instead of guessing. A `version` check with `"skipped": true` is
   an upgrade notice, not a failure — and the fix command it suggests is not
   a real subcommand, so re-run the installer instead of running it (see
   [references/command-reference.md](references/command-reference.md)).

3. **Not authenticated** (`doctor`'s `token` check fails, or any command
   exits `4`): **ask the user to run `sling login` in their own terminal**,
   then wait for them to say it is done. Do not run it yourself, and never
   pass `--agent` to it.

   Signing in needs a person at a browser: `sling login` prints a device
   code, opens a GitHub approval page, and blocks until someone approves it.
   Launched as a subprocess that is unworkable in both directions — the
   output arrives only once the command has already finished or timed out,
   so the code is never visible while it can still be used, and `--agent`
   would refuse the prompt outright with exit `2`. There is no automated
   path here, and pretending otherwise just hangs the session.

   The credential is saved per user at `~/.config/sling/credentials`, so
   once they have signed in, an agent running as them is authenticated with
   no further setup. Re-run `sling doctor --agent` to confirm, then carry
   on.

4. **Wrong org** (`doctor`'s `org` check, an exit `2` naming org ambiguity,
   or an id that resolves to nothing but plausibly exists elsewhere): `sling
   org switch <slug>`, or pass `--org <slug>` on the single command. `--org`
   and `--repo <owner/name>` are global flags on every subcommand; `--repo`
   otherwise defaults to the git remote of the current directory.

5. **StarSling gate — no installation on the org.** `sling` reports CI that
   the **StarSling GitHub App** collected, so a valid login sees nothing for
   an org where the app was never installed. It surfaces as exit `4` with
   `You don't have access to org "<name>"`, and the message ends `Run
   \`sling login\`` — advice that cannot work, because the credential was
   never the problem. A sandboxed agent shell can also fail an auth probe for
   reasons of its own, so retry once with host access before trusting a
   single failure.

   When it is genuinely not installed, **STOP and tell the user plainly** —
   every answer this skill gives comes from StarSling's record of their runs,
   so without the app on that org there is no run data to read and only the
   `gh` read path remains. Give them the path: install the app on that
   organization at `https://github.com/apps/starslingdev`, or ask an owner to
   add them. Continue on the `gh`-only path ONLY if they say so, and name
   what is unavailable when you do.

   **Organizations only.** The app installs on a personal repository but
   StarSling does not pick up its jobs, so there is never data to report for
   one — say that rather than reporting an empty result.

6. **If `sling` cannot be installed or authenticated at all**, fall back to
   the `gh` read path for whatever is achievable (`gh run view`, `gh run
   view --log-failed`) and **tell the user plainly** that the richer
   diagnosis — `sling why`'s classification, `sling time`'s phase
   breakdown — is unavailable without `sling`. Do not silently degrade.

   Run the gh gate below before promising that path: with neither CLI
   available there is nothing left to fall back to, and saying so up front
   beats discovering it one failed command at a time.

## Always pass `--agent`

`--agent` is a global machine-mode flag, exactly equivalent to `--json
--compact --no-input --no-color --yes`. Pass it on every **data** command
and parse stdout as JSON. Do not also pass `--json`; `--agent` already
implies it.

**Never pass `--agent` to `sling login`.** Signing in requires a human to
open a browser and approve a device code, and `--agent` carries
`--no-input`, which refuses the prompt rather than showing it — turning the
one command that needs a person into exit `2`. `sling org switch` has the
same shape: under `--agent` its picker is refused, so give it an explicit
slug (`sling org switch acme --agent`).

- **Parse stdout first, whatever the exit code.** Human chrome (spinners,
  summary rows, prompts) goes to stderr, and only when stderr is a TTY. A
  genuine error — `1`, `2`, `3`, `4`, `5`, `7` — writes nothing to stdout
  and puts a plain-text message on stderr. But `6` and `10` are outcomes,
  not errors: they emit the **full JSON payload on stdout** with stderr
  empty. An unhealthy `sling doctor --agent` exits `10` and still returns
  every check, which is exactly what the preflight above asks you to read.
  So read stdout, and fall back to stderr only when stdout is empty.
- Under `--agent`, **every** command returns JSON, `sling logs` included.
- JSON keys are `snake_case` on every command **except `whoami`**, which
  returns camelCase (`userId`, `githubLogin`, `expiresAt`). Key a parser per
  command, not on a local-vs-remote rule.

The traps in this contract are collected under [Gotchas](#gotchas) — worth
reading before the first parse.

## The routing table

**Decide with two questions, in order.**

**1. Does the request change anything, or only read?** If the user wants to
*change* CI state — re-run a job, cancel a run, trigger a workflow, flip a
workflow on or off, pull down an artifact — `sling` cannot do it in this
release. Use `gh` directly; do not attempt a `sling` subcommand for these
under any name.

**2. If it only reads: is it about one run, one job, or aggregate
cost/usage — or about the repo's CI *configuration* as a whole?** One run,
one job, or spend attribution → `sling`. The repo's workflow YAML, its
best-practice adherence, its security posture, or a multi-run optimization
sweep → hand off (see [Handoff](#handoff-to-the-audit-skills)) instead of
reading YAML yourself.

| User is asking about... | Route to | Command |
|---|---|---|
| Why a specific job failed | `sling` | `sling why <run\|job\|attempt id\|URL>` |
| Where a run's wall-clock went (queue wait vs. provision vs. steps vs. teardown) | `sling` | `sling time <run\|job\|attempt id\|URL>` |
| The same, as percentiles across a repo | `sling` | `sling time --repo <owner/name> --window <n>d` |
| Recent runs and their status | `sling` | `sling runs list [--branch --status --conclusion --trigger --workflow-path --label --window --limit]` |
| Detail on one run (its jobs, attempts, runner) | `sling` | `sling runs show <run id\|URL> [--wait] [--fail-fast]` |
| Which jobs in a run failed, or jobs across a repo | `sling` | `sling jobs list --run <id>` / `--repo <owner/name>` |
| Which step in a job failed, and each step's duration | `sling` | `sling jobs show <job id\|URL>` |
| Only the log lines that matter for a failure | `sling` | `sling logs <run\|job\|attempt id\|URL> [--grep <re>] [--since <dur>] [--limit <n>]` |
| Turning a pasted Actions URL into a run/job/attempt id | `sling` | `sling resolve <id\|URL> [--target run\|job\|attempt]` |
| The biggest time or cost hotspots | `sling` | `sling top [--by workflow\|job\|label\|repo\|branch] [--metric ...]` |
| Runner-minutes and cost attributed per repo/workflow/label/day | `sling` | `sling usage [--group-by <axis>] [--window <n>d]` |
| What is owed this period, or past invoices | `sling` | `sling bill`, `sling bill history` |
| Which runner labels/sizes exist and what they cost | `sling` | `sling labels list` |
| Identity, org, credential, environment health | `sling` | `sling whoami`, `sling doctor`, `sling org switch` — and `sling login`, which the **user** runs, not you |
| **Re-run a run or its failed jobs** | `gh` | `gh run rerun <run-id> [--failed]` |
| **Cancel an in-progress run** | `gh` | `gh run cancel <run-id>` |
| **Trigger a workflow (`workflow_dispatch`)** | `gh` | `gh workflow run <workflow> [-f key=value]` |
| **Enable or disable a workflow** | `gh` | `gh workflow enable\|disable <workflow>` |
| **Download an artifact** | `gh` | `gh run download <run-id>` |
| **Delete a run** | `gh` | `gh run delete <run-id>` |
| Approve or reject a pending deployment | `gh` | `gh api` — see [references/gh-fallback.md](references/gh-fallback.md) |
| The checks on a specific PR | `gh` | `gh pr checks <pr>` — then take the failing run/job id back into `sling why` / `sling time` |
| Secrets or repo/environment variables | `gh` | `gh secret`, `gh variable` |
| The contents of a workflow file | neither | Read the `.yml` directly — this is a file question, not a CLI action |
| A repo-wide grade, speed sweep, or security scan | neither | Hand off — see [Handoff](#handoff-to-the-audit-skills) |

**Ambiguous asks.** With no action verb (re-run / cancel / trigger / enable
/ disable / download) and a specific run, job, or spend in view, default to
`sling`: it is the read path and costs nothing to try. If the ask is
read-only but `sling` has no matching subcommand — "what does this
workflow's `on:` trigger include" is YAML content, not run data — read the
file rather than forcing it through either CLI.

**Compound asks** — "tell me why this run failed, then re-run it" — are two
steps, in order: do the `sling` half first (`sling why`), **report it**,
then do the `gh` half (`gh run rerun`) as an explicit, separately announced
action. Never chain into a state change without telling the user what
changed.

## Command reference

Full per-command flags and the verified JSON shape of every response:
[references/command-reference.md](references/command-reference.md). The
machine-readable list of every command that exists, which is what keeps this
skill from inventing one:
[references/command-surface.json](references/command-surface.json).

The short version, grouped the way `sling --help` groups them:

- **Auth & setup** — `login`, `logout`, `whoami`, `doctor`, `org switch`.
- **Inspect CI** — `runs list`, `runs show`, `jobs list`, `jobs show`,
  `logs`, `resolve`.
- **Diagnose** — `why` (classification, evidence, suggested actions, and a
  ready-to-use `prompt` field), `time` (wall-clock split into
  `queue_wait`, `provision`, `image_pull`, `cache_restore`,
  `checkout+patch`, `steps`, `cache_save`, `teardown`).
- **Cost & capacity** — `usage`, `top`, `bill`, `bill history`,
  `labels list`.

Two habits worth keeping:

- **Start from `why` for a failure**, not from `logs`. `why` is classified
  server-side with no LLM in the loop, and its `suggested_actions[]` carry
  the exact follow-up command (usually a `sling logs --grep`) instead of
  making you guess a pattern. Its `prompt` field is written for an agent to
  act on directly.
- **`sling logs` is a filter, not a dump.** Reach for `--grep` and
  `--limit` before pulling a whole transcript; `has_more` plus `--cursor`
  pages the rest.

## Exit codes

Branch on `$?`, read immediately after the command. Never infer failure
from empty stdout — a real result can be empty and a failed command writes
nothing to stdout at all.

| Code | Meaning | What to do |
|---|---|---|
| `0` | Success | — |
| `1` | Unexpected internal error (a crash) | Do not retry blindly. Surface the stderr text; this is a bug report, not a routing decision |
| `2` | Usage — bad flags, a prompt refused under `--agent`, or org ambiguity | Fix the invocation against `sling <cmd> --help`; if org-ambiguous, pass `--org` or run `sling org switch` |
| `3` | Not found — no such run/job/attempt in this org, or a real job that stores no logs | Try `sling resolve` on the raw id or URL, confirm the org, then ask the user to confirm the id |
| `4` | Auth — **or no StarSling installation on that org.** Read the stderr text before acting | `You don't have access to org "<name>"` → the app is not installed there (or the user is not a member); point them at `https://github.com/apps/starslingdev`, do NOT retry login. Anything else → ask the user to run `sling login` themselves (a browser approval, never `--agent`), then retry once |
| `5` | Control-plane or API error (5xx or transport) | Retry once after a short backoff (~2s); on a second failure fall back to the `gh` read equivalent and **say** `sling` was unreachable |
| `6` | Partial — telemetry incomplete, result still emitted (`time`, `why`) | Not an error. Use the result, and tell the user it is partial |
| `7` | Rate limited (HTTP 429) | Back off and retry once. Do not hammer |
| `10` | Remote outcome failed — `doctor` unhealthy, or `runs show --wait` on a run that did not succeed | **Not a CLI error.** This is the answer: report the unhealthy check, or the run's failure |

`sling exit-codes` on v0.1.2 prints only `0`–`5`; codes `6`, `7`, and `10`
are real and documented (and `doctor --help` names `10` itself), so treat
that help text as abridged. Any non-zero code not in this table: surface
stderr to the user rather than guessing a recovery.

## Gotchas

These are the places where `sling` behaves differently from what its own
output, its `--help`, or its documentation implies. Each was found by
running the binary; none of them announce themselves at runtime.

- **On the current release (0.1.2), `sling doctor` recommends a command that
  does not exist.** Its `version` check emits `"fix_command": "sling
  update"`, and the binary rejects `sling update` as unknown. Fixed on the
  CLI's main branch, where the same check emits the installer one-liner
  instead — but 0.1.2 is what the installer serves today, so it is what a
  user has. Never run a `fix_command` unchecked. The general lesson outlives
  the bug: a command name printed by a tool is not proof the tool has it.
- **Exit `4` does not always mean the credential is stale, and its message
  misdirects when it does not.** An org StarSling was never installed on
  fails with `You don't have access to org "<name>". Run \`sling login\`` —
  the same shape as an expired session, ending in advice that cannot work.
  Read the message before acting on the code: a named org means the app is
  missing there, not that the login is.
- **An empty listing is a coverage hole, never a finding.** `sling runs list
  --repo <name>` returns `{"runs": []}` and exit `0` both when the repo truly
  had no runs in the window and when StarSling is not watching that repo at
  all. The payload cannot tell them apart, so an empty result means this
  check did NOT run — say so with the reason you cannot rule out, rather than
  reporting "you have no CI runs" as a fact about their repo.
- **Unknown flags are ignored, not rejected.** `sling runs list --bogus`
  exits `0` and returns unfiltered rows. Exit `0` is therefore not evidence
  that a filter applied — check the rows you got back before reporting a
  filtered answer.
- **`--help` lists an abridged flag set.** `runs list --help` omits
  `--trigger`, `--workflow-path`, `--label` and the window flags, all of
  which work. Absence from `--help` is not absence from the CLI; the fuller
  list is in [references/command-reference.md](references/command-reference.md).
- **`sling logs` returns JSON under `--agent`**, despite documentation that
  calls it the one command streaming raw text in both modes. That describes
  human mode.
- **A zero phase in `sling time` can mean "not measured".** In v1,
  `image_pull` and `cold_start` are bundled into `provision` and reported as
  `0` with a reason in `meta.truncated`. Read that array before telling a
  user a phase took no time — a measurement gap reported as a finding is a
  false finding.
- **Exit `10` is an answer, not a failure.** `doctor` unhealthy and `runs
  show --wait` on a failed run both exit `10`. Report what it says. Exit `6`
  likewise carries a real result, flagged partial.
- **`--agent` silently includes `--yes`.** Harmless while `sling` is
  read-only, but do not treat a confirmation prompt as a safety net.
- **`sling runs show` gives no `job_id`** — its `jobs[]` carry `job_name`
  and `attempts[]` only. Use `jobs list --run <id>` or `resolve` when you
  need job ids.
- **`sling top` can report a repo as `(multiple)`** when one key spans
  repos. Do not print that as a repo name without saying what it means.
- **`sling bill` has two totals.** `amount_due_usd` is what is owed;
  `amount_usd` is before credits. `status: "open"` means the period is still
  accruing, so it is not a final number.

## Handoff to the audit skills

`sling` reads live run data and has **no ability to read workflow
configuration at all**, so it structurally cannot audit, grade, or scan a
repo. When a conversation turns into "audit my whole CI setup", "grade my
CI", "why is CI slow generally", or "is this secure", **stop and name the
right skill** rather than looping `sling` over every run:

| The ask | The skill |
|---|---|
| A configuration best-practices grade ("grade my CI", "CI score") | `ci-score` |
| A measured speed / runner-minute audit across many runs ("why is CI slow") | `ci-speedup` |
| A security scan of the workflow files ("is my CI secure") | `ci-secure` |

Each reads a local checkout and runs its own catalog. Name the skill and let
the user run it; do not approximate one of these from run data.

**Suggest, do not auto-chain.** After `sling usage` or `sling top` shows a
clear cost outlier, or `sling time` shows one phase dominating repeatedly,
end the answer by suggesting `ci-speedup` for the across-many-runs root
cause and fix — `sling` shows *that* this run is slow or expensive;
`ci-speedup` shows *why*, with a fix. Never invoke another skill without the
user asking.

## `gh` fallback

The mutating commands this skill routes to, with their real syntax:
[references/gh-fallback.md](references/gh-fallback.md).

**gh gate, before the first `gh` call.** If `gh` isn't installed or `gh auth
status` fails, `gh` cannot do any of it — and `gh` authenticates separately
from `sling`, so a working `sling` says nothing about whether `gh` is signed
in. Sandboxed agent shells (Codex) can't reach keyring credentials: retry
with host access before trusting a failure, and never report auth "expired"
off a sandboxed probe. Then STOP and tell the user plainly which half of
their request is unavailable — the reads still work through `sling`, the
state change does not. Give the path (https://cli.github.com; then `gh auth
login`), and never report an action as done that never ran.
