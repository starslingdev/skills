---
pattern: OPT89
impact: HIGH
class: data-driven
detector: manual
risk: MEDIUM
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml,.bazelrc,Cargo.toml,.cargo/config.toml,.config/nextest.toml'
fix_strategy: build-once-test-natively
title_template: 'The slowest platform compiles its own tests'
category: 4
---

# OPT89 — The Slowest Platform Compiles Its Own Tests

**Not auto-detected yet.** Like OPT86 and OPT88, no pass emits OPT89, so the audit never reports it on its own and never claims to have checked it. No audit phase applies it; a reader applies it by hand, reading the slowest platform leg's steps against the heuristic below.

**TL;DR**: A multi-platform test matrix makes every platform build its own test binaries, so the slowest or scarcest runner (Windows, an ARM machine, an emulated architecture) spends most of its time compiling instead of testing. Build the test binaries once on a fast runner (cross-compiling for the target), hand them over as an artifact, and run them natively on each platform.

**Anti-pattern**: The platform legs of a matrix each run the full build-and-test, and one leg is the long pole because its runner is slow at compiling, or because the architecture is emulated (for example an ARM build under QEMU on an x86 runner). The compile is the same work on every leg; only running the tests needs the target platform.

**Relation to other patterns**: [OPT81](#opt81--the-same-job-is-measurably-faster-on-another-runner) moves a whole job to a faster runner class. OPT89 moves only the build, and keeps the tests on the platform they must run on. [OPT14](#opt14--repeated-checkoutsetup-without-artifact-handoff) is the same artifact-handoff idea for setup work inside one platform.

**Detection heuristic** (manual; read, never inferred):

1. A matrix runs the same build-and-test on several platforms or architectures, and one platform leg is the slow outlier.
2. On that leg, the build or compile work (or an emulation setup step such as QEMU) takes most of the job, and running the tests is a minority of it. When compile and test share one step (`bazel test`, or a test command that compiles first), step timings cannot split them: read the run log, a build-timing artifact (Bazel's profile, cargo's `--timings` report), or a test-only run of the same tests on another runner before concluding.
3. The toolchain can build for that target elsewhere: cross-compilation is supported (for example Rust targets, Go `GOOS`/`GOARCH`, a C or C++ cross toolchain), or the test runner can package built tests as an archive to replay elsewhere (for example `cargo nextest archive`).

**Fix recipe**:

1. Build the test binaries for the slow target on a fast runner, cross-compiling, and upload them as an artifact (a test archive where the runner supports one).
2. Download and run them on a native runner of the target platform; shard the run across several native runners when the test phase is still long.
3. Where a native runner exists, prefer it to emulation: an emulated architecture is slower at both building and testing.
4. Keep a fully native build-and-test of that platform somewhere, after merge or on a schedule, for anything the cross-built binary cannot cover.

**Guardrail**: a cross-compiled binary is not always the binary a native build produces (different linker, toolchain or bundled native libraries). List what the cross-built tests do not exercise, keep a native run that covers it, and do not drop a test from the native platform because it does not build cross.

**Sizing: uncredited**. No `_SIZING` model. The ceiling is the measured build share of the slow leg; the saving is what the next sampled runs show.

**Real-world example (OpenAI)**, their numbers, not a forecast for your repo:

- openai/codex [#20585](https://github.com/openai/codex/pull/20585) (2026-05-01): Windows test builds moved to remote Linux build execution, and the change "keeps `TestRunner` actions local on the Windows runner". The PR reports a "Baseline native Windows Bazel test on `main`: success in about 28m12s" (the linked job is named "Local Bazel build on windows-latest for x86_64-pc-windows-gnullvm", so build and test ran in it together) against a cross-compiled run's "Windows test 9m16s", both linked CI runs. The two are not like for like: the baseline was a push to `main`, and the cross-compiled PR leg leaves out the V8 and code-mode unit tests, so part of the gap is fewer tests. The fully native Windows job stayed, on pushes to `main`, to keep the coverage the cross build lacked.
- openai/codex [#23358](https://github.com/openai/codex/pull/23358) (2026-05-20): the Windows ARM64 test archive is "cross-compiled on Windows x64 and replayed on native Windows ARM64 shards so the slow ARM64 machine is used for execution rather than compilation". Completed runs took "`34m30s`" and "`30m36s`" end to end, where "comparable pre-cross-compile sharded Windows ARM64 runs were `55m01s`, `50m21s`, and `46m42s`".
- openai/tiktoken builds its ARM64 wheels on native `ubuntu-24.04-arm` runners instead of under QEMU emulation (commit 853da97, "Improve aarch64 and mac builds (#380)"; no timing published).

**Risk**: **MEDIUM**. Reversible, but a cross-built binary can hide a platform-specific build or link problem; the native run in step 4 is what catches it.
