"""Repo-wide pytest guards: git-environment isolation + a repo-state tripwire.

Incident, 2026-07-30: the pre-commit hook ran this suite from a LINKED
WORKTREE. In worktrees, git exports its repo-addressing environment
(GIT_DIR, GIT_INDEX_FILE, ...) to hooks as ABSOLUTE paths; every
`subprocess.run(["git", ...])` in every test inherited it, so a fixture
test's `git init/add/commit` sequence executed against the REAL
repository's metadata instead of its pytest tmp_path — re-initializing
the developer checkout as bare and walking `main` onto a fixture commit.
(Normal checkouts were never bitten: there the addressing is relative and
resolves harmlessly inside each test's cwd.)

Guard 1 — scrub the addressing env at import time, before any test runs:
no git subprocess anywhere in the suite can be redirected at a repo it
did not name itself.

Guard 2 — tripwire: record the repo's HEAD / branch / bareness before
the first test and verify them after the last. If any test mutates the
real repo again — by this vector or a new one — the suite fails loudly
at session end instead of leaving silent corruption.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent

# Guard 1: at import time — before collection, before any test subprocess.
for _var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
             "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
             "GIT_NAMESPACE"):
    os.environ.pop(_var, None)


def _repo_state() -> dict | None:
    """(HEAD sha, symbolic branch, core.bare) of THIS repo — None outside git."""
    def probe(*args: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(_REPO_ROOT), *args],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    head = probe("rev-parse", "HEAD")
    if head is None:
        return None
    return {"head": head,
            "branch": probe("symbolic-ref", "-q", "HEAD") or "(detached)",
            "bare": probe("config", "core.bare") or "false"}


def pytest_sessionstart(session):
    session.config._repo_guard_state = _repo_state()


def pytest_sessionfinish(session, exitstatus):
    before = getattr(session.config, "_repo_guard_state", None)
    if before is None:
        return
    after = _repo_state()
    if after != before:
        raise pytest.UsageError(
            "REPO-STATE TRIPWIRE: a test mutated this repository's git state "
            f"during the run — before={before} after={after}. No test may touch "
            "the real repo (2026-07-30 incident: a fixture repo's git commands "
            "escaped via inherited GIT_DIR and rewrote the developer checkout). "
            "Find the test whose git subprocess is not confined to its tmp_path."
        )
