---
pattern: OPT86
impact: HIGH
class: data-driven
detector: manual
risk: LOW
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml,pyproject.toml,requirements*.txt,uv.lock,poetry.lock,requirements*.lock,package.json,Cargo.toml'
fix_strategy: use-prebuilt-dependency-packages
title_template: 'CI compiles a dependency that ships prebuilt'
category: 12
---

# OPT86 — CI Compiles a Dependency That Ships Prebuilt

**Not auto-detected yet.** Like OPT88, no pass emits OPT86, so the audit never reports it on its own and never claims to have checked it. No audit phase applies it; a reader applies it by hand, reading a slow install step's log against the heuristic below.

**TL;DR**: An install step compiles a dependency from source (a C or C++ extension, a native add-on, a large machine-learning framework) when a prebuilt package exists for that interpreter and platform. Pin a version that ships prebuilt packages for every leg of the matrix, and make the install refuse to build from source, so the next lockfile bump cannot bring the compile back unnoticed.

**Anti-pattern**: The usual causes:

- the locked version has no prebuilt package (wheel) for one interpreter in the matrix, typically the newest Python before the library publishes for it, so the installer falls back to the source archive and compiles it on every cold run;
- the install pulls a far larger build than the job uses, such as a GPU build of a machine-learning framework on a runner with no GPU, when the project publishes a CPU-only package index;
- a dependency that publishes a prebuilt binary is built by the job's own toolchain because a feature flag, an environment variable or a missing platform match asks for source.

**Detection heuristic** (manual; read from the step log, never inferred):

1. The slow step is an install or build step, and its log shows a dependency being compiled: pip or uv `Building wheel for <pkg>` / `Building <pkg>==<version>`, `Running setup.py install`, `node-gyp rebuild` or `gyp info` during a JavaScript install, or a cargo build script compiling a bundled C or C++ library. The compile ends at the matching `Built <pkg>==<version>` line, so the span between the two is the compile time. A `Building <project> @ file://...` line is the project building itself, not a dependency compile. For the oversized-build cause there is no compile: the log instead shows the large build being downloaded and installed on a runner that cannot use it (for example the `nvidia-*` CUDA packages a default PyTorch install pulls on Linux, on a runner with no GPU).
2. A prebuilt package exists for the job's interpreter, OS and architecture at a version the project accepts (check the package index), or the project publishes a prebuilt binary or a smaller build.
3. Where a matrix exists, the leg that compiles (or downloads the oversized build) is the slow outlier.

[OPT61](#opt61--missing-dependency-caching) and [OPT63](#opt63--dependency-install-with-cache-disabled) are the cache side of the same step (no cache, or one switched off). OPT86 is about what gets installed; both can apply at once, and a warm cache can hide a source build until its key changes.

**Fix recipe**:

1. Constrain the dependency to a version that ships prebuilt packages for every interpreter and platform in the matrix, with per-interpreter markers when one version cannot cover them all.
2. Make a source build an error, so a future bump fails loudly instead of slowing down: `pip install --only-binary=<pkg>` for the package that compiled, or uv's `--no-build-package <pkg>`. Widen it to every dependency (`--only-binary=:all:`, or uv's `UV_NO_BUILD=1` / `--no-build`) only once every dependency, pure-Python ones included, ships a prebuilt package, since the wide form also refuses a pure-Python dependency published only as a source archive. Exempt only the project itself if it must be built.
3. Install the smallest build the job needs: for example PyTorch from its CPU-only index on runners without a GPU, in its own install command (`pip install torch --index-url https://download.pytorch.org/whl/cpu`), because `--index-url` replaces PyPI for everything in the same command and any other dependency would then fail to resolve.
4. Cache the result as well ([OPT61](#opt61--missing-dependency-caching)); prebuilt and cached are complementary.

**Guardrail**: the prebuilt package must match what the project ships or tests against. A pinned version must satisfy the project's declared range; a CPU-only build is for jobs that do not test GPU code. A job that exists to test building from source (a packaging job, a source-build smoke test) keeps compiling.

**Sizing: uncredited**. No `_SIZING` model. The ceiling is the measured span of the compile lines in the install step's log (for an oversized build, the measured download and install span); when a compiling leg is also slow for another reason, only the compile span counts toward OPT86's ceiling; the saving is what the next sampled runs show.

**Real-world example (OpenAI)**, their numbers, not a forecast for your repo:

- openai/openai-python [#3573](https://github.com/openai/openai-python/pull/3573) (2026-08-03): "the lockfiles pinned a NumPy version without Python 3.14 wheels, forcing slow source builds", and environment caching was disabled. The PR selected "prebuilt NumPy wheels for each supported interpreter", restored the cache, and stopped the HTTPX 2 job rerunning a suite the main matrix already covered. Together those took "HTTPX 2: **10m42s → 2m12s**" and "Python 3.14: **8m37s → 1m53s**". The PR does not split the saving between its changes, so none of it is a number for prebuilt wheels alone.
- openai/whisper's test workflow installs `torch==<version>+cpu --index-url https://download.pytorch.org/whl/cpu` (no timing published).
- openai/openai-python's CI sets `UV_NO_BUILD: '1'` at workflow level (added in [#3641](https://github.com/openai/openai-python/pull/3641) for supply-chain safety; no timing published), so a dependency without a prebuilt package fails the install instead of compiling.

**Risk**: **LOW**. A version constraint and an install flag, both reversible; the hard error surfaces any gap at install time.
