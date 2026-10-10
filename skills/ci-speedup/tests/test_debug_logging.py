"""`STARSLING_LOG_LEVEL` is the documented opt-in for the scripts' DEBUG trace.

The scripts log through `logging.getLogger(__name__)` at every interesting
boundary (gh endpoints, response sizes, detector gates). Those calls are only
worth anything if an entry point a user runs actually configures the root logger
from the variable; without that, `logger.debug(...)` is unreachable and a user
who sets `STARSLING_LOG_LEVEL=DEBUG` gets an empty stderr.

The fixture is the smallest run that reaches a DEBUG line: `collect_runs.py`
replaying the committed gh corpus against a `--root` that is not a git checkout,
which logs "no origin remote under <root>" at DEBUG.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh_replay"

# Every script SKILL.md / run.py launches as a program.
_ENTRY_POINTS = ("run.py", "scan.py", "collect_runs.py", "blocking_path.py",
                 "summary.py", "record_timing.py")


def _env(level: str | None) -> dict:
    # Strip GIT_* so a hook / worktree run's absolute GIT_DIR cannot leak the
    # subprocess's git calls onto the real repository.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k != "STARSLING_LOG_LEVEL"}
    env["CI_SPEEDUP_GH_FIXTURES"] = str(_FIXTURES)
    if level is not None:
        env["STARSLING_LOG_LEVEL"] = level
    return env


def _collect(tmp_path: Path, level: str | None) -> subprocess.CompletedProcess:
    root = tmp_path / "not-a-checkout"
    root.mkdir()
    src = tmp_path / "in.json"
    src.write_text(json.dumps({"findings": []}), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / "collect_runs.py"), "--in", str(src),
         "--out", str(tmp_path / "out.json"), "--repo", "synthetic/repo",
         "--root", str(root)],
        env=_env(level), cwd=tmp_path, capture_output=True, text=True, timeout=120)


def test_debug_level_reaches_stderr(tmp_path):
    r = _collect(tmp_path, "DEBUG")
    assert r.returncode == 0, r.stderr
    debug = [ln for ln in r.stderr.splitlines() if ln.startswith("DEBUG ")]
    assert any("no origin remote under" in ln for ln in debug), (
        f"STARSLING_LOG_LEVEL=DEBUG printed no DEBUG line; stderr was:\n{r.stderr!r}")


def test_lowercase_level_name_is_accepted(tmp_path):
    r = _collect(tmp_path, "debug")
    assert "no origin remote under" in r.stderr, r.stderr


def test_unset_prints_nothing_below_warning(tmp_path):
    r = _collect(tmp_path, None)
    assert r.returncode == 0, r.stderr
    assert "no origin remote under" not in r.stderr, r.stderr
    assert not [ln for ln in r.stderr.splitlines()
                if ln.startswith(("DEBUG ", "INFO "))], r.stderr


def test_invalid_level_falls_back_to_warning_with_one_notice(tmp_path):
    r = _collect(tmp_path, "LOUD")
    assert r.returncode == 0, r.stderr
    assert "no origin remote under" not in r.stderr, r.stderr
    notices = [ln for ln in r.stderr.splitlines() if "STARSLING_LOG_LEVEL" in ln]
    assert len(notices) == 1, r.stderr
    assert "'LOUD'" in notices[0] and "WARNING" in notices[0], notices


@pytest.mark.parametrize("script", _ENTRY_POINTS)
def test_every_entry_point_configures_logging(script):
    """A static pin: each `if __name__ == "__main__":` block calls the shared
    helper, so a new entry point (or a rewritten one) cannot silently drop it."""
    tree = ast.parse((_SCRIPTS / script).read_text(encoding="utf-8"))
    mains = [n for n in tree.body if isinstance(n, ast.If)
             and "__main__" in ast.unparse(n.test)]
    assert mains, f"{script} has no __main__ block"
    calls = {ast.unparse(c.func) for n in mains for c in ast.walk(n)
             if isinstance(c, ast.Call)}
    assert "configure_logging" in calls, f"{script}'s __main__ never configures logging"
