"""Four shapes where a repository that follows the practice was FAILED.

Each cell below is a repository doing the right thing, paired with a negative
control that must keep failing, so no fix can over-credit:

1. Dependency caching that is ON BY DEFAULT (setup-go v4+, setup-uv v5+,
   setup-node v5+ with a package-manager field, gradle/actions/setup-gradle,
   gradle/gradle-build-action, bahmutov/npm-install) or switched on by an
   input other than `cache:` (setup-ruby's `bundler-cache`).
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
    """The facts the current engine gives these files. (The comparison with
    the pre-OD-CS23 engine runs over an explicit case list in
    `test_ci_score_no_score_drops.py`, not here.)"""
    parsed = [(rel, yaml.safe_load(raw) or {}, raw) for rel, raw in files]
    return pf_mod._practice_facts(parsed, root)


def _manifest(root: Path, name: str, content: str) -> None:
    """Write a manifest; a Go module also gets the `go.sum` setup-go's default
    cache keys on (without it, setup-go restores nothing)."""
    (root / name).write_text(content)
    if name == "go.mod":
        (root / "go.sum").write_text("")


def _steps_wf(*steps: str, runs_on: str = "ubuntu-latest") -> tuple[str, str]:
    body = "".join(f"      - {s}\n" for s in steps)
    return ("ci.yml", PR_ON + f"jobs:\n  test:\n    runs-on: {runs_on}\n    steps:\n" + body)


# ---------------------------------------------------------------------------
# 1. Dependency caching that is on by default, or not switched by `cache:`
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("manifest,content,step", [
    # setup-go caches by default from v4 (its `cache` input defaults to true)
    ("go.mod", "module x\n", "uses: actions/setup-go@v5"),
    # a SHA names no major; its `# v5` version comment names a default-on
    # release, so the comment rule leaves the credit in place
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
    _manifest(tmp_path, manifest, content)
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
    _manifest(tmp_path, manifest, content)
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


@pytest.mark.parametrize("manifest,step,state", [
    # a SHA pin whose version comment names a release from before the default
    # turned on earns nothing (owner decision, OD-CS23)
    ("go.mod", f"uses: actions/setup-go@{SHA} # v3.5.0", "fail"),
    ("go.mod", f"uses: actions/setup-go@{SHA}  #v3", "fail"),
    ("pyproject.toml", f"uses: astral-sh/setup-uv@{SHA} # v4.2.0", "fail"),
    # a comment naming a default-on release, or no comment, credits as before
    ("go.mod", f"uses: actions/setup-go@{SHA} # v4", "pass"),
    ("go.mod", f"uses: actions/setup-go@{SHA} # v5.0.2", "pass"),
    ("go.mod", f"uses: actions/setup-go@{SHA}", "pass"),
    # an explicit cache input decides whatever the comment says
    ("go.mod", f"{{uses: actions/setup-go@{SHA}, with: {{cache: true}}}} # v3", "pass"),
])
def test_a_sha_pin_version_comment_decides_the_default(tmp_path, manifest, step, state):
    _manifest(tmp_path, manifest, "module x\n" if manifest == "go.mod" else "[project]\n")
    f = facts_for(tmp_path, _steps_wf("uses: actions/checkout@v5", step, "run: make test"))[CACHE]
    assert f["state"] == state, f


@pytest.mark.parametrize("runs_on,step,state", [
    # setup-uv's default (`enable-cache: auto`) caches only on GitHub-hosted
    # runners: an input-free step is credited only on a literal hosted label
    ("[self-hosted, linux]", "uses: astral-sh/setup-uv@v5", "fail"),
    ("my-runner", "uses: astral-sh/setup-uv@v6", "fail"),
    ("${{ matrix.os }}", "uses: astral-sh/setup-uv@v5", "fail"),
    ("{group: big}", "uses: astral-sh/setup-uv@v5", "fail"),
    ("[ubuntu-latest, self-hosted]", "uses: astral-sh/setup-uv@v5", "fail"),
    ("ubuntu-latest", "uses: astral-sh/setup-uv@v5", "pass"),
    ("windows-2022", "uses: astral-sh/setup-uv@v6", "pass"),
    ("[macos-14]", "uses: astral-sh/setup-uv@v5", "pass"),
    ("ubuntu-24.04", f"uses: astral-sh/setup-uv@{SHA}", "pass"),
    # an explicit enable-cache: true caches on any runner
    ("[self-hosted, linux]", "{uses: astral-sh/setup-uv@v5, with: {enable-cache: true}}", "pass"),
    # other default-on actions are not runner-gated
    ("[self-hosted, linux]", "uses: actions/setup-go@v5", "pass"),
])
def test_setup_uv_default_is_credited_only_on_github_hosted_runners(tmp_path, runs_on, step, state):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    _manifest(tmp_path, "go.mod", "module x\n")
    f = facts_for(tmp_path, _steps_wf("uses: actions/checkout@v5", step, "run: make test",
                                      runs_on=runs_on))[CACHE]
    assert f["state"] == state, f


def test_the_cache_fix_names_setup_uvs_input():
    """An input-free setup-uv that earns no credit is fixed by one input."""
    rr = _load("ci_score_render_report_undeserved_fails", "scripts/render_report.py")
    recipe = rr._FIX_TABLE["ci.cache.dependency-cache"]["recipe"]
    assert "enable-cache: true" in recipe, recipe
    assert "self-hosted" not in recipe.lower(), recipe


def test_setup_uv_default_in_a_composite_is_not_credited(tmp_path):
    """A composite action runs on whatever runner calls it, so its runner is
    not known from its own file: the input-free default earns nothing."""
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    act = tmp_path / ".github" / "actions" / "setup"
    act.mkdir(parents=True)
    (act / "action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - uses: astral-sh/setup-uv@v5\n")
    f = facts_for(tmp_path, _steps_wf("uses: actions/checkout@v5",
                                      "uses: ./.github/actions/setup", "run: uv sync"))[CACHE]
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


# ---------------------------------------------------------------------------
# 5. Default-on credit withheld: version comments, runners, go.sum, workspace
# ---------------------------------------------------------------------------

def _cache(root: Path, *steps: str, runs_on: str = "ubuntu-latest") -> dict:
    return facts_for(root, _steps_wf(*steps, runs_on=runs_on))[CACHE]


@pytest.mark.parametrize("step,state", [
    # every common pin-comment spelling names the release it pins
    (f"uses: actions/setup-go@{SHA} # tag=v3.5.0", "fail"),
    (f"uses: actions/setup-go@{SHA} # ratchet:actions/setup-go@v3", "fail"),
    (f"uses: actions/setup-go@{SHA} # pin@v3", "fail"),
    (f'uses: "actions/setup-go@{SHA}" # v3', "fail"),
    (f"uses: 'actions/setup-go@{SHA}' # v3.5.0", "fail"),
    # a flow-mapping step carries its comment after the closing brace
    (f"{{uses: actions/setup-go@{SHA}}} # v3", "fail"),
    (f"{{uses: actions/setup-go@{SHA}, with: {{go-version: '1.22'}}}} # v3", "fail"),
    # a comment that is there but names no release cannot vouch for the pin
    (f"uses: actions/setup-go@{SHA} # pinned by the platform team", "fail"),
    # controls
    (f"uses: actions/setup-go@{SHA} # v3.5.0", "fail"),
    (f"uses: actions/setup-go@{SHA} # v4", "pass"),
    (f"uses: actions/setup-go@{SHA} # tag=v5.0.2", "pass"),
    (f"uses: actions/setup-go@{SHA} # ratchet:actions/setup-go@v5", "pass"),
    (f"{{uses: actions/setup-go@{SHA}}} # v5", "pass"),
    (f"uses: actions/setup-go@{SHA}", "pass"),
])
def test_every_version_comment_spelling_is_read(tmp_path, step, state):
    _manifest(tmp_path, "go.mod", "module x\n")
    assert _cache(tmp_path, "uses: actions/checkout@v5", step, "run: go test")["state"] == state


def test_a_version_comment_in_a_composite_is_read(tmp_path):
    _manifest(tmp_path, "go.mod", "module x\n")
    act = tmp_path / ".github" / "actions" / "setup"
    act.mkdir(parents=True)
    (act / "action.yml").write_text(
        f"runs:\n  using: composite\n  steps:\n    - uses: actions/setup-go@{SHA} # v3\n")
    f = _cache(tmp_path, "uses: actions/checkout@v5", "uses: ./.github/actions/setup",
               "run: go test")
    assert f["state"] == "fail", f


def test_a_composite_action_file_is_read_once(tmp_path, monkeypatch):
    """The composite's raw text (for version comments) comes from the same
    read that parsed it: no second read that could see different bytes."""
    _manifest(tmp_path, "go.mod", "module x\n")
    act = tmp_path / ".github" / "actions" / "setup"
    act.mkdir(parents=True)
    (act / "action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - uses: actions/setup-go@v5\n")
    reads: list[str] = []
    real = Path.read_text

    def counting(self, *a, **k):
        if self.name == "action.yml":
            reads.append(str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting)
    pf_mod._practice_facts([("ci.yml", {"on": "pull_request", "jobs": {}}, "")], tmp_path)
    assert len(reads) == len(set(reads)), reads


@pytest.mark.parametrize("pkg,steps,state", [
    # `package-manager-cache: ${{ … }}` decides nothing by itself: read as its
    # default (`true`), the automatic cache still needs a manager to key on
    ('{"name": "x"}', ("uses: actions/checkout@v5",), "fail"),
    ('{"name": "x"}', (), "fail"),
    ('{"packageManager": "npm@10.9.0"}', (), "fail"),
    # control: a manager field after a root checkout caches
    ('{"packageManager": "npm@10.9.0"}', ("uses: actions/checkout@v5",), "pass"),
])
def test_an_expression_package_manager_cache_is_read_as_its_default(tmp_path, pkg, steps, state):
    (tmp_path / "package.json").write_text(pkg)
    step = ('{uses: actions/setup-node@v5, with: '
            '{package-manager-cache: "${{ inputs.cache }}"}}')
    assert _cache(tmp_path, *steps, step, "run: npm ci")["state"] == state


@pytest.mark.parametrize("files,steps,state", [
    # setup-go's default cache keys on go.sum in the checked-out workspace
    ({"go.mod": "module x\n"}, ("uses: actions/setup-go@v5", "uses: actions/checkout@v5"), "fail"),
    ({"go.mod": "module x\n"}, ("uses: actions/checkout@v5", "uses: actions/setup-go@v5"), "fail"),
    ({"go.mod": "module x\n", "go.sum": ""},
     ("uses: actions/setup-go@v5", "uses: actions/checkout@v5"), "fail"),
    ({"go.mod": "module x\n", "go.sum": ""},
     ("{uses: actions/checkout@v5, with: {repository: o/r}}", "uses: actions/setup-go@v5"), "fail"),
    # controls
    ({"go.mod": "module x\n", "go.sum": ""},
     ("uses: actions/checkout@v5", "uses: actions/setup-go@v5"), "pass"),
    ({"svc/go.sum": ""},
     ("uses: actions/checkout@v5",
      "{uses: actions/setup-go@v5, with: {cache-dependency-path: svc/go.sum}}"), "pass"),
    ({"svc/go.sum": ""},
     ("uses: actions/checkout@v5",
      "{uses: actions/setup-go@v5, with: {cache-dependency-path: 'svc/*.sum'}}"), "pass"),
    ({"go.sum": ""},
     ("uses: actions/checkout@v5",
      "{uses: actions/setup-go@v5, with: {cache-dependency-path: missing/go.sum}}"), "fail"),
])
def test_setup_go_default_needs_a_checkout_and_a_go_sum(tmp_path, files, steps, state):
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    assert _cache(tmp_path, *steps, "run: go build")["state"] == state


@pytest.mark.parametrize("runs_on,state", [
    ("ubuntu-selfhosted-pool", "fail"),
    ("self-hosted", "fail"),
    ("windows-builders", "fail"),
    ("macos-m1-farm", "fail"),
    ("ubuntu-latest", "pass"),
    ("ubuntu-24.04", "pass"),
    ("ubuntu-24.04-arm", "pass"),
    ("ubuntu-slim", "pass"),
    ("macos-15", "pass"),
    ("macos-15-intel", "pass"),
    ("macos-15-xlarge", "pass"),
    ("windows-2025", "pass"),
    ("windows-11-arm", "pass"),
])
def test_only_published_github_hosted_labels_earn_setup_uvs_default(tmp_path, runs_on, state):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    f = _cache(tmp_path, "uses: actions/checkout@v5", "uses: astral-sh/setup-uv@v6",
               "run: uv sync", runs_on=runs_on)
    assert f["state"] == state, f


@pytest.mark.parametrize("files,step", [
    # action inputs are case-insensitive: `Cache: false` is `cache: false`
    ({"go.mod": "module x\n", "go.sum": ""}, "{uses: actions/setup-go@v5, with: {Cache: false}}"),
    ({"pyproject.toml": "[project]\n"}, "{uses: astral-sh/setup-uv@v6, with: {Enable-Cache: false}}"),
    ({"build.gradle": ""}, "{uses: gradle/actions/setup-gradle@v4, with: {Cache-Disabled: true}}"),
    ({"package.json": '{"packageManager": "npm@10.9.0"}'},
     "{uses: actions/setup-node@v5, with: {Package-Manager-Cache: false}}"),
])
def test_input_names_are_read_case_insensitively(tmp_path, files, step):
    for rel, text in files.items():
        (tmp_path / rel).write_text(text)
    f = _cache(tmp_path, "uses: actions/checkout@v5", step, "run: make")
    assert f["state"] == "fail", f


@pytest.mark.parametrize("pkg,step,reason", [
    ("not json", "uses: actions/setup-node@v5", "package.json is not valid JSON"),
    ("[1, 2]", "uses: actions/setup-node@v5", "package.json is not a JSON object"),
    ('{"packageManager": "pnpm@9.1.0"}', f"uses: actions/setup-node@{SHA}",
     "a SHA pin names no release, and setup-node v5 and v6 decide this package.json differently"),
])
def test_an_unresolved_default_is_named_in_the_fail(tmp_path, pkg, step, reason):
    (tmp_path / "package.json").write_text(pkg)
    f = _cache(tmp_path, "uses: actions/checkout@v5", step, "run: npm ci")
    assert f["state"] == "fail", f
    ref = step.split("@", 1)[1]
    assert f"setup-node@{ref} default cache could not be resolved: {reason}" in f["evidence"], f
    assert f["files"] == ["ci.yml"], f
    raw = _steps_wf("uses: actions/checkout@v5", step)[1]
    recorded = pf_mod._unresolved_setup_defaults([("ci.yml", yaml.safe_load(raw), raw)], tmp_path)
    assert recorded == [{"file": "ci.yml", "step": f"actions/setup-node@{ref}",
                         "reason": reason}], recorded


def test_an_unresolved_default_is_recorded_in_data_sources(tmp_path):
    import subprocess
    cc = _load("ci_score_collect_config_undeserved_fails", "scripts/collect_config.py")
    root = tmp_path / "repo"
    (root / ".github" / "workflows").mkdir(parents=True)
    wf = _steps_wf("uses: actions/checkout@v5", "uses: actions/setup-node@v5", "run: npm ci")[1]
    (root / ".github" / "workflows" / "ci.yml").write_text(wf)
    (root / "package.json").write_text("{nope")
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(root),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)
    doc, code = cc.collect(root)
    assert code == 0
    assert doc["data_sources"]["unresolved_setup_defaults"] == [
        {"file": ".github/workflows/ci.yml", "step": "actions/setup-node@v5",
         "reason": "package.json is not valid JSON"}]


def test_a_resolved_repo_records_nothing(tmp_path):
    (tmp_path / "package.json").write_text('{"packageManager": "npm@10.9.0"}')
    raw = _steps_wf("uses: actions/checkout@v5", "uses: actions/setup-node@v5")[1]
    assert pf_mod._unresolved_setup_defaults([("ci.yml", yaml.safe_load(raw), raw)],
                                             tmp_path) == []


@pytest.mark.parametrize("comment,state", [
    # owner decision 3 (OD-CS23): a version comment may settle a SHA pin the
    # v5 and v6 rules disagree on, but only when every release it names caches
    ("# v5.0.0", "pass"),
    ("# tag=v5", "pass"),
    ("# v6.0.0", "fail"),
    ("", "fail"),
    ("# pinned", "fail"),
])
def test_a_version_comment_settles_an_undecided_setup_node_pin(tmp_path, comment, state):
    (tmp_path / "package.json").write_text('{"packageManager": "yarn@4.1.0"}')
    f = _cache(tmp_path, "uses: actions/checkout@v5",
               f"uses: actions/setup-node@{SHA} {comment}".rstrip(), "run: yarn install")
    assert f["state"] == state, f


def test_a_comment_cannot_settle_a_pin_without_a_root_checkout(tmp_path):
    (tmp_path / "package.json").write_text('{"packageManager": "yarn@4.1.0"}')
    f = _cache(tmp_path, f"uses: actions/setup-node@{SHA} # v5.0.0", "run: yarn install")
    assert f["state"] == "fail", f


_RACING_CHECKOUTS = {
    # a checkout in the same `parallel:` group may still be running
    "same group": ("on: pull_request\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
                   "      - parallel:\n"
                   "          - uses: actions/checkout@v5\n"
                   "          - uses: {action}\n"
                   "      - run: make\n"),
    # a background checkout may still be running when the next step starts
    "background": ("on: pull_request\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
                   "      - uses: actions/checkout@v5\n        background: true\n"
                   "      - uses: {action}\n"
                   "      - run: make\n"),
    # control: a checkout in an EARLIER group has finished (implicit wait)
    "earlier group": ("on: pull_request\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
                      "      - parallel:\n"
                      "          - uses: actions/checkout@v5\n"
                      "          - run: echo hi\n"
                      "      - uses: {action}\n"
                      "      - run: make\n"),
}


@pytest.mark.parametrize("shape,state", [("same group", "fail"), ("background", "fail"),
                                         ("earlier group", "pass")])
@pytest.mark.parametrize("action", ["actions/setup-node@v5", "actions/setup-go@v5"])
def test_a_checkout_that_may_still_be_running_does_not_count(tmp_path, shape, state, action):
    (tmp_path / "package.json").write_text('{"packageManager": "npm@10.9.0"}')
    _manifest(tmp_path, "go.mod", "module x\n")
    raw = _RACING_CHECKOUTS[shape].replace("{action}", action)
    f = facts_for(tmp_path, ("ci.yml", raw))[CACHE]
    assert f["state"] == state, f


# --- edge cases --------------------------------------------------------------

def test_one_unconditional_full_history_checkout_still_counts(tmp_path):
    wf = ("ci.yml", "on: [pull_request, push]\njobs:\n  test:\n    steps:\n"
          "      - uses: actions/checkout@v4\n"
          "        if: github.event_name == 'push'\n"
          "        with:\n          fetch-depth: 0\n"
          "      - uses: actions/checkout@v4\n"
          "        with:\n          fetch-depth: 0\n"
          "      - run: pytest\n")
    assert facts_for(tmp_path, wf)[SHALLOW]["state"] == "fail"


def test_a_checkout_of_another_repository_does_not_feed_setup_node(tmp_path):
    (tmp_path / "package.json").write_text('{"packageManager": "npm@10.9.0"}')
    f = _cache(tmp_path, "{uses: actions/checkout@v5, with: {repository: o/r}}",
               "uses: actions/setup-node@v5", "run: npm ci")
    assert f["state"] == "fail", f


def test_a_local_reusable_chain_keeps_job_timeouts_graded(tmp_path):
    caller = ("ci.yml", PR_ON + "jobs:\n  t:\n    uses: ./.github/workflows/a.yml\n")
    mid = ("a.yml", "on: workflow_call\njobs:\n  b:\n    uses: ./.github/workflows/b.yml\n")
    assert facts_for(tmp_path, caller, mid)[TIMEOUTS]["state"] == "fail"


def test_a_literal_string_matrix_is_not_credited(tmp_path):
    wf = ("ci.yml", PR_ON + "jobs:\n  test:\n    strategy:\n      matrix: not-an-expression\n"
          "    steps:\n      - run: pytest\n")
    assert facts_for(tmp_path, wf)[SHARD]["state"] == "fail"


# ---------------------------------------------------------------------------
# 6. Input-name case, and naming every withheld default with its reason
# ---------------------------------------------------------------------------

def _write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def _recorded(root: Path, raw: str, rel: str = "ci.yml") -> list:
    return pf_mod._unresolved_setup_defaults([(rel, yaml.safe_load(raw), raw)], root)


_YARN = '{"packageManager": "yarn@4.1.0"}'
_PNPM = '{"packageManager": "pnpm@9.1.0"}'
_NPM = '{"packageManager": "npm@10.9.0"}'
_DEV_NPM = '{"devEngines": {"packageManager": {"name": "npm"}}}'
_GO = {"go.mod": "module x\n", "go.sum": ""}


@pytest.mark.parametrize("files,step,runs_on", [
    # action inputs are case-insensitive in BOTH directions: an opt-in written
    # `Cache: npm` is the same input as `cache: npm`
    ({"package.json": "{}"}, "{uses: actions/setup-node@v4, with: {Cache: npm}}", "ubuntu-latest"),
    ({"pyproject.toml": "[project]\n"},
     "{uses: astral-sh/setup-uv@v4, with: {Enable-Cache: true}}", "ubuntu-latest"),
    ({"pyproject.toml": "[project]\n"},
     "{uses: astral-sh/setup-uv@v4, with: {Enable-Cache: true}}", "self-hosted"),
    (_GO, "{uses: actions/setup-go@v3, with: {Cache: true}}", "ubuntu-latest"),
    ({"requirements.txt": "x\n"}, "{uses: actions/setup-python@v5, with: {Cache: pip}}",
     "ubuntu-latest"),
    ({"package.json": _YARN}, f"{{uses: actions/setup-node@{SHA}, with: {{Cache: yarn}}}}",
     "ubuntu-latest"),
    # an explicit input decides whatever a comment beside the pin says
    ({"package.json": _YARN},
     f"{{uses: actions/setup-node@{SHA}, with: {{Cache: yarn}}}} # pinned", "ubuntu-latest"),
])
def test_an_explicit_cache_input_in_any_case_passes(tmp_path, files, step, runs_on):
    _write(tmp_path, files)
    f = _cache(tmp_path, "uses: actions/checkout@v5", step, "run: make", runs_on=runs_on)
    assert f["state"] == "pass", f


def test_a_comment_that_resolves_a_pin_to_off_records_nothing(tmp_path):
    """`@<sha> # v6` with yarn: the comment decides it (v6 does not cache
    yarn), so it is a plain off, never "could not be resolved"."""
    _write(tmp_path, {"package.json": _YARN})
    raw = _steps_wf("uses: actions/checkout@v5", f"uses: actions/setup-node@{SHA} # v6",
                    "run: yarn")[1]
    f = facts_for(tmp_path, ("ci.yml", raw))[CACHE]
    assert f["state"] == "fail", f
    assert "could not be resolved" not in f["evidence"], f
    assert _recorded(tmp_path, raw) == []


def test_a_branch_ref_is_not_called_a_sha_pin(tmp_path):
    _write(tmp_path, {"package.json": _PNPM})
    raw = _steps_wf("uses: actions/checkout@v5", "uses: actions/setup-node@main", "run: pnpm i")[1]
    f = facts_for(tmp_path, ("ci.yml", raw))[CACHE]
    assert f["state"] == "fail", f
    assert "SHA pin" not in f["evidence"], f
    assert "a branch or SHA ref names no release" in f["evidence"], f
    assert [u["reason"] for u in _recorded(tmp_path, raw)] == [
        "a branch or SHA ref names no release, and setup-node v5 and v6 decide this "
        "package.json differently"]


@pytest.mark.parametrize("files,steps,runs_on,reason", [
    # a version comment that names no release
    (_GO, ("uses: actions/checkout@v5",
           f"uses: actions/setup-go@{SHA} # pinned by the platform team"),
     "ubuntu-latest", "version comment 'pinned by the platform team' names no release"),
    # setup-uv's `auto` default on a runner this scan cannot read
    ({"pyproject.toml": "[project]\n"},
     ("uses: actions/checkout@v5", "uses: astral-sh/setup-uv@v6"),
     "${{ matrix.os }}", "runs-on is an expression"),
    ({"pyproject.toml": "[project]\n"},
     ("uses: actions/checkout@v5", "uses: astral-sh/setup-uv@v6"),
     "{group: big}", "runs-on is a runner group or object"),
    # a cache-dependency-path this scan cannot check
    ({"go.mod": "module x\n"},
     ("uses: actions/checkout@v5",
      "{uses: actions/setup-go@v5, with: {cache-dependency-path: '${{ inputs.p }}'}}"),
     "ubuntu-latest", "cache-dependency-path is an expression"),
    ({"go.mod": "module x\n"},
     ("uses: actions/checkout@v5",
      "{uses: actions/setup-go@v5, with: {cache-dependency-path: /abs/go.sum}}"),
     "ubuntu-latest", "cache-dependency-path '/abs/go.sum' is an absolute path"),
    ({"go.mod": "module x\n"},
     ("uses: actions/checkout@v5",
      "{uses: actions/setup-go@v5, with: {cache-dependency-path: ../x/go.sum}}"),
     "ubuntu-latest", "cache-dependency-path '../x/go.sum' leaves the repository"),
    ({"go.mod": "module x\n"},
     ("uses: actions/checkout@v5",
      "{uses: actions/setup-go@v5, with: {cache-dependency-path: '!vendor/**'}}"),
     "ubuntu-latest", "cache-dependency-path has only exclusion (!) patterns"),
    # a checkout that may still be running, or went elsewhere
    (_GO, ("{uses: actions/checkout@v5, background: true}", "uses: actions/setup-go@v5"),
     "ubuntu-latest", "a checkout may still be running"),
    ({"package.json": _NPM},
     ("{uses: actions/checkout@v5, background: true}", "uses: actions/setup-node@v5"),
     "ubuntu-latest", "a checkout may still be running"),
    ({"package.json": _NPM},
     ("{parallel: [{uses: actions/checkout@v5}, {uses: actions/setup-node@v5}]}",),
     "ubuntu-latest", "a checkout may still be running"),
    ({"package.json": _NPM},
     ("{uses: actions/checkout@v5, with: {path: src}}", "uses: actions/setup-node@v5"),
     "ubuntu-latest", "the checkout before this step went to a sub-path or another repository"),
])
def test_every_withhold_names_its_reason(tmp_path, files, steps, runs_on, reason):
    _write(tmp_path, files)
    raw = _steps_wf(*steps, "run: make", runs_on=runs_on)[1]
    f = facts_for(tmp_path, ("ci.yml", raw))[CACHE]
    assert f["state"] == "fail", f
    assert reason in f["evidence"], f
    rec = _recorded(tmp_path, raw)
    assert len(rec) == 1 and reason in rec[0]["reason"], rec
    assert f["files"] == ["ci.yml"], f


@pytest.mark.parametrize("files,steps", [
    # no checkout at all: nothing to read, a plain off
    ({"package.json": _NPM}, ("uses: actions/setup-node@v5",)),
    (_GO, ("uses: actions/setup-go@v5",)),
    # an invalid package.json is never blamed when no checkout has run
    ({"package.json": "not json"}, ("uses: actions/setup-node@v5", "uses: actions/checkout@v5")),
    # a resolved pre-default comment is a plain off
    (_GO, ("uses: actions/checkout@v5", f"uses: actions/setup-go@{SHA} # v3")),
])
def test_a_plain_off_names_no_reason(tmp_path, files, steps):
    _write(tmp_path, files)
    raw = _steps_wf(*steps, "run: make")[1]
    f = facts_for(tmp_path, ("ci.yml", raw))[CACHE]
    assert f["state"] == "fail", f
    assert "package.json" not in f["evidence"] and ";" not in f["evidence"], f
    assert _recorded(tmp_path, raw) == []


@pytest.mark.parametrize("files,step,reason", [
    # a date or a version range is not a release
    ({"package.json": _DEV_NPM}, f"uses: actions/setup-node@{SHA} # 2024-05-01",
     "version comment '2024-05-01' names no release"),
    (_GO, f"uses: actions/setup-go@{SHA} # 2023-05-01",
     "version comment '2023-05-01' names no release"),
    (_GO, f"uses: actions/setup-go@{SHA} # 20.x", "version comment '20.x' names no release"),
    # a major no release of the action has ever had
    (_GO, f"uses: actions/setup-go@{SHA} # v99", "version comment 'v99' names no release"),
])
def test_a_comment_that_is_not_a_release_withholds_and_is_named(tmp_path, files, step, reason):
    _write(tmp_path, files)
    raw = _steps_wf("uses: actions/checkout@v5", step, "run: make")[1]
    f = facts_for(tmp_path, ("ci.yml", raw))[CACHE]
    assert f["state"] == "fail", f
    assert reason in f["evidence"], f
    assert [u["reason"] for u in _recorded(tmp_path, raw)] == [reason]


@pytest.mark.parametrize("comment", ["# pin@v5", "# actions/setup-go@v5", "# @v5", "# V5"])
def test_more_release_comment_spellings_pass(tmp_path, comment):
    _write(tmp_path, _GO)
    f = _cache(tmp_path, "uses: actions/checkout@v5", f"uses: actions/setup-go@{SHA} {comment}",
               "run: go test")
    assert f["state"] == "pass", f


def test_one_pre_default_comment_on_a_shared_pin_withholds(tmp_path):
    """The same SHA commented `# v5` on one line and `# v3` on another: every
    commented release must cache, so it is not credited."""
    _write(tmp_path, _GO)
    f = _cache(tmp_path, "uses: actions/checkout@v5", f"uses: actions/setup-go@{SHA} # v5",
               f"uses: actions/setup-go@{SHA} # v3", "run: go test")
    assert f["state"] == "fail", f


def test_a_string_true_background_checkout_races(tmp_path):
    _write(tmp_path, _GO)
    f = _cache(tmp_path, '{uses: actions/checkout@v5, background: "true"}',
               "uses: actions/setup-go@v5", "run: go test")
    assert f["state"] == "fail", f


def test_a_mixed_case_checkout_counts(tmp_path):
    _write(tmp_path, {"package.json": _NPM})
    f = _cache(tmp_path, "uses: Actions/Checkout@v5", "uses: actions/setup-node@v5", "run: npm ci")
    assert f["state"] == "pass", f


@pytest.mark.parametrize("dep_path,state", [
    ('"missing/go.sum\\nsvc/go.sum"', "pass"),
    ("../x/go.sum", "fail"),
])
def test_setup_go_dependency_paths(tmp_path, dep_path, state):
    root = tmp_path / "repo"
    _write(root, {"go.mod": "module x\n", "svc/go.sum": ""})
    _write(tmp_path, {"x/go.sum": ""})   # outside the repository
    f = _cache(root, "uses: actions/checkout@v5",
               f"{{uses: actions/setup-go@v5, with: {{cache-dependency-path: {dep_path}}}}}",
               "run: go test")
    assert f["state"] == state, f


@pytest.mark.parametrize("runs_on", ["Ubuntu-Latest", "xcode-27"])
def test_more_hosted_labels_earn_setup_uvs_default(tmp_path, runs_on):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    f = _cache(tmp_path, "uses: actions/checkout@v5", "uses: astral-sh/setup-uv@v6",
               "run: uv sync", runs_on=runs_on)
    assert f["state"] == "pass", f


def test_four_unresolved_defaults_name_three_and_count_the_rest(tmp_path):
    (tmp_path / "package.json").write_text("not json")
    files = [(f"w{i}.yml", _steps_wf("uses: actions/checkout@v5",
                                     "uses: actions/setup-node@v5")[1]) for i in range(4)]
    f = facts_for(tmp_path, *files)[CACHE]
    assert f["state"] == "fail", f
    assert f["evidence"].count("could not be resolved") == 3, f
    assert f["evidence"].endswith(" (and 1 more)"), f


def test_the_same_step_in_two_jobs_is_recorded_once(tmp_path):
    (tmp_path / "package.json").write_text("not json")
    job = ("    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v5\n"
           "      - uses: actions/setup-node@v5\n")
    raw = PR_ON + "jobs:\n  a:\n" + job + "  b:\n" + job
    assert len(_recorded(tmp_path, raw)) == 1


def test_a_bom_package_json_earns_no_default_cache(tmp_path):
    """A byte-order mark is decided, not undecided: setup-node's parse rejects
    the file, so its automatic cache is off, and the evidence says so."""
    (tmp_path / "package.json").write_bytes(b'\xef\xbb\xbf{"packageManager": "npm@10.9.0"}')
    f = _cache(tmp_path, "uses: actions/checkout@v5", "uses: actions/setup-node@v5", "run: npm ci")
    assert f["state"] == "fail", f
    assert ("setup-node@v5 earns no default cache: package.json starts with a byte-order mark"
            in f["evidence"]), f
    assert "could not be resolved" not in f["evidence"], f


def test_an_unresolved_default_beside_a_cross_repo_reusable_is_named(tmp_path):
    (tmp_path / "package.json").write_text("not json")
    raw = (PR_ON + "jobs:\n  r:\n    uses: o/r/.github/workflows/x.yml@v1\n"
           "  t:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v5\n"
           "      - uses: actions/setup-node@v5\n")
    f = facts_for(tmp_path, ("ci.yml", raw))[CACHE]
    assert f["state"] == "not_applicable", f
    assert ("setup-node@v5 default cache could not be resolved: package.json is not valid JSON"
            in f["evidence"]), f


def test_an_unresolved_default_reaches_the_card(tmp_path):
    import subprocess
    cc = _load("ci_score_collect_config_undeserved_card", "scripts/collect_config.py")
    rc = _load("ci_score_render_card_undeserved_card", "scripts/render_card.py")
    root = tmp_path / "repo"
    (root / ".github" / "workflows").mkdir(parents=True)
    wf = _steps_wf("uses: actions/checkout@v5", "uses: actions/setup-node@v5", "run: npm ci")[1]
    (root / ".github" / "workflows" / "ci.yml").write_text(wf)
    (root / "package.json").write_text("{nope")
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(root),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)
    doc, code = cc.collect(root)
    assert code == 0
    card = "\n".join(rc._render_score_card(doc))
    assert "default cache could not be resolved: package.json is not valid JSON" in card, card
