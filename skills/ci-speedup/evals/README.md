# ci-speedup evals

`evals.json` holds ten scenarios in Anthropic's skill-eval format. Most are
reference cases a maintainer runs by hand against public repositories. The
entries that carry a `checks` list are also executed automatically, as real
agent sessions, so a change to `SKILL.md` or `references/` that breaks the
audit turns a check red.

## What runs

| id | name | what it proves |
|----|------|----------------|
| 10 | `full-run-synthetic` | a full audit of the synthetic repository: the skill is invoked, `run.py` runs, the report names `ci.yml` / `CI / test` as long pole 1, `verify_report.py` passes, nothing is committed or pushed |
| 6 | `security-ask-routes-to-ci-secure` | a security-posture request is answered by ci-secure (mounted beside ci-speedup), never by ci-speedup's pipeline |

Both use the `synthetic` fixture (`fixtures/synthetic/scaffold.py`), which
builds the repository and gh replay corpus of `tests/test_offline_pipeline_e2e.py`
with that test's own helpers. Every run-history read is answered offline from
the corpus, so the report's numbers are fixed. The agent's own `gh repo view` /
`gh api` calls about the synthetic repository are answered by a maintainer-side
stand-in from the same corpus; `gh auth status` still goes to the real `gh`.

Optional entry fields: `fixture` (scaffold under `fixtures/`), `plugins`
(other skills to mount), `max_turns`, and `checks`. A check is
`{name, type, pattern, match?, min?, max?}`: `tool_used` counts tool calls
whose `"<Tool> <json input>"` matches `pattern` (default `min` 1); `trace`,
`files` and `last_message` search the transcript, the files the session
wrote, or its final message (`match`: `contains` or `not_contains`). No
`trace`/`files`/`last_message` pattern may match text the agent can read in
the skill; `tests/test_ci_speedup_evals.py` enforces it.

## How to run

    python3 maintainers/scripts/run_skill_evals.py --skill ci-speedup [--id 10] [--runs 3] [--model M]

Needs `claude` and an authenticated `gh` (the skill's phase 1 checks it).
The skill is mounted as a copy without `evals/`, `CHANGELOG.md`,
`tests/test_*.py`, `tests/conftest.py` or `tests/fixtures/` (the answer key); `tests/verify_report.py`
stays because the skill runs it. CI runs the same command in the
`ci-speedup skill evals` job of `.github/workflows/skill-evals.yml`.

Exit codes: **0** every run passed; **1** a session ran and failed a check;
**2** the harness could not run (no `claude`, scaffold failed, no session, turn
cap hit, or a `files` check with nothing collected). A 2 is a coverage gap,
never a behaviour change.

## Cost

About $0.40 to $0.65 and 30 to 95 seconds per run (measured on claude 2.1.307
locally and the pinned 2.1.235 in CI).
Tokens, duration and cost per run are printed and written to
`skill-eval-results/` at the repo root (gitignored); failing sessions are kept
in `skill-eval-logs/`.

## Not covered

- The other eight scenarios: they need a live repository of a given shape, a
  follow-up fix session, or a judgement a regex cannot make.
- A no-skill baseline arm, and LLM-judged checks.
- The close's wording beyond what the checks pin.
