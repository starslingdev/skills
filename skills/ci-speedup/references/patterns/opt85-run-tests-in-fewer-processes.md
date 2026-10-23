---
pattern: OPT85
impact: HIGH
class: structural
detector: critical-path
risk: MEDIUM
affected_files: 'pyproject.toml,pytest.ini,setup.cfg,tox.ini,conftest.py,Cargo.toml,.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: run-tests-in-fewer-processes
title_template: 'Tests start a new process for every case or every test binary'
category: 14
---

# OPT85 — Tests Run in Many Processes Instead of One

**Not auto-detected yet.** No router emits OPT85 today, so `scan.py` lists it in `catalog_structural_patterns_without_detector` (as it does OPT74) and the audit never claims to have checked it. No audit phase applies it; a reader applies it by hand, reading the drilled long pole's log and the test code against the heuristic below.

**TL;DR**: The slowest test job spends most of its time starting processes rather than running tests: every test case launches a fresh interpreter or script, or every test file is linked into its own test binary. Running the same cases in one process per worker (or one binary) keeps every case and pays the startup once.

**Anti-pattern**: Two shapes of the same cost.

1. **A process per test case.** A parametrized suite whose every case shells out (`subprocess.run([sys.executable, "checker.py", ...])`, a CLI entry point, a helper script or a stub binary), so each case pays interpreter start, imports and compilation before any logic runs. With hundreds or thousands of cases, the startup is the suite.
2. **A binary per test file (Rust).** Every file directly under a crate's `tests/` directory is its own integration-test target. Each one is linked against the crate and its dependencies, and `cargo test` runs the binaries one after another, so tests only run in parallel inside a binary.

**Relation to OPT78**: same family (setup paid per unit instead of once), different mechanism. [OPT78](#opt78--per-file-test-isolation-rebuilding-shared-module-state) is vitest's per-file module registry, a setting inside one test process, emitted by its own log leaf; its scope note keeps it vitest-only, and this entry does not widen it. OPT85 is the operating-system process or the linked binary, whatever the runner.

**Detection heuristic** (manual; read, never inferred):

1. The drilled long pole's dominant step category is `test`. This is a gate only: it holds before and after a regression alike, so it cannot identify one by itself.
2. Process per case: the test code starts a process inside a parametrized test, a per-case fixture, or a helper either one calls (`subprocess`, `os.system`, `pexpect`, a CLI runner), and the collected case count is large (hundreds or more; count cases from the collect-only listing, not from suite pass totals, which differ across commits). Corroborate with timing before reporting: the runner's slowest-tests report (`pytest --durations=N`) shows many cases of similar duration well above what their logic should cost, or a profile of one case shows most of its time in process creation. The slow run often has no slowest-tests report (the fix may be what adds one); then corroborate by comparing the job's test-step duration with a known-good run of the same job, and by timing one case with and without the process call.
3. Binary per file: the crate has several `tests/*.rs` files, and the captured log shows one `Running tests/<name>.rs` line per file. Corroborate with timing: the link time per binary (`cargo test --no-run` timed, or `cargo build --timings`) and the gaps between binaries in the log.

The shape alone is not a finding. The measured share of the step it accounts for is the claim.

**Fix recipe**:

- **Process per case**: load or compile the code under test once per worker (a session- or module-scoped fixture), and run each case in-process with fresh state: new globals or a fresh module namespace, the case's existing fixtures, and every piece of process state the case can touch (environment variables, working directory, `sys.argv`, patched modules, signal handlers) saved before and restored after it. Keep a small set of real subprocess cases for whatever depends on the process boundary: argument parsing, exit codes, stdout and stderr wiring, signal handling, and isolation itself.
- **Binary per file**: move the integration tests into one test target, for example `tests/all/main.rs` declaring each former file as a module (`mod foo;`). Every test keeps its body and gains a module path. The crate is linked once, and the harness runs all tests in parallel.
- Either way, turn on slow-test reporting (`pytest --durations=N` in the configuration, or the runner's timing report) so the next regression of this shape is visible in the log.

**Mandatory guardrail (this pattern is invalid without it)**: the collected test list before and after must match (diff the collect-only listing; a Rust conversion adds a module path to every test name, so map each old name to its new one and check that none is lost or duplicated); each case must still execute the same code path; state a case can leak must be reset explicitly; and the converted suite must pass in random order where the runner can shuffle (for example with pytest-randomly). Stable Rust's test harness cannot shuffle, so run the converted binary both with `--test-threads=1` and with the default parallelism and require both green.

**No-weakening rail**: never drop, merge or skip cases, and never skip setup a case needs, to make the suite fit in one process. Cases that test process behaviour stay real subprocesses.

**Failure mode, plainly**: one process lets a case pass only because an earlier case left a global, an environment variable or a patched module behind. Separate processes made that impossible; the conversion makes it possible, so the reset and the order checks are what keep a green meaning something.

**Conservative rollout (REQUIRED)**: convert one suite at a time; run the old and new paths side by side over real PR traffic and compare case lists and results; keep the subprocess path for the boundary cases permanently.

**Sizing: deliberately uncredited**. No `_SIZING` model. The ceiling is measured, never assumed: per-case startup times the case count, or per-binary link time plus the gaps between binaries. **Benchmark first.**

**Real-world example (OpenAI)**, their numbers, not a forecast for your repo:

- openai/openai-python [#3830](https://github.com/openai/openai-python/pull/3830) (2026-09-09): a 1,760-case dependency-policy matrix had each case launch "a Python checker and up to four Python Git stubs". A harness comparison "measured median 180ms per subprocess case versus 0.9ms in-process". The fix compiles the checker "once per pytest worker" and runs each case "with fresh globals", keeping "real subprocess execution for CLI parity cases". The Python 3.14 test job went from 2m24s before the regression, to 7m00s with it, to 2m41s after the fix.
- openai/codex [#2633](https://github.com/openai/codex/pull/2633) (2025-08-24): moving every file in the crate's `tests/` into "a single suite in order to have a single binary" took `cargo test -p codex-core` from "8:49.80 total" to "21.407 total". Both runs were measured "hot", on a second run "to exclude compile times"; it is a developer-machine `time` measurement, not a CI run.

**Risk**: **MEDIUM**. Reversible, but in-process execution can let one case's leftovers decide another's result. Never list as a quick win.
