"""The Phase 2 scan block must derive `--repo owner/repo` ITSELF.

Each phase of a session runs in its own shell, so a `REPO` variable set while
following Phase 1 is gone by the time Phase 2's block runs. The block used to
read `REPO="${REPO-}"` and nothing else, so in a real session `run.py` got no
`--repo` and every API-gated config fact (plus the dormancy lookup) came back
unmeasured. These tests execute the block exactly as SKILL.md ships it, in a
FRESH shell with no `REPO` carried over, inside a throwaway git repo, with a
stub `gh` and a stub `run.py` that records its argv.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_SKILL_MD = (Path(__file__).resolve().parent.parent / "SKILL.md").read_text(encoding="utf-8")

_SHELLS = [s for s in ("bash", "zsh") if shutil.which(s)]


def _phase2_block() -> str:
    phase2 = _SKILL_MD[_SKILL_MD.index("## Phase 2"):]
    m = re.search(r"```bash\n(.*?)```", phase2, re.S)
    assert m, "Phase 2 has no ```bash block"
    block = m.group(1)
    assert "<ci-secure>/scripts/run.py" in block, "Phase 2 block no longer calls run.py"
    return block


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_block(tmp_path: Path, shell: str, origin: str | None, gh_ok: bool,
               extra_env: dict[str, str] | None = None) -> list[str]:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    argv_file = tmp_path / "argv.txt"

    _write_exe(bin_dir / "gh", f"#!/bin/sh\n[ \"$1 $2\" = 'auth status' ] && exit {0 if gh_ok else 1}\nexit 1\n")
    _write_exe(bin_dir / "run.py", f"#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done > '{argv_file}'\n")

    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
    }
    env.update(extra_env or {})

    git = ["git", "-C", str(repo_dir)]
    subprocess.run([*git, "init", "-q"], check=True, env=env)
    if origin is not None:
        subprocess.run([*git, "remote", "add", "origin", origin], check=True, env=env)

    block = _phase2_block().replace("<ci-secure>/scripts/run.py", str(bin_dir / "run.py"))
    proc = subprocess.run([shell, "-c", block], cwd=repo_dir, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"{shell} block failed: {proc.stderr}"
    return argv_file.read_text(encoding="utf-8").splitlines()


def _repo_arg(argv: list[str]) -> str | None:
    if "--repo" not in argv:
        return None
    return argv[argv.index("--repo") + 1]


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("origin", [
    "https://github.com/acme/widgets.git",
    "https://github.com/acme/widgets",
    "git@github.com:acme/widgets.git",
])
def test_fresh_shell_derives_repo_from_github_origin(tmp_path, shell, origin):
    argv = _run_block(tmp_path, shell, origin, gh_ok=True)
    assert _repo_arg(argv) == "acme/widgets", f"run.py argv: {argv}"


@pytest.mark.parametrize("shell", _SHELLS)
def test_gh_not_authenticated_passes_no_repo(tmp_path, shell):
    argv = _run_block(tmp_path, shell, "https://github.com/acme/widgets.git", gh_ok=False)
    assert "--repo" not in argv, f"run.py argv: {argv}"


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("origin", [
    "https://gitlab.com/acme/widgets.git",
    "git@gitlab.com:acme/widgets.git",
    "https://github.com/acme/widgets/extra.git",
    # A relative-path origin has no GitHub prefix; it must not be read as
    # owner/repo (that would point the API lookups at an unrelated repo).
    "acme/widgets.git",
    "acme/widgets",
    None,
])
def test_non_github_or_malformed_origin_passes_no_repo(tmp_path, shell, origin):
    argv = _run_block(tmp_path, shell, origin, gh_ok=True)
    assert "--repo" not in argv, f"run.py argv: {argv}"


@pytest.mark.parametrize("shell", _SHELLS)
def test_ci_secure_repo_override_wins_over_origin(tmp_path, shell):
    argv = _run_block(tmp_path, shell, "https://github.com/acme/widgets.git",
                      gh_ok=True, extra_env={"CI_SECURE_REPO": "other/place"})
    assert _repo_arg(argv) == "other/place", f"run.py argv: {argv}"


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("stray", ["other-org/other-repo", "/path/to/dir", "acme"])
def test_generic_repo_env_var_is_ignored(tmp_path, shell, stray):
    """`REPO` is a generic name a user's profile may set for another tool. A
    valid-looking one would read ANOTHER repo's settings and run history and
    score them as this repo's (a false pass); a path-shaped one makes scan.py
    exit 2 and stops the whole audit. The block must ignore it entirely."""
    argv = _run_block(tmp_path, shell, "https://github.com/acme/widgets.git",
                      gh_ok=True, extra_env={"REPO": stray})
    assert _repo_arg(argv) == "acme/widgets", f"run.py argv: {argv}"


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("override", ["/path/to/dir", "https://github.com/acme/widgets", "acme"])
def test_override_not_owner_repo_is_dropped(tmp_path, shell, override):
    """A malformed override leaves the checks unmeasured; it never reaches
    run.py, where scan.py would reject it with exit 2 and stop the audit."""
    argv = _run_block(tmp_path, shell, "https://github.com/acme/widgets.git",
                      gh_ok=True, extra_env={"CI_SECURE_REPO": override})
    assert "--repo" not in argv, f"run.py argv: {argv}"


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("origin", [
    "https://x-access-token:s3cr3tTOKEN@github.com/acme/widgets.git",
    "https://someone@github.com/acme/widgets.git",
    "ssh://git@github.com/acme/widgets.git",
    "https://github.com/acme/widgets/",
])
def test_other_github_shapes_derive_repo(tmp_path, shell, origin):
    argv = _run_block(tmp_path, shell, origin, gh_ok=True)
    assert _repo_arg(argv) == "acme/widgets", f"run.py argv: {argv}"
    assert not any("s3cr3tTOKEN" in a for a in argv), f"token leaked: {argv}"


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("origin", [
    "https://github.com.example.net/acme/widgets.git",
    "https://notgithub.com/acme/widgets.git",
    "git@github.example.net:acme/widgets.git",
])
def test_lookalike_host_passes_no_repo(tmp_path, shell, origin):
    argv = _run_block(tmp_path, shell, origin, gh_ok=True)
    assert "--repo" not in argv, f"run.py argv: {argv}"


@pytest.mark.parametrize("shell", _SHELLS)
def test_repo_is_two_argv_tokens(tmp_path, shell):
    """The zsh trap: `--repo owner/repo` must reach run.py as two tokens."""
    argv = _run_block(tmp_path, shell, "git@github.com:acme/widgets.git", gh_ok=True)
    assert "--repo acme/widgets" not in argv
    assert argv[argv.index("--repo"):argv.index("--repo") + 2] == ["--repo", "acme/widgets"]
