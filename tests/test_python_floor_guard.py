"""The floor guard must be able to fail, and must not fire on healthy code.

`.github/scripts/check_python_floor.py` imports every shipped module on the
oldest Python this repo supports. Two ways for it to be worthless, and both
have already happened once:

- **It passes over a real break.** The AST guard beside it does exactly that
  for anything that parses on 3.9 and then fails when the module body runs —
  a possessive quantifier in a compiled regex, a union evaluated for real.
  That whole class shipped undetected until someone read the code by hand.
- **It fails on healthy code.** The first version of the probe did not register
  the module in `sys.modules` before executing it, so every module using
  `@dataclass` died with `AttributeError: 'NoneType' object has no attribute
  '__dict__'`. Three healthy modules were reported broken. A guard that cries
  wolf gets switched off, and then it protects nothing.

So both directions are pinned here, against fixture trees rather than against
the repository, so the assertions are about the GUARD and not about whatever
the shipped code happens to be today.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_GUARD = _REPO / ".github" / "scripts" / "check_python_floor.py"


def _tree(root: Path, name: str, body: str) -> Path:
    """A fixture laid out like a shipped skill: skills/<name>/scripts/*.py."""
    d = root / "skills" / name / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "mod.py").write_text(body, encoding="utf-8")
    return root


def _run(root: Path):
    return subprocess.run(
        [sys.executable, str(_GUARD), "--root", str(root)],
        capture_output=True, text=True)


def test_the_guard_exists_and_is_wired_into_ci() -> None:
    """A guard nothing invokes is a file, not a check."""
    assert _GUARD.is_file(), f"{_GUARD} is missing"
    workflows = (_REPO / ".github" / "workflows").glob("*.yml")
    invoked = [w.name for w in workflows
               if "check_python_floor.py" in w.read_text(encoding="utf-8")]
    assert invoked, ("no workflow runs check_python_floor.py, so nothing "
                     "checks the floor on a version CI actually installs")


def test_healthy_code_passes() -> None:
    """The false-positive direction. This is what broke on the first attempt."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(Path(tmp), "healthy", (
            "from __future__ import annotations\n"
            "from dataclasses import dataclass\n"
            "\n"
            "@dataclass\n"
            "class Thing:\n"
            "    name: str\n"
            "    count: int = 0\n"
        ))
        proc = _run(root)
    assert proc.returncode == 0, (
        "the guard reported healthy code as broken:\n" + proc.stdout + proc.stderr)


def test_a_runtime_import_failure_is_caught() -> None:
    """The direction the AST guard cannot cover, and the one that shipped.

    This module PARSES on every version. It fails when the body executes,
    which is precisely why only a real import on the floor finds it.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(Path(tmp), "broken", (
            "import re\n"
            # Possessive quantifier: valid syntax everywhere, re.error below 3.11.
            "PATTERN = re.compile(r'(?P<c>-)(?P=c)++')\n"
        ))
        proc = _run(root)

    if sys.version_info >= (3, 11):
        # On a modern interpreter this pattern is legal, so there is nothing to
        # catch — and saying so is more honest than asserting a pass that means
        # nothing. CI runs this guard on the floor, where the assertion bites.
        assert proc.returncode == 0
        return
    assert proc.returncode == 1, (
        "a module that fails at import time was reported as fine:\n" + proc.stdout)
    assert "mod.py" in proc.stdout
    assert "::error" in proc.stdout


def test_an_empty_tree_is_a_failure_not_a_pass() -> None:
    """Zero modules checked must never read as zero problems found."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        proc = _run(Path(tmp))
    assert proc.returncode == 1
    assert "checking nothing" in proc.stdout
