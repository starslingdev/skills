---
pattern: OPT91
impact: HIGH
class: data-driven
detector: manual
risk: MEDIUM
affected_files: 'Cargo.toml,.cargo/config.toml,tsconfig*.json,CMakeLists.txt,.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: profile-the-compile
title_template: 'The compile is the long pole and nobody profiled it'
category: 12
---

# OPT91 — The Compile Is the Long Pole and Nobody Profiled It

The compiler-profile flavor remains manual: no pass infers a compiler hotspot
without timing evidence. A narrow Xcode CI flavor is auto-detected from workflow
YAML when a compiling `xcodebuild` invocation leaves its index store on, or a
simulator build/test uses a generic destination without one host architecture.

**TL;DR**: The slowest job's time goes to compiling, the cache is already doing its job, and nobody has asked the compiler where the time goes. Profile the compile with the compiler's own timing reports, then fix the hotspot it names: a build-profile setting that costs far more build time than it returns at runtime, or one piece of code that is expensive to compile.

**Anti-pattern**: A compile-heavy job (a release build, a crate or package rebuilt on every run) is the long pole, and the response so far has been a bigger runner or more cache. Neither touches the cause when one setting or one hotspot dominates: a link-time optimization mode that serializes the final link, or macro- or generic-heavy code that multiplies the compiler's work.

**Relation to other patterns**: rule out the cache first. [OPT61](#opt61--missing-dependency-caching) and [OPT62](#opt62--build-artifacts-destroyed-before-every-run) cover work that should not be repeated at all; OPT91 is the compile that genuinely has to run, made cheaper. [OPT81](#opt81--the-same-job-is-measurably-faster-on-another-runner) is the bigger-runner option, which this pattern tries first not to need.

**Detection heuristic** (manual; read, never inferred):

1. The long pole's dominant step is a compile or build step.
2. The cache is not the explanation: dependencies are restored, or the job builds clean by design (a release build), and the slow part is compiling the project's own code or the final optimized link.
3. Nobody has read the compiler's timing report for this job (a report CI already produces but nobody examines counts as unread; check the workflow for it and whether its artifact survives), or the report names one dominant item: for Rust, `cargo build --timings` (per crate) and `-Z time-passes` (per compiler pass, nightly); for TypeScript, `tsc --extendedDiagnostics` or `--generateTrace`; for C and C++, Clang's `-ftime-trace`.

**Static Xcode CI check**:

1. Inspect workflow `run:` steps that invoke `xcodebuild` for `build`, `test`,
   `build-for-testing`, `archive` or `analyze`. A version probe and
   `test-without-building` do not compile and stay quiet.
2. Flag a compiling invocation unless the command or effective workflow/job/step
   environment sets `COMPILER_INDEX_STORE_ENABLE=NO`.
3. On simulator build/test work, also flag a `generic/platform=… Simulator`
   destination unless `ARCHS` selects one architecture (`arm64`, `x86_64`, the
   host's `uname -m`, or a runner-architecture expression). A multiple-architecture
   value does not clear the finding. Keep a generic device destination for an
   archive/release build; it is not this simulator-test waste.
4. Fail closed on an unreadable parallel step group. An invocation hidden in a
   helper or source program is not classified from workflow YAML.

**Fix recipe**:

1. Profile the slow target on the same runner class with the same cache state as the slow job (dependencies restored, or a clean build when the job builds clean by design), and keep the report. A clean build profiled separately is a comparison, not the measurement.
2. If a build-profile setting dominates (for example fat link-time optimization in a release profile), measure the alternatives (ThinLTO, more codegen units) on build time AND on what the setting is for: runtime of the real workloads, binary size.
3. If one crate, module or construct dominates, change that code (for example a macro that generates heavy wrappers for every implementation, or deeply nested generics), keeping its public shape where possible.
4. Re-profile, and record the before and after with the commands used, so a later regression can be measured the same way.

For the static Xcode flavor, start with the two independent settings: add
`COMPILER_INDEX_STORE_ENABLE=NO` to CI build/test invocations whose index is not
consumed, then restrict `ARCHS` to the runner host for generic simulator builds.
Preserve the project, scheme, configuration, signing flags, test selection and
outputs. Do not replace a generic archive/release destination. Measure each
setting against the same runner class and cache state so either can be rolled
back independently if output parity changes.

**Guardrail**: a release-profile setting changes the product, not just the build. Measure the runtime and size of what ships before changing it, and keep the shipped artifact on the settings that were measured. A profile setting used only to speed up CI must not silently become the release setting.

**Sizing: uncredited**. No `_SIZING` model. The ceiling is the measured compile share of the step; the saving is what the next profile and the next sampled runs show. For the static Xcode flavor, the runner-minute effect is a **saving**, but it remains unsized until matched before/after runs isolate these settings and the workflow's run volume is known. The public result below is evidence that the lever can matter, not a forecast for another repository.

**Public Xcode evidence**: [OpenClaw pull request #187136](https://github.com/openclaw/openclaw/pull/187136)
disabled the compiler index store and selected the host architecture for a
Debug simulator build while preserving its project, scheme, test selection and
signing behavior. Its reported median improvement is not reused as another
repository's saving.

**Real-world example (OpenAI)**, their numbers, not a forecast for your repo:

- openai/codex [#23710](https://github.com/openai/codex/pull/23710) (2026-06-04): "switching from fat LTO to ThinLTO (`3/thin/1`) reduced a clean `codex-cli` release build from 2073.893 seconds to 1243.172 seconds, a 40.06% improvement". In codex's release CI, only the stable Windows builds still used fat LTO at the time; the rest already used ThinLTO. The trade was measured too: "The resulting binary increased from 196.7 MiB to 211.8 MiB (+7.63%)", and "the worst image workload median was +0.86%". The PR does not say where the build was timed.
- openai/codex [#16630](https://github.com/openai/codex/pull/16630) (2026-04-02): the hotspot was "a large compile-time tax from `#[async_trait]` on every concrete handler impl". Replacing it with native `impl Future + Send` futures took "a clean `codex-core` package rebuild" from "**187.15s to 68.98s**" in rustc's `total`, profiled with `cargo +nightly build -p codex-core --lib -Z unstable-options --timings=json` and `-Z time-passes`; the PR links no CI run for these numbers.

**Risk**: **MEDIUM**. A profile setting trades build time against runtime and size; the guardrail requires measuring both.
