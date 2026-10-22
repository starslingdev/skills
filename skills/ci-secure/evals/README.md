# ci-secure evals

`evals.json` holds five scenarios that run the whole skill as a real agent
session, from the user's request to the close, and check what it did. Unit
tests cover the scanner; these catch a change to `SKILL.md`, `references/` or
the scripts that alters what the agent actually does.

## What runs

| id | name | fixture | what it pins |
|----|------|---------|--------------|
| 1 | `template-injection` | `inject` | the engine runs; P14.10 is reported at its real file and line |
| 2 | `clean-repo` | `clean` | zero findings is reported, with the impostor-check state |
| 3 | `pwn-request` | `pwn-request` | findings land on `vulnerable.yml`; `safe.yml` comes back clean |
| 4 | `many-findings` | `many-findings` | all nine static groups are found and the render plan is requested (the table itself is judge-only) |
| 5 | `impostor-check-skipped` | `inject` | the static scan completes with the network check off, and says it was skipped |

Every entry also asserts nothing was committed or pushed. Each check carries
a `why` that records why it is anchored the way it is.

`fixtures/<slug>/scaffold.py` un-cloaks `dot-github/workflows/*.yml.fixture`
into `.github/workflows/` in an empty sandbox, makes it a git repository with
one commit, and refuses any directory that is not empty or cannot be listed.
The fixtures stay cloaked because a registry scanner reads a tracked
`.github/workflows/` path as this repository's own automation.

## How to run

    python3 maintainers/scripts/run_skill_evals.py --skill ci-secure [--id 3] [--runs 3] [--model M]

Needs `claude` on `PATH` and PyYAML importable by `python3` (the scanner's one
dependency). The runner does not check for PyYAML: without it the scanner fails
inside the session and the run exits 1, which reads as a behaviour change, so
confirm `python3 -c "import yaml"` first. The skill is mounted as a copy without `evals/`, `CHANGELOG.md`
and its oracle tests, so no answer key is on disk for the session. CI runs the
same command in `.github/workflows/skill-evals.yml`.

Exit codes: **0** every run passed; **1** a session ran and failed a check;
**2** nothing worth checking happened (no `claude`, scaffold failed, no
session, turn cap, timeout, or a `files` check with no `ci-secure-report*.md`
collected, which `files_require` enforces). A 2 is a coverage gap, never a
behaviour change. Tokens and time per run go to `skill-eval-results/`;
failing sessions to `skill-eval-logs/`, both at the repo root.

## Rules the checks obey

`tests/test_ci_secure_evals.py` enforces them offline: no `trace`, `files` or
`last_message` pattern may match text the agent can read in the mounted skill
(including `scripts/`); a pattern opening with a digit needs a `(?<![\d.])`
guard; a `trace` negative must not match `grep -n` output over the fixture;
and each entry needs one positive `trace` check that only a completed engine
run satisfies, with its `files` checks holding on the report the engine renders.
A `tool_used` pattern keeps any alternation inside a group, so it stays bound
to its tool. The vacuity rule covers the mounted skill only: the session runs
with bypassed permissions, so it could still read committed sample reports
elsewhere in the repository checkout; nothing in the prompts points it there.

## Dropped, and why

These scenarios were first written as case files for `claude plugin eval`, and
ran on a ci-secure-only stand-in harness while that command was early access. Moving to the shared runner dropped its seven `llm` graders: no
regex can judge prose (attacker scenarios, a complete numbered table, honest
scope and zero-findings wording), and nothing here runs a judge model. Their
criteria are kept in each entry's `expected_output` so the intent is not lost.
Still unchecked: the no-skill baseline arm, fix subagents staying inside their
target file, and the scanner noticing on its own that `gh` is missing.
