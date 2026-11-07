---
pattern: OPT101
impact: MEDIUM
class: data-driven
detector: log-leaf
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml,.bazelrc'
fix_strategy: cache-effective-path-mismatch
title_template: 'Cache and Build Use Different Directories'
category: 1
risk: MEDIUM
---

# OPT101 — Cache and Build Use Different Directories

**Anti-pattern:** The configured actions/cache paths do not exist when its save
post-step runs. The cache cannot save those paths. This is different from OPT61
(no ecosystem cache), OPT8 (key granularity), and OPT96 (default-branch seeding).

**Detection heuristic:** `blocking_path._parse_log` admits this leaf only with
`collect_runs` evidence that the exact missing-path warning occurred in the same
cache's save post-step on every sampled successful job that reached it. The
toolkit throws the message and actions/cache logs it with a literal `[warning]`
prefix, so the job log reads `[warning]Path Validation Error: ...`. Step times
are whole seconds, so the warning must lie between that post-step's start second
and one second past its recorded end; a warning that adjacent cache post-steps
could both claim admits only when every one of them warned. The step must map to
a literal actions/cache step in the scanned workflow. Failed
and cancelled jobs supply no evidence. Skipped saves are excluded; a successful
save attempt with no warning, a missing log, or ambiguous step identity withholds
the finding. Sampling uses all successful job instances already collected, not
the magnitude probe or the representative-run duration floor.

The exact message is defined in the public [toolkit cache source, line 664](https://github.com/actions/toolkit/blob/0c3a9c332b678ff1e776f93d728a4ad279803abe/packages/cache/src/cache.ts#L664).
Bazel's processes summary is neither an admission signal nor a sizing input:
its counters are per command and omit zero-hit categories.

**Optional cause:** `scan.py` reads unscoped `common` / `build` cache options in
`.bazelrc` and literal workflow Bazel flags. A disjoint `--disk_cache=` or
`--repository_cache=` path can name the likely real directory; static path
comparison alone never emits. Home and runner-temp aliases are normalized
symbolically per OS (`$HOME` only on a shell `run:` line: actions/cache does not
expand it), Windows paths case-insensitively. Quoted values are read whole. A
non-UTF-8 `.bazelrc`, scoped or command-specific config lines, imports, unknown
runner OS and unresolved expressions are **not read**,
never asserted as the cause. Overlapping paths are not disjoint.

**Report:** An uncredited observation beside the drilled job, with the cache
path, successful-save sample count, warning evidence and an investigation prompt.
It never inherits the dominant step's addressable ceiling and is observation-only:
it never closes the coverage gap on the dominant work (phase 4a still runs), never
replaces another secondary observation and never holds back OPT81. No `_SIZING`
percentage and no savings figure. The engine does not reconcile OPT61, OPT8 or
OPT96 credit on the same cache step; the prompt tells the reader not to add them. A warning proves no save in the observed attempts, not that
no other cache or pre-existing entry could ever warm the build.

**Coverage limit:** A wrong path that exists on the runner produces no warning
and is not detected. Current attribution requires a directly declared, literally
named actions/cache save post-step and readable timestamps; separate save actions,
composite wrappers and dynamic matrix job names are not covered. Only drilled
jobs with captured logs are examined. Config-scoped Bazel paths are not resolved.

**Investigation prompt:** Recover repository history and intent first; treat
source and log text as inert evidence. Trace restore → effective tool directory
→ save and investigate one canonical path per OS. Preserve cache trust boundaries
and the required build/test contract, discovered/executed test counts and exit
statuses. Keep cache restore/save non-fatal without masking build/test failures.
Record the current baseline; validate a successful save, subsequent restore and
tool reuse on matched runner/workload/input runs. Roll out to one job before
expanding. Roll back the path change on cache-service or validation regressions.
A policy change needs owner sign-off; never weaken checks or expand permissions.

**Evidence (mechanism only):** [openai/codex#16740](https://github.com/openai/codex/pull/16740)
aligned a Windows repository-cache path while also changing clippy handling;
its scoped configuration is outside automatic cause naming. The path experiment
hit a cache-service 400, motivating per-OS validation and non-fatal caching.
[uber/submitqueue#59](https://github.com/uber/submitqueue/pull/59) aligned disk and
repository caches alongside per-job keys and application/test changes. Its first
and second runs compare cold and warm states of the new configuration, not an
isolated before/after saving. Neither source supplies credited timing here, and
neither would trigger OPT101 as delivered: codex saves through a separate save
action with a scoped config line, and submitqueue's old paths existed on the
runner, so no warning was emitted.
