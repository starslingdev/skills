"""run.py provenance forwarding/derivation.

`scan.py` only stamps `skill_commit_sha` / `commit_sha` when it is handed them,
and `run.py` is the one-process orchestrator most runs go through. Before this,
run.py forwarded neither, so a findings JSON produced via run.py recorded NULL
provenance - which blanks the report's `Audited commit` row and leaves the
skill-commit footer unverifiable (`verify_report` then fails). These tests lock
in that run.py forwards explicit provenance AND derives it from git otherwise, so
no run silently drops it.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# `run` is NOT a unique module name in this repo (ci-secure also ships
# scripts/run.py), so a bare `import run` under the full-repo pytest session
# binds whichever skill's run.py was imported first -> wrong module. Load THIS
# skill's run.py by path under a unique name. run.py imports only stdlib at
# module load (it shells out to scan/collect_runs), so this is side-effect-free.
_spec = importlib.util.spec_from_file_location("ci_speedup_run", _SCRIPTS / "run.py")
run_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_mod)


def _stub_steps(monkeypatch):
    """Patch run._step so no real scan/collect runs: capture every command, feed
    the scan step a valid empty-findings JSON, and pass the collect step."""
    captured: list[list[str]] = []

    def fake_step(cmd, *, capture_stdout):
        captured.append(list(cmd))
        if any(str(c).endswith("scan.py") for c in cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout='{"findings": []}')
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(run_mod, "_step", fake_step)
    return captured


def _scan_cmd(captured: list[list[str]]) -> list[str]:
    return next(c for c in captured if any(str(x).endswith("scan.py") for x in c))


def _arg_value(cmd: list[str], flag: str) -> str | None:
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def test_explicit_provenance_is_forwarded_to_scan(monkeypatch, tmp_path):
    captured = _stub_steps(monkeypatch)
    out = tmp_path / "findings.json"
    rc = run_mod.main(["--root", str(tmp_path), "--out", str(out),
                       "--skill-commit-sha", "deadbee", "--commit-sha", "cafef00"])
    assert rc == 0
    scan = _scan_cmd(captured)
    assert _arg_value(scan, "--skill-commit-sha") == "deadbee"
    assert _arg_value(scan, "--commit-sha") == "cafef00"


def test_provenance_is_derived_from_git_when_omitted(monkeypatch, tmp_path):
    # --root is a real git checkout (this repo), so commit-sha derives from it; the
    # skill sha derives from run.py's own dir. Both must be non-empty and forwarded.
    captured = _stub_steps(monkeypatch)
    out = tmp_path / "findings.json"
    rc = run_mod.main(["--root", str(_SCRIPTS), "--out", str(out)])
    assert rc == 0
    scan = _scan_cmd(captured)
    skill = _arg_value(scan, "--skill-commit-sha")
    commit = _arg_value(scan, "--commit-sha")
    assert skill and commit
    assert skill == run_mod._git_short_sha(run_mod._DIR)


def test_git_short_sha_none_outside_a_repo(tmp_path):
    assert run_mod._git_short_sha(tmp_path) is None


# --- Installed-copy provenance from the skills-CLI lockfile (issue #2) ------------
# The skills CLI installs `skills/<name>/` as a recursive copy with NO `.git`, so
# `_git_short_sha` returns None. Rather than record a NULL sha (which blanks the
# report footer and FAILS verify_report, historically triggering a full re-run of the
# gh data pass just to pass a GUESSED --skill-commit-sha), run.py derives a distinct
# `installed:<hash12>` form from the installer's `.skill-lock.json` content hash, or
# the terminal `installed:unversioned` when no lockfile entry exists.

_REAL_HASH = "23f31c573ee00a7b5262978f90aa65f97ab6eb62"  # observed skillFolderHash


def _write_lock(path: Path, entries: dict) -> None:
    path.write_text(json.dumps({"version": 3, "skills": entries}),
                    encoding="utf-8")


def test_lock_provenance_reads_the_sibling_lockfile(tmp_path):
    # Observed layout: the lockfile is a SIBLING of the installed skill dirs.
    skills = tmp_path / "skills"
    root = skills / "ci-speedup"
    root.mkdir(parents=True)
    _write_lock(skills / ".skill-lock.json", {"ci-speedup": {"skillFolderHash": _REAL_HASH}})
    assert run_mod._skill_lock_provenance(root) == "installed:23f31c573ee0"


def test_lock_provenance_accepts_a_lockfile_inside_the_root(tmp_path):
    # Defensive second location: a lockfile directly inside the skill root.
    root = tmp_path / "ci-speedup"
    root.mkdir()
    _write_lock(root / ".skill-lock.json", {"ci-speedup": {"skillFolderHash": "deadbeefcafe1234"}})
    assert run_mod._skill_lock_provenance(root) == "installed:deadbeefcafe"


def test_lock_provenance_sole_entry_fallback_on_a_renamed_dir(tmp_path):
    # The install dir was renamed so it no longer keys the entry; a SINGLE entry
    # still resolves (there's no ambiguity about which skill it is).
    skills = tmp_path / "skills"
    root = skills / "renamed"
    root.mkdir(parents=True)
    _write_lock(skills / ".skill-lock.json", {"ci-speedup": {"skillFolderHash": "001122334455aabb"}})
    assert run_mod._skill_lock_provenance(root) == "installed:001122334455"


def test_lock_provenance_ambiguous_multi_entry_miss_is_unversioned(tmp_path):
    # A rename with MULTIPLE entries is ambiguous — don't guess; stay unversioned.
    skills = tmp_path / "skills"
    root = skills / "renamed"
    root.mkdir(parents=True)
    _write_lock(skills / ".skill-lock.json",
                {"a": {"skillFolderHash": "aaaaaaaaaaaa1111"},
                 "b": {"skillFolderHash": "bbbbbbbbbbbb2222"}})
    assert run_mod._skill_lock_provenance(root) == run_mod._INSTALLED_UNVERSIONED


def test_lock_provenance_missing_lockfile_is_unversioned(tmp_path):
    root = tmp_path / "ci-speedup"
    root.mkdir()
    assert run_mod._skill_lock_provenance(root) == "installed:unversioned"


def test_lock_provenance_malformed_json_is_unversioned(tmp_path):
    skills = tmp_path / "skills"
    root = skills / "ci-speedup"
    root.mkdir(parents=True)
    (skills / ".skill-lock.json").write_text("{not json", encoding="utf-8")
    assert run_mod._skill_lock_provenance(root) == "installed:unversioned"


def test_lock_provenance_non_utf8_lockfile_is_unversioned(tmp_path):
    # A corrupt (non-UTF-8) lockfile must degrade to unversioned, never raise a
    # UnicodeDecodeError out of the provenance probe and fail the run. Regression
    # pin: the except arm catches ValueError (covers JSONDecodeError AND
    # UnicodeDecodeError), not json.JSONDecodeError alone.
    skills = tmp_path / "skills"
    root = skills / "ci-speedup"
    root.mkdir(parents=True)
    (skills / ".skill-lock.json").write_bytes(b"\xff\xfe\x00not utf-8")
    assert run_mod._skill_lock_provenance(root) == "installed:unversioned"


def test_lock_provenance_non_hex_hash_is_unversioned(tmp_path):
    # A hash with < 12 usable hex chars can't form a valid identity → unversioned,
    # never a truncated/garbage token.
    skills = tmp_path / "skills"
    root = skills / "ci-speedup"
    root.mkdir(parents=True)
    _write_lock(skills / ".skill-lock.json", {"ci-speedup": {"skillFolderHash": "xyz"}})
    assert run_mod._skill_lock_provenance(root) == "installed:unversioned"


# --- Bounded upward walk: current-CLI GRANDPARENT layout (issue #91) --------------
# The current skills CLI writes the lockfile at the skill root's GRANDPARENT
# (`~/.agents/.skill-lock.json` for `~/.agents/skills/ci-speedup`), where earlier
# CLIs wrote it at the parent. The probe walks three levels (root, parent,
# grandparent), NEAREST first, stopping at the first lockfile with a matching entry.


def test_lock_provenance_reads_the_grandparent_lockfile(tmp_path):
    # Live current-CLI shape: lockfile two levels above the skill root.
    agents = tmp_path / ".agents"
    root = agents / "skills" / "ci-speedup"
    root.mkdir(parents=True)
    _write_lock(agents / ".skill-lock.json", {"ci-speedup": {"skillFolderHash": _REAL_HASH}})
    assert run_mod._skill_lock_provenance(root) == "installed:23f31c573ee0"


def test_lock_provenance_parseable_no_entry_at_parent_keeps_walking_to_grandparent(tmp_path):
    # A parseable lockfile WITHOUT a matching entry must NOT stop the walk — the
    # grandparent lockfile carries the real entry and wins.
    agents = tmp_path / ".agents"
    root = agents / "skills" / "ci-speedup"
    root.mkdir(parents=True)
    # Parent: valid JSON, but no `ci-speedup` entry (and multi-entry so no sole
    # fallback) → no match, walk continues.
    _write_lock(root.parent / ".skill-lock.json",
                {"other-a": {"skillFolderHash": "aaaaaaaaaaaa1111"},
                 "other-b": {"skillFolderHash": "bbbbbbbbbbbb2222"}})
    # Grandparent: the matching entry.
    _write_lock(agents / ".skill-lock.json", {"ci-speedup": {"skillFolderHash": _REAL_HASH}})
    assert run_mod._skill_lock_provenance(root) == "installed:23f31c573ee0"


def test_lock_provenance_does_not_walk_above_three_levels(tmp_path):
    # A matching lockfile ONE level above the grandparent (great-grandparent, the
    # 4th level up) must NOT be read — the walk is bounded to three levels. Pin it.
    great = tmp_path / "great"
    root = great / ".agents" / "skills" / "ci-speedup"
    root.mkdir(parents=True)
    # great-grandparent = root.parent.parent.parent = `great`.
    _write_lock(great / ".skill-lock.json", {"ci-speedup": {"skillFolderHash": _REAL_HASH}})
    assert run_mod._skill_lock_provenance(root) == "installed:unversioned"


def test_lock_provenance_nearest_lockfile_wins_a_tie(tmp_path):
    # Both parent and grandparent carry a matching entry; nearest (parent) wins.
    agents = tmp_path / ".agents"
    root = agents / "skills" / "ci-speedup"
    root.mkdir(parents=True)
    _write_lock(root.parent / ".skill-lock.json", {"ci-speedup": {"skillFolderHash": "111111111111aaaa"}})
    _write_lock(agents / ".skill-lock.json", {"ci-speedup": {"skillFolderHash": _REAL_HASH}})
    assert run_mod._skill_lock_provenance(root) == "installed:111111111111"


def _installed_run(monkeypatch, tmp_path, *, lock: dict | None, extra: list | None = None):
    """Drive run.py as if from an INSTALLED (non-git) skill copy: point `_DIR` at a
    non-git scripts dir under a skill root, optionally with a lockfile beside it."""
    captured = _stub_steps(monkeypatch)
    skills = tmp_path / "skills"
    scripts = skills / "ci-speedup" / "scripts"
    scripts.mkdir(parents=True)
    if lock is not None:
        _write_lock(skills / ".skill-lock.json", lock)
    monkeypatch.setattr(run_mod, "_DIR", scripts)  # non-git → git derivation returns None
    out = tmp_path / "findings.json"
    rc = run_mod.main(["--root", str(tmp_path), "--out", str(out), *(extra or [])])
    assert rc == 0
    return _scan_cmd(captured)


def test_run_stamps_installed_form_for_a_non_git_skill_copy(monkeypatch, tmp_path):
    scan = _installed_run(monkeypatch, tmp_path,
                          lock={"ci-speedup": {"skillFolderHash": _REAL_HASH}})
    assert _arg_value(scan, "--skill-commit-sha") == "installed:23f31c573ee0"


def test_run_stamps_unversioned_when_no_lockfile(monkeypatch, tmp_path):
    scan = _installed_run(monkeypatch, tmp_path, lock=None)
    assert _arg_value(scan, "--skill-commit-sha") == "installed:unversioned"


def test_run_explicit_skill_sha_still_wins_over_the_lockfile(monkeypatch, tmp_path):
    scan = _installed_run(monkeypatch, tmp_path,
                          lock={"ci-speedup": {"skillFolderHash": _REAL_HASH}},
                          extra=["--skill-commit-sha", "deadbee"])
    assert _arg_value(scan, "--skill-commit-sha") == "deadbee"


def test_git_short_sha_resolves_for_a_real_checkout():
    sha = run_mod._git_short_sha(_SCRIPTS)
    if sha is None:
        pytest.skip("not a git checkout (e.g. source tarball / git absent) - "
                    "nothing to resolve; the graceful-None path is covered above")
    assert all(c in "0123456789abcdef" for c in sha)


def test_creates_a_missing_out_parent_directory(monkeypatch, tmp_path):
    # The fix's whole point is the MISSING-parent case: a fresh `--out` whose parent
    # dir doesn't exist used to traceback on the first (partial-file) write. Point
    # --out at a nested path that does NOT exist yet and assert the run succeeds and
    # the directory was created (regression guard: dropping parents=True would fail
    # here but pass every already-existing-tmp_path test).
    captured = _stub_steps(monkeypatch)
    out = tmp_path / "deeply" / "nested" / "findings.json"
    assert not out.parent.exists()
    rc = run_mod.main(["--root", str(tmp_path), "--out", str(out)])
    assert rc == 0
    assert out.parent.is_dir()
    # Sanity: the run still drove its steps (scan was invoked) despite the missing dir.
    assert _scan_cmd(captured)


# --- Report renders to an INTERNAL/session path beside --out, NOT the working tree ---
# Issue #18: the full markdown report is opt-in. It renders + verify-gates internally on
# EVERY run beside the scratch findings.json; it is copied into the working tree only when
# the user opts into "save the full report" at the phase-6 close. So run.py's printed render
# command must target the session path beside --out, NOT cwd — the pre-#18 default (report
# in cwd) is now the opt-in *surfacing* path, not the default render target.

def _capture_report_out(monkeypatch) -> dict[str, str]:
    """Patch build_summary to record the out_path run.py hands it (the render
    command's --out) without needing a real render."""
    seen: dict[str, str] = {}

    def fake_summary(data, *, findings_path, out_path, root=None, bmf=False):
        seen["out_path"] = out_path
        seen["findings_path"] = findings_path
        seen["root"] = root
        return "SUMMARY"

    monkeypatch.setattr(run_mod, "build_summary", fake_summary)
    return seen


def test_report_defaults_to_the_session_path_beside_out_not_cwd(monkeypatch, tmp_path):
    captured = _stub_steps(monkeypatch)  # noqa: F841 — keeps scan/collect from running
    seen = _capture_report_out(monkeypatch)
    scratch = tmp_path / "scratch"
    out = scratch / "findings.json"
    monkeypatch.chdir(tmp_path)  # cwd is the working tree — the report must NOT land here by default
    rc = run_mod.main(["--root", str(tmp_path), "--out", str(out)])
    assert rc == 0
    report_out = Path(seen["out_path"])
    # The report renders BESIDE findings.json (the internal/session scratch dir) …
    assert report_out.parent == scratch
    assert report_out == scratch / "ci-speedup-findings-report.md"
    # … NOT the working-tree cwd. That path is the opt-in *surfacing* target (phase-6
    # "save the full report"), never the default render destination.
    assert report_out != tmp_path / "ci-speedup-findings-report.md"
    # findings.json itself still points at the scratch --out (unchanged).
    assert seen["findings_path"] == str(out)


def test_report_out_flag_overrides_the_default(monkeypatch, tmp_path):
    captured = _stub_steps(monkeypatch)  # noqa: F841
    seen = _capture_report_out(monkeypatch)
    out = tmp_path / "scratch" / "findings.json"
    custom = tmp_path / "elsewhere" / "my-report.md"
    rc = run_mod.main(["--root", str(tmp_path), "--out", str(out),
                       "--report-out", str(custom)])
    assert rc == 0
    assert Path(seen["out_path"]) == custom
