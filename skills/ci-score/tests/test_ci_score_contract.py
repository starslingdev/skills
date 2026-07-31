"""Contract cells for the v0.1-basic CI Score (OD-CS15).

Rebuilt for ci-score by the score-ectomy (2026-07-16): the layers under test
are the same, but every module is loaded from ci-score's OWN scripts by file
path, and the corpus-pinning cells read the six SCORED corpus fixtures under
`tests/fixtures/corpora/` — this suite imports NO ci-speedup code and reads
NOTHING from ci-speedup's `reports/`.

Two layers, matching the two-layer design:

1. `practice_facts._practice_facts` — the eleven configuration facts,
   table-driven over real YAML snippets (each case is the exact shape a
   maintainer would check).
2. `ci_score.compute_ci_score` — the arithmetic that maps facts to a stamp:
   passed / applicable, one refusal, purity, determinism, no egress.

Plus the card renderer (`render_card._render_score_card`) and the
recompute-and-diff verifiability promise over the committed stamped fixture.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SPEC_PATH = _SKILL_DIR / "references" / "ci-score-spec.json"
_CORPORA_DIR = _SKILL_DIR / "tests" / "fixtures" / "corpora"


def _load(mod_name: str, rel: str):
    # Load ci-score's own modules by FILE PATH: sibling skills also ship
    # a scan.py and other scripts on the shared pythonpath, so a bare `import`
    # could bind the wrong module. File-path loading pins each to this skill.
    spec = importlib.util.spec_from_file_location(mod_name, _SKILL_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


pf_mod = _load("ci_score_practice_facts", "scripts/practice_facts.py")
_cs_mod = _load("ci_score_ci_score", "scripts/ci_score.py")
rc_mod = _load("ci_score_render_card", "scripts/render_card.py")
compute_ci_score = _cs_mod.compute_ci_score
_round_half_up = _cs_mod._round_half_up

CORPORA = ["better-auth", "deepgram-python-sdk", "langfuse", "mastra",
           "OneSignal-Flutter-SDK", "requests"]


@pytest.fixture(scope="module")
def spec() -> dict:
    return json.loads(_SPEC_PATH.read_text())


def parsed(*files: tuple[str, str]) -> list[tuple[str, dict, str]]:
    return [(rel, yaml.safe_load(raw) or {}, raw) for rel, raw in files]


def facts_for(root: Path, *files: tuple[str, str]) -> dict:
    return pf_mod._practice_facts(parsed(*files), root)


PR_ON = "on:\n  pull_request:\n"


# ---------------------------------------------------------------------------
# Layer 1: the facts, one table per check. Each case is real YAML.
# ---------------------------------------------------------------------------

def test_fact_dependency_cache(tmp_path):
    # a manifest exists → the uncached case is a real FAIL (the manifest-absent
    # n/a path has its own cell, test_fact_dependency_cache_is_na_without_a_manifest)
    (tmp_path / "package.json").write_text("{}\n")
    yes = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/cache@v4\n")
    also = ("b.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/setup-node@v4\n        with:\n          cache: pnpm\n")
    no = ("c.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - run: pnpm install\n")
    assert facts_for(tmp_path, yes)["ci.cache.dependency-cache"]["state"] == "pass"
    assert facts_for(tmp_path, also)["ci.cache.dependency-cache"]["state"] == "pass"
    f = facts_for(tmp_path, no)["ci.cache.dependency-cache"]
    assert f["state"] == "fail" and f["evidence"].strip()


def test_fact_dependency_cache_is_na_only_without_any_install_signal(tmp_path):
    """OD-CS19 (install-signal retarget): the check is not_applicable ONLY when
    the repo installs no dependencies at all — no manifest at the root AND no
    inline install command. The gate is install-SIGNAL, not file-only: a
    manifest OR an inline install makes it applicable, so a manifest-less repo
    with a bare inline `pip install` is a real FAIL (the anti-masking case —
    a file-only gate would have gone n/a and inflated the grade), and a
    manifest-only repo with no cache is a real FAIL."""
    # neither manifest nor install command → not_applicable, never a fail
    no_install = ("c.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - run: echo hello\n")
    f = facts_for(tmp_path, no_install)["ci.cache.dependency-cache"]
    assert f["state"] == "not_applicable" and f["evidence"].strip()
    # ANTI-MASKING: no manifest, but a bare inline install → applicable, FAIL
    inline = ("c.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - run: pip install pytest pyyaml\n")
    f = facts_for(tmp_path, inline)["ci.cache.dependency-cache"]
    assert f["state"] == "fail" and f["evidence"].strip()
    # a manifest with no install command and no cache → applicable, FAIL
    (tmp_path / "requirements.txt").write_text("pytest\n")
    f = facts_for(tmp_path, no_install)["ci.cache.dependency-cache"]
    assert f["state"] == "fail" and f["evidence"].strip()
    # a cache hit PASSES regardless of any signal
    (tmp_path / "requirements.txt").unlink()
    yes = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/cache@v4\n")
    assert facts_for(tmp_path, yes)["ci.cache.dependency-cache"]["state"] == "pass"


def test_fact_dependency_cache_manifest_probe_covers_the_families(tmp_path):
    """The manifest half of the signal is a file-existence check (structure,
    not substrings) over the documented list, including the requirements*.txt
    family. `run: build` is not an install command, so only the manifest file
    makes the check applicable here."""
    uncached = ("c.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - run: build\n")
    for manifest in ("package.json", "go.mod", "Cargo.toml", "pyproject.toml",
                     "Gemfile", "pnpm-lock.yaml", "requirements-dev.txt",
                     "pom.xml", "build.gradle", "build.gradle.kts"):
        # fresh dir per manifest so only the one under test exists
        d = tmp_path / manifest.replace("/", "_")
        d.mkdir()
        (d / manifest).write_text("x\n")
        assert facts_for(d, uncached)["ci.cache.dependency-cache"]["state"] == "fail", manifest


def test_fact_dependency_cache_install_signal_covers_the_managers(tmp_path):
    """The install half of the signal: a package manager followed by an
    install/ci/sync/add verb (plus `go mod download` / `cargo fetch`) in a
    run: step makes the check applicable even with NO manifest file — this is
    the anti-masking guarantee, one cell per manager family."""
    for cmd in ("pip install -r requirements.txt", "pip3 install pytest",
                "pipx install ruff", "poetry install", "uv pip install foo",
                "uv sync", "npm ci", "npm install", "pnpm install", "yarn install",
                "yarn add foo", "bun install", "composer install",
                "bundle install", "gem install rake", "conda install numpy",
                "go mod download", "cargo fetch",
                # mainstream compiled/managed ecosystems (masking hole a review
                # caught): these often carry no root manifest in the fixed list
                "mvn -B install", "mvn verify", "./gradlew build",
                "gradle assemble", "dotnet restore", "dotnet add package Foo",
                "mix deps.get", "cabal build", "stack build",
                # build tools with a PROJECT-PATH / goal target between the
                # tool and the verb (the canonical monorepo invocation — a
                # masking gap a review caught): the verb is no longer adjacent
                "./gradlew :app:test", "gradle :service:build",
                "mvn -pl app -am install", "dotnet tool install foo",
                # `i` shorthand for the JS managers
                "npm i", "pnpm i", "bun i",
                # a real install with a trailing same-line comment still FAILS
                "pip install foo  # pin deps"):
        d = tmp_path / re.sub(r"[^A-Za-z0-9]", "_", cmd)
        d.mkdir()  # no manifest file anywhere in this dir
        wf = ("c.yml", PR_ON + f"jobs:\n  b:\n    steps:\n      - run: {cmd}\n")
        assert facts_for(d, wf)["ci.cache.dependency-cache"]["state"] == "fail", cmd
    # NON-install commands must NOT trip the signal — the verb must be adjacent
    # to the manager, so a script whose NAME merely contains a verb word
    # (`npm run ci`, `yarn run add-thing`) stays not_applicable, and a shell-
    # COMMENTED install is stripped before matching (the module's structure-
    # not-substrings invariant: a comment must never cause a false FAIL).
    for i, cmd in enumerate((
            "npm run build", "npm run ci", "npm run ci-lint",
            "yarn run add-thing", "echo hi  # npm install skipped",
            "# pip install pytest")):
        d = tmp_path / f"noninstall{i}"
        d.mkdir()
        wf = ("c.yml", PR_ON + f"jobs:\n  b:\n    steps:\n      - run: {cmd}\n")
        assert facts_for(d, wf)["ci.cache.dependency-cache"]["state"] == "not_applicable", cmd


def test_fact_dependency_cache_setup_action_is_a_signal(tmp_path):
    """A language SETUP action implies a package ecosystem regardless of
    language — the general applicability signal that closes the masking hole
    for ecosystems outside the manifest/install-command lists (.NET, Elixir,
    Scala, ...). setup-<lang> with no cache and no manifest → applicable FAIL,
    never a masked not_applicable."""
    for action in ("actions/setup-node@v4", "actions/setup-dotnet@v4",
                   "erlef/setup-beam@v1", "actions/setup-java@v4"):
        d = tmp_path / re.sub(r"[^A-Za-z0-9]", "_", action)
        d.mkdir()  # no manifest, no install command
        wf = ("c.yml", PR_ON + f"jobs:\n  b:\n    steps:\n      - uses: {action}\n")
        assert facts_for(d, wf)["ci.cache.dependency-cache"]["state"] == "fail", action
    # a non-runtime setup action (docker buildx) is NOT a dependency signal
    d = tmp_path / "buildx"
    d.mkdir()
    wf = ("c.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: docker/setup-buildx-action@v3\n")
    assert facts_for(d, wf)["ci.cache.dependency-cache"]["state"] == "not_applicable"


def test_fact_dependency_cache_signal_boundaries(tmp_path):
    """By-design boundaries of the dependency-cache signal, pinned so a future
    change can't silently flip a grade:
      - the manifest probe is ROOT-ONLY: a manifest that lives only in a
        SUBDIR, with no install command or setup action anywhere, is n/a
        (structure-not-substrings; a monorepo's real install command / setup
        action is what makes it applicable, not a deep-scanned file).
      - an install command inside a LOCAL COMPOSITE action still counts."""
    # subdir-only manifest, no other signal → n/a (root-only probe, intended)
    sub = tmp_path / "subdir_manifest"
    (sub / "backend").mkdir(parents=True)
    (sub / "backend" / "pom.xml").write_text("<project/>\n")
    wf = ("c.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - run: echo build\n")
    assert facts_for(sub, wf)["ci.cache.dependency-cache"]["state"] == "not_applicable"
    # but a real install command reaches applicability even from a subdir repo
    wf2 = ("c.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - run: cd backend && mvn -pl app install\n")
    assert facts_for(sub, wf2)["ci.cache.dependency-cache"]["state"] == "fail"


def test_fact_dependency_cache_install_regex_is_linear(tmp_path):
    """The install regex runs over an arbitrary (untrusted) target repo's
    run: text, so it must not catastrophically backtrack. A pathological line
    (a manager token + many dash-flags + no reachable verb) must resolve fast
    AND, having no reachable install verb and no manifest, must stay n/a."""
    import time
    evil = "uv " + " ".join(["--flag"] * 60) + " zzz"
    d = tmp_path / "redos"
    d.mkdir()
    wf = ("c.yml", PR_ON + f"jobs:\n  b:\n    steps:\n      - run: {evil}\n")
    t = time.time()
    state = facts_for(d, wf)["ci.cache.dependency-cache"]["state"]
    assert (time.time() - t) < 1.0, "install regex backtracked catastrophically"
    assert state == "not_applicable"


def test_fact_dependency_cache_ecosystem_manifests(tmp_path):
    """Root manifests for mainstream compiled/managed ecosystems make the check
    applicable (a repo with the manifest and no cache is a real FAIL, not a
    masked n/a). Covers the fixed names and the glob families (.csproj, .cabal,
    .gemspec)."""
    uncached = ("c.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - run: build\n")
    for manifest in ("mix.exs", "build.sbt", "packages.config", "stack.yaml",
                     "Package.swift", "app.csproj", "my-lib.gemspec", "proj.cabal"):
        d = tmp_path / re.sub(r"[^A-Za-z0-9]", "_", manifest)
        d.mkdir()
        (d / manifest).write_text("x\n")
        assert facts_for(d, uncached)["ci.cache.dependency-cache"]["state"] == "fail", manifest


# --- OD-CS20: automation-only refusal --------------------------------------

def test_automation_only_refuses_bots_but_never_real_ci(tmp_path):
    """A repo whose workflows do NO project build or test — only automation
    (bots, releases, triage) — is automation_only=True (→ refusal). But ANY ONE
    project-work signal scores it: a test job, a build/lint job or command, or
    a dependency-install COMMAND. Conservative: never a false refusal of real
    CI. Setup actions do NOT count (bots use setup-node/setup-bun)."""
    IS = "on:\n  issues:\n"
    SCHED = "on:\n  schedule:\n    - cron: '0 0 * * *'\n"
    REFUSE = {
        "issue-triage bot (github-script)":
            [("a.yml", IS + "jobs:\n  triage:\n    steps:\n      - uses: actions/github-script@v7\n")],
        "scheduled bot that setup-buns a script":
            [("a.yml", SCHED + "jobs:\n  sweep:\n    steps:\n      - uses: oven-sh/setup-bun@v2\n      - run: bun run sweep.ts\n")],
        "release-notify only":
            [("a.yml", "on:\n  push:\n    tags: ['v*']\njobs:\n  notify:\n    steps:\n      - run: echo shipped\n")],
        "'make' as English prose in a bot script":
            [("a.yml", IS + "jobs:\n  b:\n    steps:\n      - run: echo please make sure to review\n")],
        "permission check bot (job name has 'check')":
            [("a.yml", IS + "jobs:\n  allowed-non-write-check:\n    steps:\n      - run: gh pr diff\n")],
    }
    for label, files in REFUSE.items():
        assert pf_mod._automation_only(parsed(*files), tmp_path) is True, label

    SCORE = {
        "a test job":
            [("a.yml", PR_ON + "jobs:\n  test:\n    steps:\n      - run: pytest\n")],
        "lint-only gate (install command + eslint)":
            [("a.yml", PR_ON + "jobs:\n  lint:\n    steps:\n      - run: npm ci\n      - run: eslint .\n")],
        "a single build job":
            [("a.yml", PR_ON + "jobs:\n  build:\n    steps:\n      - run: go build ./...\n")],
        "leonardo-shaped (pnpm install in ci)":
            [("a.yml", PR_ON + "jobs:\n  ci:\n    steps:\n      - run: pnpm install --frozen-lockfile\n")],
        "coolify-shaped (build-push job)":
            [("a.yml", "on:\n  push:\njobs:\n  build-push:\n    steps:\n      - uses: docker/build-push-action@v6\n")],
        # a real test-only repo whose job is NOT named test-ish and whose deps
        # come from a setup action (no inline install command) must NOT refuse —
        # the test-COMMAND probe is what saves it (the cardinal false-refuse).
        "go test in a job named 'ci'":
            [("a.yml", PR_ON + "jobs:\n  ci:\n    steps:\n      - run: go test ./...\n")],
        "npm test in 'ci' with setup-node, no install cmd":
            [("a.yml", PR_ON + "jobs:\n  ci:\n    steps:\n      - uses: actions/setup-node@v4\n      - run: npm test\n")],
        "cargo nextest in a 'check' job":
            [("a.yml", PR_ON + "jobs:\n  check:\n    steps:\n      - run: cargo nextest run\n")],
        "pytest bare in 'ci'":
            [("a.yml", PR_ON + "jobs:\n  ci:\n    steps:\n      - run: pytest -q\n")],
        # CI delegated to a cross-repo reusable workflow is invisible, not absent
        "reusable-workflow-only":
            [("a.yml", PR_ON + "jobs:\n  ci:\n    uses: some-org/shared/.github/workflows/ci.yml@main\n")],
    }
    # long-tail ecosystems + non-`lint`-named lint gates: a real CI whose test
    # or lint command isn't in the JS/Python/Go core must still score (the
    # cardinal false-refuse a review caught). Each runs in a job named `ci`.
    for cmd in ("flutter test", "dart test", "swift test", "sbt test",
                "node --test", "bin/rails test", "python manage.py test",
                "golangci-lint run", "mypy src", "bundle exec rubocop",
                "cargo clippy", "swiftlint", "npx playwright test"):
        SCORE[f"'{cmd}' in a job named ci"] = [
            ("a.yml", PR_ON + f"jobs:\n  ci:\n    steps:\n      - run: {cmd}\n")]
    for label, files in SCORE.items():
        assert pf_mod._automation_only(parsed(*files), tmp_path) is False, label


def test_automation_only_flag_drives_a_scorer_refusal(spec):
    """The collector sets doc.automation_only; compute_ci_score turns it into
    the automation_only refusal — no value, ahead of facts_unavailable."""
    facts = {c["check_id"]: {"state": "not_applicable", "evidence": "e", "files": []}
             for c in spec["checks"]}
    doc = {"scanned_workflows": 4, "practice_facts": facts, "automation_only": True}
    stamp = _cs_mod.compute_ci_score(doc, spec)
    assert stamp["value"] is None and stamp["grade"] is None
    assert stamp["refusal"]["reason_code"] == "automation_only"
    assert "automation" in stamp["refusal"]["human_reason"].lower()
    # a repo with real facts and no automation flag is NOT refused for this
    doc2 = {"scanned_workflows": 4, "practice_facts":
            {c["check_id"]: {"state": "pass", "evidence": "e", "files": []}
             for c in spec["checks"]}, "automation_only": False}
    assert _cs_mod.compute_ci_score(doc2, spec)["refusal"] is None


def test_fact_test_sharding_is_na_without_any_test_job(tmp_path):
    """OD-CS19: you cannot shard tests you do not have. No test-like job and no
    shard-like matrix axis anywhere → not_applicable, never a fail."""
    no_test = ("a.yml", PR_ON + "jobs:\n  lint:\n    steps:\n      - run: ruff check\n")
    assert facts_for(tmp_path, no_test)["ci.parallel.test-sharding"]["state"] == "not_applicable"
    # a test-like job WITHOUT a matrix still FAILS — you have tests, shard them
    has_test = ("a.yml", PR_ON + "jobs:\n  test:\n    steps:\n      - run: pytest\n")
    f = facts_for(tmp_path, has_test)["ci.parallel.test-sharding"]
    assert f["state"] == "fail" and f["evidence"].strip()
    # a shard-like matrix axis on a non-test job keeps the check applicable (fail
    # only because THIS case has no matrix on a test job — here it PASSES since
    # the axis is shard-like)
    shard_axis = ("a.yml", PR_ON + "jobs:\n  build:\n    strategy:\n      matrix:\n        shard: [1, 2]\n    steps: []\n")
    assert facts_for(tmp_path, shard_axis)["ci.parallel.test-sharding"]["state"] == "pass"


def test_fact_build_cache_is_na_without_a_build_tool(tmp_path):
    wf = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - run: make\n")
    assert facts_for(tmp_path, wf)["ci.cache.build-cache"]["state"] == "not_applicable"
    (tmp_path / "turbo.json").write_text("{}")
    assert facts_for(tmp_path, wf)["ci.cache.build-cache"]["state"] == "fail"
    cached = ("a.yml", PR_ON + "jobs:\n  b:\n    env:\n      TURBO_TOKEN: x\n    steps:\n      - run: turbo build\n")
    assert facts_for(tmp_path, cached)["ci.cache.build-cache"]["state"] == "pass"


def test_fact_shallow_clone(tmp_path):
    deep = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          fetch-depth: 0\n")
    shallow = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@v4\n")
    # fetch-depth: 0 on a NON-PR workflow (release) must not fail the check.
    release_deep = ("r.yml", "on: push\njobs:\n  b:\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          fetch-depth: 0\n")
    f = facts_for(tmp_path, deep)["ci.checkout.shallow-clone"]
    assert f["state"] == "fail" and "a.yml" in f["evidence"]
    assert facts_for(tmp_path, shallow)["ci.checkout.shallow-clone"]["state"] == "pass"
    assert facts_for(tmp_path, shallow, release_deep)["ci.checkout.shallow-clone"]["state"] == "pass"


def test_fact_test_sharding(tmp_path):
    sharded = ("a.yml", PR_ON + "jobs:\n  test:\n    strategy:\n      matrix:\n        shard: [1, 2, 3]\n    steps: []\n")
    unsharded = ("a.yml", PR_ON + "jobs:\n  test:\n    steps: []\n")
    # a plain os-matrix on a NON-test job does not count as test sharding; with
    # a (matrix-less) test job also present the check stays applicable and FAILS
    build_matrix = ("a.yml", PR_ON + "jobs:\n  build:\n    strategy:\n      matrix:\n        os: [ubuntu]\n    steps: []\n  test:\n    steps: []\n")
    assert facts_for(tmp_path, sharded)["ci.parallel.test-sharding"]["state"] == "pass"
    assert facts_for(tmp_path, unsharded)["ci.parallel.test-sharding"]["state"] == "fail"
    assert facts_for(tmp_path, build_matrix)["ci.parallel.test-sharding"]["state"] == "fail"


def test_fact_change_scoped_builds(tmp_path):
    (tmp_path / "turbo.json").write_text("{}")
    scoped = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - run: pnpm turbo run build --filter=...[origin/main]\n")
    handrolled = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: dorny/paths-filter@sha\n")
    unscoped = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - run: pnpm turbo run build\n")
    assert facts_for(tmp_path, scoped)["ci.build.change-scoped"]["state"] == "pass"
    assert facts_for(tmp_path, handrolled)["ci.build.change-scoped"]["state"] == "pass"
    assert facts_for(tmp_path, unscoped)["ci.build.change-scoped"]["state"] == "fail"


def test_fact_change_scoped_is_na_without_a_task_graph(tmp_path):
    plain = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - run: pytest\n")
    assert facts_for(tmp_path, plain)["ci.build.change-scoped"]["state"] == "not_applicable"


def test_fact_concurrency_groups_and_cancel(tmp_path):
    both = ("a.yml", PR_ON + "concurrency:\n  group: ci-${{ github.ref }}\n  cancel-in-progress: true\njobs: {}\n")
    group_only = ("a.yml", PR_ON + "concurrency:\n  group: ci-${{ github.ref }}\njobs: {}\n")
    neither = ("a.yml", PR_ON + "jobs: {}\n")
    f = facts_for(tmp_path, both)
    assert f["ci.trigger.concurrency-groups"]["state"] == "pass"
    assert f["ci.trigger.cancel-superseded"]["state"] == "pass"
    f = facts_for(tmp_path, group_only)
    assert f["ci.trigger.concurrency-groups"]["state"] == "pass"
    assert f["ci.trigger.cancel-superseded"]["state"] == "fail"
    f = facts_for(tmp_path, neither)
    assert f["ci.trigger.concurrency-groups"]["state"] == "fail"
    assert f["ci.trigger.cancel-superseded"]["state"] == "fail"
    # an explicit false is not the cancel practice; a templated expression is.
    explicit_false = ("a.yml", PR_ON + "concurrency:\n  group: g\n  cancel-in-progress: false\njobs: {}\n")
    templated = ("a.yml", PR_ON + "concurrency:\n  group: g\n  cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}\njobs: {}\n")
    assert facts_for(tmp_path, explicit_false)["ci.trigger.cancel-superseded"]["state"] == "fail"
    assert facts_for(tmp_path, templated)["ci.trigger.cancel-superseded"]["state"] == "pass"


def test_fact_path_filter(tmp_path):
    filtered = ("a.yml", "on:\n  pull_request:\n    paths: ['src/**']\njobs: {}\n")
    unfiltered = ("a.yml", PR_ON + "jobs: {}\n")
    no_pr = ("a.yml", "on: push\njobs: {}\n")
    assert facts_for(tmp_path, filtered)["ci.trigger.path-filter"]["state"] == "pass"
    assert facts_for(tmp_path, unfiltered)["ci.trigger.path-filter"]["state"] == "fail"
    assert facts_for(tmp_path, no_pr)["ci.trigger.path-filter"]["state"] == "not_applicable"


def test_fact_job_timeouts(tmp_path):
    # v0.1.2 (OD-CS18): draft-gate is no longer a fact — only job-timeouts here.
    wf = ("a.yml", PR_ON + "jobs:\n  b:\n    timeout-minutes: 20\n    steps: []\n")
    bare = ("a.yml", PR_ON + "jobs:\n  b:\n    steps: []\n")
    assert facts_for(tmp_path, wf)["ci.hygiene.job-timeouts"]["state"] == "pass"
    assert facts_for(tmp_path, bare)["ci.hygiene.job-timeouts"]["state"] == "fail"
    # the draft-gate fact is gone from the registry entirely
    assert "ci.trigger.draft-gate" not in facts_for(tmp_path, wf)


def test_fact_scoped_id_token(tmp_path):
    none = ("a.yml", PR_ON + "jobs:\n  b:\n    steps: []\n")
    job_level = ("a.yml", PR_ON + "jobs:\n  b:\n    permissions:\n      id-token: write\n    steps: []\n")
    wf_level = ("a.yml", "on: push\npermissions:\n  id-token: write\njobs:\n  b:\n    steps: []\n")
    assert facts_for(tmp_path, none)["ci.security.scoped-id-token"]["state"] == "not_applicable"
    assert facts_for(tmp_path, job_level)["ci.security.scoped-id-token"]["state"] == "pass"
    f = facts_for(tmp_path, wf_level)["ci.security.scoped-id-token"]
    assert f["state"] == "fail" and "a.yml" in f["evidence"]


def test_fact_pinned_shas(tmp_path):
    pinned = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@8f4b7f84864484a7bf31766abe9204da3cbe65b3\n")
    mixed = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@8f4b7f84864484a7bf31766abe9204da3cbe65b3\n      - uses: actions/cache@v4\n")
    local = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: ./.github/actions/mine\n")
    assert facts_for(tmp_path, pinned)["ci.security.pinned-action-shas"]["state"] == "pass"
    f = facts_for(tmp_path, mixed)["ci.security.pinned-action-shas"]
    assert f["state"] == "fail" and "1 of 2" in f["evidence"]
    assert facts_for(tmp_path, local)["ci.security.pinned-action-shas"]["state"] == "not_applicable"


def test_fact_pinned_shas_threshold_is_95_percent(tmp_path):
    """v0.1.1 (OD-CS17): the check passes at >=95% pinned - a Renovate-style
    straggler (1 in 20) passes, partial adoption (2 in 20) does not. Exact
    integer arithmetic: 19/20 = 95.0% sits ON the boundary and must pass."""
    sha = "8f4b7f84864484a7bf31766abe9204da3cbe65b3"
    def wf(n_pinned, n_unpinned):
        steps = [f"      - uses: actions/checkout@{sha}\n"] * n_pinned
        steps += ["      - uses: actions/cache@v4\n"] * n_unpinned
        return ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n" + "".join(steps))
    f = facts_for(tmp_path, wf(19, 1))["ci.security.pinned-action-shas"]
    assert f["state"] == "pass"
    assert "19 of 20" in f["evidence"]
    f = facts_for(tmp_path, wf(18, 2))["ci.security.pinned-action-shas"]
    assert f["state"] == "fail"
    assert "18 of 20" in f["evidence"]
    assert ">=95%" in f["evidence"]  # the threshold is named on the card
    # 18/19 = 94.7% pins the lower edge: any silent loosening to <=94% would
    # pass this fixture while still passing 19/20 - the 18/20 case alone only
    # guards the (90%, 95%] band.
    assert facts_for(tmp_path, wf(18, 1))["ci.security.pinned-action-shas"]["state"] == "fail"


# ---------------------------------------------------------------------------
# Layer 2: the scorer arithmetic over a facts-bearing document.
# ---------------------------------------------------------------------------

def doc_with_facts(states: dict[str, str], spec: dict) -> dict:
    facts = {c["check_id"]: {"state": states.get(c["check_id"], "pass"),
                             "evidence": "test evidence", "files": []}
             for c in spec["checks"]}
    return {"scanned_workflows": 3, "practice_facts": facts, "findings": []}


def test_all_pass_is_100_a_plus(spec):
    stamp = compute_ci_score(doc_with_facts({}, spec), spec)
    assert (stamp["value"], stamp["grade"]) == (100, "A+")
    assert stamp["checks_passed"] == stamp["checks_applicable"] == 11
    assert stamp["refusal"] is None


def test_na_leaves_the_denominator(spec):
    states = {"ci.cache.build-cache": "not_applicable",
              "ci.security.scoped-id-token": "not_applicable",
              "ci.trigger.path-filter": "fail"}
    stamp = compute_ci_score(doc_with_facts(states, spec), spec)
    # 11 checks, 2 n/a leave 9 applicable; 1 fail -> 8/9 = 88.9 -> 89 A-.
    assert stamp["checks_applicable"] == 9 and stamp["checks_passed"] == 8
    assert stamp["value"] == 89 and stamp["grade"] == "A-"


def test_rounding_is_half_up(spec):
    # 9 of 11: 81.81 -> 82 B+; and the unit ties that banker's rounding loses.
    states = {"ci.trigger.path-filter": "fail", "ci.security.pinned-action-shas": "fail"}
    stamp = compute_ci_score(doc_with_facts(states, spec), spec)
    assert (stamp["value"], stamp["grade"]) == (82, "B+")
    assert _round_half_up(84.5) == 85 and _round_half_up(54.5) == 55


def test_refusal_only_when_nothing_to_check(spec):
    stamp = compute_ci_score({"scanned_workflows": 0, "practice_facts": {}}, spec)
    assert stamp["refusal"]["reason_code"] == "no_workflow_yaml"
    assert stamp["value"] is None and stamp["grade"] is None
    assert stamp["checks_passed"] is None


def test_vintage_document_refuses_honestly(spec):
    """A pre-reset findings.json has no practice_facts: the honest output is
    're-run the skill', never a guessed score."""
    stamp = compute_ci_score({"scanned_workflows": 5, "findings": []}, spec)
    assert stamp["refusal"]["reason_code"] == "facts_unavailable"
    assert "re-run the skill" in stamp["refusal"]["human_reason"]


# The committed corpora's scores, pinned. Every corpus fixture carries a stamp,
# and pinning the values makes any scorer/facts regression visible as a grade
# change on real data (the crown-join lesson). These fixtures are the SCORED
# findings.json copied from ci-speedup's worked-example corpora at the split;
# they stay in this private repo. A refresh re-pins deliberately.
# Recomputed under v0.1.2 (OD-CS18, draft-gate removed) by running the scorer
# over each corpus's committed practice_facts — never hand-derived. Re-stamped
# under v0.1.3 (OD-CS19, two applicability gates added): every corpus carries a
# real dependency manifest and a real test job, so neither gate fires and no
# value/grade moved — only the stamp's spec_version bumped.
EXPECTED_CORPUS_SCORES = {
    "better-auth": (82, "B+"),
    "deepgram-python-sdk": (33, "F"),
    "langfuse": (82, "B+"),
    "mastra": (82, "B+"),
    "OneSignal-Flutter-SDK": (75, "B"),
    "requests": (56, "C-"),
}


@pytest.mark.parametrize("name", CORPORA)
def test_committed_corpora_carry_current_evidence_format(name):
    """The recompute guard compares the stamp against practice_facts, so a
    corpus whose BOTH copies kept a stale evidence format would pass it
    silently. Pin the current scanner wording on the shipped corpora: a
    countable pinning fact must name the threshold the way practice_facts does."""
    doc = json.loads((_CORPORA_DIR / name / "findings.json").read_text())
    fact = doc["practice_facts"]["ci.security.pinned-action-shas"]
    if fact["state"] in ("pass", "fail"):
        assert fact["evidence"].endswith(
            "(workflow + local composite action files; passes at >=95%)"), name


@pytest.mark.parametrize("name", CORPORA)
def test_committed_corpora_scores_are_pinned(spec, name):
    doc = json.loads((_CORPORA_DIR / name / "findings.json").read_text())
    stamp = doc.get("ci_score")
    assert isinstance(stamp, dict), f"{name}: scored corpus fixture must carry a stamp"
    assert stamp["refusal"] is None, stamp["refusal"]
    assert (stamp["value"], stamp["grade"]) == EXPECTED_CORPUS_SCORES[name]
    # and the committed stamp recomputes byte-for-byte (the recompute guard
    # over REAL data, not only the synthetic fixture)
    body = {k: v for k, v in doc.items() if k != "ci_score"}
    assert json.dumps(compute_ci_score(body, spec), sort_keys=True) == \
        json.dumps(stamp, sort_keys=True)


def test_all_checks_na_refuses_never_scores_100(spec):
    """The Scorecard anomaly guard, basic edition: if nothing was checkable,
    the output is a refusal — not a vacuous perfect score."""
    states = {c["check_id"]: "not_applicable" for c in spec["checks"]}
    stamp = compute_ci_score(doc_with_facts(states, spec), spec)
    assert stamp["refusal"] is not None and stamp["value"] is None


def test_evidence_survives_into_the_stamp_and_fail_is_never_bare(spec):
    doc = doc_with_facts({"ci.trigger.path-filter": "fail"}, spec)
    doc["practice_facts"]["ci.trigger.path-filter"]["evidence"] = "no PR workflow scopes itself with paths"
    stamp = compute_ci_score(doc, spec)
    chk = next(c for c in stamp["checks"] if c["check_id"] == "ci.trigger.path-filter")
    assert chk["state"] == "fail"
    assert chk["evidence"] == "no PR workflow scopes itself with paths"


def test_measured_note_is_display_only_and_fail_only(spec):
    doc = doc_with_facts({"ci.cache.dependency-cache": "fail"}, spec)
    doc["findings"] = [{"pattern": "OPT2", "wall_clock_p50_s": 95.0}]
    stamp = compute_ci_score(doc, spec)
    failed = next(c for c in stamp["checks"] if c["check_id"] == "ci.cache.dependency-cache")
    assert failed["measured_note"] == "measured cost on this repo: ~95s per run"
    # the same finding on a PASSING doc changes nothing
    doc2 = doc_with_facts({}, spec)
    doc2["findings"] = [{"pattern": "OPT2", "wall_clock_p50_s": 95.0}]
    stamp2 = compute_ci_score(doc2, spec)
    assert stamp2["value"] == 100
    assert all(c["measured_note"] is None for c in stamp2["checks"])


def test_unrecognized_fact_state_is_na_not_a_crash(spec):
    doc = doc_with_facts({}, spec)
    doc["practice_facts"]["ci.trigger.path-filter"]["state"] = "maybe"
    stamp = compute_ci_score(doc, spec)
    chk = next(c for c in stamp["checks"] if c["check_id"] == "ci.trigger.path-filter")
    assert chk["state"] == "not_applicable"
    assert stamp["checks_applicable"] == 10


def test_determinism_and_no_input_mutation(spec):
    doc = doc_with_facts({"ci.trigger.path-filter": "fail"}, spec)
    before = json.dumps(doc, sort_keys=True)
    a = compute_ci_score(copy.deepcopy(doc), spec)
    b = compute_ci_score(copy.deepcopy(doc), spec)
    assert json.dumps(a) == json.dumps(b)
    compute_ci_score(doc, spec)
    assert json.dumps(doc, sort_keys=True) == before


def test_scoring_path_is_fully_offline(spec, monkeypatch):
    import socket
    import subprocess

    def boom(*_a, **_k):
        raise AssertionError("the scorer touched the network / a subprocess")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    stamp = compute_ci_score(doc_with_facts({}, spec), spec)
    assert stamp["value"] == 100


# ---------------------------------------------------------------------------
# Adversarial-round regression cells: each was a CONFIRMED false FAIL or false
# PASS against the raw-substring first draft. The structure walk must hold.
# ---------------------------------------------------------------------------

def test_partial_facts_stamp_refuses_never_publishes_a_subset_score(spec):
    """A doc carrying 3 of 11 facts (all pass) must refuse — not publish a
    perfect score computed from the subset it happens to carry."""
    doc = doc_with_facts({}, spec)
    keep = list(doc["practice_facts"])[:3]
    doc["practice_facts"] = {k: doc["practice_facts"][k] for k in keep}
    stamp = compute_ci_score(doc, spec)
    assert stamp["refusal"]["reason_code"] == "facts_unavailable"


def test_fact_dependency_cache_recognizes_cache_action_families(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n")  # a manifest, so the disabled case is a real fail
    rust = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: Swatinem/rust-cache@v2\n")
    split = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/cache/restore@v4\n")
    disabled = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/setup-node@v4\n        with:\n          cache: false\n")
    assert facts_for(tmp_path, rust)["ci.cache.dependency-cache"]["state"] == "pass"
    assert facts_for(tmp_path, split)["ci.cache.dependency-cache"]["state"] == "pass"
    # cache explicitly DISABLED is not the practice (the first draft passed it)
    assert facts_for(tmp_path, disabled)["ci.cache.dependency-cache"]["state"] == "fail"


def test_fact_dependency_cache_sees_local_composite_actions(tmp_path):
    act = tmp_path / ".github" / "actions" / "setup"
    act.mkdir(parents=True)
    act.joinpath("action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - uses: actions/setup-node@v4\n"
        "      with:\n        cache: pnpm\n")
    wf = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: ./.github/actions/setup\n")
    assert facts_for(tmp_path, wf)["ci.cache.dependency-cache"]["state"] == "pass"


def test_fact_sharding_recognizes_e2e_and_shard_axes(tmp_path):
    e2e = ("a.yml", PR_ON + "jobs:\n  e2e:\n    strategy:\n      matrix:\n        chunk: [1, 2, 3]\n    steps: []\n")
    # os-matrix on lint + the word "shard" in a COMMENT is not test sharding;
    # a matrix-less test job keeps the check applicable so this is a real FAIL
    comment_shard = ("a.yml", "# we shard elsewhere\n" + PR_ON + "jobs:\n  lint:\n    strategy:\n      matrix:\n        os: [ubuntu, macos]\n    steps: []\n  test:\n    steps: []\n")
    assert facts_for(tmp_path, e2e)["ci.parallel.test-sharding"]["state"] == "pass"
    assert facts_for(tmp_path, comment_shard)["ci.parallel.test-sharding"]["state"] == "fail"


def test_fact_shallow_clone_ignores_comments_and_catches_quoted_zero(tmp_path):
    comment = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      # we do NOT use fetch-depth: 0 here (slow)\n      - uses: actions/checkout@v4\n")
    quoted = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          fetch-depth: '0'\n")
    assert facts_for(tmp_path, comment)["ci.checkout.shallow-clone"]["state"] == "pass"
    assert facts_for(tmp_path, quoted)["ci.checkout.shallow-clone"]["state"] == "fail"


def test_fact_change_scoped_sphinx_is_not_nx(tmp_path):
    """'sphinx' contains 'nx' — the substring bug that failed a Python docs
    repo for a monorepo practice. Word boundaries + structure walk fix it."""
    docs = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - run: pip install sphinx && sphinx-build docs out\n")
    assert facts_for(tmp_path, docs)["ci.build.change-scoped"]["state"] == "not_applicable"


def test_fact_change_scoped_gradle_is_na_not_fail(tmp_path):
    (tmp_path / "settings.gradle").write_text("")
    wf = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - run: ./gradlew build\n")
    assert facts_for(tmp_path, wf)["ci.build.change-scoped"]["state"] == "not_applicable"


def test_merge_queue_only_repo_pr_path_checks_are_na(tmp_path):
    """A merge-queue-only repo (no PR triggers) must not be dinged for the
    PR-path checks — they are meaningless without PR triggers (OD-CS15's own
    motivation). (Draft-gate, the original subject here, was removed in v0.1.2
    per OD-CS18; shallow-clone still exercises the merge-queue-only path.)"""
    queue_only = ("a.yml", "on:\n  merge_group:\njobs:\n  b:\n    steps: []\n")
    f = facts_for(tmp_path, queue_only)
    assert "ci.trigger.draft-gate" not in f
    assert f["ci.checkout.shallow-clone"]["state"] == "not_applicable"


def test_fact_timeouts_ignore_comments(tmp_path):
    comment = ("a.yml", PR_ON + "# TODO: add timeout-minutes\njobs:\n  b:\n    steps: []\n")
    assert facts_for(tmp_path, comment)["ci.hygiene.job-timeouts"]["state"] == "fail"


def test_fact_id_token_write_all_is_workflow_wide(tmp_path):
    wf = ("a.yml", "on: push\npermissions: write-all\njobs:\n  b:\n    steps: []\n")
    f = facts_for(tmp_path, wf)["ci.security.scoped-id-token"]
    assert f["state"] == "fail" and "write-all" in f["evidence"]


def test_fact_pinned_shas_counts_composite_actions_and_first_party(tmp_path):
    act = tmp_path / ".github" / "actions" / "setup"
    act.mkdir(parents=True)
    act.joinpath("action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - uses: evil/unpinned@main\n")
    wf = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@8f4b7f84864484a7bf31766abe9204da3cbe65b3\n      - uses: ./.github/actions/setup\n")
    f = facts_for(tmp_path, wf)["ci.security.pinned-action-shas"]
    assert f["state"] == "fail" and "1 of 2" in f["evidence"]


# ---------------------------------------------------------------------------
# Persona-round regression cells (scored-maintainer + Series-B CTO reviews):
# each was a verified misgrade or misleading evidence string on a real card.
# ---------------------------------------------------------------------------

def test_post_merge_automation_is_not_pr_gating(tmp_path):
    """backport-auto.yml: pull_request_target types [closed] + fetch-depth: 0.
    It cherry-picks commits (needs history) and runs AFTER merge (gates
    nobody). The maintainer persona's top dispute — and it was our own
    methodology's example evidence string."""
    backport = ("backport-auto.yml",
                "on:\n  pull_request_target:\n    types: [closed]\n"
                "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@v4\n"
                "        with:\n          fetch-depth: 0\n")
    ci = ("ci.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@v4\n")
    f = facts_for(tmp_path, backport, ci)
    assert f["ci.checkout.shallow-clone"]["state"] == "pass"
    # and the backport workflow leaves every PR-path denominator
    assert "1 PR-gating" in f["ci.trigger.concurrency-groups"]["evidence"].replace("of 1", "1 PR-gating") or "of 1" in f["ci.trigger.concurrency-groups"]["evidence"]


def test_delegated_ci_is_na_never_fail(tmp_path):
    """The CTO's uninstall event: a repo whose CI lives in cross-repo reusable
    workflows fails checks it passes. 'Never fail a mechanism this fact cannot
    see' applies to every check."""
    caller = ("ci.yml", PR_ON + "jobs:\n  ci:\n    uses: my-org/workflows/.github/workflows/node-ci.yml@v1\n")
    f = facts_for(tmp_path, caller)
    assert f["ci.cache.dependency-cache"]["state"] == "not_applicable"
    assert "my-org/workflows" in f["ci.cache.dependency-cache"]["evidence"]
    assert f["ci.parallel.test-sharding"]["state"] == "not_applicable"


def test_capped_file_lists_are_never_phrased_as_exhaustive(tmp_path):
    """mastra has SIX fetch-depth offenders; the card named three as if the
    list were complete — a maintainer who fixed the named three would still
    fail with no idea why. Counts must be complete even when files are capped."""
    files = [(f"w{i}.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@v4\n"
              "        with:\n          fetch-depth: 0\n") for i in range(5)]
    f = facts_for(tmp_path, *files)["ci.checkout.shallow-clone"]
    assert f["state"] == "fail"
    assert "5 PR-gating workflow(s)" in f["evidence"] and "e.g." in f["evidence"]
    assert len(f["files"]) == 3  # capped, but the count above is complete


def test_pinned_offender_files_rank_third_party_first(tmp_path):
    """The maintainer was pointed at actions/labeler while changesets/action@v1
    held release credentials. Third-party offenders sort first."""
    wf = ("a.yml", PR_ON + "jobs:\n  b:\n    steps:\n      - uses: actions/labeler@v6\n")
    wf2 = ("z-release.yml", "on: push\njobs:\n  b:\n    steps:\n      - uses: changesets/action@v1\n")
    f = facts_for(tmp_path, wf, wf2)["ci.security.pinned-action-shas"]
    assert f["state"] == "fail"
    assert f["files"][0] == "z-release.yml"  # third-party offender first, despite the alphabet


# ---------------------------------------------------------------------------
# The score card renders ONLY from the stamp, and the committed stamped fixture
# makes recompute-and-diff non-vacuous.
# ---------------------------------------------------------------------------

_FIXTURE = _SKILL_DIR / "tests" / "fixtures" / "ci-score" / "stamped-fixture.json"


def test_recompute_and_diff_on_the_committed_stamped_fixture(spec):
    """The verifiability promise, checkable by anyone with git: recomputing the
    stamp from the committed doc-minus-stamp reproduces the committed stamp
    byte for byte. Scores change only via re-run or spec bump — enforced, not
    promised."""
    doc = json.loads(_FIXTURE.read_text())
    committed = doc.pop("ci_score")
    recomputed = compute_ci_score(doc, spec)
    assert json.dumps(recomputed, sort_keys=True) == json.dumps(committed, sort_keys=True)


def test_recompute_guard_goes_red_on_a_drifted_stamp(spec):
    """Prove the guard is non-vacuous: a hand-drifted stamp (the value nudged
    by one) must NOT survive recompute-and-diff."""
    doc = json.loads(_FIXTURE.read_text())
    drifted = doc.pop("ci_score")
    drifted["value"] += 1
    recomputed = compute_ci_score(doc, spec)
    assert json.dumps(recomputed, sort_keys=True) != json.dumps(drifted, sort_keys=True)


def test_card_renders_only_stamp_numbers(spec):
    """Every number on the card IS the stamp (the single-source rule)."""
    doc = json.loads(_FIXTURE.read_text())
    lines = rc_mod._render_score_card(doc)
    card = "\n".join(lines)
    stamp = doc["ci_score"]
    assert f"**{stamp['value']}/100**" in card
    assert f"{stamp['checks_passed']} of {stamp['checks_applicable']}" in card
    assert stamp["scope_statement"] in card
    for chk in stamp["checks"]:
        assert chk["label"] in card
        assert chk["evidence"].split(",")[0] in card
    # measured_note rides its failed check on the card
    failed = next(c for c in stamp["checks"] if c["state"] == "fail" and c["measured_note"])
    assert failed["measured_note"] in card


def _gauge_of(card: str) -> tuple[int, str]:
    """(value, bar) parsed out of the rendered gauge line."""
    import re
    m = re.search(r"CI Score  (\d+)/100  ▏([█░]+)▕", card)
    assert m, f"no gauge line in card:\n{card}"
    return int(m.group(1)), m.group(2)


@pytest.mark.parametrize("value,filled", [(0, 0), (38, 10), (50, 13), (100, 25)])
def test_gauge_blocks_agree_with_value(spec, value, filled):
    """The gauge's filled-block count is round-half-up(value·25/100), pinned at
    the 0 / 38 / 50 / 100 boundaries (50→12.5 rounds UP to 13, the banker's-
    rounding trap)."""
    doc = json.loads(_FIXTURE.read_text())
    doc["ci_score"]["value"] = value
    card = "\n".join(rc_mod._render_score_card(doc))
    shown, bar = _gauge_of(card)
    assert shown == value
    assert len(bar) == 25
    assert bar.count("█") == filled
    assert bar == "█" * filled + "░" * (25 - filled)
    # the gauge value equals the headline value — one number, two places
    assert f"**{value}/100**" in card


def test_gauge_is_the_cards_first_line_and_quotes_pass_counts(spec):
    doc = json.loads(_FIXTURE.read_text())
    lines = rc_mod._render_score_card(doc)
    assert lines[0] == "```"          # the gauge is fenced (monospace survives)
    assert lines[2] == "```"          # ...and the fence CLOSES on the next line
    # an unbalanced fence would swallow the rest of the report into a code block
    assert "\n".join(lines).count("```") % 2 == 0
    stamp = doc["ci_score"]
    na = sum(1 for c in stamp["checks"] if c["state"] == "not_applicable")
    assert (f"{stamp['checks_passed']} of {stamp['checks_applicable']} "
            f"checks pass · {na} n/a") in lines[1]


def test_gauge_dropped_on_non_int_value_without_raising(spec):
    """The `isinstance(value, int)` guard: a malformed non-refusal stamp with a
    null value renders best-effort (no gauge, no exception)."""
    doc = json.loads(_FIXTURE.read_text())
    doc["ci_score"]["value"] = None
    lines = rc_mod._render_score_card(doc)   # must not raise
    assert "CI Score  " not in "\n".join(lines)  # no gauge on a valueless stamp


def test_skill_md_example_bars_obey_the_formula(spec):
    """The shipped close-banner (30-block) and delta-bar (25-block) illustration
    art must satisfy its own round-half-up rule — a hand-edited example that
    drifts from the formula fails here (artifacts self-check). The example boxes
    moved to references/close-contract.md with the close/kickoff protocol
    (2026-07-30, behavior-neutral extraction); the re-derivation reads their new
    home."""
    import re
    close_contract = (_SKILL_DIR / "references" / "close-contract.md").read_text()

    def rhu(value, n):
        return (value * n + 50) // 100

    # `label  VALUE/100  <bar>` lines in the delta bar (25 blocks each)
    for value, bar in re.findall(r"(\d+)/100  ([█░]+)", close_contract):
        v = int(value)
        assert len(bar) == 25, (v, len(bar))
        assert bar.count("█") == rhu(v, 25), (v, bar.count("█"), rhu(v, 25))
    # the banner bar sits on its own boxed line (30 blocks); its value is the
    # `NN / 100` on the boxed line above it. Both must be inside `│…│` rows so
    # the "value × 30 / 100" formula prose above the box can't false-match.
    banner = re.search(
        r"│[^\n]*?(\d+) / 100[^\n]*│\n│[^\n]*?([█░]{5,})", close_contract
    )
    assert banner, "banner art not found in references/close-contract.md"
    bv, bbar = int(banner.group(1)), banner.group(2)
    assert len(bbar) == 30, (bv, len(bbar))
    assert bbar.count("█") == rhu(bv, 30), (bv, bbar.count("█"), rhu(bv, 30))


def test_gauge_na_tail_only_when_na_present(spec):
    """The `· N n/a` tail appears iff the not-applicable count is positive."""
    doc = json.loads(_FIXTURE.read_text())
    # drop every n/a check → applicable-only stamp, no tail
    doc["ci_score"]["checks"] = [c for c in doc["ci_score"]["checks"]
                                 if c["state"] != "not_applicable"]
    card = "\n".join(rc_mod._render_score_card(doc))
    assert "n/a" not in card.split("```")[1]  # gauge segment carries no tail


def test_refusal_and_error_cards_have_no_gauge(spec):
    refusal = {"scanned_workflows": 0, "practice_facts": {}}
    refusal["ci_score"] = compute_ci_score(refusal, spec)
    assert "CI Score  " not in "\n".join(rc_mod._render_score_card(refusal))
    err = {"data_sources": {"ci_score_error": {"error": "RuntimeError: boom"}}}
    assert "CI Score  " not in "\n".join(rc_mod._render_score_card(err))


def test_no_stamp_no_card(spec):
    assert rc_mod._render_score_card({"findings": []}) == []


def test_refusal_stamp_renders_reason_and_states(spec):
    doc = {"scanned_workflows": 0, "practice_facts": {}}
    doc["ci_score"] = compute_ci_score(doc, spec)
    card = "\n".join(rc_mod._render_score_card(doc))
    assert "no score:" in card.lower()
    assert "nothing to check" in card
    assert card.lower().count("no score:") == 1  # the heading must not stutter
    # the check table still renders (states are n/a on a refusal): 11 checks
    assert card.count("n/a") >= 11


def test_automation_only_card_does_not_stutter(spec):
    """OD-CS20's automation_only reason begins 'Not scored:', a DIFFERENT prefix
    from the other two ('No score:'). The card heading is 'CI Score — no score:
    <reason>', so both prefixes must be stripped or it stutters
    ('no score: Not scored: ...'). Guards the exact bug a review caught."""
    facts = {c["check_id"]: {"state": "not_applicable", "evidence": "e", "files": []}
             for c in spec["checks"]}
    doc = {"scanned_workflows": 4, "practice_facts": facts, "automation_only": True}
    doc["ci_score"] = compute_ci_score(doc, spec)
    card = "\n".join(rc_mod._render_score_card(doc))
    low = card.lower()
    assert low.count("no score:") == 1, card       # no 'no score: Not scored:'
    assert "not scored:" not in low, card          # the prefix was stripped
    assert "no build or test activity" in card     # the reason body survived


def test_scoring_error_renders_one_honest_line(spec):
    doc = {"data_sources": {"ci_score_error": {"error": "RuntimeError: boom"}}}
    card = "\n".join(rc_mod._render_score_card(doc))
    assert "CI Score unavailable" in card and "RuntimeError: boom" in card


def test_committed_corpora_render_their_cards(spec):
    """Every scored corpus fixture renders a card whose headline equals its
    stamp — the showcase job of the worked examples. (ci-score carries only
    the findings.json fixtures, not ci-speedup's rendered .md reports, so this
    asserts the render, not a committed report beside it.)"""
    for name in CORPORA:
        doc = json.loads((_CORPORA_DIR / name / "findings.json").read_text())
        card = "\n".join(rc_mod._render_score_card(doc))
        stamp = doc["ci_score"]
        headline = f"## CI Score: **{stamp['value']}/100**"
        assert headline in card, name


def test_card_survives_hostile_evidence_and_malformed_stamps(spec):
    """The report must never die because of the card, and a pipe or newline in
    an evidence string must not collapse the markdown table row."""
    doc = json.loads(_FIXTURE.read_text())
    doc["ci_score"]["checks"][0]["evidence"] = "weird | file\nwith newline.yml"
    lines = rc_mod._render_score_card(doc)
    row = next(l for l in lines if "weird" in l)
    assert "\n" not in row          # the newline is collapsed
    assert "\\|" in row             # the pipe arrives ESCAPED, not as a cell break
    assert row.count("|") - row.count("\\|") == 4  # exactly the 4 structural pipes
    # malformed shapes render best-effort, never raise
    assert rc_mod._render_score_card({"ci_score": {"checks": "garbage", "refusal": "nope"}})
    assert rc_mod._render_score_card({"ci_score": {"checks": [None, 42]}})
