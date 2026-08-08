# starslingdev/skills

Public Claude Code skills from StarSling. The repo ships three skills, each in
its own self-contained directory under `skills/`: `ci-speedup` (measured
speed / runner-minute waste), `ci-score` (configuration best practices), and
`ci-secure` (the ten critical CI/CD attack vectors).

## Repository layout

```
skills/
  ci-score/                         # skill: configuration best practices (the CI Score)
  ci-secure/                        # skill: the ten critical CI/CD attack vectors
  ci-speedup/                       # skill: measured speed waste (self-contained)
    SKILL.md                        # the skill's canonical contract — read it first
    CHANGELOG.md                    # dated, PR-referenced change history (keep updated)
    ARCHITECTURE.md                 # how the pipeline fits together
    scripts/                        # the deterministic engine (stdlib only)
    references/                     # the pattern catalog + methodology docs
    evals/                          # eval cases
    tests/                          # oracle tests + verify_report.py invariants
docs/methodology.md                 # public front-door methodology (links into the skill)
examples/                           # sanitized sample report(s)
maintainers/                        # maintainer-only loop infra, OUTSIDE the installable tree
  ci-score/                         #   (ci-score's maintainer-only material)
  ci-speedup/                       #   (the `skills` CLI copies skills/<name>/ recursively, so
    MAINTAINERS.md                  #    keeping this here is the only way to keep it out of installs)
    loops/                          # loop prompts + summary schema (gap→catalog, transcript)
    scripts/                        # draft_detector.py, aggregate_lessons.py, dogfood helpers
    workflows/                      # the automated dogfood loop (ci-speedup-dogfood.js)
    tests/                          # tests for the above
tests/                              # repo-level guard (install-surface invariant)
pyproject.toml                      # pytest config; testpaths span the skill + maintainers/
.github/workflows/ci.yml            # runs `pytest -v` on push / PR to main (ubuntu-latest)
```

## Working in this repo

These rules apply to EVERY shipped skill — `ci-speedup`, `ci-score`,
`ci-secure` — not just the one they were first written for.

- **`skills/<skill>/SKILL.md` is that skill's authoritative spec.** Read it
  before editing anything else under the skill.
- **Every installable skill ships zero maintainer-loop infra.** The `skills`
  CLI copies `skills/<name>/` recursively, excluding only `{.git, __pycache__,
  __pypackages__}` — there is no dotfile/ignore exclusion. So anything that must
  NOT ship to end users (loop prompts, drafting scripts, the dogfood workflow,
  runtime capture dirs) lives under `maintainers/<skill>/`, never under
  `skills/<skill>/`. One install-surface guard per skill under `tests/`
  (`test_skill_install_surface.py` for ci-speedup, `test_ci_score_…` and
  `test_ci_secure_install_surface.py` for the others) makes this a PASS/FAIL
  invariant — each fails if maintainer infra leaks back into its skill dir.
- **Keep the changelog current.** Every change that alters a skill's behavior
  adds a dated (UTC) bullet to THAT skill's `CHANGELOG.md` under the right
  Added / Changed / Fixed heading, *in the same PR*. If you changed a skill and
  didn't touch its changelog, the PR is incomplete. Pure-docs / test-only
  refactors that don't change behavior can be noted briefly or skipped.
- **Tests.** From the repo root, `python3 -m pytest -v` runs every suite (the
  root `pyproject.toml` wires the paths). CI runs the same command; the
  `.githooks/pre-commit` hook runs it locally (`git config core.hooksPath
  .githooks`).

## Opt-in DEBUG logging (off by default)

The skill's scripts call `logger.debug(...)` at every interesting boundary — gh
API calls, per-pattern detector dispatch, rate-governor decisions, etc. These
calls live in the shipped code; whether they surface depends on the runtime log
level, which defaults to quiet. `ci-speedup` follows the `STARSLING_LOG_LEVEL`
convention (referenced in `skills/ci-speedup/scripts/collect_runs.py`) for opting
into DEBUG output; raise the log level to DEBUG when you need a diagnostic trace
of a run. The debug lines record **endpoints, response sizes, and
pattern/workflow names — never gh response bodies**, so secrets returned by gh
API calls won't appear. Workflow file paths and pattern names DO appear; review a
captured log before sharing it externally if your workflow names are sensitive.

## Maintainer self-improvement loops (local, maintainers-only)

`ci-speedup` has local, maintainer-only loops that run on a maintainer's machine
via Claude Code — **never as a GitHub Action** — so private run data can't leak
to CI or fork PRs. Full runbook: `maintainers/ci-speedup/MAINTAINERS.md`.

- **Gap → catalog loop.** When a drilled long pole's job log matches no catalog
  detector, the skill fills the gap with a log-grounded LLM analysis (SKILL.md
  phase 4a) and auto-captures it to the gitignored `.ci-speedup-gaps/` (phase
  4b). From a maintainer **source checkout** it then drafts a deterministic
  `_parse_log` detector + test + `_FIX_META` and asks once before opening a PR
  (phase 4c). Committed infra: `maintainers/ci-speedup/loops/gap-to-catalog-prompt.md`
  + `maintainers/ci-speedup/scripts/draft_detector.py`.
- **Transcript self-improvement loop.** Reads a whole session and turns operator
  steering into durable `SKILL.md` / reference-doc / `evals` edits. Committed
  infra: `maintainers/ci-speedup/loops/loop-analysis-prompt.md` +
  `loop-summary.schema.json`, aggregated by
  `maintainers/ci-speedup/scripts/aggregate_lessons.py`.

**Loop data is precious and never committed.** `.ci-speedup-gaps/`,
`.ci-speedup-loop/`, and `.ci-speedup-dogfood/` (the dogfood loop's third-party
repo clones + run captures) are gitignored. They hold third-party job logs /
transcripts / clones (possible repo internals or tokens), are **never committed**,
and git **cannot restore them if deleted** — keep bulk `git clean` and parallel
agents away from the tree while they are populated. Only the human-reviewed PR the loop proposes
(a detector + test, or a SKILL.md / references / evals edit) is ever committed.

## NEVER

- Never `git add -A` / `git add .`. Stage only the files you touched this
  session, by explicit path — a bulk add sweeps in unrelated or generated files.
- Never commit to `main` directly. `main` is protected; branch protection
  requires the `test` check (full `pytest -v`) to pass — open a PR.
- Never commit `.ci-speedup-gaps/`, `.ci-speedup-loop/`, or `.ci-speedup-dogfood/`
  (gitignored, maintainer-local, may contain secrets; unrecoverable if deleted).
