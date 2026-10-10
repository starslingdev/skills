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
import shutil
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
    assert r.returncode == 0, r.stderr
    debug = [ln for ln in r.stderr.splitlines() if ln.startswith("DEBUG ")]
    assert any("no origin remote under" in ln for ln in debug), r.stderr


def test_warn_and_fatal_aliases_are_levels_not_notices(tmp_path):
    """`WARN` and `FATAL` are the stdlib's aliases for WARNING and CRITICAL;
    log_level.py documents them as accepted, so neither prints the notice."""
    for alias in ("WARN", "fatal"):
        sub = tmp_path / alias
        sub.mkdir()
        r = _collect(sub, alias)
        assert r.returncode == 0, r.stderr
        assert "STARSLING_LOG_LEVEL" not in r.stderr, (alias, r.stderr)
        assert "no origin remote under" not in r.stderr, (alias, r.stderr)


def test_notset_shows_everything(tmp_path):
    """NOTSET is documented as showing every line, DEBUG included, with no
    notice (the stdlib treats a NOTSET root logger as "log everything")."""
    r = _collect(tmp_path, "NOTSET")
    assert r.returncode == 0, r.stderr
    assert "STARSLING_LOG_LEVEL" not in r.stderr, r.stderr
    assert any(ln.startswith("DEBUG ") and "no origin remote under" in ln
               for ln in r.stderr.splitlines()), r.stderr


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


def _is_path_setup(stmt: ast.stmt) -> bool:
    """A statement allowed before the call: an import, a `sys.path` edit, or an
    `if` whose body is only `sys.path` edits (scan.py's guard shape)."""
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        return ast.unparse(stmt.value.func).startswith("sys.path.")
    if isinstance(stmt, ast.If) and not stmt.orelse:
        return all(_is_path_setup(b) for b in stmt.body)
    return False


@pytest.mark.parametrize("script", _ENTRY_POINTS)
def test_every_entry_point_configures_logging(script):
    """A static pin on each `if __name__ == "__main__":` block: the shared
    helper is called as a top-level statement of the block (not nested under
    another `if`, `try` or loop), before anything else runs (only imports and
    `sys.path` setup may precede it, so `main()` / `sys.exit` never run first),
    and the name it calls is bound by `from log_level import configure_logging`
    (in the block before the call, or at module level)."""
    tree = ast.parse((_SCRIPTS / script).read_text(encoding="utf-8"))
    mains = [n for n in tree.body if isinstance(n, ast.If)
             and "__main__" in ast.unparse(n.test)]
    assert len(mains) == 1, f"{script} needs exactly one __main__ block"
    body = mains[0].body
    idx = next((i for i, st in enumerate(body)
                if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)
                and isinstance(st.value.func, ast.Name)), None)
    assert idx is not None, (
        f"{script}'s __main__ has no unconditional top-level call; "
        f"block is:\n{ast.unparse(mains[0])}")
    call = body[idx].value
    assert not call.args and not call.keywords, ast.unparse(call)
    name = call.func.id
    for st in body[:idx]:
        assert _is_path_setup(st), (
            f"{script}: `{ast.unparse(st)}` runs before {name}() in __main__")

    def binds(st: ast.stmt) -> bool:
        return (isinstance(st, ast.ImportFrom) and st.module == "log_level"
                and st.level == 0
                and any(a.name == "configure_logging" and (a.asname or a.name) == name
                        for a in st.names))
    assert any(binds(st) for st in [*tree.body, *body[:idx]]), (
        f"{script}: the first call in __main__ is {name}(), which is not "
        f"`from log_level import configure_logging`")


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="PYTHONSAFEPATH / -P arrived in Python 3.11")
@pytest.mark.parametrize("script", ("run.py", "scan.py", "summary.py",
                                    "record_timing.py"))
def test_every_entry_point_starts_with_a_safe_sys_path(script, tmp_path):
    """Under PYTHONSAFEPATH (`python -P`) the script's own directory is not on
    `sys.path`, so the `__main__` block's `from log_level import ...` only
    resolves if the script puts its directory there itself. `--help` exits in
    argparse, after the logging setup has run. (collect_runs.py and
    blocking_path.py are left out: they import siblings at module level with no
    such guard, so they never started under -P, before or after logging.)"""
    env = _env(None)
    env["PYTHONSAFEPATH"] = "1"
    r = subprocess.run([sys.executable, str(_SCRIPTS / script), "--help"],
                       env=env, cwd=tmp_path, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, f"{script} under PYTHONSAFEPATH=1:\n{r.stderr}"
    assert "ModuleNotFoundError" not in r.stderr, r.stderr


# ---- run.py end to end ------------------------------------------------------
# `run.py` is the program a user actually starts; it runs scan.py and
# collect_runs.py as child processes. These pin that the level reaches the
# children (they inherit run.py's environment) and that each program configures
# itself once. The repo is the offline e2e's synthetic checkout with NO origin
# remote, so collect_runs logs "no origin remote under ..." at DEBUG, and the
# gh calls replay the committed corpus.

_SENTINEL = "SENTINEL-BODY-7f3a91c2-NEVER-LOGGED"


def _plant_sentinel(node):
    """Give every JSON object in a response body an extra key holding the
    sentinel, first in key order, so any line that echoes a body (or the start
    of one) carries it. No reader looks the key up."""
    if isinstance(node, dict):
        return {"x_starsling_sentinel": _SENTINEL,
                **{k: _plant_sentinel(v) for k, v in node.items()}}
    if isinstance(node, list):
        return [_plant_sentinel(v) for v in node]
    return node


def _run_py(work: Path, level: str, *, plant: bool) -> subprocess.CompletedProcess:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(_SCRIPTS))
    import test_offline_pipeline_e2e as e2e  # noqa: E402  (shared fixture builders)

    work.mkdir()
    root = work / "repo"
    e2e._init_repo(root, origin=None)
    fixtures = e2e._replay_dir(work)
    if plant:
        for f in fixtures.glob("*.json"):
            doc = json.loads(f.read_text(encoding="utf-8"))
            f.write_text(json.dumps(_plant_sentinel(doc)), encoding="utf-8")
        for f in fixtures.glob("*.txt"):
            f.write_text(f.read_text(encoding="utf-8") + f"\n{_SENTINEL}\n",
                         encoding="utf-8")
    # Run a copy of the skill outside any git checkout. From the tracked source
    # the collector captures bill-gap candidates into the checkout's
    # `.ci-speedup-gaps/` (maintainer loop data); a copy outside git is treated
    # as an installed skill and captures nothing.
    skill = work / "skill"
    shutil.copytree(_SCRIPTS.parent, skill,
                    ignore=shutil.ignore_patterns("tests", "__pycache__"))
    env = _env(level)
    env["CI_SPEEDUP_GH_FIXTURES"] = str(fixtures)
    env.pop("CI_SPEEDUP_GH_RECORD", None)
    return subprocess.run(
        [sys.executable, str(skill / "scripts" / "run.py"), "--root", str(root),
         "--out", str(work / "out" / "findings.json"), "--repo", e2e._REPO],
        env=env, cwd=work, capture_output=True, text=True, timeout=300)


@pytest.fixture(scope="module")
def debug_run(tmp_path_factory):
    """One DEBUG run of run.py over a corpus copy with the sentinel planted in
    every response body (JSON and job logs)."""
    return _run_py(tmp_path_factory.mktemp("debug") / "w", "DEBUG", plant=True)


def test_run_py_children_inherit_the_debug_level(debug_run):
    r = debug_run
    assert r.returncode == 0, r.stderr
    debug = [ln for ln in r.stderr.splitlines() if ln.startswith("DEBUG ")]
    # The logger name proves the line came from the collector child process.
    assert any(ln.startswith("DEBUG collect_runs: no origin remote under")
               for ln in debug), (
        f"no collect_runs DEBUG line reached run.py's stderr:\n{r.stderr[-3000:]!r}")


def test_debug_output_never_prints_a_gh_response_body(debug_run):
    """The promise in log_level.py: DEBUG lines carry endpoints, sizes and
    pattern / workflow names, not what gh returned. Every replayed body holds
    the sentinel; none of it may reach stderr. (The two lines that print up to
    200 characters of gh's own error text, or a YAML parse error, are not on
    this path: the replay corpus has no failing call or unparsable file.)"""
    r = debug_run
    assert r.returncode == 0, r.stderr
    assert sum(ln.startswith("DEBUG ") for ln in r.stderr.splitlines()) > 10, r.stderr
    leaked = [ln for ln in r.stderr.splitlines() if _SENTINEL in ln]
    assert not leaked, "a response body reached stderr:\n" + "\n".join(leaked[:5])


def test_run_py_invalid_level_prints_one_notice_per_program(tmp_path):
    """run.py, scan.py and collect_runs.py each configure logging once, so an
    invalid level prints exactly three notices (the CHANGELOG's count)."""
    r = _run_py(tmp_path / "w", "LOUD", plant=False)
    assert r.returncode == 0, r.stderr
    notices = [ln for ln in r.stderr.splitlines() if "STARSLING_LOG_LEVEL" in ln]
    assert len(notices) == 3, r.stderr
    assert all("'LOUD'" in n for n in notices), notices
    assert "no origin remote under" not in r.stderr, r.stderr
