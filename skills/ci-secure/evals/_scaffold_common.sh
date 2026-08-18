#!/usr/bin/env bash
# Shared body of every case's `scaffold.sh`. Sourced, never run directly.
#
# The caller sets FIXTURE_SLUG and sources this file. It materializes
# `evals/files/$FIXTURE_SLUG/dot-github/workflows/*.yml.fixture` into a real
# `.github/workflows/` tree in the eval sandbox's working directory, then makes
# that directory its own git repository.
#
# Why the fixtures are stored cloaked in the first place: they are deliberately
# vulnerable workflow YAML. Tracked at a literal `.github/workflows/` path they
# would read to a registry scanner as this repository's own live automation --
# `tests/test_ci_secure_install_surface.py::test_no_tracked_workflow_shaped_fixture_paths`
# makes that a hard failure. So the tracked form is `dot-github/*.yml.fixture`
# and every consumer un-cloaks it at runtime: pytest via
# `skills/ci-secure/tests/conftest.py`, the eval harness via this script.
#
# Sandbox facts this relies on (from `claude plugin eval`'s own documentation):
#   - the script runs as `bash <script>` with cwd = the agent's empty working
#     directory, a minimal environment (PATH, HOME, TMPDIR, TERM,
#     GIT_CONFIG_NOSYSTEM=1), no credentials, and a 2-minute hard limit;
#   - case resources are addressed relative to the script itself;
#   - a failing scaffold scores the run 0 and keeps the sandbox for debugging,
#     which is why every step below fails loudly instead of leaving a silently
#     empty workspace that would grade as "no workflows to scan".
set -euo pipefail

: "${FIXTURE_SLUG:?scaffold: the calling scaffold.sh must set FIXTURE_SLUG}"

# REFUSE ANY WORKING DIRECTORY THAT IS NOT AN EMPTY EVAL SANDBOX.
#
# What this script does next -- overwrite `.github/workflows/` with deliberately
# vulnerable YAML and commit it -- is correct in the sandbox the harness hands
# it and destructive everywhere else. Run by hand from a real checkout (the
# obvious thing to do while authoring a case) it replaces that repository's live
# CI workflow with a `pull_request_target` template-injection workflow and
# commits the result to the current branch: the exact tracked-vulnerable-
# workflow condition the `dot-github/*.yml.fixture` cloak exists to prevent.
#
# Nothing downstream notices on its own. `git init` on an existing repository is
# a silent re-init, `cp` overwrites without complaint, and
# `tests/test_ci_secure_install_surface.py` only inspects paths under
# `skills/ci-secure/`, so a repository-root clobber is invisible to it. The
# refusal has to happen here, before the first `cp`.
# The test is EMPTINESS, not "am I inside a git repository". The harness's
# sandbox working directory already sits inside a repository one level up (see
# the `git init` note below), so a `git rev-parse --is-inside-work-tree` guard
# would refuse every legitimate run. An empty directory is the one condition
# that is both true of the sandbox and false of any checkout worth protecting.
#
# The listing's exit status is checked, not just its output. An `ls` that errors
# prints nothing, and "no output" read as "the directory is empty" would turn
# this guard into a no-op in exactly the case where it cannot see what it is
# about to overwrite. Unknown is not empty — the same rule ci-secure applies to
# a check that could not run.
if ! contents="$(ls -A . 2>/dev/null)"; then
  echo "scaffold: refusing to run — cannot list '$PWD' to confirm it is an" >&2
  echo "scaffold: empty sandbox. A directory this script cannot read is not a" >&2
  echo "scaffold: directory it may overwrite." >&2
  exit 1
fi
if [ -n "$contents" ]; then
  echo "scaffold: refusing to run — '$PWD' is not empty." >&2
  echo "scaffold: this script overwrites and COMMITS .github/workflows/ with" >&2
  echo "scaffold: intentionally vulnerable workflow fixtures, which is only" >&2
  echo "scaffold: ever safe in an empty eval sandbox. Run the suite through" >&2
  echo "scaffold: 'claude plugin eval --scaffold' (see evals/README.md), or cd" >&2
  echo "scaffold: to an empty directory first." >&2
  exit 1
fi

evals_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$evals_root/files/$FIXTURE_SLUG/dot-github/workflows"

if [ ! -d "$src" ]; then
  echo "scaffold: no cloaked fixture workflows at $src" >&2
  exit 1
fi

mkdir -p .github/workflows

count=0
for fixture in "$src"/*.yml.fixture; do
  if [ ! -e "$fixture" ]; then
    echo "scaffold: $src contains no *.yml.fixture files" >&2
    exit 1
  fi
  base="$(basename "$fixture")"
  cp "$fixture" ".github/workflows/${base%.fixture}"
  count=$((count + 1))
done

# ci-secure Phase 1 resolves the tree to scan with `git rev-parse
# --show-toplevel`. The sandbox's working directory already sits INSIDE an
# empty repository one level up (the harness puts `.git` in the sandbox HOME so
# that upward git walks stop there), so without a repository of its own the
# skill would resolve the root to the sandbox home -- a directory with no
# `.github/workflows/` in it -- and the scan would find nothing to do.
git init -q .
git add .github/workflows
git -c user.email=evals@ci-secure.invalid -c user.name='ci-secure evals' \
    commit -q -m 'workflows under audit'

echo "scaffold: materialized $count workflow file(s) from fixture '$FIXTURE_SLUG'"
