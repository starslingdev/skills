"""Tests for the eval `gh` shim (`maintainers/ci-speedup/scripts/eval_gh_shim.py`).

The shim answers the REPLAYED repository from the replay corpus during a
behavioural eval. The replayed slug (`synthetic/repo`) names a namespace that
real GitHub does not reserve for us, so anything about that repository the
corpus does not hold must fail closed (a local 404), never reach the real `gh`:
a stranger who registers it could otherwise feed the session their data.

No network and no real `gh`: CI_SPEEDUP_EVAL_REAL_GH points at a fake that
records its argv, so "delegated" and "not delegated" are both observable.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SHIM = _ROOT / "maintainers" / "ci-speedup" / "scripts" / "eval_gh_shim.py"
_SCRIPTS = _ROOT / "skills" / "ci-speedup" / "scripts"
_CORPUS = _ROOT / "skills" / "ci-speedup" / "tests" / "fixtures" / "gh_replay"
_LOG_FIXTURE = "repos_synthetic_repo_actions_jobs_60014_logs.txt"
_LIST_FIXTURE = "repos_synthetic_repo_actions_workflows_per_page_100.json"


@pytest.fixture()
def shim(tmp_path):
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    (fixtures / "repos_synthetic_repo.json").write_text(
        json.dumps({"full_name": "synthetic/repo", "default_branch": "main",
                    "visibility": "public", "private": False}), encoding="utf-8")
    shutil.copy(_CORPUS / _LOG_FIXTURE, fixtures / _LOG_FIXTURE)
    shutil.copy(_CORPUS / _LIST_FIXTURE, fixtures / _LIST_FIXTURE)
    calls = tmp_path / "real-gh-calls"
    real = tmp_path / "real-gh"
    real.write_text(f'#!/bin/sh\necho "REAL $*"\necho "$*" >> "{calls}"\n',
                    encoding="utf-8")
    real.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{_SHIM}" "$@"\n', encoding="utf-8")
    gh.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
           "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
           "CI_SPEEDUP_GH_FIXTURES": str(fixtures),
           "CI_SPEEDUP_EVAL_REAL_GH": str(real),
           "CI_SPEEDUP_EVAL_REPLAYED_REPO": "synthetic/repo",
           "CI_SPEEDUP_EVAL_SCRIPTS": str(_SCRIPTS)}
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git = {**env, "GIT_DIR": str(checkout / ".git")}
    subprocess.run(["git", "init", "-q", str(checkout)], check=True, env=git)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/synthetic/repo.git"],
                   cwd=str(checkout), check=True, env=git)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    class S:
        pass
    s = S()
    s.env, s.checkout, s.elsewhere, s.calls, s.tmp = env, checkout, elsewhere, calls, tmp_path

    def gh(*args, cwd=None, env=None):
        return subprocess.run(["gh", *args], cwd=str(cwd or checkout), env=env or s.env,
                              capture_output=True, text=True, timeout=60)
    s.gh = gh

    def real_calls():
        return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    s.real_calls = real_calls
    return s


def _is_404(r):
    return r.returncode != 0 and "HTTP 404" in r.stderr and "REAL" not in r.stdout


# --- 1. fail closed on the replayed repository --------------------------------

@pytest.mark.parametrize("args", [
    ("run", "list", "-R", "synthetic/repo"),
    ("run", "list", "--repo=synthetic/repo"),
    ("run", "list", "-Rsynthetic/repo"),
    ("run", "view", "5001", "--repo", "Synthetic/Repo"),
    ("workflow", "view", "ci.yml"),
    ("pr", "list"),
    ("issue", "list"),
    ("release", "list"),
    ("cache", "list"),
    ("secret", "list"),
    ("variable", "list"),
    ("label", "list"),
    ("ruleset", "list"),
    ("browse",),
    ("repo", "view", "--web"),
    ("repo", "clone", "synthetic/repo"),
    ("api", "-X", "GET", "repos/synthetic/repo/not-in-corpus", "-f", "k=v"),
    ("api", "repos/synthetic/repo/x", "-X", "POST"),
    ("api", "-X", "POST", "repos/synthetic/repo/issues"),
    ("api", "--method=PATCH", "repos/synthetic/repo"),
    ("api", "repos/synthetic/repo", "--input", "body.json"),
    ("api", "repos/SYNTHETIC/repo/actions/runs"),
    ("api", "graphql", "-f", "query=query{repository(owner:\"synthetic\",name:\"repo\"){id}}"),
    ("api", "graphql", "-F", "owner={owner}", "-F", "name={repo}", "-f", "query=q"),
    ("search", "code", "foo", "--repo", "synthetic/repo"),
    # gh accepts flags before the endpoint, and the endpoint in URL forms
    ("api", "-F", "owner=synthetic", "-F", "name=repo", "-f", "query=q", "graphql"),
    ("api", "/graphql", "-F", "owner=synthetic", "-F", "name=repo"),
    ("api", "https://api.github.com/graphql", "-F", "owner=synthetic"),
    # a query the shim cannot read (a file, stdin) could name the repository
    ("api", "graphql", "--input", "q.json"),
    ("api", "graphql", "-F", "query=@q.graphql"),
    # the replayed owner's namespace, not only its one repository
    ("api", "orgs/synthetic"),
    ("api", "/users/Synthetic"),
    ("api", "orgs/synthetic/repos?per_page=100"),
])
def test_unserved_calls_on_the_replayed_repo_fail_closed(shim, args):
    r = shim.gh(*args)
    assert _is_404(r), (args, r.returncode, r.stdout, r.stderr)
    assert shim.real_calls() == [], f"{args} reached the real gh"


def test_gh_repo_env_naming_the_replayed_repo_fails_closed(shim):
    r = shim.gh("run", "list", cwd=shim.elsewhere, env={**shim.env, "GH_REPO": "synthetic/repo"})
    assert _is_404(r) and shim.real_calls() == []


def test_a_non_origin_remote_naming_the_replayed_repo_fails_closed(shim):
    other = shim.tmp / "other"
    other.mkdir()
    git = {**shim.env, "GIT_DIR": str(other / ".git")}
    subprocess.run(["git", "init", "-q", str(other)], check=True, env=git)
    subprocess.run(["git", "remote", "add", "upstream", "git@github.com:synthetic/repo.git"],
                   cwd=str(other), check=True, env=git)
    r = shim.gh("pr", "list", cwd=other)
    assert _is_404(r) and shim.real_calls() == []


@pytest.mark.parametrize("args,where", [
    (("auth", "status"), "checkout"),
    (("--version",), "checkout"),
    (("version",), "checkout"),
    (("help",), "checkout"),
    (("api", "repos/cli/cli"), "checkout"),
    (("api", "user"), "checkout"),
    (("run", "list", "-R", "cli/cli"), "checkout"),
    (("pr", "list", "--repo=cli/cli"), "checkout"),
    (("repo", "view", "cli/cli"), "checkout"),
    (("pr", "list"), "elsewhere"),
    (("repo", "view"), "elsewhere"),
])
def test_everything_not_about_the_replayed_repo_still_reaches_the_real_gh(shim, args, where):
    r = shim.gh(*args, cwd=getattr(shim, where))
    assert r.stdout.startswith("REAL " + " ".join(args)), (args, r.stdout, r.stderr)


# --- 2. attached short-flag values ---------------------------------------------

@pytest.mark.parametrize("args", [
    ("api", "-XPOST", "repos/synthetic/repo"),
    ("api", "repos/synthetic/repo", "-fname=x"),
    ("api", "repos/synthetic/repo", "-Fname=x"),
    ("api", "repos/synthetic/repo", "--raw-field=name=x"),
])
def test_attached_write_flags_are_never_served(shim, args):
    r = shim.gh(*args)
    assert _is_404(r), (args, r.returncode, r.stdout, r.stderr)
    assert shim.real_calls() == []


def test_attached_jq_is_honoured(shim):
    r = shim.gh("api", "repos/synthetic/repo", "-q.default_branch")
    assert (r.returncode, r.stdout.strip()) == (0, "main")


@pytest.mark.parametrize("args", [
    ("api", "-X", "GET", "repos/synthetic/repo/actions/workflows", "-f", "per_page=100"),
    ("api", "-XGET", "repos/synthetic/repo/actions/workflows", "-Fper_page=100"),
])
def test_get_with_fields_is_a_read_with_a_query_string(shim, args):
    r = shim.gh(*args)
    want = json.loads((_CORPUS / _LIST_FIXTURE).read_text(encoding="utf-8"))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == want


# --- 3. placeholders and full API URLs -----------------------------------------

@pytest.mark.parametrize("endpoint", [
    "repos/{owner}/{repo}",
    "https://api.github.com/repos/synthetic/repo",
    "/repos/synthetic/repo",
])
def test_placeholders_and_api_urls_resolve_to_the_corpus(shim, endpoint):
    r = shim.gh("api", endpoint, "--jq", ".default_branch")
    assert (r.returncode, r.stdout.strip()) == (0, "main"), r.stderr
    assert shim.real_calls() == []


# --- 4. job logs are served from the .txt fixtures ------------------------------

def test_job_logs_are_served_raw(shim):
    r = shim.gh("api", "repos/synthetic/repo/actions/jobs/60014/logs")
    assert r.returncode == 0, r.stderr
    assert r.stdout == (_CORPUS / _LOG_FIXTURE).read_text(encoding="utf-8")


# --- 5. jq errors are not swallowed --------------------------------------------

def test_an_invalid_jq_expression_fails(shim):
    if not shutil.which("jq"):
        pytest.skip("jq not installed")
    r = shim.gh("api", "repos/synthetic/repo", "-q", ".[[[")
    assert r.returncode != 0 and r.stderr.strip()


@pytest.fixture()
def no_jq_env(shim):
    bare = shim.tmp / "bare"
    bare.mkdir()
    (bare / "git").symlink_to(shutil.which("git"))
    return {**shim.env, "PATH": f"{shim.tmp / 'bin'}{os.pathsep}{bare}"}


def test_without_jq_plain_paths_still_work(shim, no_jq_env):
    r = shim.gh("api", "repos/synthetic/repo", "-q", ".default_branch", env=no_jq_env)
    assert (r.returncode, r.stdout.strip()) == (0, "main"), r.stderr
    r = shim.gh("api", "repos/synthetic/repo", "-q", ".", env=no_jq_env)
    assert r.returncode == 0 and json.loads(r.stdout)["default_branch"] == "main"


@pytest.mark.parametrize("expr", [".default_branch | length", ".[0]", "keys", ".a[]"])
def test_without_jq_anything_but_a_plain_path_is_refused(shim, no_jq_env, expr):
    r = shim.gh("api", "repos/synthetic/repo", "-q", expr, env=no_jq_env)
    assert r.returncode != 0 and r.stderr.strip() and "null" not in r.stdout, (
        expr, r.returncode, r.stdout, r.stderr)


# --- 6. repo view --json rejects fields it does not model -----------------------

def test_repo_view_rejects_unknown_json_fields(shim):
    r = shim.gh("repo", "view", "--json", "nameWithOwner,stargazerCount")
    assert r.returncode != 0
    assert 'Unknown JSON field: "stargazerCount"' in r.stderr
    assert shim.real_calls() == []


def test_repo_view_still_serves_modeled_fields(shim):
    r = shim.gh("repo", "view", "--json", "nameWithOwner,defaultBranchRef")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"nameWithOwner": "synthetic/repo",
                                    "defaultBranchRef": {"name": "main"}}
