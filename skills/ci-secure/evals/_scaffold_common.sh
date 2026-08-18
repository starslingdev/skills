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
