"""CI Score v0.1-basic — the practice-facts computation (extracted from
ci-speedup's scanner, 2026-07-16).

`_practice_facts(parsed, root)` walks a repo's PARSED workflow YAML + local
composite actions and returns the eleven pass/fail CONFIGURATION FACTS the CI
Score consumes (each fact: {"state": "pass"|"fail"|"not_applicable",
"evidence": str, "files": [..<=3]}). `ci_score.compute_ci_score` maps these
onto the frozen v0.1.5 registry and does the arithmetic.

STRUCTURE, NOT SUBSTRINGS: every fact walks the parsed workflow (jobs, steps,
with-blocks, on-blocks). The only substring matching is over the CODE inside a
step's `run:`/`script:` value (a real command line) — the change-scoped signal,
and the OD-CS19/20 dependency-install, test-command, and build-command signals.
The install/test/build command signals strip shell comments first, so a
commented-out command never produces a false signal; the change-scoped signal
predates this and matches raw code, but its only effect is a PASS (it never
turns a comment into a false FAIL). A false FAIL on a public page is this
design's worst outcome, and no substring path can produce one from a comment.

VISIBILITY: workflow files plus LOCAL COMPOSITE ACTIONS
(.github/actions/**/action.yml). Cross-repo reusable workflows remain
invisible; the affected facts land not_applicable-with-evidence naming what was
searched, never a confident claim about unseen files.

This module is a self-contained COPY of the helpers it needs (cross-skill
imports are forbidden — a skill must install standalone). It shares no code
with ci-speedup at runtime.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover — surfaced loudly if missing
    print("ERROR: PyYAML is required (`pip install pyyaml`)", file=sys.stderr)
    sys.exit(1)


_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_TESTISH_RE = re.compile(r"test|spec|e2e|integration|unit", re.IGNORECASE)
_SHARD_AXIS_RE = re.compile(r"shard|chunk|split|partition", re.IGNORECASE)
_SCOPED_CMD_RE = re.compile(
    r"--filter=[^\s]*\[|\bnx affected\b|--changed\b|--onlyChanged\b|--affected\b|turbo-ignore")
_CHANGED_FILES_ACTIONS = ("dorny/paths-filter", "tj-actions/changed-files",
                          "step-security/changed-files")
_CACHE_ACTION_RE = re.compile(r"(^|/)[^/@]*cache[^/@]*(/|$)", re.IGNORECASE)

# Dependency manifests a cache could key on, probed at the repo ROOT by
# file existence (structure, not substrings — the same shape as the
# build-tool gate below). One half of the dependency-cache applicability
# SIGNAL (OD-CS19, install-signal retarget).
_DEP_MANIFESTS = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb",
    "package.json", "requirements.txt", "pyproject.toml", "setup.py",
    "setup.cfg", "Pipfile", "Pipfile.lock", "poetry.lock", "uv.lock",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock",
    "composer.json", "composer.lock", "pubspec.yaml", "pubspec.lock",
    # JVM (Maven / Gradle), .NET, Elixir, Scala, Haskell, Swift — a repo with
    # any of these at its root declares deps a cache can key on. Omitting a
    # mainstream ecosystem would let its repos fall to not_applicable and
    # INFLATE the grade (the OD-CS19 masking hole a review caught). File
    # existence, not substrings.
    "pom.xml", "build.gradle", "build.gradle.kts",
    "packages.config", "mix.exs", "mix.lock", "build.sbt", "build.sc",
    "cabal.project", "stack.yaml", "Package.swift", "Package.resolved",
)

# Extra manifests probed by GLOB at the root (the family cannot be a fixed
# name): the requirements*.txt family plus .NET project files, Ruby gemspecs,
# and Haskell .cabal files.
_DEP_MANIFEST_GLOBS = (
    "requirements*.txt", "*.csproj", "*.fsproj", "*.vbproj", "*.gemspec",
    "*.cabal",
)

# A language SETUP action (actions/setup-node, setup-python, setup-java,
# setup-dotnet, setup-go, setup-ruby, erlef/setup-beam, ...) reliably implies
# a package ecosystem regardless of language — the general applicability
# signal that does not depend on enumerating every ecosystem's install
# command. Excludes non-runtime "setup-*" actions (docker/setup-buildx, qemu).
_SETUP_LANG_ACTION_RE = re.compile(
    r"setup-(?:node|python|java|dotnet|go(?:lang)?|ruby|elixir|beam|erlang|"
    r"scala|swift|haskell|php|bun|deno|pnpm|uv|python-poetry|gradle|maven|"
    r"sbt|rust|dart|flutter|xcode|nim|crystal|zig)\b",
    re.IGNORECASE)

# The OTHER half of the signal: a dependency-INSTALL command in any
# workflow/composite step's run:/script: code. Install-signal semantics
# (OD-CS19 round-6 retarget) — the file-existence gate alone (a) missed the
# very repo that motivated the gate (the public skills repo carries a
# pyproject.toml for pytest config with dependencies=[], so it stayed
# applicable and should) and (b) would MASK real waste: a repo with no
# manifest file but a bare inline `pip install ...` in every run would go
# not_applicable, silently dropping a real fail from the denominator and
# INFLATING the grade. So the check is applicable when a manifest OR an
# inline install exists; only a repo with NEITHER is not_applicable.
# Structural-ish match over the existing step_code collection (precedent
# _SCOPED_CMD_RE). Two shapes:
#  - PACKAGE MANAGERS (pip/pipx/poetry/uv/npm/pnpm/yarn/bun/composer/bundle/
#    gem/conda): the install/ci/sync/add/i verb must be ADJACENT (only flag
#    tokens may sit between), so `npm run ci` / `yarn run add-x` do NOT match,
#    only `npm ci` / `yarn add` / `npm i`. Adjacency keeps the check from a
#    FALSE FAIL on a non-install script whose name merely contains a verb word.
#  - BUILD TOOLS (mvn/gradle(w)/dotnet/mix/cabal/stack): the verb may follow
#    project targets/goals on the same line (`./gradlew :app:test`,
#    `mvn -pl app -am install`), so the tool→verb gap is a BOUNDED lazy window.
# Every quantifier is unambiguous (a single `-\S+` decomposition per flag
# token, and a bounded `{0,200}` window) so the pattern is LINEAR — no
# catastrophic backtracking, even on a hostile target repo's run: text.
_INSTALL_CMD_RE = re.compile(
    r"\b(?:pip3?|pipx|poetry|npm|pnpm|yarn|bun|composer|bundle|gem|conda)"
    r"(?:\s+-\S+)*\s+(?:install|ci|sync|add|i)\b"
    r"|\buv(?:\s+-\S+)*\s+(?:pip\s+|tool\s+)?(?:install|sync|add)\b"
    # JVM / .NET / Elixir / Haskell build+resolve commands (often the only
    # signal in a monorepo whose manifest sits in a subdir, not the root);
    # the verb may sit after project targets/goals — bounded lazy gap.
    r"|\bmvn\b[^\n]{0,200}?\b(?:install|verify|package|test|compile|deploy)\b"
    r"|(?:\./)?\bgradlew?\b[^\n]{0,200}?\b(?:build|assemble|test|check|dependencies|classes|jar|war)\b"
    r"|\bdotnet\b[^\n]{0,200}?\b(?:restore|build|add|test|run|pack|publish|tool)\b"
    r"|\bmix\s+(?:deps\.\w+|compile|test|run|release)\b"
    r"|\b(?:cabal|stack)(?:\s+-\S+)*\s+(?:build|install|update|setup|test)\b"
    r"|\bgo\s+mod\s+download\b|\bcargo\s+fetch\b",
    re.IGNORECASE)

# A shell comment must never produce a false install signal (the module's
# STRUCTURE-NOT-SUBSTRINGS invariant): strip `# ...` to end-of-line before
# matching. Only strips `#` at line start or after whitespace, so a `#` that
# is part of a token (e.g. a `color=#fff` literal) is left alone. Tradeoff: a
# `#` inside a quoted string that is preceded by whitespace is also treated as
# a comment start; this errs toward NOT signalling, and the residual (a real
# install hidden after a quoted `#` on the same physical line) is negligible.
_SHELL_COMMENT_RE = re.compile(r"(?m)(?:^|\s)#.*$")

# BUILD / project-work signals for the automation-only refusal (OD-CS20). A
# repo does real project work if a job compiles/bundles/lints it. Three probes:
#  - a build-like or lint/typecheck JOB NAME (build/compile/bundle/assemble/
#    dist/package/lint/typecheck — but NOT deploy/release/publish, which are
#    DELIVERY automation, not the project's build);
#  - a build command in a run: step;
#  - a container/artifact build ACTION.
# Lint/typecheck count as project work so a real lint-only gate never refuses
# (owner: never false-refuse a small-but-real CI).
# Job NAMES that mean project build/lint work (NOT deploy/release/publish,
# which are DELIVERY, and NOT the bare word "check", which names permission /
# status / policy bots — `allowed-non-write-check` is not a project build).
_BUILDISH_JOB_RE = re.compile(
    r"\b(?:build|compile|bundle|assemble|dist|package|webpack|vite|rollup|"
    r"esbuild|typecheck|tsc|lint)\b", re.IGNORECASE)
# Build/lint COMMANDS in a run: step. `make` is deliberately NOT here (the bare
# English word "make", e.g. "make sure", false-matches a bot script — a
# Makefile at the root is the structural build signal instead). Every tool
# token is distinctive enough not to collide with prose.
_BUILD_CMD_RE = re.compile(
    r"\bdocker\s+build|\bgo\s+build\b|\bcargo\s+build\b"
    r"|\bnpm\s+run\s+build\b|\byarn\b[^\n]{0,60}?\bbuild\b|\bpnpm\b[^\n]{0,60}?\bbuild\b"
    r"|\bdotnet\s+build\b|\bmvn\b[^\n]{0,120}?\b(?:package|install|verify)\b"
    r"|(?:\./)?\bgradlew?\b[^\n]{0,120}?\b(?:build|assemble)\b|\bbazel\s+build\b"
    r"|\bcmake\b|\bninja\b|\btsc\b|\bvite\s+build\b|\bwebpack\b|\brollup\b|\besbuild\b"
    r"|\bnx\b[^\n]{0,60}?\bbuild\b|\bturbo\b[^\n]{0,60}?\bbuild\b|\bswift\s+build\b"
    r"|\bmix\s+compile\b|\bcabal\s+build\b|\bstack\s+build\b|\bsbt\b[^\n]{0,60}?\bcompile\b"
    r"|\bflutter\s+build\b|\bdart\s+compile\b"
    # LINT commands (a lint gate is real project work; a job named `ci` running
    # a linter must not false-refuse) — distinctive tool names only
    r"|\bruff\b|\beslint\b|\bflake8\b|\bgolangci-lint\b|\bstaticcheck\b|\bgo\s+vet\b"
    r"|\bmypy\b|\bpyright\b|\bpylint\b|\brubocop\b|\bprettier\b|\bbiome\b|\bstylelint\b"
    r"|\bcargo\s+clippy\b|\bmix\s+credo\b|\bshellcheck\b|\bktlint\b|\bdetekt\b"
    r"|\bcheckstyle\b|\bdotnet\s+format\b|\bswiftlint\b",
    re.IGNORECASE)
_BUILD_ACTION_RE = re.compile(
    r"docker/build-push-action|docker/build|goreleaser|electron-builder|"
    r"gradle/gradle-build-action|gradle/actions", re.IGNORECASE)
# Build-TOOL / build-system config files at the repo root (a project built with
# turbo/nx/gradle/bazel/make/cmake is a buildable project — a build signal).
_BUILD_TOOL_ROOT = ("turbo.json", "nx.json", "settings.gradle",
                    "settings.gradle.kts", ".bazelrc", "WORKSPACE",
                    "WORKSPACE.bazel", "MODULE.bazel", "Makefile", "makefile",
                    "GNUmakefile", "CMakeLists.txt")
# TEST COMMANDS in a run: step — the automation-only refusal's test signal must
# not depend on the job being NAMED test-ish (a real test-only repo often runs
# `go test` / `pytest` / `npm test` in a job named `ci`, with deps from a
# setup-* action rather than an inline install command). Distinctive test-runner
# invocations only, so a bot script that merely contains the word "test" does
# not match (no bare `\btest\b`).
_TEST_CMD_RE = re.compile(
    r"\bpytest\b|\btox\b|\bnox\b|\bpython\s+-m\s+(?:pytest|unittest)\b"
    r"|\b(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b|\bbun\s+test\b"
    r"|\bnpx\s+(?:jest|vitest|mocha|playwright|cypress)\b|\bjest\b|\bvitest\b"
    r"|\bnode\s+--(?:experimental-)?test\b"
    r"|\bgo\s+test\b|\bgotestsum\b|\bcargo\s+(?:test|nextest)\b"
    r"|\brspec\b|\brake\s+(?:test|spec)\b|(?:bin/)?\brails\s+test\b|\bphpunit\b|\bpest\b"
    r"|\bdeno\s+test\b|\bctest\b|\bmix\s+test\b|\bdotnet\s+test\b"
    # long-tail ecosystems whose test runner is neither build nor install
    r"|\bflutter\s+test\b|\bdart\s+test\b|\bswift\s+test\b|\bsbt\b[^\n]{0,60}?\btest\b"
    r"|\bmanage\.py\s+test\b|\bbats\b|\bbusted\b|\bgotest\b|\bginkgo\b"
    r"|\bmake\s+(?:test|check)\b"
    r"|(?:\./)?\bgradlew?\b[^\n]{0,120}?\btest\b|\bmvn\b[^\n]{0,120}?\btest\b",
    re.IGNORECASE)


def _has_dep_manifest(root: Path) -> bool:
    """A dependency manifest at the repo root for a cache to key on
    (file-existence probe over the fixed list plus the glob families)."""
    if any((root / m).exists() for m in _DEP_MANIFESTS):
        return True
    return any(next(root.glob(g), None) is not None for g in _DEP_MANIFEST_GLOBS)


def _has_install_signal(step_sources: list[tuple[str, list[dict]]]) -> bool:
    """A dependency-install signal in the repo's own step definitions — the
    second half of the dependency-cache applicability signal (OD-CS19): an
    install command in a run:/github-script: block, OR a language SETUP action
    (setup-node/python/java/dotnet/...) which reliably implies a package
    ecosystem regardless of language. Shell comments are stripped first, so a
    commented-out install never produces a false signal."""
    def has(code: object) -> bool:
        return (isinstance(code, str)
                and bool(_INSTALL_CMD_RE.search(_SHELL_COMMENT_RE.sub("", code))))
    for _rel, steps in step_sources:
        for step in steps:
            ua = _step_uses(step)
            if ua and _SETUP_LANG_ACTION_RE.search(ua[0]):
                return True
            if has(step.get("run")):
                return True
            with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
            if has(with_.get("script")):
                return True
    return False


def _has_test_command(step_sources: list[tuple[str, list[dict]]]) -> bool:
    """A test-runner command in a run:/script: step — the test signal that does
    not depend on the job being named test-ish (OD-CS20). Shell comments are
    stripped first."""
    for _rel, steps in step_sources:
        for step in steps:
            run = step.get("run")
            if isinstance(run, str) and _TEST_CMD_RE.search(_SHELL_COMMENT_RE.sub("", run)):
                return True
            with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
            script = with_.get("script")
            if isinstance(script, str) and _TEST_CMD_RE.search(_SHELL_COMMENT_RE.sub("", script)):
                return True
    return False


def _has_build_signal(parsed: list[tuple[str, dict, str]], root: Path,
                      step_sources: list[tuple[str, list[dict]]]) -> bool:
    """Any signal that the repo's CI builds/lints the project (OD-CS20): a
    build-tool config at the root, a build-like/lint job name, a build command
    in a step, or a container-build action."""
    if any((root / p).exists() for p in _BUILD_TOOL_ROOT):
        return True
    for _rel, doc, _raw in parsed:
        for jid, job in _wf_jobs(doc).items():
            name = str(job.get("name", ""))
            if _BUILDISH_JOB_RE.search(jid) or _BUILDISH_JOB_RE.search(name):
                return True
    for _rel, steps in step_sources:
        for step in steps:
            run = step.get("run")
            if isinstance(run, str) and _BUILD_CMD_RE.search(_SHELL_COMMENT_RE.sub("", run)):
                return True
            ua = _step_uses(step)
            if ua and _BUILD_ACTION_RE.search(ua[0]):
                return True
    return False


def _has_install_command(step_sources: list[tuple[str, list[dict]]]) -> bool:
    """A dependency-INSTALL COMMAND in a run:/script: step — the install signal
    for the automation-only refusal (OD-CS20). Unlike the dependency-cache
    signal, this does NOT count a setup-* action: an issue-triage bot legitimately
    uses setup-node/setup-bun to run its automation script, so a setup action is
    too weak to mean 'the project's CI installs its dependencies'."""
    def has(code: object) -> bool:
        return (isinstance(code, str)
                and bool(_INSTALL_CMD_RE.search(_SHELL_COMMENT_RE.sub("", code))))
    for _rel, steps in step_sources:
        for step in steps:
            if has(step.get("run")):
                return True
            with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
            if has(with_.get("script")):
                return True
    return False


def _automation_only(parsed: list[tuple[str, dict, str]], root: Path) -> bool:
    """True when the repo's workflows show NO project build or test activity —
    only automation (bots, releases, triage) — so the honest output is a
    refusal, never a technically-honest-but-absurd score (OD-CS20). CONSERVATIVE
    by design: refuse ONLY when NONE of three workflow-activity signals is
    present, so a small-but-real gate (a lint-only job, a single build job,
    a `pip install && pytest` step) always scores:
      1. a test signal — a test-like JOB (test/spec/e2e/integration/unit in an
         id or name) OR a test-runner COMMAND (pytest / go test / npm test /
         rspec / ...); a real test-only repo often names its job `ci` and gets
         deps from a setup-* action, so the command probe is essential;
      2. a build signal (build tool, build/lint job, build command, build action);
      3. a dependency-INSTALL COMMAND in a workflow (npm ci, pip install, ...).
         NOT a setup-* action (bots use setup-node/setup-bun to run their
         automation scripts) and NOT manifest presence (an automation-only repo
         may carry a package.json its CI never installs). The live case:
         anthropics/claude-code is an npm package whose visible workflows are
         all issue-triage bots (which setup-bun to run bun scripts) and release
         automation — no job builds or tests the project itself.
    No workflows at all is a DIFFERENT refusal (no_workflow_yaml), handled
    upstream; this predicate only fires when workflows exist but do no CI.
    A repo that DELEGATES its CI to a cross-repo reusable workflow is never
    refused either — a mechanism this fact cannot see is never failed (the
    module's standing rule)."""
    if not parsed:
        return False
    if _remote_reusable_refs(parsed):
        # CI delegated to a cross-repo reusable workflow — invisible, not absent.
        return False
    for _rel, doc, _raw in parsed:
        for jid, job in _wf_jobs(doc).items():
            if _TESTISH_RE.search(jid) or _TESTISH_RE.search(str(job.get("name", ""))):
                return False  # signal 1 (test-like job name)
    step_sources = _step_sources(parsed, _composite_action_docs(root))
    if _has_test_command(step_sources):
        return False  # signal 1 (test-runner command)
    if _has_build_signal(parsed, root, step_sources):
        return False  # signal 2
    if _has_install_command(step_sources):
        return False  # signal 3 — install COMMAND only (a setup action is used
                       # by bots too, so it does not count here)
    return True


def _wf_is_pr_triggered(doc: dict) -> bool:
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return on in ("pull_request", "pull_request_target")
    if isinstance(on, list):
        return any(str(e) in ("pull_request", "pull_request_target") for e in on)
    if isinstance(on, dict):
        return "pull_request" in on or "pull_request_target" in on
    return False


def _wf_is_pr_gating(doc: dict) -> bool:
    """PR-triggered AND actually on the PR path: a trigger whose `types` are
    only post-merge events (`closed`) is backport/changelog automation that
    runs AFTER merge and gates nobody — scoring its full-history checkout as
    a PR-speed defect was the maintainer persona's top dispute (and our own
    methodology's example, which made the canonical example a false
    positive)."""
    on = doc.get("on", doc.get(True))
    if not isinstance(on, dict):
        return _wf_is_pr_triggered(doc)
    for key in ("pull_request", "pull_request_target"):
        if key not in on:
            continue
        trig = on.get(key)
        types = trig.get("types") if isinstance(trig, dict) else None
        if types and set(str(x) for x in types) <= {"closed"}:
            continue  # post-merge automation, not a gate
        return True
    return False


# A `uses:` naming THIS repository: `./path`, or the self-repository prefix
# `$/path` GitHub shipped 2026-07-30 (same commit, same files, no checkout
# needed). Both resolve to `<repo root>/path`, so `ref[2:]` strips either.
_LOCAL_USES_PREFIXES = ("./", "$/")


def _is_local_uses(uses: str) -> bool:
    """True for a `uses:` that names an action or reusable workflow in this
    repository (`./…` or the self-repository `$/…`)."""
    return uses.startswith(_LOCAL_USES_PREFIXES)


def _remote_reusable_refs(parsed: list[tuple[str, dict, str]]) -> list[str]:
    """Cross-repo reusable workflows this repo's jobs delegate to
    (`uses: org/repo/.github/workflows/x.yml@ref` at JOB level). Their
    contents are invisible to this scan; a check that finds nothing locally
    while CI is delegated must land not_applicable, never fail — 'a mechanism
    this fact cannot see is never failed' applies to every check, not one."""
    refs = []
    for _rel, doc, _raw in parsed:
        for job in _wf_jobs(doc).values():
            uses = job.get("uses")
            # A self-repository `$/…` call is local, never remote. GitHub
            # rejects an `@ref` suffix on `$/`, but a malformed one must still
            # stay out of this list, where it could flip the no-CI refusal.
            if isinstance(uses, str) and not _is_local_uses(uses) and "@" in uses:
                refs.append(uses.split("@")[0])
    return sorted(set(refs))


def _wf_jobs(doc: dict) -> dict[str, dict]:
    jobs = doc.get("jobs")
    return {str(k): v for k, v in jobs.items() if isinstance(v, dict)} if isinstance(jobs, dict) else {}


# ---- The step walker ---------------------------------------------------------
#
# GitHub Actions parallel steps (shipped by GitHub 2026-06-25 on github.com
# and GHEC; not GHES): a step may be `- parallel:` holding a LIST of ordinary
# child steps, and the control steps `- wait:`, `- wait-all:` and `- cancel:`
# carry no `run:`/`uses:`. A flat read of `steps:` saw a `parallel:` entry as
# a step with neither key, so every child inside it — an unpinned action, the
# only cache step, a `fetch-depth: 0` checkout, the history op that exempts
# it — was invisible and a check could PASS or FAIL on a step it never read.
# EVERY step read in this module goes through `_walk_steps`;
# `tests/test_parallel_steps.py::test_no_step_list_is_read_outside_the_walker`
# fails if a `.get("steps")` appears anywhere else, so no check can drift back
# to a flat read.
#
# Control steps inside a local composite action: composites cannot use
# `background:`, so a `wait:` / `cancel:` there coordinates nothing. It is
# skipped and counted like any other control step (it runs no code either way).
_PARALLEL_KEY = "parallel"
_CONTROL_STEP_KEYS = ("wait", "wait-all", "cancel")

# Bounds on one walk, so a hostile or broken workflow cannot stall or crash
# the scorer. YAML aliases let a few lines build a group nested 1,000 deep
# (Python's recursion limit) or one that fans out to a billion entries
# (`lK: [{parallel: *lK-1}, {parallel: *lK-1}]`). Depth matches ci-secure's
# and ci-speedup's walker cap. The budget counts every entry visited — steps,
# control steps and groups alike, so a chain of empty groups is bounded too —
# per top-level walk; GitHub allows at most 1,000 steps per job, so no real
# job comes near it. A group past either bound is counted as unreadable and
# named, never silently dropped. A self-referencing group cannot make the walk
# recurse forever; a shared (aliased) group is read once per reference, up to
# the budget, and is NOT deduplicated: a step reused twice through an alias
# counts twice, exactly as the same steps written out flat would.
_MAX_PARALLEL_DEPTH = 64
_MAX_WALK_STEPS = 10_000

# YAML-facing names for the Python type a non-list `parallel:` value (or a
# non-step entry inside a group) loaded as; they form the record's reason codes.
_YAML_TYPE_NAMES = {"dict": "mapping", "str": "string", "int": "number",
                    "float": "number", "bool": "boolean", "list": "list"}


def _yaml_type(value: Any) -> str:
    return _YAML_TYPE_NAMES.get(type(value).__name__, type(value).__name__)


def _new_step_stats() -> dict[str, Any]:
    """What one walk read, by bucket (the collector records it as
    `data_sources.parallel_steps`):

    - `groups` / `steps_in_groups`: documented groups — a `parallel:` list on
      a workflow job's own step list — and the steps directly inside them,
      which GitHub runs side by side.
    - `control_steps`: `wait:` / `wait-all:` / `cancel:` steps skipped.
    - `invalid_groups` / `invalid`: groups in a shape GitHub does not
      document, read anyway (defensively: the same verdict as if their steps
      were written flat) and NOT counted in `groups`. Reasons: `in_composite`,
      `nested` (a group inside a group), `beside_run_uses` (on a step that
      also has `run:`/`uses:`), `not_a_list:mapping` (a single step mapping
      instead of a list).
    - `malformed_groups` / `malformed`: groups that could not be read, so
      their steps were not checked. Reasons: `not_a_list:<type>`, `null`,
      `cyclic` (contains itself through a YAML alias), `too_deep` (past
      `_MAX_PARALLEL_DEPTH`), `over_budget` (the walk spent
      `_MAX_WALK_STEPS` inside it).
    - `skipped_children` / `skipped`: entries inside a read group that are
      not steps (`not_a_step:<type>` or `null`).

    Every entry is `{"file", "job", "step", "reasons"}`: `job` is None in a
    composite action, `step` is the 1-based position (`3.2` = child 2 of
    step 3), None when the walk ran out of budget outside any group. A step
    with `run:` beside an unreadable `parallel:` lands in BOTH `invalid` and
    `malformed`."""
    return {"groups": 0, "steps_in_groups": 0, "control_steps": 0,
            "invalid_groups": 0, "invalid": [],
            "malformed_groups": 0, "malformed": [],
            "skipped_children": 0, "skipped": []}


_BUCKET_COUNT = {"invalid": "invalid_groups", "malformed": "malformed_groups",
                 "skipped": "skipped_children"}


def _note(stats: dict[str, Any] | None, bucket: str, rel: str, job: str | None,
          step: str | None, reasons: list[str]) -> None:
    if stats is None:
        return
    stats[_BUCKET_COUNT[bucket]] += 1
    stats[bucket].append({"file": rel, "job": job, "step": step, "reasons": reasons})


def _walk_steps(steps: Any, stats: dict[str, Any] | None = None,
                rel: str = "", _depth: int = 0, *, in_composite: bool = False,
                job: str | None = None, _prefix: str = "",
                _path: frozenset[int] = frozenset(),
                _budget: list[int] | None = None,
                _side_by_side: bool = False,
                _group_of: dict[int, int] | None = None,
                _group: int | None = None) -> list[dict]:
    """Every step the checks read — any mapping that is not a `parallel:`
    group or a bare control step — in declaration order, descending into
    `parallel:` groups. `_depth` is the nesting level of `steps` (0 = the
    job's or composite action's own list); a group whose children would sit
    past `_MAX_PARALLEL_DEPTH` is not entered. `_budget` is the one counter
    the whole walk spends from (`_MAX_WALK_STEPS`). `_side_by_side` marks
    `steps` as a documented group's children, the only steps counted in
    `steps_in_groups`. With `_group_of`, every step read inside a `parallel:`
    group is mapped (by `id`) to the position of the top-level group holding
    it, so a caller can tell which steps may run side by side. See
    `_new_step_stats` for
    what is counted and why: inside `parallel:` groups nothing is skipped
    without being counted and named."""
    out: list[dict] = []
    if not isinstance(steps, list):
        return out
    if _budget is None:
        _budget = [_MAX_WALK_STEPS]

    def keep(step: dict) -> None:
        out.append(step)
        if _group_of is not None and _group is not None:
            _group_of[id(step)] = _group
        if stats is not None and _side_by_side:
            stats["steps_in_groups"] += 1

    path = _path | {id(steps)}
    for i, step in enumerate(steps):
        if _budget[0] <= 0:
            # Spent: stop here and count the group being read as unreadable
            # (the job's own list when the budget ran out outside any group).
            if _budget[0] == 0:
                _note(stats, "malformed", rel, job, _prefix.rstrip(".") or None,
                      ["over_budget"])
                _budget[0] = -1  # noted once; every enclosing level just stops
            break
        _budget[0] -= 1
        label = f"{_prefix}{i + 1}"
        if not isinstance(step, dict):
            if _depth > 0:
                _note(stats, "skipped", rel, job, label,
                      ["null"] if step is None else [f"not_a_step:{_yaml_type(step)}"])
            continue
        is_leaf = "run" in step or "uses" in step
        if _PARALLEL_KEY in step:
            group = step.get(_PARALLEL_KEY)
            if is_leaf:
                keep(step)
            undocumented = [r for r, hit in (("in_composite", in_composite),
                                              ("nested", _depth > 0),
                                              ("beside_run_uses", is_leaf)) if hit]
            if isinstance(group, dict):
                children: Any = [group]  # one step mapping: read it as a list of one
                undocumented.append("not_a_list:mapping")
            else:
                children = group
            bad = None
            if group is None:
                bad = "null"
            elif not isinstance(children, list):
                bad = f"not_a_list:{_yaml_type(group)}"
            elif id(group) in path:
                bad = "cyclic"
            elif _depth + 1 > _MAX_PARALLEL_DEPTH:
                bad = "too_deep"
            if undocumented:
                _note(stats, "invalid", rel, job, label, undocumented)
            if bad:
                _note(stats, "malformed", rel, job, label, [bad])
                continue
            if stats is not None and not undocumented:
                stats["groups"] += 1
            out.extend(_walk_steps(children, stats, rel, _depth + 1,
                                   in_composite=in_composite, job=job,
                                   _prefix=f"{label}.", _path=path | {id(group)},
                                   _budget=_budget, _side_by_side=not undocumented,
                                   _group_of=_group_of,
                                   _group=i if _group is None else _group))
            continue
        if not is_leaf and any(k in step for k in _CONTROL_STEP_KEYS):
            if stats is not None:
                stats["control_steps"] += 1
            continue
        keep(step)
    return out


def _job_steps(job: dict, group_of: dict[int, int] | None = None) -> list[dict]:
    return _walk_steps(job.get("steps"), _group_of=group_of)


def _composite_steps(doc: dict, stats: dict[str, Any] | None = None,
                     rel: str = "") -> list[dict]:
    runs = doc.get("runs")
    if not isinstance(runs, dict):
        return []
    return _walk_steps(runs.get("steps"), stats, rel, in_composite=True)


def _step_sources(parsed: list[tuple[str, dict, str]],
                  composites: list[tuple[str, dict]],
                  stats: dict[str, Any] | None = None) -> list[tuple[str, list[dict]]]:
    """Steps everywhere the repo's own files define them — workflow jobs plus
    local composite actions (the `runs.steps` shape), via the walker. With
    `stats`, the same walk records what it read (see `_new_step_stats`)."""
    out: list[tuple[str, list[dict]]] = []
    for rel, doc, _raw in parsed:
        for jid, job in _wf_jobs(doc).items():
            out.append((rel, _walk_steps(job.get("steps"), stats, rel, job=jid)))
    for rel, doc in composites:
        if isinstance(doc.get("runs"), dict):
            out.append((rel, _composite_steps(doc, stats, rel)))
    return out


def _step_walk_stats(parsed: list[tuple[str, dict, str]], root: Path) -> dict[str, Any]:
    """What the walker found across workflows + local composite actions — the
    very walk `_step_sources` does, so the provenance can never describe a
    different read than the checks made. The collector records this; the
    report header discloses it."""
    stats = _new_step_stats()
    _step_sources(parsed, _composite_action_docs(root), stats)
    return stats


def _step_uses(step: dict) -> tuple[str, str] | None:
    """(action, ref) for a remote `uses:`; None for local (`./`, `$/`) /
    docker:// / run steps."""
    uses = step.get("uses")
    if not isinstance(uses, str) or _is_local_uses(uses) or uses.startswith("docker://"):
        return None
    action, _, ref = uses.partition("@")
    return (action, ref) if action else None


# ---- Git-history carve-out ---------------------------------------------------
#
# CANONICAL SOURCE: `skills/ci-speedup/scripts/scan.py` — `_GIT_HISTORY_RE`,
# `_HISTORY_JOB_NAME_RE`, `_LINE_CONTINUATION_RE`, `_has_git_history_op`,
# `_NESTED_LOCAL_USES_RE`, `_MAX_LOCAL_ACTION_DEPTH`, `_local_ref_path`,
# `_read_local_history_file`, `_local_action_needs_history`,
# `_index_local_git_actions` and `_job_needs_git_history`. What follows is a
# deliberate VERBATIM COPY of that predicate, not an import.
# `_LOCAL_USES_PREFIXES` and `_is_local_uses` are duplicated separately (they
# sit with the pinning helpers above, not in this block), copied from the same
# canonical source.
#
# ONE KNOWN DIVERGENCE: the step reader is NOT part of the copy. Here
# `_job_steps` routes through `_walk_steps`; ci-speedup walks through its own
# `job_leaf_steps`. Both read a history op inside a GitHub Actions `parallel:`
# LIST. They differ on a `parallel:` value that is one step MAPPING (an
# undocumented shape): this walker reads it as a list of one, ci-speedup counts
# the group malformed and holds its shallow-checkout pattern back on the job.
# The repo-root parity test pins that gap as a STRICT xfail; delete the marker
# when ci-speedup reads the mapping.
#
# WHY A COPY. Every skill in this repo installs and runs standalone, so
# ci-score may not import ci-speedup at runtime (the same rule that made this
# whole module a copy — see the header). Duplication is the house style here.
# The cost of duplication is drift, so the copy is pinned: the repo-root
# `tests/test_git_history_carveout_parity.py` loads BOTH modules and fails if
# the two predicates ever disagree.
#
# WHY THE CARVE-OUT. `fetch-depth: 0` is LOAD-BEARING for a job that walks git
# history — changelog generation, merge-base diffs, tag/describe, changeset
# publishing. Telling a maintainer to shallow such a job is not an
# optimization, it is a broken build. The speed engine has always failed
# CLOSED here on the documented ground that the cost of a miss is a lost
# finding, never a fix that breaks a job; grading the identical configuration
# fact the opposite way meant the two shipped engines could give one
# repository contradictory advice about one line of YAML. Same fact, same
# stance.
_GIT_HISTORY_RE = re.compile(
    r"auto-?changeset|changeset(s)?[ -](version|publish|cli)|changesets/action|"
    r"changelog|release-please|"
    r"git\s+(fetch|show|log|describe|rev-list|rev-parse|tag)|"
    # History-walking ops a bot commit-back / sync job needs: `git pull --rebase`,
    # `git rebase`, `git merge`, `git cherry-pick` all require base history, so a
    # job running them genuinely needs `fetch-depth: 0`.
    r"git\s+(pull|rebase|merge|cherry-pick)\b|"
    r"git\s+diff\b.*(\.\.\.|origin/)|"
    # Two-SHA / two-ref diff: `git diff <base.sha> <head.sha>` (PR change
    # detection). A shallow checkout doesn't contain base.sha, so this needs
    # full history just like a `...` merge-base diff — but it has neither `...`
    # nor `origin/`. Match a `git diff` line that references a `.sha` expression.
    r"git\s+diff\b[^\n|]*\.sha\b|"
    # Diff against a base ref held in a shell variable: `git diff --name-only
    # "$base" HEAD` or as a positional parameter like `git diff "$1" HEAD`. The
    # base commit is just as absent from a shallow clone as a literal
    # `<sha>...HEAD` is — it simply carries no `...`, no `origin/` and no
    # `.sha` for the clauses above to see.
    #
    # The variable only counts where a REF operand can stand. Two positions on
    # the command line can never hold a ref, and a `$` reached through either
    # one says nothing about history:
    #   * past a redirection arrow — `git diff --stat >> $GITHUB_STEP_SUMMARY`
    #     and `git diff > $OUT` name the FILE the diff is written to;
    #   * past a bare `--` separator — everything after it is a pathspec, so
    #     `git diff --exit-code -- "$FILE"` names a path, not a base commit.
    # Option flags such as `--name-only` are not the separator (no trailing
    # space), so `git diff --no-renames --name-only "$base" HEAD` still counts.
    r"git\s+diff\b(?:(?!\s--\s|>)[^\n|])*\$[({]?[A-Za-z_0-9]|"
    # Object-reachability probe: `git cat-file -e "${base}^{commit}"` succeeds
    # only when the object is present in the clone, which is exactly what full
    # history buys. A cat-file whose operand is anchored at HEAD is the
    # opposite case — `git cat-file -p HEAD:package.json` reads a blob at the
    # checked-out commit, which every clone depth already has — so a HEAD
    # operand (but not `HEAD~1` / `HEAD^`, which do reach back) disqualifies
    # the clause. The lookahead stops at a command separator so a later,
    # unrelated HEAD read cannot cancel a genuine probe.
    r"git\s+cat-file\b(?![^\n|;&]*\bHEAD(?![~^]))|"
    r"fetch-tags|--tags|"
    # Change-detection actions that diff the head against a BASE ref need base
    # history: `dorny/paths-filter` with `base:` set, and `tj-actions/changed-files`
    # both run `git diff` against the base branch under the hood, so a shallow
    # checkout breaks them. Treating them as history ops (fail-closed) avoids the
    # false positive of "shallow this, no git-history op found" on a change-gate job.
    r"dorny/paths-filter|tj-actions/changed-files|"
    r"nx\s+affected|--affected|lerna\b.*--since|\[origin/|\.\.\.[A-Za-z]",
    re.I)

# A job whose NAME signals history work (the actual git op is often hidden in a
# repo script the YAML invokes, e.g. `node .github/scripts/auto-changeset.ts`).
_HISTORY_JOB_NAME_RE = re.compile(
    r"changeset|changelog|release|version|snapshot|publish", re.I)

# A `run:` block routinely breaks ONE shell command across several lines with a
# trailing backslash, and every clause of `_GIT_HISTORY_RE` is line-scoped. Join
# the continuations back together before matching, so a git command whose
# history-revealing operand sits on the next line is still seen as one command.
_LINE_CONTINUATION_RE = re.compile(r"\\\n[ \t]*")


def _has_git_history_op(text: str) -> bool:
    """True when `text` runs a git operation that needs full history. Applied to
    every surface `_GIT_HISTORY_RE` is matched against, so line continuations are
    joined in exactly one place."""
    return bool(_GIT_HISTORY_RE.search(_LINE_CONTINUATION_RE.sub(" ", text)))


def _step_uses_ref(step: dict) -> str:
    """The raw `uses:` string (local `./...` / `$/...` refs included, unlike
    `_step_uses`, which is scoped to remote actions)."""
    return str(step.get("uses") or "")


# A local `uses:` reference written inside an action or reusable-workflow file
# (`./…` or the self-repository `$/…`), quoted or bare.
_NESTED_LOCAL_USES_RE = re.compile(r"['\"]?uses['\"]?:\s*['\"]?((?:\./|\$/)[^\s'\"#]+)")
# How many local references deep the history read follows a chain before it
# gives up. Past the cap the chain is unproven, so it fails CLOSED.
_MAX_LOCAL_ACTION_DEPTH = 16


def _local_ref_path(root: Path, ref: str) -> "Path | None":
    """`<root>/<ref[2:]>` for a local ref (`./…` or `$/…`), or None when that
    path lands outside the repository root (`$/../x`, `$//abs`): a file there
    is not this repository's, so the caller fails CLOSED on it."""
    base = root / ref[2:]  # strip leading "./" or "$/"
    try:
        root_n = os.path.normpath(os.path.abspath(root))
        base_n = os.path.normpath(os.path.abspath(base))
        if os.path.commonpath([root_n, base_n]) != root_n:
            return None
    except ValueError:
        return None
    return base


def _read_local_history_file(root: Path, ref: str) -> "str | None":
    """Text of the file a local ref names: `<root>/<ref[2:]>` itself when it is
    a `.yml`/`.yaml` (a reusable workflow), else that directory's `action.yml`
    or `action.yaml`. None when no candidate file is readable, or when the
    ref resolves outside the repository root."""
    base = _local_ref_path(root, ref)
    if base is None:
        return None
    candidates = [base] if base.suffix in (".yml", ".yaml") else [
        base / "action.yml", base / "action.yaml"]
    for cand in candidates:
        try:
            return cand.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):   # ValueError: an embedded NUL byte
            continue
    return None


def _local_action_needs_history(root: Path, ref: str) -> bool:
    """True when the local action `ref` names, or any local action or reusable
    workflow it reaches through nested `uses:` references, runs a git-history
    op. A composite action routinely delegates to another local action, and the
    inner one can be the step that walks history. Any link that cannot be read
    (or a chain deeper than `_MAX_LOCAL_ACTION_DEPTH`) fails CLOSED: it cannot
    be PROVEN history-free. A cycle terminates on the visited set."""
    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(ref, 0)]
    while queue:
        cur, depth = queue.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if depth > _MAX_LOCAL_ACTION_DEPTH:
            return True
        text = _read_local_history_file(root, cur)
        if text is None or _has_git_history_op(text):
            return True
        queue += [(m.split("@")[0].strip(), depth + 1)
                  for m in _NESTED_LOCAL_USES_RE.findall(text)]
    return False


def _index_local_git_actions(root: Path,
                             parsed: list[tuple[str, dict, str]]) -> set[str]:
    """Return the set of local `uses:` refs (e.g. `./.github/actions/changed`,
    or the self-repository `$/.github/actions/changed`) whose composite-action
    file performs a git-history op, directly or through a nested local
    reference (`_local_action_needs_history`)."""
    refs: set[str] = set()
    for _rel, doc, _raw in parsed:
        for job in _wf_jobs(doc).values():
            for s in _job_steps(job):
                u = _step_uses_ref(s).split("@")[0].strip()
                if _is_local_uses(u):
                    refs.add(u)
    # Fail CLOSED on anything unproven (see `_local_action_needs_history`):
    # the cost is at most a missed finding, never advice that breaks a job.
    return {ref for ref in refs if _local_action_needs_history(root, ref)}


def _job_needs_git_history(job: dict, job_name: str = "",
                           local_history_actions: set[str] | None = None) -> bool:
    """True when shallowing this job's checkout would break it."""
    blob = "\n".join(str(s.get("run") or "") for s in _job_steps(job))
    uses_blob = "\n".join(_step_uses_ref(s) for s in _job_steps(job))
    if _has_git_history_op(blob) or _has_git_history_op(uses_blob):
        return True
    # A local composite action the job invokes may run the git op internally
    # (the workflow yaml shows only `uses: ./…` or `uses: $/…`). Consult the
    # per-scan index.
    if local_history_actions:
        for s in _job_steps(job):
            if _step_uses_ref(s).split("@")[0].strip() in local_history_actions:
                return True
    # Name-based fallback: a changeset/release/version job almost always needs
    # history even when the op lives in an invoked script. Conservative on
    # purpose — never recommend shallowing one of these.
    return bool(_HISTORY_JOB_NAME_RE.search(job_name))


# ---- Job-level reachability from a pull request ------------------------------
#
# `_wf_is_pr_gating` answers the question at the WORKFLOW level. A workflow can
# be triggered by `pull_request` and still hold a job whose own `if:` can never
# be true for a pull-request event — a heavy job scoped to pushes on a release
# branch, or to manual dispatch. No pull request ever waits for that job, so a
# job-scoped fact about the PR path must not grade the repository on it.
#
# The parser below is deliberately TINY and FAILS CLOSED. It recognises exactly
# one atom — `github.event_name` compared with a string literal by `==` or `!=`
# — combined with `&&`, `||` and parentheses. A variable, a function call, any
# other context reference, or text it cannot fully consume makes the whole
# expression unresolvable, and an unresolvable `if:` leaves the job ON the PR
# path. A wrong exclusion silently drops a real finding, which is the one
# outcome worth designing against; a missed exclusion only costs a finding that
# was already being reported.
_EVENT_NAME_TOKEN = "github.event_name"
_IF_TOKEN_RE = re.compile(
    r"\s*(?:(\()|(\))|(\|\|)|(&&)|(==|!=)|('[^']*'|\"[^\"]*\")|([A-Za-z_][\w.\-]*))")


class _UnresolvableIf(Exception):
    """The `if:` contains something this parser will not reason about."""


def _tokenize_if(expr: str) -> list[tuple[str, str]]:
    # `${{ ... }}` is optional around a job-level `if:`; both forms are common.
    text = expr.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    if "${{" in text or "}}" in text:
        raise _UnresolvableIf("nested or partial expression syntax")
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _IF_TOKEN_RE.match(text, pos)
        if not m:
            raise _UnresolvableIf(f"unrecognized text at offset {pos}")
        pos = m.end()
        lp, rp, or_, and_, cmp_, lit, word = m.groups()
        if lp:
            out.append(("(", lp))
        elif rp:
            out.append((")", rp))
        elif or_:
            out.append(("||", or_))
        elif and_:
            out.append(("&&", and_))
        elif cmp_:
            out.append(("cmp", cmp_))
        elif lit is not None:
            out.append(("lit", lit[1:-1]))
        else:
            if word != _EVENT_NAME_TOKEN:
                # Any other identifier — `needs.*`, `env.*`, `success`,
                # `contains`, `matrix.event`, a bare boolean — is out of scope.
                raise _UnresolvableIf(f"unsupported reference {word!r}")
            out.append(("event", word))
    if not out:
        raise _UnresolvableIf("empty expression")
    return out


def _eval_if_for_event(tokens: list[tuple[str, str]], event_name: str) -> bool:
    """Evaluate the tokenized expression with `github.event_name` bound to
    `event_name`. Raises `_UnresolvableIf` on anything outside the grammar."""
    pos = 0

    def peek() -> str | None:
        return tokens[pos][0] if pos < len(tokens) else None

    def parse_or() -> bool:
        nonlocal pos
        val = parse_and()
        while peek() == "||":
            pos += 1
            val = parse_and() or val   # no short circuit: both sides must parse
        return val

    def parse_and() -> bool:
        nonlocal pos
        val = parse_atom()
        while peek() == "&&":
            pos += 1
            val = parse_atom() and val
        return val

    def parse_atom() -> bool:
        nonlocal pos
        if peek() == "(":
            pos += 1
            val = parse_or()
            if peek() != ")":
                raise _UnresolvableIf("unbalanced parenthesis")
            pos += 1
            return val
        # the only comparison shape: event_name OP 'literal', either order
        kinds = [t[0] for t in tokens[pos:pos + 3]]
        if kinds == ["event", "cmp", "lit"]:
            op, want = tokens[pos + 1][1], tokens[pos + 2][1]
        elif kinds == ["lit", "cmp", "event"]:
            op, want = tokens[pos + 1][1], tokens[pos][1]
        else:
            raise _UnresolvableIf("not an event_name comparison")
        pos += 3
        # GitHub Actions does case-insensitive string comparison
        event = event_name.casefold()
        expected = want.casefold()
        return (event == expected) if op == "==" else (event != expected)

    result = parse_or()
    if pos != len(tokens):
        raise _UnresolvableIf("trailing tokens")
    return result


_PR_EVENT_NAMES = ("pull_request", "pull_request_target")


def _wf_pr_event_names(doc: dict) -> list[str]:
    """The pull-request event names this workflow is actually triggered by."""
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        names = [on]
    elif isinstance(on, list):
        names = [str(e) for e in on]
    elif isinstance(on, dict):
        names = [str(k) for k in on]
    else:
        names = []
    return [n for n in names if n in _PR_EVENT_NAMES]


def _job_if_excludes_pull_requests(job: dict, doc: dict) -> bool:
    """True only when the job's own `if:` is provably FALSE for every
    pull-request event the workflow is triggered by — i.e. no pull request can
    ever wait for this job. Fails closed: anything unresolvable returns False,
    keeping the job on the PR path."""
    cond = job.get("if")
    if not isinstance(cond, str) or not cond.strip():
        return False
    pr_events = _wf_pr_event_names(doc)
    if not pr_events:
        return False
    try:
        tokens = _tokenize_if(cond)
        # `!=` is why the workflow's OWN trigger list matters: on a workflow
        # triggered by `pull_request_target`, `event_name != 'pull_request'`
        # is TRUE and the job still gates pull requests.
        return not any(_eval_if_for_event(tokens, ev) for ev in pr_events)
    except _UnresolvableIf:
        return False


def _composite_action_docs(root: Path,
                           errors: list[str] | None = None,
                           raw_out: dict[str, str] | None = None) -> list[tuple[str, dict]]:
    """Local composite actions' parsed action.yml files - setup (and its
    caching / pinning) frequently lives there rather than in the workflow.
    With `errors`, an action file that cannot be read or parsed, or is not a
    mapping, is appended by name (the collector records them as
    `data_sources.composite_parse_errors`, the way it records
    `workflow_parse_errors`). With `raw_out`, each parsed file's raw text is
    kept by name, so a caller that also needs the comments YAML drops reads
    the same bytes instead of opening the file again."""
    out: list[tuple[str, dict]] = []
    for pattern in ("action.yml", "action.yaml"):
        for path in sorted((root / ".github" / "actions").rglob(pattern)):
            rel = str(path.relative_to(root))
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                doc = yaml.safe_load(text)
            except (OSError, yaml.YAMLError):
                if errors is not None:
                    errors.append(rel)
                continue
            if isinstance(doc, dict):
                out.append((rel, doc))
                if raw_out is not None:
                    raw_out[rel] = text
            elif errors is not None:
                errors.append(rel)
    return out


def _unreadable_step_sources(parsed: list[tuple[str, dict, str]],
                             root: Path) -> tuple[list[str], list[str]]:
    """(step lists that are not lists, composite action files that did not
    parse), by name. A job whose `steps:` is a mapping or a string, or a
    composite whose `runs.steps` is, reads as having no steps at all; neither
    may go unrecorded. An absent or empty `steps:` is not listed (a job that
    calls a reusable workflow has none)."""
    bad_lists: list[str] = []
    for rel, doc, _raw in parsed:
        for jid, job in _wf_jobs(doc).items():
            steps = job.get("steps")
            if steps is not None and not isinstance(steps, list):
                bad_lists.append(f"{rel}: jobs.{jid}.steps")
    parse_errors: list[str] = []
    for rel, doc in _composite_action_docs(root, parse_errors):
        runs = doc.get("runs")
        steps = runs.get("steps") if isinstance(runs, dict) else None
        if steps is not None and not isinstance(steps, list):
            bad_lists.append(f"{rel}: runs.steps")
    return bad_lists, parse_errors


# ---- Setup actions whose caching is on by default ----------------------------
#
# CANONICAL SOURCE: `skills/ci-speedup/scripts/collect_runs.py` —
# `_OPT79_SETUP_CACHE_INPUTS`, `_OPT79_SETUP_NODE_AUTO_CACHE_FROM_MAJOR`,
# `_OPT79_SETUP_NODE_V5_PM_RE`, `_OPT79_SETUP_NODE_NPM_RE`, `_OPT79_OFF_VALUES`,
# `_opt79_ref_major`, `_opt79_setup_node_auto_cache` and
# `_opt79_setup_cache_state`. A COPY, not an import (each skill installs
# standalone); `tests/test_ci_score_setup_cache_parity.py` pins the two tables.
# `{action: (the input that switches its cache, the first major whose default
# is ON, or None when the default is OFF)}`; every other setup-* action is
# switched by `cache:` and is off without it. A SHA or branch ref names no
# major, so in this shared verdict a default-on action pinned by SHA counts as
# caching: the shared verdict never reads a `# v5` comment beside the SHA,
# exactly as in ci-speedup. ci-score's rules live in the wrapper or walk
# below (`_step_caches_by_default`, `_dependency_cache_walk`), never here.
_SETUP_CACHE_INPUTS: dict[str, tuple[str, int | None]] = {
    "actions/setup-go": ("cache", 4),
    "astral-sh/setup-uv": ("enable-cache", 5),
    "ruby/setup-ruby": ("bundler-cache", None),
}
_SETUP_NODE_AUTO_CACHE_FROM_MAJOR = 5
_SETUP_NODE_V5_PM_RE = re.compile(r"^(?:\^)?(npm|yarn|pnpm)@")
_SETUP_NODE_NPM_RE = re.compile(r"(\^)?npm(@.*)?")   # fullmatch = JS ^…$
_SETUP_OFF_VALUES = ("false", "no", "off")
# Actions that restore a dependency cache of their own without `cache` in the
# action name or an `owner/setup-*` shape (ci-speedup's
# `_OPT79_OTHER_CACHE_USES_RE`, minus the ones `_CACHE_ACTION_RE` already
# matches). Gradle's are on unless `cache-disabled` is set true.
_DEFAULT_ON_CACHE_ACTIONS_RE = re.compile(
    r"^(gradle/actions/setup-gradle|gradle/gradle-build-action|bahmutov/npm-install)$",
    re.IGNORECASE)


def _ref_major(ref: str) -> int | None:
    """The major a `uses:` ref names (`v5`, `v5.1`, `5.1.2`), or None for a
    SHA (even one starting with digits) or a branch."""
    m = re.fullmatch(r"v?(\d+)(?:\.\d+){0,2}", (ref or "").strip())
    if not m or len(m.group(1)) > 4:
        return None
    return int(m.group(1))


def _setup_node_auto_cache(major: int | None, package_json: Any) -> str:
    """setup-node v5+'s automatic cache for one parsed `package.json`: `on`,
    `off`, or `maybe` (file unread/invalid, or a SHA ref on which the v5 and
    v6+ rules disagree)."""
    if not isinstance(package_json, dict):
        return "maybe"

    def _v5() -> bool:
        pm = package_json.get("packageManager")
        return isinstance(pm, str) and bool(_SETUP_NODE_V5_PM_RE.match(pm))

    def _v6() -> bool:
        dev = package_json.get("devEngines")
        dev_pm = dev.get("packageManager") if isinstance(dev, dict) else None
        items = (dev_pm if isinstance(dev_pm, list) else [dev_pm]) if dev_pm else []
        for obj in items:
            nm = obj.get("name") if isinstance(obj, dict) else None
            if isinstance(nm, str) and _SETUP_NODE_NPM_RE.fullmatch(nm):
                return True
        pm = package_json.get("packageManager")
        return isinstance(pm, str) and bool(_SETUP_NODE_NPM_RE.fullmatch(pm))

    if major is None:
        a, b = _v5(), _v6()
        return ("on" if a else "off") if a == b else "maybe"
    if major == _SETUP_NODE_AUTO_CACHE_FROM_MAJOR:
        return "on" if _v5() else "off"
    return "on" if _v6() else "off"


def _setup_cache_state(action: str, ref: str, with_in: dict[str, Any],
                       package_json: Any = None) -> str:
    """Whether a `setup-*` step restores a cache: `on`, `off`, `maybe` or
    `expr` (its cache input is an unevaluated `${{ … }}` expression). An input
    that is SET decides it; an absent one falls back to the action's default."""
    action = action.strip().lower()
    major = _ref_major(ref)
    input_name, on_from = _SETUP_CACHE_INPUTS.get(action, ("cache", None))
    raw = with_in.get(input_name)
    node_auto = action == "actions/setup-node" and (
        major is None or major >= _SETUP_NODE_AUTO_CACHE_FROM_MAJOR)
    if raw is not None:
        val = str(raw).strip()
        if "${{" in val:
            return "expr"
        if not val and node_auto:
            raw = None   # setup-node v5+: an EMPTY `cache:` falls through to auto
        else:
            return "off" if (not val or val.lower() in _SETUP_OFF_VALUES) else "on"
    if on_from is not None and (major is None or major >= on_from):
        return "on"
    if node_auto:
        pmc = with_in.get("package-manager-cache")
        pmc_val = "" if pmc is None else str(pmc).strip()
        if "${{" in pmc_val:
            return "expr"
        # `(input || 'true').toUpperCase() === 'TRUE'`
        if pmc_val and pmc_val.upper() != "TRUE":
            return "off"
        return _setup_node_auto_cache(major, package_json)
    return "off"


_BOM = chr(0xFEFF)   # built at runtime: no invisible character in the source
_PJ_BOM_REASON = ("package.json starts with a byte-order mark, which setup-node's "
                  "JSON parse rejects")
# Reasons that DECIDE the default off (named as "earns no default cache"),
# as opposed to the ones that leave it undecided ("could not be resolved").
_DECIDED_OFF_REASONS = frozenset({_PJ_BOM_REASON})


def _root_package_json(root: Path) -> tuple[Any, str | None]:
    """(the repo-root package.json as setup-node parses it, why it could not
    be). Absent: `(None, None)` - setup-node then finds no package manager, so
    the default is not credited and not named. Present but unusable: `(None or
    the parsed non-object, reason)`; the shared verdict reads that as
    undecided, and the caching fact names the reason
    (`_unresolved_setup_defaults`). setup-node reads the file with
    `fs.readFileSync(…, 'utf-8')` and hands it to `JSON.parse` with no BOM
    strip, so a byte-order mark makes it fail there too: that one is decided
    off (setup-node cannot use the file), named as "earns no default cache",
    never credited."""
    path = root / "package.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError):
        return None, "package.json could not be read"
    if text.startswith(_BOM):
        return None, _PJ_BOM_REASON
    try:
        doc = json.loads(text)
    except ValueError:
        return None, "package.json is not valid JSON"
    if not isinstance(doc, dict):
        return doc, "package.json is not a JSON object"
    return doc, None


def _checkout_state(state: str, step: dict) -> str:
    """Where `$GITHUB_WORKSPACE` stands after `step` (ci-speedup's
    `_opt79_checkout_state`): `none` (nothing checked out yet), `root` (this
    repository at the workspace root) or `moved` (a sub-path or another
    repository - sticky, since the root files are then not the ones this scan
    read)."""
    ua = _step_uses(step)
    if not ua or ua[0].strip().lower() != "actions/checkout":
        return state
    w = step.get("with") if isinstance(step.get("with"), dict) else {}
    path = str(w.get("path") or "").strip()
    if w.get("repository") or path not in ("", ".", "./"):
        return "moved"
    return "moved" if state == "moved" else "root"


def _is_background(step: dict) -> bool:
    v = step.get("background")
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _workspace_states(steps: list[dict], group_of: dict[int, int],
                      start: str = "none") -> list[str]:
    """`_checkout_state` as each step sees it (ci-speedup's
    `_opt79_workspace_states`): only a checkout that has FINISHED counts - one
    earlier in sequence, or in an earlier `parallel:` group (a group ends in an
    implicit wait). A checkout that may still be running - in the step's own
    group, or a top-level `background: true` checkout before it - leaves the
    workspace `racing`, never `root`, so a default that reads the workspace is
    not credited on declaration order alone."""
    states: list[str] = []
    done = start
    racing_bg = False
    for st in steps:
        g = group_of.get(id(st))
        racing = racing_bg or (g is not None and any(
            group_of.get(id(o)) == g and o is not st
            and _checkout_state("none", o) != "none" for o in steps))
        states.append("racing" if racing else done)
        if _checkout_state("none", st) == "none":
            continue
        if g is None and _is_background(st):
            racing_bg = True
        else:
            done = _checkout_state(done, st)
    return states


# ---- ci-score's own narrowing of the copied verdict (OD-CS23)
#
# ci-speedup counts an unseen cache as ON because there that can only withhold
# a savings claim; here the same choice would hand out a pass. So the rules
# below sit in the per-step wrapper (`_step_caches_by_default`) or the walk
# that calls it (`_dependency_cache_walk`), never in the shared
# `_setup_cache_state` (which the parity test pins verdict for verdict). Every
# rule but one only WITHHOLDS credit the shared verdict gave:
#
# 1. A SHA-pinned action carrying a version comment (`@<sha> # v3.5.0`,
#    `# tag=v3.5.0`, `# ratchet:owner/action@v3`, `# pin@v3`) is re-checked at
#    the commented major: a comment naming a release from before caching
#    became the default earns nothing, and so does a comment that names no
#    release at all (it is there, so the pin is meant to be read by it, and
#    what it says cannot be read). A date (`# 2024-05-01`), a range
#    (`# 20.x`) or a major no release of the action has had is not a release.
#    No comment: the shared verdict stands. Gradle's and npm-install's
#    default-on actions always cache, so their comments are not read.
# 2. setup-uv's input-free default (`enable-cache: auto`) caches only on
#    GitHub-hosted runners, so it is credited only when the job's `runs-on` is
#    one of GitHub's published image labels. Any other runner, including a
#    larger runner whose label its owner names, earns the credit with
#    `enable-cache: true`.
# 3. (The one rule that may ADD credit, owner decision 3.) A SHA-pinned
#    setup-node whose v5 and v6 rules disagree (`maybe`) is credited when it
#    carries version comment(s) and EVERY commented major caches for this repo.
#
# And four readings the shared verdict leaves open (all withhold only):
# `package-manager-cache: ${{ … }}` is read as its default `true`, so it
# still needs a manager in package.json; setup-go's default needs a finished
# root checkout and a go.sum (its cache key); and a checkout that may still
# be running (`parallel:` sibling, earlier `background: true`) does not count.
# `with:` keys are read case-insensitively in both directions (GitHub matches
# action inputs that way): the shared verdict is computed from the lower-cased
# inputs, so `Cache: npm` opts in exactly as `Cache: false` opts out.
#
# A withhold this scan could not decide is never silent: it is recorded with
# its reason (`{file, step, reason}`) and named in the fact's evidence. A
# plain off (an opt-out, a pre-default release, no checkout at all, no
# package manager in package.json) names nothing.
_SHA_REF_RE = re.compile(r"^[0-9a-fA-F]{40}$")
# `uses: owner/action@<sha>` and whatever follows on the same line.
_USES_SHA_LINE_RE = re.compile(
    r"""uses:\s*['"]?([\w.-]+/[\w./-]+)@([0-9a-fA-F]{40})\b([^\n]*)""")
# A YAML comment after the ref: `#` at the start of the rest of the line or
# after whitespace, a quote, or a closing `}` / `]` / `,` of a flow mapping.
_TRAILING_COMMENT_RE = re.compile(r"""(?:^|[\s'"}\],])#(.*)$""")
# The release a pin comment names: `v3`, `3.5.0`, `tag=v3.5.0`,
# `ratchet:owner/action@v3`, `pin@v3`, `owner/action@v3`. A number followed
# by `-` (a date such as `2024-05-01`) or by `.x` / another word character (a
# range such as `20.x`) names no release.
_COMMENT_VERSION_RE = re.compile(
    r"""^\s*(?:tag\s*=\s*|ratchet:\s*[\w./-]*@|pin@|[\w.-]+/[\w./-]+@|@)?v?(\d{1,4})(?:\.\d+){0,2}(?![\w-]|\.\w)""",
    re.IGNORECASE)
# The highest major a version comment may name for each default-on action
# (its current major plus headroom); anything above, or below 1, is a toolchain
# version, a date or a typo, never one of the action's releases.
_PLAUSIBLE_MAJOR = {"actions/setup-go": 9, "actions/setup-node": 9,
                    "astral-sh/setup-uv": 15, "ruby/setup-ruby": 5}
_PLAUSIBLE_MAJOR_DEFAULT = 20
# GitHub's published hosted-runner image labels. Source: the "GitHub-hosted
# runners" reference
# (https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
# which lists the ubuntu-*, windows-*, macos-* and `xcode-27` labels, plus the
# macOS larger-runner labels. Larger Linux / Windows runners carry names their
# owner chooses, which no pattern can tell from a self-hosted label, so they
# earn the credit the same way any runner does: with `enable-cache: true`.
_GITHUB_HOSTED_LABEL_RE = re.compile(
    r"^(?:ubuntu-(?:latest|slim|\d+\.\d+(?:-arm)?)"
    r"|windows-(?:latest|\d{4}(?:-vs\d{4})?|11(?:-vs\d{4})?-arm)"
    r"|macos-(?:latest|\d+)(?:-intel|-large|-xlarge)?"
    r"|xcode-\d+)$", re.IGNORECASE)
_RUNNER_GATED_DEFAULTS = {"astral-sh/setup-uv": "enable-cache"}
_UNDECIDED_TAIL = "and setup-node v5 and v6 decide this package.json differently"
_SHA_UNDECIDED = f"a SHA pin names no release, {_UNDECIDED_TAIL}"
_REF_UNDECIDED = f"a branch or SHA ref names no release, {_UNDECIDED_TAIL}"
_RUNNER_UNREAD_TAIL = ("so this scan cannot tell whether the runner is GitHub-hosted, the "
                       "only place setup-uv's default (enable-cache: auto) caches")
_RUNS_ON_EXPR = f"runs-on is an expression, {_RUNNER_UNREAD_TAIL}"
_RUNS_ON_GROUP = f"runs-on is a runner group or object, {_RUNNER_UNREAD_TAIL}"
_WS_RACING = ("a checkout may still be running when this step starts (a parallel: "
              "sibling, or an earlier background: true checkout), so what the action "
              "reads from the workspace is not settled")
_WS_MOVED = ("the checkout before this step went to a sub-path or another repository, "
             "so the workspace root is not the tree this scan read")


def _sha_version_comments(texts: list[str],
                          unparsed: dict[tuple[str, str], list[str]] | None = None,
                          ) -> dict[tuple[str, str], set[int | None]]:
    """{(action, sha): {majors}} from version comments on `uses:
    owner/action@<sha>` lines in the raw workflow / composite text (YAML
    parsing drops comments). A comment that names no release (or a major
    outside the action's plausible range) adds None, and with `unparsed` its
    text is kept under the same key, for the evidence."""
    out: dict[tuple[str, str], set[int | None]] = {}
    for text in texts:
        for m in _USES_SHA_LINE_RE.finditer(text or ""):
            c = _TRAILING_COMMENT_RE.search(m.group(3))
            if not c:
                continue
            action = m.group(1).lower()
            key = (action, m.group(2).lower())
            v = _COMMENT_VERSION_RE.match(c.group(1))
            major = int(v.group(1)) if v else None
            if major is not None and not (
                    1 <= major <= _PLAUSIBLE_MAJOR.get(action, _PLAUSIBLE_MAJOR_DEFAULT)):
                major = None
            out.setdefault(key, set()).add(major)
            if major is None and unparsed is not None:
                unparsed.setdefault(key, []).append(c.group(1).strip())
    return out


def _runs_on_github_hosted(runs_on: Any) -> bool:
    """A published GitHub-hosted image label, as a string or a one-item list.
    Expressions, other labels, groups/objects and multi-label lists: False."""
    if isinstance(runs_on, list) and len(runs_on) == 1:
        runs_on = runs_on[0]
    return isinstance(runs_on, str) and bool(_GITHUB_HOSTED_LABEL_RE.match(runs_on.strip()))


def _runs_on_unread(runs_on: Any) -> str | None:
    """Why this scan cannot read the runner from `runs-on` (an expression, or
    a group / object), or None when it is literal labels."""
    if isinstance(runs_on, dict):
        return _RUNS_ON_GROUP
    items = runs_on if isinstance(runs_on, list) else [runs_on]
    if any(isinstance(x, str) and "${{" in x for x in items):
        return _RUNS_ON_EXPR
    if any(isinstance(x, (dict, list)) for x in items):
        return _RUNS_ON_GROUP
    return None


def _go_sum_check(root: Path, dep_path: Any) -> tuple[bool, str | None]:
    """(a go.sum setup-go keys on exists, why it could not be checked).
    setup-go keys its cache on `cache-dependency-path` (files or globs, one
    per line) or, without it, on the workspace's `go.sum`; with no such file it
    restores nothing. An expression, an absolute path, one leaving the
    repository, or only `!` exclusions cannot be checked here: not credited
    (fail closed), and the reason is returned so the fact can name it."""
    val = "" if dep_path is None else str(dep_path)
    if not val.strip():
        return (root / "go.sum").is_file(), None
    if "${{" in val:
        return False, ("cache-dependency-path is an expression, so the go.sum setup-go "
                       "keys its cache on cannot be checked")
    unread: str | None = None
    includes = 0
    for line in val.splitlines():
        p = line.strip().strip("'\"")
        if p.startswith("./"):
            p = p[2:]
        if not p or p.startswith("!"):
            continue
        includes += 1
        if p.startswith("/"):
            unread = unread or (f"cache-dependency-path '{p}' is an absolute path, outside "
                                f"the tree this scan read")
            continue
        if ".." in Path(p).parts:
            unread = unread or (f"cache-dependency-path '{p}' leaves the repository (..), "
                                f"so it cannot be checked")
            continue
        if any(ch in p for ch in "*?["):
            try:
                if any(x.is_file() for x in root.glob(p)):
                    return True, None
            except (ValueError, NotImplementedError, OSError):
                continue
        elif (root / p).is_file():
            return True, None
    if unread:
        return False, unread
    if not includes:
        return False, ("cache-dependency-path has only exclusion (!) patterns, so no "
                       "go.sum to key on can be checked")
    return False, None


def _step_caches_by_default(step: dict, package_json: Any, workspace_unknown: bool,
                            version_comments: dict[tuple[str, str], set[int | None]] | None = None,
                            github_hosted: bool = True, *, root: Path | None = None,
                            package_json_problem: str | None = None,
                            unresolved: list[dict[str, str]] | None = None,
                            rel: str = "", workspace: str | None = None,
                            runner_unread: str | None = None,
                            comment_texts: dict[tuple[str, str], list[str]] | None = None,
                            ) -> bool:
    """A step whose own action restores a dependency cache with no `cache:`
    input (or through a differently-named input): ci-speedup's verdict, then
    ci-score's rules (see above). `workspace_unknown`: no root checkout of this
    repository has finished before the step (none yet, a moved one, or one
    that may still be running), so nothing read from the workspace counts;
    `workspace` is that state (`none`, `moved`, `racing`), for the reason.
    `root` None skips the go.sum check (the parity harness has no tree).
    `runner_unread` is why the job's `runs-on` could not be read. A withhold
    this scan could not decide is appended to `unresolved` as
    `{file, step, reason}`; a plain off appends nothing."""
    ua = _step_uses(step)
    if not ua:
        return False
    with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
    with_lc = {str(k).strip().lower(): v for k, v in with_.items()}
    if _DEFAULT_ON_CACHE_ACTIONS_RE.match(ua[0]):
        return all(str(w.get("cache-disabled", "")).strip().lower() != "true"
                   for w in (with_, with_lc))
    if not re.match(r"^[\w.-]+/setup-[\w.-]+$", ua[0], re.IGNORECASE):
        return False
    action = ua[0].strip().lower()
    ref = (ua[1] or "").strip()
    pj = None if workspace_unknown else package_json
    # The shared verdict reads the lower-cased inputs: GitHub matches input
    # names case-insensitively, so `Cache: npm` is `cache: npm`.
    shared = _setup_cache_state(ua[0], ua[1], with_lc, pj)
    eff = dict(with_lc)
    if (action == "actions/setup-node"
            and "${{" in str(eff.get("package-manager-cache") or "")
            and "${{" not in str(eff.get("cache") or "")):
        # the expression is the only thing undecided: read it as its default
        eff["package-manager-cache"] = "true"
    state = _setup_cache_state(ua[0], ua[1], eff, pj)
    key = (action, ref.lower())
    comments = ((version_comments or {}).get(key, set())
                if _SHA_REF_RE.match(ref) else set())
    texts = (comment_texts or {}).get(key) or []
    comment_reason = (f"version comment '{texts[0]}' names no release" if texts
                      else "a version comment names no release")
    ws_reason = {"racing": _WS_RACING, "moved": _WS_MOVED}.get(workspace or "")

    def withhold(reason: str | None) -> bool:
        if reason and unresolved is not None:
            unresolved.append({"file": rel, "step": f"{ua[0]}@{ref}", "reason": reason})
        return False

    def at(major: int) -> str:
        return _setup_cache_state(ua[0], f"v{major}", eff, pj)

    credited = shared in ("on", "expr") and state in ("on", "expr")
    if (not credited and shared in ("on", "expr", "maybe") and state == "maybe" and comments
            and None not in comments and all(at(m) == "on" for m in comments)):
        credited = True   # owner decision 3: every commented release caches
    if not credited:
        if state != "maybe":
            return False
        if workspace_unknown:
            # The workspace is the cause, never package.json: name a racing or
            # moved checkout when the default would otherwise not be off; no
            # checkout at all is a plain off.
            would = _setup_cache_state(ua[0], ua[1], eff, package_json)
            return withhold(ws_reason if would != "off" else None)
        if package_json_problem:
            return withhold(package_json_problem)
        if comments:
            # a comment naming releases decides it (none caches): a plain off
            return withhold(comment_reason if None in comments else None)
        if isinstance(pj, dict) and _ref_major(ref) is None:
            return withhold(_SHA_UNDECIDED if _SHA_REF_RE.match(ref) else _REF_UNDECIDED)
        return False
    if action == "actions/setup-go" and eff.get("cache") is None:
        if workspace_unknown:
            return withhold(ws_reason)
        if root is not None:
            present, why = _go_sum_check(root, eff.get("cache-dependency-path"))
            if not present:
                return withhold(why)
    gate = _RUNNER_GATED_DEFAULTS.get(action)
    if gate and eff.get(gate) is None and not github_hosted:
        return withhold(runner_unread)
    if None in comments:
        return withhold(comment_reason)
    return all(at(m) in ("on", "expr") for m in comments)


def _dependency_cache_walk(parsed: list[tuple[str, dict, str]], root: Path,
                           composites: list[tuple[str, dict]],
                           composite_raw: dict[str, str],
                           ) -> tuple[list[str], list[dict[str, str]]]:
    """(files that configure dependency caching, setup defaults left
    unresolved). One walk over every job and local composite action."""
    hits: list[str] = []
    unresolved: list[dict[str, str]] = []
    package_json, pj_problem = _root_package_json(root)
    raw_texts = [raw for _rel, _doc, raw in parsed]
    raw_texts.extend(composite_raw.get(crel, "") for crel, _cdoc in composites)
    comment_texts: dict[tuple[str, str], list[str]] = {}
    version_comments = _sha_version_comments(raw_texts, comment_texts)

    def scan(rel: str, steps: list[dict], group_of: dict[int, int], start: str,
             github_hosted: bool, runner_unread: str | None) -> None:
        # setup-node and setup-go read the workspace when their step runs, so
        # it counts only once a root checkout has FINISHED earlier in the job.
        states = _workspace_states(steps, group_of, start)
        for step, ws in zip(steps, states):
            ua = _step_uses(step)
            with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
            if ua and _CACHE_ACTION_RE.search(ua[0]):
                hits.append(rel)
            elif ua and "setup-" in ua[0] and any(
                    str(k).strip().lower() in ("cache", "enable-cache")
                    and v not in (False, "false", None, "")
                    for k, v in with_.items()):
                hits.append(rel)
            elif _step_caches_by_default(step, package_json, ws != "root",
                                         version_comments, github_hosted, root=root,
                                         package_json_problem=pj_problem,
                                         unresolved=unresolved, rel=rel, workspace=ws,
                                         runner_unread=runner_unread,
                                         comment_texts=comment_texts):
                hits.append(rel)

    # The same walk `_step_sources` makes, keeping each job's `runs-on` and its
    # `parallel:` groups.
    for rel, doc, _raw in parsed:
        for job in _wf_jobs(doc).values():
            group_of: dict[int, int] = {}
            steps = _job_steps(job, group_of)
            runs_on = job.get("runs-on")
            scan(rel, steps, group_of, "none",
                 _runs_on_github_hosted(runs_on), _runs_on_unread(runs_on))
    # A local composite action is itself read from the checked-out workspace,
    # so its steps start checked out; its runner is its caller's, unknown here.
    for rel, doc in composites:
        if isinstance(doc.get("runs"), dict):
            scan(rel, _composite_steps(doc), {}, "root", False, None)
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for u in unresolved:
        key = (u["file"], u["step"], u["reason"])
        if key not in seen:
            seen.add(key)
            unique.append(u)
    return hits, unique


def _unresolved_evidence(unresolved: list[dict[str, str]]) -> str:
    """`; <step> <verdict>: <reason>` for the first three recorded defaults,
    and how many more there are: what the caching fact appends to its
    evidence, so a withhold is never a silent fail."""
    if not unresolved:
        return ""

    def verdict(reason: str) -> str:
        if reason in _DECIDED_OFF_REASONS:
            return "earns no default cache"
        return "default cache could not be resolved"

    named = "; ".join(f"{u['step'].split('/', 1)[-1]} {verdict(u['reason'])}: {u['reason']}"
                      for u in unresolved[:3])
    more = f" (and {len(unresolved) - 3} more)" if len(unresolved) > 3 else ""
    return f"; {named}{more}"


def _unresolved_setup_defaults(parsed: list[tuple[str, dict, str]],
                               root: Path) -> list[dict[str, str]]:
    """Setup defaults the caching fact could not decide (`{file, step,
    reason}`), for the collector's `data_sources.unresolved_setup_defaults`:
    unread config is visible on every surface, never silently a fail."""
    raw: dict[str, str] = {}
    composites = _composite_action_docs(root, raw_out=raw)
    return _dependency_cache_walk(parsed, root, composites, raw)[1]


def _conc_cancels_basic(conc: Any) -> bool:
    """cancel-in-progress set and not literally false - a templated
    expression is conditional cancellation, which is the practice too."""
    if not isinstance(conc, dict):
        return False
    val = conc.get("cancel-in-progress")
    return val is not None and val is not False


def _practice_facts(parsed: list[tuple[str, dict, str]], root: Path) -> dict[str, Any]:
    """The eleven CI Score v0.1-basic facts. Shape per fact:
    {"state": "pass"|"fail"|"not_applicable", "evidence": str, "files": [..<=3]}.
    Evidence is NEVER empty: a failed check must hand the maintainer the exact
    thing to look at."""
    pr_wfs = [(rel, doc) for rel, doc, _raw in parsed if _wf_is_pr_gating(doc)]
    remote_reusables = _remote_reusable_refs(parsed)
    composite_raw: dict[str, str] = {}
    composites = _composite_action_docs(root, raw_out=composite_raw)
    # steps everywhere the repo's own files define them: workflows + local
    # composite actions (the "runs.steps" shape), leaf steps via the walker
    # (children of `parallel:` groups included).
    step_sources = _step_sources(parsed, composites)

    def fact(state: str, evidence: str, files: list[str]) -> dict[str, Any]:
        # Dedupe preserving order (never re-sort): callers rank meaningfully —
        # the pinning fact puts third-party offenders first, and an alphabetical
        # re-sort once pointed a maintainer at actions/labeler while an unpinned
        # release-credential action sat hidden past the cap.
        return {"state": state, "evidence": evidence,
                "files": list(dict.fromkeys(files))[:3]}

    facts: dict[str, dict[str, Any]] = {}

    # 1. Dependency caching: a cache action (actions/cache incl. /restore
    # /save, Swatinem/rust-cache, buildjet/cache, ccache-action - anything
    # whose action name says cache), or a setup-* step's truthy cache input.
    # Also a setup action whose caching is ON BY DEFAULT or switched by a
    # differently-named input (setup-go v4+, setup-uv v5+, setup-ruby's
    # bundler-cache, setup-node v5+ with a package-manager field,
    # gradle/actions/setup-gradle, gradle/gradle-build-action,
    # bahmutov/npm-install) - ci-speedup's table, copied above, narrowed by
    # ci-score's wrapper. A default left undecided is named in the evidence.
    hits, unresolved = _dependency_cache_walk(parsed, root, composites, composite_raw)
    if hits:
        facts["ci.cache.dependency-cache"] = fact(
            "pass",
            f"dependency caching configured in {len(set(hits))} workflow/action file(s)",
            hits)
    elif remote_reusables:
        facts["ci.cache.dependency-cache"] = fact(
            "not_applicable",
            "CI is delegated to cross-repo reusable workflow(s) "
            f"({', '.join(remote_reusables[:2])}); caching there is not visible from this repo"
            + _unresolved_evidence(unresolved),
            [])
    elif not (_has_dep_manifest(root) or _has_install_signal(step_sources)):
        # No manifest AND no inline install — nothing is installed, so there is
        # nothing to cache: not_applicable, never a fail (OD-CS19). A manifest
        # OR an inline install makes the check applicable (and here, a fail).
        facts["ci.cache.dependency-cache"] = fact(
            "not_applicable",
            "no dependency manifest and no dependency-install step detected; nothing to cache",
            [])
    else:
        # A withheld default is never a silent fail: name it
        # (data_sources.unresolved_setup_defaults records it too).
        evidence = ("no cache action and no setup-* cache input in any workflow or "
                    "local composite action") + _unresolved_evidence(unresolved)
        facts["ci.cache.dependency-cache"] = fact(
            "fail", evidence, [u["file"] for u in unresolved])

    # 2. Build caching: applicable only when a detectable build tool exists.
    tools = sorted({name for name, probe in (
        ("turbo", "turbo.json"), ("nx", "nx.json"),
        ("gradle", "settings.gradle"), ("gradle", "settings.gradle.kts"),
        ("bazel", ".bazelrc")) if (root / probe).exists()})
    if not tools:
        facts["ci.cache.build-cache"] = fact(
            "not_applicable", "no build tool with a cache detected in the repo tree", [])
    else:
        sig = re.compile(r"TURBO_TOKEN|turbo_token|nx-cloud|nx_cloud|gradle-build-action|"
                         r"setup-gradle|sccache")
        hits = [rel for rel, _doc, raw in parsed if sig.search(raw)]
        hits += [rel for rel, steps in step_sources for step in steps
                 if (ua := _step_uses(step)) and _CACHE_ACTION_RE.search(ua[0])]
        facts["ci.cache.build-cache"] = fact(
            "pass" if hits else "fail",
            (f"{'/'.join(tools)} detected; cache mechanism configured in "
             f"{len(set(hits))} file(s)" if hits else
             f"{'/'.join(tools)} detected but no build-cache mechanism found in workflows"),
            hits)

    # 3. Shallow checkout: no checkout STEP on the PR path sets
    # fetch-depth: 0 (structure walk - a comment mentioning it never counts,
    # and a quoted '0' does).
    # A job that WALKS git history is exempt: `fetch-depth: 0` is load-bearing
    # there and shallowing it breaks the job, so it is not an offender (the
    # carve-out above, shared verbatim with ci-speedup). The exemption is
    # per-JOB, not per-workflow: a second job in the same file that takes full
    # history for nothing is still a real finding.
    local_history_actions = _index_local_git_actions(root, parsed)
    offenders = []
    carved_out = 0   # PR-gating jobs that take full history AND need it
    off_pr_path = 0  # jobs whose own `if:` can never be true on a pull request
    off_pr_steps = 0  # jobs whose every full-history checkout STEP is PR-excluded
    for rel, doc in pr_wfs:
        for job_name, job in _wf_jobs(doc).items():
            full_history_steps = [
                step for step in _job_steps(job)
                if str((step.get("with") if isinstance(step.get("with"), dict) else {})
                       .get("fetch-depth")).strip("'\"") == "0"]
            if not full_history_steps:
                continue
            # A job whose own `if:` can never be true for a pull-request event
            # is not on the PR path at all — no pull request waits for it, so
            # its checkout depth is not a PR-speed fact.
            if _job_if_excludes_pull_requests(job, doc):
                off_pr_path += 1
                continue
            # The same PR-reachability read as the job-level `if:` above, on
            # the checkout STEP's own `if:`: a step that can never run for a
            # pull-request event takes no history on the PR path. Fails
            # closed - an unresolvable step `if:` still counts.
            if all(_job_if_excludes_pull_requests(step, doc)
                   for step in full_history_steps):
                off_pr_steps += 1
                continue
            if _job_needs_git_history(job, job_name, local_history_actions):
                carved_out += 1
                continue
            offenders.append(rel)
    uniq_offenders = sorted(set(offenders))
    if offenders:
        evidence = (f"fetch-depth: 0 on {len(uniq_offenders)} PR-gating workflow(s), "
                    f"e.g. {', '.join(uniq_offenders[:3])}")
    elif not pr_wfs:
        evidence = "no PR-gating workflows"
    elif carved_out or off_pr_path or off_pr_steps:
        # A pass that an exemption produced must SAY so. `fetch-depth: 0` is
        # right there in a PR-triggered workflow, and evidence claiming
        # otherwise sends the maintainer to a file that contradicts the card.
        why = []
        if carved_out:
            why.append(f"{carved_out} walk(s) git history, so shallowing the "
                       f"checkout would break the job")
        if off_pr_path:
            why.append(f"{off_pr_path} cannot run on a pull request at all (a "
                       f"job-level if: that is never true for a pull-request event)")
        if off_pr_steps:
            why.append(f"{off_pr_steps} take(s) full history only in a checkout "
                       f"step whose own if: is never true for a pull-request event")
        evidence = (f"{carved_out + off_pr_path + off_pr_steps} job(s) in PR-triggered workflow(s) "
                    f"check out full history and are exempt: " + "; ".join(why) +
                    "; no other PR-gating job takes full history")
    else:
        evidence = ("no PR-gating workflow checks out full history (post-merge "
                    "automation like backport/changelog jobs is exempt - it needs "
                    "history and gates nobody)")
    facts["ci.checkout.shallow-clone"] = fact(
        "fail" if offenders else ("pass" if pr_wfs else "not_applicable"),
        evidence, offenders)

    # 4. Test sharding / matrix: a matrix on a TEST-LIKE job (test/spec/e2e/
    # integration/unit in its id or name), or a matrix whose axis is
    # shard-like on any job.
    hits = []
    has_testish_job = False  # any test-like job at all, matrix or not
    has_shard_axis = False   # any job with a shard-like matrix axis
    for rel, doc, _raw in parsed:
        matched = False
        for jid, job in _wf_jobs(doc).items():
            testish = bool(_TESTISH_RE.search(jid) or _TESTISH_RE.search(str(job.get("name", ""))))
            if testish:
                has_testish_job = True
            strategy = job.get("strategy")
            matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
            if isinstance(matrix, str) and "${{" in matrix:
                # A matrix GENERATED at runtime (`${{ fromJSON(...) }}`): the
                # job runs a matrix whose axes this scan cannot read. On a
                # test-like job that is the practice (the fact is "a test-like
                # job runs a matrix"); on any other job no axis is visible to
                # call shard-like, so the job-name rule decides, as it does
                # for a literal matrix.
                if testish and not matched:
                    hits.append(rel)
                    matched = True
                continue
            if not isinstance(matrix, dict):
                continue
            shard_axis = any(_SHARD_AXIS_RE.search(str(k)) for k in matrix
                             if k not in ("include", "exclude"))
            if shard_axis:
                has_shard_axis = True
            if (testish or shard_axis) and not matched:
                hits.append(rel)
                matched = True
    if hits:
        facts["ci.parallel.test-sharding"] = fact(
            "pass",
            f"shard/matrix strategy on test job(s) in {len(set(hits))} workflow(s)",
            hits)
    elif remote_reusables:
        facts["ci.parallel.test-sharding"] = fact(
            "not_applicable",
            "CI is delegated to cross-repo reusable workflow(s) "
            f"({', '.join(remote_reusables[:2])}); test strategy there is not visible from this repo",
            [])
    elif not (has_testish_job or has_shard_axis):
        # No test-like job anywhere — nothing to shard (OD-CS19).
        facts["ci.parallel.test-sharding"] = fact(
            "not_applicable",
            "no test-like job (test/spec/e2e/integration/unit) detected; nothing to shard",
            [])
    else:
        facts["ci.parallel.test-sharding"] = fact(
            "fail",
            "no test-like job (test/spec/e2e/integration/unit) uses a shard or matrix strategy",
            [])

    # 5. Change-scoped builds: applicable when a task-graph tool this fact can
    # actually recognize is in play (turbo/nx config or commands). Gradle/
    # bazel scoping is not detectable here - n/a, disclosed, never a fail for
    # a mechanism the check cannot see.
    # Step CODE (run: commands and github-script blocks) — code is evidence,
    # comments are not; mastra's hand-rolled affected-tests mechanism lives in
    # script blocks and must be credited like any other.
    step_code = []
    for rel, steps in step_sources:
        for step in steps:
            if isinstance(step.get("run"), str):
                step_code.append((rel, step["run"]))
            with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
            if isinstance(with_.get("script"), str):
                step_code.append((rel, with_["script"]))
    graphish = (root / "turbo.json").exists() or (root / "nx.json").exists() or any(
        re.search(r"\bturbo\b|\bnx\b", code) for _rel, code in step_code)
    if not graphish:
        facts["ci.build.change-scoped"] = fact(
            "not_applicable",
            "no turbo/nx task graph detected (change-scoping is not checkable here)", [])
    else:
        changed_code_re = re.compile(r"changed[-_]?[Ff]iles|\baffected", re.IGNORECASE)
        hits = [rel for rel, code in step_code
                if _SCOPED_CMD_RE.search(code) or changed_code_re.search(code)]
        hits += [rel for rel, steps in step_sources for step in steps
                 if (ua := _step_uses(step)) and any(ua[0].startswith(a) for a in _CHANGED_FILES_ACTIONS)]
        facts["ci.build.change-scoped"] = fact(
            "pass" if hits else "fail",
            (f"change-scoped build/test mechanisms in {len(set(hits))} file(s)" if hits
             else "CI builds/tests run unscoped (no --filter=...[base], affected/changed "
                  "modes, or changed-files step)"),
            hits)

    # 6. Concurrency groups on the PR path.
    hits = []
    for rel, doc in pr_wfs:
        has_group = isinstance(doc.get("concurrency"), (dict, str)) or any(
            isinstance(j.get("concurrency"), (dict, str)) for j in _wf_jobs(doc).values())
        if has_group:
            hits.append(rel)
    facts["ci.trigger.concurrency-groups"] = fact(
        "pass" if hits else ("fail" if pr_wfs else "not_applicable"),
        (f"concurrency group declared on {len(hits)} of {len(pr_wfs)} PR workflow(s)"
         if pr_wfs else "no PR-triggered workflows"),
        # absence-type: on fail the offenders ARE the PR workflows missing the
        # practice — name them (B4: the fixing agent must re-derive nothing)
        hits or [rel for rel, _doc in pr_wfs])

    # 7. Superseded runs cancelled on the PR path.
    hits = []
    for rel, doc in pr_wfs:
        if _conc_cancels_basic(doc.get("concurrency")) or any(
                _conc_cancels_basic(j.get("concurrency")) for j in _wf_jobs(doc).values()):
            hits.append(rel)
    facts["ci.trigger.cancel-superseded"] = fact(
        "pass" if hits else ("fail" if pr_wfs else "not_applicable"),
        (f"concurrency + cancel-in-progress on {len(hits)} of {len(pr_wfs)} PR workflow(s)"
         if pr_wfs else "no PR-triggered workflows"),
        hits or [rel for rel, _doc in pr_wfs])

    # 8. Path filters on the PR path (on-block structure).
    hits = []
    for rel, doc in pr_wfs:
        on = doc.get("on", doc.get(True))
        pr = (on or {}).get("pull_request") if isinstance(on, dict) else None
        prt = (on or {}).get("pull_request_target") if isinstance(on, dict) else None
        if any(isinstance(x, dict) and ("paths" in x or "paths-ignore" in x) for x in (pr, prt)):
            hits.append(rel)
    facts["ci.trigger.path-filter"] = fact(
        "pass" if hits else ("fail" if pr_wfs else "not_applicable"),
        (f"paths filters on {len(hits)} of {len(pr_wfs)} PR workflow(s)"
         if pr_wfs else "no PR-triggered workflows"),
        hits or [rel for rel, _doc in pr_wfs])

    # 9. Job timeouts: a JOB carries timeout-minutes (never a comment).
    # A job that calls a reusable workflow (`uses:` at job level) cannot set
    # timeout-minutes at all; a LOCAL callee's own jobs are graded where they
    # are defined. When every job delegates and some delegate cross-repo, the
    # jobs that could carry a timeout live where this scan cannot see:
    # not_applicable, never a fail (the same handoff as caching/sharding).
    hits = [rel for rel, doc, _raw in parsed
            if any("timeout-minutes" in j for j in _wf_jobs(doc).values())]
    runs_own_steps = any("uses" not in j for _rel, doc, _raw in parsed
                         for j in _wf_jobs(doc).values())
    if not hits and remote_reusables and not runs_own_steps:
        facts["ci.hygiene.job-timeouts"] = fact(
            "not_applicable",
            "every job calls a reusable workflow, so none can set timeout-minutes here; "
            f"CI is delegated to cross-repo reusable workflow(s) "
            f"({', '.join(remote_reusables[:2])}) whose jobs are not visible from this repo",
            [])
    else:
        facts["ci.hygiene.job-timeouts"] = fact(
            "pass" if hits else "fail",
            (f"timeout-minutes set in {len(hits)} workflow(s)" if hits
             else "no job sets timeout-minutes (GitHub's default is 360 minutes)"),
            hits or [rel for rel, _doc, _raw in parsed])

    # 10. Scoped id-token: job-level grants pass; a workflow-level id-token
    # grant - explicit or via write-all - fails.
    wf_level, job_level = [], []
    for rel, doc, _raw in parsed:
        perms = doc.get("permissions")
        if (isinstance(perms, dict) and perms.get("id-token") == "write") or perms == "write-all":
            wf_level.append(rel)
        for j in _wf_jobs(doc).values():
            jp = j.get("permissions")
            if isinstance(jp, dict) and jp.get("id-token") == "write":
                job_level.append(rel)
    if not wf_level and not job_level:
        facts["ci.security.scoped-id-token"] = fact(
            "not_applicable", "no workflow file requests an OIDC id-token", [])
    else:
        facts["ci.security.scoped-id-token"] = fact(
            "fail" if wf_level else "pass",
            (f"workflow-level id-token grant (explicit or write-all) in "
             f"{', '.join(sorted(set(wf_level))[:3])}" if wf_level
             else f"id-token: write scoped at job level in {len(set(job_level))} workflow(s)"),
            wf_level or job_level)

    # 11. Pinned action SHAs: >=95% of remote action references pinned
    # (workflows AND local composite actions; the count is always shown).
    # "Remote" includes first-party actions/* - the pinning practice does not
    # exempt them. The threshold asks "is pinning an adopted, automated
    # practice?": it admits Renovate-style stragglers (<=1 in 20) and rejects
    # partial adoption (OD-CS17). Integer arithmetic - no float boundary.
    pinned = total = 0
    offenders_ranked: list[tuple[int, str]] = []
    for rel, steps in step_sources:
        for step in steps:
            ua = _step_uses(step)
            if not ua:
                continue
            total += 1
            if _SHA40_RE.match(ua[1]):
                pinned += 1
            else:
                # Third-party offenders sort FIRST: pointing the maintainer at
                # actions/labeler while an unpinned third-party action holds
                # release credentials is worse than no file list.
                first_party = ua[0].startswith(("actions/", "github/"))
                offenders_ranked.append((1 if first_party else 0, rel))
    offender_files = [rel for _rank, rel in sorted(set(offenders_ranked))]
    if total == 0:
        facts["ci.security.pinned-action-shas"] = fact(
            "not_applicable", "no remote actions used in workflow or local composite action files", [])
    else:
        facts["ci.security.pinned-action-shas"] = fact(
            "pass" if pinned * 100 >= total * 95 else "fail",
            f"{pinned} of {total} remote action references SHA-pinned "
            f"(workflow + local composite action files; passes at >=95%)",
            offender_files)

    return facts
