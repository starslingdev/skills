---
pattern: OPT13
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: build-step-in-jobs-that-dont-need-it
title_template: 'Build Step in Jobs That Don''t Need It'
category: 2
---

# OPT13 — Build Step in Jobs That Don't Need It

**TL;DR**: A job runs a full build even though it only lints or type-checks and doesn't need the built output.

**Anti-pattern**: Running a full build in jobs that only need type-checking, linting, or running tests against source (not built output).

**Detection heuristic**:

```bash
# Check turbo.json for task dependencies
# If 'test' depends on 'build' in turbo.json, the build is required
# If not, check if the job explicitly runs build before test
```

**Fix**: Remove unnecessary build steps. If using Turbo, ensure `turbo.json` task graph is accurate so Turbo skips unneeded builds.

**GUARDRAIL — Framework build steps (`next build`, `nuxt build`, `vite build`, `remix build`, `astro build`, `gatsby build`, `ng build`, `nest build`, `expo export`, `react-scripts build`, `webpack`/`rollup`/`turbopack`/`rspack` production builds, `tsc -b`/project-references full builds, `mvn package`, `gradle assemble`, etc.):**

A framework build is NOT just a typecheck. Before recommending deletion, you MUST enumerate what the build does that the proposed replacement does not. For example, `next build` performs:

- Route validation (catch broken `app/`/`pages/` exports, invalid route configs, conflicting routes)
- Page-data collection / static analysis for `getStaticProps`/`generateStaticParams`
- React Server Component bundling and "use client"/"use server" boundary checks
- Code splitting, dead-code elimination, tree-shaking — surfaces unresolved imports a typecheck misses
- Codegen (e.g. `.next/types`, route types, manifests)
- Env-var validation for `NEXT_PUBLIC_*` (compile-time inlining)
- Image / font / asset processing
- Production-only minification and bundler errors
- Runtime config validation (middleware, edge functions, ISR config)

`tsc --noEmit`, `eslint`, `vitest`, `jest`, etc. cover only a strict subset. Treat them as **partial substitutes** — never claim they preserve the same gating signal as the build.

**Required handling for framework-build deletion findings:**

1. **Default to "shrink the build", not "delete the build".** Recommend cheaper variants first:
   - `next build` → `--no-lint` (lint runs separately), disable telemetry (`NEXT_TELEMETRY_DISABLED=1`), strip dev-only env stripping, trim `output: 'standalone'` if unneeded, prune unused locales, cache `.next/cache` across runs.
   - Other frameworks: equivalent flags (e.g. `vite build --minify=false` for non-prod gating, `gradle assemble -x test`, etc.).
2. **Severity cap.** A "delete the framework build" finding is capped at **MEDIUM** severity, regardless of measured wall-time savings, unless the finding includes an explicit "the build does the following N things and the replacement covers all N" justification table. Without that table, the finding stays MEDIUM and is presented as an _option_, not a recommendation.
3. **Never frame `tsc --noEmit` (or any single tool) as equivalent gating.** It is a typecheck. If you suggest it, label it as "type-only gate; will not catch route, bundler, codegen, or env-var regressions that the deploy build catches later."
4. **Cite the deploy path.** If production deploys (Vercel, Netlify, Cloud Run, etc.) re-run the same build, note that pre-merge deletion shifts failure detection from PR time to deploy time and call out the worse signal / slower feedback explicitly as a tradeoff.
5. **Scope:** This guardrail applies ONLY to "remove a framework's production build/compile step" findings. It does NOT apply to:
   - Removing a redundant lint step that another job already runs
   - Removing sleep-based polling
   - Sharing a single build across jobs via artifacts or cache
   - Deleting a build that produces nothing the test/lint job consumes AND has no validation surface beyond what other steps already enforce (e.g., a stray `tsc -b` duplicating a typecheck job that already runs).

If the finding is "share/cache the build" or "build once, fan out artifacts", that is OPT14 / OPT15 territory and the guardrail above does not apply — those are still legitimate.

**GUARDRAIL — Runtime dependency on compiled output (D11 backstop):**

Before recommending removal of any pre-test build step (including the "stray `tsc -b`" carve-out above), the audit MUST grep the affected package's source for these four runtime-load patterns:

1. **Worker-thread spawn sites**: `grep -RnE 'new Worker\(|worker_threads' <pkg>/src` — Node's `worker_threads.Worker` resolves its target file at runtime, typically via `path.join(__dirname, "worker-thread.js")`. If `__dirname` resolves under the compiled `dist/`, the build is load-bearing.
2. **child_process exec/spawn/fork sites**: `grep -RnE 'child_process|\.fork\(|\.spawn\(|\.exec\(|\.execFile\(|\.execSync\('` — same hazard. `fork()` and `spawn()` of a node script under `dist/` need the dist to exist.
3. **Dynamic / relative-path require of `dist/` from non-test source**: `grep -RnE "require\([\"\']\.\.+/+dist|from [\"\']\.\.+/+dist|import\([\"\']\.\.+/+dist" <pkg>/src | grep -v '\.test\.\|\.spec\.'` — direct runtime imports of compiled output.
4. **`__dirname`-relative paths resolving under `dist/` at runtime**: `grep -RnE 'path\.join\(__dirname,.*\.js[\"\']' <pkg>/src` — common when a TS source file constructs a path that points into its compiled-output sibling.

If ANY non-test source contains the above, the build is load-bearing at runtime even if no test imports `dist/` directly. The finding must be **INVALIDATED**, not downgraded to "probe" — a probe that ships and then silently regresses in production (because the test that would exercise the worker path didn't run, or the Worker pool init silently caught the failure) is worse than no finding at all.

**Coverage table addition** (extends the existing framework-build coverage table requirement): for any build-removal finding, the coverage table MUST enumerate Worker-thread spawn sites, `child_process` exec sites, and `require()`/`import()` of `../dist/...` from non-test source. Cite each as either "0 hits" or "N hits at file:line — see Finding N evidence". A missing or empty coverage table is a Phase 6 fail.

**Worked counter-example.** The 2026-05-19 langfuse audit's Finding 15 shipped a "drop `pnpm --filter=worker... run build`" recommendation as a MEDIUM-severity probe, citing ~150 min/mo. The D7 source-code safety check (DB-retry, HTTP-client, service-discovery patterns) returned zero hits, so the probe looked safe. Phase 6 round 2 ran the four-grep set above and found `worker/src/features/tokenisation/worker-thread.ts:4` requires `dist/features/tokenisation/usage.js` via a spawned Node Worker thread (`new Worker(path.join(__dirname, "worker-thread.js"))` inside `async-usage.ts` → `TokenCountWorkerManager`). The finding was INVALIDATED — a build that is load-bearing at runtime (a spawned Worker resolving its target under `dist/`) must never be proposed for removal, even as a probe.

---
