---
pattern: OPT112
impact: HIGH
class: data-driven
detector: old-go-cache-broad-recompile
risk: LOW
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml,**/*.go,go.mod,go.sum'
fix_strategy: restored-go-build-cache-trimmed-before-reuse
title_template: 'Restored Go Build Cache Trimmed Before Reuse'
category: 1
---

# OPT112 — Restored Go Build Cache Trimmed Before Reuse

**TL;DR**: A job restores an exact `setup-go` build-cache archive older than
Go's five-day trim window. One Go command refreshes only its own flag-specific
entries, then a later incompatible command recompiles a broad package set that
the restored archive should have supplied. Mark the restored files as recently
used before the first Go command so `cmd/go` keeps both populations.

**Detection heuristic** (every gate required):

1. A captured long-pole job log contains the standard `Cache restored from key:`
   line for an exact `setup-go-*` key.
2. GitHub's cache inventory returns exactly one archive for that key, and its
   creation time is at least five days before the measured job.
3. Two successive `go build`, `go test`, or `go vet` command boundaries use
   incompatible race modes: one includes `-race` and the other does not.
4. The later command's ordinary `go -x` compiler output shows at least 50
   distinct packages whose first-to-last invocation spans at least 30 measured
   seconds. Work after the last compiler invocation is excluded. A cache hit,
   old archive, command shape, or compiler output by itself is not enough.
5. Missing or ambiguous cache metadata withholds the finding. A fresh archive,
   fewer compiler invocations, or the same race mode on both commands stays
   quiet.

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

**Sizing**: the timestamped first-to-last compiler-invocation span measures the
broad recompilation, excluding later tests and uploads. It is not credited as a
saving because GitHub's cache API does not expose the archive inventory: an
exact hit cannot prove that the later command's entries existed before the
trim. Runner-minute saving is therefore unsized until the same-job before/after
shows what retention removed; the report never credits the entire Go test step.

**Real-world example (OpenClaw)**, their numbers, not a forecast for another
repository: openclaw/crabbox [#2833](https://github.com/openclaw/crabbox/pull/2833)
reported the first in-test CLI build falling from a 135-second mean to 30
seconds after retaining restored Go cache entries, while the race-shard test
time fell by about 110 seconds.

**Risk**: **LOW**. The change is reversible and leaves cache keys and test
commands intact, but its ordering is load-bearing: it must run after restore
and before the first Go command.
