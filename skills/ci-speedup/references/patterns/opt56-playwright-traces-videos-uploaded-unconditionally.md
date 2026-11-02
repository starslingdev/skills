---
pattern: OPT56
impact: MEDIUM
class: static
detector: yaml-path
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: playwright-traces-videos-uploaded-unconditionally
title_template: 'Playwright Traces/Videos Uploaded Unconditionally'
category: 11
---

# OPT56 — Playwright Traces/Videos Uploaded Unconditionally

**Anti-pattern**: Playwright configured to always capture traces and videos, even for passing tests. This adds storage and upload time.

**Detection heuristic**:

```bash
# Check playwright config for trace/video settings
grep -rn 'trace:\|video:' playwright.config.*
# Flag if set to 'on' instead of 'on-first-retry' or 'retain-on-failure'
```

**Fix**: Set `trace: 'on-first-retry'` and `video: 'retain-on-failure'` in Playwright config.

---
