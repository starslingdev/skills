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
.github/workflows/        ci.yml (self-hosted + fork suites, `test` verdict)
```

## Running tests

From the repo root:

```bash
pip install -e ".[dev]"    # pytest + PyYAML + pytest-xdist
python3 -m pytest -v
```

This runs every suite — the root `pyproject.toml` wires the paths so one command
finds tests under `tests/`, `skills/ci-speedup/tests/`, and
`maintainers/ci-speedup/tests/`. CI runs the same full suite with four workers:
`python3 -m pytest -v -n 4 --dist worksteal`. Use that command to reproduce
parallel execution locally; the command above runs sequentially.
Without PyYAML, pytest aborts at collection with `INTERNALERROR` and runs nothing.
Enable the local pre-commit hook to run the same suite before each commit:

```bash
git config core.hooksPath .githooks
```

## Pull requests

- **Never commit to `main`.** It is protected; branch (e.g. `docs/agents-md`),
  open a PR, and merge on green.
- **The `test` check must pass.** Branch protection requires the full
  `pytest -v` suite before a PR can merge. `test` is the always-running verdict
  job in `ci.yml`; it passes only when the suite that should have run did run
  and passed, so a skipped suite can never read as green.
- **Fork PRs run the `test (fork)` job** (GitHub-hosted runners, no repo
  secrets). Internal pushes and PRs run the `test (self-hosted)` job on
  StarSling's own runners (we dogfood what we sell). Both live in `ci.yml` and
  run the identical suite.

## Public-output privacy gate

This is a public repository. Treat every repository name, URL, issue number,
PR number, branch name, and other identifier learned from user-provided or
internal context as confidential unless its publication is explicitly approved.
A link supplied for research is not permission to republish it.

Before creating or updating any public GitHub artifact (commit, branch, PR,
issue, review, comment, release, or changelog):

1. Verify the destination repository is `PUBLIC`.
2. Default to naming only the destination repository. Cross-repository
   provenance must be described generically (for example, “internal review”).
3. Inspect the exact outbound title/body, commit messages, and added diff lines.
   Run the fail-closed preflight when preparing a PR:

   ```bash
   python3 .github/scripts/check_public_output.py \
     --target starslingdev/skills \
     --text-file /tmp/pr-title.txt \
     --text-file /tmp/pr-body.md \
     --base origin/main
   ```

4. After publishing, fetch the artifact back and scan the returned content too.

Any cross-repository reference requires the user's explicit approval to publish
that exact reference and an immediate verification that the referenced
repository is public. If either condition is missing or uncertain, stop and
remove or neutralize the reference. Never use a public artifact as a breadcrumb
to private context.

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
