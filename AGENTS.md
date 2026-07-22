# AGENTS.md

Instructions for coding agents contributing to `starslingdev/skills`.

`AGENTS.md` is the cross-tool convention (Codex, Cursor, and others read it).
**Claude Code reads [`CLAUDE.md`](CLAUDE.md)** — it carries the same rules in
fuller form. This file is the contributor subset; when they disagree, the
source files below win.

## What this repo is

Public Claude Code skills from StarSling. Today it ships **one** skill,
`ci-speedup`, self-contained under `skills/ci-speedup/`. `ci-speedup` audits a
repository's GitHub Actions workflows for CI optimization opportunities and
produces a prioritized report with measured wall-clock and runner-minute
savings.

**Read [`skills/ci-speedup/SKILL.md`](skills/ci-speedup/SKILL.md) before editing
anything under the skill** — it is the authoritative spec.

## Repository layout

```
skills/ci-speedup/        the installable skill (self-contained)
  SKILL.md                canonical contract — read it first
  CHANGELOG.md            dated, PR-referenced change history
  ARCHITECTURE.md         how the pipeline fits together
  scripts/                the deterministic engine (stdlib only)
  references/             the pattern catalog + methodology docs
  evals/                  eval cases
  tests/                  oracle tests + verify_report.py invariants
docs/methodology.md       public front-door methodology
examples/                 sanitized sample report(s)
maintainers/ci-speedup/   maintainer-only loop infra — OUTSIDE the installable tree
tests/                    repo-level guards (install-surface invariant)
pyproject.toml            pytest config; testpaths span the skill + maintainers/
.github/workflows/        ci.yml (internal), ci-fork.yml (fork PRs)
```

## Running tests

From the repo root:

```bash
python3 -m pytest -v
```

This runs every suite — the root `pyproject.toml` wires the paths so one command
finds tests under `tests/`, `skills/ci-speedup/tests/`, and
`maintainers/ci-speedup/tests/`. **CI runs the identical command**, so green on
your machine means green in CI. Enable the local pre-commit hook to run the same
suite before each commit:

```bash
git config core.hooksPath .githooks
```

## Pull requests

- **Never commit to `main`.** It is protected; branch (e.g. `docs/agents-md`),
  open a PR, and merge on green.
- **The `test` check must pass.** Branch protection requires the full
  `pytest -v` suite before a PR can merge.
- **Fork PRs run on the hosted `ci-fork.yml`** (GitHub-hosted runners, no repo
  secrets). Internal pushes and PRs run on StarSling's own runners (`ci.yml` — we
  dogfood what we sell). Both run the identical suite.

## Changelog discipline

Every change that alters **skill behavior** adds a dated (UTC) bullet to
[`skills/ci-speedup/CHANGELOG.md`](skills/ci-speedup/CHANGELOG.md) under the
right Added / Changed / Fixed heading, **in the same PR**. If you changed the
skill and didn't touch its changelog, the PR is incomplete. Pure-docs or
test-only refactors that don't change behavior can be noted briefly or skipped.

## Staging discipline

**Stage by explicit path.** `git add` only the files you changed this session —
never `git add -A` / `git add .`, which sweeps in unrelated or generated files
(including the local run-data dirs below).

## Never-touch directories

`.ci-speedup-gaps/`, `.ci-speedup-loop/`, and `.ci-speedup-dogfood/` are
gitignored maintainer-loop capture dirs. They hold third-party job logs and
(for the dogfood dir) full repo clones — possibly sensitive internals or tokens.
They are **never committed**, and git **cannot restore them if deleted**. Keep
bulk `git clean` and parallel agents away from the tree while they are
populated.

## Install-surface invariant

The `skills` CLI copies `skills/<name>/` recursively, excluding only
`{.git, __pycache__, __pypackages__}` — there is no dotfile/ignore exclusion. So
anything that must **not** ship to end users (loop prompts, drafting scripts,
the dogfood workflow, runtime capture dirs) lives under
`maintainers/ci-speedup/`, never under `skills/ci-speedup/`.
`tests/test_skill_install_surface.py` makes this a PASS/FAIL invariant — it
fails if maintainer-only infra leaks into the skill dir.

## Contributing detection patterns

The pattern catalog is
[`skills/ci-speedup/references/optimization-patterns.md`](skills/ci-speedup/references/optimization-patterns.md);
each detector is registered under `skills/ci-speedup/scripts/`. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the full flow and
[`skills/ci-speedup/ARCHITECTURE.md`](skills/ci-speedup/ARCHITECTURE.md) for how
the pipeline fits together.
