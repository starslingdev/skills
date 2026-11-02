---
pattern: OPT26
impact: LOW
class: static
detector: regex
match: 'uses:\s*actions/(checkout|setup-node|setup-python|cache|upload-artifact|download-artifact)@v[123]\b'
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: outdated-action-major-versions
title_template: 'Outdated Action Major Versions'
category: 5
---

# OPT26 — Outdated Action Major Versions

**Anti-pattern**: Using old major versions of actions (e.g., `actions/checkout@v3` when `v4` is available).

**Detection heuristic**:

```bash
# Extract all action references and compare to latest versions
grep -rn 'uses:' .github/workflows/ | grep -oP 'uses: \K[^@]+@[^ ]+'
```

**Fix**: Update to the latest major version. Check changelogs for breaking changes — the `upload-artifact`/`download-artifact` v3→v4 bump in particular is NOT drop-in: v4 artifacts are immutable and name-unique (a matrix where every leg uploads to the same artifact name fails on v4 — give each leg a unique name and merge on download), and `download-artifact@v3` cannot read v4 uploads, so bump both sides together.

---
