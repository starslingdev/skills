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

This repository is public. Links and internal context supplied for a task are
inputs, not permission to publish them: treat repository names, URLs, issue/PR
numbers, and other source identifiers as confidential unless publishing the
exact reference is explicitly approved.

Before every public GitHub write (commit, branch, PR, issue, review, comment,
release, or changelog):

1. Verify the destination repository is `PUBLIC`.
2. Name only the destination repository. Describe private provenance
   generically ("internal design", "internal review"). Keep private
   identifiers out of branch names too; the gate does not scan them.
3. Commit first, then scan the exact outbound title/body, every outgoing
   commit's message and added lines, and the net added diff. Only committed
   history is scanned. Write the PR text to fresh per-run files:

   ```bash
   title="$(mktemp)"; body="$(mktemp)"
   # write the exact PR title to "$title" and body to "$body", then:
   python3 .github/scripts/check_public_output.py --target starslingdev/skills \
     --text-file "$title" --text-file "$body" --base origin/main
   ```

   Exit 0 passes. Exit 1 blocks and prints only locations
   (`commit <sha> <path>:<line>`, `commit <sha> message:<line>`,
   `added diff:<path>:<line>`, `text file <n>:<line>`), never the identifier.
   Exit 2 is a fail-closed error.
4. Fetch the published artifact back and scan its returned title/body.

CI runs the same gate on every pull request inside both suite jobs, so it gates
the required `test` check. A title/body edit is re-checked only on the next
push, so scan edits locally first.

**Fixture paths.** The diff scan alone skips added lines under `tests/`,
`skills/*/tests/`, `skills/*/evals/`, and `maintainers/*/tests/`, where
synthetic `owner/repo` slugs are routine. Commit messages and PR text stay
strict. Never put a real private reference in a fixture.

**Exceptions.** A cross-repository reference is allowed only when the user
explicitly approves publishing that exact reference. Pass `--allow owner/repo`
(repeatable); the gate fails closed unless that repository verifies `PUBLIC`.
If approval or visibility is uncertain, remove or neutralize the reference.
Never expose private context to explain provenance, credit, motivation, or
review history.

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
