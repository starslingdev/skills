"""The fact engine as it stood before OD-CS23, for "no score can drop" checks.

OD-CS23 claims every verdict it changes moves toward credit. That claim is
checked, not only stated: the pre-change engine is read out of git history at
the pinned commit below (a `main` commit whose ci-score is the pre-change
engine, registry `ci-score-v0.1.4`) and run beside the current one on the same
input, for the two frozen calibration controls and an explicit list of
OD-CS23 cases (`test_ci_score_no_score_drops.py`).

Why git history rather than a vendored copy: a copy of a 1,400-line engine
inside the installable skill would ship to every user and could drift from
the commit it claims to be. CI checks out full history (`fetch-depth: 0`), so
the commit is always there. A checkout of this repository that cannot read
it fails loudly with git's own error; only a copy with no repository around
it (an installed skill) has nothing to compare against, and the callers skip
visibly there.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

BASE_COMMIT = "369ba9893f7251973654771e315da11efe38a541"
_REL = "skills/ci-score/scripts/practice_facts.py"
_SKILL_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SKILL_DIR.parents[1]

# The only moves allowed relative to the base engine: toward credit.
TOWARD_CREDIT = {("fail", "pass"), ("fail", "not_applicable"), ("not_applicable", "pass")}

_cached: list[Any] = []


def _git(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(_REPO_ROOT), *args],
                          capture_output=True, text=True, env=env)


def _adapt(mod: Any) -> Any:
    """Let the old engine take a newer collector's calls: extra positional or
    keyword arguments to `_practice_facts` (a later collector may pass, for
    example, `parse_errors`) are dropped, and `_unresolved_setup_defaults`,
    which OD-CS23 added, reads as "nothing recorded"."""
    facts = mod._practice_facts

    def _practice_facts(parsed: Any, root: Any, *_args: Any, **_kwargs: Any) -> Any:
        return facts(parsed, root)

    mod._practice_facts = _practice_facts
    if not hasattr(mod, "_unresolved_setup_defaults"):
        mod._unresolved_setup_defaults = lambda *_a, **_k: []
    return mod


def baseline_engine() -> Any:
    """The base commit's `practice_facts` module. None only when there is no
    repository at all (an installed copy of the skill). Inside a repository,
    a base commit that cannot be read raises with git's stderr: never None,
    so a shallow clone cannot quietly turn the comparison off."""
    if _cached:
        return _cached[0]
    mod = None
    if (_REPO_ROOT / ".git").exists():
        try:
            shown = _git("show", f"{BASE_COMMIT}:{_REL}")
        except OSError as exc:   # no git binary
            raise AssertionError(f"base engine {BASE_COMMIT[:12]}: cannot run git: {exc}")
        if shown.returncode != 0:
            raise AssertionError(
                f"base engine {BASE_COMMIT[:12]} cannot be read from this checkout "
                f"(fetch full history): {shown.stderr.strip()}")
        path = Path(tempfile.mkdtemp(prefix="ci-score-base-")) / "practice_facts_base.py"
        path.write_text(shown.stdout, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("ci_score_practice_facts_base", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ci_score_practice_facts_base"] = mod
        spec.loader.exec_module(mod)
        mod = _adapt(mod)
    _cached.append(mod)
    return mod


def moves_away_from_credit(before: dict, after: dict) -> list[tuple[str, str, str]]:
    """Every fact the base engine graded that moved away from credit, as
    `(check, old_state, new_state)`. A pass that turns into a fail or
    not_applicable, or a not_applicable that turns into a fail, is a drop."""
    out = []
    for check, old in before.items():
        move = (old["state"], after[check]["state"])
        if move[0] != move[1] and move not in TOWARD_CREDIT:
            out.append((check, *move))
    return out


def assert_toward_credit(before: dict, after: dict, where: str = "",
                         allowed: frozenset = frozenset()) -> None:
    """No fact moved away from credit, except the checks in `allowed`."""
    drops = [d for d in moves_away_from_credit(before, after) if d[0] not in allowed]
    assert not drops, "; ".join(
        f"{where}{check} moved {old} -> {new} vs the base engine: "
        f"{before[check]['evidence']!r} -> {after[check]['evidence']!r}"
        for check, old, new in drops)
