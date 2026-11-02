"""The eval `gh` shim: replayed-repo reads come from the corpus, nothing writes.

Offline: `os.execv` is recorded instead of run, so no real `gh` is ever called.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "skills" / "ci-speedup" / "scripts"
sys.path.insert(0, str(_REPO / "maintainers" / "scripts"))
sys.path.insert(0, str(_SCRIPTS))
import collect_runs  # noqa: E402
import eval_gh_shim as shim  # noqa: E402

_META = {"default_branch": "trunk", "private": False, "visibility": "public"}


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """Run the shim with a corpus dir; returns (run, execv calls, fixtures dir)."""
    calls = []
    monkeypatch.setenv("EVAL_GH_SHIM_REPO", "synthetic/repo")
    monkeypatch.setenv("EVAL_GH_REAL", "/nonexistent/gh")
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(tmp_path))
    monkeypatch.setenv("EVAL_GH_SCRIPTS", str(_SCRIPTS))
    monkeypatch.setattr(shim.os, "execv", lambda *a: calls.append(a))
    return (lambda *argv: shim.main(list(argv))), calls, tmp_path


def _write(root: Path, endpoint: str, doc) -> None:
    (root / collect_runs._fixture_name(endpoint, "json")).write_text(json.dumps(doc))


@pytest.mark.parametrize("argv", [
    ["api", "-X", "POST", "repos/synthetic/repo/issues"],
    ["api", "--method=PATCH", "repos/{owner}/{repo}"],
    ["api", "repos/synthetic/repo/dispatches", "-f", "ref=main"],
    ["api", "repos/synthetic/repo", "--input", "body.json"],
    ["api", "graphql", "-f", "query=x"],
    ["pr", "create", "-R", "synthetic/repo"],
    ["run", "rerun", "1"],
    # Writes that do not name the replayed owner never reach GitHub either.
    ["api", "-X", "POST", "repos/other/x"],
    ["api", "repos/other/x", "-F", "a=1"],
    ["api", "--input", "b.json", "repos/other/x"],
])
def test_writes_and_unknown_commands_fail_closed(gh, argv):
    run, calls, _ = gh
    assert run(*argv) == 1
    assert calls == []


@pytest.mark.parametrize("argv", [
    ["auth", "status"], ["auth", "token"], ["--version"], ["api", "repos/other/x"]])
def test_read_only_calls_delegate_to_real_gh(gh, argv):
    run, calls, _ = gh
    run(*argv)
    assert calls == [("/nonexistent/gh", ["/nonexistent/gh", *argv])]


@pytest.mark.parametrize("endpoint", ["repos/{owner}/{repo}", "/repos/synthetic/repo"])
def test_api_read_serves_fixture(gh, capsys, endpoint):
    run, calls, root = gh
    _write(root, "repos/synthetic/repo", _META)
    assert run("api", endpoint) == 0
    assert json.loads(capsys.readouterr().out) == _META
    assert calls == []


def test_api_read_without_fixture_is_404(gh):
    run, calls, _ = gh
    assert run("api", "repos/synthetic/repo/actions/runs") == 1
    assert calls == []


def test_repo_view_maps_fixture_fields(gh, capsys):
    run, calls, root = gh
    _write(root, "repos/synthetic/repo", _META)
    assert run("repo", "view", "--json", "nameWithOwner,defaultBranchRef") == 0
    assert json.loads(capsys.readouterr().out) == {
        "nameWithOwner": "synthetic/repo", "defaultBranchRef": {"name": "trunk"}}


def test_repo_view_without_fixture_is_404(gh):
    run, calls, _ = gh
    assert run("repo", "view", "--json", "nameWithOwner") == 1
    assert calls == []


def test_repo_view_of_other_repo_is_404_without_delegating(gh):
    run, calls, _ = gh
    assert run("repo", "view", "other/x") == 1
    assert calls == []
