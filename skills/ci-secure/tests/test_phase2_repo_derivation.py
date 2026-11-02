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
    # A github.com-prefixed host, built at runtime: a literal look-alike host
    # in shipped text reads as a malicious URL to registry security scans.
    "https://" + ".".join(["github", "com", "example", "net"]) + "/acme/widgets.git",
    "https://notgithub.com/acme/widgets.git",
    "git@github.example.net:acme/widgets.git",
])
def test_lookalike_host_passes_no_repo(tmp_path, shell, origin):
    argv = _run_block(tmp_path, shell, origin, gh_ok=True)
    assert "--repo" not in argv, f"run.py argv: {argv}"


# --- every phase block stands alone in a fresh shell --------------------------
#
# The same bug class as `REPO`, one phase later: the Phase 3 render block built
# `ci-secure-report-${SLUG}.md` from a SLUG that only Phase 2's shell had set,
# so in a real session every repository rendered to `ci-secure-report-.md` (the
# cross-repo collision the slug exists to prevent), and the later blocks read
# `$FINDINGS` / `$REPORT` that nothing in their own shell assigned.

_SKILL_DIR = Path(__file__).resolve().parent.parent
# Supplied by the environment or the user, never assigned by a block.
_AMBIENT = {"TMPDIR", "HOME", "PATH", "CI_SECURE_REPO"}


def _bash_blocks_with_origin() -> list[tuple[str, str]]:
    out = []
    for doc in [_SKILL_DIR / "SKILL.md", *sorted((_SKILL_DIR / "references").glob("*.md"))]:
        text = doc.read_text(encoding="utf-8")
        for m in re.finditer(r"```(?:bash|sh)\n(.*?)```", text, re.S):
            line = text[:m.start()].count("\n") + 1
            out.append((f"{doc.name}:{line}", m.group(1)))
    return out


def test_every_bash_block_assigns_every_variable_it_expands():
    """Each block runs in its own shell, so a name it expands must be assigned
    in THAT block, not in an earlier phase's."""
    blocks = _bash_blocks_with_origin()
    assert len(blocks) >= 4, "bash-block extractor drifted"
    unbound = {}
    for where, block in blocks:
        expanded = set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)", block))
        assigned = set(re.findall(r"(?:^|;)\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=", block, re.M))
        missing = sorted(expanded - assigned - _AMBIENT)
        if missing:
            unbound[where] = missing
    assert not unbound, f"bash blocks expand names they never assign: {unbound}"


_SCRIPT_STUB = r"""#!/bin/sh
# Records argv; behaves like the real script only as far as paths go.
{ for a in "$@"; do printf '%s\037' "$a"; done; printf '\n'; } >> "$ARGV_LOG"
inp=""; out=""; plan=""
while [ $# -gt 0 ]; do
  case "$1" in
    --in|--findings) inp="$2"; shift ;;
    --out) out="$2"; shift ;;
    --render-plan) plan=1 ;;
  esac
  shift
done
[ -n "$inp" ] && [ ! -f "$inp" ] && { echo "missing input: $inp" >&2; exit 3; }
case "$(basename "$0")" in
  run.py) printf '{"gh_checks": {"P14.11": "ran"}, "findings": []}' > "$out" ;;
  report.py)
    if [ -n "$plan" ]; then echo '[]'
    else printf 'CI Secure banner ran\n| x | P14.11 |\n| **Coverage** | complete |\nIncomplete coverage\n' > "$out"; fi ;;
esac
"""


def _session(tmp_path: Path, shell: str, name: str) -> list[list[str]]:
    """Run every SKILL.md bash block, from Phase 2 on, each in its OWN fresh
    shell with `-u` (an unassigned name is an error, not an empty string)."""
    base = tmp_path / name
    bin_dir = base / "bin"
    bin_dir.mkdir(parents=True)
    repo_dir = base / "repo"
    repo_dir.mkdir()
    log = base / "argv.log"
    for script in ("run.py", "report.py", "record_timing.py"):
        _write_exe(bin_dir / script, _SCRIPT_STUB)
    _write_exe(bin_dir / "gh", "#!/bin/sh\nexit 1\n")
    shared_tmp = tmp_path / "tmp"  # shared: two repos must not collide in it
    shared_tmp.mkdir(exist_ok=True)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(base),
        "TMPDIR": str(shared_tmp),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
        "ARGV_LOG": str(log),
    }
    subprocess.run(["git", "-C", str(repo_dir), "init", "-q"], check=True, env=env)
    skill = _SKILL_MD[_SKILL_MD.index("## Phase 2"):]
    blocks = re.findall(r"```bash\n(.*?)```", skill, re.S)
    assert len(blocks) >= 3, "expected the Phase 2, Phase 3 and Phase 4 blocks"
    for i, block in enumerate(blocks):
        block = block.replace("<ci-secure>/scripts/", f"{bin_dir}/")
        proc = subprocess.run([shell, "-u", "-c", block], cwd=repo_dir, env=env,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"{shell}: block {i} after '## Phase 2' failed in a fresh shell "
            f"(rc={proc.returncode}): {proc.stderr.strip()}")
    return [ln.split("\037")[:-1] for ln in log.read_text().splitlines()]


def _paths(calls: list[list[str]], flag: str) -> list[str]:
    return [c[c.index(flag) + 1] for c in calls if flag in c]


@pytest.mark.parametrize("shell", _SHELLS)
def test_each_phase_block_runs_alone_with_repo_scoped_paths(tmp_path, shell):
    a = _session(tmp_path, shell, "a")
    b = _session(tmp_path, shell, "b")
    for calls in (a, b):
        findings = _paths(calls, "--out")[0]  # what Phase 2 wrote
        assert all(p == findings for p in _paths(calls, "--in")), calls
    report_a = [p for p in _paths(a, "--out") if "report" in p]
    report_b = [p for p in _paths(b, "--out") if "report" in p]
    assert report_a and report_b, f"no report rendered: {a}"
    assert not report_a[0].endswith("ci-secure-report-.md"), report_a
    assert report_a[0] != report_b[0], "two repos rendered to the same report path"
    assert _paths(a, "--out")[0] != _paths(b, "--out")[0]


@pytest.mark.parametrize("shell", _SHELLS)
def test_repo_is_two_argv_tokens(tmp_path, shell):
    """The zsh trap: `--repo owner/repo` must reach run.py as two tokens."""
    argv = _run_block(tmp_path, shell, "git@github.com:acme/widgets.git", gh_ok=True)
    assert "--repo acme/widgets" not in argv
    assert argv[argv.index("--repo"):argv.index("--repo") + 2] == ["--repo", "acme/widgets"]
