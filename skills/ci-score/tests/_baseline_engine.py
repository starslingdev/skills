"""The fact engine as it stood before OD-CS23, for "no score can drop" checks.

OD-CS23 claims every verdict it changes moves toward credit. That claim is
enforced, not stated: the pre-change engine is read out of git history at the
pinned commit below (the `main` this change branched from, registry
`ci-score-v0.1.4`) and run beside the current one on the same input.

Why git history rather than a vendored copy: a copy of a 1,400-line engine
inside the installable skill would ship to every user and could drift from
the commit it claims to be. CI checks out full history (`fetch-depth: 0`), so
the commit is always there; a checkout inside this repository that lacks it
fails loudly. An installed copy of the skill (no repository around it) has no
history to compare against and returns None.
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

# The only moves allowed relative to the base engine: toward credit.
TOWARD_CREDIT = {("fail", "pass"), ("fail", "not_applicable"), ("not_applicable", "pass")}

_cached: list[Any] = []


def _git(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(_SKILL_DIR), *args],
                          capture_output=True, text=True, env=env)


def baseline_engine() -> Any:
    """The base commit's `practice_facts` module, or None in an installed copy."""
    if _cached:
        return _cached[0]
    mod = None
    top = _git("rev-parse", "--show-toplevel")
    here = _SKILL_DIR / "scripts" / "practice_facts.py"
    if top.returncode == 0 and Path(top.stdout.strip()).resolve() / _REL == here:
        shown = _git("show", f"{BASE_COMMIT}:{_REL}")
        if shown.returncode != 0:
            raise AssertionError(
                f"base engine {BASE_COMMIT[:12]} is not in this checkout's history "
                f"(fetch full history): {shown.stderr.strip()}")
        path = Path(tempfile.mkdtemp(prefix="ci-score-base-")) / "practice_facts_base.py"
        path.write_text(shown.stdout, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("ci_score_practice_facts_base", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ci_score_practice_facts_base"] = mod
        spec.loader.exec_module(mod)
    _cached.append(mod)
    return mod


def assert_toward_credit(before: dict, after: dict, where: str = "") -> None:
    """Every fact the base engine graded is either unchanged or moved toward
    credit. A pass that turns into a fail or not_applicable, or a
    not_applicable that turns into a fail, is a score drop."""
    for check, old in before.items():
        new = after[check]
        move = (old["state"], new["state"])
        assert move[0] == move[1] or move in TOWARD_CREDIT, (
            f"{where}{check} moved {move[0]} -> {move[1]} vs the base engine: "
            f"{old['evidence']!r} -> {new['evidence']!r}")
