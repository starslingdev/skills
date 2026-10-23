---
pattern: OPT62
impact: HIGH
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: build-artifacts-destroyed-before-every-run
title_template: 'Build Artifacts Destroyed Before Every Run'
category: 12
---

# OPT62 — Build Artifacts Destroyed Before Every Run

**Anti-pattern**: Explicit `rm -rf` of build directories in workflow steps or build scripts, preventing incremental builds on self-hosted runners where the workspace persists.

**Detection heuristic**:

1. Search workflow steps for clean commands:

```bash
grep -rn 'rm -rf build\|rm -rf target\|rm -rf dist\|rm -rf node_modules\|cargo clean\|make clean\|gradle clean' /tmp/workflows/
```

2. **CRITICAL**: Also read the build scripts invoked by workflow steps. A workflow step may call `./scripts/build.sh` which internally does `rm -rf build`. The detection heuristic must trace through to the actual script, not stop at the workflow YAML.
3. Flag when the job runs on `self-hosted` runners (where workspace persists between runs). On GitHub-hosted runners, the workspace is always fresh, so `rm -rf` has no effect.

**Fix**: Make the clean step conditional on cache miss, or add an `--incremental` flag to the build script. For cmake: check for `CMakeCache.txt` existence before cleaning. For cargo: incremental compilation is the default — don't `cargo clean`.

---
