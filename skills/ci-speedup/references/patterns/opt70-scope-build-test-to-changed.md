---
pattern: OPT70
impact: HIGH
class: structural
detector: critical-path
risk: HIGH
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: scope-build-test-to-changed
title_template: 'Scope the long-pole build/test to changed targets'
category: 14
aliases:
  - opt70--scope-the-build-test-to-only-what-changed
alias_note: 'Alias anchor: findings files stamped before the GFM anchor fix link this older slug; keep it so those deep links still land.'
---

# OPT70 — Scope the Build/Test to Only What Changed

**TL;DR**: Your slowest job rebuilds and re-tests the whole project on every PR, even when the change touched only a small part — it could run just the parts that changed instead.

**Anti-pattern**: A build or test job that always processes every package / module / test file regardless of the PR diff, in a repo whose tooling supports change-scoped execution (a Turborepo/Nx/Bazel/Gradle workspace, or a test runner with a `--changed`/`--onlyChanged`/affected mode). The redundant-work ratio (build+install time ÷ the work actually exercised by the diff) is high — see OPT72.

**Detection heuristic** (routed, not YAML-matched):

1. The job is on the measured critical path (top of the PR check-runs list).
2. Its dominant step is a `build` or `test` category step (from the step-duration decomposition), and the redundant-work ratio is not high enough to route to OPT72.

The deterministic router stops there — it routes on **dominant category alone**, it does **not** inspect the repo's build config. Confirming a scoping mechanism exists is a per-candidate **reasoning step**, not part of detection:

3. *(reasoning step, not auto-detected)* Before recommending the scope, verify the repo's tooling supports change-scoped execution (`turbo.json` / `nx.json` / `WORKSPACE` / `settings.gradle` present, or a `--changed`/`--onlyChanged`-capable test runner). If no such mechanism exists, this candidate is not actionable as written.

**Why this is the most dangerous lever in the catalog.** Scoping trades correctness headroom for speed. Concretely it can:

- **Miss an undeclared / transitive dependency.** If the dependency graph the scoper reads (`turbo` task graph, `nx` project graph, `package.json` deps) is incomplete — a runtime `import`, a generated file, a path alias the graph doesn't know about — a change can affect a target the scoper marks "unaffected", and the gate passes without testing it.
- **Silently drop coverage.** A "speedup" that runs fewer tests is indistinguishable, in green-CI terms, from a real speedup — until a bug ships. The coverage regression is invisible in the metric you're optimizing.
- **Turn a build/import error into a false pass.** In any exit-code-driven gate, if the scoper resolves "nothing affected" it exits 0 — so a broken import or a build error in an "unaffected" area reads as a green check.
- **Diverge from the shipped artifact.** Testing unbuilt source while production ships `dist/` (subpath exports, build-time defines, custom transforms, `tsconfig` path remapping) means the gate validates something the user never runs.

**Fix recipe**: Adopt change-scoped execution for the dominant step, with the mandatory guardrail below. E.g. `turbo run build test --filter='...[origin/${{ github.base_ref }}]'`, `nx affected -t build test --base=origin/${{ github.base_ref }}`, or `vitest --changed origin/${{ github.base_ref }}`. The base must be the **merge base**, not `HEAD~1` — and it must EXIST in the clone: the default `actions/checkout` is a shallow, single-branch clone where `origin/${{ github.base_ref }}` does not resolve. Add `fetch-depth: 0` (or a targeted `git fetch origin ${{ github.base_ref }}` step) to the scoped job's checkout (an explicit exception to OPT28's shallow-checkout guidance). turbo/nx fail loudly on an unresolvable base, but `vitest --changed` against a missing ref can resolve to "no changed files" and exit 0 green — exactly the false pass Mandatory Guardrail #1 exists to prevent.

**Fail closed:** the always-running verdict must check every detector and intermediate job whose failure can suppress a required suite. Listing them in `needs:` is not an assertion. Accept a skipped suite only after successful detection with a valid explicit unchanged output; reject failures and cancellations, including an unchanged output written before failure. A strict suite-success assertion also rejects an upstream failure that skips the suite.

**Mandatory guardrail (this pattern is invalid without it)**:

1. **Full fallback on resolution error.** If the scoper errors, can't resolve the graph, or returns an empty set on a non-trivial diff, run the **full** build/suite. Never let "couldn't figure out what changed" become "ran nothing → green".
2. **Distinguish a build error from a test failure.** A non-zero exit from graph resolution / compilation must fail the gate, separately from "the scoped tests ran and failed". Don't collapse both into one exit code the scoper can zero out.
3. **Output-diff before adoption.** Before cutting over, run scoped and full in parallel and diff the artifact/coverage set; adopt only when they match on the dimensions that matter (built outputs, covered files).

**Conservative rollout (REQUIRED)**: Run the scoped job **in parallel with** the existing full job for **N runs** (≥1–2 weeks of PR traffic), comparing pass/fail and coverage on every PR. Cut over only after the scoped job has matched the full job across the diff distribution — including at least one PR that touches a shared/base package. Keep the full job on the merge queue / `main` even after cutover, so the trunk is always validated end-to-end.

**Sizing**: population-weighted Δ wall-clock = (dominant-step p50 − the scoped-run floor) × the share of PRs whose diff is narrow enough to scope, capped at the cross-workflow critical-path floor (the next-slowest check still gates the PR). Never size this off the single best-case PR.

**Risk**: **HIGH** — correctness exposure. NEVER list as a quick win.

---
