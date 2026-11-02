---
pattern: OPT87
impact: HIGH
class: structural
detector: critical-path
risk: HIGH
affected_files: 'conftest.py,pyproject.toml,pytest.ini,.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: stub-slow-external-dependency-in-tests
title_template: 'Tests wait on a slow external dependency they do not verify'
category: 14
---

# OPT87 — Tests Wait on a Slow Dependency They Do Not Verify

**Not auto-detected yet.** No router emits OPT87 today, so `scan.py` lists it in `catalog_structural_patterns_without_detector` and the audit never claims to have checked it. No audit phase applies it; a reader applies it by hand, reading the drilled long pole's log and the test code against the heuristic below.

**TL;DR**: Tests on the slowest job call a real, slow dependency (a network API, a model endpoint, a package registry, a remote service) for behaviour the tests are not about, so they spend their time waiting on calls, timeouts and retries. Stub that dependency at its boundary in the pull-request suite, and keep a run against the real dependency elsewhere so the integration is still checked.

**Anti-pattern**: A unit or component suite reaches the network because a mock is missing, a fixture does not cover a new code path, or the suite was written against the live service from the start. Each test then waits for a response, a connection timeout or a retry budget, and the suite's runtime tracks the dependency's latency instead of the code under test. An evaluation or smoke workflow that calls a paid model for every case is the same cost, paid in both time and money.

**Relation to OPT19**: [OPT19](#opt19--test-source-sleep-dominance-includes-playwright-waitfortimeout) is a literal sleep in test source. OPT87 is waiting on an external call; the two often appear together (a retry loop with a sleep between attempts).

**Detection heuristic** (manual; read, never inferred):

1. The drilled long pole's dominant step category is `test`.
2. Per-test durations (`pytest --durations=N`, or the runner's equivalent) show many tests clustered near one value that matches a timeout or retry budget, or a few tests far slower than the rest.
3. The log or test code shows the external call: connection errors, DNS failures, retries or rate-limit responses that the test swallows; a real client constructed without a fixture override; or an API credential passed to the test step's environment.

**Fix recipe**:

1. Stub at the boundary the code already has (the client interface or adapter), with a fake that returns recorded or minimal realistic responses. Prefer one shared fixture over per-test patches, so a new test inherits the stub.
2. Make an un-stubbed call fail fast in the pull-request lane (block outbound sockets in the test session, with an allow-list for local services), so the next missing mock is a red test instead of a slow green one.
3. For model or evaluation calls, run pull-request CI against a dummy model that returns a fixed response, and spend the real model on a scheduled or post-merge run.
4. Keep the real-dependency run: the same tests, or a contract test of the boundary, against the real dependency on a schedule, after merge or in the merge queue, with someone alerted when it fails.

**Mandatory guardrail (this pattern is invalid without it)**: never mock away the thing the test exists to verify. A test whose subject is the integration itself (the client against the real API, the resolver against the real registry) moves to the real-dependency run; it is not stubbed. The real-dependency run must exist before the stub lands.

**No-weakening rail**: the same tests and the same assertions run after the change. A stub returns data recorded from the real dependency, with credentials, tokens and auth headers scrubbed before the recording is committed, refreshed when the dependency changes, never whatever makes the assertion pass.

**Failure mode, plainly**: the stub drifts from the real dependency, and the suite stays green while production breaks. The real-dependency run is what catches that drift; without it this pattern trades a slow suite for a blind one.

**Conservative rollout (REQUIRED)**: introduce the stub behind a fixture; run both the stubbed and the real-dependency paths over a stretch of PR traffic and compare results; only then drop the real calls from the pull-request lane.

**Sizing: deliberately uncredited**. No `_SIZING` model. The ceiling is the measured time of the affected tests, never a forecast. **Benchmark first.**

**Real-world example (OpenAI)**, their numbers, not a forecast for your repo:

- OpenAI's Core Experimentation team, ["Maintaining software with the GPT-6 family"](https://developers.openai.com/blog/maintaining-software-with-the-gpt-6-family): "adding a missing mock cut a 63-test resolver suite’s runtime from 592.6 seconds to 29.8 seconds—a 95% reduction, with all 63 tests retained." The post publishes the figure but does not name the repository or link CI runs.
- openai/evals commit [feed6c75](https://github.com/openai/evals/commit/feed6c75d47bdfb9ec0c8eea3ce9932934d47324) (2023-03-18), "Change CI to use a dummy model call": the workflow that smoke-tests each new eval stopped passing an API key and ran every new eval against `dummy-chat` and `dummy-completion` models with `--max_samples 10`, instead of a live model. No timing was published, and the 2023 run history is no longer retrievable.

**Risk**: **HIGH**. A stub can hide a real integration failure. Never list as a quick win.
