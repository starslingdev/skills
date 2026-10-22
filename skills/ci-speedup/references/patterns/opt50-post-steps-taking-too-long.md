---
pattern: OPT50
impact: MEDIUM
class: data-driven
detector: manual
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: post-steps-taking-too-long
title_template: 'Post Steps Taking Too Long'
category: 10
---

# OPT50 — Post Steps Taking Too Long

**Anti-pattern**: GitHub Actions "Post" steps (cache save, cleanup) taking excessive time, often due to large cache uploads.

**Detection heuristic**:

- Look for "Post" steps in timing data with duration >30s
- Check cache sizes being uploaded

**Fix**: Reduce cache scope, exclude unnecessary files, or use more granular cache keys.

---
