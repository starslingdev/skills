"""A workflow lockfile must never make the impostor-SHA check go quiet.

GitHub's workflow lockfile (`.github/workflows/actions.lock`, written by
`gh actions-lock`) lets a repository keep `uses: owner/repo@v7.0.1` in its
YAML while the commit that runs lives only in the lockfile. The impostor-SHA
check (P14.11) reads full-commit pins from `uses:` lines, so on a locked
repository it has nothing to check — and before this test the report said
nothing about it either. A check with nothing to check must say why.

The note is informational, never a coverage gap: GitHub itself verifies a
locked commit at run time (reachable from its recorded ref, same repository
identity), so a locked repository did the right thing and must still read
"Coverage: complete". An unreadable or malformed lockfile is never credited.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SCAN_SCRIPT = _SKILL_DIR / "scripts" / "scan.py"
_SCRIPTS = str(_SKILL_DIR / "scripts")

# Import report.py the way test_report.py does (module names collide across
# the skills' scripts/ dirs under the repo-wide run).
_saved_config = sys.modules.pop("config", None)
_saved_report = sys.modules.pop("report", None)
sys.path.insert(0, _SCRIPTS)
try:
    import report  # noqa: E402
finally:
    try:
        sys.path.remove(_SCRIPTS)
    except ValueError:                          # pragma: no cover - defensive
        pass
    if _saved_config is not None:
        sys.modules["config"] = _saved_config
    else:
        sys.modules.pop("config", None)
    if _saved_report is not None:
        sys.modules["report"] = _saved_report
    else:
        sys.modules.pop("report", None)


# The setup from GitHub's lockfile launch material: two jobs both checking out
# with a tag, one third-party action with a tag, and a lockfile that onboards
# the workflow and records the commit for each reference.
_LINT_YML = """\
name: lint
on: [push, pull_request]
permissions: {}
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7.0.1
        with:
          persist-credentials: false
      - uses: j178/prek-action@v3.1.0
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7.0.1
        with:
          persist-credentials: false
      - run: echo done
"""

_ACTIONS_LOCK = """\
version: 'v0.0.3'
workflows:
  .github/workflows/lint.yml:
    - actions/checkout@v7.0.1
    - j178/prek-action@v3.1.0
dependencies:
  actions/checkout@v7.0.1:
    ref: v7.0.1
    commit: 'sha1-3d3c42e5aac5ba805825da76410c181273ba90b1'
    owner_id: 44036562
    repo_id: 197814629
  j178/prek-action@v3.1.0:
    ref: v3.1.0
    commit: 'sha1-44924fe9ef4a6f88253c4b6b1bf11c689d3ce53d'
    owner_id: 2123180
    repo_id: 905126443
"""


def _repo(tmp_path: Path, lock: str | None) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "lint.yml").write_text(_LINT_YML, encoding="utf-8")
    if lock is not None:
        (wf / "actions.lock").write_text(lock, encoding="utf-8")
    return tmp_path


def _scan(root: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(root),
         "--gh-impostor", "off"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _lockfile_row(md: str) -> str:
    rows = [ln for ln in md.splitlines() if ln.startswith("| **Action lockfile** |")]
    return rows[0] if rows else ""


def test_locked_references_are_disclosed_not_silently_unchecked(tmp_path: Path) -> None:
    data = _scan(_repo(tmp_path, _ACTIONS_LOCK))
    lock = data.get("action_lockfile")
    assert lock == {
        "path": ".github/workflows/actions.lock",
        "state": "valid",
        "covered_references": 3,
        "covered_workflows": [".github/workflows/lint.yml"],
    }, lock
    # Informational: GitHub verifies locked commits itself, so a locked
    # repository's coverage stays complete.
    assert data["coverage_notes"] == []
    assert data["scan_incomplete"] == []
    assert data["dropped_matches"] == []

    md = report.render(data)
    row = _lockfile_row(md)
    assert row, "the report must carry an Action lockfile row"
    assert "3 action reference(s) in 1 workflow(s)" in row
    assert "`.github/workflows/actions.lock`" in row
    assert "GitHub verifies each locked commit at run time" in row
    assert "P14.11" in row
    assert "✅ complete" in md


@pytest.mark.parametrize("lock", [
    None,                                            # no lockfile
    "version: [unclosed\n",                          # does not parse
    _ACTIONS_LOCK.replace("sha1-3d3c42e5aac5ba805825da76410c181273ba90b1",
                          "sha1-3d3c42e5"),          # short commit
    _ACTIONS_LOCK.replace("'v0.0.3'", "'v9.0.0'"),   # undocumented version
    _ACTIONS_LOCK.replace("    owner_id: 2123180\n", ""),  # missing identity
    _ACTIONS_LOCK + "  actions/checkout@v7.0.1:\n    ref: v7.0.1\n",  # dup key
    _ACTIONS_LOCK.replace("    - j178/prek-action@v3.1.0\n",
                          "    - j178/prek-action@v3.1.0\n"
                          "    - other/action@v1\n"),  # listed, no entry
], ids=["absent", "unparseable", "short-commit", "unknown-version",
        "missing-owner-id", "duplicate-key", "dangling-reference"])
def test_no_or_malformed_lockfile_credits_nothing(tmp_path: Path, lock) -> None:
    data = _scan(_repo(tmp_path, lock))
    entry = data.get("action_lockfile")
    assert isinstance(entry, dict), entry
    assert entry["covered_references"] == 0
    assert entry["covered_workflows"] == []
    assert entry["state"] == ("absent" if lock is None else "invalid")
    row = _lockfile_row(report.render(data))
    assert "verifies each locked commit" not in row
    if lock is None:
        assert row == ""
    else:
        assert "not a readable, well-formed lockfile" in row


def test_unlisted_workflow_is_not_covered(tmp_path: Path) -> None:
    """Only a workflow listed under `workflows:` runs its locked commits."""
    lock = _ACTIONS_LOCK.replace(".github/workflows/lint.yml",
                                 ".github/workflows/other.yml")
    data = _scan(_repo(tmp_path, lock))
    entry = data["action_lockfile"]
    assert entry["state"] == "valid"
    assert entry["covered_references"] == 0
    assert entry["covered_workflows"] == []
    assert "verifies each locked commit" not in _lockfile_row(report.render(data))
