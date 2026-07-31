"""CI Score v0.1-basic — the practice-facts computation (extracted from
ci-speedup's scanner, 2026-07-16).

`_practice_facts(parsed, root)` walks a repo's PARSED workflow YAML + local
composite actions and returns the eleven pass/fail CONFIGURATION FACTS the CI
Score consumes (each fact: {"state": "pass"|"fail"|"not_applicable",
"evidence": str, "files": [..<=3]}). `ci_score.compute_ci_score` maps these
onto the frozen v0.1.3 registry and does the arithmetic.

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
    step_sources: list[tuple[str, list[dict]]] = []
    for rel, doc, _raw in parsed:
        for job in _wf_jobs(doc).values():
            step_sources.append((rel, _job_steps(job)))
    for rel, doc in _composite_action_docs(root):
        runs = doc.get("runs")
        if isinstance(runs, dict):
            step_sources.append((rel, [s for s in (runs.get("steps") or [])
                                       if isinstance(s, dict)]))
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
            if isinstance(uses, str) and not uses.startswith("./") and "@" in uses:
                refs.append(uses.split("@")[0])
    return sorted(set(refs))


def _wf_jobs(doc: dict) -> dict[str, dict]:
    jobs = doc.get("jobs")
    return {str(k): v for k, v in jobs.items() if isinstance(v, dict)} if isinstance(jobs, dict) else {}


def _job_steps(job: dict) -> list[dict]:
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def _step_uses(step: dict) -> tuple[str, str] | None:
    """(action, ref) for a remote `uses:`; None for local (./) / docker:// /
    run steps."""
    uses = step.get("uses")
    if not isinstance(uses, str) or uses.startswith("./") or uses.startswith("docker://"):
        return None
    action, _, ref = uses.partition("@")
    return (action, ref) if action else None


def _composite_action_docs(root: Path) -> list[tuple[str, dict]]:
    """Local composite actions' parsed action.yml files - setup (and its
    caching / pinning) frequently lives there rather than in the workflow."""
    out: list[tuple[str, dict]] = []
    for pattern in ("action.yml", "action.yaml"):
        for path in sorted((root / ".github" / "actions").rglob(pattern)):
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, yaml.YAMLError):
                continue
            if isinstance(doc, dict):
                out.append((str(path.relative_to(root)), doc))
    return out


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
    composites = _composite_action_docs(root)
    # steps everywhere the repo's own files define them: workflows + local
    # composite actions (the "runs.steps" shape).
    step_sources: list[tuple[str, list[dict]]] = []
    for rel, doc, _raw in parsed:
        for job in _wf_jobs(doc).values():
            step_sources.append((rel, _job_steps(job)))
    for rel, doc in composites:
        runs = doc.get("runs")
        if isinstance(runs, dict):
            step_sources.append((rel, [s for s in (runs.get("steps") or [])
                                       if isinstance(s, dict)]))

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
    hits = []
    for rel, steps in step_sources:
        for step in steps:
            ua = _step_uses(step)
            with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
            if ua and _CACHE_ACTION_RE.search(ua[0]):
                hits.append(rel)
            elif ua and "setup-" in ua[0] and any(
                    k in ("cache", "enable-cache") and v not in (False, "false", None, "")
                    for k, v in with_.items()):
                hits.append(rel)
    if hits:
        facts["ci.cache.dependency-cache"] = fact(
            "pass",
            f"dependency caching configured in {len(set(hits))} workflow/action file(s)",
            hits)
    elif remote_reusables:
        facts["ci.cache.dependency-cache"] = fact(
            "not_applicable",
            "CI is delegated to cross-repo reusable workflow(s) "
            f"({', '.join(remote_reusables[:2])}); caching there is not visible from this repo",
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
        facts["ci.cache.dependency-cache"] = fact(
            "fail",
            "no cache action and no setup-* cache input in any workflow or local composite action",
            [])

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
    offenders = []
    for rel, doc in pr_wfs:
        for job in _wf_jobs(doc).values():
            for step in _job_steps(job):
                with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
                if str(with_.get("fetch-depth")).strip("'\"") == "0":
                    offenders.append(rel)
    uniq_offenders = sorted(set(offenders))
    facts["ci.checkout.shallow-clone"] = fact(
        "fail" if offenders else ("pass" if pr_wfs else "not_applicable"),
        (f"fetch-depth: 0 on {len(uniq_offenders)} PR-gating workflow(s), "
         f"e.g. {', '.join(uniq_offenders[:3])}"
         if offenders else
         ("no PR-gating workflow checks out full history (post-merge automation "
          "like backport/changelog jobs is exempt - it needs history and gates nobody)"
          if pr_wfs else "no PR-gating workflows")),
        offenders)

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
    hits = [rel for rel, doc, _raw in parsed
            if any("timeout-minutes" in j for j in _wf_jobs(doc).values())]
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
