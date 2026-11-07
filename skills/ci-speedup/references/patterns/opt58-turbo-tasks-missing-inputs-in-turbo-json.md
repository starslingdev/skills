---
pattern: OPT58
impact: MEDIUM
class: static
detector: repo-file-check
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: turbo-tasks-missing-inputs-in-turbo-json
title_template: 'Turbo Tasks Missing `inputs` in turbo.json'
category: 11
aliases:
  - opt58--turbo-tasks-missing-inputs-in-turbo-json
alias_note: 'Alias anchor: findings files stamped before the GFM anchor fix link this older slug; keep it so those deep links still land.'
---

# OPT58 — Turbo Tasks Missing `inputs` in turbo.json

**TL;DR**: A Turbo task declares no inputs, so unrelated edits (even a README) needlessly bust its cache.

**Anti-pattern**: Turbo tasks without explicit `inputs` configured in root or package-level `turbo.json`. Without `inputs`, Turbo hashes ALL git-tracked files in the package directory, so changes to test files, READMEs, `CHANGELOG.md`, `.eslintrc`, etc. invalidate the build cache unnecessarily. This is the counterpart to OPT52 (missing `outputs`): `outputs` controls what Turbo stores, `inputs` controls what Turbo hashes to compute cache keys.

**Detection heuristic**:

```bash
# Check root turbo.json for tasks without inputs
cat /tmp/turbo.json | jq '
  .tasks // .pipeline | to_entries[] |
  select(.value.inputs == null) |
  .key
'

# Find packages without turbo.json (inheriting root defaults, no inputs override)
# Use gh API tree endpoint to list all turbo.json locations
gh api "repos/{owner}/{repo}/git/trees/HEAD?recursive=1" \
  --jq '.tree[] | select(.path | test("turbo\\.json$")) | .path'

# Count packages that LACK a turbo.json (and therefore have no inputs override)
# Compare total package count vs packages with turbo.json
```

**Fix**: Two approaches (can be combined):

1. **Exclusion-based** (simpler) — Use `$TURBO_DEFAULT$` to keep default `.gitignore`-aware behavior while excluding non-build files:

```json
{
  "extends": ["//"],
  "tasks": {
    "build": {
      "inputs": [
        "$TURBO_DEFAULT$",
        "!**/*.test.*",
        "!**/*.spec.*",
        "!**/__tests__/**",
        "!**/*.md",
        "!vitest.config.*"
      ]
    }
  }
}
```

2. **Inclusion-based** (more precise) — List only files that affect build output:

```json
{
  "extends": ["//"],
  "tasks": {
    "build": {
      "inputs": ["src/**", "tsup.config.ts", "tsconfig.json", "package.json"]
    }
  }
}
```

Note: When `inputs` is set, Turbo opts out of `.gitignore` default behavior unless `$TURBO_DEFAULT$` is included. `package.json`, `turbo.json`, and lockfiles are always considered inputs regardless of the `inputs` setting.

**Real-world example (mastra)**: PR #14432 added explicit `inputs` to 35 packages that were missing them. Without `inputs`, every README or test file change invalidated build cache across the entire monorepo.

---
