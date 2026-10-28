"""CI Score v0.1-basic — the practice-facts computation (extracted from
ci-speedup's scanner, 2026-07-16).

`_practice_facts(parsed, root, parse_errors)` walks a repo's PARSED workflow
YAML + local composite actions and returns the eleven pass/fail CONFIGURATION
FACTS the CI Score consumes (each fact: {"state": "pass"|"fail"|"not_applicable",
"evidence": str, "files": [..<=3]}). `ci_score.compute_ci_score` maps these
onto the frozen v0.1.6 registry and does the arithmetic.

STRUCTURE, NOT SUBSTRINGS: every fact walks the parsed workflow (jobs, steps,
with-blocks, on-blocks). The only substring matching is over the CODE inside a
step's `run:`/`script:` value (a real command line) — the change-scoped signal,
and the OD-CS19/20 dependency-install, test-command, and build-command signals.
The install/test/build command signals strip shell comments first, so a
commented-out command never produces a false signal; the change-scoped signal
predates this and matches raw code, but its only effect is a PASS (it never
turns a comment into a false FAIL). A false FAIL on a public page is this
design's worst outcome, and no substring path can produce one from a comment.

VISIBILITY: workflow files plus LOCAL COMPOSITE ACTIONS (everything under
.github/actions/**, and every local action a step's `uses:` names wherever it
lives, followed through nested references), plus the workflow lockfile
(.github/workflows/actions.lock) for the pinning check. Same-repository
reusable workflows a pull-request job calls are graded on the PR path.
Cross-repo reusable workflows' CONTENTS remain invisible (their reference is
counted for pinning); the affected facts land not_applicable-with-evidence
naming what was searched, never a confident claim about unseen files.

This module is a self-contained COPY of the helpers it needs (cross-skill
imports are forbidden — a skill must install standalone). It shares no code
with ci-speedup at runtime.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unicodedata
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
    step_sources = _step_sources(parsed, _composite_action_docs(root, parsed=parsed))
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
    _step_sources(parsed, _composite_action_docs(root, parsed=parsed), stats)
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


def _composite_action_docs(root: Path, errors: list[str] | None = None,
                           parsed: list[tuple[str, dict, str]] | None = None,
                           raw_out: dict[str, str] | None = None,
                           ) -> list[tuple[str, dict]]:
    """Local composite actions' parsed action.yml files - setup (and its
    caching / pinning) frequently lives there rather than in the workflow.
    With `parsed`, every local action a `uses:` names is read too, wherever
    it lives (`_local_composites`). With `errors`, an action file that cannot
    be read or parsed, or is not a mapping, is appended by name (the collector
    records them as `data_sources.composite_parse_errors`, the way it records
    `workflow_parse_errors`). With `raw_out`, each parsed file's raw text is
    kept by name, so a caller that also needs the comments YAML drops reads
    the same bytes instead of opening the file again."""
    return [(rel, doc) for rel, doc, _locked in
            _local_composites(root, parsed or [], errors, raw_out=raw_out)]


# How many distinct local action files one scan reads through `uses:`
# references. A real repository has a handful; past the cap the reference is
# recorded as unread, never silently dropped.
_MAX_LOCAL_COMPOSITES = 256


def _local_uses_key(uses: Any) -> str | None:
    """The `./…` / `$/…` path of a local `uses:` reference, or None (remote
    or docker://). At step level it always names an action directory, even
    one called `setup.yml`; at job level, a reusable workflow file. No suffix
    is filtered here: the level the `uses:` sits at decides which it is."""
    if not isinstance(uses, str) or not _is_local_uses(uses):
        return None
    return uses.split("@")[0].strip()


def _local_call_target(key: str) -> str:
    """The normalized repository path a local job-level `uses:` key names
    (`./.github/workflows/x.yml` -> `.github/workflows/x.yml`), or "" when
    the key names nothing after its prefix."""
    rest = key[2:]
    return os.path.normpath(rest) if rest else ""


def _with_map(node: dict) -> dict:
    """A step's or calling job's `with:` mapping; {} when absent or not a
    mapping."""
    w = node.get("with")
    return w if isinstance(w, dict) else {}


# ---- What a runner checks out: exact path spelling ---------------------------
#
# A runner checks out exactly the paths git stores, byte for byte. A Linux
# runner (the common case) has a case-sensitive disk, so `uses: ./CI/Setup`
# does not exist there when git stores `ci/setup`; Windows and macOS runners
# ignore case and would find it. The scan refuses a wrong-case path rather than
# grade a file that may not run. The local disk is the wrong witness for this:
# a macOS disk ignores case and accent form (a decomposed `é` on disk is the
# composed `é` git stores), and a directory it cannot list says nothing. So
# when the root is a git work tree, git's own file list decides; only a path
# git does not track (or a root that is not a work tree, or no git at all)
# falls back to the directory listing, and a directory that cannot be listed
# there fails CLOSED.
_UNSET: Any = object()


class _Tracked:
    """The paths git tracks under one root: exact spellings, and every
    spelling under its case- and accent-folded key."""

    def __init__(self, paths: list[str]) -> None:
        self.exact = frozenset(paths)
        self._folded: dict[str, list[str]] | None = None

    @property
    def folded(self) -> dict[str, list[str]]:
        if self._folded is None:  # built only when a lookup misses
            self._folded = {}
            for p in sorted(self.exact):
                self._folded.setdefault(_fold(p), []).append(p)
        return self._folded


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _git_tracked(root: Path) -> "_Tracked | None":
    """git's file list for `root` (`git ls-files`, relative to `root`), or None
    when git is missing, fails, or `root` is not a git work tree. GIT_*
    variables are dropped so a calling hook's index or work tree cannot leak
    in. Local only: ls-files reads the index, never the network."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached"],
                             capture_output=True, timeout=60, env=env)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return _Tracked([os.fsdecode(p) for p in out.stdout.split(b"\0") if p])


def _path_state(root: Path, rel: str, tracked: "_Tracked | None") -> tuple[str, str | None]:
    """How the posix path `rel` under `root` stands for a runner:
    ("exact", rel) when that spelling exists; ("case", actual) when only a
    spelling that differs in case (or accent form) exists, `actual` being it;
    ("unlisted", None) when a directory on the way could not be listed (fail
    closed); ("missing", None) when nothing like it exists. git's file list
    decides for a tracked path; anything else is read from the disk."""
    if tracked is not None:
        if rel in tracked.exact:
            return "exact", rel
        variants = tracked.folded.get(_fold(rel))
        if variants:
            return "case", variants[0]
    cur = os.path.normpath(os.path.abspath(root))
    seen: list[str] = []
    case = False
    for part in rel.split("/"):
        try:
            with os.scandir(cur) as entries:
                names = sorted(e.name for e in entries)
        except OSError:
            return "unlisted", None
        if part not in names:
            # outside git, a name that differs only in accent form is the
            # same file on the disk that holds it
            same = [n for n in names if unicodedata.normalize("NFC", n)
                    == unicodedata.normalize("NFC", part)]
            folded = same or [n for n in names if _fold(n) == _fold(part)]
            if not folded:
                return "missing", None
            case = case or not same
            part = folded[0]
        seen.append(part)
        cur = os.path.join(cur, part)
    actual = "/".join(seen)
    return ("case", actual) if case else ("exact", actual)


def _rel_posix(root_n: str, path: Path) -> str:
    """`path` relative to the normalized root, posix-style, in composed
    (NFC) form so one file has one key whatever form the disk returns."""
    rel = Path(os.path.relpath(os.path.normpath(os.path.abspath(path)), root_n)).as_posix()
    return unicodedata.normalize("NFC", rel)


def _case_mismatch(root: Path, path: Path, tracked: Any = _UNSET) -> bool:
    """True when `path` under `root`, as written, is not a spelling a runner
    checks out but one differing from it in case exists, or when a directory
    on the way could not be listed (fail closed). See the block comment above:
    git's file list decides when there is one."""
    if tracked is _UNSET:
        tracked = _git_tracked(root)
    root_n = os.path.normpath(os.path.abspath(root))
    state, _actual = _path_state(Path(root_n), _rel_posix(root_n, path), tracked)
    return state in ("case", "unlisted")


def _outside_root(root: Path, path: Path) -> bool:
    """True when `path` resolves (symbolic links followed) outside `root`."""
    try:
        real_root = os.path.realpath(os.path.normpath(os.path.abspath(root)))
        return os.path.commonpath([real_root, os.path.realpath(path)]) != real_root
    except ValueError:
        return True


def _local_composites(root: Path, parsed: list[tuple[str, dict, str]],
                      errors: list[str] | None = None,
                      info: dict[str, Any] | None = None,
                      raw_out: dict[str, str] | None = None,
                      ) -> list[tuple[str, dict, frozenset[str] | None]]:
    """(rel, doc, self_locked) for every local action the scan reads: each
    `action.yml` under `.github/actions/**`, plus each action a step's `uses:`
    names wherever it lives (`./ci/setup`, `$/tools/x`), followed through the
    local references inside those actions. The `uses:` path is the truth: an
    action outside `.github/actions/` is as much a part of the job as one
    inside it, cache step and remote actions included.

    Fails CLOSED, the same rules as the history read: a reference that
    resolves outside the repository root, names no readable `action.yml` /
    `action.yaml`, sits deeper than `_MAX_LOCAL_ACTION_DEPTH`, or arrives past
    `_MAX_LOCAL_COMPOSITES` is never read and is appended to `errors` by its
    reference, so nothing it holds can earn credit unseen.

    `self_locked` is the set of workflow files that reach the action, and is
    set only when every reference that reaches it is a self-repository `$/`
    reference from a workflow or from another self-locked action (else None):
    `$/` runs at the workflow's own commit, so a workflow lockfile covers the
    actions it uses, while `./` runs whatever is in the workspace and the
    lockfile cannot cover it. Credit then needs an entry under every one of
    those workflows, never one borrowed from a workflow that does not run it.

    The seed walk under `.github/actions/**` applies the same symbolic-link
    rule. A local `uses:` path must be a spelling a runner checks out
    (`_path_state`: git's file list when there is one): a wrong-case path does
    not exist on a Linux runner, so it is never read, and an action the seed
    walk read whose every reference is wrong-case earns nothing either. Action
    files load with the duplicate-key-refusing loader, since plain YAML keeps
    the last of two equal keys and would hide the first; a file that fails to
    load is named with its reason.

    With `info`, also fills: `unread_reasons` (every step-level local
    reference whose action was not read, for any reason -> the first reason,
    None when its file was already named by the walk that failed on it),
    `key_rel` (reference -> the action file it read), and `caller_withs`
    (action file -> the `with:` mapping of every step that reaches it, for
    resolving `inputs.*`). With `raw_out`, each read action's raw text is
    kept by name (the caching check reads version comments YAML drops)."""
    root_n = os.path.normpath(os.path.abspath(root))
    tracked = _git_tracked(root)
    docs: dict[str, dict] = {}
    unreadable: set[str] = set()
    arrivals: dict[str, list[tuple[str | None, bool]]] = {}
    errs: list[str] = []
    unread_reasons: dict[str, str | None] = {}
    key_rel: dict[str, str] = {}
    caller_withs: dict[str, list[dict]] = {}
    case_targets: set[str] = set()
    queue: list[tuple[str, str | None, int, dict]] = []

    def load(path: Path, rel: str) -> None:
        why = "not a mapping"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            doc, why = None, "could not be read"
        else:
            try:
                doc = _load_strict_yaml(text)
            except _DuplicateKeyError as exc:
                doc, why = None, str(exc)
            except (ValueError, yaml.YAMLError):
                doc, why = None, "does not parse as YAML"
        if not isinstance(doc, dict):
            unreadable.add(rel)
            errs.append(f"{rel} ({why})")
            return
        docs[rel] = doc
        if raw_out is not None:
            raw_out[rel] = text
        for step in _composite_steps(doc):
            key = _local_uses_key(step.get("uses"))
            if key is not None:
                queue.append((key, rel, 0, _with_map(step)))

    for pattern in ("action.yml", "action.yaml"):
        for path in sorted((root / ".github" / "actions").rglob(pattern)):
            rel = _rel_posix(root_n, path)
            # a symbolic link out of the checkout (the file itself, or a
            # directory above it, `.github/actions` included) is not this
            # repository's file
            if _outside_root(root_n, path):
                unreadable.add(rel)
                errs.append(f"{rel} (resolves outside the repository)")
                continue
            load(path, rel)
    # depth counts nested hops; the seeds' own references start at depth 1
    queue = [(k, p, 1, w) for k, p, _d, w in queue]
    wf_marks: dict[str, str] = {}  # "wf:<rel>" parent marker -> workflow rel
    for wf_rel, doc, _raw in parsed:
        mark = "\0wf:" + os.path.normpath(wf_rel)
        wf_marks[mark] = os.path.normpath(wf_rel)
        for job in _wf_jobs(doc).values():
            for step in _job_steps(job):
                key = _local_uses_key(step.get("uses"))
                if key is not None:
                    queue.append((key, mark, 0, _with_map(step)))

    def unread(key: str, why: str | None) -> None:
        unread_reasons.setdefault(key, why)
        if why is not None:
            errs.append(f"{key} ({why})")

    while queue:
        key, parent, depth, with_ = queue.pop(0)
        base = _local_ref_path(root, key)
        if base is None:
            unread(key, "resolves outside the repository")
            continue
        found = None
        case_actual = None
        unlisted = False
        for cand in (base / "action.yml", base / "action.yaml"):
            state, actual = _path_state(Path(root_n), _rel_posix(root_n, cand), tracked)
            if state == "exact":
                try:
                    if cand.is_file():
                        found = cand
                        break
                except (OSError, ValueError):
                    continue
            elif state == "case":
                case_actual = case_actual or actual
            elif state == "unlisted":
                unlisted = True
        if found is None:
            if case_actual is not None:
                # a runner checks out exact spellings: on a Linux runner this
                # path does not exist, so it is never read
                case_targets.add(unicodedata.normalize("NFC", case_actual))
                unread(key, "path case does not match the repository")
            elif unlisted:
                unread(key, "directory could not be listed")
            else:
                unread(key, _NO_ACTION_REASON)
            continue
        rel = _rel_posix(root_n, found)
        if rel in unreadable:
            unread(key, None)  # already named once, by the walk that failed on it
            continue
        # a symbolic link out of the checkout is not this repository's file
        if _outside_root(root_n, found):
            unreadable.add(rel)
            unread(key, "resolves outside the repository")
            continue
        if depth > _MAX_LOCAL_ACTION_DEPTH:
            unread(key, f"nested more than {_MAX_LOCAL_ACTION_DEPTH} deep")
            continue
        arrivals.setdefault(rel, []).append((parent, key.startswith("$/")))
        caller_withs.setdefault(rel, []).append(with_)
        if rel in docs:
            key_rel[key] = rel
            continue
        if len(docs) >= _MAX_LOCAL_COMPOSITES:
            unread(key, f"past the {_MAX_LOCAL_COMPOSITES}-action read limit")
            continue
        before = len(queue)
        load(found, rel)
        if rel in unreadable:
            unread(key, None)
            continue
        key_rel[key] = rel
        queue[before:] = [(k, p, depth + 1, w) for k, p, _d, w in queue[before:]]

    # An action the seed walk read whose every reference is in the wrong case
    # never runs on a Linux runner: it earns nothing (its references are named).
    for rel in case_targets:
        if rel in docs and rel not in arrivals:
            del docs[rel]
            if raw_out is not None:
                raw_out.pop(rel, None)

    # self-locked: every arrival is `$/` from a workflow or a self-locked
    # action. Start optimistic for referenced actions and only ever lower a
    # flag, so the loop terminates (cycles included).
    locked = {rel: rel in arrivals for rel in docs}
    changed = True
    while changed:
        changed = False
        for rel, arr in arrivals.items():
            if locked.get(rel) and any(
                    not dollar or (p not in wf_marks and not locked.get(p, False))
                    for p, dollar in arr):
                locked[rel] = False
                changed = True
    # the workflows that reach each action, directly or through other actions:
    # lockfile credit inside it needs an entry under EVERY one of them
    reach: dict[str, set[str]] = {rel: set() for rel in docs}
    changed = True
    while changed:
        changed = False
        for rel, arr in arrivals.items():
            for p, _dollar in arr:
                add = {wf_marks[p]} if p in wf_marks else reach.get(p, set())
                if not add <= reach.setdefault(rel, set()):
                    reach[rel] |= add
                    changed = True
    if errors is not None:
        errors.extend(dict.fromkeys(errs))
    if info is not None:
        info.update(unread_reasons=unread_reasons, key_rel=key_rel,
                    caller_withs=caller_withs)
    return [(rel, doc, frozenset(reach[rel]) if locked[rel] and reach[rel] else None)
            for rel, doc in docs.items()]


def _unreadable_step_sources(parsed: list[tuple[str, dict, str]], root: Path,
                             composites: list[tuple[str, dict]] | None = None,
                             ) -> tuple[list[str], list[str]]:
    """(step lists that are not lists, composite action files that did not
    parse), by name. A job whose `steps:` is a mapping or a string, or a
    composite whose `runs.steps` is, reads as having no steps at all; neither
    may go unrecorded. An absent or empty `steps:` is not listed (a job that
    calls a reusable workflow has none). With `composites` (already read),
    those are checked and no action is read again; the second list is then
    empty, as the caller holds the read's own errors."""
    bad_lists: list[str] = []
    for rel, doc, _raw in parsed:
        for jid, job in _wf_jobs(doc).items():
            steps = job.get("steps")
            if steps is not None and not isinstance(steps, list):
                bad_lists.append(f"{rel}: jobs.{jid}.steps")
    parse_errors: list[str] = []
    if composites is None:
        composites = _composite_action_docs(root, parse_errors, parsed)
    for rel, doc in composites:
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


# The `owner/setup-*` shape `_step_caches_by_default` grades (any case).
_SETUP_SHAPE_RE = re.compile(r"^[\w.-]+/setup-[\w.-]+$", re.IGNORECASE)


def _cache_switch_input(action: str) -> str | None:
    """The input that switches an action's dependency cache, lower-cased (the
    key GitHub matches in any case), or None for an action with no cache
    switch: Gradle's and npm-install's `cache-disabled`, a setup action's
    entry in `_SETUP_CACHE_INPUTS` (setup-ruby `bundler-cache`, setup-uv
    `enable-cache`), else `cache` for any action whose name holds `setup-`.
    Action names match in any case."""
    if _DEFAULT_ON_CACHE_ACTIONS_RE.match(action):
        return "cache-disabled"
    name = action.strip().lower()
    if "setup-" not in name:
        return None
    return _SETUP_CACHE_INPUTS.get(name, ("cache", None))[0]


def _dependency_cache_walk(parsed: list[tuple[str, dict, str]], root: Path,
                           composites: list[tuple[str, dict]],
                           composite_raw: dict[str, str],
                           cache_values: Any = None,
                           ) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """(files that configure dependency caching, setup defaults left
    unresolved, files whose cache input is an expression that resolves to no
    caching value). One walk over every job and local composite action;
    `parsed` is the workflows the caching check grades. `cache_values(rel, v)`
    (`_cache_input_resolver`, OD-CS24) gives every value an expression on a
    cache switch takes; without it every expression is unresolved.

    A LITERAL `cache:` / `enable-cache:` value on a `setup-` action keeps the
    rule it has always had (any value but false or empty is credited; owner
    decision A leaves that to a separate change). An EXPRESSION on the input
    that switches the action's cache (`_cache_switch_input`: setup-ruby
    `bundler-cache`, setup-uv `enable-cache`, Gradle `cache-disabled`, else
    `cache`; action names in any case) decides its step on its own: each value
    it resolves to is put back in place and graded by the same verdict a
    written value gets (`_step_caches_by_default`), and the step is credited
    when any of them caches. It never falls back to the action's default,
    whose shared verdict reads any expression as on: an expression that does
    not resolve earns nothing and is disclosed. An expression on a key the
    action does not read changes nothing (its default decides)."""
    hits: list[str] = []
    unresolved: list[dict[str, str]] = []
    expr_unresolved: list[str] = []
    package_json, pj_problem = _root_package_json(root)
    raw_texts = [raw for _rel, _doc, raw in parsed]
    raw_texts.extend(composite_raw.get(crel, "") for crel, _cdoc in composites)
    comment_texts: dict[tuple[str, str], list[str]] = {}
    version_comments = _sha_version_comments(raw_texts, comment_texts)
    if cache_values is None:
        def cache_values(_rel: str, v: Any) -> list[Any]:
            return [_UNRESOLVED if _is_expression(v) else v]

    def literal_cache(ua: tuple, with_: dict) -> bool:
        # the literal rule, unchanged (owner decision A)
        return "setup-" in ua[0] and any(
            str(k).strip().lower() in ("cache", "enable-cache") and not _is_expression(v)
            and v not in (False, "false", None, "")
            for k, v in with_.items())

    def expression_cache(rel: str, step: dict, ua: tuple, with_: dict,
                         by_default: Any) -> str | None:
        """`on` when an expression on the action's cache switch resolves to a
        value that caches; `off` when it resolves only to values that do not,
        or does not resolve (then `rel` is disclosed); None when the switch
        carries no expression (the default decides)."""
        key = _cache_switch_input(ua[0])
        exprs = [v for k, v in with_.items()
                 if key and str(k).strip().lower() == key and _is_expression(v)]
        if not exprs:
            return None
        rest = {k: v for k, v in with_.items() if str(k).strip().lower() != key}
        unread = False
        for x in (x for v in exprs for x in cache_values(rel, v)):
            if x is _UNRESOLVED:
                unread = True
                continue
            placed = dict(step)
            placed["with"] = {**rest, key: x}
            if by_default(placed) or (
                    not _SETUP_SHAPE_RE.match(ua[0])
                    and not _DEFAULT_ON_CACHE_ACTIONS_RE.match(ua[0])
                    and _setup_cache_state(ua[0], ua[1] or "", {key: x}) == "on"):
                return "on"
        if unread:
            expr_unresolved.append(rel)
        return "off"

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
                continue
            if ua and literal_cache(ua, with_):
                hits.append(rel)
                continue

            def by_default(s: dict, ws: str = ws) -> bool:
                return _step_caches_by_default(
                    s, package_json, ws != "root", version_comments, github_hosted,
                    root=root, package_json_problem=pj_problem, unresolved=unresolved,
                    rel=rel, workspace=ws, runner_unread=runner_unread,
                    comment_texts=comment_texts)

            explicit = expression_cache(rel, step, ua, with_, by_default) if ua else None
            if explicit == "on" or (explicit is None and by_default(step)):
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
    return hits, unique, list(dict.fromkeys(expr_unresolved))


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
    return _cache_walk_inputs(parsed, root)[1]


def _cache_input_resolver(parsed: list[tuple[str, dict, str]],
                          graded: list[tuple[str, dict, str]],
                          comp_docs: dict[str, dict],
                          caller_withs: dict[str, list[dict]]) -> Any:
    """`cache_values(rel, v)` for the caching walk (OD-CS24): a cache input
    set by an expression earns credit only when it resolves.
    `${{ inputs.<name> }}` inside a local action takes every caller's `with:`
    value, else the action's own input default; inside a workflow it takes
    every calling job's `with:` value, else the `workflow_call` input default
    (and the empty value a trigger other than a call sees, plus a manual
    run's `workflow_dispatch` default). An input the workflow declares under
    neither trigger has no value this scan can read. Any other expression -
    `${{ env.X }}`, `${{ matrix.x }}`, `${{ inputs.x || 'npm' }}` - is
    `_UNRESOLVED`: it earns nothing and is disclosed."""
    wf_docs = {os.path.normpath(rel): doc for rel, doc, _raw in graded}
    wf_call_withs = _all_call_withs(parsed)

    def cache_values(rel: str, v: Any) -> list[Any]:
        if rel in comp_docs:
            return _resolve_input(v, caller_withs.get(rel, []), comp_docs[rel].get("inputs"))
        norm = os.path.normpath(rel)
        doc = wf_docs.get(norm)
        if doc is None:
            return [_UNRESOLVED if _is_expression(v) else v]
        name = _input_name(v)
        call_inputs = _trigger_inputs(doc, "workflow_call")
        dispatch_inputs = _trigger_inputs(doc, "workflow_dispatch")
        if name and not any(isinstance(i, dict) and name in i
                            for i in (call_inputs, dispatch_inputs)):
            return [_UNRESOLVED]
        vals = _resolve_input(v, wf_call_withs.get(norm, []), call_inputs,
                              direct=_wf_runs_uncalled(doc))
        # a manual run takes the workflow_dispatch input's own default
        dispatch = _input_default(dispatch_inputs, name) if name else None
        return vals + ([dispatch] if dispatch is not None else [])

    return cache_values


def _cache_walk_inputs(parsed: list[tuple[str, dict, str]], root: Path,
                       read: tuple[list[tuple[str, dict]], dict[str, str],
                                   dict[str, list[dict]]] | None = None,
                       ) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """`_dependency_cache_walk` over exactly what the caching fact grades, for
    the card and the collector alike: every workflow except a called one no
    PR path could read (`_pr_path_workflows`' `unread_files`), and the local
    actions `_local_composites` reads from THOSE workflows (plus its
    `.github/actions/**` seed walk), so an action that only an unread
    workflow runs earns nothing. Returns (hits, unresolved setup defaults,
    files with an unresolved cache expression). `read` is a caller's own
    `_local_composites` read over all of `parsed` (composites, raw texts,
    caller withs): reused when every workflow is graded, so each action file
    is read once."""
    pr_wfs = [(rel, doc) for rel, doc, _raw in parsed if _wf_is_pr_gating(doc)]
    unread_files = _pr_path_workflows(parsed, pr_wfs)[3]
    graded = [p for p in parsed if os.path.normpath(p[0]) not in unread_files]
    if read is not None and len(graded) == len(parsed):
        composites, raw, caller_withs = read
    else:
        raw = {}
        info: dict[str, Any] = {}
        composites = [(rel, doc) for rel, doc, _locked in
                      _local_composites(root, graded, None, info, raw_out=raw)]
        caller_withs = info["caller_withs"]
    resolver = _cache_input_resolver(parsed, graded, dict(composites), caller_withs)
    return _dependency_cache_walk(graded, root, composites, raw, resolver)


def _conc_cancels_basic(conc: Any) -> bool:
    """cancel-in-progress set and not literally false - a templated
    expression is conditional cancellation, which is the practice too."""
    if not isinstance(conc, dict):
        return False
    val = conc.get("cancel-in-progress")
    return val is not None and val is not False


def _pr_path_workflows(parsed: list[tuple[str, dict, str]],
                       pr_wfs: list[tuple[str, dict]],
                       ) -> tuple[list[tuple[str, dict, dict]], list[str],
                                  dict[str, list[dict]], set[str]]:
    """(entries, unread_calls, call_withs, unread_files) for the job-level
    checks on the PR path.

    `entries` is every PR-gating workflow, followed by every same-repository
    reusable workflow (`jobs.x.uses: ./.github/workflows/y.yml` or `$/…`)
    that a PR-reachable job calls, directly or through nested calls, each as
    (rel, doc, event_doc). A callee's own trigger is `workflow_call`, but it
    runs on every pull request its caller runs on, and inside it
    `github.event_name` is the CALLER's event: `event_doc` carries those
    pull-request event names, so a callee job's own `if:` is judged against
    the events that actually reach it. For a PR-gating workflow that no other
    PR workflow calls, `event_doc` is its own doc. A calling job whose `if:`
    can never be true for a pull request reaches nothing.

    `unread_calls` names each local call on the PR path that could not be
    followed: a path outside the repository root, a file that is not a parsed
    workflow (missing or unparseable) or that holds a duplicated key (plain
    YAML keeps the last of two equal keys, hiding the first), or a chain
    deeper than `_MAX_LOCAL_ACTION_DEPTH`. Those are never graded as clean or
    as dirty; the caller discloses them.

    `call_withs` maps each called workflow (normalized path) to the `with:`
    mapping of every PR-reachable job that calls it, so a callee's
    `${{ inputs.x }}` can be resolved against what its callers pass.

    `unread_files` is every parsed workflow (normalized path) that an
    unreadable call names (a duplicated key, or past the depth cap) and that
    no PR path followed: the caching and pinning checks leave it out, so the
    card's "not read" note about that call is true."""
    by_rel = {os.path.normpath(rel): (rel, doc) for rel, doc, _raw in parsed}
    raw_by = {os.path.normpath(rel): raw for rel, _doc, raw in parsed}
    strict_ok: dict[str, bool] = {}
    call_withs: dict[str, list[dict]] = {}
    refused: set[str] = set()
    # A PR workflow that another PR workflow also calls starts from its own
    # events and gains the caller's: `github.event_name` inside the call is
    # the caller's event, so a job scoped to it runs on pull requests too.
    own = {os.path.normpath(rel): set(_wf_pr_event_names(doc)) for rel, doc in pr_wfs}
    events: dict[str, set[str]] = {t: set(e) for t, e in own.items()}
    unread: list[str] = []
    queue = [(doc, set(own[os.path.normpath(rel)]), 0) for rel, doc in pr_wfs]
    while queue:
        doc, evs, depth = queue.pop(0)
        for job in _wf_jobs(doc).values():
            key = _local_uses_key(job.get("uses"))
            if key is None:
                continue
            # only the events the calling job itself can run on reach the callee
            job_evs = _job_pr_events(job, evs)
            if not job_evs:
                continue
            target = _local_call_target(key)
            if (not target or os.path.isabs(target) or target == ".."
                    or target.startswith(".." + os.sep) or target not in by_rel):
                unread.append(key)
                continue
            if depth >= _MAX_LOCAL_ACTION_DEPTH:
                unread.append(key)
                refused.add(target)
                continue
            if target not in strict_ok:
                try:
                    _load_strict_yaml(raw_by[target])
                    strict_ok[target] = True
                except (ValueError, yaml.YAMLError):
                    strict_ok[target] = False
            if not strict_ok[target]:
                unread.append(key)
                refused.add(target)
                continue
            call_withs.setdefault(target, []).append(_with_map(job))
            if job_evs <= events.get(target, set()):
                continue  # no new events reach it
            events.setdefault(target, set()).update(job_evs)
            queue.append((by_rel[target][1], set(events[target]), depth + 1))
    entries = []
    for rel, doc in pr_wfs:
        norm = os.path.normpath(rel)
        entries.append((rel, doc, doc if events[norm] == own[norm]
                        else {"on": sorted(events[norm])}))
    callees = sorted((by_rel[t][0], by_rel[t][1], {"on": sorted(e)})
                     for t, e in events.items() if t not in own)
    return entries + callees, sorted(set(unread)), call_withs, refused - set(events)


def _job_pr_events(job: dict, events: set[str]) -> set[str]:
    """The pull-request events in `events` for which the job's own `if:` can
    be true. Fails closed: no condition, or one that cannot be resolved,
    keeps every event."""
    cond = job.get("if")
    if not isinstance(cond, str) or not cond.strip():
        return set(events)
    try:
        tokens = _tokenize_if(cond)
        return {ev for ev in events if _eval_if_for_event(tokens, ev)}
    except _UnresolvableIf:
        return set(events)


# GitHub workflow lockfile (public preview 2026-10-14; written by the
# `gh actions-lock` CLI extension): `.github/workflows/actions.lock` records,
# per workflow file, the exact commit every action reference runs at, while
# the YAML keeps its tags. Shape (format version v0.0.x):
#   version: 'v0.0.3'
#   workflows: {'.github/workflows/ci.yml': ['actions/checkout@v7.0.1', ...]}
#   dependencies: {'actions/checkout@v7.0.1': {ref, commit: 'sha1-<40 hex>',
#                                              owner_id, repo_id, uses?}}
_LOCKFILE_REL = ".github/workflows/actions.lock"
_LOCK_VERSION_RE = re.compile(r"\Av\d+\.\d+\.\d+\Z")
_LOCK_COMMIT_RE = re.compile(r"\Asha1-[0-9a-f]{40}\Z")
# The format versions GitHub documents: v0.0.2 in the docs' example lockfile,
# v0.0.3 as gh actions-lock writes it. The tool is pre-1.0 and says its format
# may change, so any other version may mean something else: no credit.
_LOCK_VERSIONS = ("v0.0.2", "v0.0.3")


class _StrictLockLoader(yaml.SafeLoader):
    """SafeLoader that refuses a duplicated mapping key. Plain yaml keeps the
    LAST of two equal keys, so a malformed entry followed by a valid one
    would read as valid; the lockfile reader must see the whole file."""


class _DuplicateKeyError(yaml.YAMLError):
    """A mapping repeats a key; the message names it ("duplicated key 'x'")."""


def _no_duplicate_keys(loader: yaml.SafeLoader, node: Any, deep: bool = False) -> Any:
    seen = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            dup = key in seen
            seen.add(key)
        except TypeError:  # an unhashable key: not a lockfile either
            raise yaml.YAMLError("unhashable mapping key")
        if dup:
            raise _DuplicateKeyError(f"duplicated key {key!r}")
    return loader.construct_mapping(node, deep=deep)


_StrictLockLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


def _load_strict_yaml(text: str) -> Any:
    """Parse one document with safe constructors and strict mapping keys.

    Instantiate the SafeLoader subclass directly so the safety boundary is
    explicit to static analyzers. Keep PyYAML's single-document semantics
    and release parser resources even when construction rejects the input.
    """
    loader = _StrictLockLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _lock_key(action: str, ref: str) -> str:
    # engine assumption (GitHub's docs are silent): owner and repo names
    # match case-insensitively, as GitHub resolves them; refs are exact
    return f"{action.strip().lower()}@{ref.strip()}"


def _lock_ref_key(text: Any) -> str | None:
    if not isinstance(text, str) or "@" not in text:
        return None
    action, _, ref = text.partition("@")
    return _lock_key(action, ref)


_LOCK_WORKFLOW_KEY_RE = re.compile(r"\A\.github/workflows/[^/]+\Z")


def _load_lockfile(root: Path) -> tuple[str, dict[str, set[str]]]:
    """(state, per_workflow). `state` is "absent", "valid", "invalid",
    "case:<path>" for a lockfile whose path differs from
    `.github/workflows/actions.lock` only in case (it covers nothing: a runner
    does not see it, and GitHub reads only the exact path; `_path_state`
    decides, from git's file list when there is one), or "version:<v>" for a
    well-formed version GitHub does not document (`_LOCK_VERSIONS`), which
    covers nothing. `per_workflow` maps a workflow file to the references the
    lockfile covers for it: listed under that workflow AND carrying a valid
    dependency entry (a full commit).

    Fails CLOSED: a lockfile that cannot be read, does not parse, carries an
    unknown top-level key, or holds ANY malformed list or entry is "invalid"
    and covers nothing: a partly-read lockfile is never credited, as GitHub
    warns against checking in a corrupt one. Malformed includes: a commit that
    is not `sha1-` plus 40 hex; a missing ref / owner_id / repo_id; an entry
    whose `ref` differs from the ref in its key (a key that is itself a full
    commit, as GitHub writes for nested dependencies, keeps its tag as `ref`
    and must instead record that same commit); a reference listed under a
    workflow, or in an entry's `uses:`, with no entry of its own; and a
    workflow key that is not exactly `.github/workflows/<file>` (a key that
    normalizes to another spelling could stand in for, or collide with, the
    real one)."""
    path = root / _LOCKFILE_REL
    bad: tuple[str, dict[str, set[str]]] = ("invalid", {})
    try:
        spelled, actual = _path_state(root, _LOCKFILE_REL, _git_tracked(root))
        if spelled == "case":
            return f"case:{actual}", {}
        if spelled in ("missing", "unlisted") and not path.is_symlink():
            return "absent", {}
        # a symbolic link out of the checkout is not this repository's lockfile
        if _outside_root(root, path):
            return bad
        doc = _load_strict_yaml(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return bad
    if not isinstance(doc, dict) or set(doc) - {"version", "workflows", "dependencies"}:
        return bad
    wfs, deps = doc.get("workflows"), doc.get("dependencies")
    if not (isinstance(doc.get("version"), str) and _LOCK_VERSION_RE.match(doc["version"])
            and isinstance(wfs, dict) and isinstance(deps, dict)):
        return bad
    if doc["version"] not in _LOCK_VERSIONS:
        return f"version:{doc['version']}", {}
    valid: set[str] = set()
    nested: set[str] = set()
    for key, entry in deps.items():
        k = _lock_ref_key(key)
        if (k is None or not isinstance(entry, dict)
                or not isinstance(entry.get("ref"), str)
                or not (isinstance(entry.get("commit"), str)
                        and _LOCK_COMMIT_RE.match(entry["commit"]))
                or not all(isinstance(entry.get(f), int) and not isinstance(entry.get(f), bool)
                           for f in ("owner_id", "repo_id"))):
            return bad
        key_ref = key.partition("@")[2].strip()
        if _SHA40_RE.match(key_ref):
            if entry["commit"] != f"sha1-{key_ref}":
                return bad
        elif entry["ref"] != key_ref:
            return bad
        uses = entry.get("uses", [])
        if not isinstance(uses, list) or any(_lock_ref_key(u) is None for u in uses):
            return bad
        valid.add(k)
        nested.update(_lock_ref_key(u) for u in uses)
    per_workflow: dict[str, set[str]] = {}
    for wf, refs in wfs.items():
        if not isinstance(wf, str) or not isinstance(refs, list):
            return bad
        if (not _LOCK_WORKFLOW_KEY_RE.match(wf) or os.path.normpath(wf) != wf
                or wf in per_workflow):
            return bad
        keys = [_lock_ref_key(r) for r in refs]
        if any(k is None or k not in valid for k in keys):
            return bad
        per_workflow[wf] = set(keys)
    if not nested <= valid:
        return bad
    return "valid", per_workflow


# ---- Content the scan could not read ----------------------------------------
#
# A check that passes or fails without having read a file is only honest if
# the card says which file. Every list of unread content - workflows that do
# not parse, step lists that are not lists, local actions not read, local
# reusable-workflow calls not followed - feeds ONE note, appended to the
# caching, pinning and shallow-checkout evidence (`_unread_note`).

# `${{ inputs.<name> }}` and nothing else: the one expression shape resolved
# against what a caller passes. Any other expression is left unresolved.
_INPUT_EXPR_RE = re.compile(r"\A\$\{\{\s*inputs\.([A-Za-z_][\w-]*)\s*\}\}\Z")
_UNRESOLVED = object()


def _is_expression(value: Any) -> bool:
    return isinstance(value, str) and "${{" in value


def _input_default(inputs: Any, name: str) -> Any:
    spec = inputs.get(name) if isinstance(inputs, dict) else None
    return spec.get("default") if isinstance(spec, dict) else None


def _input_name(value: Any) -> str | None:
    """`<name>` when `value` is exactly `${{ inputs.<name> }}`, else None."""
    m = _INPUT_EXPR_RE.match(value.strip()) if isinstance(value, str) else None
    return m.group(1) if m else None


def _resolve_input(value: Any, withs: list[dict], inputs: Any,
                   direct: bool = False) -> list[Any]:
    """Every value `${{ inputs.<name> }}` takes across the callers in `withs`
    (each caller's own `with:` value, else the default the `inputs:` spec
    declares), with `_UNRESOLVED` wherever no plain value is known: no caller
    value and no default, or a value that is itself an expression. `direct`
    adds the empty value a workflow triggered directly (not called) sees. A
    plain value is returned as is; any other expression comes back as
    `_UNRESOLVED`."""
    name = _input_name(value)
    if name is None:
        return [_UNRESOLVED if _is_expression(value) else value]
    default = _input_default(inputs, name)
    out: list[Any] = []
    for w in withs or ([] if direct else [{}]):
        v = w.get(name, default) if isinstance(w, dict) else default
        out.append(_UNRESOLVED if v is None or _is_expression(v) else v)
    if direct:
        out.append("")
    return out


def _unproven_history_links(root: Path, ref: str) -> list[str]:
    """For a local action `_local_action_needs_history` flags: [] when some
    link in its chain this check can read PROVES a git-history op, else the
    links it treats as unread (missing, outside the repository by path or by
    a symbolic link, wrong case, or past the depth cap). The copied predicate
    applies neither the symbolic-link nor the case check, so for such a link
    the verdict may rest on a history op this check refuses to read; the
    explanation is then conservative ("exempt only because ... could not be
    read"). Not part of the copied carve-out predicate (see the block comment
    above it): this only explains a verdict, it never changes one."""
    tracked = _git_tracked(root)
    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(ref, 0)]
    unread: list[str] = []
    while queue:
        cur, depth = queue.pop()
        if cur in seen:
            continue
        seen.add(cur)
        base = _local_ref_path(root, cur)
        if depth > _MAX_LOCAL_ACTION_DEPTH or base is None:
            unread.append(cur)
            continue
        text = None
        cands = [base] if base.suffix in (".yml", ".yaml") else [
            base / "action.yml", base / "action.yaml"]
        for cand in cands:
            try:
                if (cand.is_file() and not _outside_root(root, cand)
                        and not _case_mismatch(root, cand, tracked)):
                    text = cand.read_text(encoding="utf-8", errors="replace")
                    break
            except (OSError, ValueError):
                continue
        if text is None:
            unread.append(cur)
            continue
        if _has_git_history_op(text):
            return []
        queue += [(m.split("@")[0].strip(), depth + 1)
                  for m in _NESTED_LOCAL_USES_RE.findall(text)]
    return unread


def _history_exemption_unread(root: Path, job: dict, job_name: str,
                              local_history_actions: set[str]) -> list[str]:
    """For a job `_job_needs_git_history` exempts: [] when the exemption is
    proven (a history op in the job itself, its name, or a readable local
    action), else the unread local actions it rests on."""
    if _job_needs_git_history(job, job_name, None):
        return []
    unread: list[str] = []
    for s in _job_steps(job):
        ref = _step_uses_ref(s).split("@")[0].strip()
        if ref in local_history_actions:
            links = _unproven_history_links(root, ref)
            if not links:
                return []
            unread += links
    return list(dict.fromkeys(unread))


_NO_ACTION_REASON = "no readable action.yml or action.yaml"
_NO_ACTION_FILE = f" ({_NO_ACTION_REASON})"


def _literally_false(cond: Any) -> bool:
    """A step `if:` that can never be true: `false`, `'false'` or
    `${{ false }}`. Anything else may run."""
    if cond is False:
        return True
    if not isinstance(cond, str):
        return False
    text = cond.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    return text == "false"


def _step_code(step: dict) -> list[str]:
    """A step's `run:` and `with.script` code, shell comments stripped."""
    code = (step.get("run"), _with_map(step).get("script"))
    return [_SHELL_COMMENT_RE.sub("", c) for c in code if isinstance(c, str)]


def _wf_runs_uncalled(doc: dict) -> bool:
    """True when the workflow has a trigger besides `workflow_call`, where an
    `inputs.*` value is empty unless that trigger supplies one."""
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return on != "workflow_call"
    if isinstance(on, (list, dict)):
        return any(str(e) != "workflow_call" for e in on)
    return False


def _trigger_inputs(doc: dict, trigger: str) -> Any:
    """The `inputs:` spec of one trigger (`on.workflow_call.inputs`), or None."""
    on = doc.get("on", doc.get(True))
    spec = on.get(trigger) if isinstance(on, dict) else None
    return spec.get("inputs") if isinstance(spec, dict) else None


def _all_call_withs(parsed: list[tuple[str, dict, str]]) -> dict[str, list[dict]]:
    """Called workflow (normalized path) -> the `with:` of every job, in any
    workflow, that calls it locally."""
    out: dict[str, list[dict]] = {}
    for _rel, doc, _raw in parsed:
        for job in _wf_jobs(doc).values():
            key = _local_uses_key(job.get("uses"))
            target = _local_call_target(key) if key is not None else ""
            if target:
                out.setdefault(target, []).append(_with_map(job))
    return out


def _first_three(names: list[str]) -> str:
    """"a, b, c and N more": the first three names, then a count of the rest."""
    more = f" and {len(names) - 3} more" if len(names) > 3 else ""
    return f"{', '.join(names[:3])}{more}"


def _unread_note(entries: list[str]) -> str:
    """"; not read, so nothing inside was graded: a, b, c and N more", or ""."""
    if not entries:
        return ""
    # A path with no action.yml in the checkout is usually one a job checks
    # out at run time (a shared-actions repo); say so instead of reading as a
    # broken repository.
    shown = [e.replace(_NO_ACTION_FILE,
                       " (no action.yml in the checkout; it may be checked out at run time)")
             for e in entries]
    return f"; not read, so nothing inside was graded: {_first_three(shown)}"


def _workflow_parse_errors_on_disk(parsed: list[tuple[str, dict, str]],
                                   root: Path) -> list[str]:
    """Workflow files on disk that are not among `parsed` - the collector's
    `workflow_parse_errors`, derived when a caller does not pass them."""
    have = {os.path.normpath(rel) for rel, _doc, _raw in parsed}
    wf_dir = root / ".github" / "workflows"
    files = sorted(set(wf_dir.glob("*.yml")) | set(wf_dir.glob("*.yaml")))
    return [str(p.relative_to(root)) for p in files
            if os.path.normpath(str(p.relative_to(root))) not in have]


def _practice_facts(parsed: list[tuple[str, dict, str]], root: Path,
                    workflow_parse_errors: list[str] | None = None) -> dict[str, Any]:
    """The eleven CI Score v0.1-basic facts. Shape per fact:
    {"state": "pass"|"fail"|"not_applicable", "evidence": str, "files": [..<=3]}.
    Evidence is NEVER empty: a failed check must hand the maintainer the exact
    thing to look at. `workflow_parse_errors` is the collector's list of
    workflow files that did not parse; when omitted it is derived from the
    files on disk."""
    if workflow_parse_errors is None:
        workflow_parse_errors = _workflow_parse_errors_on_disk(parsed, root)
    pr_wfs = [(rel, doc) for rel, doc, _raw in parsed if _wf_is_pr_gating(doc)]
    remote_reusables = _remote_reusable_refs(parsed)
    unread_actions: list[str] = []
    read_info: dict[str, Any] = {}
    composite_raw: dict[str, str] = {}
    local_composites = _local_composites(root, parsed, unread_actions, read_info,
                                         raw_out=composite_raw)
    composites = [(rel, doc) for rel, doc, _locked in local_composites]
    comp_docs = dict(composites)
    bad_step_lists, _ = _unreadable_step_sources(parsed, root, composites)
    # steps everywhere the repo's own files define them: workflows + local
    # composite actions (the "runs.steps" shape), leaf steps via the walker
    # (children of `parallel:` groups included).
    step_sources = _step_sources(parsed, composites)
    # PR workflows plus the same-repository reusable workflows they call. A
    # called workflow no PR path could read (a duplicated key, past the depth
    # cap) is left out of the caching and pinning reads too, so the "not
    # read" note those cards carry about the call is true.
    pr_path, unread_calls, call_withs, unread_files = _pr_path_workflows(parsed, pr_wfs)
    graded = [p for p in parsed if os.path.normpath(p[0]) not in unread_files]
    graded_sources = (step_sources if len(graded) == len(parsed)
                      else _step_sources(graded, composites))

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
    # A cache input set by an expression earns credit only when it resolves
    # (`_cache_input_resolver`, OD-CS24): any other expression earns nothing
    # and is disclosed. The walk is the collector's own (`_cache_walk_inputs`),
    # so a local action only an unread workflow runs earns nothing on either.
    hits, unresolved, cache_unresolved = _cache_walk_inputs(
        parsed, root, (composites, composite_raw, read_info["caller_withs"]))
    # Owner decision 2026-10-27 (OD-CS24): not_applicable only when a local
    # action that is not in the checkout is the ONLY thing that could explain
    # the missing cache. All of these must hold: no readable step is a setup-*
    # action or carries a cache input (the same test that grants credit); the
    # unread action runs, in a job on the PR path, BEFORE an install, build
    # or test step, and its own `if:` is not literally false; its only reason
    # for being unread is that no action.yml is in the checkout (an action
    # fetched at run time) - a wrong-case path, a path outside the repository
    # or a file that would not load is not; and no install anywhere runs with
    # no such action before it in its job. Readable local actions are followed
    # in step order, so an unread action they run counts in place.
    unread_reasons = read_info["unread_reasons"]
    key_rel = read_info["key_rel"]
    pr_jobs = {(os.path.normpath(rel), jid) for rel, doc, event_doc in pr_path
               for jid, job in _wf_jobs(doc).items()
               if not _job_if_excludes_pull_requests(job, event_doc)}

    def _scan(steps: list[dict], on_pr: bool, pending: list[str],
              seen: frozenset = frozenset()) -> tuple[bool, list[str]]:
        """(an install with no qualifying unread action before it, the
        qualifying unread actions that run before an install/build/test)."""
        plain, used = False, []
        for s in steps:
            if _literally_false(s.get("if")):
                continue
            k = _local_uses_key(s.get("uses"))
            if k is not None:
                if k in unread_reasons:
                    if on_pr and unread_reasons[k] == _NO_ACTION_REASON:
                        pending.append(k)
                elif key_rel.get(k) in comp_docs and key_rel[k] not in seen:
                    sub_p, sub_u = _scan(_composite_steps(comp_docs[key_rel[k]]), on_pr,
                                         pending, seen | {key_rel[k]})
                    plain = plain or sub_p
                    used += sub_u
                continue
            code = _step_code(s)
            installs = any(_INSTALL_CMD_RE.search(c) for c in code)
            if installs or any(_BUILD_CMD_RE.search(c) or _TEST_CMD_RE.search(c) for c in code):
                if pending:
                    used += pending
                elif installs:
                    plain = True
        return plain, used

    cache_unread: list[str] = []
    plain_uncached_install = False
    for rel, doc, _raw in graded:
        for jid, job in _wf_jobs(doc).items():
            plain, used = _scan(_job_steps(job), (os.path.normpath(rel), jid) in pr_jobs, [])
            cache_unread += used
            plain_uncached_install = plain_uncached_install or plain
    cache_unread = list(dict.fromkeys(cache_unread))
    # "the same test that grants credit": a setup-* step, a default-on cache
    # action (OD-CS23), or a cache input under any key case
    readable_setup = any(
        (ua := _step_uses(s)) and (
            "setup-" in ua[0].lower() or _DEFAULT_ON_CACHE_ACTIONS_RE.match(ua[0])
            or any(str(k).strip().lower() in ("cache", "enable-cache")
                   for k in _with_map(s)))
        for _rel, steps in graded_sources for s in steps)
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
    elif not (_has_dep_manifest(root) or _has_install_signal(graded_sources)):
        # No manifest AND no inline install — nothing is installed, so there is
        # nothing to cache: not_applicable, never a fail (OD-CS19). A manifest
        # OR an inline install makes the check applicable (and here, a fail).
        facts["ci.cache.dependency-cache"] = fact(
            "not_applicable",
            "no dependency manifest and no dependency-install step detected; nothing to cache",
            [])
    elif cache_unread and not readable_setup and not plain_uncached_install:
        # the action itself is named once, in the "not read" note below
        facts["ci.cache.dependency-cache"] = fact(
            "not_applicable",
            "no readable workflow or local action configures a cache or has a setup-* "
            "step that could; a local action that is not in the checkout runs before an "
            "install, build or test step and may hold the cache",
            [])
    else:
        # A withheld default is never a silent fail: name it
        # (data_sources.unresolved_setup_defaults records it too).
        evidence = ("no cache action and no setup-* cache input in any workflow or "
                    "local composite action") + _unresolved_evidence(unresolved)
        facts["ci.cache.dependency-cache"] = fact(
            "fail", evidence, [u["file"] for u in unresolved])
    if cache_unresolved and not hits:
        facts["ci.cache.dependency-cache"]["evidence"] += (
            "; cache input set by an expression that could not be resolved, so not "
            f"credited: {_first_three(cache_unresolved)}")

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
    carved_unread: list[str] = []  # unread actions an exemption rests on
    carved_unread_jobs = 0
    depth_unresolved: list[str] = []  # jobs whose depth is an unresolved expression
    # `pr_path`: PR workflows plus the same-repository reusable workflows they
    # call (computed above, with `unread_calls` and `call_withs`)
    triggered_norm = {os.path.normpath(rel) for rel, _doc in pr_wfs}
    for rel, doc, event_doc in pr_path:
        norm = os.path.normpath(rel)
        call_inputs = _trigger_inputs(doc, "workflow_call")
        for job_name, job in _wf_jobs(doc).items():
            # `fetch-depth: ${{ inputs.x }}` in a called workflow takes each
            # caller's value (else the input default); any other expression,
            # or an input nothing resolves, leaves the job ungraded, disclosed
            depth_strs: list[str | None] = []
            # steps that take, or may take, full history, with their depths
            deep_steps: list[tuple[dict, list[str | None]]] = []
            for step in _job_steps(job):
                with_ = _with_map(step)
                if "fetch-depth" in with_:
                    step_depths = [
                        None if d is _UNRESOLVED else str(d).strip("'\"")
                        for d in _resolve_input(with_["fetch-depth"], call_withs.get(norm, []),
                                                call_inputs, direct=norm in triggered_norm)]
                    depth_strs += step_depths
                    if "0" in step_depths or None in step_depths:
                        deep_steps.append((step, step_depths))
            takes_full_history = "0" in depth_strs
            if not takes_full_history and None not in depth_strs:
                continue
            # A job whose own `if:` can never be true for a pull-request event
            # is not on the PR path at all — no pull request waits for it, so
            # its checkout depth is not a PR-speed fact.
            if _job_if_excludes_pull_requests(job, event_doc):
                if takes_full_history:
                    off_pr_path += 1
                continue
            # The same PR-reachability read as the job-level `if:` above, on
            # the checkout STEP's own `if:`: a step that can never run for a
            # pull-request event takes no history on the PR path. Fails
            # closed - an unresolvable step `if:` still counts. Only the steps
            # that can run on a pull request decide the job.
            live = [d for step, ds in deep_steps
                    if not _job_if_excludes_pull_requests(step, event_doc) for d in ds]
            if not live:
                if takes_full_history:
                    off_pr_steps += 1
                continue
            takes_full_history = "0" in live
            if _job_needs_git_history(job, job_name, local_history_actions):
                if takes_full_history:
                    carved_out += 1
                    unread = _history_exemption_unread(root, job, job_name,
                                                       local_history_actions)
                    if unread:
                        carved_unread_jobs += 1
                        carved_unread += unread
                continue
            if not takes_full_history:
                depth_unresolved.append(rel)
                continue
            offenders.append(rel)
    uniq_offenders = sorted(set(offenders))
    if offenders:
        # a called reusable workflow has no pull_request trigger of its own:
        # name it as what it is, so nobody hunts it for one
        triggered = {rel for rel, _doc in pr_wfs}
        n_called = sum(1 for rel in uniq_offenders if rel not in triggered)
        n_gating = len(uniq_offenders) - n_called
        kinds = [f"{n_gating} PR-gating workflow(s)"] if n_gating else []
        if n_called:
            kinds.append(f"{n_called} reusable workflow(s) a PR-gating workflow calls")
        evidence = (f"fetch-depth: 0 on {' and '.join(kinds)}, "
                    f"e.g. {', '.join(uniq_offenders[:3])}")
    elif not pr_wfs:
        evidence = "no PR-gating workflows"
    elif carved_out or off_pr_path or off_pr_steps:
        # A pass that an exemption produced must SAY so. `fetch-depth: 0` is
        # right there in a PR-triggered workflow, and evidence claiming
        # otherwise sends the maintainer to a file that contradicts the card.
        why = []
        proven = carved_out - carved_unread_jobs
        if proven:
            why.append(f"{proven} walk(s) git history, so shallowing the "
                       f"checkout would break the job")
        if carved_unread_jobs:
            # fail-closed, but not proof: the action may or may not walk history
            names = list(dict.fromkeys(carved_unread))
            why.append(f"{carved_unread_jobs} exempt only because "
                       f"{', '.join(names[:3])}{' and more' if len(names) > 3 else ''} "
                       f"could not be read")
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
    # A call to a workflow that did not parse is named once, as that workflow,
    # by the shared note below; every other call that could not be followed
    # is named here, never graded either way, so a pass never reads as
    # covering it.
    parse_norm = {os.path.normpath(rel) for rel in workflow_parse_errors}
    shallow_calls = [key for key in unread_calls
                     if os.path.normpath(key[2:]) not in parse_norm]
    if shallow_calls:
        evidence += (f"; {len(shallow_calls)} reusable workflow call(s) on the PR path "
                     f"could not be read and were not graded: {', '.join(shallow_calls[:3])}")
    if depth_unresolved:
        rels = sorted(set(depth_unresolved))
        evidence += (f"; {len(depth_unresolved)} job(s) on the PR path set fetch-depth by "
                     f"an expression that could not be resolved and were not graded: "
                     f"{', '.join(rels[:3])}")
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
    # Every remote reference counts: step-level actions AND job-level calls to
    # another repository's reusable workflow (`jobs.x.uses: org/r/...yml@ref`).
    # A reference also counts as pinned when the workflow lockfile covers it:
    # a valid entry listed under its own workflow file, or, inside a local
    # action reached only through `$/`, a valid entry under every workflow
    # that reaches it. A call to another repository's reusable workflow is
    # pinned ONLY by a full SHA: the `gh actions-lock` README states that such
    # calls are not locked (github/gh-actions-lock#179, open), even though
    # GitHub's docs page says reusable workflows are pinned. Until the tool
    # locks them, a lockfile entry for one enforces nothing (OD-CS24).
    workflow_rels = {os.path.normpath(rel) for rel, _doc, _raw in parsed}
    self_locked = {rel: wfs for rel, _doc, wfs in local_composites if wfs}
    lock_state, lock_per_wf = _load_lockfile(root)
    remote_refs = [(rel, ua, True) for rel, steps in graded_sources for step in steps
                   if (ua := _step_uses(step))]
    reusable_calls = 0
    for rel, doc, _raw in graded:
        for job in _wf_jobs(doc).values():
            uses = job.get("uses")
            if isinstance(uses, str) and not _is_local_uses(uses) and "@" in uses:
                action, _, ref = uses.partition("@")
                remote_refs.append((rel, (action, ref), False))
                reusable_calls += 1
    pinned = total = via_lock = 0
    offenders_ranked: list[tuple[int, str]] = []
    for rel, ua, lockable in remote_refs:
        total += 1
        norm = os.path.normpath(rel)
        key = _lock_key(ua[0], ua[1])
        if _SHA40_RE.match(ua[1]):
            pinned += 1
        elif lockable and (key in lock_per_wf.get(norm, ()) if norm in workflow_rels
                           else (rel in self_locked and all(
                               key in lock_per_wf.get(wf, ()) for wf in self_locked[rel]))):
            pinned += 1
            via_lock += 1
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
        # The base sentence gains a clause only for what applies: the
        # reusable-workflow calls counted, the lockfile credit, or why a
        # lockfile grants none.
        scope = "workflow + local composite action files"
        if reusable_calls:
            scope += f", including {reusable_calls} reusable-workflow call(s)"
        head = (f"{pinned} of {total} remote action references pinned, {via_lock} via "
                f"lockfile ({_LOCKFILE_REL})" if lock_state == "valid" else
                f"{pinned} of {total} remote action references SHA-pinned")
        evidence = f"{head} ({scope}; passes at >=95%)"
        if lock_state == "invalid":
            evidence += (f"; {_LOCKFILE_REL} is not a readable, well-formed lockfile, "
                         f"so it grants no credit")
        elif lock_state.startswith("version:"):
            evidence += (f"; {_LOCKFILE_REL} format version {lock_state[len('version:'):]} "
                         f"is not one GitHub documents ({', '.join(_LOCK_VERSIONS)}), "
                         f"so it grants no credit")
        elif lock_state.startswith("case:"):
            evidence += (f"; {lock_state[len('case:'):]}: path case does not match the "
                         f"repository (a runner reads {_LOCKFILE_REL}), so it grants no credit")
        facts["ci.security.pinned-action-shas"] = fact(
            "pass" if pinned * 100 >= total * 95 else "fail", evidence, offender_files)

    # Content the scan could not read may hold the cache step, unpinned
    # actions or a full-history checkout: never graded either way, and named
    # on all three cards so no state reads as covering it (data_sources
    # carries the same lists). The shallow-checkout card names the
    # reusable-workflow calls it could not follow in its own sentence, so
    # they are not repeated there. On every card a call to a workflow that did
    # not parse is named once, as that workflow. The caching and pinning cards
    # name a call only when the file it names was read by no PR path: those
    # checks leave such a file out (`unread_files`), so the note is true.
    parse_errs = [f"{rel} (does not parse as a workflow)" for rel in workflow_parse_errors]
    followed = {os.path.normpath(rel) for rel, _doc, _event in pr_path}
    calls = [f"{key} (reusable workflow call not followed)" for key in shallow_calls
             if os.path.normpath(key[2:]) not in followed]
    lists = [f"{entry} (not a list of steps)" for entry in bad_step_lists]
    base = unread_actions + parse_errs + lists
    for check, entries in (("ci.cache.dependency-cache", base + calls),
                           ("ci.security.pinned-action-shas", base + calls),
                           ("ci.checkout.shallow-clone", base)):
        facts[check]["evidence"] += _unread_note(entries)

    return facts
