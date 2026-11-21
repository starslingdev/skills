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

**Auto-detected from a drilled log.** The OPT85 startup leaves require at least ten timestamped, strictly serial iterations. Test subprocesses use the runner's ordinary start/work/completion output. Tool loops use repeated shell-trace commands (`+ ...` or `> ...`) with one argv prefix and a distinct final argument; startup is measured from each trace line to that process's first output line, and an invocation that prints nothing withholds the finding. Their category comes from the measured dominant step. Worker-pool loops use the runner's own start/ready/close lifecycle lines. Explicit crash isolation, fork-per-test, per-case process isolation, `isolate: true`, `--isolate` or `--forked` withholds the finding. Unparseable timestamps, mixed commands, overlapping iterations, retries, periodic progress lines and per-image scanner loops fail closed.

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
3. Tool/pool loop: timestamped shell traces repeat the same argv prefix with a unique last argument, or ordinary pool lifecycle lines bind one pool to a unique case. The pole's measured dominant step supplies the tool-loop category. Mixed tools and explicitly isolated cases are withheld rather than treated as reusable startup.
4. For either measured shape, at least ten unique iterations must be strictly serial. Median startup must be at least half the median iteration, and count × median startup must reach 20 seconds.
5. Binary per file: the crate has several `tests/*.rs` files, and the captured log shows one `Running tests/<name>.rs` line per file. Corroborate with timing: the link time per binary (`cargo test --no-run` timed, or `cargo build --timings`) and the gaps between binaries in the log.

The shape alone is not a finding. The measured share of the step it accounts for is the claim.

**Fix recipe**:

- **Process per case**: load or compile the code under test once per worker (a session- or module-scoped fixture), and run each case in-process with fresh state: new globals or a fresh module namespace, the case's existing fixtures, and every piece of process state the case can touch (environment variables, working directory, `sys.argv`, patched modules, signal handlers) saved before and restored after it. Keep a small set of real subprocess cases for whatever depends on the process boundary: argument parsing, exit codes, stdout and stderr wiring, signal handling, and isolation itself.
- **Binary per file**: move the integration tests into one test target, for example `tests/all/main.rs` declaring each former file as a module (`mod foo;`). Every test keeps its body and gains a module path. The crate is linked once, and the harness runs all tests in parallel.
- **Tool process per item**: safest first, put a measured memory ceiling on each batch, keep the existing per-item output and failures, and send several items to one tool process. Compare produced bytes or reports before increasing the cap.
- **Worker pool per case**: reset case-scoped state after every case, retain a fixed-size pool across cases, and close it in the suite's final teardown. Keep a canary that detects state leaking from one case into the next.
- **Isolation exception**: when crash isolation, fork-per-test or another per-case process boundary is part of the suite's correctness contract, keep it. Do not trade that contract for worker reuse; optimize work inside each isolated case instead.
- Either way, turn on slow-test reporting (`pytest --durations=N` in the configuration, or the runner's timing report) so the next regression of this shape is visible in the log.

**Mandatory guardrail (this pattern is invalid without it)**: the collected test list before and after must match (diff the collect-only listing; a Rust conversion adds a module path to every test name, so map each old name to its new one and check that none is lost or duplicated); each case must still execute the same code path; state a case can leak must be reset explicitly. Put a deliberately broken iteration at several positions and require every position to fail, then add a state-leak canary. The converted suite must also pass in random order where the runner can shuffle (for example with pytest-randomly). Stable Rust's test harness cannot shuffle, so run the converted binary both with `--test-threads=1` and with the default parallelism and require both green.

**No-weakening rail**: never drop, merge or skip cases, and never skip setup a case needs, to make the suite fit in one process. Cases that test process behaviour stay real subprocesses.

**Failure mode, plainly**: one process lets a case pass only because an earlier case left a global, an environment variable or a patched module behind. Separate processes made that impossible; the conversion makes it possible, so the reset and the order checks are what keep a green meaning something.

**Conservative rollout (REQUIRED)**: convert one suite at a time; run the old and new paths side by side over real PR traffic and compare case lists and results; keep the subprocess path for the boundary cases permanently.

**Sizing**: the modeled saving is a ceiling, never a promise. Without a known batch cap it is at most `(N − 1) × median startup`; the first process remains. With cap `C`, it is `(N − ceil(N/C)) × median startup`, so every batch's startup remains. Multiply that measured ceiling by the workflow's 30-day run count when it is available; otherwise state that the monthly saving is unsized because the count is unavailable. Running independent iterations concurrently is an alternative on those same measured seconds, never an added saving. **Benchmark first.**

**Runner-minute effect**: **saving** — batching or retaining the bounded pool removes measured startup work from the same runner. Report it as **at most** the measured monthly ceiling and never credit the process starts that remain.

**Public evidence**, exactly as reported by the source issues, not a forecast for your repo:

- openclaw/openclaw [#200290](https://github.com/openclaw/openclaw/pull/200290): median elapsed time changed from 64.5 seconds to 40.6 seconds.
- openclaw/openclaw [#196117](https://github.com/openclaw/openclaw/pull/196117).

**Risk**: **MEDIUM**. Reversible, but in-process execution can let one case's leftovers decide another's result. Never list as a quick win.
