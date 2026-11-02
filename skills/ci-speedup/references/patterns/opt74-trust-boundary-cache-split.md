---
pattern: OPT74
impact: MEDIUM
class: structural
detector: critical-path
risk: MEDIUM
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: trust-boundary-cache-split
title_template: 'Untrusted fork-PR job redoes cold work it can''t cache securely'
category: 14
---

# OPT74 — Trust-Boundary-Forced Cold Work (producer/consumer split)

**TL;DR**: A fork PR can only *restore* what the base branch already published — anything it saves is scoped to its own pull request — so where the warm asset is behind repo secrets, or nothing on the base branch publishes it under a key the fork can compute, every fork PR redoes the full install/build from scratch.

**Anti-pattern**: A fork-PR-triggered job on the critical path whose setup can't be warm because the trust boundary denies it. Two constraints, routinely confused — keep them apart. **Save side**: a `pull_request` run saves into the merge ref's scope (`refs/pull/.../merge`), and per GitHub's dependency-caching reference such a cache "can only be restored by re-runs of the pull request" — so a fork PR warms nothing but itself. (This binds same-repo PRs identically; it is not what makes a fork colder.) **Restore side**: a fork PR **can** read the base branch — "If a workflow run is triggered for a pull request, it can also restore caches created in the base branch, including base branches of forked repositories" (same reference) — so a base-branch entry keyed on something the fork can compute, such as a lockfile hash, **does** hit, and a job like that is not this anti-pattern. What is fork-specific is what the fork cannot reach at all: it runs with a read-only `GITHUB_TOKEN` and **no repo secrets**, so a secrets-gated remote build cache (Turborepo/Nx Cloud, a registry-backed BuildKit cache, a private base image) is unavailable to it, and it cannot restore an upstream *feature* branch's own scope. Where the warm asset sits behind one of those, trust-boundary-forced cold work is structural, not a missing-cache hygiene bug.

**Detection heuristic** (routed): the job runs on `pull_request` (fork-reachable, not `pull_request_target`), has a high setup/build floor, and its warm asset is one a fork cannot reach — a secrets-gated remote cache, or a scope no fork run can restore. **Gate on this first, or the pattern fires on repos that are already warm:** check whether a trusted trigger (`push` to the base branch, or `schedule`) already publishes a GitHub Actions cache entry under a key the fork can compute — `actions/setup-*` with `cache:` enabled, or `actions/cache` on a `push` job, keyed on a lockfile hash, is the common case, and it restores fine on a fork PR. If such an entry exists, OPT74 does not apply. Where it doesn't, the cold setup is the addressable cost — but the naive fix (let the fork write the shared cache) is a **cache-poisoning** vector.

**Fix recipe**: **Trusted-producer + read-only-consumer split.** **First confirm one isn't already there** — if the base branch already publishes a restorable entry under a consumer-computable key, the split is in place and a second producer changes nothing. Otherwise: a trusted workflow (`push` to the base branch, or `schedule`) builds the shared dependencies/base image and publishes them **keyed by a ref the consumer can compute** (base-branch SHA, lockfile hash). The untrusted fork job **restores read-only** with a **local fallback**: on a cache miss it does the cold work rather than failing, so a fork PR is never blocked on the producer. The producer's output must be **content-addressed and validated** (the consumer recomputes/verifies the key from its own inputs) so a poisoned cache entry can't be served to the trusted side.

**Encode the cache-poisoning guardrails generally**: never let an untrusted job **write** a cache/artifact the trusted side reads; key shared artifacts by an input the consumer independently derives (not an attacker-controlled branch name); validate restored content before use; and keep the fallback path (cold build) always available so availability doesn't depend on the producer.

**Risk**: **MEDIUM** — security-sensitive. A careless split (untrusted job writing the shared cache, or trusted job consuming fork-produced artifacts) introduces a supply-chain hole worse than the slow CI it fixes.

**Guardrail**: The poisoning guardrails above are mandatory, not optional. If the split can't be made read-only-for-untrusted, do NOT recommend it — keep the cold work.

**Rollout**: Stand up the trusted producer first, confirm the consumer restores warm on same-base PRs and falls back cold on a forced miss, then measure.

---
