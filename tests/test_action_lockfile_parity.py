"""The workflow-lockfile reader is duplicated, so pin it two-sided.

ci-score reads `.github/workflows/actions.lock` to credit Action pinning;
ci-secure carries a smaller, fail-closed copy to say which action references
the impostor-SHA check leaves to GitHub's own run-time verification. Each
skill installs standalone, so a copy is the only option; this test is the
coupling. If the two drift, one engine calls a lockfile valid that the other
rejects, and the two reports disagree about the same file.

ci-secure's copy is deliberately narrower in one place: it does not consult
git's file list for the lockfile's spelling, and refuses any symbolic link.
Both cases only withhold credit. Every case below is a plain file in a
non-git directory, where the two readers must agree exactly.

It lives at the repo ROOT: a cross-skill read is normal here and forbidden
inside either skill's own suite.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCORE = _ROOT / "skills" / "ci-score" / "scripts" / "practice_facts.py"
_SECURE_TESTS = _ROOT / "skills" / "ci-secure" / "tests"

_GOOD = """\
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

_NESTED = """\
version: 'v0.0.2'
workflows:
  .github/workflows/ci.yml:
    - Owner/Composite@v1
dependencies:
  owner/composite@v1:
    ref: v1
    commit: 'sha1-1111111111111111111111111111111111111111'
    owner_id: 1
    repo_id: 2
    uses:
      - other/inner@2222222222222222222222222222222222222222
  other/inner@2222222222222222222222222222222222222222:
    ref: v3
    commit: 'sha1-2222222222222222222222222222222222222222'
    owner_id: 3
    repo_id: 4
"""

_CASES = {
    "good": _GOOD,
    "nested-good": _NESTED,
    "unparseable": "version: [unclosed\n",
    "not-a-mapping": "- a\n- b\n",
    "unknown-top-key": _GOOD + "extra: 1\n",
    "undocumented-version": _GOOD.replace("'v0.0.3'", "'v9.0.0'"),
    "short-commit": _GOOD.replace("3d3c42e5aac5ba805825da76410c181273ba90b1", "3d3c42e5"),
    "missing-owner-id": _GOOD.replace("    owner_id: 2123180\n", ""),
    "bool-repo-id": _GOOD.replace("repo_id: 905126443", "repo_id: true"),
    "ref-mismatch": _GOOD.replace("ref: v3.1.0", "ref: v3.1.1"),
    "duplicate-key": _GOOD + "  actions/checkout@v7.0.1:\n    ref: v7.0.1\n",
    "dangling-listed": _GOOD.replace(
        "    - j178/prek-action@v3.1.0\n",
        "    - j178/prek-action@v3.1.0\n    - other/action@v1\n"),
    "dangling-nested": _NESTED.replace(
        "      - other/inner@2222222222222222222222222222222222222222",
        "      - other/missing@v1"),
    "sha-key-commit-mismatch": _NESTED.replace(
        "    commit: 'sha1-2222222222222222222222222222222222222222'",
        "    commit: 'sha1-3333333333333333333333333333333333333333'"),
    "bad-workflow-key": _GOOD.replace(".github/workflows/lint.yml",
                                      ".github/workflows/../lint.yml"),
    "workflows-not-mapping": _GOOD.replace(
        "workflows:\n  .github/workflows/lint.yml:\n"
        "    - actions/checkout@v7.0.1\n    - j178/prek-action@v3.1.0\n",
        "workflows: []\n"),
}


def _load(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sides():
    pytest.importorskip("yaml")
    score = _load("_parity_lockfile_practice_facts", _SCORE)
    sys.path.insert(0, str(_SECURE_TESTS))
    try:
        from _scan_import import load_scan
    finally:
        sys.path.remove(str(_SECURE_TESTS))
    return score, load_scan()


@pytest.mark.parametrize("name", sorted(_CASES))
def test_the_two_readers_agree(tmp_path: Path, sides, name: str) -> None:
    score, secure = sides
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "actions.lock").write_text(_CASES[name], encoding="utf-8")
    score_state, score_map = score._load_lockfile(tmp_path)
    secure_state, secure_map = secure._load_action_lockfile(tmp_path)
    # ci-score names an undocumented version separately; both credit nothing.
    if score_state.startswith("version:"):
        score_state = "invalid"
    assert (secure_state, secure_map) == (score_state, score_map)


def test_good_cases_are_actually_valid(tmp_path: Path, sides) -> None:
    """Guard the table itself: agreement on two always-invalid readers would
    pass vacuously."""
    _score, secure = sides
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "actions.lock").write_text(_GOOD, encoding="utf-8")
    state, per_wf = secure._load_action_lockfile(tmp_path)
    assert state == "valid"
    assert per_wf == {".github/workflows/lint.yml": {
        "actions/checkout@v7.0.1", "j178/prek-action@v3.1.0"}}


def test_absent_agrees(tmp_path: Path, sides) -> None:
    score, secure = sides
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    assert score._load_lockfile(tmp_path)[0] == "absent"
    assert secure._load_action_lockfile(tmp_path)[0] == "absent"
