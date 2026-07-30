"""Regression proof for the repo-root ``conftest.py`` guards (PR #18 / the
2026-07-30 worktree-corruption incident).

Guard 1 (import-time ``GIT_*`` scrub) and Guard 2 (repo-state tripwire) are the
kind of safety code that passes silently when broken: the whole suite stays green
whether the scrub loop exists or not, because nothing ELSE exercises a hostile
``GIT_DIR``, and the tripwire's ``raise`` never runs in a normal, non-mutating
run. So a refactor that deletes the scrub or inverts the drift compare would ship
with CI green. These tests red-prove both guards.

The Guard-1 proof runs a NESTED pytest in a subprocess with a hostile absolute
``GIT_DIR`` exported into the child env, pointing at a throwaway *victim* repo —
exactly the incident vector. With the real conftest's scrub loaded, the inner
test's ``git commit`` cannot reach the victim; a negative control WITHOUT the
scrub proves the victim IS reachable, so the positive assertion has teeth.
Everything stays inside ``tmp_path``; ``GIT_DIR`` never points at the real repo
(that would trip this very suite's own Guard 2).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CONFTEST = _REPO / "conftest.py"


# ---------------------------------------------------------------------------
# Guard 1 — the import-time GIT_* scrub (end-to-end, nested pytest subprocess)
# ---------------------------------------------------------------------------

def _git(*args: str, cwd: Path):
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True)


def _make_victim(tmp_path: Path):
    """A throwaway repo with one commit; return (path, head_sha, core.bare)."""
    victim = tmp_path / "victim"
    victim.mkdir()
    _git("init", "-q", cwd=victim)
    _git("-c", "user.email=a@b.c", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "base", cwd=victim)
    head = _git("rev-parse", "HEAD", cwd=victim).stdout.strip()
    bare = _git("config", "core.bare", cwd=victim).stdout.strip()
    assert head and bare == "false"
    return victim, head, bare


# Inner test: attempt a commit with NO -C and NO explicit GIT_DIR, so it inherits
# whatever repo-addressing env the parent exported. No assertion here — the OUTER
# test judges the vector by the victim's state, not this test's pass/fail.
_INNER_TEST = (
    "import subprocess\n"
    "def test_attempt_commit():\n"
    "    subprocess.run(['git', '-c', 'user.email=x@x.c', '-c', 'user.name=x',\n"
    "                    'commit', '--allow-empty', '-m', 'pwn-attempt'],\n"
    "                   capture_output=True)\n"
)


def _run_inner(proj: Path, victim: Path, *, with_scrub: bool):
    proj.mkdir()
    (proj / "test_inner.py").write_text(_INNER_TEST, encoding="utf-8")
    if with_scrub:
        # The REAL guard under test — a byte copy of the root conftest.
        (proj / "conftest.py").write_text(
            _CONFTEST.read_text(encoding="utf-8"), encoding="utf-8")
    env = dict(os.environ)
    env["GIT_DIR"] = str((victim / ".git").resolve())  # the hostile inheritance
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(proj),
         "-p", "no:cacheprovider", "-q"],
        cwd=str(proj), env=env, capture_output=True, text=True,
    )


def test_scrub_protects_victim_repo(tmp_path: Path):
    """WITH the real conftest scrub loaded, a nested test's git commit cannot
    reach a repo it inherited via GIT_DIR — the victim is byte-identical after."""
    victim, head0, bare0 = _make_victim(tmp_path)
    _run_inner(tmp_path / "proj", victim, with_scrub=True)
    assert _git("rev-parse", "HEAD", cwd=victim).stdout.strip() == head0
    assert _git("config", "core.bare", cwd=victim).stdout.strip() == bare0


def test_negative_control_victim_clobbered_without_scrub(tmp_path: Path):
    """Teeth check: WITHOUT the scrub, the IDENTICAL inner commit DOES reach the
    victim (HEAD moves). Proves the positive test passes because of the scrub, not
    because the inherited-GIT_DIR vector was inert to begin with."""
    victim, head0, _ = _make_victim(tmp_path)
    _run_inner(tmp_path / "proj", victim, with_scrub=False)
    assert _git("rev-parse", "HEAD", cwd=victim).stdout.strip() != head0


# ---------------------------------------------------------------------------
# Guard 2 — the repo-state tripwire (unit tests on the session hooks)
# ---------------------------------------------------------------------------

def _load_conftest():
    """Load the root conftest as an isolated module so we can drive its hooks and
    monkeypatch _repo_state. (Re-running its import-time scrub is a harmless no-op —
    the GIT_* vars are already gone from this process's environ.)"""
    spec = importlib.util.spec_from_file_location(
        "_root_conftest_under_test", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeSession:
    def __init__(self):
        self.config = type("_Cfg", (), {})()


def test_tripwire_raises_on_drift(monkeypatch):
    mod = _load_conftest()
    session = _FakeSession()
    monkeypatch.setattr(mod, "_repo_state",
                        lambda: {"head": "aaa", "branch": "refs/heads/main", "bare": "false"})
    mod.pytest_sessionstart(session)
    monkeypatch.setattr(mod, "_repo_state",
                        lambda: {"head": "bbb", "branch": "refs/heads/main", "bare": "false"})
    with pytest.raises(pytest.UsageError):
        mod.pytest_sessionfinish(session, 0)


def test_tripwire_silent_when_no_drift(monkeypatch):
    mod = _load_conftest()
    session = _FakeSession()
    state = {"head": "aaa", "branch": "refs/heads/main", "bare": "false"}
    monkeypatch.setattr(mod, "_repo_state", lambda: dict(state))
    mod.pytest_sessionstart(session)
    mod.pytest_sessionfinish(session, 0)  # must not raise — unchanged state


def test_tripwire_inactive_and_loud_when_no_baseline(monkeypatch, capsys):
    """Missing baseline must fail OPEN but LOUD, never silently: sessionfinish
    does not raise, and 'INACTIVE' is announced on stderr (the silent fail-open fix)."""
    mod = _load_conftest()
    session = _FakeSession()
    monkeypatch.setattr(mod, "_repo_state", lambda: None)
    mod.pytest_sessionstart(session)
    mod.pytest_sessionfinish(session, 0)  # must not raise despite no baseline
    assert "INACTIVE" in capsys.readouterr().err


def test_tripwire_inconclusive_not_alarm_when_end_read_fails(monkeypatch, capsys):
    """A failed re-read at session end is INCONCLUSIVE, not proof of mutation:
    it must warn, not raise the misleading 'a test mutated this repo' tripwire."""
    mod = _load_conftest()
    session = _FakeSession()
    monkeypatch.setattr(mod, "_repo_state",
                        lambda: {"head": "aaa", "branch": "b", "bare": "false"})
    mod.pytest_sessionstart(session)
    monkeypatch.setattr(mod, "_repo_state", lambda: None)  # re-read fails at end
    mod.pytest_sessionfinish(session, 0)  # must NOT raise
    assert "INCONCLUSIVE" in capsys.readouterr().err
