"""A partial (sparse) checkout must never render as complete coverage.

Every detector in this scanner reasons about the files it can see on disk. A
sparse checkout, a partial clone, or a locally-deleted workflow is therefore
invisible to it: the detectors run on what is present, find nothing in what is
absent, and the report used to render

    | **Coverage** | ✅ complete — every workflow file was scanned |

for a repository where 1 of 17 workflow files had been read. That sentence reads
as a clean bill of health, which is exactly the claim the scan did not establish
— the same failure the network-gated impostor check already refuses to commit
("a skipped check is NOT a pass"), one layer down. It also let the config facts
assert "all 1 workflow(s) declare `permissions:`" about a repository whose other
16 workflows were never opened.

``_workflow_files_absent_from_tree`` closes it by asking git for the audited
commit's own tree (``git ls-tree`` reads the object database, so it sees files a
sparse checkout left out of the working tree) and folding any absentee into
``scan_incomplete`` — the existing channel that flips Coverage to PARTIAL, raises
the incomplete-coverage banner, and degrades the config facts to unmeasured.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _scan_import import load_scan  # the shim that pins ci-secure's own scan.py

scan = load_scan()
_SCAN_PY = Path(__file__).resolve().parents[1] / "scripts" / "scan.py"

_WORKFLOW = """\
name: {name}
on: [push]
permissions: {{contents: read}}
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hello
"""


def _git(repo, *args):
    # Hermetic git: a bare inherited environment leaks the OUTER repo's absolute
    # GIT_DIR / GIT_WORK_TREE into these fixture commands (worktree and pre-commit
    # -hook runs both set them), which has previously let fixture git commands
    # escape onto the real repository. Scrub every git variable and pin identity.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "HOME": str(repo),
    })
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=env, timeout=30)


@pytest.fixture()
def two_workflow_repo(tmp_path):
    """A real git repo whose commit carries TWO workflow files."""
    repo = tmp_path / "vendor"
    (repo / ".github" / "workflows").mkdir(parents=True)
    for name in ("alpha", "beta"):
        (repo / ".github" / "workflows" / f"{name}.yml").write_text(
            _WORKFLOW.format(name=name), encoding="utf-8")
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    assert _git(repo, "add", ".github/workflows/alpha.yml",
                ".github/workflows/beta.yml").returncode == 0
    commit = _git(repo, "commit", "-qm", "two workflows")
    assert commit.returncode == 0, commit.stderr
    return repo


def test_complete_checkout_reports_no_absent_files(two_workflow_repo):
    # The guard must not manufacture a gap on a normal, whole checkout — a false
    # PARTIAL on every ordinary run would be worse than the bug it fixes.
    assert scan._workflow_files_absent_from_tree(two_workflow_repo) == []


def test_a_workflow_missing_from_the_working_tree_is_reported_absent(two_workflow_repo):
    # THE REGRESSION. Simulate the sparse checkout the way git actually does it,
    # so this pins real behavior rather than a hand-rolled approximation.
    sparse = _git(two_workflow_repo, "sparse-checkout", "set", "--no-cone",
                  ".github/workflows/alpha.yml")
    if sparse.returncode != 0:  # pragma: no cover - old git without sparse-checkout
        pytest.skip(f"git sparse-checkout unavailable: {sparse.stderr.strip()!r}")
    wf = two_workflow_repo / ".github" / "workflows"
    assert (wf / "alpha.yml").is_file(), "sparse checkout should keep alpha.yml"
    assert not (wf / "beta.yml").exists(), "sparse checkout should drop beta.yml"

    absent = scan._workflow_files_absent_from_tree(two_workflow_repo)
    assert absent == [".github/workflows/beta.yml"], (
        "a workflow file that is in the audited commit but not in the scanned "
        f"working tree must be reported as never-scanned; got {absent!r}. Without "
        "this the report renders '✅ complete — every workflow file was scanned' "
        "for a repository the scan only partially read."
    )


def test_an_inherited_git_dir_cannot_redirect_the_probe(two_workflow_repo, tmp_path,
                                                        monkeypatch):
    # greptile P1 on #60. `git -C <root>` selects a working directory, NOT a
    # repository: an inherited absolute GIT_DIR / GIT_WORK_TREE outranks it, so the
    # probe would list some OTHER repository's tree. Every path it named would then
    # be missing from <root>, or — the dangerous direction — that repository has no
    # workflows at all, the probe returns nothing, and a partial checkout renders as
    # "✅ complete — every workflow file was scanned" again. This is not exotic: git
    # hooks and `git worktree` commands both export GIT_DIR, and this repo runs its
    # own suite from a pre-commit hook.
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    assert _git(decoy, "init", "-q", "-b", "main").returncode == 0
    (decoy / "readme.md").write_text("no workflows here\n", encoding="utf-8")
    assert _git(decoy, "add", "readme.md").returncode == 0
    assert _git(decoy, "commit", "-qm", "decoy").returncode == 0

    sparse = _git(two_workflow_repo, "sparse-checkout", "set", "--no-cone",
                  ".github/workflows/alpha.yml")
    if sparse.returncode != 0:  # pragma: no cover
        pytest.skip(f"git sparse-checkout unavailable: {sparse.stderr.strip()!r}")

    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    absent = scan._workflow_files_absent_from_tree(two_workflow_repo)
    assert absent == [".github/workflows/beta.yml"], (
        "an inherited GIT_DIR/GIT_WORK_TREE must not redirect the coverage probe to "
        f"another repository; got {absent!r}. Reading the decoy repo's tree finds no "
        "workflows, so the probe reports no gap and the partial checkout renders as "
        "complete coverage — the exact bug this probe exists to prevent."
    )


def test_a_non_git_directory_never_manufactures_a_coverage_gap(tmp_path):
    # An inconclusive probe must report NO gap: degrading every scan run outside a
    # git checkout to PARTIAL would make the honest signal meaningless.
    plain = tmp_path / "not-a-repo"
    (plain / ".github" / "workflows").mkdir(parents=True)
    (plain / ".github" / "workflows" / "alpha.yml").write_text(
        _WORKFLOW.format(name="alpha"), encoding="utf-8")
    assert scan._workflow_files_absent_from_tree(plain) == []


def test_absent_workflows_degrade_coverage_end_to_end(two_workflow_repo):
    # The unit above proves detection; this proves the WIRING, which is the half
    # that actually reaches the reader: an absent file has to land in
    # `scan_incomplete`, because that is the single channel report.py reads to
    # flip the Coverage row to PARTIAL and raise the incomplete-coverage banner.
    sparse = _git(two_workflow_repo, "sparse-checkout", "set", "--no-cone",
                  ".github/workflows/alpha.yml")
    if sparse.returncode != 0:  # pragma: no cover
        pytest.skip(f"git sparse-checkout unavailable: {sparse.stderr.strip()!r}")

    # Run the real CLI, exactly as the skill does — the wiring under test spans
    # scan.py's arg handling, discovery and JSON assembly, so an in-process call to
    # one function would prove less than the thing the user actually runs.
    proc = subprocess.run(
        [sys.executable, str(_SCAN_PY), "--root", str(two_workflow_repo),
         "--gh-impostor", "off"],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    result = json.loads(proc.stdout)
    gaps = result["scan_incomplete"]
    absent = [g for g in gaps if "absent from the scanned working tree" in g["reason"]]
    assert absent, (
        f"scan_incomplete must carry the unscanned workflow; got {gaps!r} — "
        "report.py reads this key alone to decide Coverage is PARTIAL, so an "
        "absent file that misses it renders as a complete, clean scan"
    )
    assert absent[0]["workflow_file"] == ".github/workflows/beta.yml"
    assert "NOT a pass" in absent[0]["reason"], (
        "the reason string must refuse the pass reading explicitly, matching the "
        "impostor-check convention")
    # And the scan still reports honestly on what it DID read.
    assert result["scanned_workflows"] == 1
