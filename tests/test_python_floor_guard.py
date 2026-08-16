"""The floor guard must be able to fail, and must not fire on healthy code.

`.github/scripts/check_python_floor.py` imports every shipped module on the
oldest Python this repo supports. Two ways for it to be worthless, and both
have already happened once:

- **It passes over a real break.** The static guard at
  `skills/ci-secure/tests/test_python_compat.py` cannot see anything that
  parses on 3.9 and then fails when the module body runs — a possessive
  quantifier in a compiled regex, a union evaluated for real.
- **It fails on healthy code.** An early version of the probe did not register
  the module in `sys.modules` before executing it, so every module using
  `@dataclass` died with `AttributeError: 'NoneType' object has no attribute
  '__dict__'`. Healthy modules were reported broken. A guard that cries wolf
  gets switched off, and then it protects nothing.

So both directions are pinned here, against fixture trees rather than against
the repository, so the assertions are about the GUARD and not about whatever
the shipped code happens to be today.

Every fixture that must be caught fails on EVERY Python version. This suite
runs on the version the rest of CI pins, so a fixture that only breaks below
3.11 would leave the guard's whole failure path asserted by nothing where it
actually runs. The floor-specific cases are marked and skipped instead.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_GUARD = _REPO / ".github" / "scripts" / "check_python_floor.py"


def _tree(root: Path, name: str, body: str) -> Path:
    """A fixture laid out like a shipped skill: skills/<name>/scripts/*.py."""
    d = root / "skills" / name / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "mod.py").write_text(body, encoding="utf-8")
    return root


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_GUARD), "--root", str(root)],
        capture_output=True, text=True)


def test_a_module_in_a_subdirectory_is_still_checked(tmp_path: Path) -> None:
    """A skill may organise its engine into subdirectories, and all of it ships.

    The whole of `skills/<name>/` is copied to a user's machine, so a module
    at `scripts/helpers/thing.py` reaches them exactly as one at
    `scripts/thing.py` does. A discovery pattern that only looks one level
    deep would walk straight past it and report a clean run — the guard
    passing while the broken file is the one it never opened.

    The fixture deliberately holds a healthy top-level module too. Without
    it a subdirectory-only tree would find nothing at all and fail for the
    unrelated "checking nothing" reason, which would pass this test for
    entirely the wrong cause.
    """
    root = _tree(tmp_path, "nested", "VALUE = 1\n")
    deep = root / "skills" / "nested" / "scripts" / "helpers"
    deep.mkdir(parents=True, exist_ok=True)
    # Fails on EVERY Python, so this stays a real assertion on the 3.12
    # interpreter the test suite runs under, not only on the floor.
    (deep / "deep.py").write_text(
        "raise RuntimeError('deep module is broken')\n", encoding="utf-8")
    proc = _run(root)

    assert proc.returncode == 1, (
        "a broken module in a scripts/ subdirectory was never opened:\n"
        + proc.stdout + proc.stderr)
    assert "deep.py" in proc.stdout


def test_the_guard_exists_and_is_wired_into_ci() -> None:
    """A guard nothing invokes is a file, not a check.

    The invocation has to be a real `run:` step. Matching the filename
    anywhere in the file would accept a workflow that only mentions the
    script in a comment — which is precisely the state this test exists to
    rule out, and it would certify the guard as wired while it was deleted.
    """
    assert _GUARD.is_file(), f"{_GUARD} is missing"
    workflow_dir = _REPO / ".github" / "workflows"
    # Both spellings: a rename to `.yaml` is still a workflow GitHub runs, and
    # failing this test for that alone would be a red with the wrong reason.
    workflows = list(workflow_dir.glob("*.yml")) + list(workflow_dir.glob("*.yaml"))
    invoked = [w.name for w in workflows
               if any(not line.lstrip().startswith("#")
                      and "check_python_floor.py" in line
                      for line in w.read_text(encoding="utf-8").splitlines())]
    assert invoked, ("no workflow actually runs check_python_floor.py, so "
                     "nothing checks the floor on a version CI installs")


def test_healthy_code_passes(tmp_path: Path) -> None:
    """The false-positive direction. This is what broke on the first attempt."""
    root = _tree(tmp_path, "healthy", (
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


def test_a_module_that_exits_early_is_not_reported_clean(tmp_path: Path) -> None:
    """Exiting zero part-way through an import is not the same as importing.

    Several shipped modules call `sys.exit(1)` at module scope when PyYAML is
    missing, so a module-level exit is an idiom here and one keystroke from a
    zero. Treating the return code as the whole verdict would mark such a
    module ok while everything below the exit — where a floor break would
    live — went unread.
    """
    root = _tree(tmp_path, "bails", (
        "import re, sys\n"
        "sys.exit(0)\n"
        "PATTERN = re.compile(r'(?P<c>-)(?P=c)++')\n"
    ))
    proc = _run(root)
    assert proc.returncode == 1, (
        "a module that exited before finishing its import was called ok:\n"
        + proc.stdout)
    assert "::error file=skills/bails/scripts/mod.py::" in proc.stdout, proc.stdout


def test_a_runtime_import_failure_is_caught(tmp_path: Path) -> None:
    """The direction the AST guard cannot cover — and it must bite everywhere.

    The fixture parses on every version and fails when the body executes,
    which is the whole class only a real import can find. Deliberately NOT a
    version-specific break: this suite runs on 3.12, so a fixture that is only
    broken below 3.11 would leave the guard's entire failure path — the exit
    code, the annotation, the reason — asserted by nothing on the interpreter
    that actually runs it. A guard reporting no failure it ever finds would
    have passed the earlier version of this test.
    """
    root = _tree(tmp_path, "broken", "raise ImportError('simulated floor break')\n")
    proc = _run(root)

    assert proc.returncode == 1, (
        "a module that fails at import time was reported as fine:\n" + proc.stdout)
    # The annotation form GitHub renders against the offending file, not a
    # loose substring: a bare `FAIL` line leaves the reader hunting.
    assert "::error file=skills/broken/scripts/mod.py::" in proc.stdout, proc.stdout
    # The reason has to survive as far as the report, or the red is unactionable.
    assert "simulated floor break" in proc.stdout, proc.stdout


@pytest.mark.skipif(sys.version_info >= (3, 11),
                    reason="possessive quantifiers are legal here; this bites "
                           "on the 3.9 floor the CI job runs")
def test_the_real_break_from_the_floor_is_caught(tmp_path: Path) -> None:
    """The actual shipped regex that started this, kept as a floor-only case."""
    root = _tree(tmp_path, "possessive", (
        "import re\n"
        "PATTERN = re.compile(r'(?P<c>-)(?P=c)++')\n"
    ))
    proc = _run(root)
    assert proc.returncode == 1, proc.stdout
    assert "::error file=skills/possessive/scripts/mod.py::" in proc.stdout


def test_a_module_importing_its_sibling_still_passes(tmp_path: Path) -> None:
    """Shipped scripts import each other by bare name, and must keep working.

    Several engines do `from config import ...` against a file beside them,
    which resolves only because the probe puts the module's own directory on
    `sys.path` first — exactly as an installed skill does. Drop that and the
    guard reports healthy code as broken, which is the cry-wolf failure this
    suite exists to prevent.
    """
    root = _tree(tmp_path, "siblings", "from helper import VALUE\n")
    (root / "skills" / "siblings" / "scripts" / "helper.py").write_text(
        "VALUE = 1\n", encoding="utf-8")
    proc = _run(root)
    assert proc.returncode == 0, (
        "a module importing its sibling was reported as broken:\n"
        + proc.stdout + proc.stderr)


def test_one_bad_module_does_not_hide_the_others(tmp_path: Path) -> None:
    """Every broken module in one pass, not the first one alphabetically.

    This is the reason each import gets its own subprocess. Someone
    refactoring the loop to stop at the first failure would send a
    contributor round the same red job once per broken file.
    """
    root = _tree(tmp_path, "aaa", "raise ImportError('first one')\n")
    _tree(root, "mmm", "raise ImportError('second one')\n")
    _tree(root, "zzz", "VALUE = 1\n")
    proc = _run(root)

    assert proc.returncode == 1, proc.stdout
    assert "::error file=skills/aaa/scripts/mod.py::" in proc.stdout, proc.stdout
    assert "::error file=skills/mmm/scripts/mod.py::" in proc.stdout, proc.stdout
    assert "ok    skills/zzz/scripts/mod.py" in proc.stdout, proc.stdout


def test_a_module_that_hangs_is_reported_not_left_to_run(tmp_path: Path) -> None:
    """A module that never finishes importing is as broken as one that raises.

    It must be named like any other failure. Letting the wait escape would
    throw away the modules already checked and print a stack trace where the
    per-file annotation belongs.
    """
    root = _tree(tmp_path, "aaa-hangs", "import time\ntime.sleep(600)\n")
    _tree(root, "zzz", "VALUE = 1\n")
    proc = subprocess.run(
        [sys.executable, str(_GUARD), "--root", str(root), "--timeout", "2"],
        capture_output=True, text=True)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "::error file=skills/aaa-hangs/scripts/mod.py::" in proc.stdout, proc.stdout
    assert "Traceback" not in proc.stderr, proc.stderr
    # The healthy module after it in the run order still got its verdict.
    assert "ok    skills/zzz/scripts/mod.py" in proc.stdout, proc.stdout


def test_a_module_reading_stdin_fails_fast(tmp_path: Path) -> None:
    """Nothing may block on input that a CI runner will never provide."""
    root = _tree(tmp_path, "asks", "ANSWER = input('are you there? ')\n")
    proc = subprocess.run(
        [sys.executable, str(_GUARD), "--root", str(root), "--timeout", "30"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1, proc.stdout
    assert "::error file=skills/asks/scripts/mod.py::" in proc.stdout, proc.stdout


def test_the_job_installs_the_version_the_project_promises() -> None:
    """The floor is declared in one place and must be honoured in the other.

    `pyproject.toml` is where the promise lives. If someone raises it to 3.10
    and the job keeps installing 3.9, the check drifts into testing a version
    nobody claims; if the job's pin drifts upward, it passes while checking
    nothing — the exact failure its own comment warns about.
    """
    declared = re.search(r'requires-python\s*=\s*"[><=~^]*\s*(\d+\.\d+)"',
                         (_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert declared, "pyproject.toml no longer declares a requires-python floor"
    floor = declared.group(1)

    workflow = (_REPO / ".github" / "workflows" / "python-floor.yml").read_text(
        encoding="utf-8")
    pinned = re.search(r'python-version:\s*"([^"]+)"', workflow)
    assert pinned, "the floor job no longer pins a python-version"
    assert pinned.group(1) == floor, (
        f"pyproject promises Python {floor} but the floor job installs "
        f"{pinned.group(1)}")

    major, minor = floor.split(".")
    asserted = f"sys.version_info[:2] == ({major}, {minor})"
    assert asserted in workflow, (
        f"the job does not confirm the interpreter really is {floor}; without "
        f"that it can silently resolve to a newer version and pass while "
        f"checking nothing")


def test_a_module_the_instructions_hand_to_a_reader_is_in_scope() -> None:
    """Whatever a skill tells someone to run has to import on the floor.

    `verify_report.py` lives under a skill's `tests/` but is not a test — the
    skill's own instructions present it as a step. It reaches a reader's
    interpreter like anything under `scripts/`, so leaving it out on the
    strength of its parent directory's name would be a hole with a tidy
    explanation.
    """
    sys.path.insert(0, str(_GUARD.parent))
    try:
        import check_python_floor
    finally:
        sys.path.pop(0)

    covered = {p.relative_to(_REPO).as_posix()
               for p in check_python_floor.shipped_modules(_REPO)}
    for skill_md in (_REPO / "skills").glob("*/SKILL.md"):
        target = skill_md.parent / "tests" / "verify_report.py"
        if not target.is_file():
            continue
        if "tests/verify_report.py" not in skill_md.read_text(encoding="utf-8"):
            continue
        assert target.relative_to(_REPO).as_posix() in covered, (
            f"{skill_md.name} tells a reader to run {target.name}, but the "
            "floor check never imports it")


def test_an_empty_tree_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    """Zero modules checked must never read as zero problems found."""
    proc = _run(tmp_path)
    assert proc.returncode == 1
    assert "checking nothing" in proc.stdout


def test_no_shipped_module_shadows_a_standard_library_name() -> None:
    """The probe puts a module's own directory first on the import path.

    That is what lets these scripts import their siblings by bare name, and it
    means a shipped file called `types.py` or `logging.py` would stand in
    front of the standard library for everything imported after it — inside
    this check and on a user's machine alike. The failure would be baffling in
    both places.
    """
    sys.path.insert(0, str(_GUARD.parent))
    try:
        import check_python_floor
    finally:
        sys.path.pop(0)

    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is None:  # pragma: no cover - only on the 3.9 floor
        pytest.skip("sys.stdlib_module_names arrived in 3.10")

    for module in check_python_floor.shipped_modules(_REPO):
        assert module.stem not in stdlib, (
            f"{module.relative_to(_REPO)} shadows the standard library's "
            f"{module.stem} module for anything imported after it")
