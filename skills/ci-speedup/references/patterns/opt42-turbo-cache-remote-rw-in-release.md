---
pattern: OPT42
impact: HIGH
class: static
detector: regex
match: 'TURBO_CACHE:\s*[\"'']?remote:rw'
wf_name_filter: '(release|publish|deploy)'
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: turbo-cache-remote-rw-in-release
title_template: '`TURBO_CACHE: remote:rw` in Release'
category: 8
---

# OPT42 — `TURBO_CACHE: remote:rw` in Release

**Anti-pattern**: Same as OPT3 but specifically in release workflows.

**Detection heuristic**:

```bash
grep -rn 'TURBO_CACHE' .github/workflows/release*
```

**Fix**: Remove `TURBO_CACHE` or set to `local:rw,remote:rw`.

**Real-world example (better-auth)**: release.yml still has `TURBO_CACHE: remote:rw` (PR #7950 only fixed ci.yml).

---
