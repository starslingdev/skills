---
pattern: OPT85
impact: HIGH
class: structural
detector: critical-path
risk: MEDIUM
affected_files: 'package.json,pyproject.toml,pytest.ini,setup.cfg,tox.ini,conftest.py,Cargo.toml,.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: run-tests-in-fewer-processes
title_template: 'Repeated work starts a new process or worker pool for every item'
category: 14
---

# OPT85 — Repeated Work Runs in Many Processes Instead of Bounded Batches

**Auto-detected from a drilled log.** The OPT85 startup leaves require at least ten timestamped, strictly serial iterations from a declared tool signature. They measure process boundary → first work and boundary → completion for every iteration. For non-test loops, the structured boundary must also name one repeated tool, its `build` or `scan` category, and a unique item; pool boundaries name one pool and a unique case. This category is checked against the pole's measured dominant step, so startup in a shorter test step cannot explain a build-dominant job. Unparseable timestamps, mixed tools, overlapping iterations, retries and periodic progress lines fail closed.

**TL;DR**: A slow job spends most of each serial iteration starting the same tool process or worker pool. Keep a bounded pool alive across cases, or process a memory-capped batch of items per tool start. Tests still keep every case, while packaging and analysis loops still keep every input.

**Anti-pattern**: Four shapes of the same cost.

1. **A process per test case.** A parametrized suite whose every case shells out (`subprocess.run([sys.executable, "checker.py", ...])`, a CLI entry point, a helper script or a stub binary), so each case pays interpreter start, imports and compilation before any logic runs. With hundreds or thousands of cases, the startup is the suite.
2. **A binary per test file (Rust).** Every file directly under a crate's `tests/` directory is its own integration-test target. Each one is linked against the crate and its dependencies, and `cargo test` runs the binaries one after another, so tests only run in parallel inside a binary.
3. **A tool process per item.** A packaging, build or analysis loop launches the same interpreter or compiler for every input, even though a bounded child can safely handle several inputs before exiting.
4. **A worker pool per case.** Each case starts and tears down the same worker pool instead of resetting case-scoped state and retaining the bounded workers until the suite's final teardown.

**Relation to OPT78**: same family (setup paid per unit instead of once), different mechanism. [OPT78](#opt78--per-file-test-isolation-rebuilding-shared-module-state) is vitest's per-file module registry, a setting inside one test process, emitted by its own log leaf; its scope note keeps it vitest-only, and this entry does not widen it. OPT85 is the operating-system process or the linked binary, whatever the runner.

**Detection heuristic**:

1. The drilled long pole's dominant step category is `build`, `scan` or `test`. This is a gate only: it holds before and after a regression alike, so it cannot identify one by itself.
2. Process per case: a declared pytest invocation, Cargo test-binary banner, or structured verification-script boundary starts each iteration; its first-work and completion lines are timestamped.
3. Tool/pool loop: structured `started`, `ready` and `finished`/`stopped` lines carry the same tool or pool identity and a unique item or case identity. A tool loop also declares `build` or `scan`; that exact category must dominate the pole. A mixed-tool or mixed-category loop is withheld rather than treated as reusable startup.
4. For either measured shape, at least ten unique iterations must be strictly serial. Median startup must be at least half the median iteration, and count × median startup must reach 20 seconds.
5. Binary per file: the crate has several `tests/*.rs` files, and the captured log shows one `Running tests/<name>.rs` line per file. Corroborate with timing: the link time per binary (`cargo test --no-run` timed, or `cargo build --timings`) and the gaps between binaries in the log.

The shape alone is not a finding. The measured share of the step it accounts for is the claim.

**Fix recipe**:

- **Process per case**: load or compile the code under test once per worker (a session- or module-scoped fixture), and run each case in-process with fresh state: new globals or a fresh module namespace, the case's existing fixtures, and every piece of process state the case can touch (environment variables, working directory, `sys.argv`, patched modules, signal handlers) saved before and restored after it. Keep a small set of real subprocess cases for whatever depends on the process boundary: argument parsing, exit codes, stdout and stderr wiring, signal handling, and isolation itself.
- **Binary per file**: move the integration tests into one test target, for example `tests/all/main.rs` declaring each former file as a module (`mod foo;`). Every test keeps its body and gains a module path. The crate is linked once, and the harness runs all tests in parallel.
- **Tool process per item**: safest first, put a measured memory ceiling on each batch, keep the existing per-item output and failures, and send several items to one tool process. Compare produced bytes or reports before increasing the cap.
- **Worker pool per case**: reset case-scoped state after every case, retain a fixed-size pool across cases, and close it in the suite's final teardown. Keep a canary that detects state leaking from one case into the next.
- Either way, turn on slow-test reporting (`pytest --durations=N` in the configuration, or the runner's timing report) so the next regression of this shape is visible in the log.

**Mandatory guardrail (this pattern is invalid without it)**: the collected test list before and after must match (diff the collect-only listing; a Rust conversion adds a module path to every test name, so map each old name to its new one and check that none is lost or duplicated); each case must still execute the same code path; state a case can leak must be reset explicitly. Put a deliberately broken iteration at several positions and require every position to fail, then add a state-leak canary. The converted suite must also pass in random order where the runner can shuffle (for example with pytest-randomly). Stable Rust's test harness cannot shuffle, so run the converted binary both with `--test-threads=1` and with the default parallelism and require both green.

**No-weakening rail**: never drop, merge or skip cases, and never skip setup a case needs, to make the suite fit in one process. Cases that test process behaviour stay real subprocesses.

**Failure mode, plainly**: one process lets a case pass only because an earlier case left a global, an environment variable or a patched module behind. Separate processes made that impossible; the conversion makes it possible, so the reset and the order checks are what keep a green meaning something.

**Conservative rollout (REQUIRED)**: convert one suite at a time; run the old and new paths side by side over real PR traffic and compare case lists and results; keep the subprocess path for the boundary cases permanently.

**Sizing**: the modeled saving is `N × median startup − one startup`; the first process remains. Running independent iterations concurrently is an alternative on those same measured seconds, never an added saving. **Benchmark first.**

**Runner-minute effect**: **saving** — batching or retaining the bounded pool removes measured startup work from the same runner. The exact monthly saving remains unsized when the drilled log has no monthly execution count.

**Real-world example (OpenAI)**, their numbers, not a forecast for your repo:

- openai/openai-python [#3830](https://github.com/openai/openai-python/pull/3830) (2026-09-09): a 1,760-case dependency-policy matrix had each case launch "a Python checker and up to four Python Git stubs". A harness comparison "measured median 180ms per subprocess case versus 0.9ms in-process". The fix compiles the checker "once per pytest worker" and runs each case "with fresh globals", keeping "real subprocess execution for CLI parity cases". The Python 3.14 test job went from 2m24s before the regression, to 7m00s with it, to 2m41s after the fix.
- openai/codex [#2633](https://github.com/openai/codex/pull/2633) (2025-08-24): moving every file in the crate's `tests/` into "a single suite in order to have a single binary" took `cargo test -p codex-core` from "8:49.80 total" to "21.407 total". Both runs were measured "hot", on a second run "to exclude compile times"; it is a developer-machine `time` measurement, not a CI run.
- openclaw/openclaw [#200290](https://github.com/openclaw/openclaw/pull/200290) (2026-11-17): batched 55 plugin builds into 14 bounded child-process starts (up to four plugins each). The PR reports median elapsed time dropping from 64.51s to 40.62s and requires byte-identical artifacts; those are that PR's samples, not a forecast for another repository.
- openclaw/openclaw [#196117](https://github.com/openclaw/openclaw/pull/196117) (2026-11-14): retained a bounded worker pool across cases, added case-scoped database cleanup, and closed the workers in final teardown. Its local comparison reports 174.152s to 97.131s over 96 cases; it is one local pair, not a runner-billing measurement.

**Risk**: **MEDIUM**. Reversible, but in-process execution can let one case's leftovers decide another's result. Never list as a quick win.
