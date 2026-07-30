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

Scope of Guard 2: it watches exactly the three invariants a hijacked repo
violates (it gets re-pointed, re-branched, or flipped bare) — the incident's
signature. It is a deliberately narrow backstop, NOT a working-tree audit:
Guard 1 is the actual prevention, and a status/index snapshot would
false-positive on the ``__pycache__`` the suite itself writes. A net HEAD
move from any source trips it — including a developer committing in this
checkout while the ~70s suite runs; that loud stop is intended. If the git
state can't be read at start (or re-read at end), the tripwire says so
loudly (a warning to stderr) rather than passing — or falsely alarming —
in silence.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent

# Guard 1: at import time — before collection, before any test subprocess.
for _var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
             "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
             "GIT_NAMESPACE"):
    os.environ.pop(_var, None)


def _guard_warn(msg: str) -> None:
    """Loud, non-fatal notice to stderr — used when the tripwire cannot do its
    job so 'guard inactive/inconclusive' is never mistaken for 'guard passed'."""
    print(f"REPO-STATE TRIPWIRE: {msg}", file=sys.stderr, flush=True)


def _repo_state() -> dict[str, str] | None:
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
    state = _repo_state()
    session.config._repo_guard_state = state
    if state is None:
        # No baseline (git missing/timeout, or not a git repo) => nothing to
        # compare against at session end. Fail-open is unavoidable here, but it
        # must not be SILENT: announce that the tripwire is inactive for this run.
        _guard_warn(
            "INACTIVE — could not read this repo's git state at session start; "
            "repo corruption during this run will NOT be detected."
        )


def pytest_sessionfinish(session, exitstatus):
    before = getattr(session.config, "_repo_guard_state", None)
    if before is None:
        return  # already announced at session start; nothing to compare against
    after = _repo_state()
    if after is None:
        # Re-read failed at session end. That is INCONCLUSIVE (a transient git
        # hiccup), not proof of mutation — do not cry corruption. Say so loudly
        # instead of raising a misleading "a test mutated this repo" tripwire.
        _guard_warn(
            "INCONCLUSIVE — could not re-read this repo's git state at session "
            f"end (baseline was {before}); drift could not be verified."
        )
        return
    if after != before:
        raise pytest.UsageError(
            "REPO-STATE TRIPWIRE: a test mutated this repository's git state "
            f"during the run — before={before} after={after}. No test may touch "
            "the real repo (2026-07-30 incident: a fixture repo's git commands "
            "escaped via inherited GIT_DIR and rewrote the developer checkout). "
            "Find the test whose git subprocess is not confined to its tmp_path."
        )
