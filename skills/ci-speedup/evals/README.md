# ci-speedup evals

`evals.json` holds thirteen scenarios in Anthropic's skill-eval format. Most are
reference cases a maintainer runs by hand against public repositories. The
entries that carry a `checks` list are also executed automatically, as real
agent sessions, so a change to `SKILL.md` or `references/` that breaks the
audit turns a check red.

## What runs

| id | name | what it proves |
|----|------|----------------|
| 10 | `full-run-synthetic` | a full audit of the synthetic repository: the skill is invoked, `run.py` runs, the report names `ci.yml` / `CI / test` as long pole 1, `verify_report.py` passes, nothing is committed or pushed |
| 6 | `security-ask-routes-to-ci-secure` | a security-posture request is answered by ci-secure (mounted beside ci-speedup), never by ci-speedup's pipeline |
| 11 | `quality-review-opt81` | **tier `measure`**: after the audit, the quality review checks the OPT81 runner-class card against its catalog entry and names the fork pull request caveat that exists only in that entry; the session must have read the `### OPT81` heading |
| 12 | `gap-fill-coverage-lookup` | before a gap-fill, the agent looks up whether a dependency compiled from source on one matrix leg is already a catalog pattern and names OPT86 (a manual pattern no script names); reading is recorded |
| 13 | `no-catalog-read-needed` | a question the measured report answers on its own (the typical wait, the slowest check); whether the catalog was opened is recorded, not asserted |

All but 12 use the `synthetic` fixture (`fixtures/synthetic/scaffold.py`), which
builds the repository and gh replay corpus of `tests/test_offline_pipeline_e2e.py`
with that test's own helpers. Every run-history read is answered offline from
the corpus, so the report's numbers are fixed. The agent's own `gh repo view` /
`gh api` calls about the synthetic repository are answered by a maintainer-side
stand-in from the same corpus. `gh auth status` and read-only `gh api` calls
about other repositories (GETs with no field or input flags) go to the real
`gh`; any other call fails closed with a local 404.

**Tiers.** An entry's `tier` is `gate` (the default) or `measure`. Gate
entries are the per-pull-request regression check and run by default; a
`measure` entry exists for a measurement (entry 11: the catalog before/after
and the baseline, where its review subagents cost about seven minutes and
$3.77 a run in CI) and runs only with `--tier measure` or `--all`. A skipped
entry still prints a `SKIPPED (tier)` line and a `skipped` row in the results.

**Parallel sessions.** Runs go up to five at a time (`--jobs N`, or env
`SKILL_EVALS_JOBS`), each with its own sandbox, skill copy and `TMPDIR`. The
full-audit entries 10, 11 and 13 are marked `serial: true` and take turns:
their sessions write their findings and report to a `/tmp/...` path they pick
themselves (seen: `/tmp/cisp-audit/`), not under `TMPDIR`, so two at once
would overwrite each other's files. Entries 6 and 12 run beside them.

Optional entry fields: `fixture` (scaffold under `fixtures/`), `plugins`
(other skills to mount), `max_turns`, `tier`, `serial`, and `checks`. A check is
`{name, type, pattern, match?, min?, max?}`: `tool_used` counts tool calls
whose `"<Tool> <json input>"` matches `pattern` (default `min` 1); `trace`,
`files` and `last_message` search the transcript, the files the session
wrote, or its final message (`match`: `contains` or `not_contains`); `reading`
counts one field of the session's reading record (below) as `{field,
pattern?, min?, max?}`. No `trace`/`files`/`last_message` pattern may match
text the agent can read in the skill; `tests/test_ci_speedup_evals.py`
enforces it.

## Reading accounting

Every run also records how much of `references/` the session pulled into its
context, because the catalog refactor's claim is "same report, less reading".
`maintainers/scripts/eval_reading.py` pairs each `Read` call, each `Grep` call
in `content` mode, and each `Bash` call with a command that prints file content
(`cat`, `sed`, `head`, `tail`, `grep`, `egrep`, `fgrep`, `rg`, `less`, `more`,
`awk`, `nl`, `jq`, `tac`, `cut`, `sort`, `uniq`, `strings`, `bat`, `diff`, an
`xargs` or `find -exec` feeding one of these, and inline `perl` / `python` /
`ruby` / `node` scripts) with its tool result and charges the result's bytes
and lines to the reference file(s) the call names (including `$VAR/references/`,
`cd .../references` then a bare file name, globs, and directory-wide greps,
split per output line). A search from the skill root or above is charged per
output line to the `references/<file>` its path prefix names. Each results row
carries `reading: {reference_bytes, reference_lines, reference_files: [{path,
bytes, lines, calls}], catalog_opened, catalog_entries_read,
unattributed_calls}`, and the PASS / FAIL line carries a compact figure such as
`refs 16.0 KB in 2 files, catalog opened; session 175k in, 23s wall`.
A failed read counts nothing, a catalog entry counts as read only when its
`##` or `###` heading comes with some of its body (an index grep reads none),
and paths that reach another copy (a URL, `gh api`, `$HOME`, or a `cd` to
another install) are not the copy under test. Name-only output reads nothing
(`Grep` in its default `files_with_matches` mode, `grep -l` / `-c`, `ls`,
`find`, `wc`). A `Bash` call that names a mounted reference file in a command
the tracker cannot read (`cp`, a script) is counted in `unattributed_calls`
and shown in the figure, never silently dropped. It measures what the agent
saw, not what it asked for: a `sed -n` of one entry costs that entry, a full
`Read` of the catalog costs the whole file. Reads by a subagent count only as
far as the stream carries them.

To prove a reading check is sensitive, run it with the entry it needs cut from
the session's copy of the catalog (the repository is not touched). The wrapper
also blocks the published copy on GitHub, which a session denied the local
entry otherwise fetches (seen with both `curl` and `gh api`): web clients that
honour the proxy variables reach only the model API, and `gh` refuses the
publishing repository. It exits non-zero if no mounted copy was cut:

    python3 maintainers/ci-speedup/scripts/eval_withhold_entry.py OPT81 --all --id 11 --model M

## Baseline

`maintainers/ci-speedup/evals/baseline/2026-10-22/` holds the committed
baseline on the unsplit catalog: five runs of every runnable entry with a
pinned model, the per-run results JSON, and `summary.md` (per case: median and
range of reference bytes, files read, input tokens, wall time, and pass counts
per check). Re-running it: `maintainers/ci-speedup/MAINTAINERS.md`.

## How to run

    python3 maintainers/scripts/run_skill_evals.py --skill ci-speedup [--id 10] [--runs 3] [--model M]
    python3 maintainers/scripts/run_skill_evals.py --skill ci-speedup --all --id 11   # the measure tier

Needs `claude` and an authenticated `gh` (the skill's phase 1 checks it).
The skill is mounted as a copy without `evals/`, `CHANGELOG.md`,
`tests/test_*.py`, `tests/conftest.py` or `tests/fixtures/` (the answer key); `tests/verify_report.py`
stays because the skill runs it. CI runs the same command (gate tier) in the
`ci-speedup skill evals` job of `.github/workflows/skill-evals.yml`; its
manual dispatch takes `tier: gate | measure | all`.

Exit codes: **0** every run passed; **1** at least one session ran and failed
a check (even when other runs could not run); **2** otherwise, when any run
could not run or nothing ran: no `claude`, no real `gh` for the shim, nothing
selected or `--runs` below 1, scaffold failed, no session, session timeout,
turn cap hit, a `files` check with nothing collected, or an unexpected crash.
A 2 is a coverage gap, never a behaviour change.

## Cost

In the 2026-10-22 baseline (claude 2.1.308, locally): $0.23 to
$0.39 and 17 to 69 seconds for entries 6, 10, 12 and 13; $0.41 to $1.18 and
47 to 153 seconds for entry 11, which may start two review subagents (in CI
on 2026-10-22: 424 seconds and $3.77). One run of all five entries is about
$2.10 locally. In that CI run the gate tier (6, 10, 12, 13) cost $1.76 of
the five entries' $5.53.
Tokens, duration and cost per run are printed and written to
`skill-eval-results/` at the repo root (gitignored); failing sessions are kept
in `skill-eval-logs/`.

## Not covered

- The other eight scenarios: they need a live repository of a given shape, a
  follow-up fix session, or a judgement a regex cannot make.
- A no-skill baseline arm, and LLM-judged checks.
- The close's wording beyond what the checks pin.
