---
pattern: OPT3
impact: MEDIUM
class: static
detector: regex
match: 'TURBO_CACHE:\s*[\"'']?remote:ro\b'
wf_name_exclude: '(release|publish|deploy)'
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: turbo-cache-misconfiguration
title_template: 'Read-Only Turbo Cache'
category: 1
---

# OPT3 — Read-Only Turbo Cache (never populated)

**TL;DR**: A job sets `TURBO_CACHE: remote:ro` — it READS the remote cache but never writes it. If nothing else writes the cache, it stays cold and every task re-executes.

**Anti-pattern**: `remote:ro` means "remote only, read-only" — local cache is skipped AND this job never populates the remote cache. That's correct ONLY when a separate writer (e.g. the main-branch build with `remote:rw`) keeps it warm; a read-only cache with no writer is always a miss.

> **NOTE — `remote:rw` is NOT flagged.** On ephemeral CI runners (GitHub-hosted and most self-hosted) the local file-system cache does not persist between runs, so `remote:rw` (remote-only, read-write — the cross-run cache that actually helps) is the *correct* config, not a misconfiguration. OPT3 only flags the read-only (`ro`) case. The cross-workflow read-only-reader / read-write-writer *race* is OPT37's territory, not this single-file check.

**Detection heuristic**:

```bash
grep -rn 'TURBO_CACHE' .github/workflows/   # flag only value 'remote:ro'
```

**Fix**: If this job should populate the cache, set `remote:rw`. If it intentionally reads a cache a sibling job/workflow writes, confirm that writer exists (see OPT37) — otherwise the cache is never warm.

---
