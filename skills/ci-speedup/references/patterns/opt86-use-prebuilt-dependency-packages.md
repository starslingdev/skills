---
pattern: OPT86
impact: HIGH
class: data-driven
detector: log-leaf
risk: MEDIUM
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml,Makefile,**/Makefile,pyproject.toml,requirements*.txt,uv.lock,poetry.lock,requirements*.lock,package.json,Cargo.toml'
fix_strategy: cache-vendored-builds-or-use-prebuilt-packages
title_template: 'CI repeatedly compiles dependencies from source'
category: 12
aliases:
  - opt86--ci-compiles-a-dependency-that-ships-prebuilt
alias_note: 'Alias anchor: reports generated before the broader vendored-build detector used this prebuilt-only title.'
---

# OPT86 — CI Repeatedly Compiles Dependencies from Source

**Narrow automatic coverage.** A measured log leaf emits OPT86 when timestamped markers show sequential builds under `contrib/`, `third_party/`, `vendor/`, or `external/` consuming at least half of a step. It requires an `Entering directory` marker and that library's matching `.stamp.*.build` completion; restored libraries without build stamps stay quiet. The other package-manager shapes below remain manual checks.

**TL;DR**: CI repeatedly compiles dependency source. For measured vendored-library builds, cache each completed library with all compatibility inputs in its key. For a package-manager source build, first establish that a compatible prebuilt package exists; only then pin it and make an unexpected source fallback fail loudly.

**Anti-pattern**: The usual causes:

- a vendored dependency under `contrib/`, `third_party/`, `vendor/`, or `external/` is rebuilt from source on every cold job even when its source, recipe, patches, toolchain and target have not changed;
- the locked version has no prebuilt package (wheel) for one interpreter in the matrix, typically the newest Python before the library publishes for it, so the installer falls back to the source archive and compiles it on every cold run;
- the install pulls a far larger build than the job uses, such as a GPU build of a machine-learning framework on a runner with no GPU, when the project publishes a CPU-only package index;
- a dependency that publishes a prebuilt binary is built by the job's own toolchain because a feature flag, an environment variable or a missing platform match asks for source.

**Detection heuristic** (the vendored shape is automatic; package-manager shapes remain manual):

1. The slow step is an install or build step, and its log shows a dependency being compiled: pip or uv `Building wheel for <pkg>` / `Building <pkg>==<version>`, `Running setup.py install`, `node-gyp rebuild` or `gyp info` during a JavaScript install, or a cargo build script compiling a bundled C or C++ library. The compile ends at the matching `Built <pkg>==<version>` line, so the span between the two is the compile time. A `Building <project> @ file://...` line is the project building itself, not a dependency compile. For the oversized-build cause there is no compile: the log instead shows the large build being downloaded and installed on a runner that cannot use it (for example the `nvidia-*` CUDA packages a default PyTorch install pulls on Linux, on a runner with no GPU).
2. A prebuilt package exists for the job's interpreter, OS and architecture at a version the project accepts (check the package index), or the project publishes a prebuilt binary or a smaller build.
3. Where a matrix exists, the leg that compiles (or downloads the oversized build) is the slow outlier.
4. For a vendored source tree, group the timestamped `Entering directory` to `.stamp.*.build` intervals by library. The automatic finding requires completed, non-overlapping intervals to account for at least half of the timestamped step wall; it does not infer a build from directory names alone.

[OPT61](#opt61--missing-dependency-caching) and [OPT63](#opt63--dependency-install-with-cache-disabled) are the cache side of the same step (no cache, or one switched off). OPT86 identifies dependency source work: either a measured vendored build or, after manual confirmation, a package that could use a compatible prebuilt artifact. A warm cache can hide either source build until its key changes.

**Vendored-library fix recipe**:

1. Cache each library separately so one recipe or patch change does not invalidate every other build. Key each entry on that library's source revision, recipe, patches, pinned toolchain, runner OS and architecture, target ABI, build mode, and output-affecting flags.
2. Give the key a stable matching `restore-keys` prefix that cannot cross incompatible platform, ABI, toolchain, or build-mode legs, and save new entries only from the default branch. Before rollout, sum the expected entry sizes and check them against the repository's cache budget with ci-speedup's cache guard helpers.
3. Treat restored files as build inputs, not proof of completeness. Make every produced artifact an explicit prerequisite, then run several warm builds after changing recipes and patches and confirm every artifact exists. Missing prerequisites can otherwise make a partial cache restore look green.

**Prebuilt-package fix recipe**:

1. Constrain the dependency to a version that ships prebuilt packages for every interpreter and platform in the matrix, with per-interpreter markers when one version cannot cover them all.
2. Make a source build an error, so a future bump fails loudly instead of slowing down: `pip install --only-binary=<pkg>` for the package that compiled, or uv's `--no-build-package <pkg>`. Widen it to every dependency (`--only-binary=:all:`, or uv's `UV_NO_BUILD=1` / `--no-build`) only once every dependency, pure-Python ones included, ships a prebuilt package, since the wide form also refuses a pure-Python dependency published only as a source archive. Exempt only the project itself if it must be built.
3. Install the smallest build the job needs: for example PyTorch from its CPU-only index on runners without a GPU, in its own install command (`pip install torch --index-url https://download.pytorch.org/whl/cpu`), because `--index-url` replaces PyPI for everything in the same command and any other dependency would then fail to resolve.
4. Cache the result as well ([OPT61](#opt61--missing-dependency-caching)); prebuilt and cached are complementary.

**Guardrail**: a restored vendored artifact must match the runner, target ABI, toolchain, build mode and flags, and every expected output must exist. A prebuilt package must match what the project ships or tests against. A pinned version must satisfy the project's declared range; a CPU-only build is for jobs that do not test GPU code. A job that exists to test building from source (a packaging job, a source-build smoke test) keeps compiling.

**Sizing: uncredited**. No `_SIZING` model. For the automatic vendored shape, the report measures completed build intervals inside one timestamped workflow-step section. For a manually confirmed package-manager shape, the ceiling is the matching compile span (or the measured oversized download/install span). When the step is slow for another reason, only the measured source-work span counts; the saving is what the next sampled runs show.

**Real-world example (OpenAI)**, their numbers, not a forecast for your repo:

- openai/openai-python [#3573](https://github.com/openai/openai-python/pull/3573) (2026-08-03): "the lockfiles pinned a NumPy version without Python 3.14 wheels, forcing slow source builds", and environment caching was disabled. The PR selected "prebuilt NumPy wheels for each supported interpreter", restored the cache, and stopped the HTTPX 2 job rerunning a suite the main matrix already covered. Together those took "HTTPX 2: **10m42s → 2m12s**" and "Python 3.14: **8m37s → 1m53s**". The PR does not split the saving between its changes, so none of it is a number for prebuilt wheels alone.
- openai/whisper's test workflow installs `torch==<version>+cpu --index-url https://download.pytorch.org/whl/cpu` (no timing published).
- openai/openai-python's CI sets `UV_NO_BUILD: '1'` at workflow level (added in [#3641](https://github.com/openai/openai-python/pull/3641) for supply-chain safety; no timing published), so a dependency without a prebuilt package fails the install instead of compiling.

**Risk**: **MEDIUM**. A package version constraint and install flag are reversible, but a native-artifact cache can serve an incompatible or incomplete build if its key omits a target input or its build graph omits an output. Scope restore prefixes to compatible legs and exercise changed recipes, patches, toolchains and targets before relying on warm builds.
