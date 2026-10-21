# Behavioural evals for ci-speedup

Two cases that run the **whole skill** (an agent, from the user's request to the
close) and grade what it did. They catch regressions in the agent-facing
contract in `SKILL.md`, which nothing else in this repository covers: `pytest`
exercises the deterministic engine underneath the skill, and
`tests/test_offline_pipeline_e2e.py` drives that engine end to end, but neither
checks that an agent reading `SKILL.md` actually runs it, in order, and closes
the way the contract says.

`evals.json` beside this file holds the original nine scenarios as prose. They
stay as the written reference; the two below are the ones that now execute.

| Case | Scenario | What it pins |
|---|---|---|
| `full-run-synthetic/` | A full audit of a known repository, request to close | The gh gate runs; `run.py --with-logs` runs; the report is rendered with the command the data pass printed; `verify_report.py` runs on it and passes; the report drills the gating checks the corpus produces, in the right order, each ending in an agent prompt; the close quotes the merge-wait figure verbatim and carries no catalog id; nothing is committed or pushed. |
| `security-ask-routes-to-ci-secure/` | `evals.json` scenario 6, the should-not-trigger case | With ci-secure installed beside it, a supply-chain / OIDC security ask is answered by ci-secure; ci-speedup is never loaded and none of its scripts run. |

Every grader is mechanical (a tool call that did or did not happen, or a regex
over the transcript, the files the session wrote, or its last message). There
are no `llm` graders yet.

## Why these run offline

ci-speedup measures a repository's real GitHub Actions history over `gh`. A live
run would audit whatever that repository did this week, and its numbers could
never be pinned. So the full-run case audits the synthetic repository the
committed replay corpus (`tests/fixtures/gh_replay/`) was recorded against, and
every run-history call is answered from that corpus:

- the scaffold builds the checkout with `tests/fixtures/synthetic_corpus.py`,
  the same builder the offline e2e test uses, so the agent audits exactly the
  repository the e2e proves renders a report `verify_report.py` accepts;
- the harness sets `CI_SPEEDUP_GH_FIXTURES` for the session, which the
  skill's own `gh` client (`collect_runs.GhClient`) already honours;
- a small `gh` shim on the session's PATH answers `gh repo view` and
  `gh api repos/synthetic/repo...` from the same corpus. Without it, a session
  that confirmed the target with its own `gh` call got real GitHub's 404 for a
  repository that exists only in the corpus, and stopped to ask about access.
  Everything else, including `gh auth status`, goes to the real `gh`.

**The gh gate is not bypassed.** `SKILL.md` phase 1 stops the audit unless
`gh auth status` passes, and the harness passes the caller's real GitHub auth
through for that (a laptop's `gh` login, or `GH_TOKEN` in CI). With replay on,
that auth is never used for a data call. No auth means the full-run case fails,
which is the skill doing what its contract says.

## Running them

The runner is the maintainer-only harness at
`maintainers/ci-speedup/scripts/run_skill_evals.py` (outside this directory on
purpose: it spends real tokens and never ships).

```bash
# from the repository root
python3 maintainers/ci-speedup/scripts/run_skill_evals.py                 # both cases
python3 maintainers/ci-speedup/scripts/run_skill_evals.py --case full-run-synthetic
python3 maintainers/ci-speedup/scripts/run_skill_evals.py --runs 3 --keep  # keep sandboxes, record every session
```

Exit 0 means every scored grader passed; 1 means a case ran and failed one; 2
means the harness could not run, or could not collect the evidence a grader
needed, which is never a pass. A failing session is written to
`skill-eval-logs/` at the repository root (gitignored, never inside this skill),
next to the files it produced.

**Cost.** One real agent session per case per run. Measured when these were
written (2026-10-21): the full run took 35 to 60 seconds and about $0.45, the
routing case about 30 seconds and $0.35.

**When to run it.** Before merging a change to any file that can change what a
session does or sees: `SKILL.md`, `references/`, `scripts/`, these cases,
`tests/verify_report.py` (the self-check the contract runs),
`tests/fixtures/gh_replay/` (the replay corpus), `tests/fixtures/synthetic_corpus.py`
(the checkout and replay builder), the harness
(`maintainers/ci-speedup/scripts/run_skill_evals.py`) or its gh shim
(`maintainers/ci-speedup/scripts/eval_gh_shim.py`), and ci-secure's `SKILL.md`
(the routing case mounts ci-secure and routes on its description).
`.github/workflows/skill-evals.yml` runs it automatically on a pull request that
touches those paths.

## What a pass here does and does not prove

The harness is a port of ci-secure's, with the same gaps against
`claude plugin eval` (the runner these case files are shaped for, which is in
early access): it does not apply `execution.allowed_tools`, does not run the
no-plugin ablation arm, does not honour `execution.model` or `runs:`, and
ignores regex `flags`. Each makes a pass weaker than the case asks for; none
makes a failure wrong.

The reverse gap is specific to this skill: `claude plugin eval` only lets a case
set environment variables named `EVAL_*`, so it cannot set
`CI_SPEEDUP_GH_FIXTURES` or put the shim on PATH. Under it, the full-run case
would audit a repository that does not exist on GitHub. Today the harness is
the only runner that can execute that case.

Two more differences from ci-secure's harness, both deliberate:

- The session loads **no user settings** (`--setting-sources project,local`).
  Otherwise a maintainer laptop with ci-speedup or ci-secure installed offers
  the session two copies of the skill, and a pass can come from the installed
  one rather than the one under test.
- The plugin copy keeps `tests/verify_report.py`, because `SKILL.md` phase 5
  runs it. Everything else under `tests/`, plus `evals/` and `CHANGELOG.md`,
  is withheld: those hold the answer key.

## Static checks (free, every pytest run)

`tests/test_ci_speedup_evals_cases.py` validates the cases without a session:
the schema, that the scaffolds exist and refuse to run anywhere but an empty
directory, that no `trace` or `files` regex matches text the skill ships (a
grader the agent could satisfy by reading the skill instead of running it), and
that every report grader holds on the report the real pipeline renders from the
replay corpus today. The harness's own guards are tested in
`maintainers/ci-speedup/tests/test_ci_speedup_eval_harness.py`.
