#!/usr/bin/env bash
# Shared body of every case's `scaffold.sh`. Sourced, never run directly.
#
# Materialises the synthetic repository the offline replay corpus
# (`tests/fixtures/gh_replay/`) was recorded against into the eval sandbox's
# working directory: its workflow YAML, the package/ESLint files the detectors
# read, one commit on `main`, and an `origin` remote naming `synthetic/repo`.
# The builder is `tests/fixtures/synthetic_corpus.py`, the same one
# `tests/test_offline_pipeline_e2e.py` uses, so the checkout an agent audits is
# the one the e2e proves renders a report `verify_report.py` accepts.
#
# The `origin` remote is part of the model, not decoration: `collect_runs.py`
# reads workflow YAML off disk only after it has verified the checkout is a
# clone of `--repo`.
#
# Sandbox facts this relies on (`claude plugin eval`'s scaffold contract, which
# `maintainers/ci-speedup/scripts/run_skill_evals.py` mirrors): `bash <script>`
# with cwd = the agent's empty working directory, a minimal environment (PATH,
# HOME, TMPDIR, TERM, GIT_CONFIG_NOSYSTEM=1), no credentials, a 2-minute limit.
set -euo pipefail

# REFUSE ANY WORKING DIRECTORY THAT IS NOT AN EMPTY EVAL SANDBOX.
#
# What follows runs `git init`, writes `.github/workflows/` and commits. Correct
# in the empty sandbox the harness hands it; destructive in a real checkout,
# where `git init` is a silent re-init and the commit lands on the current
# branch. The test is emptiness (the same guard ci-secure's scaffold uses, for
# the same reasons), and the listing's own exit status is checked: an `ls` that
# errors prints nothing, and "no output" must not read as "empty".
if ! contents="$(ls -A . 2>/dev/null)"; then
  echo "scaffold: refusing to run — cannot list '$PWD' to confirm it is an" >&2
  echo "scaffold: empty sandbox. A directory this script cannot read is not a" >&2
  echo "scaffold: directory it may overwrite." >&2
  exit 1
fi
if [ -n "$contents" ]; then
  echo "scaffold: refusing to run — '$PWD' is not empty." >&2
  echo "scaffold: this script writes and COMMITS a synthetic .github/workflows/" >&2
  echo "scaffold: tree, which is only ever safe in an empty eval sandbox. Run the" >&2
  echo "scaffold: suite through maintainers/ci-speedup/scripts/run_skill_evals.py" >&2
  echo "scaffold: (see evals/README.md), or cd to an empty directory first." >&2
  exit 1
fi

evals_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
builder="$evals_root/../tests/fixtures/synthetic_corpus.py"

if [ ! -f "$builder" ]; then
  echo "scaffold: no synthetic repo builder at $builder" >&2
  exit 1
fi

# The builder's git calls carry their own author/committer identity, so this
# works on a runner with no global git config.
python3 "$builder" repo . >/dev/null

if [ ! -f .github/workflows/ci.yml ] || ! git rev-parse --verify -q HEAD >/dev/null; then
  echo "scaffold: the synthetic checkout was not materialised" >&2
  exit 1
fi

echo "scaffold: materialised $(ls .github/workflows | wc -l | tr -d ' ') workflow file(s) for synthetic/repo"
