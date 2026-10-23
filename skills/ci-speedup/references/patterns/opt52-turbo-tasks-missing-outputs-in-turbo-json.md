---
pattern: OPT52
impact: MEDIUM
class: static
detector: repo-file-check
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: turbo-tasks-missing-outputs-in-turbo-json
title_template: 'Turbo Tasks Missing `outputs` in turbo.json'
category: 11
aliases:
  - opt52--turbo-tasks-missing-outputs-in-turbo-json
alias_note: 'Alias anchor: findings files stamped before the GFM anchor fix link this older slug; keep it so those deep links still land.'
---

# OPT52 — Turbo Tasks Missing `outputs` in turbo.json

**TL;DR**: A Turbo task declares no outputs, so Turbo can never cache its result — it re-runs in full every time.

**Anti-pattern**: Turbo tasks without `outputs` configured in `turbo.json`, meaning Turbo can't effectively cache the task results.

**Detection heuristic**:

```bash
# Parse turbo.json and check each task for outputs
cat turbo.json | jq '.tasks // .pipeline | to_entries[] | select(.value.outputs == null or (.value.outputs | length == 0)) | .key'
```

**Fix**: Add appropriate `outputs` globs to each task in `turbo.json`.

---
