"""Regression test for the ``GIT_*`` cleanup in ``.githooks/pre-commit``
(issue #14).

``git commit`` exports ``GIT_DIR``, ``GIT_INDEX_FILE`` (and sometimes
``GIT_WORK_TREE``) to hook children. This hook runs the full pytest suite,
and fixture tests that shell out to ``git`` would otherwise operate on the
REAL repository instead of their tmp fixture dir — observed as HEAD resets,
index truncation, and stray fixture commits on real refs. The fix unsets
``GIT_DIR``, ``GIT_INDEX_FILE``, ``GIT_WORK_TREE`` and ``GIT_INDEX_VERSION``
at the top of the hook, before even the ``git rev-parse`` used to locate
the repo, so every git invocation in the hook and its children sees the
repository the hook runs in.

Why this test executes the hook instead of parsing it: the existing
``tests/test_pre_commit_hook.py`` guard parses the hook text, and
``tests/test_repo_guards.py`` covers the separate cleanup in
``conftest.py``. Neither catches removal of the unset line, or moving it
after ``git rev-parse`` — in the second case the test runner would still
be clean, but ``git rev-parse`` itself would already have run with the
inherited variables. So this test runs the real hook with inherited
variables set and stubbed ``git``/``uvx`` commands that record exactly
which ``GIT_*`` variables they received, and asserts that BOTH receive
none of them.

The negative controls below prove the test has teeth: a hook with the
unset line removed leaks the variables to both commands, and a hook with
the unset moved after ``git rev-parse`` leaks them to ``git`` (but not to
the runner) — so a regression in either direction fails here, not
silently in a later commit.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_HOOK = _REPO / ".githooks" / "pre-commit"

_VARS = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_INDEX_VERSION")
_UNSET_LINE = "unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_INDEX_VERSION"
_CD_LINE = 'cd "$(git rev-parse --show-toplevel)" || exit 1'
_GIT_PATTERN = r"^GIT_(DIR|INDEX_FILE|WORK_TREE|INDEX_VERSION)="


def _write_stub(bin_dir: Path, name: str, log_env: str, extra: str = "") -> None:
    """A stub command that logs any of the four variables it received."""
    script = (
        "#!/bin/bash\n"
        f"env | grep -E '{_GIT_PATTERN}' >> \"${log_env}\" || true\n"
        f"{extra}"
        "exit 0\n"
    )
    path = bin_dir / name
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_hook(tmp_path: Path, hook_text: str):
    """Run ``hook_text`` with inherited GIT_* vars and stubbed git/uvx.

    Returns (result, git_log_text, uvx_log_text).
    """
    if shutil.which("bash") is None:
        pytest.skip("bash is required to run the pre-commit hook")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_top = tmp_path / "fake-repo"
    fake_top.mkdir()
    git_log = tmp_path / "git.log"
    uvx_log = tmp_path / "uvx.log"
    git_log.write_text("")
    uvx_log.write_text("")
    _write_stub(
        bin_dir,
        "git",
        "STUB_GIT_LOG",
        'if [ "$1" = "rev-parse" ]; then echo "$FAKE_TOPLEVEL"; fi\n',
    )
    _write_stub(bin_dir, "uvx", "STUB_UVX_LOG")
    hook_path = tmp_path / "pre-commit"
    hook_path.write_text(hook_text)
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    for var in _VARS:
        env[var] = f"/tmp/fake-{var.lower()}"
    env["FAKE_TOPLEVEL"] = str(fake_top)
    env["STUB_GIT_LOG"] = str(git_log)
    env["STUB_UVX_LOG"] = str(uvx_log)
    result = subprocess.run(
        ["bash", str(hook_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, git_log.read_text(), uvx_log.read_text()


def _leaked(log_text: str) -> set[str]:
    """The GIT_* variable names present in a stub's log."""
    found = set()
    for line in log_text.splitlines():
        for var in _VARS:
            if line.startswith(var + "="):
                found.add(var)
    return found


def test_hook_unsets_git_vars_for_git_and_runner(tmp_path):
    """Both git rev-parse and the test runner must see no GIT_* vars."""
    result, git_log, uvx_log = _run_hook(tmp_path, _HOOK.read_text())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All tests passed." in result.stdout
    assert _leaked(git_log) == set(), f"git saw inherited vars: {git_log!r}"
    assert _leaked(uvx_log) == set(), f"uvx saw inherited vars: {uvx_log!r}"


def test_removing_unset_line_leaks_vars_to_both(tmp_path):
    """Negative control: without the unset line, both commands leak."""
    variant = _HOOK.read_text().replace(_UNSET_LINE + "\n", "", 1)
    assert _UNSET_LINE not in variant
    result, git_log, uvx_log = _run_hook(tmp_path, variant)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GIT_DIR" in _leaked(git_log), "git log should show the leak"
    assert "GIT_DIR" in _leaked(uvx_log), "uvx log should show the leak"


def test_moving_unset_after_rev_parse_leaks_to_git_only(tmp_path):
    """Negative control: unset after rev-parse leaves git exposed.

    The runner would still be clean in this variant, so checking only the
    runner would miss the regression — the git log check is what catches
    the ordering.
    """
    text = _HOOK.read_text().replace(_UNSET_LINE + "\n", "", 1)
    variant = text.replace(
        _CD_LINE + "\n", _CD_LINE + "\n" + _UNSET_LINE + "\n", 1
    )
    assert variant.index(_UNSET_LINE) > variant.index(_CD_LINE)
    result, git_log, uvx_log = _run_hook(tmp_path, variant)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GIT_DIR" in _leaked(git_log), "git log should show the leak"
    assert _leaked(uvx_log) == set(), "runner must stay clean in this variant"
