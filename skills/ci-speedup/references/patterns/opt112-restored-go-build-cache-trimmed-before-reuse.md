---
pattern: OPT112
impact: HIGH
class: data-driven
detector: same-key-go-cache-age-slowdown
risk: LOW
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml,**/*.go,go.mod,go.sum'
fix_strategy: restored-go-build-cache-trimmed-before-reuse
title_template: 'Restored Go Build Cache Trimmed Before Reuse'
category: 1
---

# OPT112 — Restored Go Build Cache Trimmed Before Reuse

**TL;DR**: `actions/setup-go` and `actions/cache` restore Go build-cache
entries with their saved modification times. `cmd/go` trims entries unused for
five days (plus a one-hour mtime granularity), at most once a day, when a Go
command finishes. Once the restored archive is past that limit, the job's first
Go command deletes every entry it did not itself use, and a later command with
different flags (for example a plain build after `-race` tests) recompiles what
the archive held. Mark the restored files as recently used before the first Go
command so `cmd/go` keeps both populations.

**Detection heuristic** (every gate required; reads only what an ordinary job
prints, no extra flags or markers):

1. A captured long-pole job log restores a Go build cache: setup-go's standard
   `Cache restored from key: setup-go-*` line, or an `actions/cache` step whose
   logged `path:` input names `go-build` or `GOCACHE`. Two different Go keys in
   one log, or a later step that touches the Go build cache (the fix already in
   place), withhold the finding.
2. GitHub's cache inventory returns exactly one archive for that key; its
   `created_at` splits the job's sampled successful runs into runs that
   restored it while it was younger than five days (fresh) and runs that
   restored it at least five days and one hour after creation (old).
3. At least two fresh and two old runs are confirmed from their own logs to
   restore that same exact key without a retention step.
4. The old runs' median job duration exceeds the fresh runs' median by at least
   30 seconds, and every old run is slower than the fresh median.
5. Missing or ambiguous cache metadata, too few runs in either window, or no
   slowdown keeps the finding quiet.

**Fix recipe**, safest first:

1. Immediately after `setup-go` or `actions/cache`, resolve `go env GOCACHE`,
   create it on a cache miss, and touch every restored file before any Go
   command runs.
2. Keep the existing cache key, flags, package set, race coverage, and failure
   behavior. Do not substitute a warm-up build: the warm-up can trim the other
   flag population before it is used.
3. Pin the retention step's position in a workflow contract test, then compare
   the same job before and after. Confirm the later build no longer recompiles
   the broad package set and that every test still runs.

**Guardrail**: touching cache entries changes only their access recency; it must
not turn an untrusted cache into executable input, weaken the cache key, skip a
race or non-race build, or hide a failed restore. On a cache miss, the retention
step is a no-op. Preserve Go's own cache validation and every existing test flag.

**Sizing**: the wall-clock saving is the measured median slowdown of old-archive
runs over fresh-archive runs of the same cache key, the same job and the same
dependency set. The runner-minute saving is stated as a ceiling: that slowdown
times the workflow's 30-day run count, because runs that restore an archive
younger than five days save nothing. The comparison is between sampled runs,
not a before/after of the fix, so confirm it on the same job after adding the
retention step.

**Real-world example (OpenClaw)**, their numbers, not a forecast for another
repository: openclaw/crabbox [#2833](https://github.com/openclaw/crabbox/pull/2833)
reported the first in-test CLI build falling from a 135-second mean to 30
seconds after retaining restored Go cache entries, while the race-shard test
time fell by about 110 seconds.

**Risk**: **LOW**. The change is reversible and leaves cache keys and test
commands intact, but its ordering is load-bearing: it must run after restore
and before the first Go command.
