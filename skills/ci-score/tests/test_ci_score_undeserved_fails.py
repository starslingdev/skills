"""Four shapes where a repository that follows the practice was FAILED.

Each cell below is a repository doing the right thing, paired with a negative
control that must keep failing, so no fix can over-credit:

1. Dependency caching that is ON BY DEFAULT (setup-go v4+, setup-uv v5+,
   setup-node v5+ with a package-manager field, setup-gradle) or switched on by
   an input other than `cache:` (setup-ruby's `bundler-cache`).
2. Test sharding through a matrix GENERATED AT RUNTIME
   (`matrix: ${{ fromJSON(...) }}`), which has no mapping to read axes from.
3. Job timeouts on a repository whose every job delegates to a cross-repo
   reusable workflow — a calling job cannot set `timeout-minutes` at all.
4. Shallow checkout when the full-history checkout STEP carries its own `if:`
   that can never be true on a pull request.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

_SKILL_DIR = Path(__file__).resolve().parents[1]


def _load(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, _SKILL_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


pf_mod = _load("ci_score_practice_facts_undeserved_fails", "scripts/practice_facts.py")

SHA = "8f4b7f84864484a7bf31766abe9204da3cbe65b3"
PR_ON = "on:\n  pull_request:\n"
CACHE = "ci.cache.dependency-cache"
SHARD = "ci.parallel.test-sharding"
TIMEOUTS = "ci.hygiene.job-timeouts"
SHALLOW = "ci.checkout.shallow-clone"


def facts_for(root: Path, *files: tuple[str, str]) -> dict:
    parsed = [(rel, yaml.safe_load(raw) or {}, raw) for rel, raw in files]
    return pf_mod._practice_facts(parsed, root)


def _steps_wf(*steps: str) -> tuple[str, str]:
    body = "".join(f"      - {s}\n" for s in steps)
    return ("ci.yml", PR_ON + "jobs:\n  test:\n    steps:\n" + body)


# ---------------------------------------------------------------------------
# 1. Dependency caching that is on by default, or not switched by `cache:`
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("manifest,content,step", [
    # setup-go caches by default from v4 (its `cache` input defaults to true)
    ("go.mod", "module x\n", "uses: actions/setup-go@v5"),
    # a SHA names no major: a default-on action counts as caching (ci-speedup)
    ("go.mod", "module x\n", f"uses: actions/setup-go@{SHA} # v5"),
    # setup-uv caches by default from v5 (`enable-cache: auto`)
    ("pyproject.toml", "[project]\nname='x'\n", "uses: astral-sh/setup-uv@v6"),
    # setup-ruby is switched by `bundler-cache`, not `cache`
    ("Gemfile", "source 'x'\n", "{uses: ruby/setup-ruby@v1, with: {bundler-cache: true}}"),
    # setup-gradle restores its own cache unless told not to
    ("build.gradle", "", "uses: gradle/actions/setup-gradle@v4"),
    ("build.gradle", "", f"uses: gradle/actions/setup-gradle@{SHA}"),
    # setup-node v5 caches with no `cache:` input when package.json names the manager
    ("package.json", '{"packageManager": "npm@10.9.0"}', "uses: actions/setup-node@v5"),
    ("package.json", '{"packageManager": "pnpm@9.1.0"}', "uses: actions/setup-node@v5"),
    # v6+: npm only, from devEngines.packageManager or packageManager
    ("package.json", '{"devEngines": {"packageManager": {"name": "npm"}}}',
     "uses: actions/setup-node@v6"),
])
def test_caching_on_by_default_passes(tmp_path, manifest, content, step):
    (tmp_path / manifest).write_text(content)
    f = facts_for(tmp_path, _steps_wf("uses: actions/checkout@v5", step, "run: make test"))[CACHE]
    assert f["state"] == "pass", f


@pytest.mark.parametrize("manifest,content,step", [
    # explicit opt-outs stay fails
    ("go.mod", "module x\n", "{uses: actions/setup-go@v5, with: {cache: false}}"),
    ("pyproject.toml", "[project]\nname='x'\n",
     "{uses: astral-sh/setup-uv@v6, with: {enable-cache: false}}"),
    ("Gemfile", "source 'x'\n", "{uses: ruby/setup-ruby@v1, with: {bundler-cache: false}}"),
    ("build.gradle", "", "{uses: gradle/actions/setup-gradle@v4, with: {cache-disabled: true}}"),
    ("package.json", '{"packageManager": "npm@10.9.0"}',
     "{uses: actions/setup-node@v5, with: {package-manager-cache: false}}"),
    # below the version where the default turned on
    ("go.mod", "module x\n", "uses: actions/setup-go@v3"),
    ("pyproject.toml", "[project]\nname='x'\n", "uses: astral-sh/setup-uv@v4"),
    ("Gemfile", "source 'x'\n", "uses: ruby/setup-ruby@v1"),
    ("package.json", '{"packageManager": "npm@10.9.0"}', "uses: actions/setup-node@v4"),
    # setup-node v5+ with no manager field, or a manager v6 does not auto-cache
    ("package.json", "{}", "uses: actions/setup-node@v5"),
    ("package.json", '{"packageManager": "pnpm@9.1.0"}', "uses: actions/setup-node@v6"),
    ("package.json", "not json", "uses: actions/setup-node@v5"),
    # a SHA-pinned setup-node: v5 credits pnpm, v6 does not — undecided, not credited
    ("package.json", '{"packageManager": "pnpm@9.1.0"}', f"uses: actions/setup-node@{SHA}"),
])
def test_caching_off_stays_a_fail(tmp_path, manifest, content, step):
    (tmp_path / manifest).write_text(content)
    f = facts_for(tmp_path, _steps_wf("uses: actions/checkout@v5", step, "run: make test"))[CACHE]
    assert f["state"] == "fail", f


def test_setup_node_auto_cache_needs_the_root_checkout(tmp_path):
    """setup-node reads `$GITHUB_WORKSPACE/package.json`. A checkout into a
    sub-path (or of another repository) means the root file this scan read is
    not the one setup-node reads, so the default is not credited."""
    (tmp_path / "package.json").write_text('{"packageManager": "npm@10.9.0"}')
    f = facts_for(tmp_path, _steps_wf("{uses: actions/checkout@v5, with: {path: src}}",
                                      "uses: actions/setup-node@v5", "run: npm test"))[CACHE]
    assert f["state"] == "fail", f


@pytest.mark.parametrize("steps", [
    # setup-node runs before the checkout: the workspace is empty when it looks
    ("uses: actions/setup-node@v5", "uses: actions/checkout@v5", "run: npm ci"),
    # no checkout at all in the job
    ("uses: actions/setup-node@v5", "run: npm ci"),
])
def test_setup_node_auto_cache_needs_a_finished_checkout(tmp_path, steps):
    """setup-node's automatic cache reads package.json when the step runs. With
    no root checkout finished before it, there is no file to read, so the
    default is not credited (ci-speedup's `_opt79_workspace_states`)."""
    (tmp_path / "package.json").write_text('{"packageManager": "npm@10.9.0"}')
    f = facts_for(tmp_path, _steps_wf(*steps))[CACHE]
    assert f["state"] == "fail", f


def test_setup_node_auto_cache_in_a_local_composite_is_credited(tmp_path):
    """A local composite action is read from the checked-out workspace, so the
    repository is already checked out when its steps run."""
    (tmp_path / "package.json").write_text('{"packageManager": "npm@10.9.0"}')
    act = tmp_path / ".github" / "actions" / "setup"
    act.mkdir(parents=True)
    (act / "action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - uses: actions/setup-node@v5\n")
    f = facts_for(tmp_path, _steps_wf("uses: actions/checkout@v5",
                                      "uses: ./.github/actions/setup", "run: npm ci"))[CACHE]
    assert f["state"] == "pass", f


# ---------------------------------------------------------------------------
# 2. A matrix generated at runtime
# ---------------------------------------------------------------------------

def _dynamic_matrix_wf(job_id: str) -> tuple[str, str]:
    return ("ci.yml", PR_ON + "jobs:\n"
            "  plan:\n    steps:\n      - run: echo m=[1,2,3] >> $GITHUB_OUTPUT\n"
            f"  {job_id}:\n    needs: plan\n"
            "    strategy:\n      matrix: ${{ fromJSON(needs.plan.outputs.m) }}\n"
            "    steps:\n      - run: pytest\n")


def test_a_test_job_with_a_generated_matrix_passes(tmp_path):
    f = facts_for(tmp_path, _dynamic_matrix_wf("test"))[SHARD]
    assert f["state"] == "pass", f


def test_a_generated_matrix_on_a_non_test_job_is_not_credited(tmp_path):
    """The job-name rule is unchanged: a non-test job's matrix only counts by a
    shard-like axis, and a generated matrix shows no axis to read."""
    wf = ("ci.yml", PR_ON + "jobs:\n"
          "  build:\n    strategy:\n      matrix: ${{ fromJSON(needs.plan.outputs.m) }}\n"
          "    steps: []\n"
          "  test:\n    steps:\n      - run: pytest\n")
    f = facts_for(tmp_path, wf)[SHARD]
    assert f["state"] == "fail", f


def test_mapping_matrices_behave_as_before(tmp_path):
    no_shard = ("ci.yml", PR_ON + "jobs:\n"
                "  build:\n    strategy:\n      matrix:\n        os: [ubuntu, macos]\n"
                "    steps: []\n"
                "  test:\n    steps:\n      - run: pytest\n")
    assert facts_for(tmp_path, no_shard)[SHARD]["state"] == "fail"
    shard = ("ci.yml", PR_ON + "jobs:\n"
             "  run:\n    strategy:\n      matrix:\n        shard: [1, 2]\n    steps: []\n")
    assert facts_for(tmp_path, shard)[SHARD]["state"] == "pass"


# ---------------------------------------------------------------------------
# 3. Job timeouts on fully delegated CI
# ---------------------------------------------------------------------------

def test_all_jobs_delegated_to_remote_reusables_is_not_applicable(tmp_path):
    wf = ("ci.yml", PR_ON + "jobs:\n"
          "  ci:\n    uses: my-org/workflows/.github/workflows/ci.yml@v1\n"
          "  lint:\n    uses: my-org/workflows/.github/workflows/lint.yml@v1\n")
    f = facts_for(tmp_path, wf)[TIMEOUTS]
    assert f["state"] == "not_applicable", f
    assert "my-org/workflows" in f["evidence"], f["evidence"]


def test_a_local_job_beside_a_remote_reusable_is_still_graded(tmp_path):
    wf = ("ci.yml", PR_ON + "jobs:\n"
          "  ci:\n    uses: my-org/workflows/.github/workflows/ci.yml@v1\n"
          "  notify:\n    steps:\n      - run: echo done\n")
    f = facts_for(tmp_path, wf)[TIMEOUTS]
    assert f["state"] == "fail", f


def test_a_local_reusable_callee_is_graded_on_its_own_jobs(tmp_path):
    """A job calling a LOCAL reusable workflow cannot set timeout-minutes
    either, but the callee is in this repo and its jobs can; they decide."""
    caller = ("ci.yml", PR_ON + "jobs:\n  t:\n    uses: ./.github/workflows/t.yml\n")
    callee = ("t.yml", "on: workflow_call\njobs:\n  unit:\n    steps:\n      - run: pytest\n")
    assert facts_for(tmp_path, caller, callee)[TIMEOUTS]["state"] == "fail"


# ---------------------------------------------------------------------------
# 4. A full-history checkout STEP that never runs on a pull request
# ---------------------------------------------------------------------------

def _step_if_wf(cond: str, on: str = "on: [pull_request, push]\n") -> tuple[str, str]:
    return ("ci.yml", on + "jobs:\n  test:\n    steps:\n"
            "      - uses: actions/checkout@v4\n"
            f"        if: {json.dumps(cond)}\n"
            "        with:\n          fetch-depth: 0\n"
            "      - uses: actions/checkout@v4\n"
            "        if: github.event_name == 'pull_request'\n"
            "      - run: pytest\n")


@pytest.mark.parametrize("cond", [
    "github.event_name == 'push'",
    "${{ github.event_name != 'pull_request' }}",
])
def test_a_step_if_that_excludes_pull_requests_is_off_the_pr_path(tmp_path, cond):
    f = facts_for(tmp_path, _step_if_wf(cond))[SHALLOW]
    assert f["state"] == "pass", f
    assert "step" in f["evidence"], f["evidence"]


@pytest.mark.parametrize("cond", [
    "github.ref == 'refs/heads/main'",
    "success() && github.event_name == 'push'",
    "github.event_name == 'push' || github.event_name == 'pull_request'",
    "false",
])
def test_a_step_if_that_cannot_be_resolved_still_counts(tmp_path, cond):
    f = facts_for(tmp_path, _step_if_wf(cond))[SHALLOW]
    assert f["state"] == "fail", f


def test_a_step_if_negating_pull_request_still_counts_under_pull_request_target(tmp_path):
    f = facts_for(tmp_path, _step_if_wf("github.event_name != 'pull_request'",
                                        on="on: [pull_request_target]\n"))[SHALLOW]
    assert f["state"] == "fail", f
